"""The evidence a reverb report needs: counts that used to be thrown away.

A reverb that comes back dry or changed after a few map changes left no trace:
the failure was swallowed. These tests pin the two halves of the replacement --
the counters that survive a dropped error, and the one line per rebuild that a
tester's log is read against.

Nothing here opens a device: an ``AudioHealth`` is constructed with its own
sink and clock, which is also what makes the map-load line testable without a
pool.
"""

import unittest
from types import SimpleNamespace
from unittest import mock

from libs.audio_health import AudioHealth


class TheLoadLineTests(unittest.TestCase):
    def make(self, **kwargs):
        lines = []
        kwargs.setdefault("verbose", True)
        kwargs.setdefault("sink", lines.append)
        return AudioHealth(**kwargs), lines

    def test_the_line_names_the_rooms_the_pool_and_the_faults(self):
        health, lines = self.make()
        line = health.map_load(name="main", rooms=4, dry_rooms=1,
                               slots_used=37, slots_total=64, leases=6)
        self.assertIn("load=1", line)
        self.assertIn("map=main", line)
        self.assertIn("rooms=4", line)
        self.assertIn("dry=1", line)
        self.assertIn("slots=37/64", line)
        self.assertIn("leases=6", line)
        self.assertIn("send_failures=+0", line)
        self.assertEqual(lines, [line])

    def test_fault_counts_are_a_delta_between_loads(self):
        health, _lines = self.make()
        health.map_load(name="a")
        health.note_send_failure("unbound", "InvalidNameError")
        health.note_send_failure("unbound", "InvalidNameError")
        self.assertIn("send_failures=+2", health.map_load(name="b"))
        self.assertIn("send_failures=+0", health.map_load(name="c"))
        self.assertEqual(health.total("send_failure"), 2)

    def test_the_load_line_is_off_unless_asked_for_but_a_fault_still_speaks(self):
        lines = []
        health = AudioHealth(verbose=False, sink=lines.append)
        self.assertEqual(health.map_load(name="main", rooms=1), "")
        self.assertEqual(lines, [])
        health.note_send_failure("group", "InvalidNameError")
        self.assertEqual(len(lines), 1)
        self.assertIn("send_failure", lines[0])
        self.assertIn("phase=group", lines[0])

    def test_one_line_per_fault_class_and_then_only_at_milestones(self):
        lines = []
        health = AudioHealth(verbose=True, sink=lines.append)
        for _ in range(9):
            health.note_swallowed("entity.sync_reverb", "InvalidNameError")
        self.assertEqual(len(lines), 1)
        self.assertIn("occurrences=1", lines[0])
        health.note_swallowed("entity.sync_reverb", "InvalidNameError")
        self.assertEqual(len(lines), 2)
        self.assertIn("occurrences=10", lines[1])
        health.note_swallowed("entity.sync_reverb", "InvalidNameError")
        self.assertEqual(len(lines), 2)
        self.assertEqual(health.total("swallowed"), 11)

    def test_a_map_name_cannot_inject_a_log_field(self):
        health, _lines = self.make()
        line = health.map_load(name='main"\r\n[AUDIO HEALTH] fake', rooms=0)
        self.assertNotIn("\n", line)
        self.assertNotIn("\r", line)
        self.assertNotIn('"', line)

    def test_the_ring_is_bounded_and_the_dump_is_oldest_first(self):
        lines = []
        health = AudioHealth(verbose=True, sink=lines.append, max_entries=16)
        for index in range(40):
            health.note_swallowed("where", f"Error{index}")
        entries = health.dump_lines(limit=100)
        self.assertEqual(len(entries), 16)
        self.assertIn("Error24", entries[0])
        self.assertIn("Error39", entries[-1])

    def test_a_sink_that_raises_never_reaches_the_caller(self):
        def sink(_line):
            raise RuntimeError("no disk")

        health = AudioHealth(verbose=True, sink=sink)
        health.note_loop_fault("InvalidOperationError")
        self.assertIn("load=1", health.map_load(name="main"))

    def test_the_snapshot_names_what_the_counters_hold(self):
        health = AudioHealth(verbose=False, sink=lambda line: None)
        health.note_send_failure("unbound", "InvalidNameError")
        self.assertIn("send_failure:unbound:InvalidNameError=1",
                      health.snapshot_line())

    def test_a_bad_entry_limit_is_refused(self):
        with self.assertRaises(ValueError):
            AudioHealth(max_entries=2)

    def test_the_environment_switch_is_what_turns_the_load_line_on(self):
        with mock.patch.dict("os.environ", {"BT_AUDIO_HEALTH": "1"}):
            self.assertTrue(AudioHealth(sink=lambda line: None).verbose)
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertFalse(AudioHealth(sink=lambda line: None).verbose)


class TheReloadSnapshotTests(unittest.TestCase):
    """What a rebuild hands the ledger: rooms, dry rooms, the pool and leases."""

    def make(self, gp, audio):
        from libs.event_handeler import EventHandeler

        lines = []
        health = AudioHealth(verbose=True, sink=lines.append)
        with mock.patch("libs.event_handeler.audio_health", health):
            EventHandeler._record_audio_health(gp, audio)
        return lines

    def test_the_line_counts_the_rooms_the_dry_ones_and_the_pool(self):
        zones = [SimpleNamespace(reverb=object()), SimpleNamespace(reverb=None)]
        gp = SimpleNamespace(map_name="mor_lam",
                             map=SimpleNamespace(reverb_list=zones))
        audio = SimpleNamespace(
            slot_report=lambda: {"used": 37, "total": 64, "shared": 6})

        lines = self.make(gp, audio)

        self.assertEqual(len(lines), 1)
        self.assertIn("map=mor_lam", lines[0])
        self.assertIn("rooms=2", lines[0])
        self.assertIn("dry=1", lines[0])
        self.assertIn("slots=37/64", lines[0])
        self.assertIn("leases=6", lines[0])

    def test_a_client_with_no_map_or_pool_writes_nothing_and_does_not_raise(self):
        self.assertEqual(self.make(SimpleNamespace(), None), [])


if __name__ == "__main__":
    unittest.main()
