"""Pure offline tests: no game, OpenAL, processes, or network requests."""

from dataclasses import FrozenInstanceError
import threading
import unittest

from libs.jukebox_media_cache import JukeboxMediaCache


class JukeboxMediaCacheTests(unittest.TestCase):
    def setUp(self):
        self.monotonic = 10.0
        self.wall = 1_700_000_000.0
        self.cache = JukeboxMediaCache(
            monotonic=lambda: self.monotonic,
            wall_clock=lambda: self.wall,
        )
        self.key = "https://www.youtube.com/watch?v=fixture"

    def info(self, *, expire=None, extra="", host="rr1.googlevideo.com",
             path="/videoplayback"):
        if expire is None:
            expire = int(self.wall + 3600)
        return {
            "url": f"https://{host}{path}?expire={expire}&sig=private{extra}",
            "http_headers": {
                "User-Agent": "Paired Agent/1.0",
                "Referer": "https://www.youtube.com/",
            },
        }

    def test_entry_copies_headers_and_returns_fresh_plain_dicts(self):
        original = self.info()
        expected = {"url": original["url"], "http_headers": dict(original["http_headers"])}
        entry = self.cache.put(self.key, original)
        original["url"] = "changed"
        original["http_headers"]["User-Agent"] = "changed"
        first = entry.info()
        self.assertEqual(first, expected)
        self.assertIs(type(first), dict)
        self.assertIs(type(first["http_headers"]), dict)
        first["http_headers"]["User-Agent"] = "changed again"
        first["url"] = "changed again"
        self.assertEqual(entry.info(), expected)
        self.assertIs(self.cache.get(self.key), entry)

    def test_entry_is_immutable_and_repr_does_not_disclose_credentials(self):
        entry = self.cache.put(self.key, self.info())
        with self.assertRaises((FrozenInstanceError, AttributeError)):
            entry._url = "changed"
        with self.assertRaises(TypeError):
            entry._headers[0] = ("changed", "changed")
        for representation in (repr(entry), str(entry), repr(self.cache)):
            self.assertNotIn("googlevideo", representation)
            self.assertNotIn("private", representation)
            self.assertNotIn("Paired Agent", representation)

    def test_non_sliding_ttl_expires_at_300_seconds(self):
        entry = self.cache.put(self.key, self.info())
        self.monotonic += 299.0
        self.assertIs(self.cache.get(self.key), entry)
        self.monotonic += 1.0
        self.assertIsNone(self.cache.get(self.key))

    def test_signed_expiry_caps_ttl_with_30_second_margin(self):
        entry = self.cache.put(self.key, self.info(expire=int(self.wall + 90)))
        self.monotonic += 59.0
        self.assertIs(self.cache.get(self.key), entry)
        self.monotonic += 1.0
        self.assertIsNone(self.cache.get(self.key))

    def test_forward_wall_clock_jump_invalidates_before_monotonic_ttl(self):
        self.cache.put(self.key, self.info(expire=int(self.wall + 90)))
        self.wall += 60.0
        self.assertIsNone(self.cache.get(self.key))

    def test_backward_wall_clock_jump_cannot_extend_ttl(self):
        self.cache.put(self.key, self.info())
        self.wall -= 10_000.0
        self.monotonic += 300.0
        self.assertIsNone(self.cache.get(self.key))

    def test_expired_or_nearly_expired_media_is_not_cached(self):
        for remaining in (-100, 0, 29, 30):
            with self.subTest(remaining=remaining):
                self.assertIsNone(self.cache.put(self.key, self.info(expire=int(self.wall + remaining))))

    def test_lru_capacity_is_eight_and_get_updates_recency(self):
        keys = [f"https://youtu.be/fixture{i}" for i in range(9)]
        for key in keys[:8]:
            self.assertIsNotNone(self.cache.put(key, self.info()))
        first = self.cache.get(keys[0])
        self.cache.put(keys[8], self.info())
        self.assertIsNone(self.cache.get(keys[1]))
        self.assertIs(self.cache.get(keys[0]), first)
        self.assertEqual(sum(self.cache.get(key) is not None for key in keys), 8)

    def test_put_prunes_expired_entries_before_evicting_live_entries(self):
        oldest = self.cache.put(self.key, self.info())
        keys = [f"https://youtu.be/fixture{i}" for i in range(8)]
        for key in keys[:7]:
            self.cache.put(key, self.info(expire=int(self.wall + 31)))
        self.monotonic += 1.0
        self.cache.put(keys[7], self.info())
        self.assertIs(self.cache.get(self.key), oldest)
        self.assertEqual(len(self.cache._entries), 2)

    def test_exact_entry_identity_invalidation_cannot_remove_replacement(self):
        info = self.info()
        older = self.cache.put(self.key, info)
        newer = self.cache.put(self.key, info)
        self.assertIsNot(older, newer)
        self.assertNotEqual(older, newer)
        self.assertFalse(self.cache.invalidate(self.key, older))
        self.assertIs(self.cache.get(self.key), newer)
        self.assertTrue(self.cache.invalidate(self.key, newer))
        self.assertFalse(self.cache.invalidate(self.key, newer))
        self.assertFalse(self.cache.invalidate(self.key, None))

    def test_invalid_keys_fail_closed_for_all_operations(self):
        for key in (None, [], {}, 1, "", "http://youtube.com/watch?v=x",
                    "https://user:secret@youtube.com/watch?v=x",
                    "https://youtube.com:444/watch?v=x",
                    "https://youtube.com.evil.invalid/watch?v=x",
                    "https://notyoutube.com/watch?v=x", "https://evil.invalid/youtube.com",
                    "file:///private", "https://youtube.com/\nprivate",
                    "https://youtube.com/" + "x" * 16384,
                    "https://youtube.com/live", "https://youtube.com/live/fixture",
                    "https://youtube.com/%6cive/fixture"):
            with self.subTest(key=key):
                self.assertIsNone(self.cache.put(key, self.info()))
                self.assertIsNone(self.cache.get(key))
                self.assertFalse(self.cache.invalidate(key, None))

    def test_canonical_https_hosts_are_accepted(self):
        for key in (self.key, "https://youtube.com/watch?v=x", "https://m.youtube.com/watch?v=x",
                    "https://music.youtube.com/watch?v=x", "https://youtu.be/x",
                    "https://youtube.com:443/watch?v=x"):
            self.assertIsNotNone(self.cache.put(key, self.info()))

    def test_invalid_media_types_and_auth_headers_are_not_cached(self):
        samples = [None, [], "", {}, {"url": 1}, {"url": self.info()["url"]}]
        for headers in ({}, None, [], {"User-Agent": "bad\r\nInjected: value"},
                        {"User-Agent": "one", "user-agent": "two"},
                        {"bad name": "value"}, {"X": "x" * 4097},
                        {str(i): "x" for i in range(33)},
                        {str(i): "x" * 4096 for i in range(9)}):
            samples.append(dict(self.info(), http_headers=headers))
        for info in samples:
            with self.subTest(info=info):
                self.assertIsNone(self.cache.put(self.key, info))

    def test_expiry_requires_one_ascii_bounded_integer(self):
        for expire in ("", "no", "-1", "+1700009999", "1700009999.0",
                       "1e12", "١٧٠٠٠٠٩٩٩٩", "9" * 21):
            self.assertIsNone(self.cache.put(self.key, self.info(expire=expire)))
        for extra in ("&expire=1700009999", "&EXPIRE=1700009999", "&%65xpire=1700009999"):
            self.assertIsNone(self.cache.put(self.key, self.info(extra=extra)))
        missing = self.info()
        missing["url"] = "https://rr1.googlevideo.com/videoplayback?sig=private"
        self.assertIsNone(self.cache.put(self.key, missing))
        uppercase = self.info()
        uppercase["url"] = uppercase["url"].replace("expire=", "EXPIRE=")
        self.assertIsNone(self.cache.put(self.key, uppercase))

    def test_invalid_put_does_not_evict_a_healthy_entry(self):
        entry = self.cache.put(self.key, self.info())
        self.assertIsNone(self.cache.put(self.key, None))
        self.assertIs(self.cache.get(self.key), entry)

    def test_clock_callbacks_run_outside_cache_lock(self):
        def monotonic():
            self.assertTrue(self.cache._lock.acquire(blocking=False))
            self.cache._lock.release()
            return self.monotonic
        self.cache._monotonic = monotonic
        entry = self.cache.put(self.key, self.info())
        self.assertIs(self.cache.get(self.key), entry)

    def test_only_direct_https_googlevideo_media_is_cached(self):
        for host in ("googlevideo.com.evil.invalid", "notgooglevideo.com", "example.invalid",
                     "user:secret@rr1.googlevideo.com", "rr1.googlevideo.com:444"):
            self.assertIsNone(self.cache.put(self.key, self.info(host=host)))
        for path in ("/api/manifest/hls", "/api/manifest/dash", "/unknown", "/videoplayback/other"):
            self.assertIsNone(self.cache.put(self.key, self.info(path=path)))
        insecure = self.info()
        insecure["url"] = insecure["url"].replace("https://", "http://")
        self.assertIsNone(self.cache.put(self.key, insecure))

    def test_live_markers_are_not_cached(self):
        for extra in ("&source=yt_live_broadcast", "&live=1", "&is_live=true",
                      "&livestream=1", "&noclen=1", "&live=", "&playlist_type=LIVE"):
            self.assertIsNone(self.cache.put(self.key, self.info(extra=extra)))
        self.assertIsNotNone(self.cache.put(self.key, self.info(extra="&live=0&source=youtube")))

    def test_excessive_query_fields_and_nonfinite_clocks_are_rejected(self):
        self.assertIsNone(self.cache.put(self.key, self.info(extra="&x=1" * 256)))
        self.monotonic = float("nan")
        self.assertIsNone(self.cache.put(self.key, self.info()))
        self.monotonic = 10.0
        self.cache.put(self.key, self.info())
        self.wall = float("inf")
        self.assertIsNone(self.cache.get(self.key))

    def test_concurrent_old_failure_never_removes_new_success(self):
        older = self.cache.put(self.key, self.info())
        replaced = threading.Event()
        errors = []
        result = []
        def publish():
            try:
                result.append(self.cache.put(self.key, self.info()))
            except BaseException as exc:
                errors.append(exc)
            finally:
                replaced.set()
        def fail_old():
            try:
                if not replaced.wait(2.0):
                    raise AssertionError("publisher did not finish")
                self.assertFalse(self.cache.invalidate(self.key, older))
            except BaseException as exc:
                errors.append(exc)
        threads = [threading.Thread(target=publish), threading.Thread(target=fail_old)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(3.0)
            self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertIs(self.cache.get(self.key), result[0])

    def test_concurrent_get_put_invalidate_keeps_capacity_bounded(self):
        barrier = threading.Barrier(5)
        errors = []
        def exercise(worker):
            try:
                barrier.wait(2.0)
                for index in range(100):
                    key = f"https://youtu.be/fixture{(worker + index) % 12}"
                    entry = self.cache.put(key, self.info())
                    found = self.cache.get(key)
                    if found is not None:
                        self.assertEqual(found.info(), self.info())
                    if index % 3 == 0:
                        self.cache.invalidate(key, entry)
            except BaseException as exc:
                errors.append(exc)
        threads = [threading.Thread(target=exercise, args=(worker,)) for worker in range(4)]
        for thread in threads:
            thread.start()
        barrier.wait(2.0)
        for thread in threads:
            thread.join(5.0)
            self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertLessEqual(len(self.cache._entries), 8)


if __name__ == "__main__":
    unittest.main()
