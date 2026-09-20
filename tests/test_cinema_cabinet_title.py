"""A cabinet's own name says whether it is a plain jukebox or a room.

The same key opens a cabinet's menu whether the map gave it a cinema room or
not, and until now both answered to "Music Jukebox". The title is the first
thing a person hears when they walk up to one, so it is where the answer
belongs -- and it is the one line that costs nothing to check:

    * a cabinet the map set to ``off``, one with nothing standing around it,
      and one whose position is not known yet keep the plain name, so every
      cabinet that shipped before this feature reads exactly as it did;
    * a cabinet that resolves a room says so, and names the **mode itself** --
      the same token the mode picker offers and the map stores (``front_only``,
      ``theatre``, ``auto``): Auto says ``auto`` rather than the profile it
      happened to resolve to, and no title ever describes the speakers instead
      of the mode, because the line underneath it names that same mode and two
      descriptions of one cabinet read as two different settings;
    * the title says what the CABINET is and never what these particular ears
      do with it: a listener who switched rooms off still reads the cabinet's
      own name, because the ``Cinema:`` line and the detail read-out are what
      explain the listening, and two people standing at one cabinet must not
      disagree about what it is.
"""

import os
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from libs import jukebox
from libs.audio.cinema.layout import CinemaLayout
from libs.audio.cinema.profiles import get_profile, profile_names
from test_cinema_room_scope import build

CABINET = (10.0, 20.0)
ANCHOR = (10.0, 20.0, 0.0)


class FakeMenu:
    """The real menu, with the one thing these tests read kept: its title."""

    last = None

    def __init__(self, game, title, *args, **kwargs):
        self.title = title
        self.items = []
        FakeMenu.last = self

    def add_items(self, items):
        self.items = list(items)

    def speak_current_item(self):
        pass

    def labels(self):
        return [label() if callable(label) else label for label, _action in self.items]


def open_cabinet_menu(game):
    """Open the cabinet's own menu on a game and hand back what it built."""
    with mock.patch("libs.menu.Menu", FakeMenu), \
            mock.patch("libs.menus.set_default_sounds"):
        jukebox.open_jukebox_menu(game, game.gameplay)
    return FakeMenu.last


def cabinet_game(*, speakers=(), modes=None):
    """A game with one cabinet, whatever speakers stand around it, and a player.

    The player stands at the cabinet because that is what the key does: the
    menu is opened for the *nearest* box, and a test standing nowhere would be
    asking about a cabinet nobody walked up to.
    """
    game, _map = build(cabinets=(CABINET,), speakers=speakers, modes=modes)
    gp = game.gameplay
    gp.player = SimpleNamespace(name="Kanya", x=CABINET[0], y=CABINET[1], z=0.0)
    gp.substates = []
    gp.add_substate = gp.substates.append
    gp.pop_last_substate = lambda: None
    return game


def slot_speakers(slots):
    """Map speakers standing where the room itself would put those slots.

    The ring the layout falls back to is the project's own answer to "where
    does a side speaker stand", so a test that wants a real room on a map asks
    it rather than inventing bearings of its own.
    """
    ring = CinemaLayout(ANCHOR)
    placed = []
    for slot in slots:
        x, y, z = ring.position(slot)
        placed.append(dict(x=x, y=y, z=z, channel=slot))
    return placed


FRONT_ROOM = slot_speakers(("front_l", "front_r"))
FULL_ROOM = slot_speakers(("front_l", "front_c", "front_r", "side_l", "side_r",
                           "rear_l", "rear_r"))


class APlainCabinetKeepsThePlainNameTests(unittest.TestCase):
    """Nothing about a cabinet that shipped before this feature changes."""

    def test_a_cabinet_with_nothing_around_it_is_a_music_jukebox(self):
        menu = open_cabinet_menu(cabinet_game())
        self.assertEqual(menu.title, "Music Jukebox")

    def test_a_cabinet_the_map_turned_off_is_a_music_jukebox(self):
        game = cabinet_game(speakers=FRONT_ROOM, modes={"j1": "off"})
        self.assertEqual(jukebox._cabinet_title(game, game.gameplay, "j1"),
                         "Music Jukebox")
        self.assertEqual(open_cabinet_menu(game).title, "Music Jukebox")

    def test_a_cabinet_whose_position_is_not_known_is_a_music_jukebox(self):
        game = cabinet_game(speakers=FRONT_ROOM, modes={"j1": "theatre"})
        game.gameplay.map = None
        self.assertEqual(jukebox._cabinet_title(game, game.gameplay, "j1"),
                         "Music Jukebox")

    def test_a_map_that_breaks_the_resolver_is_still_a_music_jukebox(self):
        # A title is read out the moment a menu opens, so no map data is worth
        # breaking that for: the plain name is the safe answer.
        game = cabinet_game(speakers=FRONT_ROOM)
        with mock.patch.object(jukebox, "room_plan", side_effect=RuntimeError):
            self.assertEqual(jukebox._cabinet_title(game, game.gameplay, "j1"),
                             "Music Jukebox")


class ARoomNamesItselfTests(unittest.TestCase):
    """A cabinet that plays through a room says so, in somebody else's words."""

    def title(self, game, mode=None):
        return jukebox._cabinet_title(game, game.gameplay, "j1", mode)

    def test_auto_with_a_room_says_auto(self):
        game = cabinet_game(speakers=FRONT_ROOM)
        self.assertEqual(self.title(game), "Cinema Jukebox: auto")

    def test_auto_does_not_borrow_the_shape_it_resolved_to(self):
        # The room a front pair makes resolves to `front_only`, and nobody
        # chose that: neither the title nor the mode may claim it, or a cabinet
        # left on Auto would answer with a shape it was never set to.
        game = cabinet_game(speakers=FRONT_ROOM)
        self.assertEqual(self.title(game), "Cinema Jukebox: auto")
        self.assertNotIn(get_profile("front_only").label, self.title(game))

    def test_an_explicit_shape_says_the_mode(self):
        game = cabinet_game(speakers=FULL_ROOM, modes={"j1": "theatre"})
        self.assertEqual(self.title(game), "Cinema Jukebox: theatre")

    def test_every_shape_that_plays_says_the_mode_it_was_set_to(self):
        # A shape asked for outlives the map: with no speakers at all it plays
        # the ring behind the cabinet, so every profile has a title -- and each
        # one is the mode, never a description of the speakers it feeds.
        game = cabinet_game()
        for name in profile_names():
            self.assertEqual(self.title(game, name),
                             f"Cinema Jukebox: {name}", name)

    def test_the_title_says_exactly_what_the_mode_picker_offers(self):
        # One vocabulary: whatever the picker writes into the map is the word
        # on the title, so a mode renamed anywhere else cannot leave the title
        # pointing at a shape the cabinet is not set to.
        game = cabinet_game(speakers=FRONT_ROOM)
        values = [value for value, _label in jukebox._cinema_mode_choices(
            game, game.gameplay, "j1")[1]]
        self.assertTrue(values)
        for value in values:
            if value == "off":
                continue
            self.assertIn(self.title(game, value).split(": ")[-1], values)

    def test_a_shape_the_map_cannot_make_still_says_which_shape_plays(self):
        # `theatre` asked for where the map has a front pair: the ring plays
        # it, and the title names the shape that is heard, not the map's pair.
        game = cabinet_game(speakers=FRONT_ROOM, modes={"j1": "theatre"})
        self.assertEqual(self.title(game), "Cinema Jukebox: theatre")


class TheMenuItselfTests(unittest.TestCase):
    """The title is what the cabinet's own menu is opened with."""

    def test_a_room_cabinet_opens_titled_after_its_mode(self):
        game = cabinet_game(speakers=FULL_ROOM, modes={"j1": "surround"})
        menu = open_cabinet_menu(game)
        self.assertEqual(menu.title, "Cinema Jukebox: surround")

    def test_the_read_out_lines_are_unchanged_by_the_name(self):
        game = cabinet_game(speakers=FULL_ROOM, modes={"j1": "surround"})
        labels = open_cabinet_menu(game).labels()
        self.assertIn("Cinema: surround - that room shape, speakers or not", labels)
        self.assertIn("View queue", labels)

    def test_a_listener_who_switched_rooms_off_still_reads_the_cabinets_name(self):
        # The switch decides what these ears hear; the title says what the
        # cabinet is. The two questions are answered on two different lines.
        game = cabinet_game(speakers=FULL_ROOM, modes={"j1": "theatre"})
        with mock.patch("libs.audio.cinema.plugin._option_enabled",
                        return_value=False):
            menu = open_cabinet_menu(game)
        self.assertEqual(menu.title, "Cinema Jukebox: theatre")


if __name__ == "__main__":
    unittest.main()
