"""A speaker's delay trim must be audible from the first bar of a song.

Reported from a live test room: an installer dials a delay into the speakers
of each cabinet, starts a song, and hears every speaker *together* -- as if
the trims were not set at all -- and only some minutes later do the cabinets
start to come apart in time. The cause was in ``CinemaSpeakerBank``:

* the trim was played as a *content* cut (`_delayed_window`): a trimmed
  speaker was fed audio cut back by its trim, as a fresh buffer;
* a speaker cannot be fed that cut until the room holds that much audio, so
  the trimmed speaker was *starved* by exactly its own trim -- the cut and
  the starvation cancelled out and its queue head landed on the very same
  sample as an untrimmed speaker's;
* the room then starts when the *shallowest* queue reaches its pre-buffer and
  starts every speaker at once, so the two heads -- now identical -- became
  audible together. The trim was gone: not merely rounded, but cancelled.

It came back after a while because a recovery (`realign`) fills a shallow
speaker from the frames the room still holds, and *that* fill is not starved.

A trim is latency, so it has to be held: the speaker sits as many whole frames
deeper than an untrimmed one (the room keeps feeding it meanwhile) and the
sub-frame remainder is the sample-exact cut that already existed. These tests
pin the whole chain, offline, against a fake transport that feeds frames,
consumes one buffer per frame and retries the room's start exactly as the
relay receiver and the direct streamer do.
"""

import array
import os
import sys
import unittest
from types import SimpleNamespace

import cyal

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from libs.audio.cinema import CinemaRenderer, CinemaSpeakerBank
from libs.audio.cinema.layout import CinemaSpeakerSpec

ANCHOR = (10.0, 20.0, 0.0)
SAMPLERATE = 48000
TAPE = 30000          # the test signal wraps here, so a lag below that is exact


class FakeSource:
    """Minimal OpenAL source: a FIFO of buffers, a play state, a tape."""

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
        self.played = []
        self.deleted = False

    @property
    def buffers_queued(self):
        return len(self.buffers)

    def play(self):
        self.state = cyal.SourceState.PLAYING

    def pause(self):
        self.state = cyal.SourceState.PAUSED

    def stop(self):
        self.state = cyal.SourceState.STOPPED

    def delete(self):
        self.deleted = True

    def queue_buffers(self, buffer):
        self.buffers.append(buffer)

    def unqueue_buffers(self):
        """Hand back the buffer that just finished, the way cyal does."""
        if self.buffers_processed <= 0:
            return None
        self.buffers_processed -= 1
        if not self.buffers:
            return []
        buffer = self.buffers.pop(0)
        samples = array.array("h", buffer.data)
        if samples:
            self.played.append(samples[0])
        return [buffer]


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


def ramp_frame(index, samples=960):
    """One frame of a sawtooth: sample value == its own programme position."""
    value = array.array("h", [(index * samples + step) % TAPE
                              for step in range(samples)])
    data = value.tobytes()
    return data, data


def placed(slot, delay_ms=0.0, radius=8.0):
    from math import cos, radians, sin
    from libs.audio.cinema.layout import IDEAL_BEARING
    angle = radians(IDEAL_BEARING[slot])
    return CinemaSpeakerSpec(
        slot,
        (ANCHOR[0] + sin(angle) * radius, ANCHOR[1] + cos(angle) * radius,
         ANCHOR[2]),
        delay_ms=delay_ms,
    )


class Transport:
    """The parts of a jukebox transport that decide when a room plays.

    A frame every tick, one buffer consumed per frame by every speaker that is
    playing, and the room started (and re-started) whenever it is not fully
    playing and its shallowest queue holds the room's pre-buffer -- which is
    what `JukeboxRelayReceiver` and `AudioStreamer` both do.
    """

    def __init__(self, delays, samples=960):
        specs = [placed("front_l", delays.get("front_l", 0.0)),
                 placed("front_r", delays.get("front_r", 0.0))]
        # The channel verdict is not what this file is about: the signal is a
        # position marker, and a mono verdict would feed every speaker a mix of
        # it and hide the timing being measured (see test_cinema_live for the
        # verdict itself).
        self.renderer = CinemaRenderer(ANCHOR, "front_only", None, specs=specs,
                                       detect_channels=False)
        self.bank = CinemaSpeakerBank(FakeGame(), self.renderer)
        self.sources = self.bank.slot_sources
        self.samples = samples
        self.index = 0
        self.played = {"front_l": [], "front_r": []}
        self.depths = {"front_l": [], "front_r": []}

    def tick(self):
        """One frame of the song: consume, then queue the new frame."""
        index = self.index
        self.index += 1
        for slot, source in self.sources.items():
            tag = None
            if source.state == cyal.SourceState.PLAYING and source.buffers_queued:
                source.buffers_processed += 1
                before = len(source.played)
                self.bank.reclaim()
                if len(source.played) > before:
                    tag = source.played[-1]
            self.played[slot].append(tag)
            self.depths[slot].append(source.buffers_queued)
        self.bank.queue_frame(*ramp_frame(index, self.samples))
        if not self.bank.playing():
            self.bank.start_playback()

    def run(self, ticks=1200):
        for _ in range(ticks):
            self.tick()
        return self

    def lags(self, first=60, last=None):
        """Programme position an untrimmed speaker leads the other by, per tick."""
        out = []
        for index in range(first, len(self.played["front_l"]) if last is None else last):
            left = self.played["front_l"][index]
            right = self.played["front_r"][index]
            if left is None or right is None:
                continue
            out.append((left - right) % TAPE)
        return out


class DelayTrimIsAudible(unittest.TestCase):
    """The trim, in samples, from the very first bar -- and it stays there."""

    def assert_lag(self, delay_ms, expected, ticks=1200):
        transport = Transport({"front_r": delay_ms}).run(ticks)
        lags = transport.lags()
        self.assertTrue(lags, "the room never played")
        wrong = [value for value in lags if value != expected]
        self.assertEqual(
            wrong, [],
            f"a {delay_ms:g}ms trim must hold {expected} samples from the first "
            f"bar; heard {sorted(set(lags))} instead")
        return transport

    def test_a_trim_of_three_frames_is_heard_from_the_first_bar(self):
        # 60ms at 20ms frames: the whole trim travels as three frames of hold.
        transport = self.assert_lag(60.0, 3 * 960)
        self.assertGreater(min(transport.depths["front_r"][80:]),
                           min(transport.depths["front_l"][80:]),
                           "the trimmed speaker has to sit deeper in its queue")

    def test_a_trim_of_a_frame_and_a_half_is_sample_exact(self):
        # 30ms = 1440 samples: one whole frame of hold plus a 480 sample cut.
        self.assert_lag(30.0, 1440)

    def test_a_sub_frame_trim_is_not_rounded_away(self):
        # 5ms is less than one frame; it is carried entirely by the cut.
        self.assert_lag(5.0, 240)

    def test_the_deepest_trim_the_map_can_carry(self):
        self.assert_lag(100.0, 5 * 960)

    def test_the_trim_does_not_drift_over_a_long_song(self):
        transport = self.assert_lag(60.0, 2880, ticks=3000)
        tail = transport.lags(first=2000)
        self.assertEqual(set(tail), {2880}, "the trim drifted late in the song")

    def test_an_untouched_room_plays_every_speaker_identically(self):
        transport = Transport({}).run(600)
        self.assertEqual(set(transport.lags()), {0})
        self.assertEqual(min(transport.depths["front_l"][100:]),
                         min(transport.depths["front_r"][100:]))

    def test_only_the_speaker_that_was_trimmed_moves(self):
        transport = Transport({"front_l": 60.0, "front_r": 0.0}).run(600)
        lags = transport.lags()
        self.assertEqual(set(lags), {TAPE - 2880},
                         "trimming the other speaker must mirror the image")


class TheRoomStillStarts(unittest.TestCase):
    """A trimmed speaker must be fed while it waits, or it never starts."""

    def test_a_trimmed_speaker_joins_after_its_own_hold_and_no_later(self):
        transport = Transport({"front_r": 60.0})
        started = None
        for index in range(60):
            transport.tick()
            if started is None and transport.sources["front_r"].state == cyal.SourceState.PLAYING:
                started = index
        self.assertIsNotNone(started, "the trimmed speaker never started")
        self.assertLessEqual(started, 12,
                             "building a 60ms hold must not stall the room")

    def test_the_room_plays_at_all_with_a_trim_on_every_speaker(self):
        transport = Transport({"front_l": 40.0, "front_r": 60.0}).run(300)
        lags = transport.lags()
        self.assertTrue(lags, "a fully trimmed room went silent")
        # 20ms apart, and the *only* thing that may separate them.
        self.assertEqual(set(lags), {960})

    def test_a_room_where_every_speaker_shares_a_trim_is_heard_level(self):
        """A trim is relative, so two speakers dialled in alike play alike.

        A hold measured from the moment playback began made the speaker that
        started the room skip its own trim while the other took all of it: two
        speakers both set to 60 ms came out 60 ms apart, which is the trim an
        installer set read back as the wrong number.
        """
        transport = Transport({"front_l": 60.0, "front_r": 60.0}).run(600)
        self.assertEqual(set(transport.lags()), {0})

    def test_a_playing_speaker_is_never_topped_up_with_old_audio(self):
        """A refill must never hand a playing speaker a bar it already played.

        ``realign`` runs whenever the room is (re)started, and a speaker that
        started before another is a frame "shallower" in the meantime -- which
        used to be read as a speaker that had run dry and was filled from the
        room's own history, so its next buffer was audio from before the one
        it had just played. Heard as the song stumbling over one bar, and it
        threw every measured trim off by that replay.
        """
        # A room whose speakers carry different trims: the shallowest one
        # starts first and is therefore a frame or two "ahead" of a deeper
        # one still inside its hold while the room is starting.
        transport = Transport({"front_l": 30.0, "front_r": 60.0})
        for _ in range(200):
            transport.tick()
            transport.bank.realign(play=False)
        for slot in ("front_l", "front_r"):
            seen = [tag for tag in transport.played[slot] if tag is not None]
            self.assertGreater(len(seen), 50, slot)
            jumps = {(later - before) % TAPE
                     for before, later in zip(seen, seen[1:])}
            self.assertEqual(jumps, {960}, slot +
                             " replayed audio it had already played")

    def test_the_shallowest_trim_is_the_room_s_own_reference(self):
        # 30 ms against 60 ms: the *difference* is what is heard, exactly.
        transport = Transport({"front_l": 30.0, "front_r": 60.0}).run(600)
        self.assertEqual(set(transport.lags()), {1440})
        # And a room whose shallowest trim is deep is still relative: both
        # speakers late by the same amount is not a delay anyone hears.
        deep = Transport({"front_l": 80.0, "front_r": 80.0}).run(600)
        self.assertEqual(set(deep.lags()), {0})

    def test_a_realign_does_not_push_the_untrimmed_speakers_back(self):
        transport = Transport({"front_r": 60.0}).run(400)
        before = transport.lags(first=200, last=400)
        transport.bank.realign()
        transport.run(500)
        after = transport.lags(first=450, last=900)
        self.assertEqual(set(before), {2880})
        self.assertEqual(set(after), {2880},
                         "realigning re-anchored the room on the trimmed speaker")


if __name__ == "__main__":
    unittest.main()
