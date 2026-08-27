"""Regression coverage for opening Options while map streams are active."""

import os
import sys
import unittest
from unittest import mock
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


class _OptionsMenu:
    def __init__(self, *args, **kwargs):
        self.items = []
        self.turning_sensitivity_item_text = "Turning sensitivity"
        self.music_paths = []

    def add_items(self, items):
        self.items.extend(items)

    def set_music(self, path):
        self.music_paths.append(path)


class _Game:
    audio_mngr = SimpleNamespace(hrtf=SimpleNamespace(current_model="default"))

    def toggle_item(self, name, *args):
        return name


class TestInGameOptionsAudio(unittest.TestCase):
    def test_in_game_options_offer_refresh_without_closing_menu(self):
        from libs import menus

        created = []
        parent = SimpleNamespace(refresh_game_audio=mock.Mock(return_value=True))

        def make_menu(*args, **kwargs):
            menu = _OptionsMenu(*args, **kwargs)
            created.append(menu)
            return menu

        with mock.patch("libs.menus.OptionsMenu", side_effect=make_menu), \
                mock.patch("libs.menus.set_default_sounds"), \
                mock.patch("libs.menus.server_config.is_production_build", return_value=True):
            menus.options_menu(
                _Game(), lambda: None, replace_call=lambda _menu: None,
                parent=parent, in_game=True,
            )

        refresh_items = [item for item in created[0].items if item[0] == "Refresh game audio. Try to restore sound."]
        self.assertEqual(len(refresh_items), 1)
        refresh_items[0][1]()
        parent.refresh_game_audio.assert_called_once_with()

    def test_in_game_options_do_not_start_main_menu_music(self):
        from libs import menus

        created = []

        def make_menu(*args, **kwargs):
            menu = _OptionsMenu(*args, **kwargs)
            created.append(menu)
            return menu

        with mock.patch("libs.menus.OptionsMenu", side_effect=make_menu), \
                mock.patch("libs.menus.set_default_sounds"), \
                mock.patch("libs.menus.server_config.is_production_build", return_value=True):
            menus.options_menu(
                _Game(), lambda: None, replace_call=lambda _menu: None,
                parent=object(), in_game=True,
            )

        self.assertEqual(created[0].music_paths, [])

    def test_title_options_keep_menu_music(self):
        from libs import menus

        created = []

        def make_menu(*args, **kwargs):
            menu = _OptionsMenu(*args, **kwargs)
            created.append(menu)
            return menu

        with mock.patch("libs.menus.OptionsMenu", side_effect=make_menu), \
                mock.patch("libs.menus.set_default_sounds"), \
                mock.patch("libs.menus.server_config.is_production_build", return_value=True):
            menus.options_menu(_Game(), lambda: None, replace_call=lambda _menu: None)

        self.assertEqual(created[0].music_paths, ["music/10.ogg"])
        self.assertFalse(any(isinstance(item[0], str) and item[0].startswith("Refresh game audio") for item in created[0].items))

    def test_refresh_button_keeps_menu_open_on_failure_and_cooldown(self):
        from libs import menus
        from test_audio_refresh import TestGameplayAudioRefresh

        gp, _, _, _ = TestGameplayAudioRefresh().make_gameplay()
        gp.music_bot.refresh_environment_audio.return_value = False
        exit_options = mock.Mock()
        replace = mock.Mock()
        with mock.patch("libs.menus.OptionsMenu", _OptionsMenu), \
                mock.patch("libs.menus.set_default_sounds"), \
                mock.patch("libs.menus.server_config.is_production_build", return_value=True):
            menus.options_menu(_Game(), exit_options, replace_call=replace, parent=gp, in_game=True)
        menu = replace.call_args.args[0]
        callback = next(item[1] for item in menu.items
                        if isinstance(item[0], str) and item[0].startswith("Refresh game audio"))
        with mock.patch("libs.gameplay.time.monotonic", side_effect=[100.0, 102.0]), \
                mock.patch("libs.gameplay.speak") as speak:
            self.assertFalse(callback())
            self.assertFalse(callback())
        exit_options.assert_not_called()
        replace.assert_called_once()
        self.assertIn("incomplete", speak.call_args_list[0].args[0])
        self.assertIn("cooling down", speak.call_args_list[1].args[0])
        gp.music_bot.refresh_environment_audio.assert_called_once_with()
