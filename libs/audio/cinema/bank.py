"""OpenAL side of the Cinema Speaker System: one source per speaker.

The renderer decides *what* each speaker should sound like; this bank owns
the OpenAL objects that play it. Keeping every native call here means the
jukebox transports only ever ask two questions -- "queue this frame for the
room" and "refresh the room" -- so their existing two-source code paths stay
exactly as they were and never grow a cinema branch of their own.

Every method that touches OpenAL runs on the audio owner thread, the same
thread that already owns the relay pump and the direct streamer's queueing.
Nothing here is called from the network or decoder threads.

Output gain mirrors the plain jukebox exactly, slot by slot:

    gain = (volume/100) * jukebox_mixer * cabinet * fade * distance_ramp

where the distance ramp is the same linear fade from reference to max
distance the two-source playback uses, so a cinema room at the cabinet is
neither louder nor quieter than the jukebox it replaces. What the bank adds
is that the ramp, the wall occlusion and the reverb zone are evaluated for
each speaker's own position instead of for the cabinet as a whole: a wall
behind the rear speakers must not muffle the screen wall.
"""

import contextlib
import functools
import threading
import time
from collections import deque

import cyal

from ...deferred_log import log_deferred as log_line
from .crossover import FULL_RANGE
from .crossover import apply as crossover_apply
from .crossover import crossover_hz
from .layout import ROOM_MAX_DISTANCE, ROOM_REFERENCE_DISTANCE
from .listener import (distance_gain, occlusion_filter, restore_filter,
                       speaker_aim_gain, speaker_filter)

# One pool and one queue allowance per speaker. The ratio between them keeps
# the same shape as the plain jukebox (32 buffers, 10 queued): enough head-
# room for the startup pre-buffer plus network jitter, and never enough to
# let a fast decoder run ahead of the room.
#
# The pool has to cover everything a speaker can legitimately be holding at
# once, or ``queue_frame`` refuses a frame -- and a refused frame is audio
# nobody ever hears (the "it jumps forward for a second" report, which the
# relay counts as a shed frame). What one speaker can hold:
#
#   10  the deepest queue the relay keeps before it sheds (its own cap)
#    4  the frames a pump hands over before it reclaims any of them
#    2  slack: the frame being rendered, and a ``realign`` fill
#
# plus, for a trimmed speaker, the whole-frame **hold** it waits out, which
# those same buffers carry (see ``hold_frames``: 100 ms is five 20 ms frames).
# 12 covered none of the above: a trimmed speaker's hold and a pump's worth of
# un-reclaimed buffers together asked for more buffers than a pool had, so the
# deepest trims dropped frames -- with the pool emptied, the room refilled and
# the trim landed correctly, which is why a delay showed up "minutes into the
# song" and looked like it had a mind of its own.
BUFFERS_PER_SLOT = 16
PREBUFFER_FRAMES = 4
RESUME_FRAMES = 3
MAX_QUEUED_FRAMES = 6

# The floor a *playing* room never lets its shallowest speaker reach.
#
# Every speaker of a room is handed one frame per frame and consumes one per
# frame, so they all carry the same queue depth -- until one of them empties,
# which is the moment the room stops being one unit: that speaker reports
# STOPPED, is left out of the feeding (see ``_feed_slots``) and only comes back
# through ``realign`` a pump later, while the speakers that kept playing did
# not. Two speakers playing the same song from different instants is heard as
# "the delay came and went on its own", and no listener-side maths can undo it
# once it is audible -- so the room holds instead (see ``_watch_low_queue``).
LOW_QUEUE_FRAMES = 2

# How many frames in a row must agree before a new frame size is believed.
#
# A frame's own size is what turns a millisecond trim into whole frames of hold
# plus a sample remainder, so re-measuring it on every frame lets a single odd
# frame silently re-measure every trimmed speaker's delay. The measured size is
# therefore latched (see ``_learn_frame_size``).
FRAME_SIZE_STABLE_FRAMES = 3

# How often the routine sink is told a room had to hold for a refill.
HOLD_REPORT_INTERVAL = 30.0

SAMPLERATE = 48000

# The cabinet's own playback uses its own category, not map music.
DEFAULT_CATEGORY = "jukebox"


def _serialized(method):
    """Run a room mutation under the bank's own lock.

    Frames arrive on the transport's thread while a speaker can be placed or
    deleted from the game thread, so the two must not interleave: without this
    a frame could be queued into a speaker that is being released.
    """
    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return wrapper


class CinemaSpeakerBank:
    """One positioned OpenAL source (and its buffer pool) per cinema slot."""

    main_thread_audio = True

    def __init__(self, game, renderer, *, volume=100, cabinet_volume=100,
                 reference_distance=ROOM_REFERENCE_DISTANCE,
                 max_distance=ROOM_MAX_DISTANCE,
                 occlusion_provider=None, reverb_slot=None, eq_slot=None,
                 category=DEFAULT_CATEGORY, duck=1.0,
                 buffers_per_slot=None, clock=None):
        self.game = game
        self.renderer = renderer
        # Which volume slider this room answers to. The cabinet's own playback
        # uses the jukebox category (deliberately separate from map music);
        # a room fed by something else passes its own, so one system's slider
        # never moves another system's sound.
        self.category = str(category or DEFAULT_CATEGORY)
        # Live duck multiplier (a megaphone broadcast lowering the music). It
        # is applied per frame by update_output() because a room has no single
        # source anyone could set a gain on.
        self.duck = float(duck)
        self.volume = max(0, min(100, int(volume)))
        self.cabinet_volume = max(0.0, min(1.0, float(cabinet_volume) / 100.0))
        self.reference_distance = float(reference_distance)
        self.max_distance = float(max_distance)
        self.occlusion_provider = occlusion_provider
        self.reverb_slot = reverb_slot
        self.eq_slot = eq_slot
        self.buffers_per_slot = int(buffers_per_slot or BUFFERS_PER_SLOT)
        # A room can be re-shaped (a speaker placed or deleted) while the
        # transport is pumping frames into it, from a different thread: the
        # structural changes and the queueing have to be serialized, or a
        # frame could land on a speaker that is being deleted.
        self._lock = threading.RLock()
        # The frames the room has queued, newest last, as the stereo source
        # frames they were rendered from. A speaker that joins the room, or
        # one that has to be put back in step with the others, is filled from
        # here -- which is the only way it can start at the room's own content
        # instant instead of a queue's worth behind (or ahead of) it. It is
        # also the audio a trimmed speaker is cut into (see `_delayed_window`),
        # so it has to stay deeper than the deepest trim the map can carry:
        # both transports deliver 20 ms frames and a trim is capped at 100 ms
        # (see `layout.MAX_TRIM_MS`), which this covers twice over.
        self._recent = deque(maxlen=max(self.buffers_per_slot, MAX_QUEUED_FRAMES))
        self._clock = clock or time.monotonic
        self._external_fade = 1.0
        self._fade_started = None
        self._fade_duration = 0.0
        self._environment_dirty = True
        self._slot_tier = {}
        # The map's own voicing last applied to each speaker, so a room whose
        # map was edited (or reloaded with the same speakers and a different
        # tone) re-voices them instead of keeping the old filter.
        self._slot_tone = {}
        # Which speakers carry a crossover mark, and the two things a *stream*
        # needs that an OpenAL filter does not: the state a crossover carries
        # from one frame to the next, and the frames it has already produced
        # (a delay trim is cut into audio the speaker really played, which for
        # a marked speaker is its own filtered history -- see ``crossover``).
        # Both are per *mark*, not per speaker: speakers dialled alike are fed
        # one filtered programme rather than one each, and a bass cabinet and a
        # tweeter are two different marks even when they meet at the same
        # frequency.
        self._slot_crossover = {}
        self._crossover_streams = {}
        self._occlusion_filters = {}
        self._retired = False
        self._stopped = False
        self.slot_sources = {}
        self._pools = {}
        self.last_output_at = None
        self.failure_reason = None
        self.frames_queued = 0
        self._frame_ms = 20.0
        self._plays_started = False
        # The frame size actually measured from the audio handed over. Latched:
        # a single frame of another size must never move a speaker's trim (see
        # ``_learn_frame_size``).
        self._frame_samples_seen = 0
        self._frame_size_streak = 0
        self.frame_size_changes = 0
        # A room that ran low holds every speaker until the depth is back (see
        # ``_watch_low_queue``), and a listener's own pause must never be
        # mistaken for one.
        self._refill_hold = False
        self._paused = False
        self.refill_holds = 0
        self._hold_reported_at = None
        # Frames the room had queued when its first speaker started playing.
        # A delay trim is measured from there, so it survives the room's own
        # start rules changing once playback has begun.
        self._start_frame = None
        self._build()

    # ------------------------------------------------------------- lifetime

    @property
    def anchor(self):
        return self.renderer.layout.anchor

    @property
    def sources(self):
        """Every speaker source, in room order (front wall first)."""
        return tuple(self.slot_sources[slot] for slot in self.renderer.slots
                     if self.slot_sources.get(slot) is not None)

    @property
    def spent(self):
        """True once this room is leaving service and must not be re-handed out.

        A room that is stopping, or already retiring because a song is being
        replaced, still holds live sources for another half second. A new song
        must never be given it: whatever fades or stops it would silence *the
        new song's* speakers, and the cleanup about to finish the teardown
        would delete them underneath it.

        ``_retired`` is set even when the fade ramp belongs to the transport
        (a relay receiver fades on its own pump and overwrites this bank's
        gain), because a room that is on its way out is on its way out
        whoever drives the ramp.
        """
        return self._stopped or self._retired or self._fade_started is not None

    @property
    def primary_source(self):
        sources = self.sources
        return sources[0] if sources else None

    @property
    def secondary_source(self):
        sources = self.sources
        return sources[1] if len(sources) > 1 else None

    def _build(self):
        audio = getattr(self.game, "audio_mngr", None)
        if audio is None:
            raise RuntimeError("cinema speakers need an audio manager")
        for slot in self.renderer.slots:
            self._pools[slot] = self._make_speaker(audio, slot)

    def _make_speaker(self, audio, slot):
        """Create one speaker: its source, its position and its buffer pool.

        The map's crossover for this speaker is read here, at creation, so a
        speaker that joins a room already playing (``reconfigure``) is born
        with the mark the map carries -- it used to be read only in
        ``_build``, which left every speaker a builder placed mid-song at full
        range for the rest of the track. It is a read, not a rule: what the
        number means is ``crossover_hz``'s, and a live change is applied by
        ``_sync_crossovers`` rather than by being created again.
        """
        context = audio.context
        source = context.gen_source()
        position = self.renderer.layout.position(slot)
        if position is not None:
            source.position = position
        # Same spatial contract as the plain jukebox pair: OpenAL's own
        # rolloff is off and the frame-by-frame gain ramp does the fading.
        source.rolloff_factor = 0.0
        source.reference_distance = self.max_distance
        source.max_distance = self.max_distance
        source.spatialize = True
        source.direct_channels = False
        source.gain = 0.0
        self.slot_sources[slot] = source
        # A song that starts mid-dive inherits the active water muffle
        # from its first frame (see camera._music_water_sources).
        active = getattr(audio, "filter", None)
        if active and active[-1] is not None:
            with contextlib.suppress(Exception):
                source.direct_filter = active[-1]
        pool = []
        for _ in range(self.buffers_per_slot):
            try:
                pool.append(context.gen_buffer())
            except Exception:
                break
        if not pool:
            with contextlib.suppress(Exception):
                source.delete()
            self.slot_sources.pop(slot, None)
            raise RuntimeError("cinema speakers could not allocate buffers")
        # Which stream this speaker is fed, recorded where the speaker is,
        # because a room's speakers are not all created at the same moment
        # (see the docstring). Written last, so a speaker that never came to
        # be (a dead context, no buffers) leaves no mark behind.
        mark = crossover_hz(self.renderer.layout.crossover(slot))
        self._slot_crossover[slot] = mark
        # A marked speaker joining a room that is already playing is filled
        # from this stream on the very frame it appears (``realign``), so it
        # has to exist with the frames the room still holds -- from the room's
        # own unfiltered history it would come back with the band it exists to
        # remove on its first queue.
        self._warm_stream(mark)
        return pool

    def _dispose_speaker(self, source):
        """Stop a speaker and give its OpenAL name back to the context."""
        if source is None:
            return
        audio = getattr(self.game, "audio_mngr", None)
        efx = getattr(audio, "efx", None)
        if efx is not None:
            for index in (0, 1):
                with contextlib.suppress(Exception):
                    efx.send(source, index, None)
        with contextlib.suppress(Exception):
            del source.direct_filter
        with contextlib.suppress(Exception):
            source.stop()
        try:
            limit = 64
            while source.buffers_processed > 0 and limit > 0:
                source.unqueue_buffers()
                limit -= 1
        except Exception:
            pass
        with contextlib.suppress(Exception):
            source.delete()

    @_serialized
    def stop(self):
        """Silence and drain every speaker; the player deletes the sources."""
        if self._stopped:
            return
        self._stopped = True
        self._reset_output(stamp=False)
        self.release_buffers()
        self._recent.clear()
        self._crossover_streams.clear()

    def release_buffers(self):
        """Drop buffer references so they are collected like any other pool."""
        for pool in self._pools.values():
            pool.clear()
        self._pools.clear()

    @_serialized
    def forget_sources(self):
        """Called once the owning player has deleted the OpenAL sources."""
        self.slot_sources.clear()
        self._recent.clear()
        self._crossover_streams.clear()
        self._stopped = True

    # ---------------------------------------------------------------- output

    @_serialized
    def queue_frame(self, left, right):
        """Render one stereo frame and queue it on every speaker it feeds.

        Buffers are reserved for every speaker *before* anything is uploaded,
        so a frame can never land on half the room: a partial frame is torn
        down rather than left to drift out of step with the other speakers.
        A speaker that already stopped while the rest play is skipped rather
        than fed (see ``_feed_slots``), so it rejoins on the live frame
        instead of replaying a growing backlog behind the room.
        """
        if self._stopped or not self.slot_sources:
            return False
        targets = set(self._feed_slots())
        programmes = self._programme(left, right, targets)
        feeds = self._slot_feeds(programmes, targets)
        if not feeds:
            return False
        claimed = []
        for slot, pcm in feeds:
            if slot not in targets:
                continue
            pool = self._pools.get(slot)
            if not pool:
                # Give back whatever was already claimed: a refused frame must
                # not quietly cost other speakers their buffers.
                for taken_slot, taken, _pcm in claimed:
                    self._pools[taken_slot].append(taken)
                return False
            claimed.append((slot, pool.pop(), pcm))
        if not claimed:
            return False
        queued = 0
        try:
            for slot, buffer, pcm in claimed:
                buffer.set_data(pcm, sample_rate=SAMPLERATE,
                                format=cyal.BufferFormat.MONO16)
                self.slot_sources[slot].queue_buffers(buffer)
                queued += 1
            self.frames_queued += 1
            self._commit(left, right, programmes)
            # One frame per speaker per call, so the queued depth IS a duration
            # once the frame's own size is known -- and the transports do not
            # agree on it (the direct streamer decodes 20 ms at a time, the
            # server relay hands 40 ms PCM frames). Measured from the audio
            # handed over, never assumed: see ``buffered_ms``.
            samples = len(left) // 2
            self._learn_frame_size(samples)
            self._watch_low_queue()
            return True
        except Exception:
            # Return whatever was not queued, then reset the whole room so
            # the next frame starts from a consistent queue on every speaker.
            self.failure_reason = "cinema speaker queue failed"
            for slot, buffer, _pcm in claimed[queued:]:
                pool = self._pools.get(slot)
                if pool is not None:
                    pool.append(buffer)
            self._reset_output()
            return False

    @_serialized
    def reclaim(self, *, drain_stopped=False):
        """Return finished buffers to their speaker's pool; True if any moved.

        Only a speaker that is *playing* -- or paused, which is OpenAL's own
        hold on it and still a truthful count -- is read for finished buffers.
        A **stopped** source reports *everything it holds* as processed
        (``AL_BUFFERS_PROCESSED`` counts a buffer finished the moment its
        source is not playing it), so reclaiming one hands back audio it has
        never played. In a room that is not two things at once, those frames
        are exactly the audio the room is waiting on:

        * the pre-buffer a room is still filling before it can start at all,
        * and the whole frames of **hold** a trimmed speaker is waiting out --
          its delay trim IS those frames (see ``hold_frames``).

        A transport that reclaims on every pump (``jukebox_relay``,
        ``AudioStreamer``) therefore gives the hold straight back before the
        speaker can spend it: the room's depth never reaches
        ``wanted_for_start`` (heard as "cinema mode went quiet"), or the
        speaker starts on the newest frame it holds instead of the oldest and
        the delay an installer dialled in is heard as the *wrong number* -- or
        as none at all ("the delays only work later in the song").
        ``peer.py`` and the voice leg already refuse to reclaim a room that is
        not playing for this very reason; this is the same rule, in the one
        place that owns the buffers.

        ``drain_stopped`` is the **teardown** path (``_reset_output``): the
        room is being taken apart and every buffer it still holds is wanted
        back, played or not.
        """
        reclaimed = False
        for slot, source in list(self.slot_sources.items()):
            pool = self._pools.get(slot)
            if pool is None:
                continue
            if not drain_stopped and not self._readable(slot):
                continue
            try:
                while source.buffers_processed > 0:
                    result = source.unqueue_buffers()
                    if result is None:
                        break
                    try:
                        pool.extend(result)
                    except TypeError:
                        # cyal returns a single Buffer, not a list.
                        pool.append(result)
                    reclaimed = True
            except Exception:
                continue
        if reclaimed:
            self.last_output_at = self._clock()
        return reclaimed

    def _readable(self, slot):
        """True while this speaker's finished-buffer count can be believed.

        Playing or paused, and only those: OpenAL reports a stopped source as
        having processed everything it holds, so the count is not a disclosure
        of what was heard until the source is actually running. A state that
        cannot be read is treated as *not* readable -- leaving buffers with the
        speaker is always safe, handing back audio nobody played is not.
        """
        source = self.slot_sources.get(slot)
        if source is None:
            return False
        try:
            return source.state in (cyal.SourceState.PLAYING,
                                    cyal.SourceState.PAUSED)
        except Exception:
            return False

    # ------------------------------------------------- frame size and floor

    def _learn_frame_size(self, samples):
        """Latch the size of a frame, and never let one odd frame move a trim.

        A trim is *played* as a cut in samples inside the frame it was queued
        in (``_delayed_window``) and its whole-frame part is a hold measured in
        frames (``hold_frames``) -- so the frame's own size is what turns a
        millisecond value into frames plus a remainder. Re-measuring that on
        every frame means a single short frame (a torn one, a warm-up replay,
        the tail of a resync) silently re-measures every trimmed speaker: a
        50 ms trim read against 40 ms frames is "one frame plus 10 ms" and
        against 20 ms frames "two frames plus 10 ms", and the speaker audibly
        moves by the difference -- then moves back. A size is therefore
        believed only after it has arrived ``FRAME_SIZE_STABLE_FRAMES`` times in
        a row, which keeps the room at the size its transport really delivers
        and ignores a frame that does not match.
        """
        if samples <= 0:
            return
        if samples == self._frame_samples_seen:
            self._frame_size_streak += 1
        else:
            self._frame_samples_seen = samples
            self._frame_size_streak = 1
        measured = samples * 1000.0 / SAMPLERATE
        if self._frame_ms > 0 and abs(measured - self._frame_ms) < 0.01:
            return
        # Before the room plays, the very first frame is the best evidence
        # there is (the start rules need a frame size immediately). Once it is
        # playing, only a size that keeps arriving is a new transport.
        if self._plays_started and self._frame_size_streak < FRAME_SIZE_STABLE_FRAMES:
            return
        previous = self._frame_ms
        self._frame_ms = measured
        # Counted only while the room plays: that is when a change can move a
        # speaker during a song (the first lock is simply the room learning its
        # transport).
        if (self._plays_started and previous > 0
                and abs(previous - measured) >= 0.01):
            self.frame_size_changes += 1
            log_line(f"[Cinema] room frame size {previous:.0f} ms -> "
                     f"{measured:.0f} ms")

    @property
    def awaiting_refill(self):
        """True while the room is held because it ran out of queued audio."""
        return self._refill_hold

    def _watch_low_queue(self):
        """Hold the whole room rather than let one speaker run out alone.

        A room plays as one unit only while every speaker has audio in front
        of it. The shallowest queue is what the room hears next, so it is the
        shallowest queue that decides: at the floor every speaker is held
        together -- the same pause a listener's own pause key uses, so nothing
        is ever dropped or re-played -- the arriving frames rebuild the depth,
        and the room starts again as one unit (``_play_ready``).

        One short hold, level across the room, instead of one speaker stopping
        alone and being put back a pump later while the rest kept playing.
        """
        if (self._refill_hold or self._paused or self._stopped
                or not self._plays_started or not self.slot_sources):
            return False
        if self._start_frame is None:
            return False
        if self.frames_queued - self._start_frame < self.wanted_for_start():
            # Still inside the room's own start: a shallow queue here is the
            # pre-buffer the caller asked for, not a room that ran dry.
            return False
        if any(self._held(slot) for slot in self.slot_sources):
            # A speaker still waiting out its delay trim is fed and silent by
            # design, and a trim is measured from the room's own clock (see
            # ``_held``) -- holding the room here would spend the trim on
            # frames nobody played.
            return False
        if not self._playing_slots():
            # Nothing is playing: the transport's own start path owns this room
            # and holds it until ``wanted_for_start`` frames are queued.
            return False
        # Only the speakers that are *playing* are measured. A speaker that
        # stopped mid-song (an underrun, a hiccup) holds an empty queue by
        # construction, and reading that as "the room is running low" held the
        # whole room for a refill -- and while a room is held, ``_feed_slots``
        # feeds *every* speaker (nothing is playing, so none is being left
        # out), which rebuilt the stopped one out of **live** frames. It then
        # started on content ahead of the room's own instant and stayed there:
        # heard as the cabinets coming apart after a stumble, and the trim an
        # installer dialled coming back as a different number (the reported
        # "it comes apart and then tries to pull itself back together").
        # A stopped speaker is ``realign``'s to put back on the room's window.
        playing = self._playing_slots()
        measured = playing or list(self.slot_sources)
        depth = min(self._queued_of(slot) for slot in measured)
        if depth > LOW_QUEUE_FRAMES:
            return False
        return self._begin_refill_hold(depth)

    def _begin_refill_hold(self, depth):
        self._refill_hold = True
        self.refill_holds += 1
        for source in self.slot_sources.values():
            with contextlib.suppress(Exception):
                source.pause()
        self._report_refill_hold(depth)
        return True

    def _report_refill_hold(self, depth):
        """Say once in a while that a room had to hold for more audio.

        Routine (the deferred sink): a room that runs low is recovering from a
        channel that dropped frames, not failing, and it must never be the
        reason a gameplay frame stalls.
        """
        now = self._clock()
        if (self._hold_reported_at is not None
                and now - self._hold_reported_at < HOLD_REPORT_INTERVAL):
            return
        self._hold_reported_at = now
        log_line(f"[Cinema] room ran low ({depth} frame(s) left of "
                 f"{len(self.slot_sources)} speaker(s)): holding every speaker "
                 f"until {self.wanted_for_start()} are queued")

    def _end_refill_hold(self):
        """Release the hold once the room has its audio again; True if it did."""
        if not self._refill_hold:
            return False
        if self.queued_frames() < self.wanted_for_start():
            return False
        self._refill_hold = False
        return True

    def _queued_of(self, slot):
        """Frames this speaker still holds, or 0 when it cannot say."""
        source = self.slot_sources.get(slot)
        if source is None:
            return 0
        try:
            return int(source.buffers_queued)
        except Exception:
            return 0

    def _playing_slots(self):
        slots = []
        for slot, source in self.slot_sources.items():
            try:
                if source.state == cyal.SourceState.PLAYING:
                    slots.append(slot)
            except Exception:
                return []
        return slots

    def _frame_samples(self):
        """Samples of audio in one queued frame (20 ms until a frame says)."""
        millis = self._frame_ms if self._frame_ms > 0 else 20.0
        return max(1, int(round(millis * SAMPLERATE / 1000.0)))

    def _trim_samples(self, slot):
        """This speaker's delay trim, in samples, straight from the map."""
        try:
            millis = float(self.renderer.layout.delay_ms(slot) or 0.0)
        except Exception:
            return 0
        if millis <= 0.0:
            return 0
        return int(round(millis * SAMPLERATE / 1000.0))

    def hold_frames(self, slot):
        """Whole frames this speaker's delay trim is, rounded up.

        A delay trim is *latency*: the speaker plays the same programme as its
        neighbours, that many samples later, and every speaker is held back by
        its own count from the one moment the room's clock starts. A queue can
        only carry a delay in whole frames: its own remainder travels as the
        sample-exact cut in ``_delay_samples``, and the two together land the
        trim exactly on the sample. The cut costs the speaker the one frame of
        feeding it takes the room to hold that much audio, which is why the
        count rounds *up*.

        Holding it is the whole point: feed a trimmed speaker the audio cut
        back by its trim and it cannot queue that audio until the room holds
        that much, which starves it by the very same amount -- the cut and the
        starvation cancel out, its queue head lands on the sample an untrimmed
        speaker is already playing, and the room starts both of them together.
        The trim was then heard as nothing at all (reported as "the delays only
        start working two or three minutes into the song", because a later
        recovery fills a speaker from the frames the room holds and *that*
        fill is not starved).
        """
        wanted = self._trim_samples(slot)
        if wanted <= 0:
            return 0
        frame = self._frame_samples()
        held = (wanted + frame - 1) // frame
        # Bounded by the buffers a speaker actually has: a room whose frames are
        # tiny next to the trim (a 100 ms trim over 8 sample frames) would ask
        # for more buffers than exist and could never take a frame at all. It
        # plays a shorter delay instead -- the one failure mode that is not
        # silence, which is what the room's pre-buffer leaves to play with.
        return min(held, max(0, self.buffers_per_slot - PREBUFFER_FRAMES - 1))

    def _delay_samples(self, slot):
        """The trim's sub-frame remainder: the cut only a sample can carry.

        What is left of the trim once the queue depth has taken its whole
        frames (``hold_frames``). It is zero for the trims an installer dials
        in whole frames and up to one frame short of that otherwise, and it is
        played -- not merely measured -- by ``_delayed_window``, so a 5 ms trim
        is a 5 ms shift rather than "nothing" or "a whole frame".
        """
        wanted = self._trim_samples(slot)
        if wanted <= 0:
            return 0
        return wanted % self._frame_samples()

    def _applied_trim(self, slot, history):
        """This speaker's delay, cut back to what the room's history reaches.

        The trim is played -- not merely measured -- as a cut into the frames
        the room still holds, so it can only ever be as deep as they are. While
        the room is still filling that is just "this speaker starts that late".
        Once the history is full and still shorter than the trim (frames much
        smaller than a twentieth of it), waiting for audio that will never
        arrive would leave the speaker unfed -- and therefore silent -- for the
        whole song, so it plays the deepest offset the room can cut: a shorter
        trim, never a dead speaker.
        """
        want = self._delay_samples(slot)
        if want <= 0:
            return 0
        depth = self._recent.maxlen
        if depth is None or len(history) < depth or len(history) < 2:
            return want
        per_frame = max(1, len(history[-1][0]) // 2)
        return min(want, (len(history) - 1) * per_frame)

    def _programme(self, left, right, targets):
        """What each crossover group *would* be fed this frame, uncommitted.

        One window per distinct *mark* among the speakers being fed, plus the
        history that window is cut from. A room with no mark anywhere -- every
        map before this attribute existed -- is the single unfiltered group it
        has always been, and the frames it is handed are the very objects they
        were, byte for byte.

        A marked group is a *stream*, not a per-frame effect: the filter carries
        its state from one frame to the next (see ``crossover``), and the frames
        it has already produced are kept so a delay trim can still be cut into
        audio that group actually played -- a bass cabinet whose trim was cut
        out of the room's unfiltered history would be handed its own mids back,
        and a tweeter its own bass.

        Nothing is committed here. A frame the room cannot queue (no free
        buffer) must not advance a filter or enter a history, or the refused
        frame the transport offers again would be filtered twice and the room
        would play a seam it does not have. The result is handed to ``_commit``
        only once the frame is on every speaker.
        """
        groups = {}
        for slot in targets:
            groups.setdefault(self._slot_crossover.get(slot, FULL_RANGE), None)
        programmes = {}
        for mark in groups:
            if mark == FULL_RANGE:
                history = list(self._recent)
                history.append((left, right))
                programmes[mark] = (history, len(history) - 1, None, None)
                continue
            group = self._crossover_streams.get(mark)
            filtered, state = crossover_apply((left, right),
                                              group["state"] if group else None,
                                              mark)
            history = list(group["history"]) if group else []
            history.append(filtered)
            programmes[mark] = (history, len(history) - 1, filtered, state)
        return programmes

    def _commit(self, left, right, programmes):
        """Accept a queued frame into the room's own history, and each group's."""
        self._recent.append((left, right))
        for mark, (_history, _index, filtered, state) in programmes.items():
            if mark == FULL_RANGE or filtered is None:
                continue
            group = self._crossover_streams.get(mark)
            if group is None:
                group = {"history": deque(maxlen=self._recent.maxlen),
                         "state": None}
                self._crossover_streams[mark] = group
            group["history"].append(filtered)
            group["state"] = state

    def _history_for(self, slot):
        """The programme this speaker is fed, newest last (see ``_programme``).

        A marked speaker's own filtered frames -- a bass cabinet's, or a
        tweeter's -- or the room's for every speaker that carries no mark: what
        ``realign`` puts a stalled speaker back in step with has to be the
        audio that speaker plays.
        """
        mark = self._slot_crossover.get(slot, FULL_RANGE)
        if mark != FULL_RANGE:
            group = self._crossover_streams.get(mark)
            if group is not None and group["history"]:
                return list(group["history"])
        return list(self._recent)

    def _sync_crossovers(self):
        """Follow the map's crossover mark for every slot; the changed ones' names.

        The map is a live document, so what a speaker is fed has to be re-read
        from it rather than only where the speaker was created: a builder
        dialling a crossover used to hear nothing until the room was rebuilt,
        which meant the next track (or a Reload Map Data and another wait),
        and every speaker a builder *placed* mid-song kept the full-range
        stream it was born with for the rest of the song.

        A stream that is already running is left alone -- it is aligned with
        the room frame for frame -- while a mark nobody was using is warmed
        from the frames the room already holds (``_warm_stream``), so the
        speaker that just started using it can be filled with audio it really
        plays instead of the room's unfiltered history.
        """
        changed = []
        for slot in self.renderer.slots:
            mark = crossover_hz(self.renderer.layout.crossover(slot))
            if self._slot_crossover.get(slot, FULL_RANGE) != mark:
                changed.append(slot)
                self._slot_crossover[slot] = mark
        for slot in changed:
            self._warm_stream(self._slot_crossover[slot])
        return changed

    def _warm_stream(self, mark):
        """Give a crossover mark the frames the room already holds, or rebuild it.

        A *new* mark has no filtered audio behind it yet, and a stream whose
        last speaker was dialled away from it stopped growing with the room, so
        its history ends a queue's worth ago. Either one is walked through the
        filter here, oldest frame first, so the history ends on the very frame
        the room is about to queue and the speaker filled from it lands on the
        room's instant with this mark's own filter already on it. Trusting a
        stream that is not the room's own length would put that speaker behind
        the song, which is the one thing the fill exists to prevent.
        """
        if mark == FULL_RANGE:
            return
        group = self._crossover_streams.get(mark)
        if group is not None and len(group["history"]) == len(self._recent):
            return
        group = {"history": deque(maxlen=self._recent.maxlen), "state": None}
        for left, right in list(self._recent):
            filtered, group["state"] = crossover_apply((left, right),
                                                       group["state"], mark)
            group["history"].append(filtered)
        self._crossover_streams[mark] = group

    def _delayed_window(self, history, index, samples):
        """The ``(left, right)`` programme ``samples`` samples behind that frame.

        A delay trim is decorrelation, not silence: the speaker plays the same
        programme, a few milliseconds later, which is what stops two speakers
        side by side from comb-filtering into one thick centre. The room keeps
        the frames it queued (``_recent``), so the cut is made at the exact
        sample offset -- rounding to whole frames would turn the 1-30 ms an
        installer actually dials in into "nothing" or "twice as much".

        ``history`` is the room's queued frames with the frame in question last,
        so the same cut serves the live feed (frame not queued yet) and a
        realign filling frames the room already holds.

        Returns None when the room has not queued that much audio yet, so a
        trimmed speaker starts that many samples late instead of being handed
        silence buffers (which OpenAL would play as a click).
        """
        frame = history[index]
        if samples <= 0:
            return frame
        frame_bytes = len(frame[0])
        need = samples * 2
        past = []
        for older in reversed(history[:index]):
            past.append(older)
            need -= len(older[0])
            if need <= 0:
                break
        if need > 0:
            return None
        chunks = list(reversed(past))
        chunks.append(frame)
        lefts = b"".join(chunk[0] for chunk in chunks)
        rights = b"".join(chunk[1] for chunk in chunks)
        end = len(lefts) - samples * 2
        start = end - frame_bytes
        if start < 0:
            return None
        return lefts[start:end], rights[start:end]

    def _slot_feeds(self, programmes, targets):
        """``[(slot, mono_pcm16), ...]`` for one frame, each at its own delay.

        Slots are grouped by the trim they carry *and* the crossover they are
        fed through, so a room with one alignment on its side pair and none on
        the screen wall renders each source frame once per distinct
        (crossover, offset) instead of once per speaker. A room with neither
        trims nor crossovers (the shipped jukebox) takes exactly the path it
        always did: one render of the live frame, byte for byte.
        """
        feeds = self._feeds_for(programmes, targets, bound=False)
        if feeds:
            return feeds
        # Nobody could be fed: every speaker of this room carries a trim deeper
        # than the audio the room still holds. A trimmed speaker waiting for
        # the history it needs is by design -- starting a few milliseconds late
        # IS the trim -- but that wait has always assumed some other speaker
        # queues the frames that build the history up. In a room where every
        # speaker was given a delay there is no such speaker, so the room never
        # queued its first frame, never grew the history, and stayed silent for
        # the whole song (reported as "I set a delay on each speaker and cinema
        # mode went quiet"). Cut every trim back to what this frame can reach:
        # each speaker starts a little early for the few frames the room needs
        # and carries its real trim from then on.
        return self._feeds_for(programmes, targets, bound=True)

    def _feeds_for(self, programmes, targets, bound):
        """One render per distinct (mark, trim), or nothing if unreachable."""
        groups = {}
        for slot in targets:
            mark = self._slot_crossover.get(slot, FULL_RANGE)
            window = programmes.get(mark)
            if window is None:
                continue
            samples = (self._reachable_trim(slot, window[0]) if bound
                       else self._applied_trim(slot, window[0]))
            groups.setdefault((mark, samples), []).append(slot)
        feeds = []
        for mark, samples in sorted(groups):
            history, index = programmes[mark][0], programmes[mark][1]
            source = self._delayed_window(history, index, samples)
            if source is None:
                continue
            wanted = set(groups[(mark, samples)])
            for slot, pcm in self.renderer.render(*source):
                if slot in wanted:
                    feeds.append((slot, pcm))
        return feeds

    def _reachable_trim(self, slot, history):
        """This speaker's trim, cut back to the audio the room still holds.

        ``_applied_trim`` deliberately plays the FULL trim while the room is
        still filling its history, because a speaker starting that late is
        what the trim asks for. This is the same trim with the one thing that
        makes feeding possible in the first place enforced: it can never be
        deeper than ``history``, which is what ``_delayed_window`` can cut.
        """
        want = self._delay_samples(slot)
        if want <= 0:
            return 0
        per_frame = max(1, len(history[-1][0]) // 2)
        return min(want, max(0, (len(history) - 1) * per_frame))

    def _feed_slots(self):
        """The speakers a frame is queued to right now.

        Normally that is all of them. A speaker that already stopped while
        the rest of the room keeps playing is deliberately left out: filling
        its queue with the frames it is missing would restart it a whole
        queue's worth behind the song and it would replay them at the live
        edge, which is heard as one speaker lagging for the rest of the song.
        Left empty, it rejoins at the live frame instead (see realign(), which
        puts it back on the room's content instant first).

        A speaker that is *waiting out its delay trim* is not in that
        situation and must keep being fed: it has not played yet, the frames
        it is waiting for are the room's own, and starving it is how its trim
        disappears (see ``hold_frames``).
        """
        playing = self._playing_slots()
        if playing and len(playing) < len(self.slot_sources):
            held = [slot for slot in self.slot_sources
                    if slot not in playing and self._held(slot)]
            return playing + held
        return list(self.slot_sources)

    def queued_frames(self):
        """Frames queued on the slowest speaker this frame reaches (min, not sum)."""
        counts = [self._queued_of(slot) for slot in self._feed_slots()]
        return min(counts) if counts else 0

    def frame_ms(self):
        """Milliseconds of audio in one queued frame (20 ms until told)."""
        return self._frame_ms

    def buffered_ms(self):
        """How far behind the live edge this room's audio is, in milliseconds.

        What a remote instrument note has to wait out to land on the beat the
        listener actually hears: one buffer per speaker per frame, so the
        shallowest speaker's queue is the room's distance behind the live
        edge. A room is never reported as "level with the live edge" either --
        the frame being staged right now is still ahead of what is audible.
        """
        return int(round(max(self.queued_frames(), 1) * self._frame_ms))

    def extra_latency_ms(self):
        """The room's delay trims, in milliseconds: latency no queue can see.

        A trim is the same programme played late, so the song gains that much
        latency without one extra frame being queued. A note scheduled from
        the queue alone would land that far ahead of the deepest speaker;
        ``listener.py``/``router.py`` report it as
        ``CinemaRenderer.extra_latency_s`` for exactly this. A room with no
        trims reports zero, so the shipped jukebox is unchanged.
        """
        try:
            return int(round(float(self.renderer.extra_latency_s or 0.0) * 1000.0))
        except Exception:
            return 0

    def playing(self):
        """True when every speaker is playing."""
        for source in self.slot_sources.values():
            try:
                if source.state != cyal.SourceState.PLAYING:
                    return False
            except Exception:
                return False
        return True

    def wanted_for_start(self):
        return RESUME_FRAMES if self._plays_started else PREBUFFER_FRAMES

    @_serialized
    def realign(self, *, play=True):
        """Put every speaker back on the same content instant as the room.

        A room is only as in-step as its shallowest queue: a speaker that
        stopped (a pause, an underrun, a device hiccup) holds less audio than
        the others, so it would resume a queue's worth ahead of the song --
        or, if it kept being fed while silent, behind it. Both are heard as
        two songs playing at once from the same room.

        The frames the room still holds are in ``_recent``, so a shallow
        speaker is handed exactly what the deepest one is about to play and
        starts from the same instant. A speaker fed through a crossover is
        filled from that crossover's own history instead (``_history_for``):
        the same instant of the same programme, with the bass cabinet's own
        low-pass already on it. Nothing is ever removed from a queue:
        only added, which is all OpenAL allows.

        Depths are compared *net of each speaker's own delay trim*: a trimmed
        speaker is meant to sit that many frames deeper than one that carries
        none (see ``hold_frames``), so the raw depth of a trimmed speaker is
        not the room's reference -- reading it as one would fill the untrimmed
        speakers with old audio to catch up with a delay they never had.

        Only a speaker that is *not playing* is filled. A playing speaker is
        at the live edge by construction -- it consumes one frame per frame
        and is handed one per frame -- so a low queue depth there is audio it
        has already played, and handing it frames from the room's history
        replays the song into it (heard from a live test room as a speaker
        stumbling over the same bar it just played).

        And nothing is ever *appended* to a speaker that already holds audio:
        a queue is a continuous run of the programme, so a window cut from
        ``_recent`` and put after frames the speaker queued earlier would leave
        a step in its own stream -- the speaker playing the song at an instant
        of its own, which is exactly the "the delay came and went by itself"
        report nothing on the listener's side can undo. A speaker that stopped
        holding frames is therefore emptied first (``_empty_speaker``), and one
        whose queue cannot be emptied is left exactly as it is.

        While the room is held for a refill it is deliberately left alone: its
        speakers are all paused and all being fed, so they are already
        carrying the same run of the programme and only need the depth to
        come back.
        """
        if self._stopped or self._refill_hold or not self.slot_sources:
            return False
        counts = {slot: self._queued_of(slot) for slot in self.slot_sources}
        holds = {slot: self.hold_frames(slot) for slot in self.slot_sources}
        base = max((max(0, queued - holds[slot])
                    for slot, queued in counts.items()), default=0)
        if base <= 0:
            return False
        fed = 0
        for slot, queued in counts.items():
            if self._playing(slot):
                continue
            # The programme this speaker is fed: the room's frames, or a
            # marked speaker's own filtered ones (see ``_history_for``). The
            # *counts* above are frame counts either way -- a crossover changes
            # the audio, never the room's step -- so the fill still starts
            # `target` frames back from the live edge of whatever this speaker
            # plays.
            stream = self._history_for(slot)
            if self._held(slot):
                # Waiting out its own delay trim, not behind it: the frames it
                # holds are the room's newest and the ones it is "missing" are
                # the hold itself. Filling them would replay audio it is about
                # to play, and starting it early is what the hold exists to
                # prevent (see ``hold_frames``).
                continue
            if queued > 0:
                # Frames are in front of it that cannot be taken back (a
                # listener's pause, a buffer the backend will not unqueue). A
                # speaker in step must not be touched, and a window spliced
                # after what it holds would move it off the room's instant.
                if not self._empty_speaker(slot):
                    continue
                queued = 0
            target = base + holds[slot]
            if target > len(stream):
                target = len(stream)
            missing = target - queued
            if missing <= 0:
                continue
            # What it is missing is the window the room is about to play, so
            # the fill starts `target` frames back from the live edge and runs
            # forward -- for the empty queue an underrun leaves, that is the
            # room's own window in its own order, which is what puts the
            # speaker back on the room's content instant. Each frame is cut to
            # that speaker's own trim remainder: handed the live frame instead,
            # a trimmed speaker would catch up with the room for as long as
            # that fill lasts and then jump backwards when the next trimmed
            # window arrives.
            origin = len(stream) - target
            offset = self._applied_trim(slot, stream)
            pool = self._pools.get(slot)
            speaker = self.slot_sources.get(slot)
            for step in range(missing):
                pair = self._delayed_window(stream, origin + step, offset)
                if pair is None:
                    continue
                pcm = dict(self.renderer.render(*pair)).get(slot)
                if pcm is None or not pool or speaker is None:
                    continue
                buffer = pool.pop()
                try:
                    buffer.set_data(pcm, sample_rate=SAMPLERATE,
                                    format=cyal.BufferFormat.MONO16)
                    speaker.queue_buffers(buffer)
                    fed += 1
                except Exception:
                    pool.append(buffer)
        if play and self._play_ready():
            fed = fed or 1
        return fed > 0

    def _paused_at(self, slot):
        """True while this speaker is held (a listener's pause, a refill)."""
        source = self.slot_sources.get(slot)
        if source is None:
            return False
        try:
            return source.state == cyal.SourceState.PAUSED
        except Exception:
            # A state that cannot be read is treated as "held": leaving a
            # speaker alone is always safe, stopping it is not.
            return True

    def _empty_speaker(self, slot):
        """Stop a speaker and hand every buffer it holds back; True if empty.

        A stopped source's queued buffers are finished as far as OpenAL is
        concerned, so they can be unqueued and reused -- which is the only way
        a speaker can be put back on the room's instant without leaving the
        audio it was holding (already behind the live edge) in its queue. A
        backend that will not give them back (or a source that still reports
        frames after stopping) is reported as not emptied, and the caller
        leaves that speaker alone rather than splicing a window after them.

        A *held* speaker is never touched: it is in step by construction (it
        was fed every frame it was held for), and stopping it would throw away
        the audio it is holding, which OpenAL gives no way to put back.
        """
        source = self.slot_sources.get(slot)
        if source is None or self._paused_at(slot):
            return False
        pool = self._pools.get(slot)
        with contextlib.suppress(Exception):
            source.stop()
        if pool is None:
            return False
        with contextlib.suppress(Exception):
            while source.buffers_processed > 0:
                result = source.unqueue_buffers()
                if result is None:
                    break
                try:
                    pool.extend(result)
                except TypeError:
                    pool.append(result)
            return self._queued_of(slot) == 0
        return False

    def _playing(self, slot):
        """True while this speaker is playing (its queue is the live edge)."""
        source = self.slot_sources.get(slot)
        if source is None:
            return False
        try:
            return source.state == cyal.SourceState.PLAYING
        except Exception:
            return False

    def _held(self, slot):
        """True while this speaker is deliberately waiting out its delay trim.

        Counted in frames the *room* has queued since playback began, not from
        the speaker's own queue: a trimmed speaker's queue is short by
        construction while it waits, and reading that as "not ready yet" is
        exactly how the trim used to be swallowed by the room's start.

        Every speaker waits its *own* trim, the one that starts the room
        included (see ``_play_ready``): the room's clock starts once, so the
        trim a speaker was given is the delay it plays at. Letting the first
        speaker start straight away while the rest waited out theirs put two
        speakers dialled in alike a whole trim apart -- the trim read back as
        the wrong number.
        """
        if self._start_frame is None:
            return False
        return (self.frames_queued - self._start_frame) < self.hold_frames(slot)

    def _ready_to_play(self, slot):
        """Whether this speaker may play: its own delay trim is filled.

        How much the room needs buffered before it plays at all is the
        *caller's* decision (``wanted_for_start``), taken before it asks; what
        is added here is only the speaker's own trim.
        """
        return not self._held(slot)

    def _play_ready(self):
        """Start every speaker whose own pre-buffer and trim are filled.

        A speaker that is still inside its hold is left playing nothing: it is
        still being fed (see ``_feed_slots``), so a later call starts it once
        the frames its trim holds it back by are queued -- and being started
        that much later *is* the trim.

        Every speaker is measured from the same instant -- the room's clock,
        pinned on the first call -- so the deepest trim starts last and the
        shallowest first, each by exactly its own delay. Nothing has to be
        called the room's reference, which is what makes two speakers dialled
        in alike come out level with each other.
        """
        self._end_refill_hold()
        if self._refill_hold:
            # The room ran out of audio and is rebuilding its depth: nothing
            # starts until ``wanted_for_start`` frames are queued again, and
            # then every speaker starts together.
            return False
        if self._start_frame is None:
            # The room's clock starts the first time it is asked to play with
            # a speaker that is not playing yet: every trim is measured from
            # this one instant, so each speaker plays its own delay -- and the
            # speaker that would start the room is held for its trim too. A
            # speaker allowed to start straight away while the rest waited
            # theirs out came out a whole trim early instead.
            self._start_frame = self.frames_queued
        started = False
        for slot, source in self.slot_sources.items():
            with contextlib.suppress(Exception):
                if source.state == cyal.SourceState.PLAYING:
                    continue
            if not self._ready_to_play(slot):
                continue
            with contextlib.suppress(Exception):
                source.play()
                started = True
        return started

    @_serialized
    def play(self):
        """Start every speaker that is not already playing its own trim."""
        return self._play_ready()

    @_serialized
    def start_playback(self):
        """Start the room once enough frames are queued on every speaker.

        ``realign`` runs first: whoever is being restarted is about to play
        while the rest of the room is still playing, and without the frames
        the room holds it would start at the wrong content instant for the
        remainder of the song. A speaker whose delay trim is still filling is
        left pending rather than started level with its neighbours, and the
        caller (which retries while the room is not fully playing) starts it
        as soon as its own hold is queued.
        """
        if self.playing():
            return True
        # End the hold first when the depth is back: the room then re-forms
        # (``realign``) *before* it becomes audible again, which is what puts a
        # speaker that ran dry back on the room's own instant.
        self._end_refill_hold()
        if not self._refill_hold:
            self.realign(play=False)
        started = self._play_ready()
        if started:
            self._plays_started = True
        return started

    @_serialized
    def set_paused(self, paused):
        """Hold or release every speaker in the room at once.

        Pausing only the source the transport was handed leaves the rest of
        the room playing (and then replaying from the front of their queues on
        resume), which is exactly how a room ends up permanently out of step
        after a pause. Holding all of them keeps every speaker's position, so
        resuming is sample-accurate across the room.
        """
        if self._stopped or not self.slot_sources:
            return False
        if paused:
            # The listener's own hold, not a refill: a room that runs low while
            # it is already paused must not be held again (nothing is playing).
            self._paused = True
            self._refill_hold = False
        else:
            self._paused = False
            self._refill_hold = False
            # Re-form the room before it becomes audible again: a speaker that
            # ran dry during the hold would otherwise come back a queue's
            # worth ahead of the others.
            self.realign(play=False)
        for source in self.slot_sources.values():
            with contextlib.suppress(Exception):
                if paused:
                    source.pause()
                else:
                    source.play()
        return True

    @_serialized
    def _follow_crossovers(self):
        """Follow the map's mark for each speaker, and say how many changed.

        A mark that changed is a *programme* change, not a restart. The frames
        that speaker already holds are the same song at the same instant with
        the other band kept -- a crossover is not a seek -- and the room's next
        frame is handed to it out of its new stream (``_programme`` reads the
        mark per slot, and the stream was warmed above so its filter is already
        settled when that frame arrives). So the speaker is never stopped,
        never emptied and never re-aligned: it plays the last of the old
        programme out, a queue's worth (60-120 ms), and carries straight on.

        Emptying it instead was heard: a builder pressing a crossover line
        three or four times in a row threw away that speaker's queue and
        started it again on every press, and a stop mid-buffer is a step in the
        audio no listener-side scheduling can undo. The one place a speaker is
        emptied is ``realign``, for one that stalled -- there the frames that
        follow are a window *from the past*, so a splice would leave that
        speaker playing at an instant of its own. Here they are the live edge,
        which is exactly where its queue already ends.

        Returns how many speakers changed mark.
        """
        changed = self._sync_crossovers()
        if changed:
            log_line(f"[Cinema] room {self.renderer.profile.name}: "
                     f"{len(changed)} speaker(s) re-cut to their crossover")
        return len(changed)

    @_serialized
    def reconfigure(self, renderer):
        """Re-shape the room in place after the map changed under it.

        A speaker placed while a song is playing used to need the whole
        feature toggled off and on again, which stops and restarts the song.
        The bank is deliberately never replaced: whatever is feeding it (a
        relay receiver, a running ffmpeg decode, the music bot) holds this
        object and would have to be restarted to see a new one. Instead the
        slot set is changed here -- untouched speakers keep playing what they
        already hold, a speaker that was removed is stopped and deleted, and a
        speaker that just appeared is filled with the frames the room still
        holds so it joins on the current beat rather than the next one.
        """
        if self._stopped:
            return False
        if renderer.signature == self.renderer.signature:
            return False
        playing = bool(self.slot_sources) and self.playing()
        old_sources = dict(self.slot_sources)
        kept = [slot for slot in renderer.slots if slot in old_sources]
        added = [slot for slot in renderer.slots if slot not in old_sources]
        dropped = [slot for slot in old_sources if slot not in renderer.slots]
        self.renderer = renderer
        for slot in dropped:
            self._pools.pop(slot, None)
            self.slot_sources.pop(slot, None)
            self._slot_tier.pop(slot, None)
            self._slot_crossover.pop(slot, None)
            self._dispose_speaker(old_sources[slot])
        changes = []
        if added:
            changes.append("+" + ", ".join(added))
        if dropped:
            changes.append("-" + ", ".join(dropped))
        log_line(f"[Cinema] room {renderer.profile.name}: "
                 f"{' '.join(changes) or 're-positioned'} "
                 f"-> {len(renderer.slots)} speaker(s) while playing")
        audio = getattr(self.game, "audio_mngr", None)
        for slot in kept:
            position = renderer.layout.position(slot)
            if position is None:
                continue
            with contextlib.suppress(Exception):
                self.slot_sources[slot].position = position
        # Keep the speakers that survive, in the room's order; the added slots
        # are filled in below once their sources exist.
        self.slot_sources = {slot: old_sources[slot] for slot in kept}
        if audio is None:
            return True
        for slot in added:
            try:
                self._pools[slot] = self._make_speaker(audio, slot)
            except Exception:
                self.failure_reason = "cinema speaker could not join the room"
        # A speaker whose crossover mark changed is the same speaker fed a
        # different programme: it is not stopped or re-filled here, it takes
        # the new stream with the next frame it is handed (see
        # _follow_crossovers).
        self._follow_crossovers()
        if added:
            # A speaker that just appeared holds nothing, so it is filled with
            # the frames the room still holds and starts on the beat that is
            # already playing.
            self.realign(play=False)
        self.touch_environment()
        self.update_output()
        if playing and not self.playing():
            self.start_playback()
        return True

    @_serialized
    def _reset_output(self, *, stamp=False):
        for source in self.slot_sources.values():
            with contextlib.suppress(Exception):
                source.stop()
        try:
            # Teardown: every buffer the room still holds is wanted back, and
            # a stopped speaker is the one thing a routine ``reclaim`` refuses
            # to read (see reclaim()).
            self.reclaim(drain_stopped=True)
        except Exception:
            pass
        self._plays_started = False
        self._start_frame = None
        self._refill_hold = False

    # ------------------------------------------------------------------ gain

    def configure(self, volume=None, cabinet_volume=None, fade=None):
        """Set output trims without refreshing, for callers that will refresh.

        The relay receiver recomputes its own retire ramp every pump, so it
        sets all three here and then calls update_output() once.
        """
        if volume is not None:
            self.volume = max(0, min(100, int(volume)))
        if cabinet_volume is not None:
            self.cabinet_volume = max(0.0, min(1.0, float(cabinet_volume) / 100.0))
        if fade is not None:
            self._external_fade = max(0.0, min(1.0, float(fade)))

    def set_duck(self, multiplier):
        """Set the live duck (megaphone-over-music) applied to every speaker."""
        self.duck = max(0.0, float(multiplier))

    def set_volume(self, volume):
        self.configure(volume=volume)
        self.update_output()

    def set_cabinet_volume(self, volume):
        self.configure(cabinet_volume=volume)
        self.update_output()

    def set_fade(self, gain):
        """External fade (the relay receiver owns its own retire ramp)."""
        self.configure(fade=gain)

    def retire(self, duration=0.5, ramp=True):
        """Take the room out of service, fading it out here over ``duration``.

        ``ramp=False`` for a room whose fade the transport owns (a relay
        receiver recomputes the room's gain every pump, so a ramp set here
        would be overwritten): the room is still marked as leaving, which is
        what keeps the song replacing it from being handed this very room.
        """
        self._retired = True
        if not ramp or self._fade_started is not None:
            return
        self._fade_started = self._clock()
        self._fade_duration = max(0.0, float(duration))

    def _fade_gain(self):
        if self._fade_started is None:
            return 1.0
        if self._fade_duration <= 0:
            return 0.0
        return max(0.0, 1.0 - (self._clock() - self._fade_started) / self._fade_duration)

    def current_fade(self):
        return self._external_fade * self._fade_gain()

    def distance_gain(self, slot, listener):
        """The room's own linear fade, evaluated at this speaker.

        The maths lives in ``listener.distance_gain`` so a live instrument
        played at this speaker is shaped by the very same curve as the song.
        """
        return distance_gain(self.slot_sources[slot].position, listener,
                             self.reference_distance, self.max_distance)

    def aim_gain(self, slot, listener):
        """How much of an aimed speaker reaches the listener (1.0 when unaimed).

        A speaker the builder never aimed is omnidirectional, which is the
        right default for a room every seat has to hear. This only matters
        for the speakers a builder deliberately pointed somewhere, and it is
        what makes "the audience is behind that speaker now" a physical fact
        instead of an assumption.
        """
        return speaker_aim_gain(listener, self.renderer.layout.position(slot),
                                self.renderer.layout.spec(slot))

    def update_output(self):
        """Refresh per-speaker gain, occlusion and environment sends.

        Called every frame by whichever transport is feeding the room, so it
        stays cheap: one distance check per speaker, and the occlusion ray is
        only cast when a speaker's wall tier actually changes.
        """
        if self._stopped or not self.slot_sources:
            return
        audio = getattr(self.game, "audio_mngr", None)
        if audio is None:
            return
        listener = getattr(audio, "position", None)
        category = audio.volume_categories.get(self.category, [100])[0] / 100.0
        local = ((self.volume / 100.0) * category * self.cabinet_volume
                 * self.current_fade() * self.duck)
        reverb_slot = self.reverb_slot
        eq_slot = self.eq_slot
        efx = getattr(audio, "efx", None)
        for slot, source in self.slot_sources.items():
            with contextlib.suppress(Exception):
                source.gain = (local * self.distance_gain(slot, listener)
                               * self.aim_gain(slot, listener))
            if listener is None:
                continue
            tier = self._wall_tier(slot, listener)
            tone = self.renderer.layout.tone(slot)
            if (tier == self._slot_tier.get(slot)
                    and tone == self._slot_tone.get(slot)
                    and not self._environment_dirty):
                continue
            self._slot_tier[slot] = tier
            self._slot_tone[slot] = tone
            # One filter per speaker, not two: the map's own voicing and the
            # wall between this speaker and these ears are the same OpenAL
            # slot, so they are composed rather than stacked (a second one
            # would silently replace the first).
            filt = self._speaker_filter(audio, tier, tone)
            with contextlib.suppress(Exception):
                if filt is not None:
                    source.direct_filter = filt
                else:
                    # Clearing a wall must not strip the underwater muffle;
                    # restore the active global filter instead of deleting.
                    restore_filter(source, audio)
            if efx is not None:
                with contextlib.suppress(Exception):
                    if reverb_slot is not None:
                        efx.send(source, 0, reverb_slot, filter=filt)
                    if eq_slot is not None:
                        efx.send(source, 1, eq_slot, filter=filt)
        self._environment_dirty = False

    def _wall_tier(self, slot, listener):
        provider = self.occlusion_provider
        if not callable(provider):
            return 0
        position = self.renderer.layout.position(slot)
        if position is None:
            return 0
        try:
            return int(provider(position, listener, self.max_distance))
        except Exception:
            return 0

    def _speaker_filter(self, audio, tier, tone=None):
        """The room's own filter for one speaker: its voicing plus that wall.

        The params live in :func:`listener.speaker_filter` (and its
        :func:`listener.occlusion_filter` for the wall alone) and nowhere
        else: the song, a live note and a voice played behind the same wall --
        or out of the same dulled speaker -- have to be shaped by the same
        numbers, or the room sounds like three different rooms depending on
        what is coming out of it.
        """
        return speaker_filter(audio, tier, tone, self._occlusion_filters)

    def _occlusion_filter(self, audio, *, heavy):
        """The room's wall filter alone, for a caller that knows only the tier."""
        return occlusion_filter(audio, 2 if heavy else 1, self._occlusion_filters)

    def set_reverb(self, slot):
        if slot is not self.reverb_slot:
            self.reverb_slot = slot
            self._environment_dirty = True

    def set_eq_slot(self, slot):
        if slot is not self.eq_slot:
            self.eq_slot = slot
            self._environment_dirty = True

    def set_profile(self, profile):
        """Swap the room's profile live and re-aim the room's speakers."""
        self.renderer.set_profile(profile)
        self._slot_tier.clear()
        self._slot_tone.clear()
        self._environment_dirty = True

    def touch_environment(self):
        """Force the next refresh to re-send occlusion and environment."""
        self._slot_tier.clear()
        self._slot_tone.clear()
        self._environment_dirty = True

    def __repr__(self):
        return (f"CinemaSpeakerBank({self.renderer.profile.name!r}, "
                f"slots={list(self.slot_sources)}, queued={self.queued_frames()})")
