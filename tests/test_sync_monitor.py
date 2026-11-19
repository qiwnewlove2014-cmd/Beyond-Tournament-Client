"""Phase 1 sync telemetry is read-only.

These tests pin the arithmetic and the absence of side effects; no test here
opens an audio device or asks a shipping path to change a buffer.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from libs import sync_monitor


class ClockSampleTests(unittest.TestCase):
    def test_a_four_timestamp_exchange_is_reduced_to_rtt_offset_and_uncertainty(self):
        monitor = sync_monitor.SyncMonitor()
        sample = monitor.clock_sample(
            1000.0, 1010.0, 1012.0, 1030.0, now=5.0)
        self.assertEqual(sample["rtt_ms"], 30.0)
        self.assertEqual(sample["server_turnaround_ms"], 2.0)
        self.assertEqual(sample["uncertainty_ms"], 14.0)
        self.assertEqual(sample["offset_ms"], -4.0)

    def test_the_best_sample_is_the_one_with_the_smallest_rtt(self):
        monitor = sync_monitor.SyncMonitor()
        monitor.clock_sample(1000.0, 1010.0, 1012.0, 1060.0, now=1.0)
        monitor.clock_sample(2000.0, 2010.0, 2011.0, 2021.0, now=2.0)
        snapshot = monitor.clock_snapshot(now=3.0)
        self.assertEqual(snapshot["samples"], 2)
        self.assertEqual(snapshot["best"]["rtt_ms"], 21.0)
        self.assertEqual(snapshot["last"]["rtt_ms"], 21.0)
        self.assertEqual(snapshot["last"]["age_ms"], 1000.0)

    def test_a_clock_that_went_backwards_is_not_a_sample(self):
        monitor = sync_monitor.SyncMonitor()
        self.assertIsNone(monitor.clock_sample(100.0, 50.0, 40.0, 90.0))
        self.assertIsNone(monitor.clock_sample(100.0, 110.0, 120.0, None))
        self.assertEqual(monitor.clock_snapshot()["samples"], 0)

    def test_report_names_the_clock(self):
        monitor = sync_monitor.SyncMonitor()
        monitor.clock_sample(1000.0, 1010.0, 1012.0, 1030.0, now=1.0)
        self.assertIn("clock: rtt=30.0ms", monitor.report(now=2.0))


class PathTelemetryTests(unittest.TestCase):
    def test_drift_and_jitter_are_tracked_with_emas_without_touching_audio(self):
        monitor = sync_monitor.SyncMonitor()
        monitor.record(
            "music", expected_ms=1000.0, actual_ms=1040.0,
            jitter_ms=8.0, buffer_ms=80.0, now=100.0)
        monitor.record(
            "music", expected_ms=1200.0, actual_ms=1230.0,
            jitter_ms=12.0, buffer_ms=100.0, now=101.0)
        stats = monitor.snapshot("music", now=102.0)
        self.assertEqual(stats["samples"], 2)
        self.assertEqual(stats["drift_ms"], 30.0)
        self.assertEqual(stats["drift_ema_ms"], 37.5)
        self.assertEqual(stats["jitter_ms"], 12.0)
        self.assertEqual(stats["jitter_ema_ms"], 9.0)
        self.assertEqual(stats["buffer_ms"], 100.0)
        self.assertEqual(stats["buffer_max_ms"], 100.0)
        self.assertEqual(stats["age_ms"], 1000.0)

    def test_drift_is_the_playhead_gap_not_the_playout_queue(self):
        monitor = sync_monitor.SyncMonitor()
        monitor.record(
            "jam", expected_ms=1000.0, actual_ms=1025.0,
            buffer_ms=240.0, now=1.0)
        stats = monitor.snapshot("jam", now=2.0)
        self.assertEqual(stats["drift_ms"], 25.0)
        self.assertEqual(stats["drift_ema_ms"], 25.0)
        self.assertEqual(stats["buffer_ms"], 240.0)

    def test_a_later_sample_without_playheads_clears_stale_drift_and_extras(self):
        monitor = sync_monitor.SyncMonitor()
        monitor.record(
            "jam", expected_ms=1000.0, actual_ms=1040.0,
            buffer_ms=240.0, aim="stamp", marker="old", now=1.0)
        monitor.record("jam", buffer_ms=120.0, aim="clock", now=2.0)
        stats = monitor.snapshot("jam", now=3.0)
        self.assertIsNone(stats["expected_ms"])
        self.assertIsNone(stats["actual_ms"])
        self.assertIsNone(stats["drift_ms"])
        self.assertIsNone(stats["drift_ema_ms"])
        self.assertEqual(stats["buffer_ms"], 120.0)
        self.assertEqual(stats["extra"], {"aim": "clock"})

    def test_paths_and_counters_do_not_mix(self):
        monitor = sync_monitor.SyncMonitor()
        monitor.record("voice", buffer_ms=20.0, underruns=1, now=1.0)
        monitor.record("pa", buffer_ms=160.0, late=2, dropped=3, now=1.0)
        voice = monitor.snapshot("voice", now=2.0)
        pa = monitor.snapshot("pa", now=2.0)
        self.assertEqual(voice["underruns"], 1)
        self.assertEqual(voice["late"], 0)
        self.assertEqual(pa["buffer_ms"], 160.0)
        self.assertEqual(pa["late"], 2)
        self.assertEqual(pa["dropped"], 3)
        self.assertIsNone(monitor.snapshot("cinema", now=2.0))

    def test_only_scalar_extras_are_kept(self):
        monitor = sync_monitor.SyncMonitor()
        monitor.record(
            "jam", buffer_ms=120.0, aim="stamp", marker="yes",
            nested={"not": "printable"}, now=1.0)
        extra = monitor.snapshot("jam", now=2.0)["extra"]
        self.assertEqual(extra, {"aim": "stamp", "marker": "yes"})

    def test_a_game_without_a_monitor_is_a_no_op(self):
        class Game:
            pass

        game = Game()
        self.assertIsNone(sync_monitor.record(game, "voice", buffer_ms=20.0))
        self.assertIsNone(
            sync_monitor.clock_sample(game, 1.0, 2.0, 3.0, 4.0))
        game.sync_monitor = sync_monitor.SyncMonitor()
        sync_monitor.record(game, "voice", buffer_ms=20.0)
        self.assertEqual(
            game.sync_monitor.snapshot("voice")["buffer_ms"], 20.0)

    def test_report_lists_paths_in_a_stable_order(self):
        monitor = sync_monitor.SyncMonitor()
        monitor.record("jam", buffer_ms=40.0, now=1.0)
        monitor.record("voice", buffer_ms=20.0, now=1.0)
        lines = monitor.report(now=2.0).splitlines()
        self.assertTrue(lines[0].startswith("clock:"))
        self.assertTrue(lines[1].startswith("voice:"))
        self.assertTrue(lines[2].startswith("jam:"))

    def test_the_existing_ping_feeds_the_clock_sample(self):
        # The probe rides the ping the voice path already sends; this pins the
        # seam rather than the monitor's arithmetic (which the class tests own).
        import time
        from types import SimpleNamespace
        from libs import event_handeler

        monitor = sync_monitor.SyncMonitor()
        sent = time.time()
        game = SimpleNamespace(sync_monitor=monitor)
        gameplay = SimpleNamespace(
            last_ping_time=sent, _auto_ping_inflight=True, pingging=True)
        handler = event_handeler.EventHandeler.__new__(event_handeler.EventHandeler)
        handler.game = game
        handler.gameplay = gameplay
        handler.ping({
            "received_at": sent * 1000.0 + 10.0,
            "server_time": sent * 1000.0 + 12.0,
        })
        sample = monitor.clock_snapshot()["last"]
        self.assertIsNotNone(sample)
        self.assertGreaterEqual(sample["rtt_ms"], 0.0)
        self.assertFalse(gameplay.pingging)


if __name__ == "__main__":
    unittest.main()
