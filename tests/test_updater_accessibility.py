import os
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from libs import updater
from libs.accessible_progress import AccessibleProgressBar


class _FakeProgressBar:
    def __init__(self):
        self.active = False
        self.created = 0
        self.values = []
        self.destroyed = 0

    def create(self):
        self.created += 1
        self.active = True
        return True

    def set_value(self, value):
        self.values.append(value)
        return self.active

    def destroy(self):
        self.destroyed += 1
        self.active = False


class _FakeDownload:
    def __init__(self, progress):
        self.progress = progress

    def isFinished(self):
        return False

    def get_progress(self):
        return self.progress


class _FakeUser32:
    def __init__(self):
        self.sent = []
        self.notified = []
        self.destroyed = []

    def SendMessageW(self, hwnd, message, value, extra):
        self.sent.append((hwnd, message, value, extra))

    def NotifyWinEvent(self, event, hwnd, object_id, child_id):
        self.notified.append((event, hwnd, object_id, child_id))

    def DestroyWindow(self, hwnd):
        self.destroyed.append(hwnd)


def _state(progress):
    obj = updater.Updater.__new__(updater.Updater)
    obj.game = SimpleNamespace()
    obj.update_info = {"tag": "v9.9.9", "zip_url": "https://example.invalid/update.zip"}
    obj.downloading = True
    obj.smart_dl = _FakeDownload(progress)
    obj.last_pct = -1
    obj.last_spoken_decile = 0
    obj.progress_bar = _FakeProgressBar()
    return obj


class UpdaterAccessibilityTests(unittest.TestCase):
    def test_native_value_is_clamped_and_notifies_accessibility(self):
        bar = AccessibleProgressBar()
        api = _FakeUser32()
        bar.hwnd = 123
        bar._user32 = api
        self.assertTrue(bar.set_value(140))
        self.assertEqual(api.sent[-1][2], 100)
        self.assertEqual(
            api.notified[-1],
            (bar._EVENT_OBJECT_VALUECHANGE, 123, bar._OBJID_CLIENT, bar._CHILDID_SELF),
        )

    def test_native_destroy_is_idempotent(self):
        bar = AccessibleProgressBar()
        api = _FakeUser32()
        bar.hwnd = 123
        bar._user32 = api
        bar.destroy()
        bar.destroy()
        self.assertEqual(api.destroyed, [123])
        self.assertFalse(bar.active)

    def test_progress_change_updates_native_control(self):
        state = _state(0.37)
        with mock.patch.object(updater.state.State, "update"), \
                mock.patch.object(updater.pygame.display, "set_caption"), \
                mock.patch.object(updater, "speak"):
            state.update([])
        self.assertEqual(state.progress_bar.created, 1)
        self.assertEqual(state.progress_bar.values, [37])
        self.assertEqual(state.last_pct, 37)

    def test_crossed_decile_is_spoken_when_percentage_skips_it(self):
        state = _state(0.09)
        with mock.patch.object(updater.state.State, "update"), \
                mock.patch.object(updater.pygame.display, "set_caption"), \
                mock.patch.object(updater, "speak") as speak:
            state.update([])
            state.smart_dl.progress = 0.11
            state.update([])
        speak.assert_called_once_with("10 percent", False)

    def test_exit_destroys_progress_control(self):
        state = _state(0.5)
        state.progress_bar.active = True
        with mock.patch.object(updater.state.State, "exit"):
            state.exit()
        self.assertEqual(state.progress_bar.destroyed, 1)
        self.assertFalse(state.progress_bar.active)

    def test_download_failure_destroys_progress_control(self):
        state = _state(0.5)
        state.progress_bar.active = True
        with mock.patch("libs.menus.main_menu"), \
                mock.patch.object(updater.pygame.display, "set_caption"), \
                mock.patch.object(updater, "speak"):
            state._on_download_failed("failed")
        self.assertEqual(state.progress_bar.destroyed, 1)
        self.assertFalse(state.downloading)


if __name__ == "__main__":
    unittest.main()
