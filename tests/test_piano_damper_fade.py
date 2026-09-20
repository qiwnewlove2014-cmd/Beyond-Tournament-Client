"""A released piano note fades on the audio thread, never on one of its own.

What this file exists to prevent: the damper fade used to start one
``threading.Thread`` per *sound*, and one note is one sound at the instrument,
one per PA speaker and one per cinema-room speaker. In a six-speaker room that
was seven threads and seven OpenAL writes (``source.gain``, ``source.stop()``)
per key release, all outside the frame batch -- and the AudioManager's own
audio inbox exists because concurrent cross-thread OpenAL use is the thing that
corrupts native memory and crashes the game (see its comment).

The drums have always faded the right way: a queue advanced once per audio pass
(``DrumAudio._schedule_fade`` / ``_finish_fades``). The piano now uses the same
queue, and the tests below hold it to three things:

* a key release makes **no OpenAL call at all** and starts **no thread**;
* every gain step and the ``stop`` happen inside an ``update`` pass, which is
  the one place OpenAL belongs;
* the whole note -- not only the instrument's own copy -- is faded and stopped
  together, whatever the room did to its size.
"""

import contextlib
import os
import sys
import threading
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from libs.piano import PianoAudio


class OpenAlPass:
    """The one window OpenAL may be touched in: an ``update`` pass."""

    def __init__(self):
        self.inside = False
        self.calls = []      # every call, in order
        self.outside = []    # the ones made outside the pass

    def note(self, kind, value=None):
        entry = (kind, value)
        self.calls.append(entry)
        if not self.inside:
            self.outside.append(entry)

    def gains(self):
        return [value for kind, value in self.calls if kind == "gain"]

    def stops(self):
        return [value for kind, value in self.calls if kind == "stop"]


@contextlib.contextmanager
def audio_pass(log):
    log.inside = True
    try:
        yield
    finally:
        log.inside = False


class GuardedSource:
    """A source that records every touch and when it happened."""

    def __init__(self, log, gain=1.0):
        self._log = log
        self._gain = float(gain)
        self.stopped = False

    @property
    def gain(self):
        self._log.note("read", self._gain)
        return self._gain

    def peek(self):
        """Read the value without asking the source -- the test's own look."""
        return self._gain

    @gain.setter
    def gain(self, value):
        self._log.note("gain", value)
        self._gain = value

    def stop(self):
        self._log.note("stop", self._gain)
        self.stopped = True


class GuardedSound:
    def __init__(self, log, gain=1.0):
        self.source = GuardedSource(log, gain)


class DeadSound:
    """A sound the manager's own cleanup has already taken apart."""

    source = None


class GrumpySource:
    """A source that refuses every question (it answers with an exception)."""

    def __getattr__(self, name):
        raise RuntimeError("this source is gone")

    def __setattr__(self, name, value):
        raise RuntimeError("this source is gone")


class Clock:
    def __init__(self, start=1000.0):
        self.now = float(start)

    def monotonic(self):
        return self.now

    def perf_counter(self):
        return self.now

    def sleep(self, seconds):
        self.now += float(seconds)

    def advance(self, seconds):
        self.now += float(seconds)


class FakeAudioManager:
    """Only what PianoAudio touches when a note is released and a map changes."""

    def __init__(self):
        self.unbound_sources = []
        self._preloaded_buffers = {}
        self.sends = ()


def make_piano(clock):
    """A real ``PianoAudio`` whose clock this test owns (no device is opened)."""
    piano = PianoAudio(FakeAudioManager())
    fake_time = SimpleNamespace(monotonic=clock.monotonic,
                                perf_counter=clock.perf_counter,
                                sleep=clock.sleep)
    patcher = mock.patch("libs.piano.time", fake_time)
    patcher.start()
    return piano, patcher


class CountingThread(threading.Thread):
    """Every thread the code under test starts, counted (and never run)."""

    started = 0

    def start(self):                     # pragma: no cover - must stay at zero
        type(self).started += 1
        raise AssertionError("a released note must not start a thread")


class AKeyReleaseTouchesNoOpenAlTests(unittest.TestCase):
    """The note-off path itself: no OpenAL call, and no thread of its own."""

    def setUp(self):
        self.clock = Clock()

    def tearDown(self):
        mock.patch.stopall()

    def a_note(self, piano, log, room_speakers=6):
        """One note as the room made it: instrument, PA copy, one per speaker."""
        sounds = [GuardedSound(log, gain=0.8)]                    # the instrument
        sounds.append(GuardedSound(log, gain=0.8))                # a PA speaker
        sounds.extend(GuardedSound(log, gain=0.8)                 # the room
                      for _ in range(room_speakers))
        piano.active_piano_notes["local-C4"] = sounds[0]
        piano.active_piano_notes["mega-local-C4"] = sounds[1]
        piano.active_piano_notes["cin-local-C4"] = sounds[2:]
        return sounds

    def test_a_release_makes_no_openal_call_and_starts_no_thread(self):
        piano, _patcher = make_piano(self.clock)
        log = OpenAlPass()
        sounds = self.a_note(piano, log)
        CountingThread.started = 0
        with mock.patch.object(threading, "Thread", CountingThread):
            piano.stop_note("local", "C4")
        # The old code started one thread per sound here; the new one starts none.
        self.assertEqual(CountingThread.started, 0)
        # Nothing was read, nothing was written: OpenAL was not touched at all.
        self.assertEqual(log.calls, [])
        # Every sound of the note is on the queue, and none of them is lost.
        self.assertEqual(len(piano._fades), len(sounds))
        self.assertEqual([fade["sound"] for fade in piano._fades], sounds)

    def test_the_start_gain_is_read_on_the_pass_not_at_the_release(self):
        """A release must not read ``source.gain`` either (that is an AL call)."""
        piano, _patcher = make_piano(self.clock)
        log = OpenAlPass()
        self.a_note(piano, log, room_speakers=0)
        piano.stop_note("local", "C4")
        self.assertEqual(log.calls, [])
        self.assertTrue(all(fade["gain"] is None for fade in piano._fades))
        with audio_pass(log):
            piano.update()
        self.assertEqual(log.gains(), [0.8, 0.8])   # read once, then stepped

    def test_a_sound_whose_source_is_gone_is_dropped_rather_than_queued(self):
        piano, _patcher = make_piano(self.clock)
        log = OpenAlPass()
        live = GuardedSound(log)
        grumpy = SimpleNamespace(source=GrumpySource())
        piano.active_piano_notes["local-C4"] = [DeadSound(), live, grumpy]
        piano.stop_note("local", "C4")
        # A sound with no source at all is not even queued; one that refuses to
        # answer is queued and then dropped on the pass, without raising.
        self.assertEqual([fade["sound"] for fade in piano._fades], [live, grumpy])
        self.clock.advance(0.02)
        with audio_pass(log):
            piano.update()
        self.assertEqual([fade["sound"] for fade in piano._fades], [live])
        self.clock.advance(0.25)
        with audio_pass(log):
            piano.update()
        self.assertTrue(live.source.stopped)
        self.assertEqual(piano._fades, [])
        self.assertEqual(log.outside, [])


class TheFadeRunsOnTheAudioPassTests(unittest.TestCase):
    """Where the calls happen, and what they do to a note that is 7 sounds."""

    def setUp(self):
        self.clock = Clock()

    def tearDown(self):
        mock.patch.stopall()

    def make_note(self, room_speakers=6):
        piano, _patcher = make_piano(self.clock)
        log = OpenAlPass()
        self.sounds = [GuardedSound(log, gain=0.9)]
        self.sounds.extend(GuardedSound(log, gain=0.9)
                           for _ in range(room_speakers))
        piano.active_piano_notes["local-C4"] = self.sounds[0]
        piano.active_piano_notes["cin-local-C4"] = self.sounds[1:]
        self.log = log
        return piano

    def pump(self, piano, passes=1, step=0.02):
        """Run ``passes`` audio passes, each one after its share of time."""
        for _ in range(passes):
            self.clock.advance(step)
            with audio_pass(self.log):
                piano.update()

    def test_the_whole_note_fades_and_is_stopped_on_the_pass(self):
        piano = self.make_note()
        piano.stop_note("local", "C4")
        self.pump(piano, passes=4)                      # ~80 ms in
        self.assertTrue(self.log.gains(), "the pass must step the gain")
        first = self.sounds[0].source.peek()
        self.assertLess(first, 0.9, "a fourth of the fade has been walked down")
        self.assertEqual(len(self.log.gains()), 4 * len(self.sounds))
        self.assertEqual(self.log.stops(), [])          # nothing stopped early
        self.assertEqual(piano._fades and len(piano._fades), len(self.sounds))
        self.pump(piano, passes=8)                      # past 180 ms
        for sound in self.sounds:
            self.assertTrue(sound.source.stopped, "every copy must stop")
        self.assertEqual(piano._fades, [])
        self.assertEqual(self.log.outside, [])

    def test_the_ramp_never_goes_up_and_ends_at_silence(self):
        piano = self.make_note(room_speakers=1)
        piano.stop_note("local", "C4")
        start = self.sounds[0].source.peek()
        self.pump(piano, passes=1)
        one_step = self.sounds[0].source.peek()
        self.pump(piano, passes=1)
        two_steps = self.sounds[0].source.peek()
        self.assertLess(one_step, start)
        self.assertLess(two_steps, one_step)
        self.pump(piano, passes=12)
        self.assertEqual(self.sounds[0].source.peek(), 0.0)
        self.assertTrue(self.sounds[0].source.stopped)
        self.assertEqual(self.log.outside, [])

    def test_a_note_that_is_stopped_twice_is_still_faded_once(self):
        """``stop_note`` pops its keys, so the second call has nothing to fade."""
        piano = self.make_note(room_speakers=3)
        piano.stop_note("local", "C4")
        queued = len(piano._fades)
        piano.stop_note("local", "C4")
        self.assertEqual(len(piano._fades), queued)
        self.pump(piano, passes=12)
        self.assertTrue(all(sound.source.stopped for sound in self.sounds))

    def test_a_map_change_leaves_no_fade_behind(self):
        piano = self.make_note(room_speakers=2)
        piano.stop_note("local", "C4")
        self.assertTrue(piano._fades)
        piano.reset()
        self.assertEqual(piano._fades, [])

    def test_nothing_runs_between_passes(self):
        """Once queued, a fade waits for the pass: no work happens on its own."""
        piano = self.make_note(room_speakers=4)
        piano.stop_note("local", "C4")
        for _ in range(20):                             # a long, quiet second
            self.clock.advance(0.05)
        self.assertEqual(self.log.calls, [])
        self.assertEqual(self.log.outside, [])
        self.assertFalse(any(sound.source.stopped for sound in self.sounds))


if __name__ == "__main__":
    unittest.main()
