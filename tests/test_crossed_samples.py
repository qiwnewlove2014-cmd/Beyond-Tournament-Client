"""Crossed note copies: the room's filter on a sample, made once and kept.

The filter itself is the real one (``libs/audio/cinema/crossover.py``) -- these
tests are about what it is *given* and how the copy is made, so the PCM, the
decode and the upload are fakes and no device, file or network is touched.
"""

from array import array
import math
import struct
import threading
import time
from types import SimpleNamespace
import unittest

from libs.audio.cinema import crossover
from libs.crossed_samples import CrossedSampleCache, crossed_pcm


def samples(values):
    return struct.pack("<" + "h" * len(values), *values)


def tone(frequency, seconds=0.2, rate=44100, amplitude=20000):
    """One sine as an s16 sample array (a list, so two can be zipped)."""
    count = int(rate * seconds)
    step = 2.0 * math.pi * frequency / rate
    return [int(amplitude * math.sin(step * index)) for index in range(count)]


def stereo(left, right):
    """Two mono sample arrays as one interleaved s16le buffer."""
    mixed = array("h", bytes(4 * len(left)))
    mixed[0::2] = array("h", left)
    mixed[1::2] = array("h", right)
    return mixed.tobytes()


def rms(pcm):
    values = array("h")
    values.frombytes(pcm)
    if not values:
        return 0.0
    return math.sqrt(sum(float(value) ** 2 for value in values) / len(values))


def ratio(pcm, original):
    before = rms(original)
    return rms(pcm) / before if before else 0.0


def decoded(pcm_bytes, channels=2, frequency=44100):
    return SimpleNamespace(buffer=pcm_bytes, channels=channels,
                           frequency=frequency)


class FakeBuffer:
    def __init__(self, data, channels, rate):
        self.data, self.channels, self.rate = data, channels, rate


class CrossedPcmTests(unittest.TestCase):
    """The maths: the mark picks the side, at the sample's own rate."""

    # One tone well inside a filter's passband and one well outside it, so a
    # filter's *side* is readable rather than its corner (a two-section
    # cascade is already 6 dB down at its corner).
    SUB = 40.0
    MID = 300.0
    MIDS = 2000.0
    TOP = 12000.0

    def test_a_bass_cabinet_keeps_the_bottom_and_loses_the_top(self):
        kept, channels = crossed_pcm(stereo(tone(self.SUB), tone(self.MIDS)),
                                     2, 44100, 120)
        self.assertEqual(channels, 2)
        values = array("h")
        values.frombytes(kept)
        # The channels are filtered independently: one holds a 60 Hz note and
        # keeps it, the other holds 2 kHz and loses it.
        self.assertGreater(ratio(values[0::2].tobytes(), samples(tone(self.SUB))),
                           0.8)
        self.assertLess(ratio(values[1::2].tobytes(), samples(tone(self.MIDS))),
                        0.05)

    def test_a_tweeter_keeps_the_top_and_loses_the_bottom(self):
        kept, channels = crossed_pcm(stereo(tone(self.TOP), tone(self.MID)),
                                     2, 44100, -3000)
        self.assertEqual(channels, 2)
        values = array("h")
        values.frombytes(kept)
        top = ratio(values[0::2].tobytes(), samples(tone(self.TOP)))
        bottom = ratio(values[1::2].tobytes(), samples(tone(self.MID)))
        # Kept versus lost, not bit-identical: two cascaded one-pole sections
        # approach unity from below at the top (they are the room's own, and a
        # sample is 44.1 kHz while the room's frames are 48), so a 12 kHz tone
        # through a 3 kHz tweeter comes back a few dB down -- and a 300 Hz tone
        # is gone.
        self.assertGreater(top, 0.5)
        self.assertLess(bottom, 0.05)
        self.assertGreater(top, bottom * 10)

    def test_the_filter_is_the_rooms_own_reader(self):
        """Not a second filter: the very sections the song is fed through."""
        left = array("h", tone(self.TOP))
        right = array("h", tone(self.MID))
        expected = crossover.apply((left.tobytes(), right.tobytes()), None,
                                   -3000, 44100)[0][0]
        kept, channels = crossed_pcm(stereo(tone(self.TOP), tone(self.MID)),
                                     2, 44100, -3000, "l")
        self.assertEqual(channels, 1)
        self.assertEqual(kept, expected)

    def test_the_corner_lands_at_the_samples_own_rate(self):
        """A sample is 44.1 kHz and the room's frames are 48: both must land."""
        sample = stereo(tone(self.SUB), tone(self.MIDS))
        kept, _channels = crossed_pcm(sample, 2, 44100, 120, "l")
        room_rate, _channels = crossed_pcm(sample, 2, 48000, 120, "l")
        self.assertNotEqual(kept, room_rate)

    def test_a_half_is_one_channel_and_the_whole_is_stereo(self):
        sample = stereo(tone(self.SUB), tone(self.MIDS))
        half, channels = crossed_pcm(sample, 2, 44100, 120, "r")
        self.assertEqual(channels, 1)
        self.assertEqual(len(half), len(sample) // 2)
        # The half is the one it was asked for (the 2 kHz channel dies).
        self.assertLess(ratio(half, samples(tone(self.MIDS))), 0.05)
        whole, channels = crossed_pcm(sample, 2, 44100, 120, None)
        self.assertEqual(channels, 2)
        self.assertEqual(len(whole), len(sample))

    def test_a_mono_sample_has_no_halves(self):
        mono = samples(tone(self.SUB))
        for half in (None, "l", "r", "nonsense"):
            kept, channels = crossed_pcm(mono, 1, 44100, 120, half)
            self.assertEqual(channels, 1, half)
            self.assertEqual(len(kept), len(mono), half)
            self.assertGreater(ratio(kept, mono), 0.8, half)

    def test_a_tweeter_is_clamped_rather_than_raising(self):
        """A high-pass is not a convex blend: a step can overshoot s16."""
        square = [32767 if index % 8 else -32768 for index in range(2000)]
        kept, channels = crossed_pcm(samples(square), 1, 44100, -800)
        self.assertEqual(channels, 1)
        values = array("h")
        values.frombytes(kept)
        self.assertEqual(len(values), len(square))
        self.assertLessEqual(max(values), 32767)
        self.assertGreaterEqual(min(values), -32768)

    def test_a_full_range_speaker_has_no_crossed_copy(self):
        sample = stereo(tone(self.SUB), tone(self.MIDS))
        for mark in (0, None, "", "not a number", 0.0):
            with self.assertRaises(ValueError, msg=mark):
                crossed_pcm(sample, 2, 44100, mark)

    def test_pcm_this_cannot_read_is_refused_rather_than_guessed(self):
        for bad, channels in ((b"\x00", 2), (b"", 2), (b"\x00" * 6, 4)):
            with self.assertRaises(ValueError, msg=(bad, channels)):
                crossed_pcm(bad, channels, 44100, 120)


class CrossedSampleCacheTests(unittest.TestCase):
    """The copy is made off the frame path, once, and never twice."""

    def setUp(self):
        self.owner = threading.get_ident()
        self.uploads = []
        self.decodes = []
        self.caches = []
        self.block = None
        self.decoded = stereo(tone(CrossedPcmTests.SUB),
                              tone(CrossedPcmTests.MIDS))

    def tearDown(self):
        if self.block is not None:
            self.block.set()
        for cache in self.caches:
            cache.close()
        for cache in self.caches:
            if cache._worker is not None:
                cache._worker.join(timeout=2)
                self.assertFalse(cache._worker.is_alive())

    def cache(self, decode=None, upload=None, resolve=None, **kwargs):
        def default_decode(path):
            self.decodes.append((path, threading.get_ident()))
            if self.block is not None:
                self.block.wait(timeout=2)
            return decoded(self.decoded)

        def default_upload(data, channels, rate):
            self.assertEqual(threading.get_ident(), self.owner)
            self.uploads.append((data, channels, rate))
            return FakeBuffer(data, channels, rate)

        result = CrossedSampleCache(resolve or (lambda path: path.lower()),
                                    decode or default_decode,
                                    upload or default_upload, **kwargs)
        self.caches.append(result)
        return result

    def wait(self, condition, timeout=2):
        deadline = time.monotonic() + timeout
        while not condition():
            if time.monotonic() >= deadline:
                self.fail("worker condition did not complete")
            threading.Event().wait(0.001)

    def pump_until(self, cache, condition):
        """Pump the owner's side until ``condition`` holds (or fail)."""
        def done():
            cache.pump(budget_seconds=0.5)
            return condition()
        self.wait(done)

    def ready(self, cache, path="note", mark=120, channel=None):
        self.pump_until(cache, lambda: cache.get(path, mark, channel) is not None)
        return cache.get(path, mark, channel)

    def test_a_copy_is_made_lazily_and_kept(self):
        cache = self.cache()
        self.assertIsNone(cache._worker)
        self.assertIsNone(cache.get("NOTE", 120))
        worker = cache._worker
        buffer = self.ready(cache, "note")
        self.assertIs(cache._worker, worker)
        self.assertEqual([path for path, _ in self.decodes], ["note"])
        self.assertNotEqual(self.decodes[0][1], self.owner)
        self.assertEqual(len(self.uploads), 1)
        self.assertEqual(buffer.channels, 2)
        self.assertEqual(buffer.rate, 44100)
        # The very bytes the room's own filter makes for that sample, at the
        # rate the *sample* is recorded at (the room's frames are 48 kHz).
        expected, channels = crossed_pcm(self.decoded, 2, 44100, 120)
        self.assertEqual(buffer.data, expected)
        self.assertEqual(buffer.channels, channels)
        # The cached copy: no second decode, no second upload.
        self.assertIs(cache.get("note", 120), buffer)
        self.assertEqual(len(self.decodes), 1)

    def test_a_mark_that_is_not_ready_is_asked_for_once(self):
        cache = self.cache()
        for _ in range(5):
            self.assertIsNone(cache.get("note", 120, "l"))
        self.assertEqual(len(cache._pending), 1)
        self.ready(cache, "note", 120, "l")
        self.assertEqual([path for path, _ in self.decodes], ["note"])

    def test_each_mark_and_each_half_is_its_own_copy(self):
        cache = self.cache()
        self.ready(cache, "note", 120, None)
        left = self.ready(cache, "note", 120, "l")
        right = self.ready(cache, "note", 120, "r")
        treble = self.ready(cache, "note", -3000, None)
        self.assertEqual(len(self.decodes), 4)
        self.assertEqual((left.channels, right.channels, treble.channels), (1, 1, 2))
        self.assertNotEqual(left.data, right.data)
        self.assertNotEqual(treble.data, self.ready(cache, "note", 120).data)
        self.assertEqual(len(self.uploads), 4)

    def test_a_full_range_speaker_never_asks_for_anything(self):
        cache = self.cache()
        for mark in (0, None, "", 0.0):
            self.assertIsNone(cache.get("note", mark))
            self.assertFalse(cache.request("note", mark))
        self.assertEqual(self.decodes, [])
        self.assertIsNone(cache._worker)

    def test_a_sample_that_cannot_be_crossed_is_remembered_as_one(self):
        def explode(path):
            self.decodes.append(path)
            raise ValueError("no such sample")

        cache = self.cache(decode=explode)
        self.assertIsNone(cache.get("note", 120))
        self.pump_until(cache, lambda: cache.stats()["failures"] == 1)
        for _ in range(3):
            self.assertIsNone(cache.get("note", 120))
        self.assertEqual(len(self.decodes), 1)
        self.assertEqual(cache.stats()["pending"], 0)

    def test_a_failed_upload_is_remembered_too(self):
        cache = self.cache(upload=lambda *args: None)
        self.assertIsNone(cache.get("note", 120))
        self.pump_until(cache, lambda: cache.stats()["failures"] == 1)
        self.assertEqual(cache.stats()["entries"], 0)
        self.assertIsNone(cache.get("note", 120))

    def test_an_unusable_pcm_format_is_a_failure_not_a_crash(self):
        cache = self.cache(decode=lambda path: decoded(b"\x00", channels=3))
        self.assertIsNone(cache.get("note", 120))
        self.pump_until(cache, lambda: cache.stats()["failures"] == 1)

    def test_the_oldest_copy_is_given_up_first(self):
        cache = self.cache(max_entries=2, max_bytes=100 * 1024 * 1024)
        first = self.ready(cache, "one", 120)
        self.ready(cache, "two", 120)
        self.ready(cache, "three", 120)
        self.assertEqual(cache.stats()["entries"], 2)
        self.assertIsNone(cache.get("one", 120))
        self.assertIsNotNone(cache.get("three", 120))
        # ...and it is made again, because it was the least recently used.
        self.ready(cache, "one", 120)
        self.assertEqual(cache.stats()["entries"], 2)
        self.assertNotEqual(self.ready(cache, "one", 120), first)

    def test_the_byte_budget_bounds_the_kept_copies(self):
        cache = self.cache(max_bytes=len(self.decoded) + 10)
        self.ready(cache, "one", 120)
        self.ready(cache, "two", 120)
        self.assertEqual(cache.stats()["entries"], 1)
        self.assertLessEqual(cache.stats()["bytes"], cache._max_bytes)

    def test_a_burst_of_notes_is_bounded(self):
        cache = self.cache(max_entries=2)
        for index in range(10):
            cache.get(f"note{index}", 120)
        self.assertLessEqual(len(cache._pending), 2)

    def test_closing_cancels_what_is_being_made(self):
        self.block = threading.Event()
        cache = self.cache()
        self.assertIsNone(cache.get("note", 120))
        self.assertTrue(cache.request("other", 120))
        cache.close()
        self.assertIsNone(cache.get("note", 120))
        self.assertFalse(cache.request("other", 120))
        self.assertEqual(cache.stats()["entries"], 0)
        self.assertTrue(cache.stats()["closed"])

    def test_clearing_a_map_drops_its_copies(self):
        cache = self.cache()
        self.ready(cache, "note", 120)
        self.assertEqual(cache.stats()["entries"], 1)
        cache.clear()
        self.assertEqual(cache.stats()["entries"], 0)
        self.assertEqual(cache.stats()["failures"], 0)
        self.assertEqual(cache.stats()["bytes"], 0)
        self.assertIsNone(cache.get("note", 120))
        # A copy made for the new map is a fresh one, not the cancelled job.
        self.ready(cache, "note", 120)
        self.assertEqual(cache.stats()["entries"], 1)

    def test_an_unresolvable_path_is_never_queued(self):
        cache = self.cache(resolve=lambda path: None)
        self.assertIsNone(cache.get("note", 120))
        self.assertIsNone(cache._worker)

    def test_the_default_bound_holds_a_keyboards_working_set(self):
        """One instrument in one room must fit, or the cache is a treadmill.

        A least-recently-used cache smaller than the working set is not a
        graceful degradation: every copy is given up before its sample comes
        round again, so the same notes are filtered over and over for the whole
        performance -- and the worker never idles, competing with the game's
        own thread for the interpreter, which a listener feels as the game
        stuttering while a band plays. A piano is 85 notes (about 474 KB each
        decoded), and a room feeds one copy per mark plus the screen wall's
        halves, so the defaults have to hold a few hundred.
        """
        from libs.crossed_samples import DEFAULT_MAX_BYTES, DEFAULT_MAX_ENTRIES
        piano_notes, marks = 85, 2
        working_set = piano_notes * marks * 2      # a copy per half per mark
        self.assertGreaterEqual(DEFAULT_MAX_ENTRIES, working_set)
        self.assertGreaterEqual(DEFAULT_MAX_BYTES,
                                working_set * 474 * 1024 // 2)
        cache = self.cache()
        for index in range(working_set):
            cache._store((f"note{index}", 120, None), object(), 1024)
        self.assertEqual(cache.stats()["entries"], working_set)

    def test_limits_must_be_positive(self):
        for kwargs in ({"max_entries": 0}, {"max_bytes": -1},
                       {"max_sample_bytes": 0}, {"max_failures": 0}):
            with self.assertRaises(ValueError, msg=kwargs):
                CrossedSampleCache(lambda path: path, lambda path: None,
                                   lambda *args: None, **kwargs)


if __name__ == "__main__":
    unittest.main()
