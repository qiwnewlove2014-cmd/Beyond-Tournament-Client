"""Locking a cabinet's cinema mode, from the listener's own client.

The mode a cabinet plays through is the map's, and staff pick it where they
stand. A venue that has been tuned should not be re-tuned mid-show by anybody
passing by, so a developer can put a *code* on one cabinet's mode. The Server
owns that lock (the code's hash is written into the map and never leaves the
Server), so everything here is about the client half of the same bargain:

    * the lock is read out of the state the Server sent -- the cabinet menu can
      say who holds it without ever holding a code itself;
    * the line that *manages* the lock exists only for the account the Server
      says may use it (``can_lock_cinema_mode``), because a menu line that
      exists is a line that works -- and a locked cabinet still says who to ask
      to everybody else;
    * a pick that will need the code says so *before* it is made, so the box is
      never a surprise;
    * the request goes to the Server, which answers the same rank again; and
    * none of it touches how a listener hears: the room lines are the same
      lines, because a lock decides who may re-tune a cabinet and never whether
      a room is heard.
"""

import os
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from libs import jukebox


class FakeNetwork:
    def __init__(self):
        self.sent = []

    def send(self, channel, event, data=None):
        self.sent.append((channel, event, data))


def make_game(state, *, name="Kanya", can_lock=False):
    gp = SimpleNamespace(
        jukebox_state=state,
        player=SimpleNamespace(name=name, x=0.0, y=0.0, z=0.0),
        substates=[],
        can_lock_cinema_mode=can_lock,
        pop_last_substate=lambda: None,
    )
    gp.add_substate = gp.substates.append
    game = SimpleNamespace(gameplay=gp, network=FakeNetwork())
    gp.game = game
    return game


def state_with(lock=None, cabinet=("cab1", 0.0, 0.0, 0.0)):
    box = {"id": cabinet[0], "x": cabinet[1], "y": cabinet[2], "z": cabinet[3],
           "cinema_mode": "auto", "current": None, "queue": []}
    if lock is not None:
        box["cinema_lock"] = lock
    return {"jukeboxes": {cabinet[0]: box}}


class CabinetLockReadTests(unittest.TestCase):
    """What the client knows about a lock, and what it must never know."""

    def test_no_lock_is_no_lock(self):
        gp = make_game(state_with()).gameplay
        self.assertIsNone(jukebox._cabinet_cinema_lock(gp, "cab1"))

    def test_a_lock_reads_as_its_owner_and_that_a_code_exists(self):
        gp = make_game(state_with({"owner": "stone", "coded": True})).gameplay
        self.assertEqual(jukebox._cabinet_cinema_lock(gp, "cab1"), ("stone", True))

    def test_an_owner_with_no_coded_flag_still_counts_as_coded(self):
        # A Server that predates the flag has only ever written locks with a
        # code, so the safe reading of a missing flag is "a code exists".
        gp = make_game(state_with({"owner": "stone"})).gameplay
        self.assertEqual(jukebox._cabinet_cinema_lock(gp, "cab1"), ("stone", True))

    def test_junk_is_not_a_lock(self):
        for junk in ({}, "stone", 7, {"owner": ""}, {"owner": None}, []):
            gp = make_game(state_with(junk)).gameplay
            self.assertIsNone(jukebox._cabinet_cinema_lock(gp, "cab1"), junk)

    def test_a_state_with_no_boxes_is_no_lock(self):
        gp = make_game({}).gameplay
        self.assertIsNone(jukebox._cabinet_cinema_lock(gp, "cab1"))

    def test_the_hash_the_server_must_never_send_would_not_be_read_as_one(self):
        # If a Server ever did ship the record itself, the client still reads
        # only the owner out of it -- there is nowhere for a code to land.
        gp = make_game(state_with({"owner": "stone", "password": "deadbeef"})).gameplay
        self.assertEqual(jukebox._cabinet_cinema_lock(gp, "cab1"), ("stone", True))


class RankTests(unittest.TestCase):
    """The rank is the Server's answer, never a guess from another flag."""

    def test_no_flag_means_no_line(self):
        gp = make_game(state_with()).gameplay
        self.assertFalse(jukebox._may_lock_cinema_mode(gp))

    def test_the_flag_is_the_only_thing_read(self):
        gp = make_game(state_with(), can_lock=True).gameplay
        gp.is_staff = True
        gp.is_builder = True
        gp.is_technician = True
        self.assertTrue(jukebox._may_lock_cinema_mode(gp))
        gp2 = make_game(state_with()).gameplay
        gp2.is_staff = True
        gp2.is_builder = True
        gp2.is_technician = True
        self.assertFalse(jukebox._may_lock_cinema_mode(gp2))

    def test_our_own_name_comes_from_login(self):
        gp = make_game(state_with(), name="memo").gameplay
        self.assertEqual(jukebox._own_name(gp), "memo")
        gp2 = make_game(state_with(), name="").gameplay
        self.assertEqual(jukebox._own_name(gp2), "")


class ModeChoicesTests(unittest.TestCase):
    """A pick that will need a code says so before it is made."""

    def choices(self, gp, jukebox_id="cab1"):
        # No game: the room preview is a separate question, and this test is
        # about the words the lock puts on the lines.
        return jukebox._cinema_mode_choices(None, gp, jukebox_id)[1]

    def test_an_unlocked_cabinet_says_nothing_about_codes(self):
        gp = make_game(state_with()).gameplay
        labels = [label for _value, label in self.choices(gp)]
        self.assertTrue(labels)
        self.assertEqual([l for l in labels if "code" in l], [])

    def test_a_stranger_is_told_every_line_will_need_the_code(self):
        gp = make_game(state_with({"owner": "stone", "coded": True})).gameplay
        labels = [label for _value, label in self.choices(gp)]
        self.assertTrue(labels)
        self.assertEqual([l for l in labels if "needs stone's code" in l], labels)

    def test_the_owner_is_not_told_they_need_their_own_code(self):
        gp = make_game(state_with({"owner": "stone", "coded": True}),
                       name="stone").gameplay
        labels = [label for _value, label in self.choices(gp)]
        self.assertEqual([l for l in labels if "code" in l], [])

    def test_an_account_that_may_lock_is_not_asked_for_a_code(self):
        gp = make_game(state_with({"owner": "stone", "coded": True}),
                       can_lock=True).gameplay
        labels = [label for _value, label in self.choices(gp)]
        self.assertEqual([l for l in labels if "code" in l], [])

    def test_the_modes_are_still_offered_while_locked(self):
        # The lock asks *after* the pick: hiding the picker would make the lock
        # look like a broken menu, and the Server is what refuses or asks.
        gp = make_game(state_with({"owner": "stone", "coded": True})).gameplay
        values = [value for value, _label in self.choices(gp)]
        self.assertIn(jukebox.CINEMA_AUTO, values)
        self.assertIn(jukebox.CINEMA_OFF, values)


class CabinetMenuTests(unittest.TestCase):
    """The lines a person at the cabinet actually reads."""

    class FakeMenu:
        def __init__(self, *args, **kwargs):
            self.items = []

        def add_items(self, items):
            self.items = list(items)

        def speak_current_item(self):
            pass

    def labels(self, game):
        with mock.patch("libs.menu.Menu", self.FakeMenu), \
                mock.patch("libs.menus.set_default_sounds"):
            jukebox.open_jukebox_menu(game, game.gameplay)
        menu = game.gameplay.substates[-1]
        return [label() if callable(label) else label for label, _action in menu.items]

    def test_a_developer_at_a_free_cabinet_is_offered_the_lock(self):
        game = make_game(state_with(), can_lock=True)
        self.assertIn("Lock cinema mode (set a code)…", self.labels(game))

    def test_a_technician_at_a_free_cabinet_is_not_offered_it(self):
        game = make_game(state_with())
        labels = self.labels(game)
        self.assertEqual([l for l in labels if "Lock cinema mode" in l], [])

    def test_a_locked_cabinet_tells_somebody_else_who_to_ask(self):
        game = make_game(state_with({"owner": "stone", "coded": True}))
        labels = self.labels(game)
        self.assertIn("Cinema mode is locked by stone (the code opens one change)",
                      labels)

    def test_the_owner_gets_the_controls_instead_of_the_read_out(self):
        game = make_game(state_with({"owner": "stone", "coded": True}), name="stone")
        labels = self.labels(game)
        self.assertIn("Manage cinema mode lock (owner: stone)…", labels)

    def test_a_developer_gets_the_controls_for_somebody_elses_lock(self):
        game = make_game(state_with({"owner": "stone", "coded": True}),
                         name="memo", can_lock=True)
        self.assertIn("Manage cinema mode lock (owner: stone)…", self.labels(game))

    def test_the_room_lines_are_untouched_by_any_of_it(self):
        # A lock is about who may re-tune a cabinet, never about whether a room
        # is heard: the lines a staff member's ears depend on are the same ones.
        for game in (make_game(state_with(), can_lock=True),
                     make_game(state_with({"owner": "stone", "coded": True}))):
            game.gameplay.is_staff = True
            labels = self.labels(game)
            self.assertTrue([l for l in labels if l.startswith("Cinema: ")])
            self.assertIn("Set cinema mode (now: auto)", labels)

    def test_a_player_who_is_not_staff_sees_no_room_lines(self):
        # A lock is the one room line everybody is meant to read: it says who
        # to ask. The mode it holds, and the read-out that describes the room
        # itself, are staff's -- a player is told nothing about the plumbing.
        game = make_game(state_with({"owner": "stone", "coded": True}))
        game.gameplay.is_staff = False
        game.gameplay.is_builder = False
        game.gameplay.is_technician = False
        labels = self.labels(game)
        self.assertIn("Cinema mode is locked by stone (the code opens one change)",
                      labels)
        self.assertEqual([l for l in labels if l.startswith("Cinema:")], [],
                         "the room's read-out is a staff line")
        self.assertEqual([l for l in labels if l.startswith("Set cinema")], [])


class LockRequestTests(unittest.TestCase):
    """The controls come from the Server, so asking is one packet."""

    def test_asking_for_the_lock_menu_names_one_cabinet(self):
        game = make_game(state_with(), can_lock=True)
        jukebox._open_cinema_lock_menu(game, game.gameplay, "cab1")
        channel, event, data = game.network.sent[-1]
        self.assertEqual(event, "builder_cinema_mode_lock")
        self.assertEqual(data, {"elementId": "cab1"})

    def test_the_client_never_sends_a_code_on_its_own_errand(self):
        # A code is answered in the Server's own box (builder_lock_input), so
        # nothing on this path may carry one.
        game = make_game(state_with(), can_lock=True)
        jukebox._open_cinema_lock_menu(game, game.gameplay, "cab1")
        _channel, _event, data = game.network.sent[-1]
        self.assertNotIn("code", data)
        self.assertNotIn("password", data)


if __name__ == "__main__":
    unittest.main()
