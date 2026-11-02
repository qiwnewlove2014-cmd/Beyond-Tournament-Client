"""Tab, at a Jukebox cabinet: every line explains itself.

A cabinet label has to stay short enough to arrow past -- "Set cinema reach
(now: 60 m)" names the setting and explains nothing -- so the explanation rides
beside the line as the fourth menu field, and Tab speaks the one the cursor is
on (``libs.menu.speak_current_help``). This is the same contract the Builder
and Music Bot menus keep: every line of every page carries a description, and a
line added without one is a line only its author can use.

The test builds the pages without a game, map, audio device or network: the
menu module only needs a fake menu to record what it was handed.
"""

import os
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from libs import jukebox  # noqa: E402
from libs.jukebox import cinema, menu as jukebox_menu  # noqa: E402


class FakeMenu:
    """The menu the cabinet code would have shown, with its lines kept."""

    def __init__(self, game, title, *args, **kwargs):
        self.game = game
        self.title = title
        self.items = []

    def add_items(self, items):
        self.items = list(items)

    def speak_current_item(self):
        pass

    def menu_has_help(self):
        return any(len(item) > 3 and item[3] for item in self.items)


def label_of(item):
    return item[0]() if callable(item[0]) else item[0]


def text_of(item):
    description = item[3] if len(item) > 3 else ""
    return description() if callable(description) else str(description or "")


def make_game():
    box = {
        "id": "cab1",
        "x": 0.0,
        "y": 0.0,
        "z": 0.0,
        "cinema_mode": "auto",
        "cinema_vocal": "left",
        "current": {"title": "A Song", "duration": 180, "queuedBy": "Kanya"},
        "queue": [{"title": "Next Song", "queuedBy": "Kanya"}],
        "repeat": "all",
        "volume": 70,
        "eq_profile": "normal",
    }
    gp = SimpleNamespace(
        jukebox_state={"jukeboxes": {"cab1": box}},
        player=SimpleNamespace(name="Kanya", x=0.0, y=0.0, z=0.0),
        substates=[],
        pop_last_substate=lambda: None,
        is_staff=True,
        is_builder=True,
        can_lock_cinema_mode=True,
    )
    gp.add_substate = gp.substates.append
    game = SimpleNamespace(gameplay=gp)
    gp.game = game
    return game


class EveryCabinetLineIsDescribedTests(unittest.TestCase):
    def assert_described(self, menu):
        self.assertTrue(menu.items, "the menu has no lines")
        self.assertTrue(menu.menu_has_help(), "the menu answers no Tab at all")
        for item in menu.items:
            with self.subTest(line=label_of(item)):
                self.assertGreaterEqual(
                    len(item), 4, f"{label_of(item)!r} carries no description")
                self.assertTrue(text_of(item).strip(),
                                f"{label_of(item)!r} has a blank description")

    def open_menu(self, call):
        game = make_game()
        with mock.patch("libs.menu.Menu", FakeMenu), \
                mock.patch("libs.menus.set_default_sounds"), \
                mock.patch("libs.jukebox.menu._cabinet_title",
                           return_value="Music Jukebox"), \
                mock.patch("libs.jukebox.menu._cabinet_cinema_reach",
                           return_value=60.0), \
                mock.patch("libs.jukebox.menu._scrub_spot",
                           return_value=(10.0, 30.0)), \
                mock.patch("libs.jukebox.menu.neighbour_line",
                           return_value="Another cabinet at 10 m (j2)"), \
                mock.patch("libs.jukebox.menu.speak"):
            call(game, game.gameplay)
        self.assert_described(game.gameplay.substates[-1])

    def test_the_cabinet_menu(self):
        self.open_menu(lambda game, gp: jukebox.open_jukebox_menu(game, gp))

    def test_the_dark_cabinet_switch_menu(self):
        self.open_menu(
            lambda game, gp: jukebox.open_jukebox_switch_menu(game, gp, "cab1"))

    def test_the_search_results_menu(self):
        def call(game, gp):
            jukebox_menu._show_results_menu(
                game, gp, "cab1",
                [{"title": "A Song", "duration": 180},
                 {"title": "A Live Set", "duration": 0, "is_live": True}])
        self.open_menu(call)

    def test_the_queue_menu(self):
        self.open_menu(lambda game, gp: jukebox_menu._view_queue(game, gp))

    def test_the_remove_my_song_menu(self):
        self.open_menu(lambda game, gp: jukebox_menu._remove_my_song(game, gp))

    def test_the_cinema_pages(self):
        for name, opener in (
                ("mode", cinema._open_cinema_mode_menu),
                ("reach", cinema._open_cinema_reach_menu),
                ("vocal", cinema._open_cinema_vocal_menu)):
            with self.subTest(page=name):
                game = make_game()
                with mock.patch("libs.menu.Menu", FakeMenu), \
                        mock.patch("libs.menus.set_default_sounds"), \
                        mock.patch("libs.jukebox.cinema._room_extent",
                                   return_value=None), \
                        mock.patch("libs.jukebox.cinema._cabinet_cinema_reach",
                                   return_value=60.0), \
                        mock.patch("libs.jukebox.cinema.cabinet_anchor",
                                   return_value=None), \
                        mock.patch("libs.jukebox.cinema.speak"):
                    opener(game, game.gameplay, "cab1")
                self.assert_described(game.gameplay.substates[-1])

    def test_the_vocal_line_explains_what_bleeds(self):
        game = make_game()
        with mock.patch("libs.menu.Menu", FakeMenu), \
                mock.patch("libs.menus.set_default_sounds"), \
                mock.patch("libs.jukebox.menu._cabinet_title",
                           return_value="Music Jukebox"), \
                mock.patch("libs.jukebox.menu._cabinet_cinema_reach",
                           return_value=60.0), \
                mock.patch("libs.jukebox.menu._scrub_spot",
                           return_value=None), \
                mock.patch("libs.jukebox.menu.speak"):
            jukebox.open_jukebox_menu(game, game.gameplay)
        line = next(item for item in game.gameplay.substates[-1].items
                    if label_of(item).startswith("Set vocal focus"))
        self.assertIn("not a stem separator", text_of(line))
        self.assertIn("centre-panned", text_of(line))


if __name__ == "__main__":
    unittest.main()
