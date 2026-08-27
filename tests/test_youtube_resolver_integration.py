"""Resolver/stream lifecycle and bootstrap checks, without game/audio/network."""

import io
import os
from pathlib import Path
import runpy
import sys
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from libs.music_bot import AudioStreamer, MapMusicBot, YouTubeSearcher


class ResolverIntegrationTests(unittest.TestCase):
    def stream(self):
        stream = AudioStreamer(SimpleNamespace(), "https://youtube.com/watch?v=fixture",
                               object(), bot=None, spatial_pair=(object(), object(), 8, 40))
        stream._cleanup = Mock()
        stream._init_buffer_pool = Mock()
        return stream

    def test_searcher_delegates_url_and_cancellation_without_local_extraction(self):
        info = {"url": "https://media.invalid/audio", "http_headers": {"User-Agent": "Exact Agent"}}
        cancelled = Mock(return_value=False)
        with patch("libs.youtube_resolver.resolve_stream_info", return_value=info) as resolve:
            self.assertIs(YouTubeSearcher.get_stream_info("https://youtube.com/watch?v=fixture",
                          cancelled=cancelled), info)
        resolve.assert_called_once_with("https://youtube.com/watch?v=fixture", cancelled=cancelled)

    def test_cancel_during_resolve_drops_late_result_before_native_setup(self):
        stream = self.stream()
        def resolve(url, *, cancelled):
            self.assertFalse(cancelled())
            stream.running = False
            self.assertTrue(cancelled())
            return {"url": "https://media.invalid/audio", "http_headers": {}}
        with patch("libs.music_bot.FFMPEG_PATH", "not-executed"), \
             patch.object(YouTubeSearcher, "get_stream_info", side_effect=resolve), \
             patch("libs.music_bot.subprocess.Popen") as launch:
            stream.run()
        launch.assert_not_called()
        stream._init_buffer_pool.assert_not_called()
        stream._cleanup.assert_called_once()
        self.assertIsNone(stream.failure_reason)
        self.assertFalse(stream.ready_event.is_set())

    def test_resolve_failure_does_not_launch_ffmpeg_with_empty_input(self):
        stream = self.stream()
        with patch("libs.music_bot.FFMPEG_PATH", "not-executed"), \
             patch.object(YouTubeSearcher, "get_stream_info", return_value=None), \
             patch("libs.music_bot.subprocess.Popen") as launch:
            stream.run()
        launch.assert_not_called()
        stream._init_buffer_pool.assert_not_called()
        stream._cleanup.assert_called_once()
        self.assertEqual(stream.failure_reason, "audio link resolution failed")

    def test_retry_resolve_cancellation_cannot_launch_another_ffmpeg(self):
        stream = self.stream()
        calls = []
        def resolve(url, *, cancelled):
            calls.append(url)
            if len(calls) == 2:
                stream.running = False
            return {"url": "https://media.googlevideo.com/audio", "http_headers": {}}
        process = SimpleNamespace(poll=lambda: 1, stderr=io.BytesIO(b"403"),
                                  kill=Mock(), wait=Mock())
        stream._read_prebuffer = Mock(return_value=(0, b""))
        def launch(*args, **kwargs):
            process.stderr.seek(0)
            return process
        with patch("libs.music_bot.FFMPEG_PATH", "not-executed"), \
             patch.object(YouTubeSearcher, "get_stream_info", side_effect=resolve), \
             patch("libs.music_bot.subprocess.Popen", side_effect=launch) as popen, \
             patch("libs.music_bot.time.sleep"):
            stream.run()
        self.assertEqual(len(calls), 2)
        self.assertEqual(popen.call_count, 2)
        stream._cleanup.assert_called_once()
        self.assertFalse(stream.ready_event.is_set())

    def test_personal_song_generation_cancels_resolve_and_stale_fallback(self):
        bot = MapMusicBot.__new__(MapMusicBot)
        generation = [1]
        bot.is_loading_stream = False
        bot._clear_personal_feed = Mock()
        bot._begin_playback_generation = Mock(return_value=1)
        bot._is_current_playback_generation = lambda value: value == generation[0]
        bot.stop = Mock()
        bot.game = SimpleNamespace(put=Mock())
        def resolve(url, *, cancelled):
            self.assertFalse(cancelled())
            generation[0] = 2
            self.assertTrue(cancelled())
            return None
        def thread(*, target, **kwargs):
            return SimpleNamespace(start=target)
        with patch.object(YouTubeSearcher, "get_stream_info", side_effect=resolve), \
             patch("libs.music_bot.threading.Thread", side_effect=thread), patch("libs.speech.speak"):
            bot._start_youtube_stream_from_search("old", "https://youtube.com/watch?v=fixture",
                                                 "https://media.invalid/stale")
        bot.game.put.assert_not_called()


class ResolverBootstrapTests(unittest.TestCase):
    @staticmethod
    def offline_entry_command(work="", real_headers=False):
        # Exercise the actual early entry on the Thai/spaced source path, with
        # only the external extractor stubbed. No remote request is possible.
        script = str(Path(__file__).resolve().parents[1] / "beyond_tournament.py")
        code = """import os,sys,runpy,types
HEADER_SETUP
class FakeYDL:
    def __init__(self, options): pass
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def extract_info(self, url, download=False):
        exec(WORK)
        assert not any(name in sys.modules for name in ('pygame','cyal','libs.vfs','libs.options','libs.crash_reporting','libs.instance_manager'))
        return {'url':'https://rr.example.googlevideo.com/audio?sig=a%2Bb','http_headers':HeaderDict({'X-Test-Pid':str(os.getpid()),'User-Agent':'Exact Agent/1.0'})}
sys.modules['yt_dlp']=types.SimpleNamespace(YoutubeDL=FakeYDL)
sys.argv=sys.argv[1:]
runpy.run_path(sys.argv[0],run_name='__main__')
""".replace("WORK", repr(work)).replace("HEADER_SETUP",
            "from yt_dlp.utils.networking import HTTPHeaderDict as HeaderDict"
            if real_headers else "HeaderDict = dict")
        return lambda port, token: [sys.executable, "-B", "-c", code, script,
                                   "--bt-youtube-resolver", str(port), token]

    def test_real_source_entry_extracts_only_in_child_without_game_modules(self):
        from libs import youtube_resolver as resolver
        result = []
        with patch.object(resolver, "_command", self.offline_entry_command()):
            thread = threading.Thread(target=lambda: result.append(
                resolver.resolve_stream_info("https://example.invalid/page")))
            thread.start()
            thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(result), 1)
        self.assertIsNotNone(result[0])
        self.assertNotEqual(int(result[0]["http_headers"]["X-Test-Pid"]), os.getpid())

    def test_installed_header_type_survives_extraction_and_real_child_ipc(self):
        try:
            from yt_dlp.utils.networking import HTTPHeaderDict
        except ImportError:
            self.skipTest("yt-dlp HTTPHeaderDict is not installed")
        from libs import youtube_resolver as resolver
        result = []
        with patch.object(resolver, "_command", self.offline_entry_command(real_headers=True)):
            thread = threading.Thread(target=lambda: result.append(
                resolver.resolve_stream_info("https://example.invalid/page")))
            thread.start()
            thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(result), 1)
        self.assertIsNotNone(result[0])
        self.assertEqual(result[0]["url"], "https://rr.example.googlevideo.com/audio?sig=a%2Bb")
        self.assertIs(type(result[0]["http_headers"]), dict)
        self.assertEqual(result[0]["http_headers"]["User-Agent"], "Exact Agent/1.0")
        self.assertNotEqual(int(result[0]["http_headers"]["X-Test-Pid"]), os.getpid())

    def test_helper_dispatch_precedes_game_imports_and_log_redirection(self):
        script = Path(__file__).resolve().parents[1] / "beyond_tournament.py"
        worker = Mock(return_value=7)
        forbidden = {name: None for name in ("pygame", "cyal", "libs.vfs", "libs.crash_reporting",
                                              "libs.instance_manager", "libs.yt_dlp_deps")}
        forbidden["libs.youtube_resolver"] = SimpleNamespace(worker_main=worker)
        with patch.object(sys, "argv", [str(script), "--bt-youtube-resolver", "1234", "token"]), \
             patch.dict(sys.modules, forbidden), \
             patch("os.dup2", side_effect=AssertionError("helper redirected game log")):
            with self.assertRaises(SystemExit) as exited:
                runpy.run_path(str(script), run_name="__main__")
        self.assertEqual(exited.exception.code, 7)
        worker.assert_called_once_with(["1234", "token"])

    def test_build_does_not_redirect_helpers_before_python_dispatch(self):
        batch = (Path(__file__).resolve().parents[1] / "build.bat").read_text(encoding="utf-8")
        self.assertNotIn("--windows-force-stdout", batch)
        self.assertNotIn("--windows-force-stderr", batch)
        self.assertIn("--windows-disable-console", batch)
        self.assertIn("--nofollow-import-to=yt_dlp", batch)

    def test_normal_source_start_does_not_redirect_output(self):
        import beyond_tournament as entry
        with patch.object(sys, "frozen", False, create=True), \
             patch("builtins.open", side_effect=AssertionError("source log write")):
            self.assertFalse(entry._configure_compiled_output())

    def test_normal_compiled_game_keeps_both_streams_in_one_log(self):
        import beyond_tournament as entry
        output = Mock()
        output.fileno.return_value = 123
        with patch.object(sys, "frozen", True, create=True), \
             patch.object(sys, "executable", os.path.abspath("Beyond Tournament.exe")), \
             patch.object(sys, "stdout"), patch.object(sys, "stderr"), \
             patch("builtins.open", return_value=output) as opened, patch("os.dup2") as dup:
            self.assertTrue(entry._configure_compiled_output())
            self.assertIs(sys.stdout, output)
            self.assertIs(sys.stderr, output)
        self.assertEqual(Path(opened.call_args.args[0]).name, "Beyond_Tournament.log")
        self.assertEqual(dup.call_count, 2)


if __name__ == "__main__":
    unittest.main()
