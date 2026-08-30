"""Offline tests: shutdown-safe EFX armor reporting and the log fsync guard.

A weakref finalizer that logs during interpreter shutdown must never block
on the logger's lock+fsync path — an fsync stalled behind an antivirus
scan at process exit once hung the quit until the watchdog reported a
native deadlock (fatal report, player icesound 2026-08-29).
"""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from libs import audio_manager
from libs import logger


class _FakeLogHandle:
    def __init__(self):
        self.data = ""

    def write(self, text):
        self.data += text

    def flush(self):
        pass

    def fileno(self):
        return 2


class ArmorBreachReportingTests(unittest.TestCase):
    def test_live_breach_uses_the_normal_logger(self):
        with mock.patch("libs.logger.log") as log_mock, \
                mock.patch("sys.stderr") as stderr:
            audio_manager._report_efx_armor_breach(
                12345, "audio_manager.py:10 (gen_filter)", "Filter", False)
        log_mock.assert_called_once()
        message = log_mock.call_args.args[0]
        self.assertIn("12345", message)
        self.assertIn("audio_manager.py:10 (gen_filter)", message)
        self.assertIn("INCREF armor", message)
        stderr.write.assert_not_called()

    def test_finalizing_breach_writes_stderr_without_the_logger(self):
        with mock.patch("libs.logger.log") as log_mock, \
                mock.patch("sys.stderr") as stderr:
            audio_manager._report_efx_armor_breach(
                12345, "audio_manager.py:10 (gen_filter)", "Filter", True)
        log_mock.assert_not_called()
        stderr.write.assert_called_once()
        message = stderr.write.call_args.args[0]
        self.assertIn("12345", message)
        self.assertIn("audio_manager.py:10 (gen_filter)", message)
        self.assertIn("shutdown", message)

    def test_breach_reporting_never_raises_even_if_everything_fails(self):
        with mock.patch("libs.logger.log", side_effect=RuntimeError("boom")), \
                mock.patch("sys.stderr") as stderr:
            stderr.write.side_effect = OSError("stream gone")
            # Both paths must swallow their own failures silently.
            audio_manager._report_efx_armor_breach(1, "s", "l", True)
            audio_manager._report_efx_armor_breach(1, "s", "l", False)


class LogFsyncGuardTests(unittest.TestCase):
    def setUp(self):
        self.handle = _FakeLogHandle()
        self.saved = logger._LOG_HANDLE
        logger._LOG_HANDLE = self.handle

    def tearDown(self):
        logger._LOG_HANDLE = self.saved

    def test_fsync_skipped_while_interpreter_is_finalizing(self):
        with mock.patch("sys.is_finalizing", return_value=True), \
                mock.patch("libs.logger.os.fsync") as fsync, \
                mock.patch("builtins.print"):
            logger.log("goodbye")
        fsync.assert_not_called()
        self.assertIn("goodbye", self.handle.data)

    def test_fsync_still_runs_while_the_game_is_live(self):
        with mock.patch("sys.is_finalizing", return_value=False), \
                mock.patch("libs.logger.os.fsync") as fsync, \
                mock.patch("builtins.print"):
            logger.log("hello")
        fsync.assert_called_once()
        self.assertIn("hello", self.handle.data)


if __name__ == "__main__":
    unittest.main()
