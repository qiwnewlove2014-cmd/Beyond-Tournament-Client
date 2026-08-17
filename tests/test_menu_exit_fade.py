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


class FakeListener:
    def __init__(self):
        self.gain = 0.0


class FakeSoundGroup:
    def play(self, *args, **kwargs):
        return None


class FakeAudioMngr:
    def __init__(self):
        self.volume_categories = {"master": [70, None]}
        self.listener = FakeListener()

    def create_soundgroup(self, direct=False):
        return FakeSoundGroup()


class FakeGame:
    """Enough of Game for menus.main_menu + a sentinel fade handler."""

    def __init__(self):
        self.direct_soundgroup = object()
        self.audio_mngr = FakeAudioMngr()
        self.replaced = None
        self.fade_out_and_exit = object()  # sentinel: item must bind this
        # Menu items reference these; they are never invoked in this test.
        self.set_account = lambda: None
        self.create_account = lambda: None

    def replace(self, m):
        self.replaced = m


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

    def test_main_menu_restores_listener_gain_after_fade(self):
        """Returning to the main menu after an in-game logout fade restores
        the listener gain so the menu is not silent."""
        game = FakeGame()
        game.audio_mngr.listener.gain = 0.0  # left at silence by the fade
        menus.main_menu(game)
        self.assertEqual(game.audio_mngr.listener.gain, 0.7)  # 70 / 100


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
