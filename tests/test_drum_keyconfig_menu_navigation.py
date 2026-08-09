import unittest
from unittest import mock

import pygame

from libs import menus, state
from libs.key_config_screen import Key_config_screen


class FakeKeyconfig:
    def __init__(self):
        self.keys = {}

    def get(self, function, default):
        return self.keys.get(function, default)

    def set(self, key_code, function, autosave=True):
        self.keys[function] = key_code

    def unset(self, function, autosave=True):
        self.keys.pop(function, None)

    def save(self):
        pass


class FakeSoundgroup:
    def play(self, *args, **kwargs):
        pass


class FakeGame:
    def __init__(self):
        self.keyconfig = FakeKeyconfig()
        self.direct_soundgroup = FakeSoundgroup()
        self.callbacks = []
        self.current_state = None

    def call_after(self, delay, callback):
        self.callbacks.append((delay, callback))

    def replace(self, new_state):
        self.current_state = new_state
        return new_state


def escape_event():
    return pygame.event.Event(pygame.KEYDOWN, key=pygame.K_ESCAPE)


class DrumKeyconfigMenuNavigationTests(unittest.TestCase):
    def setUp(self):
        self.patches = (
            mock.patch.object(menus, "set_default_sounds"),
            mock.patch.object(menus.menu.Menu, "enter"),
            mock.patch.object(menus.menu.Menu, "exit"),
            mock.patch.object(Key_config_screen, "enter"),
            mock.patch("libs.key_config_screen.speak"),
        )
        for patcher in self.patches:
            patcher.start()

    def tearDown(self):
        for patcher in reversed(self.patches):
            patcher.stop()

    def test_in_game_escape_unwinds_each_menu_level_without_stacking(self):
        game = FakeGame()
        gameplay = state.State(game)
        options_marker = state.State(game, parrent=gameplay)
        gameplay.add_substate(options_marker)

        menus.drum_keyconfig_menu(
            game,
            gameplay.pop_last_substate,
            replace_call=gameplay.add_substate,
            parent=gameplay,
            in_game=True,
        )
        drum_list = gameplay.substates[-1]
        self.assertEqual(len(gameplay.substates), 2)

        drum_list.items[0][1]()
        pad_menu = gameplay.substates[-1]
        self.assertEqual(len(gameplay.substates), 3)

        pad_menu.items[0][1]()
        capture = gameplay.substates[-1]
        self.assertIsInstance(capture, Key_config_screen)
        self.assertEqual(len(gameplay.substates), 4)

        capture.update((escape_event(),))
        self.assertEqual(len(game.callbacks), 1)
        game.callbacks.pop()[1]()

        self.assertEqual(len(gameplay.substates), 3)
        refreshed_pad_menu = gameplay.substates[-1]
        self.assertIsInstance(refreshed_pad_menu, menus.menu.Menu)
        self.assertEqual(refreshed_pad_menu.title, "Configure Kick keys.")

        refreshed_pad_menu.update((escape_event(),))
        self.assertEqual(gameplay.substates, [options_marker, drum_list])

        drum_list.update((escape_event(),))
        self.assertEqual(gameplay.substates, [options_marker])

    def test_main_menu_escape_still_replaces_pad_with_drum_list(self):
        game = FakeGame()
        on_exit = mock.Mock()
        menus.drum_keyconfig_menu(game, on_exit, replace_call=game.replace)
        drum_list = game.current_state

        drum_list.items[0][1]()
        self.assertEqual(game.current_state.title, "Configure Kick keys.")

        game.current_state.update((escape_event(),))
        self.assertEqual(game.current_state.title, "Configure drum keys.")

        game.current_state.update((escape_event(),))
        on_exit.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
