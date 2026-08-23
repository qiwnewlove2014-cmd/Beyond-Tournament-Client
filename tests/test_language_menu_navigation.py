"""Regression coverage for the in-game chat-language menu."""

import unittest
from unittest import mock

import pygame

from libs import state
from libs.gameplay import Gameplay


class _DirectSoundGroup:
    def play(self, *args, **kwargs):
        pass


class _Network:
    def __init__(self):
        self.sent = []

    def send(self, *args):
        self.sent.append(args)


class _Game:
    def __init__(self):
        self.direct_soundgroup = _DirectSoundGroup()
        self.network = _Network()
        self.current_language = "th"


def _keydown(key):
    return pygame.event.Event(
        pygame.KEYDOWN,
        key=key,
        mod=pygame.KMOD_NONE,
        unicode="",
    )


class LanguageMenuNavigationTests(unittest.TestCase):
    def setUp(self):
        self.game = _Game()
        self.gameplay = Gameplay.__new__(Gameplay)
        state.State.__init__(self.gameplay, self.game)
        self.patches = (
            mock.patch("libs.gameplay.speak"),
            mock.patch("libs.gameplay.menus.set_default_sounds"),
            mock.patch("libs.menu.Menu.enter"),
            mock.patch("libs.menu.Menu.exit"),
        )
        for patcher in self.patches:
            patcher.start()

    def tearDown(self):
        for patcher in reversed(self.patches):
            patcher.stop()

    def _open_menu(self):
        self.gameplay.show_language_menu(
            {"en": "English", "th": "Thai"},
            {"en": 1, "th": 2},
            "th",
        )
        return self.gameplay.substates[-1]

    def test_escape_closes_without_changing_language(self):
        language_menu = self._open_menu()
        self.assertEqual(language_menu.items[language_menu.pos][0], "Current Thai 2 players")

        language_menu.update((_keydown(pygame.K_ESCAPE),))

        self.assertEqual(self.gameplay.substates, [])
        self.assertEqual(self.game.network.sent, [])
        self.assertEqual(self.game.current_language, "th")

    def test_cancel_with_enter_closes_without_changing_language(self):
        language_menu = self._open_menu()
        language_menu.pos = len(language_menu.items) - 1

        language_menu.update((_keydown(pygame.K_RETURN),))

        self.assertEqual(self.gameplay.substates, [])
        self.assertEqual(self.game.network.sent, [])

    def test_selecting_language_sends_once_and_closes_once(self):
        language_menu = self._open_menu()
        language_menu.pos = 0

        language_menu.update((_keydown(pygame.K_RETURN),))

        self.assertEqual(self.gameplay.substates, [])
        self.assertEqual(self.game.current_language, "en")
        self.assertEqual(len(self.game.network.sent), 1)
        self.assertEqual(self.game.network.sent[0][1], "change_language")
        self.assertEqual(self.game.network.sent[0][2], {"language": "en"})


if __name__ == "__main__":
    unittest.main()
