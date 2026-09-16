"""The shield reminder belongs to a competition, not to the world.

Pressing S with no shield is a stray key in a world/build map, in the Pong
cabinet and at a Blackjack table, and the spoken line for it talks over the
map's own sounds for nothing. The server's enter_match / exit_match packets --
which only a real match's start() / remove_player send -- are what tells the two
apart, kept on Gameplay.game_started.
"""

import os
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from libs.gameplay import Gameplay  # noqa: E402


def make_gameplay(game_started=False, pong_mode=False, in_minigame_match=False,
                  equipped_shield=None, dead=False, spectator=False):
    gameplay = Gameplay.__new__(Gameplay)
    gameplay.spectator_mode = spectator
    gameplay.game_started = game_started
    gameplay.in_minigame_match = in_minigame_match
    gameplay.shield_mngr = SimpleNamespace(
        equipped_shield=equipped_shield,
        is_raising=False,
        raise_shield=mock.Mock(),
        lower_shield=mock.Mock(),
    )
    gameplay.player = SimpleNamespace(dead=dead, hfacing=90)
    gameplay.game = SimpleNamespace(
        pong_mode=pong_mode,
        network=mock.Mock(),
    )
    return gameplay


class ShieldReminderScopeTests(unittest.TestCase):
    @mock.patch("libs.gameplay.speak")
    def test_a_world_map_answers_a_stray_s_with_silence(self, speak_mock):
        gameplay = make_gameplay(game_started=False)
        gameplay.start_raise_shield()
        speak_mock.assert_not_called()
        gameplay.game.network.send.assert_not_called()

    @mock.patch("libs.gameplay.speak")
    def test_the_pong_cabinet_answers_a_stray_s_with_silence(self, speak_mock):
        gameplay = make_gameplay(game_started=False, pong_mode=True)
        gameplay.start_raise_shield()
        speak_mock.assert_not_called()

    @mock.patch("libs.gameplay.speak")
    def test_a_minigame_match_menu_answers_a_stray_s_with_silence(self, speak_mock):
        gameplay = make_gameplay(game_started=False, in_minigame_match=True)
        gameplay.start_raise_shield()
        speak_mock.assert_not_called()

    @mock.patch("libs.gameplay.speak")
    def test_a_competition_still_says_the_shield_is_missing(self, speak_mock):
        gameplay = make_gameplay(game_started=True)
        gameplay.start_raise_shield()
        speak_mock.assert_called_once_with("No shield equipped.")
        # A reminder is not a raise: nothing is told to the server either way.
        gameplay.game.network.send.assert_not_called()

    @mock.patch("libs.gameplay.speak")
    def test_an_equipped_shield_still_raises_in_the_world(self, speak_mock):
        gameplay = make_gameplay(
            game_started=False, equipped_shield={"id": "wood_shield_1"},
        )
        gameplay.start_raise_shield()
        speak_mock.assert_not_called()
        gameplay.shield_mngr.raise_shield.assert_called_once_with()
        gameplay.game.network.send.assert_called_once()

    @mock.patch("libs.gameplay.speak")
    def test_a_dead_player_in_a_competition_still_says_nothing(self, speak_mock):
        gameplay = make_gameplay(game_started=True, dead=True)
        gameplay.start_raise_shield()
        speak_mock.assert_not_called()

    @mock.patch("libs.gameplay.speak")
    def test_a_spectator_in_a_competition_still_says_nothing(self, speak_mock):
        gameplay = make_gameplay(game_started=True, spectator=True)
        gameplay.start_raise_shield()
        speak_mock.assert_not_called()


class CompetitionFlagWiringTests(unittest.TestCase):
    """`game_started` is written by the two match packets and read here."""

    def make_handler(self):
        from libs.event_handeler import EventHandeler

        handler = EventHandeler.__new__(EventHandeler)
        handler.gameplay = make_gameplay(game_started=False)
        handler.gameplay.pa_test_mode = True
        handler.gameplay._default_vc_compression = None
        handler.gameplay.voice_chat = None
        handler.gameplay.player.lock_weapon = False
        return handler

    @mock.patch("libs.gameplay.speak")
    def test_entering_a_match_makes_the_reminder_speak(self, speak_mock):
        handler = self.make_handler()
        handler.enter_match({})
        self.assertTrue(handler.gameplay.game_started)
        handler.gameplay.start_raise_shield()
        speak_mock.assert_called_once_with("No shield equipped.")

    @mock.patch("libs.gameplay.speak")
    def test_leaving_a_match_makes_it_silent_again(self, speak_mock):
        handler = self.make_handler()
        handler.enter_match({})
        handler.exit_match({})
        self.assertFalse(handler.gameplay.game_started)
        handler.gameplay.start_raise_shield()
        speak_mock.assert_not_called()

    def test_pong_and_blackjack_never_send_enter_match(self):
        # The gate is only honest while those two paths stay out of it. Reading
        # the server sources is the cheapest way to pin that: `enter_match` is
        # sent from a match's start() and nowhere else.
        import os
        import re

        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        server = os.path.join(os.path.dirname(root), "server", "libs")
        pattern = re.compile(r'send\([^)]*"enter_match"', re.S)
        senders = set()
        for folder, _dirs, files in os.walk(server):
            for name in files:
                if not name.endswith(".ts"):
                    continue
                path = os.path.join(folder, name)
                with open(path, encoding="utf-8") as fh:
                    text = fh.read()
                if pattern.search(text):
                    senders.add(os.path.relpath(path, server).replace(os.sep, "/"))
        self.assertEqual(senders, {"game_mode.ts"})


if __name__ == "__main__":
    unittest.main()
