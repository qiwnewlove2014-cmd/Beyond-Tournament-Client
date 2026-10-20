"""A dropped connection mid-game retries the session instead of the login menu.

`menus.main_menu` is the login screen (Login / Set account / Create account /
options) because a client with no session has no account to show. A transport
drop used to land there even in the middle of play, so any hiccup -- a slow
machine, a brief network loss -- cost the player the login they already had.
The logout flow must still land there, which is what `logging_out` is for.
"""

import unittest
from types import SimpleNamespace
from unittest import mock

from libs import game as game_module


class DropTests(unittest.TestCase):
    def make(self, logging_out=False, in_game=True):
        game = game_module.Game.__new__(game_module.Game)
        game.network = None
        game.reconnecting = False
        game.logging_out = logging_out
        game._reconnect_attempts = 0
        game.gameplay = SimpleNamespace()
        game.stack = [game.gameplay] if in_game else []
        game.replaced = []
        game.replace = lambda state: game.replaced.append(state)
        game.reconnect_state = lambda: None
        return game

    def test_a_drop_while_playing_retries_the_session(self):
        game = self.make()
        with mock.patch.object(game_module.menus, "main_menu") as main_menu, \
                mock.patch.object(game_module, "speak"):
            game_module.Game.disconnected(game)
        self.assertTrue(game.reconnecting)
        self.assertEqual(len(game.replaced), 1)
        main_menu.assert_not_called()

    def test_a_logout_still_lands_on_the_main_menu(self):
        game = self.make(logging_out=True)
        with mock.patch.object(game_module.menus, "main_menu") as main_menu, \
                mock.patch.object(game_module, "speak"):
            game_module.Game.disconnected(game)
        self.assertFalse(game.reconnecting)
        main_menu.assert_called_once()

    def test_a_drop_before_login_lands_on_the_main_menu(self):
        game = self.make(in_game=False)
        with mock.patch.object(game_module.menus, "main_menu") as main_menu, \
                mock.patch.object(game_module, "speak"):
            game_module.Game.disconnected(game)
        self.assertFalse(game.reconnecting)
        main_menu.assert_called_once()

    def test_the_retry_is_bounded_then_the_login_menu_returns(self):
        game = self.make()
        game.reconnecting = True
        game._reconnect_attempts = game_module.Game.RECONNECT_ATTEMPTS
        game.reconnect_clock = SimpleNamespace(elapsed=5000, restart=lambda: None)
        with mock.patch.object(game_module.menus, "main_menu") as main_menu, \
                mock.patch.object(game_module, "speak"):
            game_module.Game.reconnect_state(game)
        self.assertFalse(game.reconnecting)
        main_menu.assert_called_once()

    def test_the_retry_keeps_trying_below_the_bound(self):
        game = self.make()
        game.reconnecting = True
        game._reconnect_attempts = 0
        game.reconnect_clock = SimpleNamespace(elapsed=5000, restart=lambda: None)
        game._close_network = lambda polite=True: None
        game._open_first_login_attempt = mock.Mock()
        with mock.patch.object(game_module.menus, "main_menu") as main_menu, \
                mock.patch.object(game_module, "speak"):
            game_module.Game.reconnect_state(game)
        self.assertEqual(game._reconnect_attempts, 1)
        main_menu.assert_not_called()


if __name__ == "__main__":
    unittest.main()
