import unittest
from unittest import mock

import pygame

from libs.key_config_screen import Key_config_screen


class FakeKeyconfig:
    def __init__(self):
        self.saved = []

    def set(self, key_code, function):
        self.saved.append((key_code, function))


class FakeSoundgroup:
    def __init__(self):
        self.played = []

    def play(self, path):
        self.played.append(path)


class FakeGame:
    def __init__(self):
        self.keyconfig = FakeKeyconfig()
        self.direct_soundgroup = FakeSoundgroup()
        self.callbacks = []

    def call_after(self, delay, callback):
        self.callbacks.append((delay, callback))


class KeyConfigScreenTests(unittest.TestCase):
    def test_validator_rejects_key_without_finishing(self):
        game = FakeGame()
        screen = Key_config_screen(
            game,
            "drum_kick",
            options_menu=lambda: None,
            key_validator=lambda _key: "That key is unavailable.",
        )
        event = pygame.event.Event(pygame.KEYDOWN, key=pygame.K_RETURN)
        with mock.patch("libs.key_config_screen.speak") as speak:
            screen.update((event,))
        self.assertFalse(screen.done)
        self.assertFalse(game.keyconfig.saved)
        self.assertEqual(game.direct_soundgroup.played, ["ui/error.ogg"])
        speak.assert_called_once_with("That key is unavailable.")

    def test_optional_escape_cancels_without_saving(self):
        game = FakeGame()
        callback = lambda: None
        screen = Key_config_screen(
            game,
            "drum_kick",
            options_menu=callback,
            cancel_keys=(pygame.K_ESCAPE,),
        )
        event = pygame.event.Event(pygame.KEYDOWN, key=pygame.K_ESCAPE)
        with mock.patch("libs.key_config_screen.speak"):
            screen.update((event,))
        self.assertTrue(screen.done)
        self.assertFalse(game.keyconfig.saved)
        self.assertEqual(game.callbacks, [(300, callback)])

    def test_existing_generic_binding_behavior_is_preserved(self):
        game = FakeGame()
        callback = lambda: None
        screen = Key_config_screen(
            game,
            "move_forward",
            options_menu=callback,
        )
        event = pygame.event.Event(pygame.KEYDOWN, key=pygame.K_UP)
        with mock.patch("libs.key_config_screen.speak"):
            screen.update((event,))
        self.assertEqual(
            game.keyconfig.saved,
            [(pygame.K_UP, "move_forward")],
        )
        self.assertEqual(game.callbacks, [(500, callback)])


if __name__ == "__main__":
    unittest.main()
