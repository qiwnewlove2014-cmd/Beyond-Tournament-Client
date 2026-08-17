"""Tests for InstanceManager single-instance behavior.

Compiled builds must allow exactly ONE running instance; running from source
keeps the old multi-instance behavior (up to 10 clients) for testing several
accounts at once.
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from libs import instance_manager as im


def _compiled_lock_path():
    return os.path.join(tempfile.gettempdir(), im.COMPILED_LOCK_NAME)


def _write_compiled_lock(pid):
    with open(_compiled_lock_path(), "w") as f:
        f.write(str(pid))


def _remove_compiled_lock():
    try:
        os.remove(_compiled_lock_path())
    except OSError:
        pass


class TestCmdlineMatch(unittest.TestCase):
    def test_cmdline_is_game(self):
        self.assertTrue(im._cmdline_is_game(r"C:\Games\Beyond Tournament.exe"))
        self.assertTrue(im._cmdline_is_game(r"C:\Games\beyond_tournament.exe"))
        self.assertTrue(im._cmdline_is_game(r"C:\Python311\python.exe beyond_tournament.py"))
        self.assertFalse(im._cmdline_is_game(r"C:\Windows\notepad.exe"))
        self.assertFalse(im._cmdline_is_game(""))


class TestInstanceManager(unittest.TestCase):
    def tearDown(self):
        # Clean any compiled lock we may have created/left behind.
        _remove_compiled_lock()
        # Remove any dev-slot locks we created (instance_1 is enough here).
        for i in range(1, 11):
            p = os.path.join(tempfile.gettempdir(), f"beyond_tournament_instance_{i}.lock")
            try:
                os.remove(p)
            except OSError:
                pass

    def test_source_mode_acquires_dev_slot(self):
        """Running from source: picks a free dev slot and reports acquired."""
        im.is_compiled = lambda: False
        mgr = im.InstanceManager()
        try:
            self.assertTrue(mgr.acquired)
            self.assertTrue(mgr.lock_file_path and "instance_" in mgr.lock_file_path)
            self.assertTrue(os.path.exists(mgr.lock_file_path))
            path = mgr.lock_file_path
        finally:
            mgr.release_lock()
        self.assertFalse(os.path.exists(path))

    def test_compiled_mode_acquires_when_free(self):
        """Compiled build with no other copy running: acquires normally."""
        im.is_compiled = lambda: True
        _remove_compiled_lock()
        mgr = im.InstanceManager()
        try:
            self.assertTrue(mgr.acquired)
            self.assertEqual(mgr.lock_file_path, _compiled_lock_path())
            self.assertTrue(os.path.exists(mgr.lock_file_path))
        finally:
            mgr.release_lock()
        self.assertFalse(os.path.exists(_compiled_lock_path()))

    def test_compiled_mode_refuses_second_instance(self):
        """Compiled build while another compiled copy is running: refused."""
        im.is_compiled = lambda: True
        _write_compiled_lock(os.getpid())  # our own live process holds it
        mgr = im.InstanceManager()
        self.assertFalse(mgr.acquired)
        self.assertIsNone(mgr.lock_file_path)
        _remove_compiled_lock()

    def test_compiled_mode_ignores_stale_lock(self):
        """A lock left by a dead process never blocks a fresh launch."""
        im.is_compiled = lambda: True
        _write_compiled_lock(99999999)  # PID that cannot exist
        mgr = im.InstanceManager()
        try:
            self.assertTrue(mgr.acquired)
            # Stale lock was replaced by our own PID.
            with open(mgr.lock_file_path, "r") as f:
                self.assertEqual(int(f.read().strip()), os.getpid())
        finally:
            mgr.release_lock()

    def test_compiled_instance_blocked(self):
        """Entry-point helper reports True exactly when another copy runs."""
        _remove_compiled_lock()
        self.assertFalse(im.InstanceManager.compiled_instance_blocked())
        _write_compiled_lock(os.getpid())
        self.assertTrue(im.InstanceManager.compiled_instance_blocked())
        _remove_compiled_lock()
        self.assertFalse(im.InstanceManager.compiled_instance_blocked())

    def test_release_never_deletes_foreign_lock(self):
        """release_lock must not delete a lock another process took over."""
        im.is_compiled = lambda: True
        mgr = im.InstanceManager()
        self.assertTrue(mgr.acquired)
        # Simulate an updater takeover: someone else rewrites the file.
        _write_compiled_lock(os.getpid() + 1)  # not our pid
        mgr.release_lock()
        self.assertTrue(
            os.path.exists(_compiled_lock_path()),
            "a lock owned by another process must survive our release",
        )
        _remove_compiled_lock()

    def test_release_lock_for_pid_handoff(self):
        """Updater relaunch hands the old process's lock over."""
        _write_compiled_lock(424242)  # a live-ish pid we pretend to replace
        im.release_lock_for_pid(424242)
        self.assertFalse(os.path.exists(_compiled_lock_path()))
        # A different holder is left alone.
        _write_compiled_lock(424243)
        im.release_lock_for_pid(424242)
        self.assertTrue(os.path.exists(_compiled_lock_path()))
        _remove_compiled_lock()

    def test_active_instances_count_includes_compiled(self):
        """Title counting sees the compiled lock too."""
        im.is_compiled = lambda: True
        mgr = im.InstanceManager()
        try:
            self.assertGreaterEqual(mgr.get_active_instances_count(), 1)
        finally:
            mgr.release_lock()


if __name__ == "__main__":
    unittest.main()
