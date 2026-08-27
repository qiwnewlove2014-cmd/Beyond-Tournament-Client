"""Device/file/network-free tests for temporary frame hitch instrumentation."""

import os
import threading
import unittest
from unittest.mock import patch

from libs.audio_diagnostics import AudioDiagnostics


class FakeClock:
    def __init__(self):
        self.wall = self.cpu = 0.0

    def advance(self, seconds, cpu=0.0):
        self.wall += seconds
        self.cpu += cpu


class AudioDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.lines = []
        self.probe = AudioDiagnostics(
            enabled=True,
            wall_clock=lambda: self.clock.wall,
            cpu_clock=lambda: self.clock.cpu,
            sink=self.lines.append,
        )

    def test_fast_first_play_event_and_return_values(self):
        @self.probe.measured("jukebox.start", trigger=True)
        def play(value):
            self.clock.advance(.002, .001)
            return value

        @self.probe.frame
        def frame():
            return play(42)

        self.assertEqual(frame(), 42)
        self.assertEqual(len(self.lines), 1)
        self.assertIn("reason=event events=jukebox.start", self.lines[0])
        self.assertIn("work_ms=2.00", self.lines[0])
        self.assertIn("cpu_ms=1.00", self.lines[0])
        self.assertIn("jukebox.start=2.00/2.00x1", self.lines[0])

    def test_normal_pacing_is_not_slow(self):
        @self.probe.frame
        def frame(event=False):
            if event:
                self.probe.event("piano.start")
            self.clock.advance(.002, .001)
            return self.probe.pace(lambda fps: (self.clock.advance(.048), 50)[1], 20)

        self.assertEqual(frame(True), 50)
        for _ in range(20):
            frame()
        self.assertEqual(len(self.lines), 1)
        self.assertIn("work_ms=2.00", self.lines[0])
        self.assertIn("pace_ms=48.00", self.lines[0])

    def test_actual_slow_call_and_inclusive_nesting(self):
        @self.probe.frame
        def frame():
            self.probe.event("piano.start")
            with self.probe.span("outer"):
                self.probe.call("decode", self.clock.advance, .020, .010)
                self.probe.call("decode", self.clock.advance, .030, .020)

        frame()
        line = self.lines[0]
        self.assertIn("reason=slow", line)
        self.assertIn("work_ms=50.00", line)
        self.assertIn("decode=50.00/30.00x2", line)
        self.assertIn("outer=50.00/50.00x1", line)
        self.assertIn("nested, do not add", line)

    def test_worst_suppressed_frame_flushes_on_idle_after_window(self):
        @self.probe.frame
        def frame(duration=0, event=False):
            if event:
                self.probe.event("relay.first_play")
            self.probe.call("work", self.clock.advance, duration)

        frame(event=True)
        frame(.060)
        frame(.025)
        self.assertEqual(len(self.lines), 1)
        self.assertEqual(self.probe._pending["work"], .060)
        self.clock.advance(16)
        frame()
        self.assertEqual(len(self.lines), 2)
        self.assertIn("work_ms=60.00", self.lines[1])
        self.assertIn("suppressed=1", self.lines[1])
        self.assertIn("frame=2 capture_ms=0.00 age_ms=16085.00", self.lines[1])
        self.assertIsNone(self.probe._pending)

    def test_rate_limit_includes_events_and_preserves_event_names(self):
        @self.probe.frame
        def frame(label):
            self.probe.event(label)

        frame("first")
        self.clock.advance(.1)
        frame("second")
        self.clock.advance(.1)
        frame("third")
        self.assertEqual(len(self.lines), 1)
        self.clock.advance(.3)
        frame("fourth")
        self.assertEqual(len(self.lines), 2)
        self.assertIn("second,third,fourth", self.lines[1])

    def test_gap_and_excess_pacing_are_reported(self):
        @self.probe.frame
        def frame(event=False, sleep=.010):
            if event:
                self.probe.event("piano.start")
            self.probe.pace(lambda fps: self.clock.advance(sleep), 100)

        frame(True)
        self.clock.advance(.5)
        frame(sleep=.040)
        self.assertEqual(len(self.lines), 2)
        self.assertIn("work_ms=0.00", self.lines[-1])
        self.assertIn("total_over_ms=30.00", self.lines[-1])
        self.assertIn("gap_over_ms=500.00", self.lines[-1])

    def test_no_window_means_no_slow_reports(self):
        @self.probe.frame
        def frame():
            self.probe.call("unrelated", self.clock.advance, 1)

        frame()
        frame()
        self.assertEqual(self.lines, [])

    def test_pacing_oversleep_is_slow_even_with_zero_work(self):
        @self.probe.frame
        def frame(event=False, sleep=.010):
            if event:
                self.probe.event("piano.start")
            self.probe.pace(lambda fps: self.clock.advance(sleep), 100)

        frame(True)
        for _ in range(50):
            frame()
        self.assertEqual(len(self.lines), 1)
        frame(sleep=.050)
        self.assertEqual(len(self.lines), 2)
        self.assertIn("work_ms=0.00", self.lines[-1])
        self.assertIn("total_over_ms=40.00", self.lines[-1])
        self.assertIn("gap_over_ms=0.00", self.lines[-1])

    def test_worker_spans_events_and_counts_are_ignored(self):
        @self.probe.frame
        def frame():
            self.probe.event("piano.start")
            def worker():
                self.probe.call("worker.decode", lambda: None)
                self.probe.event("worker.event")
                self.probe.count("worker.count")
                with self.probe.span("worker.span"):
                    pass
                self.probe.frame(lambda: self.probe.event("worker.frame"))()
            thread = threading.Thread(target=worker)
            thread.start()
            thread.join(1)
            self.assertFalse(thread.is_alive())

        frame()
        self.assertNotIn("worker", self.lines[0])

    def test_exceptions_propagate_and_frame_is_cleared(self):
        error = ValueError("private details must not be logged")

        @self.probe.frame
        def frame():
            self.probe.event("piano.start")
            with self.probe.span("outer"):
                def fail():
                    self.clock.advance(.020)
                    raise error
                self.probe.call("failure", fail)

        with self.assertRaises(ValueError) as caught:
            frame()
        self.assertIs(caught.exception, error)
        self.assertIsNone(self.probe._active())
        self.assertIn("failure=20.00/20.00x1", self.lines[0])
        self.assertNotIn("private", self.lines[0])

    def test_disabled_and_no_active_frame_do_not_read_clocks(self):
        def unexpected():
            raise AssertionError("clock must not be read")
        for enabled in (False, True):
            probe = AudioDiagnostics(enabled=enabled, wall_clock=unexpected,
                                     cpu_clock=unexpected, sink=unexpected)
            probe.event("ignored")
            probe.count("ignored")
            with probe.span("ignored"):
                self.assertEqual(probe.call("ignored", lambda: 7), 7)
            self.assertEqual(probe.measured("ignored", trigger=True)(lambda: 8)(), 8)
            self.assertEqual(probe.pace(lambda fps: fps, 60), 60)
            if not enabled:
                self.assertEqual(probe.frame(lambda: 9)(), 9)
        with patch.dict(os.environ, {"BT_AUDIO_DIAGNOSTICS": "0"}):
            self.assertFalse(AudioDiagnostics().enabled)

    def test_bounds_counters_and_unsafe_labels(self):
        class Unsafe:
            def __str__(self):
                raise AssertionError("must not stringify")

        @self.probe.frame
        def frame():
            self.probe.event("piano.start")
            self.probe.count("notes", 10 ** 100)
            for label in ("C:/private/name", "username secret", "bad\nline", Unsafe(), "a" * 49):
                self.probe.count(label)
                self.probe.event(label)
                self.probe.call(label, lambda: None)
            for index in range(1000):
                label = "sample." + str(index)
                self.probe.count(label)
                self.probe.call(label, self.clock.advance, .000001)
            record = self.probe._active()
            self.assertEqual(len(record.labels), 64)
            self.assertGreater(record.overflow, 0)

        frame()
        self.assertLessEqual(len(self.lines[0]), 2048)
        self.assertIn("notes=1000000000", self.lines[0])
        self.assertNotIn("private", self.lines[0])
        self.assertNotIn("\n", self.lines[0])

    def test_default_is_off_and_only_explicit_one_enables_diagnostics(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(AudioDiagnostics().enabled)
        for value in ("0", "", "false", "true", "invalid", "2", "1"):
            with self.subTest(value=value), patch.dict(os.environ, {"BT_AUDIO_DIAGNOSTICS": value}):
                self.assertEqual(AudioDiagnostics().enabled, value == "1")
                self.assertFalse(AudioDiagnostics(enabled=False).enabled)
                self.assertTrue(AudioDiagnostics(enabled=True).enabled)

    def test_default_off_never_collects_or_writes_main_or_worker_timings(self):
        def unexpected(*args):
            self.fail("disabled diagnostics must not read clocks or write logs")
        with patch.dict(os.environ, {}, clear=True):
            probe = AudioDiagnostics(wall_clock=unexpected, cpu_clock=unexpected, sink=unexpected)
        @probe.frame
        def frame():
            probe.event("jukebox.start")
            probe.count("relay.frames", 4)
            with probe.span("audio.total"):
                self.assertEqual(probe.call("audio.body", lambda: 7), 7)
            self.assertEqual(probe.worker_call("direct.resolve", lambda: 8), 8)
            self.assertEqual(probe.measured("map.parse", trigger=True)(lambda: 9)(), 9)
            return probe.pace(lambda fps: fps, 60)
        self.assertEqual(frame(), 60)
        self.assertIsNone(probe._owner)
        self.assertIsNone(probe._pending)
        self.assertEqual(probe._frame_sequence, 0)
        self.assertTrue(probe._worker_samples.empty())

    def test_sink_failure_does_not_replace_gameplay_exception(self):
        def sink(line):
            raise RuntimeError("sink failed")
        self.probe._sink = sink

        @self.probe.frame
        def frame():
            self.probe.event("piano.start")
            raise ValueError("original")

        with self.assertRaisesRegex(ValueError, "original"):
            frame()
        self.assertIsNotNone(self.probe._pending)
        self.probe._sink = self.lines.append
        self.clock.advance(1)
        self.probe.frame(lambda: None)()
        self.assertEqual(len(self.lines), 1)

    def test_first_play_survives_saturated_pending_event_list(self):
        @self.probe.frame
        def frame(labels, duration=0):
            for label in labels:
                self.probe.event(label)
            self.clock.advance(duration)
            self.probe.pace(lambda fps: self.clock.advance(.010), 100)

        frame(["initial"])
        frame(["event." + str(index) for index in range(20)], .050)
        frame(["relay.first_play"])
        self.assertIn("relay.first_play", self.probe._pending["events"])
        self.assertEqual(len(self.probe._pending["events"]), 8)
        while self.clock.wall < .6:
            frame([])
        self.assertIn("relay.first_play", self.lines[-1])
        self.assertIn("work_ms=50.00", self.lines[-1])

    def test_false_sink_result_retains_report_and_limits_retry_rate(self):
        attempts = []
        def full_sink(line):
            attempts.append(line)
            return False
        self.probe._sink = full_sink

        @self.probe.frame
        def frame(event=False):
            if event:
                self.probe.event("relay.first_play")

        frame(True)
        for _ in range(10):
            self.clock.advance(.01)
            frame()
        self.assertEqual(len(attempts), 1)
        self.assertIsNotNone(self.probe._pending)
        self.probe._sink = self.lines.append
        self.clock.advance(.5)
        frame()
        self.assertEqual(len(self.lines), 1)
        self.assertIsNone(self.probe._pending)

    def test_timing_failures_do_not_change_callable_results_or_exceptions(self):
        def broken_clock():
            raise RuntimeError("diagnostic clock failed")
        broken = AudioDiagnostics(enabled=True, wall_clock=broken_clock,
                                  cpu_clock=broken_clock, sink=self.lines.append)
        self.assertEqual(broken.frame(lambda: 42)(), 42)
        original = ValueError("game error")

        @self.probe.frame
        def frame():
            for operation in ("call", "span", "pace"):
                for failure_at_start in (True, False):
                    with self.subTest(operation=operation, start=failure_at_start):
                        calls = []
                        original_clock = self.probe._wall
                        def action(*args):
                            calls.append(1)
                            self.probe._wall = broken_clock
                            raise original
                        if failure_at_start:
                            self.probe._wall = broken_clock
                        try:
                            with self.assertRaises(ValueError) as caught:
                                if operation == "call":
                                    self.probe.call("failure", action)
                                elif operation == "span":
                                    with self.probe.span("failure"):
                                        action()
                                else:
                                    self.probe.pace(action, 60)
                            self.assertIs(caught.exception, original)
                            self.assertEqual(calls, [1])
                        finally:
                            self.probe._wall = original_clock
            with patch.object(self.probe, "_duration", side_effect=RuntimeError("timing")):
                self.assertEqual(self.probe.call("bookkeeping", lambda: 7), 7)
                with self.probe.span("bookkeeping"):
                    pass

        frame()

    def test_recursive_frame_keeps_outer_record(self):
        @self.probe.frame
        def inner():
            self.probe.call("nested", self.clock.advance, .003)

        @self.probe.frame
        def outer():
            self.probe.event("piano.start")
            record = self.probe._active()
            inner()
            self.assertIs(self.probe._active(), record)

        outer()
        self.assertEqual(len(self.lines), 1)
        self.assertIn("nested=3.00/3.00x1", self.lines[0])


if __name__ == "__main__":
    unittest.main()
