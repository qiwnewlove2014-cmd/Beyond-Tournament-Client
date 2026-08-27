"""Instrument cache tests: fake PCM/upload, no devices, files or network."""

from collections import Counter
import struct
import threading
import time
from types import SimpleNamespace
import unittest

from libs.instrument_samples import InstrumentSampleCache


def pcm(values=(100, -99, -3, 0, 32767, 32767, -32768, -32768), channels=2):
    return SimpleNamespace(buffer=struct.pack("<" + "h" * len(values), *values),
                           channels=channels, frequency=48000)


class FakeBuffer:
    def __init__(self, data, channels, rate):
        self.data, self.channels, self.rate = data, channels, rate


class InstrumentSampleCacheTests(unittest.TestCase):
    def setUp(self):
        self.owner = threading.get_ident()
        self.uploads = []
        self.decodes = []
        self.caches = []
        self.releases = []

    def tearDown(self):
        for event in self.releases:
            event.set()
        for cache in self.caches:
            cache.close()
        for cache in self.caches:
            if cache._worker is not None:
                cache._worker.join(timeout=2)
                self.assertFalse(cache._worker.is_alive())

    def cache(self, decode=None, upload=None, resolve=None, **kwargs):
        def default_decode(path):
            self.decodes.append((path, threading.get_ident()))
            return pcm()

        def default_upload(data, channels, rate):
            self.assertEqual(threading.get_ident(), self.owner)
            self.uploads.append((data, channels, rate))
            return FakeBuffer(data, channels, rate)

        result = InstrumentSampleCache(resolve or (lambda path: path.lower()),
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

    def ready(self, cache, paths):
        def done():
            cache.pump(budget_seconds=0.1)
            return cache.status(paths) == "ready"
        self.wait(done)

    def test_lazy_worker_and_deduplicated_representations(self):
        cache = self.cache()
        self.assertIsNone(cache._worker)
        self.assertIsNone(cache.get("NOTE", "split"))
        worker = cache._worker
        self.assertIsNone(cache.get("note", "stereo"))
        self.assertIsNone(cache.get("note", "mono"))
        self.assertEqual(cache.request(["NOTE", "note"]), 0)
        self.ready(cache, ["note"])
        self.assertIs(cache._worker, worker)
        self.assertEqual([path for path, _ in self.decodes], ["note"])
        self.assertNotEqual(self.decodes[0][1], self.owner)
        self.assertEqual(len(self.uploads), 4)
        original = cache.get("note")
        mono = cache.get("note", "mono")
        left, right = cache.get("note", "split")
        self.assertEqual(original.data, pcm().buffer)
        self.assertEqual(struct.unpack("<4h", mono.data), (0, -2, 32767, -32768))
        self.assertEqual(struct.unpack("<4h", left.data), (100, -3, 32767, -32768))
        self.assertEqual(struct.unpack("<4h", right.data), (-99, 0, 32767, -32768))
        self.assertEqual(original.channels, 2)
        self.assertEqual(mono.channels, 1)

    def test_mono_file_reuses_one_upload_for_all_representations(self):
        source = pcm((3, -7, 111), channels=1)
        cache = self.cache(decode=lambda path: source)
        cache.request("mono")
        self.ready(cache, ["mono"])
        buffer = cache.get("mono")
        self.assertIs(cache.get("mono", "mono"), buffer)
        self.assertEqual(cache.get("mono", "split"), (buffer, buffer))
        self.assertEqual(len(self.uploads), 1)
        self.assertEqual(cache.stats()["cache_bytes"], len(source.buffer))

    def test_native_conversion_preserves_chunk_boundaries(self):
        frame = struct.pack("<hh", -32768, 32767)
        source = SimpleNamespace(buffer=frame * 20000, channels=2, frequency=44100)
        cache = self.cache(decode=lambda path: source)
        cache.request("long")
        self.ready(cache, ["long"])
        self.assertEqual(cache.get("long", "mono").data, struct.pack("<h", -1) * 20000)
        left, right = cache.get("long", "split")
        self.assertEqual(left.data, struct.pack("<h", -32768) * 20000)
        self.assertEqual(right.data, struct.pack("<h", 32767) * 20000)
        self.assertEqual(left.rate, 44100)

    def test_cold_calls_return_while_decoder_is_blocked(self):
        entered, release = threading.Event(), threading.Event()
        self.releases.append(release)

        def decode(path):
            entered.set()
            release.wait(2)
            return pcm()

        cache = self.cache(decode=decode)
        self.assertIsNone(cache.get("note"))
        self.assertTrue(entered.wait(1))
        self.assertFalse(release.is_set())
        self.assertEqual(cache.status(["note"]), "loading")
        self.assertEqual(cache.request(["note"]), 0)
        self.assertEqual(cache.pump(), 0)
        self.assertEqual(self.uploads, [])
        release.set()
        self.ready(cache, ["note"])

    def test_per_pump_cap_and_no_partial_ready_buffers(self):
        cache = self.cache()
        cache.request("note")
        self.wait(lambda: cache.stats()["completed"] == 1)
        for index in range(3):
            self.assertEqual(cache.pump(max_uploads=1, budget_seconds=1), 1)
            self.assertEqual(len(self.uploads), index + 1)
            self.assertIsNone(cache.get("note", "split"))
            self.assertEqual(cache.status(["note"]), "loading")
        self.assertEqual(cache.pump(max_uploads=1, budget_seconds=1), 1)
        self.assertEqual(cache.status(["note"]), "ready")

    def test_pump_observes_injected_clock_budget_between_uploads(self):
        now = [0.0]

        def upload(data, channels, rate):
            now[0] += 0.003
            self.uploads.append(1)
            return FakeBuffer(data, channels, rate)

        cache = self.cache(upload=upload, clock=lambda: now[0])
        cache.request("note")
        self.wait(lambda: cache.stats()["completed"] == 1)
        self.assertEqual(cache.pump(max_uploads=4, budget_seconds=0.002), 1)
        self.assertEqual(len(self.uploads), 1)
        self.assertEqual(cache.pump(max_uploads=0), 0)
        self.assertEqual(cache.pump(budget_seconds=0), 0)

    def test_owner_only_api_and_worker_never_uploads(self):
        cache = self.cache()
        failures = []

        def foreign():
            for call in (lambda: cache.request("note"), lambda: cache.get("note"),
                         lambda: cache.status(["note"]), cache.pump,
                         cache.clear, cache.close, cache.stats):
                try:
                    call()
                except RuntimeError:
                    failures.append(True)

        thread = threading.Thread(target=foreign)
        thread.start()
        thread.join(1)
        self.assertEqual(len(failures), 7)
        cache.request("note")
        self.wait(lambda: cache.stats()["completed"] == 1)
        self.assertEqual(self.uploads, [])
        cache.pump(budget_seconds=1)
        self.assertEqual(len(self.uploads), 4)

    def test_clear_during_decode_discards_stale_generation(self):
        entered, release = threading.Event(), threading.Event()
        self.releases.append(release)
        count = [0]

        def decode(path):
            count[0] += 1
            if count[0] == 1:
                entered.set()
                release.wait(2)
                return pcm((11,), channels=1)
            return pcm((22,), channels=1)

        cache = self.cache(decode=decode)
        cache.request("note")
        self.assertTrue(entered.wait(1))
        cache.clear()
        self.assertEqual(cache.stats()["pending"], 0)
        cache.request("note")
        release.set()
        self.ready(cache, ["note"])
        self.assertEqual([data for data, _, _ in self.uploads], [struct.pack("<h", 22)])
        self.assertEqual(count[0], 2)

    def test_clear_discards_partial_upload_and_ready_cache_on_owner(self):
        cache = self.cache()
        cache.request("note")
        self.wait(lambda: cache.stats()["completed"] == 1)
        cache.pump(max_uploads=1, budget_seconds=1)
        self.assertTrue(cache.stats()["uploading"])
        cache.clear()
        self.assertFalse(cache.stats()["uploading"])
        self.assertEqual(cache.pump(), 0)
        self.assertEqual(cache.stats()["entries"], 0)
        cache.request("note")
        self.ready(cache, ["note"])
        external_buffer = cache.get("note")
        cache.clear()
        self.assertEqual(external_buffer.data, pcm().buffer)

    def test_close_does_not_wait_for_blocked_decoder_and_is_terminal(self):
        entered, release = threading.Event(), threading.Event()
        self.releases.append(release)

        def decode(path):
            entered.set()
            release.wait(2)
            return pcm()

        cache = self.cache(decode=decode)
        cache.request("note")
        self.assertTrue(entered.wait(1))
        cache.close()
        cache.close()
        self.assertFalse(release.is_set())
        self.assertTrue(cache._worker.is_alive())
        self.assertEqual(cache.request("new"), 0)
        self.assertIsNone(cache.get("new"))
        self.assertEqual(cache.status(["new"]), "failed")
        release.set()
        cache._worker.join(1)
        self.assertEqual(cache.pump(), 0)
        self.assertEqual(self.uploads, [])

    def test_pending_and_completed_queues_stay_bounded(self):
        entered, release = threading.Event(), threading.Event()
        self.releases.append(release)

        def decode(path):
            entered.set()
            release.wait(2)
            return pcm((1,), channels=1)

        cache = self.cache(decode=decode, max_pending=4, max_completed=1)
        self.assertEqual(cache.request([str(index) for index in range(20)]), 4)
        self.assertTrue(entered.wait(1))
        self.assertEqual(cache.stats()["pending"], 4)
        self.assertLessEqual(cache.stats()["queued"], 4)
        release.set()
        self.wait(lambda: cache.stats()["completed"] == 1)
        self.assertLessEqual(cache.stats()["completed"], 1)
        self.ready(cache, ["0", "1", "2", "3"])
        self.assertEqual(cache.request(["4"]), 1)
        self.ready(cache, ["4"])

    def test_lru_entry_and_byte_budgets_preserve_external_owner(self):
        cache = self.cache(decode=lambda path: pcm((1, 2), channels=1),
                           max_entries=2, max_cache_bytes=8)
        for path in ("a", "b"):
            cache.request(path)
            self.ready(cache, [path])
        external = cache.get("a")  # Keep a hot; b should be evicted.
        cache.request("c")
        self.ready(cache, ["c"])
        self.assertEqual(cache.stats()["entries"], 2)
        self.assertEqual(cache.stats()["cache_bytes"], 8)
        self.assertIs(cache.get("a"), external)
        self.assertIsNotNone(cache.get("c"))
        self.assertIsNone(cache.get("b"))
        self.assertEqual(external.data, struct.pack("<hh", 1, 2))

    def test_failures_are_bounded_and_not_retried_each_frame(self):
        calls = Counter()

        def decode(path):
            calls[path] += 1
            raise ValueError("fake decode failure")

        cache = self.cache(decode=decode, max_failures=2)
        for path in ("a", "b", "c"):
            cache.request(path)
            self.wait(lambda: (cache.pump(budget_seconds=1), cache.status([path]))[1] == "failed")
        self.assertEqual(cache.stats()["failures"], 2)
        for _ in range(20):
            self.assertIsNone(cache.get("c"))
            self.assertEqual(cache.status(["c"]), "failed")
            self.assertEqual(cache.request("c"), 0)
        self.assertEqual(calls["c"], 1)
        cache.clear()
        cache.request("c")
        self.wait(lambda: calls["c"] == 2)

    def test_invalid_and_oversized_pcm_fail_without_upload(self):
        invalid = [pcm((1,), channels=2), pcm((1,), channels=3),
                   SimpleNamespace(buffer=b"", channels=1, frequency=48000),
                   SimpleNamespace(buffer=b"\0\0", channels=1, frequency=0),
                   pcm((1, 2, 3, 4), channels=1)]
        for sample in invalid:
            with self.subTest(sample=sample):
                cache = self.cache(decode=lambda path, value=sample: value,
                                   max_sample_bytes=6)
                cache.request("bad")
                self.wait(lambda: (cache.pump(budget_seconds=1), cache.status(["bad"]))[1] == "failed")
        self.assertEqual(self.uploads, [])

    def test_combined_representations_must_fit_budget_before_upload(self):
        cache = self.cache(max_cache_bytes=len(pcm().buffer))
        cache.request("large")
        self.wait(lambda: (cache.pump(budget_seconds=1), cache.status(["large"]))[1] == "failed")
        self.assertEqual(self.uploads, [])

    def test_batch_larger_than_lru_budget_fails_instead_of_redecode_loop(self):
        cache = self.cache(decode=lambda path: pcm((1, 2), channels=1),
                           max_cache_bytes=4)
        cache.request(["a", "b"])
        self.wait(lambda: (cache.pump(budget_seconds=1), cache.status(["a", "b"]))[1] == "failed")
        self.assertLessEqual(cache.stats()["cache_bytes"], 4)
        cache = self.cache(max_entries=1)
        self.assertEqual(cache.status(["a", "b"]), "failed")
        self.assertEqual(cache.stats()["pending"], 0)

    def test_upload_failure_drops_partial_buffers_without_retry(self):
        calls = [0]

        def upload(data, channels, rate):
            calls[0] += 1
            if calls[0] == 2:
                raise RuntimeError("fake upload failure")
            return FakeBuffer(data, channels, rate)

        cache = self.cache(upload=upload)
        cache.request("note")
        self.wait(lambda: (cache.pump(budget_seconds=1), cache.status(["note"]))[1] == "failed")
        self.assertEqual(calls[0], 2)
        self.assertFalse(cache.stats()["uploading"])
        self.assertEqual(cache.stats()["entries"], 0)
        self.assertIsNone(cache.get("note"))

    def test_bad_resolver_kind_and_limits(self):
        cache = self.cache(resolve=lambda path: None)
        self.assertEqual(cache.status(["invalid"]), "failed")
        self.assertEqual(cache.request(["invalid"]), 0)
        self.assertIsNone(cache.get("invalid"))
        with self.assertRaises(ValueError):
            cache.get("invalid", "unknown")
        with self.assertRaises(ValueError):
            self.cache(max_pending=0)


if __name__ == "__main__":
    unittest.main()
