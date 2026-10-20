"""The watchdog witnesses a freeze; it never kills or relaunches the Client.

A compiled Client used to relaunch itself after a guessed 15 s deadlock. On a slow
first load (instrument samples out of ``sounds.dat``) that guess was wrong, and the
cost was the whole session: the replacement is refused by the single-instance guard
or starts at the login menu, while the old process exits. The dump is the deliverable
(libs/watchdog.py).
"""

import os
import tempfile
import unittest
from unittest import mock


def _watchdog():
    from libs import watchdog
    return watchdog


class WatchdogNeverActsTests(unittest.TestCase):
    def dump(self, fatal, path):
        watchdog = _watchdog()
        wd = watchdog.GameWatchdog(game=None, timeout=0.05)
        with mock.patch.object(watchdog, "FREEZE_STACK_FILE", path), \
                mock.patch.object(watchdog.logger, "log"), \
                mock.patch("libs.crash_reporting.queue_exception") as queue, \
                mock.patch("libs.crash_reporting.send_pending") as send, \
                mock.patch("subprocess.Popen") as popen, \
                mock.patch("os._exit") as exit_process:
            wd._dump_thread_stacks(20.0 if fatal else 6.0, fatal=fatal)
        return queue, send, popen, exit_process

    def test_a_long_freeze_is_reported_and_the_process_kept(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "last_freeze.txt")
            queue, send, popen, exit_process = self.dump(True, path)
            self.assertTrue(os.path.exists(path))
            with open(path, encoding="utf-8") as handle:
                self.assertIn("WATCHDOG ALARM", handle.read())
        queue.assert_called_once()
        self.assertEqual(queue.call_args.kwargs.get("severity"), "recovered")
        popen.assert_not_called()
        exit_process.assert_not_called()
        send.assert_not_called()      # no game object in this test

    def test_a_short_freeze_only_writes_the_stacks(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "last_freeze.txt")
            queue, _send, popen, exit_process = self.dump(False, path)
            self.assertTrue(os.path.exists(path))
        queue.assert_not_called()
        popen.assert_not_called()
        exit_process.assert_not_called()

    def test_a_hang_still_running_is_one_report_per_window(self):
        watchdog = _watchdog()
        wd = watchdog.GameWatchdog(game=None, timeout=0.05)
        self.assertTrue(wd._fatal_due())
        self.assertFalse(wd._fatal_due())
        wd._fatal_reported_at -= watchdog.FATAL_REPORT_INTERVAL_S
        self.assertTrue(wd._fatal_due())


if __name__ == "__main__":
    unittest.main()
