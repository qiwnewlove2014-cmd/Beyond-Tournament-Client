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

        refresh_items = [item for item in created[0].items if item[0] == "Refresh game audio"]
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
        self.assertFalse(any(item[0] == "Refresh game audio" for item in created[0].items))
