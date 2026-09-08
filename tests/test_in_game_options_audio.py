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
        self.turning_mode_item_text = "Turning mode"
        self.music_paths = []

    def add_items(self, items):
        self.items.extend(items)

    def set_music(self, path):
        self.music_paths.append(path)


class _Game:
    audio_mngr = SimpleNamespace(hrtf=SimpleNamespace(current_model="default"))

    def toggle_item(self, name, *args):
        return name


def _open_options(in_game=False, parent=None, production=True):
    from libs import menus

    created = []

    def make_menu(*args, **kwargs):
        menu = _OptionsMenu(*args, **kwargs)
        created.append(menu)
        return menu

    with mock.patch("libs.menus.OptionsMenu", side_effect=make_menu), \
            mock.patch("libs.menus.set_default_sounds"), \
            mock.patch("libs.menus.server_config.is_production_build", return_value=production):
        kwargs = {}
        if in_game:
            kwargs.update(parent=parent, in_game=True)
        menus.options_menu(_Game(), lambda: None, replace_call=lambda _menu: None, **kwargs)
    return created[0]


def _has_item(menu, prefix):
    return any(isinstance(item[0], str) and item[0].startswith(prefix) for item in menu.items)


class TestInGameOptionsAudio(unittest.TestCase):
    def test_in_game_options_no_longer_offer_refresh_game_audio(self):
        # The Refresh game audio. Try to restore sound. option was removed:
        # it rarely restored broken playback, and closing Options already
        # asks Music Bot and Jukebox streams to recover automatically.
        menu = _open_options(in_game=True, parent=object())
        self.assertFalse(_has_item(menu, "Refresh game audio"))

    def test_production_build_hides_custom_presence_sounds_menu(self):
        # Compiled release builds hide the presence-sound upload menu; the
        # feature stays reachable only while running from source.
        menu = _open_options(in_game=True, parent=object(), production=True)
        self.assertFalse(_has_item(menu, "Configure custom online and offline sounds"))

    def test_source_run_still_offers_custom_presence_sounds_menu(self):
        menu = _open_options(in_game=True, parent=object(), production=False)
        self.assertTrue(_has_item(menu, "Configure custom online and offline sounds"))

    def test_in_game_options_do_not_start_main_menu_music(self):
        menu = _open_options(in_game=True, parent=object())
        self.assertEqual(menu.music_paths, [])

    def test_title_options_keep_menu_music(self):
        menu = _open_options()
        self.assertEqual(menu.music_paths, ["music/10.ogg"])
        self.assertFalse(_has_item(menu, "Refresh game audio"))

    def test_speak_direction_on_turn_defaults_to_on(self):
        # New players should hear their direction as soon as they finish a
        # turn, so the option must start ON (saved configs keep their choice).
        from libs import menus

        captured = {}

        class CapturingGame(_Game):
            def toggle_item(self, name, *args):
                if name == "speak your direction when finished turning":
                    captured["args"] = args
                return name

        def make_menu(*args, **kwargs):
            menu = _OptionsMenu(*args, **kwargs)
            return menu

        with mock.patch("libs.menus.OptionsMenu", side_effect=make_menu), \
                mock.patch("libs.menus.set_default_sounds"), \
                mock.patch("libs.menus.server_config.is_production_build", return_value=True):
            menus.options_menu(
                CapturingGame(), lambda: None, replace_call=lambda _menu: None
            )

        self.assertEqual(captured.get("args"), ("speak_on_turn", True))
