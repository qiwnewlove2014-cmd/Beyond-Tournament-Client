"""Direct startup diagnostics: no real media, devices, subprocesses or network."""

import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from libs.audio_diagnostics import AudioDiagnostics, probe


class WorkerDiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.now = self.cpu = 100.0
        self.lines = []
        fresh = AudioDiagnostics(enabled=True, wall_clock=lambda: self.now,
                                 cpu_clock=lambda: self.cpu, sink=self.lines.append)
        state = patch.dict(probe.__dict__, fresh.__dict__, clear=True)
        state.start()
        self.addCleanup(state.stop)
        probe.frame(lambda: None)()  # Establish the common timestamp origin.

    def advance(self, wall, cpu=0):
        self.now += wall
        self.cpu += cpu

    def worker_lines(self):
        return [line for line in self.lines if line.startswith("[AudioDiagWorker]")]

    def in_worker(self, function):
        results, errors = [], []
        def run():
            try:
                results.append(function())
            except BaseException as error:
                errors.append(error)
        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        thread.join(2)
        self.assertFalse(thread.is_alive())
        if errors:
            raise errors[0]
        return results[0]

    def flush(self):
        probe.frame(lambda: None)()

    def test_worker_wall_cpu_ranges_are_separate_from_main_frame(self):
        def work():
            self.advance(.2, .03)
            return 42
        self.assertEqual(self.in_worker(lambda: probe.worker_call("direct.resolve", work)), 42)
        self.assertEqual(self.lines, [])  # Never call the sink on the worker.
        self.flush()
        self.assertIn("direct.resolve=200.00/200.00/30.00x1@0.00..200.00", self.worker_lines()[0])
        self.assertIn("separate_from_frame=true", self.worker_lines()[0])
        main = [line for line in self.lines if line.startswith("[AudioDiag]")]
        self.assertIn("work_ms=0.00", main[0])
        self.assertIn("cpu_ms=0.00", main[0])
        self.assertNotIn("direct.resolve=", main[0])

    def test_failed_attempt_keeps_original_exception_without_details(self):
        error = ValueError("private signed URL and credentials")
        def fail():
            self.advance(.01)
            raise error
        with self.assertRaises(ValueError) as caught:
            probe.worker_call("direct.launch", fail)
        self.assertIs(caught.exception, error)
        self.flush()
        self.assertIn("direct.launch=10.00", self.worker_lines()[0])
        self.assertNotIn("private", " ".join(self.lines))

    def test_disabled_invalid_labels_and_clock_failures_call_once(self):
        action = Mock(return_value=7)
        def broken_clock():
            raise RuntimeError("diagnostic failure")
        with patch.object(probe, "_wall", broken_clock):
            self.assertEqual(probe.worker_call("direct.resolve", action), 7)
        action.assert_called_once_with()
        action.reset_mock()
        with patch.object(probe, "enabled", False), patch.object(probe, "_wall", broken_clock):
            self.assertEqual(probe.worker_call("direct.resolve", action, 1), 7)
        action.assert_called_once_with(1)
        with patch.object(probe, "_wall", broken_clock):
            for label in ("private.user", "direct.resolve\n", None, [], "a" * 300):
                self.assertEqual(probe.worker_call(label, lambda: 9), 9)
        self.assertTrue(probe._worker_samples.empty())

    def test_end_clock_failure_preserves_return_and_exception(self):
        original = ValueError("gameplay error")
        for raises in (False, True):
            calls = []
            with patch.object(probe, "_wall", side_effect=[100, RuntimeError("clock")]):
                def action():
                    calls.append(1)
                    if raises:
                        raise original
                    return 8
                if raises:
                    with self.assertRaises(ValueError) as caught:
                        probe.worker_call("direct.resolve", action)
                    self.assertIs(caught.exception, original)
                else:
                    self.assertEqual(probe.worker_call("direct.resolve", action), 8)
            self.assertEqual(calls, [1])
        self.assertTrue(probe._worker_samples.empty())

    def test_queue_full_is_bounded_and_does_not_block_actions(self):
        for _ in range(1000):
            self.assertEqual(probe.worker_call("direct.queue", lambda: 9), 9)
        self.assertEqual(probe._worker_samples.qsize(), 64)
        self.flush()
        self.assertTrue(probe._worker_samples.empty())
        self.assertEqual(probe._worker_samples.unfinished_tasks, 0)
        self.assertIn("x64@", self.worker_lines()[0])
        self.assertIn("dropped=936", self.worker_lines()[0])
        self.assertLessEqual(len(self.worker_lines()[0]), 2048)

    def test_workers_share_bounded_queue_without_losing_call_results(self):
        threads = [threading.Thread(target=lambda: [probe.worker_call("direct.queue", lambda: 3)
                                                    for _ in range(100)]) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(2)
            self.assertFalse(thread.is_alive())
        self.assertEqual(probe._worker_samples.qsize(), 64)
        self.flush()
        self.assertEqual(len(self.worker_lines()), 1)

    def test_rate_limit_retries_full_sink_and_aggregates_all_streams(self):
        attempts = []
        def full_sink(line):
            attempts.append(line)
            return False
        with patch.object(probe, "_sink", full_sink):
            probe.worker_call("direct.first_play", self.advance, .01)
            self.flush()
            probe.worker_call("direct.first_play", self.advance, .02)
            self.flush()
        self.assertEqual(len([line for line in attempts if line.startswith("[AudioDiagWorker]")]), 1)
        self.assertEqual(len(probe._worker_pending), 1)
        self.advance(1)
        self.flush()
        self.assertIn("direct.first_play=30.00/20.00/0.00x2@", self.worker_lines()[0])
        self.assertEqual(probe._worker_pending, {})

    def test_late_worker_completion_rearms_window_after_slow_resolve(self):
        self.advance(30)
        probe.worker_call("direct.resolve", self.advance, .02)
        self.flush()
        self.assertGreater(probe._window_until, self.now + 14)
        self.advance(1)
        @probe.frame
        def main():
            probe.call("gp.player", self.advance, .08)
        main()
        self.assertIn("gp.player=80.00", self.lines[-1])

    def test_personal_music_bot_bypasses_worker_diagnostics(self):
        from libs.music_bot import AudioStreamer
        stream = AudioStreamer.__new__(AudioStreamer)
        stream.bot = object()
        action = Mock(return_value=13)
        with patch.object(probe, "worker_call", side_effect=AssertionError("not a jukebox")):
            self.assertEqual(stream._diagnostic_startup_call("direct.resolve", action, 2, key=4), 13)
        action.assert_called_once_with(2, key=4)

    def test_real_direct_run_keeps_startup_order_and_measures_native_stages(self):
        from libs.music_bot import AudioStreamer
        stream = AudioStreamer(SimpleNamespace(), "https://youtube.com/watch?v=private", object(),
                               bot=None, spatial_pair=(object(), object(), 8, 40))
        order = []
        def step(name, duration, result=None):
            def action(*args, **kwargs):
                order.append(name)
                self.advance(duration)
                return result
            return action
        process = SimpleNamespace(poll=lambda: None, stdout=SimpleNamespace(
            read=step("read", .02, b"\0" * stream.BUFFER_SIZE)))
        stream._init_buffer_pool = step("buffers", .03)
        stream._queue_local = step("queue", .002, True)
        stream._update_spatial_gain = step("spatial", .025)
        def first_play():
            order.append("play")
            self.advance(.04)
            stream.running = False  # End this fake run without a streaming loop.
        stream._play_all = first_play
        stream._route_aligned_network_frame = step("route", 0)
        stream._cleanup = step("cleanup", 0)
        with patch("libs.music_bot.FFMPEG_PATH", "never-executed"), \
             patch("libs.music_bot.YouTubeSearcher.get_stream_info", side_effect=step(
                 "resolve", .025, {"url": "https://example.invalid/private", "http_headers": {}})), \
             patch("libs.music_bot.subprocess.Popen", side_effect=step("launch", .01, process)) as popen:
            self.in_worker(stream.run)
        self.assertEqual(order, ["resolve", "buffers", "launch"] + ["read", "queue"] * 5
                         + ["spatial", "play", "route", "cleanup"])
        self.assertTrue(stream.ready_event.is_set())
        popen.assert_called_once()
        self.flush()
        line = self.worker_lines()[0]
        for expected in ("direct.buffers=30.00", "direct.prebuffer=110.00",
                         "direct.read=100.00/20.00/0.00x5", "direct.queue=10.00/2.00/0.00x5",
                         "direct.first_play=40.00", "direct.spatial=25.00"):
            self.assertIn(expected, line)
        self.assertNotIn("private", line)
        self.assertNotIn("example.invalid", line)


if __name__ == "__main__":
    unittest.main()
