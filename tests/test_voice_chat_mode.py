"""Voice chat key mode tests: "Tap to talk" (toggle) vs "Push to talk" (hold).

Covers:
- The saved option (default toggle, clamped to the known set).
- Gameplay key routing: PTT starts on key down and stops on key up; toggle
  mode toggles on key down and ignores key up.
- The Options menu control (Left/Right on "Voice chat mode", placed right
  after the "Voice Chat" enable toggle).
"""
import unittest
from types import SimpleNamespace
from unittest import mock

from libs import gameplay, menus, options


class TestVoiceChatModeOption(unittest.TestCase):
    def test_default_mode_is_ptt(self):
        self.assertEqual(options.get_voice_chat_mode(), "ptt")

    def test_set_and_label(self):
        self.assertEqual(options.set_voice_chat_mode("toggle"), "toggle")
        self.assertEqual(options.get_voice_chat_mode(), "toggle")
        self.assertIn("hold", options.get_voice_chat_mode_label("ptt").lower())
        self.assertEqual(options.set_voice_chat_mode("ptt"), "ptt")

    def test_unknown_saved_mode_falls_back_to_ptt(self):
        with mock.patch.object(options, "get", return_value="banana"):
            self.assertEqual(options.get_voice_chat_mode(), "ptt")

    def test_set_unknown_mode_clamps_to_ptt(self):
        self.assertEqual(options.set_voice_chat_mode("banana"), "ptt")


class TestVoiceChatKeyRouting(unittest.TestCase):
    def make_gp(self):
        gp = object.__new__(gameplay.Gameplay)
        gp.voice_chat_start = mock.Mock()
        gp.voice_chat_stop = mock.Mock()
        gp.voice_chat_toggle = mock.Mock()
        return gp

    def test_ptt_mode_starts_on_key_down(self):
        gp = self.make_gp()
        with mock.patch.object(options, "get_voice_chat_mode", return_value="ptt"):
            gp.voice_chat_key(0)
        gp.voice_chat_start.assert_called_once_with(0)
        gp.voice_chat_toggle.assert_not_called()

    def test_ptt_mode_stops_on_key_up(self):
        gp = self.make_gp()
        with mock.patch.object(options, "get_voice_chat_mode", return_value="ptt"):
            gp.voice_chat_key_release(0)
        gp.voice_chat_stop.assert_called_once_with(0)

    def test_toggle_mode_ignores_key_up(self):
        gp = self.make_gp()
        with mock.patch.object(options, "get_voice_chat_mode", return_value="toggle"):
            gp.voice_chat_key_release(0)
        gp.voice_chat_stop.assert_not_called()

    def test_toggle_mode_toggles_on_key_down(self):
        gp = self.make_gp()
        with mock.patch.object(options, "get_voice_chat_mode", return_value="toggle"):
            gp.voice_chat_key(0)
        gp.voice_chat_toggle.assert_called_once_with(0)
        gp.voice_chat_start.assert_not_called()


class TestVoiceChatModeMenuControl(unittest.TestCase):
    def make_menu(self):
        m = menus.OptionsMenu.__new__(menus.OptionsMenu)
        m.edge = ""
        m.direct_soundgroup = object()
        m.voice_chat_mode_item_index = None
        return m

    def test_adjust_forward_cycles_to_ptt(self):
        with mock.patch.object(menus.options, "get_voice_chat_mode", return_value="toggle"), \
                mock.patch.object(menus.options, "get_voice_chat_mode_label", return_value="Tap to talk"), \
                mock.patch.object(menus.options, "set_voice_chat_mode", side_effect=lambda mode: mode) as set_mode, \
                mock.patch.object(menus.speech, "speak"):
            m = self.make_menu()
            m._adjust_voice_chat_mode(1)
        set_mode.assert_called_once_with("ptt")

    def test_adjust_backward_wraps_to_ptt(self):
        with mock.patch.object(menus.options, "get_voice_chat_mode", return_value="toggle"), \
                mock.patch.object(menus.options, "get_voice_chat_mode_label", return_value="Tap to talk"), \
                mock.patch.object(menus.options, "set_voice_chat_mode", side_effect=lambda mode: mode) as set_mode, \
                mock.patch.object(menus.speech, "speak"):
            m = self.make_menu()
            m._adjust_voice_chat_mode(-1)
        set_mode.assert_called_once_with("ptt")


class TestVoiceChatModeMenuItemPlacement(unittest.TestCase):
    class _FakeMenu:
        def __init__(self, *args, **kwargs):
            self.items = []
            self.turning_sensitivity_item_text = "Turning sensitivity"
            self.turning_mode_item_text = "Turning mode"
            self.voice_chat_mode_item_text = "Voice chat mode"
            self._adjust_voice_chat_mode = mock.Mock()
            self.music_paths = []

        def add_items(self, items):
            self.items.extend(items)

        def set_music(self, path):
            self.music_paths.append(path)

    def test_item_sits_right_after_voice_chat_toggle(self):
        from libs import menus as menus_mod

        class _Game:
            audio_mngr = SimpleNamespace(hrtf=SimpleNamespace(current_model="default"))

            def toggle_item(self, name, *args):
                return name, lambda: None

        created = []

        def make_menu(*args, **kwargs):
            m = self._FakeMenu(*args, **kwargs)
            created.append(m)
            return m

        with mock.patch("libs.menus.OptionsMenu", side_effect=make_menu), \
                mock.patch("libs.menus.set_default_sounds"), \
                mock.patch("libs.menus.server_config.is_production_build", return_value=True):
            menus_mod.options_menu(_Game(), lambda: None, replace_call=lambda _m: None)

        labels = [item[0] for item in created[0].items]
        voice_index = labels.index("Voice Chat")
        self.assertEqual(labels[voice_index + 1], "Voice chat mode")

    def test_enter_activation_cycles_the_mode(self):
        # Enter runs the item's action (Menu.select_current_item), so the
        # action must cycle the mode forward instead of doing nothing.
        from libs import menus as menus_mod

        class _Game:
            audio_mngr = SimpleNamespace(hrtf=SimpleNamespace(current_model="default"))

            def toggle_item(self, name, *args):
                return name, lambda: None

        created = []

        def make_menu(*args, **kwargs):
            m = self._FakeMenu(*args, **kwargs)
            created.append(m)
            return m

        with mock.patch("libs.menus.OptionsMenu", side_effect=make_menu), \
                mock.patch("libs.menus.set_default_sounds"), \
                mock.patch("libs.menus.server_config.is_production_build", return_value=True):
            menus_mod.options_menu(_Game(), lambda: None, replace_call=lambda _m: None)

        labels = [item[0] for item in created[0].items]
        voice_index = labels.index("Voice chat mode")
        action = created[0].items[voice_index][1]
        action()
        created[0]._adjust_voice_chat_mode.assert_called_once_with(1)

    def test_item_text_mentions_enter_not_arrows(self):
        with mock.patch.object(menus.options, "get_voice_chat_mode_label", return_value="Tap to talk"):
            text = menus.OptionsMenu.voice_chat_mode_item_text()
        self.assertIn("Press Enter", text)
        self.assertNotIn("Left or Right", text)


if __name__ == "__main__":
    unittest.main()