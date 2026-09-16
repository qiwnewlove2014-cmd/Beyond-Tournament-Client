"""A crossover: the map's ``crossover``, and the stream it makes.

Reported wish, twice over: a speaker behind or beside the room that is only a
**sub** -- the low end of whatever the room is playing, with the mids and the
top not merely quieter but gone -- and the mirror of it, a speaker that is only
a **tweeter**, with the bass and the mids gone instead. Both are one attribute
whose sign says which half the speaker keeps, so a speaker can hold one
crossover and never two (two of them is a band-pass, and nobody is asking for
one). Neither can be an OpenAL filter, and the reason is worth pinning down
here because it is what decides the shape of this feature:

* every filter gain EFX offers is capped at 1.0 (``AL_LOWPASS_MAX_GAIN`` and
  friends), so a filter only ever cuts;
* ``GAIN`` -- the low-pass's overall gain -- multiplies the bass and the mids
  together, so pulling the mids down takes the bass down with them and the
  ratio never moves;
* ``AL_FILTER_BANDPASS`` is the only stock filter with a low band at all, and
  its band sits at unity while ``GAINLF`` can only cut below it: a speaker's
  bass can never be raised above its mids;
* ``AL_EFFECT_EQUALIZER`` does have the bands, but it rides an auxiliary
  *send*, and a send adds a shaped copy -- the dry signal's mids are still
  there, so the strongest honest result is a bass tilt, not a bass cabinet.

So the room filters the audio itself, per speaker, on the frames it is about to
hand over (``libs/audio/cinema/crossover.py``): a real 12 dB/oct crossover,
the same for the song and for a voice, at one number the builder sets -- two
cascaded one-pole sections as a low-pass for a positive mark and as a
high-pass for a negative one. These tests pin the whole promise, offline,
against the fake transport the delay tests use:

* a room with no crossover anywhere renders **byte for byte** what it always
  did -- the shipped jukebox cannot be changed by a feature nobody asked for;
* a bass cabinet is fed the bass and loses the top, a tweeter the top and
  loses the bass (measured, not asserted by eye: a DFT at 4 kHz against the
  same frame's 100 Hz);
* the sign is the side, and the two halves never meet: a mark the reader does
  not understand is the full-range speaker, a *negative* one is a tweeter and
  never "no crossover";
* it is a *stream*, not a per-frame effect: the filter's state carries, a
  refused frame does not walk it on, a delay trim is cut out of the marked
  speaker's own filtered history, and one that stopped is filled from that
  history rather than from the room's unfiltered frames.
"""

import array
import hashlib
import math
import os
import sys
import unittest
from types import SimpleNamespace

import cyal

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from libs.audio.cinema import CinemaRenderer, CinemaSpeakerBank
from libs.audio.cinema.crossover import (FULL_RANGE, MAX_CROSSOVER_HZ,
                                         MAX_TREBLE_HZ, MIN_CROSSOVER_HZ,
                                         MIN_TREBLE_HZ, apply as crossover_apply,
                                         crossover_hz, initial_state, smoothing)
from libs.audio.cinema.layout import CinemaSpeakerSpec, IDEAL_BEARING, coerce_spec
from libs.world_map import Map

ANCHOR = (10.0, 20.0, 0.0)
SAMPLERATE = 48000
SAMPLES = 960          # one 20 ms frame, the size the direct streamer sends
BASS = 100.0
TOP = 4000.0


# --------------------------------------------------------------- fake OpenAL


class FakeSource:
    """Minimal OpenAL source: a FIFO of buffers and a play state."""

    def __init__(self, context):
        self.context = context
        self.position = None
        self.rolloff_factor = None
        self.reference_distance = None
        self.max_distance = None
        self.spatialize = None
        self.direct_channels = None
        self.direct_filter = None
        self.gain = 0.0
        self.buffers = []
        self.buffers_processed = 0
        self.state = cyal.SourceState.STOPPED
        self.played = 0
        self.deleted = False

    @property
    def buffers_queued(self):
        return len(self.buffers)

    def play(self):
        # Counted, because "this speaker was never stopped and restarted" is
        # what re-cutting a room in place means (see TheRoomFollowsTheMarkTests).
        self.played += 1
        self.state = cyal.SourceState.PLAYING

    def pause(self):
        self.state = cyal.SourceState.PAUSED

    def stop(self):
        # What the driver really reports: OpenAL counts a buffer as processed
        # the moment its source is not playing it, so a source that has just
        # been stopped offers back everything it holds (see
        # ``CinemaSpeakerBank._empty_speaker``, which re-cuts a speaker by
        # taking its queue away). A fake that kept the queue after a stop could
        # only ever model a backend that refuses to empty a speaker.
        self.state = cyal.SourceState.STOPPED
        self.buffers_processed = len(self.buffers)

    def delete(self):
        self.deleted = True

    def queue_buffers(self, buffer):
        self.buffers.append(buffer)

    def unqueue_buffers(self):
        if self.buffers_processed <= 0:
            return None
        self.buffers_processed -= 1
        return [self.buffers.pop(0)] if self.buffers else []


class FakeBuffer:
    def __init__(self):
        self.data = None

    def set_data(self, data, sample_rate=None, format=None):
        self.data = bytes(data)


class FakeContext:
    def gen_source(self, **kwargs):
        return FakeSource(self)

    def gen_buffer(self):
        return FakeBuffer()


class FakeAudio:
    def __init__(self):
        self.context = FakeContext()
        self.efx = SimpleNamespace(send=lambda *a, **k: None)
        self.filter = []
        self.position = ANCHOR
        self.volume_categories = {"jukebox": [100], "music": [100]}

    def gen_filter(self, kind, *params):
        return ("filter", kind, params)


class FakeGame:
    def __init__(self):
        self.audio_mngr = FakeAudio()


# -------------------------------------------------------------------- signal


def signal_frame(index, samples=SAMPLES, bass=BASS, top=TOP):
    """One frame of a 100 Hz and a 4 kHz tone together, phase-continuous."""
    values = []
    for step in range(samples):
        when = (index * samples + step) / float(SAMPLERATE)
        values.append(int(9000 * math.sin(2 * math.pi * bass * when)
                          + 9000 * math.sin(2 * math.pi * top * when)))
    data = array.array("h", values).tobytes()
    return data, bytes(data)


def tone_frame(frequency, index=0, samples=SAMPLES, amplitude=9000):
    values = []
    for step in range(samples):
        when = (index * samples + step) / float(SAMPLERATE)
        values.append(int(amplitude * math.sin(2 * math.pi * frequency * when)))
    data = array.array("h", values).tobytes()
    return data, bytes(data)


def samples_of(pcm):
    values = array.array("h")
    values.frombytes(pcm)
    if sys.byteorder != "little":
        values.byteswap()
    return values


def room_hz(room, slot):
    """The crossover a room's slot was built with, as its reader reads it."""
    return room.bank._slot_crossover.get(slot, 0.0)


def magnitude(pcm, frequency, rate=SAMPLERATE):
    """How much of this frame sits at one frequency, as an amplitude."""
    values = samples_of(pcm)
    if not values:
        return 0.0
    step = 2.0 * math.pi * frequency / rate
    real = imaginary = 0.0
    for index, value in enumerate(values):
        angle = step * index
        real += value * math.cos(angle)
        imaginary += value * math.sin(angle)
    return math.hypot(real, imaginary) / len(values) * 2.0


def digests(frames):
    """One short fingerprint per frame (a frame, or a stereo pair of them).

    Compared instead of the frames themselves: ``assertEqual`` on two lists of
    2 KB byte strings hands the difference to ``difflib``, which spends longer
    printing the mistake than the song took to play. A digest fails just as
    loudly and reads at a glance.
    """
    out = []
    for frame in frames:
        if frame is None:
            out.append(None)
        elif isinstance(frame, (tuple, list)):
            out.append(hashlib.sha256(b"|".join(frame)).hexdigest()[:16])
        else:
            out.append(hashlib.sha256(frame).hexdigest()[:16])
    return out


def rms(pcm):
    values = samples_of(pcm)
    if not values:
        return 0.0
    return math.sqrt(sum(value * value for value in values) / len(values))


# ---------------------------------------------------------------------- room


def placed(slot, delay_ms=0.0, crossover=None, radius=8.0):
    from math import cos, radians, sin
    angle = radians(IDEAL_BEARING[slot])
    return CinemaSpeakerSpec(
        slot,
        (ANCHOR[0] + sin(angle) * radius, ANCHOR[1] + cos(angle) * radius,
         ANCHOR[2]),
        delay_ms=delay_ms,
        crossover=crossover,
    )


class Room:
    """A front pair over the fake transport, one frame per tick."""

    SLOTS = ("front_l", "front_r")
    PROFILE = "front_only"

    def __init__(self, crossovers=None, delays=None, samples=SAMPLES,
                 maker=signal_frame):
        self.crossovers = dict(crossovers or {})
        self.delays = dict(delays or {})
        self.profile = self.PROFILE
        specs = [placed(slot, self.delays.get(slot, 0.0),
                        self.crossovers.get(slot)) for slot in self.SLOTS]
        # The channel verdict is not what this file is about (see
        # test_cinema_live for it), and with it off the front pair passes the
        # signal through untouched -- which is exactly what makes "this
        # speaker's frames are the frames it was always handed" checkable.
        self.renderer = CinemaRenderer(ANCHOR, self.profile, None, specs=specs,
                                       detect_channels=False)
        self.bank = CinemaSpeakerBank(FakeGame(), self.renderer)
        self.samples = samples
        self.maker = maker
        self.index = 0
        self.left = self.right = None
        # slot -> the PCM of every frame handed to that speaker, and the
        # programme position of each frame the room really accepted.
        self.handed = {slot: [] for slot in self.SLOTS}
        self.fed = []

    @property
    def sources(self):
        """The room's speakers as they stand right now.

        A re-shape keeps the speakers that did not change but hands the bank a
        new mapping, so reading them here is the only way to see a room that
        has been re-cut (see ``remap``).
        """
        return self.bank.slot_sources

    def remap(self, crossovers, slots=None, profile=None, delays=None):
        """The builder's edit, and the room re-resolved exactly as the game does.

        A live room is re-shaped when the map under it changes
        (``plugin.acquire`` -> ``CinemaSpeakerBank.reconfigure``), and what
        makes a crossover change count as one is the room's own signature
        (``router.CinemaRenderer.signature``) -- so the new renderer here is
        built the way the transports build it, and handed over the same way.
        """
        self.crossovers = dict(crossovers or {})
        if delays is not None:
            self.delays = dict(delays)
        self.profile = profile or self.profile
        specs = [placed(slot, self.delays.get(slot, 0.0),
                        self.crossovers.get(slot))
                 for slot in (slots or self.SLOTS)]
        self.renderer = CinemaRenderer(ANCHOR, self.profile, None, specs=specs,
                                       detect_channels=False)
        return self.bank.reconfigure(self.renderer)

    def tick(self, again=False):
        """Consume a buffer on every playing speaker, then queue one frame.

        ``again`` re-offers the frame that was just refused -- which is what a
        transport does with one it could not queue (the relay receiver and the
        direct streamer both retry it), and is the only way to ask whether a
        filter moved on for audio nobody heard.
        """
        for source in self.sources.values():
            if source.state == cyal.SourceState.PLAYING and source.buffers_queued:
                source.buffers_processed += 1
        self.bank.reclaim()
        if not again:
            self.left, self.right = self.maker(self.index, self.samples)
            self.index += 1
        queued = self.bank.queue_frame(self.left, self.right)
        if queued:
            for slot, source in self.sources.items():
                if source.buffers:
                    self.handed.setdefault(slot, []).append(
                        source.buffers[-1].data)
            self.fed.append(self.index - 1)
        if not self.bank.playing():
            self.bank.start_playback()
        return queued

    def stream(self, slot):
        """The filtered stream this slot should have been handed, exactly."""
        hz = room_hz(self, slot)
        state = None
        out = []
        for index in self.fed:
            (frame,), state = crossover_apply((self.maker(index, self.samples)[0],),
                                              state, hz)
            out.append(frame)
        return out

    def run(self, ticks=40):
        for _ in range(ticks):
            self.tick()
        return self

    def settle(self, ticks=20, depth=5):
        """Play, then run the queue deeper than the room's own low-queue floor.

        This harness consumes one buffer per speaker per tick and queues one,
        so a room it starts by itself ends up a single frame deep -- exactly the
        case the room's low-queue hold exists for. A *re-cut* is a change to a
        room that is playing, so the tests that make one fill the queue first,
        the way a transport that ran ahead of the sink does.
        """
        self.run(ticks)
        for _ in range(depth):
            self.left, self.right = self.maker(self.index, self.samples)
            self.index += 1
            if self.bank.queue_frame(self.left, self.right):
                self.fed.append(self.index - 1)
        if not self.bank.playing():
            self.bank.start_playback()
        return self

    def stall(self, slot):
        """A speaker that stopped holding its queue (an underrun, a hiccup)."""
        source = self.sources[slot]
        source.stop()
        self.bank._pools[slot].extend(source.buffers)
        source.buffers.clear()
        source.buffers_processed = 0


# -------------------------------------------------------- the map's own mark


class CrossoverNumberTests(unittest.TestCase):
    """One reader of the map's number, and the promise that the sign is the side."""

    def test_a_map_that_says_nothing_is_a_full_range_speaker(self):
        for value in (None, "", 0, 0.0, -0.0, -0, "loud", float("inf"),
                      float("nan")):
            self.assertEqual(crossover_hz(value), FULL_RANGE, repr(value))

    def test_a_number_is_kept_and_the_ends_are_clamped(self):
        self.assertEqual(crossover_hz(120), 120.0)
        self.assertEqual(crossover_hz("120"), 120.0)
        # Nobody's typo may silence a speaker, and a speaker with a crossover
        # above 300 Hz is not a bass cabinet: both ends are clamped rather
        # than obeyed.
        self.assertEqual(crossover_hz(1), MIN_CROSSOVER_HZ)
        self.assertEqual(crossover_hz(5000), MAX_CROSSOVER_HZ)

    def test_a_negative_mark_is_the_tweeter_half_and_is_never_nothing(self):
        # The sign is the side the speaker keeps, so a negative mark is a
        # crossover like any other: a reader that treated it as "not above
        # zero" would hand a tweeter the room's full-range frames.
        self.assertEqual(crossover_hz(-3000), -3000.0)
        self.assertEqual(crossover_hz("-1500"), -1500.0)
        self.assertEqual(crossover_hz(-20), -MIN_TREBLE_HZ)
        self.assertEqual(crossover_hz(-90000), -MAX_TREBLE_HZ)

    def test_the_two_halves_never_meet(self):
        # The sign picks which pair of ends a number is settled into, so a
        # speaker is a sub or a tweeter and never one clamped to a corner the
        # other half could not play.
        self.assertEqual(crossover_hz(400), MAX_CROSSOVER_HZ)
        self.assertEqual(crossover_hz(-400), -MIN_TREBLE_HZ)
        self.assertEqual(crossover_hz(120), 120.0)
        self.assertEqual(crossover_hz(-800), -800.0)
        self.assertNotEqual(crossover_hz(120), crossover_hz(-120))
        self.assertNotEqual(FULL_RANGE, crossover_hz(-120))

    def test_a_lower_corner_is_a_tighter_filter(self):
        self.assertEqual(smoothing(0), 0.0)
        values = [smoothing(hz) for hz in (40, 120, 300)]
        self.assertTrue(all(0.0 < value < 1.0 for value in values), values)
        self.assertEqual(values, sorted(values))

    def test_a_negative_mark_is_a_corner_like_any_other(self):
        # The corner is the magnitude of the mark, so a tweeter is a real
        # filter and never "no corner at all": a reader that ignored a
        # negative mark (``if hz <= 0: return 0.0``) would hand a tweeter the
        # room's unfiltered frames.
        self.assertGreater(smoothing(-3000), 0.0)
        self.assertEqual(smoothing(-3000), smoothing(-3000.0))
        self.assertEqual(smoothing(-3000), smoothing("-3000"))
        self.assertLess(smoothing(-800), smoothing(-3000))
        self.assertLess(smoothing(-3000), smoothing(-6000))


# ------------------------------------------------------------- the low-pass


class TheLowPassTests(unittest.TestCase):
    """The filter itself: bass kept, top gone, and a continuous stream."""

    def test_a_bass_cabinet_keeps_the_bass_and_loses_the_top(self):
        state = None
        plain_state = None
        raw_bass = raw_top = bass = top = 0.0
        for index in range(40):
            frame = signal_frame(index)[0]
            (raw,) = frame,
            plain_state = plain_state
            raw_bass = max(raw_bass, magnitude(raw, BASS))
            raw_top = max(raw_top, magnitude(raw, TOP))
            (filtered,), state = crossover_apply((frame,), state, 120)
            bass = max(bass, magnitude(filtered, BASS))
            top = max(top, magnitude(filtered, TOP))
        self.assertGreater(bass, 0.5 * raw_bass,
                           "the low end of a bass cabinet is the point")
        self.assertLess(top, 0.02 * raw_top,
                        "the top of a bass cabinet is not merely quieter")

    def test_a_lower_crossover_keeps_less_of_the_middle(self):
        kept = []
        for hz in (40, 120, 300):
            state = None
            peak = 0.0
            for index in range(30):
                (filtered,), state = crossover_apply((tone_frame(200, index)[0],),
                                                     state, hz)
                peak = max(peak, magnitude(filtered, 200))
            kept.append(peak)
        self.assertEqual(kept, sorted(kept), kept)
        # A 200 Hz tone at 40 Hz against 300 Hz is a spread of more than 9 dB:
        # the number really is where the bass band ends.
        self.assertGreater(kept[-1], kept[0] * 3, kept)

    def test_a_stream_is_not_a_frame(self):
        """Split into frames, the filter is the same filter -- state and all."""
        whole = b"".join(signal_frame(index)[0] for index in range(8))
        once, _state = crossover_apply((whole,), None, 120)
        state = None
        pieces = []
        for index in range(8):
            (piece,), state = crossover_apply((signal_frame(index)[0],), state, 120)
            pieces.append(piece)
        self.assertEqual(b"".join(pieces), once[0])
        # And the frame a stateless filter would produce is a different thing,
        # which is what makes the state worth carrying.
        fresh = [crossover_apply((signal_frame(index)[0],), None, 120)[0][0]
                 for index in range(8)]
        self.assertNotEqual(fresh[1], pieces[1])

    def test_a_frame_can_be_offered_again_untouched(self):
        frame = signal_frame(3)[0]
        state = initial_state(1)
        first, after = crossover_apply((frame,), state, 120)
        second, again = crossover_apply((frame,), state, 120)
        self.assertEqual(first, second)
        self.assertEqual(after, again)
        self.assertEqual(state, initial_state(1), "the filter mutated its input")

    def test_the_two_channels_of_a_pair_do_not_share_a_state(self):
        left, _right = tone_frame(BASS)
        _left, right = tone_frame(TOP)
        (out_left, out_right), state = crossover_apply((left, right), None, 120)
        self.assertGreater(magnitude(out_left, BASS), 0.5 * magnitude(left, BASS))
        self.assertLess(magnitude(out_right, TOP), 0.02 * magnitude(right, TOP))
        self.assertEqual(len(state), 4)

    def test_bytes_it_cannot_read_are_handed_straight_back(self):
        odd = b"\x01\x02\x03"
        (out,), state = crossover_apply((odd,), None, 120)
        self.assertEqual(out, odd)
        self.assertEqual(state, initial_state(1))

    def test_full_range_is_the_room_it_always_was(self):
        frame = signal_frame(0)[0]
        (out,), state = crossover_apply((frame,), (5.0, 5.0), 0)
        self.assertEqual(out, frame, "a speaker with no crossover is untouched")
        self.assertEqual(state, (5.0, 5.0), "and its state is left where it was")


# ------------------------------------------------------------ the high-pass


class TheTweeterTests(unittest.TestCase):
    """The mirror: the top kept, the bass and the mids gone.

    A tweeter is the other half of one split, so it is the same module, the
    same slope and the same state -- read the other way round. What it must
    not be is a *quieter* speaker or a bass cabinet with the volume down: the
    test measures the whole band, low and high.
    """

    def test_a_tweeter_keeps_the_top_and_loses_the_bass(self):
        state = None
        plain = signal_frame(0)[0]
        top = bass = 0.0
        for index in range(40):
            (filtered,), state = crossover_apply((signal_frame(index)[0],),
                                                 state, -2000)
            top = max(top, magnitude(filtered, TOP))
            bass = max(bass, magnitude(filtered, BASS))
        self.assertGreater(top, 0.5 * magnitude(plain, TOP),
                           "the top of a tweeter is the point")
        self.assertLess(bass, 0.02 * magnitude(plain, BASS),
                        "the bass of a tweeter is not merely quieter")

    def test_a_higher_corner_keeps_less_of_the_middle(self):
        kept = []
        for hz in (-800, -1500, -3000):
            state = None
            peak = 0.0
            for index in range(30):
                (filtered,), state = crossover_apply((tone_frame(600, index)[0],),
                                                     state, hz)
                peak = max(peak, magnitude(filtered, 600))
            kept.append(peak)
        self.assertEqual(kept, sorted(kept, reverse=True), kept)
        self.assertGreater(kept[0], kept[-1] * 3, kept)

    def test_the_two_halves_meeting_at_one_corner_make_one_split(self):
        # A bass cabinet and a tweeter at the same corner are the two sides of
        # one crossover point, which is what makes a two-way room possible: a
        # 100 Hz tone belongs to the side the sign names and to nothing else.
        plain = tone_frame(BASS)[0]
        kept_below = kept_above = 0.0
        for index in range(30):
            frame = tone_frame(BASS, index)[0]
            (below,), _state = crossover_apply((frame,), None, 800)
            (above,), _state = crossover_apply((frame,), None, -800)
            kept_below = max(kept_below, magnitude(below, BASS))
            kept_above = max(kept_above, magnitude(above, BASS))
        self.assertGreater(kept_below, 0.5 * magnitude(plain, BASS),
                           "the bass half lost the bass")
        self.assertLess(kept_above, 0.02 * kept_below,
                        "the treble half kept the bass")

    def test_a_tweeter_is_a_stream_too(self):
        whole = b"".join(signal_frame(index)[0] for index in range(8))
        once, _state = crossover_apply((whole,), None, -3000)
        state = None
        pieces = []
        for index in range(8):
            (piece,), state = crossover_apply((signal_frame(index)[0],), state, -3000)
            pieces.append(piece)
        self.assertEqual(b"".join(pieces), once[0])
        fresh = [crossover_apply((signal_frame(index)[0],), None, -3000)[0][0]
                 for index in range(8)]
        self.assertNotEqual(fresh[1], pieces[1], "the state was not carried")

    def test_a_step_cannot_overflow_the_buffer(self):
        # A cascade of high-pass sections is not a convex blend of its input:
        # a full-scale step can overshoot it, and ``array('h')`` raises rather
        # than wrap. The filter has to clamp, or the loudest moment of a song
        # takes the room down.
        step = array.array("h", [32767 if (i // 24) % 2 else -32768
                                 for i in range(SAMPLES)]).tobytes()
        for mark in (-40, -800, -3000, -6000):
            (out,), _state = crossover_apply((step,), None, mark)
            self.assertEqual(len(out), len(step), mark)
            self.assertLessEqual(max(abs(value) for value in samples_of(out)), 32768,
                                 mark)

    def test_bytes_it_cannot_read_are_handed_straight_back(self):
        odd = b"\x01\x02\x03"
        (out,), state = crossover_apply((odd,), None, -3000)
        self.assertEqual(out, odd)
        self.assertEqual(state, initial_state(1))

    def test_a_bass_cabinet_and_a_tweeter_at_one_frequency_are_two_streams(self):
        room = Room(crossovers={"front_l": 120, "front_r": -120}).run(20)
        self.assertEqual(sorted(room.bank._crossover_streams), [-MIN_TREBLE_HZ, 120])
        self.assertNotEqual(digests(room.handed["front_l"]),
                            digests(room.handed["front_r"]))
        low = room.handed["front_l"][-1]
        high = room.handed["front_r"][-1]
        plain = signal_frame(0)[0]
        self.assertGreater(magnitude(low, BASS), 0.5 * magnitude(plain, BASS))
        self.assertLess(magnitude(low, TOP), 0.02 * magnitude(plain, TOP))
        self.assertLess(magnitude(high, BASS), 0.02 * magnitude(plain, BASS))
        self.assertGreater(magnitude(high, TOP), 0.5 * magnitude(plain, TOP))


# --------------------------------------------------------------- the map


class MapMarkTests(unittest.TestCase):
    """``crossover`` from a map element to the room's reader, and no further."""

    def speaker(self, **attributes):
        game = SimpleNamespace(audio_mngr=FakeAudio(), map=None)
        world = Map(game)
        world.spawn_cinemaSpeaker(minx=0.5, maxx=1.5, miny=0.5, maxy=1.5,
                                  minz=0, maxz=1, id="spk", channel="rear_l",
                                  **attributes)
        return world.get_cinema_speakers()[0]

    def test_the_map_carries_the_mark_to_the_speaker_and_its_spec(self):
        spec = coerce_spec(self.speaker(crossover=120))
        self.assertEqual(spec.crossover, 120.0)

    def test_a_speaker_nobody_marked_is_full_range(self):
        # An element the map never marked carries no crossover at all (the
        # room asks the map, not a default), and one written as 0 says the
        # same thing in the map's own words.
        for attributes in ({}, {"crossover": 0}):
            spec = coerce_spec(self.speaker(**attributes))
            self.assertEqual(crossover_hz(spec.crossover), 0.0, attributes)
        self.assertEqual(coerce_spec({"x": 1, "y": 2, "z": 0}).crossover, None)

    def test_a_value_the_map_cannot_mean_is_saying_nothing(self):
        self.assertEqual(crossover_hz(coerce_spec(self.speaker(crossover="loud")).crossover),
                        0.0)

    def test_a_ring_speaker_is_never_a_bass_cabinet(self):
        from libs.audio.cinema.layout import CinemaLayout
        layout = CinemaLayout(ANCHOR, [placed("front_l")], use_ring=True)
        self.assertEqual(layout.crossover("front_l"), 0.0,
                         "the mark belongs to a speaker a builder placed")
        self.assertEqual(layout.crossover("rear_l"), 0.0)

    def test_the_layout_reports_a_marked_speaker(self):
        from libs.audio.cinema.layout import CinemaLayout
        layout = CinemaLayout(ANCHOR, [placed("front_r", crossover=120),
                                       placed("front_l")], use_ring=False)
        self.assertEqual(layout.crossover("front_r"), 120.0)
        self.assertEqual(layout.crossover("front_l"), 0.0)


# ------------------------------------------------------------ the song


class TheRoomPlaysTheBassTests(unittest.TestCase):
    """What the bank hands each speaker, frame by frame."""

    def test_a_room_with_no_crossover_queues_the_very_frames_it_always_did(self):
        room = Room().run(30)
        self.assertEqual(room.bank._crossover_streams, {},
                         "nothing may hold a filter nobody asked for")
        for index, (left, right) in enumerate(zip(room.handed["front_l"],
                                                  room.handed["front_r"])):
            expected = signal_frame(index)[0]
            self.assertEqual(left, expected, f"frame {index} was not the signal")
            self.assertEqual(right, expected, f"frame {index} was not the signal")

    def test_only_the_speaker_that_was_marked_changes(self):
        room = Room(crossovers={"front_r": 120}).run(30)
        last = room.handed["front_r"][-1]
        plain = room.handed["front_l"][-1]
        self.assertEqual(plain, signal_frame(room.index - 1)[0])
        self.assertNotEqual(last, plain)
        self.assertLess(magnitude(last, TOP), 0.02 * magnitude(plain, TOP))
        self.assertGreater(magnitude(last, BASS), 0.5 * magnitude(plain, BASS))

    def test_the_cabinet_gets_the_stream_the_crossover_makes(self):
        """The bank's frames are the module's stream, byte for byte."""
        room = Room(crossovers={"front_r": 120}).run(20)
        self.assertEqual(digests(room.handed["front_r"]),
                         digests(room.stream("front_r")))

    def test_speakers_dialled_alike_share_one_stream(self):
        room = Room(crossovers={"front_l": 120, "front_r": 120}).run(20)
        self.assertEqual(sorted(room.bank._crossover_streams), [120])
        self.assertEqual(digests(room.handed["front_l"]),
                         digests(room.handed["front_r"]))

    def test_the_stream_is_per_crossover_not_per_speaker(self):
        room = Room(crossovers={"front_l": 120, "front_r": 200}).run(20)
        self.assertEqual(sorted(room.bank._crossover_streams), [120, 200])
        self.assertNotEqual(digests(room.handed["front_l"]),
                            digests(room.handed["front_r"]))
        for slot in ("front_l", "front_r"):
            self.assertLess(magnitude(room.handed[slot][-1], TOP),
                            0.02 * magnitude(signal_frame(0)[0], TOP), slot)


class TheCabinetIsAStreamTests(unittest.TestCase):
    """A refused frame, a trim, and a cabinet that stopped and came back."""

    def test_a_refused_frame_does_not_walk_the_filter_on(self):
        room = Room(crossovers={"front_r": 120})
        room.run(5)
        state_before = room.bank._crossover_streams[120]["state"]
        history_before = digests(room.bank._crossover_streams[120]["history"])
        # No free buffer anywhere: the whole frame is refused, and the
        # transport re-offers that very frame next tick.
        pool = room.bank._pools["front_l"]
        room.bank._pools["front_l"] = []
        self.assertFalse(room.tick(), "a frame with no buffer must be refused")
        room.bank._pools["front_l"] = pool
        self.assertEqual(room.bank._crossover_streams[120]["state"], state_before,
                         "a refused frame advanced the filter")
        self.assertEqual(digests(room.bank._crossover_streams[120]["history"]),
                         history_before, "a refused frame entered the history")
        self.assertTrue(room.tick(again=True), "the retry has to be accepted")
        # The retry produced the frame that stream would have made anyway,
        # not a second filtering of the same audio.
        self.assertEqual(digests(room.handed["front_r"]),
                         digests(room.stream("front_r")))

    def test_a_trimmed_cabinet_is_cut_out_of_its_own_history(self):
        # A trim deeper than the audio the room holds on its first frames is
        # exactly the case ``_slot_feeds`` retries with the trim cut back -- and
        # the fallback window has to be the cabinet's own filtered frame, or
        # the first bar of a bass cabinet comes back full range.
        room = Room(crossovers={"front_r": 120}, delays={"front_r": 60.0}).run(40)
        plain = signal_frame(0)[0]
        for index, frame in enumerate(room.handed["front_r"]):
            self.assertLess(magnitude(frame, TOP), 0.02 * magnitude(plain, TOP),
                            f"frame {index} of the cabinet still had the top")
            self.assertGreater(magnitude(frame, BASS), 0.3 * magnitude(plain, BASS),
                               f"frame {index} of the cabinet lost the bass")
        # And the trim itself is still the room's timing: the count of frames
        # the cabinet sits deeper in its queue is the hold the trim asks for.
        self.assertEqual(room.bank.hold_frames("front_r"),
                         room.bank.hold_frames("front_l") + 3)
        self.assertEqual(room.handed["front_l"][-1],
                         signal_frame(room.index - 1)[0])

    def test_a_cabinet_that_stopped_is_filled_from_its_own_stream(self):
        room = Room(crossovers={"front_r": 120}).run(30)
        self.assertGreater(room.sources["front_l"].buffers_queued, 0)
        room.stall("front_r")
        self.assertEqual(room.sources["front_r"].buffers_queued, 0)
        room.bank.realign(play=False)
        refilled = list(room.sources["front_r"].buffers)
        self.assertTrue(refilled, "the cabinet was never put back in step")
        plain = signal_frame(0)[0]
        for buffer in refilled:
            self.assertLess(magnitude(buffer.data, TOP),
                            0.02 * magnitude(plain, TOP),
                            "a fill from the room's history handed it the mids")
            self.assertGreater(magnitude(buffer.data, BASS),
                               0.3 * magnitude(plain, BASS))
        self.assertEqual(room.sources["front_r"].buffers_queued,
                         room.sources["front_l"].buffers_queued,
                         "the cabinet came back on the room's own instant")


# ---------------------------------------------------------- the mark, live


class TheMarkIsPartOfTheRoomTests(unittest.TestCase):
    """A crossover makes a room *that* room, so a re-resolve notices it.

    The reported failure: a builder dialled a crossover and heard it minutes
    later, when the room happened to be built anew (which stops the song). A
    re-resolve compared the room's *shape*, and a bass mark is not a shape, so
    it handed back the renderer the bank already held and the new number was
    never read at all. The voicing had the same hole (it is the same kind of
    per-speaker property), so both are pinned here.
    """

    def renderer(self, crossover=None, tone=None):
        right = placed("front_r", 0.0, crossover)
        if tone is not None:
            right = CinemaSpeakerSpec(right.slot, right.position,
                                      tone=tone, crossover=crossover)
        return CinemaRenderer(
            ANCHOR, "front_only", None,
            specs=[placed("front_l"), right],
            detect_channels=False,
        )

    def test_a_voicing_dialled_is_a_different_room_too(self):
        self.assertNotEqual(self.renderer().signature,
                            self.renderer(tone=0.4).signature)

    def test_a_mark_dialled_is_a_different_room(self):
        self.assertNotEqual(self.renderer().signature,
                            self.renderer(120).signature)

    def test_one_mark_is_one_room_however_the_map_wrote_it(self):
        # The reader normalises the number, so a re-resolve must not re-cut a
        # room because the map said "120" instead of 120.
        for again in (120, 120.0, "120"):
            self.assertEqual(self.renderer(120).signature,
                             self.renderer(again).signature, repr(again))

    def test_a_value_the_map_cannot_mean_is_the_room_without_a_mark(self):
        for nonsense in (None, 0, "loud"):
            self.assertEqual(self.renderer().signature,
                             self.renderer(nonsense).signature, repr(nonsense))

    def test_the_sign_is_part_of_the_mark_a_room_is_re_cut_for(self):
        # A tweeter is not a bass cabinet at the same frequency, so a re-resolve
        # has to see the difference -- the sign is the side the speaker keeps,
        # and leaving it out of the room's own description is how a speaker
        # would keep playing the half the builder just replaced.
        self.assertNotEqual(self.renderer(120).signature,
                            self.renderer(-120).signature)
        self.assertNotEqual(self.renderer(-120).signature,
                            self.renderer().signature)
        self.assertEqual(self.renderer(-3000).signature,
                         self.renderer("-3000").signature)


class TheRoomFollowsTheMarkTests(unittest.TestCase):
    """A mark changed while the song plays is heard, in place."""

    def test_a_mark_dialled_mid_song_is_played_without_a_rebuild(self):
        room = Room().settle()
        left = room.sources["front_l"]
        cabinet = room.sources["front_r"]
        plays = {slot: source.played for slot, source in room.sources.items()}
        depth = {slot: source.buffers_queued for slot, source in room.sources.items()}
        self.assertTrue(room.remap({"front_r": 120}), "the room was not re-cut")
        # *Neither* speaker is stopped, emptied or replaced: the room was
        # re-cut, not rebuilt, and no speaker was interrupted for it.
        self.assertIs(room.sources["front_l"], left)
        self.assertIs(room.sources["front_r"], cabinet)
        self.assertEqual(room.sources["front_l"].state,
                         cyal.SourceState.PLAYING)
        self.assertEqual(room.sources["front_r"].state,
                         cyal.SourceState.PLAYING)
        self.assertEqual(
            {slot: source.played for slot, source in room.sources.items()},
            plays, "a speaker was stopped and started for a re-cut")
        self.assertEqual(
            {slot: source.buffers_queued for slot, source in room.sources.items()},
            depth, "a speaker's queue was thrown away for a re-cut")
        self.assertTrue(room.bank.playing())
        # The frames it already holds are the song at the room's own instant --
        # a crossover is not a seek -- and they play out; from its next frame
        # on it is handed the cabinet's stream.
        room.run(6)
        for buffer in room.sources["front_r"].buffers[-2:]:
            self.assertLess(magnitude(buffer.data, TOP),
                            0.05 * magnitude(buffer.data, BASS),
                            "the cabinet was still being handed its mids")
        self.assertEqual(room.sources["front_r"].buffers_queued,
                         room.sources["front_l"].buffers_queued)

    def test_pressing_the_line_four_times_never_touches_a_speaker(self):
        """The reported one: adjust it three or four times and it stumbles.

        Every press used to empty that speaker and start it again -- a stop in
        the middle of a buffer, four times over. A re-cut is a change of
        programme, so nothing about the speaker is touched at all: the frames
        it holds play out and the new band takes over from the next frame.
        """
        room = Room().settle()
        plays = {slot: source.played for slot, source in room.sources.items()}
        # Settling runs the room at a single frame deep, which is the case its
        # own low-queue hold exists for; what is measured here is what the
        # presses add to that.
        holds = room.bank.refill_holds
        for mark in (120, -3000, 120, -3000, 0, 120):
            room.remap({"front_r": mark})
            room.run(3)
        self.assertEqual(
            {slot: source.played for slot, source in room.sources.items()},
            plays, "a press stopped and started the speaker")
        self.assertTrue(room.bank.playing(), "the room went silent")
        self.assertEqual(room.bank.refill_holds, holds,
                         "the room was held for a re-cut")
        depths = {slot: source.buffers_queued for slot, source in room.sources.items()}
        self.assertEqual(len(set(depths.values())), 1,
                         "the room came apart at the re-cut: %s" % (depths,))
        self.assertGreaterEqual(min(depths.values()), 1, "a speaker ran dry")
        self.assertEqual(room.bank._slot_crossover["front_r"], 120.0)
        # And the last band really is the one it is being handed.
        room.run(6)
        for buffer in room.sources["front_r"].buffers[-2:]:
            self.assertLess(magnitude(buffer.data, TOP),
                            0.05 * magnitude(buffer.data, BASS),
                            "the cabinet was still being handed its mids")

    def test_the_new_stream_is_on_the_room_s_own_instant(self):
        room = Room().settle()
        room.remap({"front_r": 120})
        group = room.bank._crossover_streams[120]
        self.assertEqual(len(group["history"]), len(room.bank._recent),
                         "the cabinet's stream is not the room's own length")
        self.assertIsNotNone(group["state"], "the stream was never walked")
        # And it came back at the room's depth, not at the front of a fresh
        # queue: a queue's worth of the song was not replayed into it.
        self.assertEqual(room.sources["front_r"].buffers_queued,
                         room.sources["front_l"].buffers_queued)

    def test_a_mark_swapped_for_the_other_half_re_cuts_the_speaker(self):
        room = Room(crossovers={"front_r": 120}).settle()
        room.run(10)
        plain = signal_frame(0)[0]
        self.assertGreater(magnitude(room.handed["front_r"][-1], BASS),
                           0.3 * magnitude(plain, BASS))
        # The builder changes their mind: the same speaker is now the tweeter.
        left = room.sources["front_l"]
        self.assertTrue(room.remap({"front_r": -3000}), "the swap was not seen")
        self.assertIs(room.sources["front_l"], left, "the room was rebuilt")
        self.assertEqual(room.bank._slot_crossover["front_r"], -3000.0)
        room.run(10)
        last = room.handed["front_r"][-1]
        self.assertLess(magnitude(last, BASS), 0.05 * magnitude(last, TOP),
                        "the speaker kept the bass it was just moved off")
        self.assertGreater(magnitude(last, TOP), 0.3 * magnitude(plain, TOP))

    def test_it_plays_the_room_s_own_instant_from_then_on(self):
        room = Room().settle()
        room.remap({"front_r": 120})
        room.run(20)
        # Nothing about this speaker is a stream of its own afterwards: the
        # frames it is handed are the module's own low-pass of the room's
        # frames, at the room's programme positions.
        self.assertEqual(digests(room.handed["front_r"][-6:]),
                         digests(room.stream("front_r")[-6:]))

    def test_the_mark_taken_away_gives_the_room_s_own_frames_back(self):
        room = Room(crossovers={"front_r": 120}).settle()
        self.assertNotEqual(room.handed["front_r"][-1],
                            room.handed["front_l"][-1])
        self.assertTrue(room.remap({}), "the way back was not taken")
        self.assertEqual(room.bank._slot_crossover["front_r"], 0.0)
        room.run(6)
        for offset, frame in enumerate(room.handed["front_r"][-3:]):
            self.assertEqual(frame, signal_frame(room.fed[-3 + offset])[0],
                             "a full-range speaker must be the room's frames")
        self.assertEqual(room.sources["front_r"].buffers_queued,
                         room.sources["front_l"].buffers_queued)

    def test_every_speaker_changed_at_once_keeps_the_room_s_clock(self):
        room = Room().settle()
        start = room.bank._start_frame
        room.remap({"front_l": 120, "front_r": 120})
        self.assertTrue(room.bank.playing(), "the room went silent")
        self.assertEqual(room.bank._start_frame, start,
                         "the room's clock was restarted")
        self.assertEqual(room.sources["front_l"].buffers_queued,
                         room.sources["front_r"].buffers_queued)
        # Speakers dialled alike are one stream again, and it is the room's
        # own instant of it.
        self.assertEqual(sorted(room.bank._crossover_streams), [120])
        self.assertEqual(digests(room.handed["front_l"][-4:]),
                         digests(room.handed["front_r"][-4:]))

    def test_a_speaker_placed_mid_song_is_born_with_its_mark(self):
        room = Room().settle()
        self.assertNotIn("front_c", room.sources)
        room.remap({"front_c": 120},
                   slots=("front_l", "front_r", "front_c"),
                   profile="front_stage")
        self.assertEqual(room.bank._slot_crossover["front_c"], 120.0,
                         "a speaker placed mid-song was born full range")
        group = room.bank._crossover_streams[120]
        self.assertEqual(len(group["history"]), len(room.bank._recent),
                         "the joining cabinet's stream was not warmed, so its "
                         "fill comes out of the room's unfiltered frames")
        for buffer in room.sources["front_c"].buffers:
            self.assertLess(magnitude(buffer.data, TOP),
                            0.05 * magnitude(buffer.data, BASS),
                            "the cabinet joined the room with its mids")

    def test_a_paused_room_keeps_its_queue_and_takes_the_mark(self):
        room = Room().settle()
        held = list(room.sources["front_r"].buffers)
        room.bank.set_paused(True)
        room.remap({"front_r": 120})
        # A held room is not touched -- nothing it is holding may be thrown
        # away while it is paused -- but the mark is taken, and it is fed the
        # new stream from its next frame on.
        self.assertEqual(room.bank._slot_crossover["front_r"], 120.0)
        self.assertEqual(list(room.sources["front_r"].buffers), held)
        room.bank.set_paused(False)
        room.run(8)
        last = room.handed["front_r"][-1]
        self.assertLess(magnitude(last, TOP), 0.05 * magnitude(last, BASS),
                        "the cabinet resumed with its mids")

    def test_a_room_with_no_mark_is_never_re_cut(self):
        room = Room().settle()
        left = room.sources["front_l"]
        plays = left.played
        self.assertFalse(room.remap({}), "an unchanged room was re-shaped")
        self.assertIs(room.sources["front_l"], left)
        self.assertEqual(left.played, plays, "a room was restarted for nothing")
        self.assertEqual(room.bank._crossover_streams, {},
                         "a stream nobody asked for")


if __name__ == "__main__":
    unittest.main()
