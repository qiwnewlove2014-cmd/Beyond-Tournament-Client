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
from .listener import cone_gain

# One pool and one queue allowance per speaker. The radio between them keeps
# the same shape as the plain jukebox (32 buffers, 10 queued): enough head-
# room for the startup pre-buffer plus network jitter, and never enough to
# let a fast decoder run ahead of the room.
BUFFERS_PER_SLOT = 12
PREBUFFER_FRAMES = 4
RESUME_FRAMES = 3
MAX_QUEUED_FRAMES = 6

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
                 reference_distance=8.0, max_distance=40.0,
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
        self._occlusion_filters = {}
        self._retired = False
        self._stopped = False
        self.slot_sources = {}
        self._pools = {}
        self.last_output_at = None
        self.failure_reason = None
        self.frames_queued = 0
        self._plays_started = False
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
        """Create one speaker: its source, its position and its buffer pool."""
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
        feeds = self._slot_feeds(left, right, targets)
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
            self._recent.append((left, right))
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
    def reclaim(self):
        """Return finished buffers to their speaker's pool; True if any moved."""
        reclaimed = False
        for slot, source in list(self.slot_sources.items()):
            pool = self._pools.get(slot)
            if pool is None:
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

    def _delay_samples(self, slot):
        """Samples this speaker plays late, straight from its own delay trim."""
        millis = float(self.renderer.layout.delay_ms(slot) or 0.0)
        if millis <= 0.0:
            return 0
        return int(round(millis * SAMPLERATE / 1000.0))

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

    def _slot_feeds(self, left, right, targets):
        """``[(slot, mono_pcm16), ...]`` for one frame, each at its own delay.

        Slots are grouped by the trim they carry, so a room with one alignment
        on its side pair and none on the screen wall renders each source frame
        once per distinct offset instead of once per speaker. A room with no
        trims at all (the shipped jukebox) takes exactly the path it always
        did: one render of the live frame, byte for byte.
        """
        history = list(self._recent)
        history.append((left, right))
        index = len(history) - 1
        groups = {}
        for slot in targets:
            groups.setdefault(self._applied_trim(slot, history), []).append(slot)
        feeds = []
        for samples in sorted(groups):
            source = self._delayed_window(history, index, samples)
            if source is None:
                continue
            wanted = set(groups[samples])
            for slot, pcm in self.renderer.render(*source):
                if slot in wanted:
                    feeds.append((slot, pcm))
        return feeds

    def _feed_slots(self):
        """The speakers a frame is queued to right now.

        Normally that is all of them. A speaker that already stopped while
        the rest of the room keeps playing is deliberately left out: filling
        its queue with the frames it is missing would restart it a whole
        queue's worth behind the song and it would replay them at the live
        edge, which is heard as one speaker lagging for the rest of the song.
        Left empty, it rejoins at the live frame instead (see realign(), which
        puts it back on the room's content instant first).
        """
        playing = self._playing_slots()
        if playing and len(playing) < len(self.slot_sources):
            return playing
        return list(self.slot_sources)

    def queued_frames(self):
        """Frames queued on the slowest speaker this frame reaches (min, not sum)."""
        counts = [self._queued_of(slot) for slot in self._feed_slots()]
        return min(counts) if counts else 0

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
        starts from the same instant. Nothing is ever removed from a queue:
        only added, which is all OpenAL allows.
        """
        if self._stopped or not self.slot_sources:
            return False
        counts = {slot: self._queued_of(slot) for slot in self.slot_sources}
        target = max(counts.values()) if counts else 0
        if target <= 0:
            return False
        history = list(self._recent)
        if target > len(history):
            target = len(history)
        if target <= 0:
            return False
        base = len(history) - target
        fed = 0
        for slot, queued in counts.items():
            missing = target - queued
            if missing <= 0:
                continue
            # The frames this speaker is missing are the ones just before the
            # queue it already holds (all of them when it holds nothing). Each
            # one is cut to that speaker's own trim: handed the live frame
            # instead, a trimmed speaker would catch up with the room for as
            # long as that fill lasts and then jump backwards when the next
            # trimmed window arrives.
            offset = self._applied_trim(slot, history)
            pool = self._pools.get(slot)
            speaker = self.slot_sources.get(slot)
            for step in range(missing):
                pair = self._delayed_window(history, base + step, offset)
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
        if fed and play:
            self.play()
        return fed > 0

    @_serialized
    def play(self):
        """Start every speaker that is not already playing."""
        started = False
        for source in self.slot_sources.values():
            with contextlib.suppress(Exception):
                if source.state != cyal.SourceState.PLAYING:
                    source.play()
                    started = True
        return started

    @_serialized
    def start_playback(self):
        """Start the room once enough frames are queued on every speaker.

        ``realign`` runs first: whoever is being restarted is about to play
        while the rest of the room is still playing, and without the frames
        the room holds it would start at the wrong content instant for the
        remainder of the song.
        """
        if self.playing():
            return True
        self.realign(play=False)
        self.play()
        self._plays_started = True
        return True

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
        if not paused:
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
        if added:
            # Every speaker holds the same frames again, so the new ones start
            # on the beat the room is already playing.
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
            self.reclaim()
        except Exception:
            pass
        self._plays_started = False

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
        """The plain jukebox's linear fade, evaluated at this speaker."""
        if listener is None:
            return 1.0
        position = self.slot_sources[slot].position
        distance = sum((float(listener[i]) - position[i]) ** 2 for i in range(3)) ** 0.5
        if distance <= self.reference_distance:
            return 1.0
        if distance >= self.max_distance:
            return 0.0
        span = max(0.0001, self.max_distance - self.reference_distance)
        return max(0.0, 1.0 - (distance - self.reference_distance) / span)

    def aim_gain(self, slot, listener):
        """How much of an aimed speaker reaches the listener (1.0 when unaimed).

        A speaker the builder never aimed is omnidirectional, which is the
        right default for a room every seat has to hear. This only matters
        for the speakers a builder deliberately pointed somewhere, and it is
        what makes "the audience is behind that speaker now" a physical fact
        instead of an assumption.
        """
        if listener is None:
            return 1.0
        spec = self.renderer.layout.spec(slot)
        if spec is None or not spec.has_cone:
            return 1.0
        return cone_gain(listener, self.renderer.layout.position(slot),
                         spec.aim_yaw, spec.cone_inner, spec.cone_outer,
                         spec.cone_outer_gain if spec.cone_outer_gain is not None else 0.0)

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
            if tier == self._slot_tier.get(slot) and not self._environment_dirty:
                continue
            self._slot_tier[slot] = tier
            filt = None
            if tier == 2:
                filt = self._occlusion_filter(audio, heavy=True)
            elif tier == 1:
                filt = self._occlusion_filter(audio, heavy=False)
            with contextlib.suppress(Exception):
                if filt is not None:
                    source.direct_filter = filt
                else:
                    # Clearing a wall must not strip the underwater muffle;
                    # restore the active global filter instead of deleting.
                    active = getattr(audio, "filter", None)
                    if active and active[-1] is not None:
                        source.direct_filter = active[-1]
                    else:
                        del source.direct_filter
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

    def _occlusion_filter(self, audio, *, heavy):
        key = "heavy" if heavy else "light"
        cached = self._occlusion_filters.get(key)
        if cached is not None:
            return cached
        if not hasattr(audio, "gen_filter"):
            return None
        params = (("GAINHF", 0.05), ("GAIN", 0.22)) if heavy else (("GAINHF", 0.45), ("GAIN", 0.75))
        try:
            cached = audio.gen_filter("LOWPASS", *params)
        except Exception:
            cached = None
        self._occlusion_filters[key] = cached
        return cached

    # -------------------------------------------------------------- settings

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
        self._environment_dirty = True

    def touch_environment(self):
        """Force the next refresh to re-send occlusion and environment."""
        self._slot_tier.clear()
        self._environment_dirty = True

    def __repr__(self):
        return (f"CinemaSpeakerBank({self.renderer.profile.name!r}, "
                f"slots={list(self.slot_sources)}, queued={self.queued_frames()})")
