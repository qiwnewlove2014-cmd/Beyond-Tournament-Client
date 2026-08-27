"""Pure fake PCM/native tests; no audio devices, files, or network."""

import gc
import threading
import time
import unittest
import weakref
from types import SimpleNamespace
from unittest.mock import patch

from libs.map_sound_cache import MapSoundCache


def pcm(size=8, channels=2, rate=48000):
    return SimpleNamespace(buffer=b"\0" * size, channels=channels, frequency=rate)


class Native:
    def __init__(self, size, released=None):
        self.size, self.released = size, released

    def __del__(self):
        if self.released is not None:
            self.released.append(threading.get_ident())


class MapSoundCacheTests(unittest.TestCase):
    def setUp(self):
        self.caches = []

    def tearDown(self):
        for cache in self.caches:
            cache.close()

    def cache(self, decode=lambda path: pcm(), upload=None, **kwargs):
        cache = MapSoundCache(lambda path: path.lower(), decode,
                              upload or (lambda data, channels, rate: Native(len(data))),
                              **kwargs)
        self.caches.append(cache)
        return cache

    def wait_for(self, predicate):
        deadline = time.monotonic() + 2
        while not predicate():
            if time.monotonic() >= deadline:
                self.fail("mock worker did not finish")
            threading.Event().wait(.001)

    def ready(self, cache, path):
        cache.get(path)
        def complete():
            cache.pump()
            return cache.status(path) in ("ready", "failed")
        self.wait_for(complete)
        self.assertEqual(cache.status(path), "ready")
        return cache.get(path)

    def test_cold_status_does_not_queue_and_request_deduplicates(self):
        calls, uploads = [], []
        owner = threading.get_ident()
        def decode(path):
            calls.append((path, threading.get_ident()))
            return pcm()
        def upload(data, channels, rate):
            uploads.append((threading.get_ident(), channels, rate))
            return Native(len(data))
        cache = self.cache(decode, upload)
        self.assertEqual(cache.status("TEST"), "cold")
        self.assertEqual(cache.stats()["pending"], 0)
        for _ in range(10):
            self.assertIsNone(cache.get("TEST"))
        self.wait_for(lambda: cache.stats()["completed"] == 1)
        self.assertEqual(cache.status("test"), "pending")
        self.assertEqual(cache.pump(), 1)
        first = cache.get("test")
        self.assertIs(cache.get("TEST"), first)
        self.assertEqual(len(calls), 1)
        self.assertNotEqual(calls[0][1], owner)
        self.assertEqual(uploads, [(owner, 2, 48000)])

    def test_begin_map_keeps_warm_buffers_and_failures(self):
        calls = []
        def decode(path):
            calls.append(path)
            if path == "missing":
                raise FileNotFoundError("not retained")
            return pcm()
        cache = self.cache(decode)
        first = self.ready(cache, "warm")
        cache.get("missing")
        self.wait_for(lambda: cache.stats()["completed"] == 1)
        cache.pump()
        cache.begin_map()
        self.assertIs(cache.get("warm"), first)
        self.assertEqual(cache.status("missing"), "failed")
        self.assertIsNone(cache.get("missing"))
        self.assertEqual(calls, ["warm", "missing"])

    def test_cancelled_decode_cannot_upload_or_displace_new_request(self):
        entered, release = threading.Event(), threading.Event()
        calls, uploads = [], []
        def decode(path):
            calls.append(path)
            if len(calls) == 1:
                entered.set()
                release.wait(2)
            return pcm()
        cache = self.cache(decode, lambda *args: uploads.append(args) or Native(8))
        try:
            cache.get("same")
            self.assertTrue(entered.wait(1))
            cache.begin_map()
            self.assertEqual(cache.status("same"), "cold")
            cache.get("same")
        finally:
            release.set()
        self.ready(cache, "same")
        self.assertEqual(calls, ["same", "same"])
        self.assertEqual(len(uploads), 1)

    def test_completed_result_cancelled_before_pump(self):
        uploads = []
        cache = self.cache(upload=lambda *args: uploads.append(args) or Native(8))
        cache.get("old")
        self.wait_for(lambda: cache.stats()["completed"] == 1)
        cache.begin_map()
        self.assertEqual(cache.pump(), 0)
        self.assertEqual(uploads, [])
        self.assertEqual(cache.status("old"), "cold")

    def test_failed_decode_does_not_retry_until_explicit_retry(self):
        calls = []
        def decode(path):
            calls.append(path)
            if len(calls) == 1:
                raise ValueError("secret failure")
            return pcm()
        cache = self.cache(decode)
        cache.get("a")
        self.wait_for(lambda: cache.stats()["completed"] == 1)
        self.assertEqual(cache.pump(), 0)
        for _ in range(10):
            self.assertIsNone(cache.get("a"))
        self.assertEqual(len(calls), 1)
        self.assertEqual(cache.status("a"), "failed")
        self.assertTrue(cache.retry("a"))
        self.assertEqual(cache.status("a"), "cold")
        self.assertFalse(cache.retry("a"))
        self.ready(cache, "a")
        self.assertEqual(len(calls), 2)

    def test_pending_and_completed_queues_are_bounded(self):
        entered, release = threading.Event(), threading.Event()
        def decode(path):
            entered.set()
            release.wait(2)
            return pcm()
        cache = self.cache(decode, max_pending=2, max_completed=1)
        try:
            cache.get("a")
            self.assertTrue(entered.wait(1))
            cache.get("b")
            cache.get("c")
            self.assertEqual(cache.stats()["pending"], 2)
            self.assertEqual(cache.status("c"), "cold")
        finally:
            release.set()
        self.wait_for(lambda: cache.stats()["completed"] == 1)
        self.assertLessEqual(cache.stats()["queued"], 2)
        self.assertEqual(cache.pump(), 1)
        self.wait_for(lambda: cache.stats()["completed"] == 1)
        self.assertEqual(cache.pump(), 1)
        self.ready(cache, "c")

    def test_lru_entry_and_byte_bounds_release_on_owner(self):
        released = []
        cache = self.cache(upload=lambda data, *args: Native(len(data), released),
                           max_entries=2, max_cache_bytes=16)
        first = self.ready(cache, "a")
        self.ready(cache, "b")
        cache.get("a")  # a is newer than b.
        self.ready(cache, "c")
        self.assertIs(cache.get("a"), first)
        self.assertEqual(cache.status("b"), "cold")
        self.assertEqual(cache.stats()["entries"], 2)
        self.assertEqual(cache.stats()["cache_bytes"], 16)
        self.assertEqual(released, [threading.get_ident()])

    def test_failure_cache_bounded_and_oversized_pcm_rejected(self):
        uploads = []
        cache = self.cache(lambda path: pcm(12),
                           lambda *args: uploads.append(args) or Native(12),
                           max_sample_bytes=8, max_failures=2)
        for path in ("a", "b", "c"):
            cache.get(path)
            self.wait_for(lambda: cache.stats()["completed"] == 1)
            cache.pump()
        self.assertEqual(cache.stats()["failures"], 2)
        self.assertEqual(cache.status("a"), "cold")
        self.assertEqual(cache.status("c"), "failed")
        self.assertEqual(uploads, [])

    def test_eviction_releases_cache_only_native_before_next_upload(self):
        previous = [None]
        def upload(data, channels, rate):
            if previous[0] is not None:
                self.assertIsNone(previous[0]())
            native = Native(len(data))
            previous[0] = weakref.ref(native)
            return native
        cache = self.cache(upload=upload, max_cache_bytes=8)
        self.ready(cache, "a")
        self.ready(cache, "b")
        self.assertEqual(cache.stats()["cache_bytes"], 8)

    def test_sample_larger_than_native_cache_budget_is_not_uploaded(self):
        uploads = []
        cache = self.cache(lambda path: pcm(12),
                           lambda *args: uploads.append(args) or Native(12),
                           max_cache_bytes=8)
        cache.get("a")
        self.wait_for(lambda: cache.stats()["completed"] == 1)
        self.assertEqual(cache.pump(), 0)
        self.assertEqual(cache.status("a"), "failed")
        self.assertEqual(uploads, [])

    def test_upload_failure_is_sticky_and_counted_as_attempt(self):
        calls = []
        def upload(*args):
            calls.append(args)
            raise RuntimeError("native failure")
        cache = self.cache(upload=upload)
        cache.get("a")
        self.wait_for(lambda: cache.stats()["completed"] == 1)
        self.assertEqual(cache.pump(), 1)
        self.assertEqual(cache.status("a"), "failed")
        self.assertIsNone(cache.get("a"))
        self.assertEqual(len(calls), 1)

    def test_soft_budget_stops_between_uploads(self):
        clock = [0.0]
        calls = []
        def upload(*args):
            calls.append(args)
            clock[0] += .003
            return Native(8)
        cache = self.cache(upload=upload, clock=lambda: clock[0], max_completed=2)
        cache.get("a")
        cache.get("b")
        self.wait_for(lambda: cache.stats()["completed"] == 2)
        self.assertEqual(cache.pump(max_uploads=2, budget_seconds=.002), 1)
        self.assertEqual(len(calls), 1)
        self.assertEqual(cache.stats()["completed"], 1)

    def test_close_blocked_worker_nonjoining_and_no_cache_retention(self):
        entered, release = threading.Event(), threading.Event()
        def decode(path):
            entered.set()
            release.wait(2)
            return pcm()
        cache = MapSoundCache(lambda path: path, decode, lambda *args: Native(8))
        cache.get("blocked")
        self.assertTrue(entered.wait(1))
        worker = cache._worker
        reference = weakref.ref(cache)
        try:
            with patch.object(worker, "join", side_effect=AssertionError("must not join")):
                cache.close()
            self.assertTrue(worker.is_alive())
            del cache
            gc.collect()
            self.assertIsNone(reference())
        finally:
            release.set()
        worker.join(1)  # Test cleanup only, never the owner API.
        self.assertFalse(worker.is_alive())

    def test_start_failure_is_sticky_and_retry_can_start_worker(self):
        cache = self.cache()
        with patch("libs.map_sound_cache.threading.Thread.start", side_effect=RuntimeError("start")):
            self.assertIsNone(cache.get("a"))
            self.assertEqual(cache.status("a"), "failed")
            self.assertEqual(cache.stats()["pending"], 0)
        cache.retry("a")
        self.ready(cache, "a")

    def test_owner_guards_every_public_method(self):
        cache = self.cache()
        errors = []
        def worker():
            for method in (lambda: cache.get("a"), lambda: cache.status("a"),
                           lambda: cache.retry("a"), cache.begin_map, cache.close,
                           cache.pump, cache.stats):
                try:
                    method()
                except RuntimeError:
                    errors.append(1)
        thread = threading.Thread(target=worker)
        thread.start()
        thread.join(1)
        self.assertEqual(len(errors), 7)
        self.assertEqual(cache.status("a"), "cold")

    def test_reentrant_map_change_during_upload_discards_native_result(self):
        released = []
        cache = None
        def upload(*args):
            cache.begin_map()
            return Native(8, released)
        cache = self.cache(upload=upload)
        cache.get("a")
        self.wait_for(lambda: cache.stats()["completed"] == 1)
        self.assertEqual(cache.pump(), 1)
        self.assertEqual(cache.status("a"), "cold")
        self.assertEqual(released, [threading.get_ident()])

    def test_invalid_pcm_format_and_alignment_are_failures(self):
        for decoded in (pcm(channels=3), pcm(rate=0), pcm(size=6), pcm(size=0)):
            with self.subTest(decoded=decoded):
                cache = self.cache(lambda path: decoded)
                cache.get("a")
                self.wait_for(lambda: cache.stats()["completed"] == 1)
                cache.pump()
                self.assertEqual(cache.status("a"), "failed")

    def test_close_is_idempotent_and_releases_ready_on_owner(self):
        released = []
        cache = self.cache(upload=lambda *args: Native(8, released))
        self.ready(cache, "a")
        cache.close()
        cache.close()
        self.assertEqual(released, [threading.get_ident()])
        self.assertIsNone(cache.get("a"))
        self.assertEqual(cache.status("a"), "failed")
        self.assertFalse(cache.retry("a"))
        self.assertEqual(cache.pump(), 0)


if __name__ == "__main__":
    unittest.main()
