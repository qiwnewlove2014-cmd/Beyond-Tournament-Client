"""The staff sound test: one short note, fired at a cabinet, answered by everyone.

A pan -- and the room a band plays through -- is resolved on each *listener's*
own machine, so "did that work" cannot be answered from one client: two clients
open on one desk hear the pan land on both and on neither, and a switch that is
off, ears out of the room's reach and an old build all look alike. These cover
the client half of the answer:

  * one short note is played through the *very* route a panned band travels
    (``live.route_to_room`` with ``pan=(cabinet, direction)``), so what a test
    sounds like is what the pan sounds like -- and the note is short, damped a
    moment later, so two shots are two notes;
  * this machine reports what *it* did: the speakers it reached, or the one
    reason it reached none (its own switch, a cabinet that is not here, a room
    that does not resolve, ears out of reach) -- four different things to fix,
    so they are never collapsed into "nothing";
  * the shot itself is the Server's (it knows who is listening and owns the rate
    limit), and the note is a *note at the speakers*: nothing here touches a
    room's frame queue, so a song in progress is left exactly as it was;
  * the answer comes back as one line ("heard 3/4") with the per-client reasons
    behind one press, because a tester firing at five cabinets wants the count
    first and the names only for the misses.
"""

import os
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from libs.audio.cinema import sound_test as cinema_sound_test
from libs.audio.cinema import plugin as cinema_plugin
from libs.world_map import Map

ANCHOR = (10.0, 20.0, 0.0)
OTHER_ANCHOR = (200.0, 300.0, 0.0)
LIVE_KEY = cinema_sound_test.__dict__.get("LIVE_KEY") or "cinema_live_instruments"

_SNAPSHOT = None


def setUpModule():
    global _SNAPSHOT
    from libs import options
    _SNAPSHOT = options.prefs.get("cinema_live_instruments")


def tearDownModule():
    from libs import options
    if _SNAPSHOT is None:
        options.prefs.pop("cinema_live_instruments", None)
    else:
        options.prefs["cinema_live_instruments"] = _SNAPSHOT


class FakeSource:
    def __init__(self):
        self.position = None
        self.pitch = None
        self.gain = 1.0
        self.direct_filter = None

    def set(self, name, value):
        setattr(self, name, value)

    def play(self):
        return None

    def stop(self):
        return None

    def delete(self):
        return None


class FakeContext:
    def __init__(self):
        self.created = []

    def gen_source(self, **kwargs):
        source = FakeSource()
        self.created.append(source)
        return source


class FakeEfx:
    def __init__(self):
        self.sends = []

    def send(self, source, index, slot, filter=None):
        self.sends.append((source, index, slot))


class FakeAudio:
    def __init__(self, position=(10.0, 25.0, 0.0)):
        self.position = position
        self.volume_categories = {"jukebox": [100], "music": [100],
                                  "miscelaneous": [100], "master": [100]}
        self.played = []
        self.filter = []
        self.context = FakeContext()
        self.efx = FakeEfx()

    def play_unbound(self, path, x, y, z, **kwargs):
        self.played.append((path, (x, y, z), kwargs))
        return SimpleNamespace(source=FakeSource())

    def gen_filter(self, kind, *params):
        return ("filter", kind, params)


class FakeBot:
    def __init__(self, volume=50):
        self.volume = volume


class FakeNetwork:
    def __init__(self):
        self.sent = []

    def send(self, channel, event, data=None):
        self.sent.append((channel, event, data))


class FakeGameplay(SimpleNamespace):
    """The gameplay object as much as a menu needs: sub-states and a game."""

    def add_substate(self, menu_obj):
        self.substates = list(getattr(self, "substates", [])) + [menu_obj]

    def pop_last_substate(self):
        self.popped = getattr(self, "popped", 0) + 1
        return None


def make_game(speakers=(("front_l", 6.5, 26.5), ("front_r", 13.5, 26.5)),
              cabinets=(("j1", ANCHOR),), position=(10.0, 25.0, 0.0),
              live=True, modes=None, trims=None):
    """A map with a cabinet, its room, and one listener standing on it."""
    from libs import options
    options.prefs["cinema_live_instruments"] = bool(live)
    game = SimpleNamespace()
    game.audio_mngr = FakeAudio(position)
    map_obj = Map(game)
    for speaker in speakers:
        name, x, y = speaker[0], speaker[-2], speaker[-1]
        channel = speaker[1] if len(speaker) > 3 else name
        level, delay = dict(trims or {}).get(name, (100.0, 0.0))
        map_obj.spawn_cinemaSpeaker(minx=x - 0.5, maxx=x + 0.5, miny=y - 0.5,
                                    maxy=y + 0.5, minz=0, maxz=1, id=name,
                                    channel=channel, level=level, delay=delay)
    map_obj.jukebox_list = [SimpleNamespace(id=cabinet_id, center=center)
                            for cabinet_id, center in cabinets]
    modes = dict(modes or {})
    player = SimpleNamespace(
        cinema_mode=lambda cabinet_id: modes.get(cabinet_id, "auto"),
        occlusion_tier=lambda *args: 0)
    game.gameplay = FakeGameplay(map=map_obj, music_bot=FakeBot(),
                                 jukebox_player=player, game=game,
                                 megaphone=None, can_use_cinema_pan=True)
    game.called_after = []
    game.call_after = lambda ms, fn: game.called_after.append((ms, fn))
    game.network = FakeNetwork()
    game.put = lambda fn: fn()
    # The audio manager always owns a piano (libs/audio_manager.py): it is what
    # plays a note at a room's speakers, so the test's own fake needs one too.
    game.audio_mngr.piano = piano_for(game)
    return game


def piano_for(game):
    from libs.piano import PianoAudio
    piano = PianoAudio(game.audio_mngr)
    piano.gameplay = game.gameplay
    return piano


class OneShortNoteTests(unittest.TestCase):
    """The note itself: the band's own route, aimed at a named cabinet."""

    def two_cabinets(self, position=(10.0, 25.0, 0.0)):
        return make_game(
            cabinets=(("j1", ANCHOR), ("j2", OTHER_ANCHOR)),
            speakers=(("a_l", "front_l", 6.5, 26.5),
                      ("a_r", "front_r", 13.5, 26.5),
                      ("b_l", "front_l", 196.5, 306.5),
                      ("b_r", "front_r", 203.5, 306.5)),
            position=position)

    def test_the_note_plays_at_the_named_cabinets_speakers(self):
        game = self.two_cabinets(position=(200.0, 305.0, 0.0))
        piano = game.audio_mngr.piano
        self.assertEqual(piano.route_test_note_to_room("j2", "left", OTHER_ANCHOR),
                         2)
        # ...and every one of them stands by j2, not by the cabinet nearest the
        # listener: the destination is the one that was named.
        for _path, spot, _kwargs in game.audio_mngr.played:
            self.assertGreater(spot[0], 100.0)
        # Flat at the source and split like the song: the room's own shape.
        _path, _spot, kwargs = game.audio_mngr.played[0]
        self.assertEqual(kwargs["rolloff"], 0.0)
        self.assertIn("stereo_provider", kwargs)

    def test_a_destination_out_of_reach_plays_nothing_where_it_stands(self):
        """The other half of "the destination is named": j2, heard here as silence.

        Standing by j1 and firing at j2 must do nothing at these ears (j2's room
        is a long way off), where the *same* shot aimed at j1 plays two samples
        -- so the silence is the named destination rather than a broken route.
        """
        game = self.two_cabinets()
        piano = game.audio_mngr.piano
        self.assertEqual(piano.route_test_note_to_room("j2", "left", OTHER_ANCHOR),
                         0)
        self.assertEqual(game.audio_mngr.played, [])
        self.assertEqual(piano.route_test_note_to_room("j1", "left", ANCHOR), 2)
        self.assertEqual(len(game.audio_mngr.played), 2)

    def test_the_note_is_short_and_two_shots_are_two_notes(self):
        game = make_game()
        piano = piano_for(game)
        piano.route_test_note_to_room("j1", "auto", ANCHOR)
        self.assertTrue(game.called_after, "the note is damped a moment later")
        self.assertLessEqual(game.called_after[0][0], 600)
        game.called_after[0][1]()          # ...and the damp really stops it
        self.assertNotIn("cin-cinema-test-C4", piano.active_piano_notes)

    def test_a_song_in_progress_is_never_touched(self):
        """The test is a note *at* the speakers, not a frame in a room's queue.

        Feeding the room -- a cabinet's own song or the Music Bot routing -- is
        what takes the speakers over, and a test must not be able to do that:
        nothing in this path may acquire a bank or a renderer.
        """
        game = make_game()
        piano = piano_for(game)
        with mock.patch.object(cinema_plugin, "acquire_bank") as acquire:
            with mock.patch.object(cinema_plugin, "acquire_renderer") as renderer:
                piano.route_test_note_to_room("j1", "auto", ANCHOR)
        self.assertFalse(acquire.called)
        self.assertFalse(renderer.called)


class WhatThisMachineDidTests(unittest.TestCase):
    """The report: four different reasons, never collapsed into "nothing"."""

    def test_heard_names_the_speakers_it_reached(self):
        game = make_game()
        report = cinema_sound_test.play(game, game.gameplay, "j1", "auto")
        self.assertTrue(report["heard"])
        self.assertEqual(report["speakers"], 2)
        self.assertEqual(report["reason"], "")

    def test_the_listeners_own_switch_is_its_own_answer(self):
        game = make_game(live=False)
        report = cinema_sound_test.play(game, game.gameplay, "j1", "auto")
        self.assertFalse(report["heard"])
        self.assertIn("Instruments", report["reason"])

    def test_a_cabinet_this_map_does_not_have_says_so(self):
        game = make_game()
        report = cinema_sound_test.play(game, game.gameplay, "j9", "auto")
        self.assertFalse(report["heard"])
        self.assertIn("j9", report["reason"])

    def test_a_room_that_does_not_resolve_reports_the_diagnosis(self):
        # One speaker is no stereo front pair: the cabinet is there and cannot
        # carry a sound, and the reason says which speaker set is missing
        # (plugin.room_diagnosis) rather than "nothing happened".
        game = make_game(speakers=(("front_l", 6.5, 26.5),))
        report = cinema_sound_test.play(game, game.gameplay, "j1", "auto")
        self.assertFalse(report["heard"])
        self.assertTrue(report["reason"])

    def test_ears_out_of_the_rooms_reach_say_how_far_back_to_walk(self):
        # The listener stands 200 m from the cabinet: the room resolves fine and
        # is simply not audible here (live.note_goes_to_a_room).
        game = make_game(cabinets=(("j1", OTHER_ANCHOR),),
                         speakers=(("front_l", 196.5, 306.5),
                                   ("front_r", 203.5, 306.5)),
                         position=(10.0, 25.0, 0.0))
        report = cinema_sound_test.play(game, game.gameplay, "j1", "auto")
        self.assertFalse(report["heard"])
        self.assertIn("out of reach", report["reason"])
        self.assertIn("m", report["reason"])

    def test_a_muted_machine_is_not_a_machine_that_heard_it(self):
        """The one silence no listening switch explains -- so it must not lie.

        The note plays through the Miscellaneous category: a muted game (or that
        slider at zero) makes no sound here whatever the switches say, and a
        report of "heard it" for it would be the false green this feature exists
        to remove.
        """
        game = make_game()
        game.audio_mngr.muted = True
        report = cinema_sound_test.play(game, game.gameplay, "j1", "auto")
        self.assertFalse(report["heard"])
        self.assertIn("muted", report["reason"])
        self.assertEqual(game.audio_mngr.played, [])
        game.audio_mngr.muted = False
        game.audio_mngr.volume_categories["miscelaneous"] = [0]
        report = cinema_sound_test.play(game, game.gameplay, "j1", "auto")
        self.assertFalse(report["heard"])
        self.assertIn("Miscellaneous", report["reason"])
        self.assertEqual(game.audio_mngr.played, [])

    def test_a_played_note_leaves_a_line_on_this_client(self):
        game = make_game()
        with mock.patch.object(cinema_sound_test, "log_line") as logged:
            cinema_sound_test.play(game, game.gameplay, "j1", "auto")
        self.assertTrue(logged.called)
        self.assertIn("sound test", logged.call_args[0][0])


class TheShotItselfTests(unittest.TestCase):
    """Asking the Server to fire, and answering a shot that arrives."""

    def test_firing_names_only_the_destination(self):
        game = make_game()
        self.assertTrue(cinema_sound_test.fire(game, game.gameplay, "j2", "left"))
        channel, event, payload = game.network.sent[-1]
        self.assertEqual(event, "cinema_test")
        self.assertEqual(payload, {"cabinet": "j2", "direction": "left"})

    def test_the_answer_carries_the_id_and_what_this_machine_did(self):
        game = make_game()
        cinema_sound_test.send_report(game, 7, {"heard": True, "speakers": 2,
                                                "reason": ""})
        _channel, event, payload = game.network.sent[-1]
        self.assertEqual(event, "cinema_test_report")
        self.assertEqual(payload["id"], 7)
        self.assertTrue(payload["heard"])
        self.assertEqual(payload["speakers"], 2)

    def test_a_reason_never_grows_longer_than_a_menu_line(self):
        game = make_game()
        cinema_sound_test.send_report(
            game, 1, {"heard": False, "speakers": 0, "reason": "x" * 400})
        self.assertLessEqual(len(game.network.sent[-1][2]["reason"]),
                             cinema_sound_test.MAX_REASON)

    def test_the_client_plays_a_relayed_test_and_answers_it(self):
        from libs import event_handeler
        game = make_game()
        handler = event_handeler.EventHandeler.__new__(event_handeler.EventHandeler)
        handler.game = game
        handler.gameplay = game.gameplay
        handler.cinema_test({"id": 3, "cabinet": "j1", "direction": "left",
                             "name": "Somchai"})
        _channel, event, payload = game.network.sent[-1]
        self.assertEqual(event, "cinema_test_report")
        self.assertEqual(payload["id"], 3)
        self.assertTrue(payload["heard"])
        self.assertEqual(len(game.audio_mngr.played), 2)

    def test_a_packet_without_a_usable_id_changes_nothing(self):
        from libs import event_handeler
        game = make_game()
        handler = event_handeler.EventHandeler.__new__(event_handeler.EventHandeler)
        handler.game = game
        handler.gameplay = game.gameplay
        for bad in ({"cabinet": "j1", "direction": "left"},
                    {"id": "3", "cabinet": "j1"},
                    {"id": True, "cabinet": "j1"},
                    {"id": 3, "cabinet": ""}):
            handler.cinema_test(bad)
        self.assertEqual(game.network.sent, [])
        self.assertEqual(game.audio_mngr.played, [])


class TheAnswerTests(unittest.TestCase):
    """What the tester reads and hears when the answers are in."""

    def result(self, **overrides):
        result = {
            "id": 4, "cabinet": "j2", "direction": "left",
            "heard": 3, "total": 4, "speakers": 2,
            "details": [
                {"name": "Kanya", "answered": True, "heard": False,
                 "speakers": 0, "reason": "Instruments switch is off here"},
                {"name": "Ratsamee", "answered": False, "heard": False,
                 "speakers": 0, "reason": ""},
                {"name": "Somchai", "answered": True, "heard": True,
                 "speakers": 2, "reason": ""},
            ],
        }
        result.update(overrides)
        return result

    def test_the_summary_is_one_line(self):
        game = make_game()
        cinema_sound_test.note_result(game.gameplay, self.result())
        self.assertEqual(cinema_sound_test.describe(game.gameplay),
                         "Test report: jukebox j2 - heard 3/4, 1 did not")
        self.assertEqual(cinema_sound_test.spoken_summary(self.result()),
                         "Test at jukebox j2 (left): heard 3/4.")

    def test_a_clean_test_says_so(self):
        game = make_game()
        cinema_sound_test.note_result(
            game.gameplay,
            self.result(heard=4, details=[{"name": "A", "answered": True,
                                           "heard": True, "speakers": 2}]))
        self.assertEqual(cinema_sound_test.describe(game.gameplay),
                         "Test report: jukebox j2 - heard 4/4, all of them")

    def test_the_details_name_a_reason_per_client(self):
        game = make_game()
        cinema_sound_test.note_result(game.gameplay, self.result())
        lines = cinema_sound_test.details(game.gameplay)
        self.assertEqual(len(lines), 3)
        self.assertIn("Kanya - not heard: Instruments switch is off here", lines)
        # Somebody who never answered is named as such: an old build drops the
        # relay silently, and that must not read as "did not hear it".
        self.assertIn("Ratsamee - no answer (an older build?)", lines)
        self.assertIn("Somchai - heard it (2 speakers)", lines)

    def test_a_shot_inside_the_cooldown_does_not_erase_the_last_answer(self):
        game = make_game()
        cinema_sound_test.note_result(game.gameplay, self.result())
        refused = cinema_sound_test.note_result(
            game.gameplay, {"error": "cooldown", "wait_ms": 900, "cabinet": "j2"})
        # It is still said out loud...
        self.assertIn("Wait 0.9", cinema_sound_test.spoken_summary(refused))
        # ...but the report a tester is reading stays the last real one.
        self.assertIn("heard 3/4", cinema_sound_test.describe(game.gameplay))
        self.assertEqual(len(cinema_sound_test.details(game.gameplay)), 3)

    def test_the_cooldown_says_how_long_to_wait(self):
        self.assertEqual(
            cinema_sound_test.spoken_summary(
                {"error": "cooldown", "wait_ms": 2100, "cabinet": "j2"}),
            "Wait 2.1 seconds before testing again.")

    def test_the_reason_survives_the_wire(self):
        """The Server ships data, the client owns the words (see the schema)."""
        self.assertEqual(cinema_sound_test.spoken_summary({}), "")
        self.assertEqual(cinema_sound_test.details(None), [])


class TheMenuTests(unittest.TestCase):
    """Where the shot is fired from, and where the answer is read back."""

    def build(self, opener):
        from libs import menu as menu_mod, menus
        built = {}

        class FakeMenu:
            def __init__(self, _game, title="", **kwargs):
                self.title = title
                self.items = []
                built["menu"] = self

            def add_items(self, items):
                self.items.extend(items)

        with mock.patch.object(menu_mod, "Menu", FakeMenu), \
                mock.patch.object(menus, "set_default_sounds", lambda *a: None):
            opened = opener()
            # Some entries are "open the next menu" closures (the direction
            # picker), and one of those is the menu this test is about.
            if "menu" not in built and callable(opened):
                opened()
        return built["menu"]

    def labels(self, menu_obj):
        return [label for label, _action in menu_obj.items]

    def test_the_test_menu_lists_the_cabinets_it_can_fire_at(self):
        from libs import cinema_pan_menu
        game = make_game()
        menu = self.build(lambda: cinema_pan_menu.open_menu(
            game, game.gameplay, cinema_pan_menu.MODE_TEST))
        self.assertEqual(menu.title, "Test which cabinet's sound?")
        self.assertTrue(any("Jukebox j1" in label for label in self.labels(menu)))
        # No player is being moved, so nothing here offers to clear a pan.
        self.assertFalse(any(label.startswith("Clear") for label in self.labels(menu)))

    def test_the_direction_chooser_fires_instead_of_panning(self):
        from libs import cinema_pan_menu
        game = make_game()
        menu = self.build(lambda: cinema_pan_menu._direction_picker(
            game, game.gameplay, None, None, "j1", cinema_pan_menu.MODE_TEST))
        self.assertIn("test", menu.title)
        action = dict(menu.items)["Towards the left of the room"]
        action()
        _channel, event, payload = game.network.sent[-1]
        self.assertEqual(event, "cinema_test")
        self.assertEqual(payload["cabinet"], "j1")
        self.assertEqual(payload["direction"], "left")
        # The menu stays on the direction list: a tester walks the sides of one
        # room by pressing direction after direction (each press is one shot),
        # so a shot must not step back to the cabinet list. Back is the way out.
        self.assertEqual(getattr(game.gameplay, "popped", 0), 0)
        self.assertIn("Back", self.labels(menu))
        action = dict(menu.items)["Towards the back of the room"]
        action()
        self.assertEqual(game.network.sent[-1][2]["direction"], "back")
        self.assertEqual(getattr(game.gameplay, "popped", 0), 0)

    def test_the_pan_flow_can_try_the_same_destination_first(self):
        from libs import cinema_pan_menu
        game = make_game()
        menu = self.build(lambda: cinema_pan_menu._direction_picker(
            game, game.gameplay, "Somchai", 9, "j1", cinema_pan_menu.MODE_PAN))
        labels = self.labels(menu)
        self.assertTrue(any("Test jukebox j1" in label for label in labels))
        # ...and the pan itself is still one press away, unchanged.
        self.assertIn("Towards the left of the room", labels)
        # The try-it picker behaves like the standalone one: that line opens the
        # test's own direction list, and a shot there stays put as well.
        test_menu = self.build(lambda: dict(menu.items)[
            "Test jukebox j1 (short note, pick a direction next)"]())
        self.assertIn("test", test_menu.title)
        dict(test_menu.items)["Towards the back of the room"]()
        self.assertEqual(game.network.sent[-1][2]["direction"], "back")
        self.assertEqual(getattr(game.gameplay, "popped", 0), 0)

    def test_the_last_answer_is_one_press_away(self):
        from libs import cinema_pan_menu
        game = make_game()
        cinema_sound_test.note_result(game.gameplay, {
            "id": 1, "cabinet": "j1", "direction": "auto", "heard": 1, "total": 2,
            "speakers": 2, "details": [
                {"name": "Kanya", "answered": True, "heard": False,
                 "speakers": 0, "reason": "Instruments switch is off here"}]})
        menu = self.build(lambda: cinema_pan_menu.open_menu(
            game, game.gameplay, cinema_pan_menu.MODE_TEST))
        report = [label for label in self.labels(menu) if label.startswith("Test report")]
        self.assertEqual(len(report), 1)
        self.assertIn("1/2", report[0])

    def test_a_client_without_the_rank_is_refused(self):
        from libs import cinema_pan_menu
        game = make_game()
        game.gameplay.can_use_cinema_pan = False
        with mock.patch.object(cinema_pan_menu, "speak") as spoke:
            cinema_pan_menu.open_menu(game, game.gameplay,
                                      cinema_pan_menu.MODE_TEST)
        self.assertIn("technicians", spoke.call_args[0][0])
        self.assertEqual(game.network.sent, [])


class TheEntryPointsTests(unittest.TestCase):
    """Both doors: the technician menu's line, and the pan flow's own try."""

    def test_the_server_event_opens_this_clients_own_menu(self):
        from libs import event_handeler
        game = make_game()
        handler = event_handeler.EventHandeler.__new__(event_handeler.EventHandeler)
        handler.game = game
        handler.gameplay = game.gameplay
        with mock.patch.object(game.gameplay, "open_cinema_test",
                               create=True) as opened:
            handler.cinema_test_menu({})
            handler.cinema_test_menu({})
        # The Server only invites this client (it cannot resolve a room from
        # another machine), so the event must reach the client's own opener.
        self.assertEqual(opened.call_count, 2)

    def test_the_gameplay_opener_refuses_and_opens(self):
        from libs import gameplay
        gp = gameplay.Gameplay.__new__(gameplay.Gameplay)
        gp.can_use_cinema_pan = False
        with mock.patch.object(gameplay, "speak") as spoke:
            gp.open_cinema_test(None)
        self.assertIn("technicians", spoke.call_args[0][0])
        gp.can_use_cinema_pan = True
        game = make_game()
        gp.game = game
        gp.cinema_sound_test = None
        with mock.patch("libs.cinema_pan_menu.open_test_menu") as opened:
            gp.open_cinema_test(None)
        opened.assert_called_once()


if __name__ == "__main__":
    unittest.main()
