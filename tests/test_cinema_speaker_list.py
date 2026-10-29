"""The technicians' cinema-speaker list, opened by Alt + C.

The Builder's Edit Map Elements is where a speaker is changed; this list is
where a room is checked. These pin the four things that make it useful rather
than a second way to open the builder:

    * it counts what the map placed and names every speaker by role and
      coordinates -- never by its element id, which speech engines read out
      as a string of letters;
    * Tab reads one speaker's room, level, delay, tone, crossover, aim and
      position; Enter asks the Server to open that speaker's own element menu
      instead, so the list itself writes nothing;
    * it is opened by the chord and not by a new keyconfig row, because the
      file holds one key per action and no modifiers;
    * it belongs to the same staff rule as the pan and the sound test.
"""

import json
import os
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

import pygame

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from libs import cinema_speaker_menu, consts
from libs.gameplay import Gameplay
from libs.menu import Menu
from libs.world_map import Map


class FakeMenu:
    """Drop-in for libs.menu.Menu capturing the title and items it was built with."""

    def __init__(self, game, title, **kwargs):
        self.game = game
        self.title = title
        self.items = []

    def add_items(self, items):
        self.items = list(items)

    def speak_current_item(self):
        pass

    def add_item(self, *args, **kwargs):
        pass


def make_game(speakers=None):
    game = SimpleNamespace()
    sent = []
    game.network = SimpleNamespace(send=lambda *args: sent.append(args))
    game.sent = sent
    game.direct_soundgroup = SimpleNamespace(play=lambda *args, **kwargs: None)
    map_obj = Map(game)
    for speaker in speakers or ():
        map_obj.spawn_cinemaSpeaker(**speaker)
    popped = []
    gameplay = SimpleNamespace(
        map=map_obj,
        can_use_cinema_pan=True,
        add_substate=None,
        pop_last_substate=lambda: popped.append(True),
    )
    game.gameplay = gameplay
    game.popped = popped
    gameplay.game = game
    return game


def speaker(speaker_id, channel, x, y, z=2, **extra):
    values = dict(minx=x - 0.5, maxx=x + 0.5,
                  miny=y - 0.5, maxy=y + 0.5,
                  minz=z, maxz=z + 1, x=x, y=y, z=z,
                  id=speaker_id, channel=channel, room="j1",
                  level=100, delay=0.0)
    values.update(extra)
    return values


class ListTests(unittest.TestCase):
    def setUp(self):
        self.game = make_game([
            speaker("cs_fl", "front_l", 21.5, 43, level=40, delay=0),
            speaker("cs_rl", "rear_l", 34, 51, level=30, delay=40,
                    tone=75, crossover=120),
        ])
        self.gp = self.game.gameplay
        self.captured = []
        self.gp.add_substate = self.captured.append

    def build(self):
        with mock.patch("libs.menu.Menu", FakeMenu), \
                mock.patch("libs.menus.set_default_sounds"):
            cinema_speaker_menu.open_menu(self.game, self.gp)
        return self.captured[-1]

    @staticmethod
    def labels(menu):
        return [label() if callable(label) else label
                for label, _action, *_extra in menu.items]

    def test_the_title_says_how_many_speakers_the_map_has(self):
        self.assertEqual(self.build().title,
                         "Cinema Speakers Menu (2 on this map)")

    def test_every_line_starts_with_the_speaker_and_its_coordinates(self):
        self.assertEqual(self.labels(self.build()), [
            "Speaker 1 of 2: front_l at (21.5, 43, 2)",
            "Speaker 2 of 2: rear_l at (34, 51, 2)",
            "Back",
        ])

    def test_tab_reads_the_rest_of_that_speakers_setup(self):
        menu = self.build()
        real = Menu(self.game, "Cinema Speakers", parrent=self.gp)
        real.items = menu.items
        real.pos = 1
        with mock.patch("libs.menu.speech.speak") as spoke:
            real.update([pygame.event.Event(pygame.KEYDOWN, key=pygame.K_TAB,
                                            mod=pygame.KMOD_NONE)])
        said = spoke.call_args.args[0]
        for expected in ("Speaker 2 of 2", "Channel rear_l",
                         "Room j1", "Level 30 percent", "Delay 40 milliseconds",
                         "Tone 75 percent", "Crossover: below 120 Hz",
                         "Aim: omnidirectional", "At X 34, Y 51, Z 2"):
            self.assertIn(expected, said)
        self.assertNotIn("cs_rl", said)
        self.assertNotIn("Element", said)

    def test_the_description_is_one_sentence_without_the_element_id(self):
        said = cinema_speaker_menu.detail({
            "id": "KyEQCFy5", "channel": "side_l", "room": "",
            "level": 200, "delay_ms": 0, "tone": 100,
            "crossover": 0, "crossover_high": 0,
            "x": 5, "y": 50, "z": 15,
        }, 1, 10)
        self.assertEqual(
            said,
            "Speaker 1 of 10. Channel side_l. Room none. Level 200 percent. "
            "Delay 0 milliseconds. Tone 100 percent. Crossover: full range. "
            "Aim: omnidirectional. At X 5, Y 50, Z 15.")
        self.assertNotIn("KyEQCFy5", said)
        self.assertNotIn("Element", said)

    def test_enter_opens_that_speakers_element_menu_on_the_server(self):
        self.build().items[1][1]()
        self.assertEqual(self.game.sent, [
            (consts.CHANNEL_MENUS, "edit_element_select", {
                "value": {"elementId": "cs_rl", "elementType": "CinemaSpeaker"},
                "cinemaList": True,
            }),
        ])
        self.assertEqual(self.game.popped, [True])

    def test_the_server_can_reopen_the_list_after_a_speaker_menu(self):
        """The list is rebuilt, not revealed under the Builder menus, so its title is heard."""
        from libs import event_handeler
        game = make_game([speaker("cs_fl", "front_l", 21, 43)])
        handler = event_handeler.EventHandeler.__new__(event_handeler.EventHandeler)
        handler.game = game
        handler.gameplay = game.gameplay
        with mock.patch.object(game.gameplay, "open_cinema_speaker_list",
                               create=True) as opened:
            handler.cinema_speaker_list({})
        opened.assert_called_once()

    def test_a_map_with_no_speakers_still_answers_the_count(self):
        empty = make_game([])
        captured = []
        empty.gameplay.add_substate = captured.append
        with mock.patch("libs.menu.Menu", FakeMenu), \
                mock.patch("libs.menus.set_default_sounds"):
            cinema_speaker_menu.open_menu(empty, empty.gameplay)
        menu = captured[-1]
        self.assertEqual(menu.title, "Cinema Speakers Menu (0 on this map)")
        self.assertEqual(self.labels(menu),
                         ["No cinema speakers on this map", "Back"])

    def test_opening_and_reading_the_list_sends_nothing(self):
        """The list is local until Enter asks for one speaker's edit menu."""
        self.build()
        self.assertEqual(self.game.sent, [])
        real = Menu(self.game, "Cinema Speakers", parrent=self.gp)
        real.items = self.captured[-1].items
        real.pos = 0
        with mock.patch("libs.menu.speech.speak"):
            real.update([pygame.event.Event(pygame.KEYDOWN, key=pygame.K_TAB,
                                            mod=pygame.KMOD_NONE)])
        self.assertEqual(self.game.sent, [])

    def test_enter_without_a_connection_says_so(self):
        menu = self.build()
        self.game.network = None
        with mock.patch("libs.cinema_speaker_menu.speak") as spoke:
            menu.items[0][1]()
        self.assertIn("connection to the Server", spoke.call_args.args[0])

    def test_an_account_without_the_staff_rule_is_refused_in_words(self):
        game = make_game([speaker("cs_fl", "front_l", 21, 43)])
        game.gameplay.can_use_cinema_pan = False
        game.gameplay.add_substate = lambda state: self.fail("menu must not open")
        with mock.patch("libs.cinema_speaker_menu.speak") as spoke:
            cinema_speaker_menu.open_menu(game, game.gameplay)
        spoke.assert_called_once_with(cinema_speaker_menu.REFUSAL)


class AltCTests(unittest.TestCase):
    def test_alt_c_opens_the_list_without_reading_the_location(self):
        opened = []
        gp = Gameplay.__new__(Gameplay)
        gp.open_cinema_speaker_list = lambda: opened.append(True)
        Gameplay.speak_location(gp, pygame.KMOD_ALT)
        self.assertEqual(opened, [True])

    def test_plain_c_still_reads_the_location(self):
        read = []
        gp = Gameplay.__new__(Gameplay)
        gp.open_cinema_speaker_list = lambda: read.append("list")
        world = SimpleNamespace(
            get_travel_point_at=lambda *args: None,
            get_zone_at=lambda *args: "Plaza",
            get_tile_at=lambda *args: "floor",
        )
        gp.camera = SimpleNamespace(focus_object=SimpleNamespace(
            x=10, y=20, z=0, hfacing=0, vfacing=0, bfacing=0, map=world,
        ))
        with mock.patch("libs.gameplay.speak") as spoke:
            Gameplay.speak_location(gp, 0)
        said = spoke.call_args.args[0]
        self.assertIn("10, \r\n20, \r\n0", said)
        self.assertIn("On floor", said)
        self.assertEqual(read, [])

    def test_the_chord_rides_the_location_key_and_needs_no_config_row(self):
        here = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(here, "..", "default_keyconfig.json"),
                  encoding="utf-8") as handle:
            bindings = json.load(handle)["bindings"]
        self.assertEqual(bindings["speak_location"], "c")
        self.assertNotIn("cinema_speaker_list", bindings)

    def test_the_world_method_hands_the_game_and_gameplay_to_the_menu(self):
        gp = Gameplay.__new__(Gameplay)
        gp.game = SimpleNamespace()
        with mock.patch("libs.cinema_speaker_menu.open_menu") as opened:
            Gameplay.open_cinema_speaker_list(gp)
        opened.assert_called_once_with(gp.game, gp)


if __name__ == "__main__":
    unittest.main()
