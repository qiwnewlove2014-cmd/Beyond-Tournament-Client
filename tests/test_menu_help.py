"""Tab, in a menu: what the line you are standing on actually does.

A staff menu's labels have to be short enough to arrow past, so they name the
thing being set and cannot explain it -- "Set Speaker Crossover (bass only below
80 Hz)" tells a person what is stored and nothing to somebody who has never met
the word. The other half rides beside the line as a description the Server sends
with it, and Tab speaks the description of the line the cursor is on: asked for,
one line at a time, never read out with the label.

Two properties are what this pins, because both are easy to lose:

  * **asking changes nothing else** -- the cursor does not move, the line is not
    chosen, the menu is not closed, and no preview sound is played. A key that
    reads a description is a key that must be safe to press;
  * **a menu that describes nothing is untouched** -- no item grows a field, Tab
    does nothing in it (so a menu written before this existed behaves exactly as
    it did), and Tab still toggles the Match History pane in a minigame menu,
    which is what Tab has always done there.
"""

import os
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

import pygame

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from libs import event_handeler as event_handeler_module
from libs import menu as menu_module
from libs.menu import Menu

HELP_A = "Places one speaker of a cinema room, which is not a jukebox."
HELP_B = "How loud this one speaker plays, 0 to 200 percent."


class _Game:
    def __init__(self):
        self.direct_soundgroup = object()


def make_menu(items=None, menu_type="normal", game=None, parent=None):
    m = Menu(game or _Game(), "Technician Menu", parrent=parent)
    m.menu_type = menu_type
    if items is not None:
        m.items = list(items)
    return m


def tab(mod=pygame.KMOD_NONE):
    return pygame.event.Event(pygame.KEYDOWN, key=pygame.K_TAB, mod=mod)


class TabSpeaksTheLineTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(menu_module.speech, "speak")
        self.speak = patcher.start()
        self.addCleanup(patcher.stop)

    def test_tab_speaks_the_focused_lines_description(self):
        m = make_menu([
            ("Cinema Speaker (needs a Jukebox on the map)", lambda: None, None, HELP_A),
            ("Set Speaker Level (now: 100)", lambda: None, None, HELP_B),
        ])
        m.pos = 1
        m.update([tab()])
        self.assertEqual(self.speak.call_args_list[-1].args[0], HELP_B)
        self.assertEqual(self.speak.call_args_list[-1].kwargs.get("id"), "menu_help")

    def test_tab_reads_the_same_line_again_when_pressed_again(self):
        m = make_menu([("A line", lambda: None, None, HELP_A)])
        m.pos = 0
        m.update([tab()])
        m.update([tab()])
        self.assertEqual([c.args[0] for c in self.speak.call_args_list], [HELP_A, HELP_A])

    def test_asking_does_not_move_the_menu_or_choose_the_line(self):
        chosen = []
        parent = SimpleNamespace(pop_last_substate=mock.Mock())
        m = make_menu([
            ("First", lambda: chosen.append("first"), None, HELP_A),
            ("Second", lambda: chosen.append("second"), None, HELP_B),
        ], parent=parent)
        m.pos = 1
        m.update([tab()])
        self.assertEqual(m.pos, 1)
        self.assertEqual(chosen, [])
        parent.pop_last_substate.assert_not_called()

    def test_a_line_with_nothing_written_about_it_says_so(self):
        m = make_menu([
            ("Described", lambda: None, None, HELP_A),
            ("Undescribed", lambda: None, None),
        ])
        m.pos = 1
        m.update([tab()])
        self.assertEqual(self.speak.call_args_list[-1].args[0],
                         "No extra description for this line.")

    def test_tab_is_silent_in_a_menu_that_describes_nothing(self):
        """The menus that set no descriptions are exactly as they were."""
        m = make_menu([
            ("Zombie Spawn", lambda: None, None),
            ("Toggle Power Notification (Currently: On)", lambda: None, None),
        ])
        m.pos = 0
        self.assertFalse(m.menu_has_help())
        m.update([tab()])
        self.speak.assert_not_called()

    def test_tab_is_silent_when_no_line_is_focused(self):
        m = make_menu([("A line", lambda: None, None, HELP_A)])
        m.pos = -1
        m.update([tab()])
        self.speak.assert_not_called()

    def test_a_preview_sound_is_not_played_by_asking(self):
        """Tab reads; Space is what plays a preview, and that is unchanged."""
        m = make_menu([("A sound", lambda: None, "weapon/ak47.ogg", HELP_A)])
        m.pos = 0
        m.environmental_preview = True
        with mock.patch.object(m, "_ensure_preview_worker") as worker:
            m.update([tab()])
        worker.assert_not_called()

    def test_tab_still_toggles_match_history_in_a_minigame_menu(self):
        game = _Game()
        game.match_history = ["the last move"]
        m = make_menu([("Card Action", lambda: None, None, HELP_A)],
                      menu_type="match_play", game=game)
        m.pos = 0
        m.update([tab()])
        spoken = [c.args[0] for c in self.speak.call_args_list]
        self.assertIn("Match History. Press Up or Down to review. Press Tab for Card Actions.", spoken)
        self.assertNotIn(HELP_A, spoken)


class ItemShapeIsUnchangedTests(unittest.TestCase):
    def test_a_described_line_grows_a_fourth_field(self):
        m = Menu(_Game(), "m")
        m.add_item("Line", lambda: None, None, HELP_A)
        m.add_item("Plain line", lambda: None)
        self.assertEqual(len(m.items[0]), 4)
        self.assertEqual(m.items[0][3], HELP_A)
        self.assertEqual(len(m.items[1]), 3)


class ServerPayloadTests(unittest.TestCase):
    """What the Server sends decides, and a payload without descriptions builds
    the very items it always did."""

    def setUp(self):
        patcher = mock.patch.object(menu_module.speech, "speak")
        self.speak = patcher.start()
        self.addCleanup(patcher.stop)

    def handler(self):
        obj = object.__new__(event_handeler_module.EventHandeler)
        obj.game = _Game()
        obj.gameplay = SimpleNamespace(states=[], add_substate=lambda _s: None,
                                       pop_last_substate=lambda: None,
                                       in_minigame_match=False)
        obj.client = SimpleNamespace(send=lambda *args, **kwargs: None)
        return obj

    def test_only_described_lines_carry_the_description(self):
        handler = self.handler()
        captured = []
        handler.gameplay = SimpleNamespace(states=[],
                                           add_substate=lambda s: captured.append(s),
                                           pop_last_substate=lambda: None,
                                           in_minigame_match=False)
        handler.make_menu({
            "event": "builder_menu_select",
            "title": "Technician Menu",
            "menu_type": "normal",
            "options": [
                {"title": "Cinema Speaker (needs a Jukebox on the map)",
                 "value": "cinemaSpeaker", "close": True, "help": HELP_A},
                {"title": "Set Speaker Level (now: 100)",
                 "value": {"action": "set_cinema_level"}, "close": True, "help": HELP_B},
                {"title": "Reload Map Data", "value": "reloadMap", "close": True},
            ],
        })
        items = captured[-1].items
        self.assertEqual([len(i) for i in items[:3]], [4, 4, 3])
        self.assertEqual(items[0][3], HELP_A)
        self.assertEqual(items[1][3], HELP_B)

    def test_a_menu_with_no_descriptions_is_three_tuples_all_the_way(self):
        handler = self.handler()
        captured = []
        handler.gameplay = SimpleNamespace(states=[],
                                           add_substate=lambda s: captured.append(s),
                                           pop_last_substate=lambda: None,
                                           in_minigame_match=False)
        handler.make_menu({
            "event": "builder_menu_select",
            "title": "Technician Menu",
            "menu_type": "normal",
            "options": [
                {"title": "Reload Map Data", "value": "reloadMap", "close": True},
                {"title": "Toggle Map Public/Private (Currently: Public)",
                 "value": "toggleMapPublic", "close": True},
            ],
        })
        m = captured[-1]
        self.assertTrue(all(len(i) == 3 for i in m.items))
        self.assertFalse(m.menu_has_help())
        m.pos = 0
        m.update([tab()])
        self.speak.assert_not_called()

    def test_a_description_is_sent_as_text_and_survives_a_long_one(self):
        """The Server's paragraphs are spoken whole, not clipped."""
        long_text = ("This speaker keeps 200 Hz to 3 kHz: voices and most "
                     "instruments, with the deep end and the very top left to "
                     "other speakers. It is the usual mid, and it wants a sub "
                     "crossed at 200 rather than at 120.")
        handler = self.handler()
        captured = []
        handler.gameplay = SimpleNamespace(states=[],
                                           add_substate=lambda s: captured.append(s),
                                           pop_last_substate=lambda: None,
                                           in_minigame_match=False)
        handler.make_menu({
            "event": "builder_cinema_crossover",
            "title": "Select Cinema Speaker Crossover",
            "menu_type": "normal",
            "options": [
                {"title": "Mid only between 200 Hz and 3 kHz - the usual mid",
                 "value": {"stage": "cinemaCrossover", "crossover": 200},
                 "close": True, "help": long_text},
            ],
        })
        m = captured[-1]
        m.pos = 0
        m.update([tab()])
        self.assertEqual(self.speak.call_args_list[-1].args[0], long_text)


if __name__ == "__main__":
    unittest.main()
