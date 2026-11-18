"""Tests for the main-menu Exit fade-out.

Pressing Esc on the root main menu (or activating its Exit item) used to quit
instantly with an audio hard cut. The Exit item now runs game.fade_out_and_exit:
a ~1.5s ramp of the OpenAL listener gain to 0, then a clean exit.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from libs import menus
from libs.automation import Automation_Task
from libs.menu import Menu


class FakeListener:
    def __init__(self):
        self.gain = 0.0
        # What the output gain was when the last sound started: the logout
        # test uses it to prove the menu music began only after the world died.
        self.last_play_gain = None


class FakeSoundGroup:
    def __init__(self, listener=None):
        self.listener = listener

    def play(self, *args, **kwargs):
        if self.listener is not None:
            self.listener.last_play_gain = self.listener.gain
        return None


class FakeAudioMngr:
    def __init__(self):
        self.volume_categories = {"master": [70, None]}
        self.listener = FakeListener()

    def create_soundgroup(self, direct=False):
        return FakeSoundGroup(self.listener)


class FakeGame:
    """Enough of Game for menus.main_menu + a sentinel fade handler."""

    def __init__(self):
        self.direct_soundgroup = object()
        self.audio_mngr = FakeAudioMngr()
        self.replaced = None
        self.fade_out_and_exit = object()  # sentinel: item must bind this
        self.ask_to_restart_client = object()
        # Menu items reference these; they are never invoked in this test.
        self.set_account = lambda: None
        self.create_account = lambda: None

    def replace(self, m):
        self.replaced = m


class FakeGameplay:
    """The world state on its way out; records the gain it exited under."""

    def __init__(self, game):
        self.game = game
        self.gain_at_exit = None

    def exit(self):
        self.gain_at_exit = self.game.audio_mngr.listener.gain


class FakeGameLeavingWorld(FakeGame):
    """A game whose gameplay state is what main_menu is about to leave."""

    def __init__(self):
        super().__init__()
        self.gameplay = FakeGameplay(self)
        self.stack = [self.gameplay]

    def pop(self):
        prev = self.stack.pop()
        prev.exit()
        return prev

    def append(self, st):
        self.stack.append(st)
        return st


class TestMainMenuExitFade(unittest.TestCase):
    def test_exit_item_binds_fade_out_and_exit(self):
        game = FakeGame()
        menus.main_menu(game)
        menu = game.replaced
        self.assertIsNotNone(menu, "main_menu must replace the state")
        titles = [item[0] for item in menu.items]
        self.assertIn("Exit", titles)
        exit_item = next(item for item in menu.items if item[0] == "Exit")
        # Esc on the root menu reaches this same item (menu.py matches the
        # "exit" keyword on the last item), so this one binding covers both.
        self.assertIs(exit_item[1], game.fade_out_and_exit)

    def test_restart_item_is_immediately_before_exit(self):
        game = FakeGame()
        menus.main_menu(game)
        items = game.replaced.items
        titles = [item[0] for item in items]
        restart_index = titles.index("Restart Client")
        self.assertEqual(restart_index, titles.index("Exit") - 1)
        self.assertIs(items[restart_index][1], game.ask_to_restart_client)

    def test_main_menu_restores_listener_gain_after_fade(self):
        """Returning to the main menu after an in-game logout fade restores
        the listener gain so the menu is not silent."""
        game = FakeGame()
        game.audio_mngr.listener.gain = 0.0  # left at silence by the fade
        menus.main_menu(game)
        self.assertEqual(game.audio_mngr.listener.gain, 0.7)  # 70 / 100

    def test_logout_keeps_the_world_silent_until_it_has_exited(self):
        """The logout fade's silence must outlast the map teardown.

        Raising the gain while main_menu is still building (which decodes the
        menu music) played the old map's music and ambience at full volume for
        that instant: the burst of game sound heard just before the menu.
        """
        game = FakeGameLeavingWorld()
        game.audio_mngr.listener.gain = 0.0  # left at silence by the fade

        menus.main_menu(game)

        self.assertEqual(
            game.gameplay.gain_at_exit, 0.0,
            "the world must be destroyed while the output is still silent",
        )
        self.assertEqual(len(game.stack), 1)
        self.assertIsInstance(game.stack[-1], Menu)
        self.assertEqual(game.audio_mngr.listener.gain, 0.7)
        self.assertEqual(
            game.audio_mngr.listener.last_play_gain, 0.7,
            "the menu music must start only after the gain is restored",
        )


class FakeTimer:
    def __init__(self):
        self.elapsed = 0.0

    def restart(self):
        self.elapsed = 0.0


class TestFadeAutomation(unittest.TestCase):
    def test_fade_ramps_to_zero_then_fires_callback(self):
        """The fade task used by fade_out_and_exit ramps 100 -> 0 over ~1.5s
        and fires its exit callback exactly at silence."""
        class FakeGameForTask:
            def __init__(self):
                self.automations = []

            def new_clock(self):
                return FakeTimer()

        game = FakeGameForTask()
        values = []
        exits = []

        task = Automation_Task(
            game, None, None, 0.0, 1500,
            callback=lambda: exits.append(True),
            step_callback=values.append,
            start_value=100,
            cancelable=False,
        )
        game.automations.append(task)

        # ~2.5s of frames at a 20ms step — well past the 1500ms fade.
        # A finished task removes itself (and its timer) from the task list.
        for _ in range(150):
            if task not in game.automations:
                break
            task.timer.elapsed = 20.0
            task.loop()

        self.assertTrue(values, "step callback must have run")
        self.assertEqual(values[-1], 0.0, "fade must end at silence")
        # Monotonic descent with no upward blips.
        self.assertEqual(values, sorted(values, reverse=True))
        self.assertEqual(exits, [True], "exit callback must fire exactly once")
        self.assertNotIn(task, game.automations, "finished task removes itself")


if __name__ == "__main__":
    unittest.main()
