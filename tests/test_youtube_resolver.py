"""Offline resolver IPC tests; fake extraction, no game or remote requests."""

import os
import socket
import subprocess
import sys
import threading
import time
import unittest
from unittest.mock import patch

from libs import youtube_resolver as resolver


class YouTubeResolverTests(unittest.TestCase):
    def in_worker(self, function, timeout=5):
        results = []
        thread = threading.Thread(target=lambda: results.append(function()))
        thread.start()
        thread.join(timeout)
        self.assertFalse(thread.is_alive(), "offline resolver worker exceeded test deadline")
        self.assertEqual(len(results), 1)
        return results[0]

    def offline_command(self, body):
        source = ("import sys\nfrom libs import youtube_resolver as r\n" + body
                  + "\nraise SystemExit(r.worker_main(sys.argv[1:]))\n")
        return lambda port, token: [sys.executable, "-c", source, str(port), token]

    def test_main_thread_and_invalid_urls_never_spawn(self):
        with patch.object(resolver.subprocess, "Popen") as spawn:
            self.assertIsNone(resolver.resolve_stream_info("https://example.invalid/watch"))
            for url in ("file:///private", "ytsearch:test", "http://", "https://x\r\nsecret", object()):
                self.assertIsNone(self.in_worker(lambda: resolver.resolve_stream_info(url)))
            spawn.assert_not_called()

    def test_source_and_frozen_and_nuitka_commands(self):
        with patch.object(resolver.sys, "frozen", False, create=True):
            source = resolver._command(1234, "a" * 64)
            self.assertEqual(source[0], sys.executable)
            self.assertTrue(os.path.isabs(source[1]))
            self.assertEqual(os.path.basename(source[1]), "beyond_tournament.py")
            self.assertEqual(source[2:], [resolver.RESOLVER_FLAG, "1234", "a" * 64])
        with patch.object(resolver.sys, "frozen", True, create=True):
            self.assertEqual(resolver._command(1234, "a" * 64),
                             [sys.executable, resolver.RESOLVER_FLAG, "1234", "a" * 64])
        with patch.object(resolver, "__compiled__", object(), create=True):
            self.assertEqual(len(resolver._command(1234, "a" * 64)), 4)

    def test_extractor_options_and_exact_paired_headers(self):
        observed = {}
        expected = {"url": "https://example.invalid/audio?sig=a%2Bb",
                    "http_headers": {"User-Agent": "Exact Agent/1.0", "Referer": "https://example.invalid/"}}
        class Extractor:
            def __init__(self, options):
                observed.update(options)
            def __enter__(self):
                return self
            def __exit__(self, *_):
                pass
            def extract_info(self, url, download):
                self_test.assertEqual(url, "https://example.invalid/page")
                self_test.assertFalse(download)
                return dict(expected, title="must be dropped", other="not returned")
        self_test = self
        self.assertEqual(resolver._extract("https://example.invalid/page", Extractor), expected)
        self.assertEqual(observed["format"], "best[acodec!=none][vcodec!=none][height<=360]/bestaudio/best")
        self.assertTrue(observed["noplaylist"])
        self.assertFalse(observed["cachedir"])
        self.assertEqual(observed["js_runtimes"], {})
        self.assertEqual(observed["remote_components"], [])
        self.assertLessEqual(observed["socket_timeout"], 6)
        self.assertEqual(observed["extractor_args"]["youtube"]["player_client"], ["android", "web"])

    def test_response_validation_rejects_header_injection_and_limits(self):
        for headers in ({"User-Agent": "x\r\nHost: evil"}, {"bad name": "value"},
                        {"User-Agent": "a", "user-agent": "b"},
                        {"X": "a" * 4097}, {str(i): "x" for i in range(33)}, [], False, ""):
            self.assertIsNone(resolver._validated_info({"url": "https://example.invalid", "http_headers": headers}))
        self.assertIsNone(resolver._validated_info({"url": "file:///private"}))
        self.assertIsNone(resolver._validated_info({"url": "https://x/" + "a" * 16384}))

    def extract_fixture(self, info):
        class Extractor:
            def __init__(self, options): pass
            def __enter__(self): return self
            def __exit__(self, *_): pass
            def extract_info(self, url, download=False): return info
        return resolver._extract("https://example.invalid/page", Extractor)

    def test_extractor_normalizes_header_dict_subclass_without_mutation(self):
        class HeaderDict(dict):
            pass
        headers = HeaderDict({"User-Agent": "Exact Agent/1.0", "Referer": "https://example.invalid/"})
        info = {"url": "https://rr.example.googlevideo.com/audio?sig=a%2Bb",
                "http_headers": headers, "title": "not returned"}
        result = self.extract_fixture(info)
        self.assertEqual(result, {"url": info["url"], "http_headers": dict(headers)})
        self.assertIs(type(result["http_headers"]), dict)
        self.assertIs(info["http_headers"], headers)
        self.assertEqual(len(info), 3)
        # The library boundary accepts subclasses; the JSON boundary stays strict.
        self.assertIsNone(resolver._validated_info(info))

    def test_extractor_accepts_installed_yt_dlp_http_header_dict(self):
        try:
            from yt_dlp.utils.networking import HTTPHeaderDict
        except ImportError:
            self.skipTest("yt-dlp HTTPHeaderDict is not installed")
        headers = HTTPHeaderDict({"User-Agent": "Exact Agent/1.0", "Accept": "*/*"})
        info = {"url": "https://rr.example.googlevideo.com/audio?sig=a%2Bb", "http_headers": headers}
        self.assertIsNot(type(headers), dict)
        self.assertEqual(self.extract_fixture(info), {"url": info["url"], "http_headers": dict(headers)})

    def test_extractor_normalization_keeps_validation_and_headerless_http(self):
        class HeaderDict(dict):
            pass
        for headers in (HeaderDict({"User-Agent": "x\r\nHost: evil"}),
                        HeaderDict({str(i): "x" for i in range(33)}),
                        HeaderDict({"X": "a" * 4097}),
                        HeaderDict({"User-Agent": "a", "user-agent": "b"}),
                        HeaderDict({"X": 123}), [], [("User-Agent", "value")], False, ""):
            with self.subTest(headers_type=type(headers).__name__):
                self.assertIsNone(self.extract_fixture({"url": "https://example.invalid/audio",
                                                       "http_headers": headers}))
        for info in (None, [], {"url": "file:///private"}, {"http_headers": {}},
                     {"url": "https://rr.example.googlevideo.com/audio"}):
            self.assertIsNone(self.extract_fixture(info))
        for headers in (None, {}, HeaderDict()):
            self.assertEqual(self.extract_fixture({"url": "https://example.invalid/audio", "http_headers": headers}),
                             {"url": "https://example.invalid/audio", "http_headers": {}})

    def test_signed_googlevideo_requires_paired_headers_but_generic_http_does_not(self):
        url = "https://rr1.example.googlevideo.com/videoplayback?sig=signed"
        self.assertIsNone(resolver._validated_info({"url": url, "http_headers": {}}))
        headers = {"User-Agent": "Keep Exact Value"}
        self.assertEqual(resolver._validated_info({"url": url, "http_headers": headers}),
                         {"url": url, "http_headers": headers})
        generic = {"url": "https://example.invalid/audio", "http_headers": {}}
        self.assertEqual(resolver._validated_info(generic), generic)

    def test_missing_yt_dlp_dependency_returns_none_without_fallback(self):
        with patch.dict(sys.modules, {"yt_dlp": None}):
            self.assertIsNone(resolver._extract("https://example.invalid/page"))

    def test_real_offline_helper_protocol_success(self):
        body = "r._extract = lambda url: {'url': 'https://example.invalid/audio', 'http_headers': {'User-Agent': 'Exact/1.0'}}"
        with patch.object(resolver, "_command", self.offline_command(body)):
            result = self.in_worker(lambda: resolver.resolve_stream_info("https://example.invalid/page"))
        self.assertEqual(result, {"url": "https://example.invalid/audio", "http_headers": {"User-Agent": "Exact/1.0"}})

    def test_wrong_token_connection_is_rejected_before_real_helper(self):
        body = ("import socket,time\n"
                "bad=socket.create_connection(('127.0.0.1',int(sys.argv[1])))\n"
                "bad.settimeout(.05)\n"
                "r._send_json(bad,{'token':'0'*64},time.monotonic()+1)\n"
                "bad.close()\n"
                "r._extract=lambda url:{'url':'https://example.invalid/audio','http_headers':{}}")
        with patch.object(resolver, "_command", self.offline_command(body)):
            result = self.in_worker(lambda: resolver.resolve_stream_info("https://example.invalid/page"))
        self.assertIsNotNone(result)

    def test_deadline_reaps_unresponsive_offline_child(self):
        body = "import time\nr._extract = lambda url: time.sleep(10)"
        owned = []
        real_popen = subprocess.Popen
        def spawn(*args, **kwargs):
            child = real_popen(*args, **kwargs)
            owned.append(child)
            return child
        with patch.object(resolver, "_command", self.offline_command(body)), \
                patch.object(resolver, "_TOTAL_TIMEOUT", .2), \
                patch.object(resolver.subprocess, "Popen", side_effect=spawn):
            self.assertIsNone(self.in_worker(lambda: resolver.resolve_stream_info("https://example.invalid")))
        self.assertEqual(len(owned), 1)
        self.assertIsNotNone(owned[0].poll())

    def test_concurrent_real_helpers_never_exceed_two(self):
        body = ("import time\ndef extract(url):\n time.sleep(.15)\n"
                " return {'url':'https://example.invalid/audio','http_headers':{}}\n"
                "r._extract=extract")
        owned, results = [], []
        maximum = [0]
        lock = threading.Lock()
        real_popen = subprocess.Popen
        def spawn(*args, **kwargs):
            with lock:
                child = real_popen(*args, **kwargs)
                owned.append(child)
                maximum[0] = max(maximum[0], sum(child.poll() is None for child in owned))
                return child
        try:
            with patch.object(resolver, "_command", self.offline_command(body)), \
                    patch.object(resolver.subprocess, "Popen", side_effect=spawn):
                workers = [threading.Thread(target=lambda: results.append(
                    resolver.resolve_stream_info("https://example.invalid"))) for _ in range(4)]
                for worker in workers:
                    worker.start()
                for worker in workers:
                    worker.join(5)
                    self.assertFalse(worker.is_alive())
            self.assertEqual(len(results), 4)
            self.assertTrue(all(result is not None for result in results))
            self.assertEqual(maximum[0], 2)
            self.assertTrue(all(child.poll() is not None for child in owned))
        finally:
            for child in owned:
                resolver._reap_owned(child)

    def test_child_environment_removes_import_and_interactive_overrides(self):
        with patch.dict(os.environ, {"PYTHONPATH": "untrusted", "PYTHONHOME": "untrusted", "PYTHONINSPECT": "1"}):
            environment = resolver._child_environment()
        self.assertNotIn("PYTHONPATH", environment)
        self.assertNotIn("PYTHONHOME", environment)
        self.assertNotIn("PYTHONINSPECT", environment)
        self.assertEqual(environment["PYTHONDONTWRITEBYTECODE"], "1")

    def test_cancel_kills_and_reaps_only_owned_offline_child(self):
        body = "import time\nr._extract = lambda url: time.sleep(10)"
        stop = threading.Event()
        owned = []
        real_popen = subprocess.Popen
        def spawn(*args, **kwargs):
            child = real_popen(*args, **kwargs)
            owned.append(child)
            return child
        timer = threading.Timer(.2, stop.set)
        try:
            with patch.object(resolver, "_command", self.offline_command(body)), \
                    patch.object(resolver.subprocess, "Popen", side_effect=spawn):
                timer.start()
                self.assertIsNone(self.in_worker(lambda: resolver.resolve_stream_info(
                    "https://example.invalid", cancelled=stop.is_set)))
            self.assertEqual(len(owned), 1)
            self.assertIsNotNone(owned[0].poll())
        finally:
            timer.cancel()
            for child in owned:
                resolver._reap_owned(child)

    def test_slot_wait_honors_cancellation_and_deadline_without_spawn(self):
        blocked = threading.BoundedSemaphore(1)
        blocked.acquire()
        with patch.object(resolver, "_SLOTS", blocked), \
                patch.object(resolver, "_TOTAL_TIMEOUT", .06), \
                patch.object(resolver.subprocess, "Popen") as spawn:
            self.assertIsNone(self.in_worker(lambda: resolver.resolve_stream_info("https://example.invalid")))
            self.assertIsNone(self.in_worker(lambda: resolver.resolve_stream_info(
                "https://example.invalid", cancelled=lambda: True)))
            spawn.assert_not_called()
        blocked.release()

    def test_spawn_failure_releases_slot_and_never_extracts_locally(self):
        semaphore = threading.BoundedSemaphore(1)
        with patch.object(resolver, "_SLOTS", semaphore), \
                patch.object(resolver.subprocess, "Popen", side_effect=OSError("private path")), \
                patch.object(resolver, "_extract") as extract:
            self.assertIsNone(self.in_worker(lambda: resolver.resolve_stream_info("https://example.invalid")))
            extract.assert_not_called()
        self.assertTrue(semaphore.acquire(blocking=False))
        semaphore.release()

    def test_oversized_frame_is_rejected_before_payload_read(self):
        class Connection:
            def recv(self, size):
                return (resolver._MAX_FRAME + 1).to_bytes(4, "big")
        with self.assertRaises(ValueError):
            resolver._recv_json(Connection(), time.monotonic() + 1)

    def test_invalid_request_child_exits_without_extraction(self):
        # A real child gets a malformed request on loopback; stub extraction
        # would exit99 if ever called. No game bootstrap or remote URL is used.
        token = "a" * 64
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            listener.settimeout(2)
            command = self.offline_command("import os\nr._extract = lambda url: os._exit(99)")
            child = subprocess.Popen(command(listener.getsockname()[1], token),
                                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL,
                                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            try:
                with listener.accept()[0] as connection:
                    connection.settimeout(.05)
                    self.assertEqual(resolver._recv_json(connection, time.monotonic() + 2), {"token": token})
                    resolver._send_json(connection, {"url": "file:///never-read"}, time.monotonic() + 2)
                self.assertEqual(child.wait(timeout=2), 2)
            finally:
                resolver._reap_owned(child)

    def test_parent_eof_stops_orphaned_offline_child(self):
        token = "b" * 64
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            listener.settimeout(2)
            command = self.offline_command("import time\nr._extract = lambda url: time.sleep(10)")
            child = subprocess.Popen(command(listener.getsockname()[1], token),
                                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL,
                                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            try:
                connection = listener.accept()[0]
                connection.settimeout(.05)
                resolver._recv_json(connection, time.monotonic() + 2)
                resolver._send_json(connection, {"url": "https://example.invalid"}, time.monotonic() + 2)
                connection.close()
                self.assertEqual(child.wait(timeout=2), 125)
            finally:
                resolver._reap_owned(child)

    def test_child_audit_policy_rejects_external_helpers(self):
        for event in ("subprocess.Popen", "os.system", "os.exec", "os.spawn", "os.posix_spawn", "os.fork"):
            with self.assertRaises(RuntimeError):
                resolver._deny_helpers(event, ())
        resolver._deny_helpers("socket.connect", ())

    def test_invalid_worker_arguments_return_without_initialization(self):
        with patch.object(resolver.socket, "create_connection") as connect:
            for args in (None, [], ["0", "a" * 64], ["65536", "a" * 64], ["1", "bad"], ["๑", "a" * 64]):
                self.assertEqual(resolver.worker_main(args), 2)
            connect.assert_not_called()


if __name__ == "__main__":
    unittest.main()
