"""Offline tests: shutdown-safe EFX armor reporting and the log fsync guard.

A weakref finalizer that logs during interpreter shutdown must never block
on the logger's lock+fsync path — an fsync stalled behind an antivirus
scan at process exit once hung the quit until the watchdog reported a
native deadlock (fatal report, player icesound 2026-08-29).

The same finalizer must not mistake the *exit sweep* for a breach either:
``weakref.finalize`` runs every still-registered finalizer at interpreter exit,
whether or not the referent was collected, so a live wrapper there is proof the
armor held, not that a slot was freed (a clean quit once wrote one false
"was garbage collected" line per armored slot, effect and filter).
"""

import ctypes
import gc
import os
import sys
import unittest
import weakref
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


class _Wrapper:
    """Stands in for a cyal EFX wrapper; only weak-referenceability matters here."""


class ArmorFinalizerAlivenessTests(unittest.TestCase):
    """The armor's finalizer reports a breach only when the referent is gone."""

    @staticmethod
    def _manager():
        # No __init__: the armor itself needs no device, and offline there is none.
        return audio_manager.AudioManager.__new__(audio_manager.AudioManager)

    @staticmethod
    def _finalizer_for(obj):
        """The finalizer ``_armor_filter`` registered for ``obj`` (None if none)."""
        for fin, info in list(weakref.finalize._registry.items()):
            if info.weakref() is obj:
                return fin
        return None

    def test_the_exit_sweep_over_live_wrappers_reports_nothing(self):
        manager = self._manager()
        wrappers = [_Wrapper() for _ in range(3)]
        with mock.patch("libs.audio_manager._report_efx_armor_breach") as breach:
            for wrapper in wrappers:
                manager._armor_filter(wrapper, "slot_pool_init", label="slot")
            finalizers = [self._finalizer_for(wrapper) for wrapper in wrappers]
            self.assertTrue(all(fin is not None for fin in finalizers),
                            "the armor must register one finalizer per wrapper")
            # Exactly what weakref.finalize._exitfunc does at interpreter exit.
            for finalizer in finalizers:
                finalizer()
        breach.assert_not_called()

    def test_a_wrapper_that_really_is_freed_still_reports(self):
        manager = self._manager()
        wrapper = _Wrapper()
        with mock.patch("libs.audio_manager._report_efx_armor_breach") as breach, \
                mock.patch.object(ctypes.pythonapi, "Py_IncRef", lambda *a, **k: None):
            # Py_IncRef neutered: the armor fails exactly as the report describes.
            # (A Mock here would hold the wrapper through its recorded call args
            # and keep the finalizer from ever firing.)
            manager._armor_filter(wrapper, "audio_manager.py:1 (gen_filter)")
            del wrapper
            gc.collect()
        breach.assert_called_once()
        args = breach.call_args.args
        self.assertEqual(args[1], "audio_manager.py:1 (gen_filter)")
        self.assertEqual(args[2], "filter")
        self.assertIs(args[3], False)


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
