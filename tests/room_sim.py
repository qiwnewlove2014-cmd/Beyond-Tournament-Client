"""A headless room: the OpenAL contract, a fake transport and fault injection.

The cinema room's failures are all *timing* failures: two speakers playing the
same song at different instants, a delay that moves by itself, a speaker that
comes back a whole queue behind, a frame that lands on half the room. None of
them is visible in a test that counts calls, and none can be reproduced by
playing the game (the song has to run for minutes and the fault has to arrive
at the right moment).

This module is the machine that reproduces them offline: a virtual OpenAL
device that behaves like the real one *including the ways it misleads*, a
transport pump that feeds the room one frame at a time, a programme tape whose
sample values are their own position in the song, and fault injection for the
moments a listener actually reported.

Every speaker records the buffers it **played**, in order, with the programme
offset each carried -- so a test asserts on what a listener would *hear*
("this speaker is 2880 samples behind that one, from the first bar to the
last"), never on an internal counter. A counter can be right while the room is
wrong: that is what every one of these bugs looked like from the inside.

The traps the device models (each is a real bug this project had):

* **a stopped source reports every buffer it holds as processed.** OpenAL
  counts a buffer finished the moment its source is not playing it, so
  ``unqueue_buffers()`` hands back audio that was never heard. Read as "the
  queue is draining", that both hides a starved speaker and lets a caller
  recycle the frame it has just queued -- the room then never reaches its
  start depth, and the next thing said through it is silent.
* **a source whose queue empties while it plays stops by itself.** Nothing
  raises and nothing is logged: the speaker is simply not in the room any
  more, and it comes back a whole queue behind unless it is put back on the
  room's content instant first.
* **a backend may refuse to give buffers back** (a source that still reports
  frames after stopping). The only safe answer is to leave that speaker as it
  is, never to splice a window after the audio it holds.
* **a device can die.** ``queue_buffers`` raises for one speaker mid-frame;
  the room must not be left with half of it holding that frame.

The injections are the reported symptoms:

=========================  ==================================================
"one speaker drifts apart  ``stall(slot)`` (a hung sink: playing, consuming
from the others"           nothing) or ``starve(slot)`` (an underrun)
"the delay moved by        ``short_frame()`` (a torn frame arriving in a
itself"                    stream of another size), or a pump that reclaims
                           while a speaker is still waiting out its hold
"it speeds up for a        ``drop_frames(n)`` (the transport sheds frames at
second"                    the live edge: audio nobody hears)
"cinema mode went quiet    a room where every speaker carries a trim
when I set a delay on      (``RoomRig(delays={"front_l": 40, "front_r": 60})``)
each speaker"
"the song stumbles over    ``refuse_unqueue(slot)`` (the speaker that cannot
one bar"                   be emptied before a refill)
"it comes apart after a    ``starve(slot)`` with no trim, watched for where the
stumble and then pulls     speaker rejoins: a stopped speaker's empty queue is
itself back together"      not "the room running low"
=========================  ==================================================

Two of those were found by this harness and fixed in ``bank.py``: a routine
``reclaim`` reading a **stopped** source (its finished count is the device
lying -- see the device notes, and ``_readable``/``drain_stopped``), and the
room's low-queue hold measuring a stopped speaker's empty queue. Both are
pinned in ``test_cinema_room_faults`` and recorded in ``AGENTS.md``.

Usage::

    rig = RoomRig(delays={"front_r": 60.0}).run(200)
    self.assertEqual(set(rig.lag("front_r", "front_l")), {2880})

A read is only fair **tick-aligned** (one entry per tick per speaker, ``None``
where nothing came out), and only the slots the profile feeds a whole channel
to can be read back at all -- see ``readable`` and ``_whole_channel_slots``.
"""

import array
import os
import sys
from types import SimpleNamespace

import cyal

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from libs.audio.cinema import CinemaRenderer, CinemaSpeakerBank
from libs.audio.cinema.layout import IDEAL_BEARING, CinemaSpeakerSpec

ANCHOR = (10.0, 20.0, 0.0)
SAMPLERATE = 48000
#: The tape wraps here, so a programme distance below it reads exactly. It has
#: to be longer than the deepest trim (100 ms) plus the deepest queue, or two
#: positions would alias into each other.
TAPE = 30000
#: A frame's worth of samples at the two sizes the transports really use: the
#: direct streamer decodes 20 ms at a time, the server relay hands 40 ms PCM.
FRAME_20MS = 960
FRAME_40MS = 1920


def tape_frame(offset, samples):
    """One frame of the programme tape: the sample value is its own position."""
    values = array.array("h", [(offset + step) % TAPE for step in range(samples)])
    return values.tobytes()


class VirtualBuffer:
    """One OpenAL buffer name, with the programme offset it carries.

    The offset is read back out of the audio itself -- the tape's sample
    values are their own position in the song -- so what a speaker "played"
    is the audio it was really handed, trims and cuts included, rather than
    anything this harness chose to believe about it.
    """

    def __init__(self):
        self.data = None
        self.offset = None

    def set_data(self, data, sample_rate=None, format=None):
        self.data = bytes(data)
        samples = array.array("h", self.data)
        self.offset = samples[0] % TAPE if samples else None

    def __repr__(self):
        return "VirtualBuffer(offset=%s)" % (self.offset,)


class VirtualSource:
    """One OpenAL source, as the room's code experiences it.

    ``buffers_queued`` is the depth the room measures itself by;
    ``buffers_processed`` is the number OpenAL *reports*, which is not the same
    as "buffers that were heard" once a source stops (see the module docstring).
    """

    def __init__(self, context, *, lying_when_stopped=True, refuse_unqueue=False):
        self.context = context
        self.state = cyal.SourceState.INITIAL
        self.position = None
        self.rolloff_factor = None
        self.reference_distance = None
        self.max_distance = None
        self.spatialize = None
        self.direct_channels = None
        self.direct_filter = None
        self.gain = 0.0
        self.deleted = False
        self.buffers = []
        self.finished = []
        self.played = []
        self.underruns = 0
        self.stalled = False
        self.dead = False
        self.lying_when_stopped = lying_when_stopped
        self.refuse_unqueue = refuse_unqueue
        self.queued_total = 0

    # ------------------------------------------------------------- the queue
    @property
    def buffers_queued(self):
        return len(self.buffers)

    @property
    def buffers_processed(self):
        """The number of buffers OpenAL calls finished.

        Which is exactly what it consumed while it plays. A source that has
        been **stopped** reports everything it still holds as finished -- that
        is what lets the room empty a speaker before refilling it, and it is
        also the number that hands back audio nobody heard, which is what the
        voice leg had to survive. A source that has never played (INITIAL) is
        the opposite case: its queue is *pending*, so the room can build its
        pre-buffer a frame at a time.
        """
        if self.state in (cyal.SourceState.PLAYING, cyal.SourceState.PAUSED):
            return len(self.finished)
        if self.state == cyal.SourceState.STOPPED:
            if not self.lying_when_stopped:
                return len(self.finished)
            return len(self.finished) + len(self.buffers)
        return 0

    def play(self):
        if self.dead:
            raise RuntimeError("source deleted")
        self.state = cyal.SourceState.PLAYING

    def pause(self):
        if not self.dead:
            self.state = cyal.SourceState.PAUSED

    def stop(self):
        if not self.dead:
            self.state = cyal.SourceState.STOPPED

    def delete(self):
        self.deleted = True
        self.dead = True

    def queue_buffers(self, buffer):
        if self.dead:
            raise RuntimeError("source deleted")
        self.buffers.append(buffer)
        self.queued_total += 1
        self.context.queued += 1

    def unqueue_buffers(self, max=None):
        """Hand back finished buffers, oldest first, the way cyal does.

        A backend that will not give them back answers ``None``, which is what
        the room's own drain loops test for.
        """
        if self.refuse_unqueue:
            return None
        available = self.buffers_processed
        if available <= 0:
            return None
        take = available if max is None else min(int(max), available)
        handed = []
        while take > 0 and self.finished:
            handed.append(self.finished.pop(0))
            take -= 1
        while take > 0 and self.buffers:
            handed.append(self.buffers.pop(0))
            take -= 1
        return handed

    # ------------------------------------------------------------ the device
    def consume(self):
        """The device played one buffer, or ran out and stopped by itself.

        Returns the buffer it played, or None when this speaker produced no
        audio this frame -- a hung sink, a dead source, or a queue that ran dry.
        """
        if self.dead or self.stalled or self.state != cyal.SourceState.PLAYING:
            return None
        if not self.buffers:
            # An empty queue while playing stops the source in OpenAL, with no
            # error and no log line.
            self.state = cyal.SourceState.STOPPED
            self.underruns += 1
            return None
        buffer = self.buffers.pop(0)
        self.finished.append(buffer)
        self.played.append(buffer.offset)
        return buffer


class VirtualContext:
    """``gen_source`` / ``gen_buffer``, and every name handed out."""

    def __init__(self, **source_options):
        self.source_options = source_options
        self.sources = []
        self.buffers = []
        self.queued = 0

    def gen_source(self, **kwargs):
        options = dict(self.source_options)
        options.update(kwargs)
        source = VirtualSource(self, **options)
        self.sources.append(source)
        return source

    def gen_buffer(self):
        buffer = VirtualBuffer()
        self.buffers.append(buffer)
        return buffer


class VirtualAudio:
    """The audio manager face the room uses."""

    def __init__(self, **source_options):
        self.context = VirtualContext(**source_options)
        self.efx = SimpleNamespace(send=lambda *args, **kwargs: None)
        self.filter = []
        self.position = ANCHOR
        self.volume_categories = {"jukebox": [100], "music": [100]}

    def gen_filter(self, kind, *params):
        return ("filter", kind, params)


class VirtualGame:
    def __init__(self, **source_options):
        self.audio_mngr = VirtualAudio(**source_options)


class VirtualClock:
    """A clock that only moves when the transport does."""

    def __init__(self, start=1000.0):
        self.now = float(start)

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += float(seconds)


def _specs(slots, delays, radius):
    """``CinemaSpeakerSpec`` for each slot, at its ideal bearing."""
    from math import cos, radians, sin

    out = []
    for slot in slots:
        angle = radians(IDEAL_BEARING[slot])
        out.append(CinemaSpeakerSpec(
            slot,
            (ANCHOR[0] + sin(angle) * radius, ANCHOR[1] + cos(angle) * radius,
             ANCHOR[2]),
            delay_ms=delays.get(slot, 0.0),
        ))
    return out


class RoomRig:
    """A room of speakers, a tape, and a transport pumping it one frame a tick.

    The pump is what the relay receiver and the direct streamer both do: let
    every speaker play, take finished buffers back, hand the room one frame,
    and ask it to start whenever it is not fully playing. Everything else --
    which speaker stalls, when a frame is torn, when the transport sheds
    frames -- is the test's to inject.
    """

    def __init__(self, delays=None, *, slots=None, profile="front_only",
                 samples=FRAME_20MS, radius=8.0, buffers_per_slot=None,
                 clock=None, update_output=False, **source_options):
        delays = dict(delays or {})
        self.samples = int(samples)
        self.clock = clock or VirtualClock()
        self.game = VirtualGame(**source_options)
        # The room's speakers are the map's, so a caller names the slots it
        # placed. The default is the profile's own shape, which is what a
        # cabinet resolves to when nobody placed anything.
        canonical = CinemaRenderer(
            ANCHOR, profile, None,
            # A mono verdict would feed two speakers the same content and hide
            # the timing measured here; the verdict has its own tests.
            detect_channels=False,
        )
        default_slots = ("front_l", "front_r")
        if slots is None:
            try:
                default_slots = tuple(canonical.layout.slots)
            except Exception:
                default_slots = ("front_l", "front_r")
        self.requested = list(slots or default_slots)
        self.renderer = CinemaRenderer(
            ANCHOR, profile, None,
            specs=_specs(self.requested, delays, radius),
            detect_channels=False,
        )
        self.bank = CinemaSpeakerBank(
            self.game, self.renderer, buffers_per_slot=buffers_per_slot,
            clock=self.clock,
        )
        self.sources = self.bank.slot_sources
        # The room's speakers are the ones the bank built, not the ones asked
        # for: a profile's ring fills the slots nobody placed (a ``theatre``
        # room builds all seven even when a test names four).
        self.slots = list(self.bank.slot_sources)
        # Which of them carry the programme verbatim, and so can be read back:
        # a profile hands its front pair a whole channel and *mixes* the rest
        # (the centre a sum, the side walls the difference), so the tape's
        # "sample value is its position" trick only survives on those. The
        # slots are measured, not assumed, because a profile is free to
        # change which ones those are.
        self.readable = [slot for slot in self.slots
                         if slot in self._whole_channel_slots()]
        self.update_output = bool(update_output)
        # The programme: `offset` is the next sample the transport hands over,
        # so a queued frame's sample values are its own position in the song.
        self.offset = 0
        self.ticks = 0
        self.shed_frames = 0
        self.failure_frames = 0
        self.failure = None
        # One entry per tick per speaker, None where that speaker produced no
        # audio -- so an index into these lists is a *tick*, and two speakers
        # read at one index are read at the same moment. Reading the i-th
        # buffer each speaker happened to play would count a speaker that
        # started later as if it were playing the same content at the same
        # time, which is the mistake every one of these bugs hid behind.
        self.played = {slot: [] for slot in self.slots}
        self.depths = {slot: [] for slot in self.slots}
        self.queued_after = {slot: [] for slot in self.slots}

    # ------------------------------------------------------------- the pump
    def tick(self, *, push=None, queue=True, start=True):
        """One frame of the song: every speaker plays, then the room is fed.

        ``queue=False`` is the transport going quiet for a moment -- the frames
        that never arrived -- while the room keeps playing what it holds. That
        is how a room runs low, and the only way to reach the hold.

        The pump shape matters as much as the fault (see ``relay_pump``): a
        one-frame pump reclaims far more often than the relay does, and a
        speaker that is not playing yet is exactly the speaker a reclaim must
        leave alone.
        """
        samples = self.samples if push is None else int(push)
        self.ticks += 1
        for slot, source in self.sources.items():
            buffer = source.consume()
            self.bank.reclaim()
            self._record(slot, buffer)
        queued = False
        if queue:
            frame = tape_frame(self.offset, samples)
            self.offset += samples
            queued = self.bank.queue_frame(frame, frame)
            if not queued and self.bank.failure_reason:
                # A device that died mid-frame: the room tears the frame down
                # rather than leaving half of itself holding it.
                self.failure_frames += 1
                self.failure = self.bank.failure_reason
            for slot, source in self.sources.items():
                self.queued_after[slot].append(source.buffers_queued)
        if self.update_output:
            self.bank.update_output()
        if start and not self.bank.playing():
            # The relay's own start rule (jukebox_relay.py, the pump's tail):
            # the room is only asked to play once the shallowest speaker holds
            # `wanted_for_start` frames. Asking earlier starts the room a
            # pre-buffer shallow -- which is what makes a trim look like it was
            # swallowed by the start.
            if self.bank.queued_frames() >= self.bank.wanted_for_start():
                self.bank.start_playback()
        self.clock.advance(samples / float(SAMPLERATE))
        return queued

    def _record(self, slot, buffer):
        """One tick's worth of this speaker's own timeline."""
        self.played[slot].append(buffer.offset if buffer is not None else None)
        self.depths[slot].append(self.sources[slot].buffers_queued)

    def run(self, ticks, **kwargs):
        for _ in range(ticks):
            self.tick(**kwargs)
        return self

    def pump(self, ticks=1, **kwargs):
        return self.run(ticks, **kwargs)

    def relay_pump(self, pumps=1, max_frames=4):
        """The server relay's own pump shape (`jukebox_relay.pump_audio`).

        It reclaims **once at the top** and then hands the room up to
        ``max_frames`` decoded frames before it asks it to start -- which is
        not the same shape as one frame per pump, and is why a fault can be
        reachable on one transport and not the other. The device plays a
        buffer per frame while they are queued, so the two are interleaved
        here exactly as they are in the room's own queue accounting.
        """
        for _ in range(pumps):
            self.ticks += 1
            self.bank.reclaim()
            for _ in range(int(max_frames)):
                for slot, source in self.sources.items():
                    buffer = source.consume()
                    self._record(slot, buffer)
                frame = tape_frame(self.offset, self.samples)
                self.offset += self.samples
                if self.bank.queue_frame(frame, frame):
                    for slot, source in self.sources.items():
                        self.queued_after[slot].append(source.buffers_queued)
                if not self.bank.playing():
                    if (self.bank.queued_frames()
                            >= self.bank.wanted_for_start()):
                        self.bank.start_playback()
            self.clock.advance(int(max_frames) * self.samples / float(SAMPLERATE))
        return self

    def _whole_channel_slots(self):
        """The slots this profile feeds one channel of the programme verbatim.

        Measured by rendering one probe frame: a slot whose output *is* the
        left or the right input carries the tape, so a buffer's own sample
        values read back as its position in the song. A slot that carries a
        sum, a difference or a scaled mix cannot be read that way, and a test
        that compared one would be measuring the mix, not the room.
        """
        left = tape_frame(0, 8)
        right = tape_frame(4096, 8)
        try:
            rendered = dict(self.renderer.render(left, right))
        except Exception:
            return set(self.slots)
        return {slot for slot, pcm in rendered.items()
                if pcm in (left, right)}

    # -------------------------------------------------------------- faults
    def stall(self, slot):
        """A hung sink: the speaker is playing but nothing comes out of it.

        Its queue grows instead of draining, which is the one fault the room's
        own code cannot see: the depth it measures is the *shallowest*
        speaker's, so a speaker that stopped draining never holds the room.
        """
        self.sources[slot].stalled = True
        return self

    def resume(self, slot):
        self.sources[slot].stalled = False
        return self

    def starve(self, slot):
        """Empty one speaker's queue, the way an interrupted feed leaves it.

        The device stops by itself on the next tick, which is the underrun a
        listener hears as one speaker dropping out of the room.
        """
        source = self.sources[slot]
        source.buffers.clear()
        return self

    def kill(self, slot):
        """A device that will not take another buffer."""
        self.sources[slot].dead = True
        return self

    def refuse_unqueue(self, slot):
        """A backend that will not give a stopped speaker its buffers back."""
        self.sources[slot].refuse_unqueue = True
        return self

    def short_frame(self):
        """One torn frame, the size a warm-up replay or a resync tail has."""
        return self.tick(push=self.samples // 2)

    def drop_frames(self, count=1):
        """The transport sheds frames at the live edge: audio nobody hears.

        The programme jumps forward and every speaker jumps with it -- heard,
        and reported, as "it speeds up for a second".
        """
        self.shed_frames += int(count)
        self.offset += int(count) * self.samples
        return self

    def realign(self, **kwargs):
        return self.bank.realign(**kwargs)

    def start_playback(self):
        return self.bank.start_playback()

    # -------------------------------------------------------------- reading
    def heard(self, slot):
        """The programme offset of every buffer this speaker played, in order."""
        return [offset for offset in self.played[slot] if offset is not None]

    def played_by_tick(self, slot):
        """``[(tick, offset_or_None), ...]`` -- this speaker's own timeline."""
        return list(zip(range(1, len(self.played[slot]) + 1), self.played[slot]))

    def lag(self, deep, shallow):
        """Per tick, how far ``deep`` trails ``shallow`` *in the programme*.

        Only the ticks where both speakers played something are reported: a
        speaker still inside its trim hold has played nothing yet, so there is
        nothing to compare it with.
        """
        out = []
        for index, tail in enumerate(self.played[deep]):
            lead = self.played[shallow][index]
            if tail is None or lead is None:
                continue
            out.append((lead - tail) % TAPE)
        return out

    def steady_lag(self, deep, shallow, after=40):
        """The programme gap once the room has settled: one value, or a set."""
        values = self.lag(deep, shallow)
        return sorted(set(values[after:]))

    def steps(self, slot):
        """The programme step between each pair of consecutive buffers played."""
        offsets = self.heard(slot)
        return [(later - before) % TAPE
                for before, later in zip(offsets, offsets[1:])]

    def trims(self):
        """The trim each speaker was given by the map, in samples."""
        return {slot: (self.renderer.layout.delay_ms(slot) or 0.0)
                * SAMPLERATE / 1000.0 for slot in self.slots}

    def min_depth(self, slot, after=0):
        depths = self.depths[slot][after:]
        return min(depths) if depths else 0

    def max_depth(self, slot, after=0):
        depths = self.depths[slot][after:]
        return max(depths) if depths else 0

    def backlog_samples(self, slot):
        """Samples between what this speaker plays next and the live edge."""
        source = self.sources[slot]
        if not source.buffers or source.buffers[0].offset is None:
            return 0
        return (self.offset - source.buffers[0].offset) % TAPE

    def audible_backlog_ms(self):
        """The room's real distance behind the live edge: the shallowest speaker."""
        return min(self.backlog_samples(slot) for slot in self.slots) * 1000.0 / SAMPLERATE

    def started(self, slot):
        """The tick this speaker first played, or None when it never did."""
        for index, offset in enumerate(self.played[slot], 1):
            if offset is not None:
                return index
        return None

    def silent_slots(self):
        return [slot for slot in self.slots if not self.heard(slot)]

    def max_lag(self, *, slots=None):
        """The widest programme gap between the speakers that can be read.

        A profile mixes its non-front slots (see ``_whole_channel_slots``), so
        comparing them measures the mix rather than the room: the default is
        the slots that carry the programme itself.
        """
        speakers = list(slots or self.readable or self.slots)
        worst = 0
        for slot in speakers[1:]:
            for value in self.lag(slot, speakers[0]):
                worst = max(worst, min(value, TAPE - value))
        return worst

    def report(self):
        return RoomReport(self)

    def __repr__(self):
        return "RoomRig(%s, ticks=%d, offset=%d, shed=%d)" % (
            ", ".join(self.slots), self.ticks, self.offset, self.shed_frames)


class RoomReport:
    """A reading of a run, in the units a person listens in."""

    def __init__(self, rig):
        self.rig = rig

    def speakers(self):
        return {slot: {"heard": len(self.rig.heard(slot)),
                       "depth_min": self.rig.min_depth(slot),
                       "depth_max": max(self.rig.depths[slot] or [0]),
                       "underruns": self.rig.sources[slot].underruns,
                       "stalled": self.rig.sources[slot].stalled,
                       "started": self.rig.started(slot)}
                for slot in self.rig.slots}

    def max_skew(self):
        return self.rig.max_lag()

    def silent_slots(self):
        return self.rig.silent_slots()

    def summary(self):
        return "%s | skew=%d | shed=%d | refill_holds=%d | failures=%d" % (
            ", ".join("%s: %s" % (slot, values)
                      for slot, values in self.speakers().items()),
            self.rig.max_lag(), self.rig.shed_frames,
            self.rig.bank.refill_holds, self.rig.failure_frames)

    def __repr__(self):
        return "RoomReport(%s)" % self.summary()
