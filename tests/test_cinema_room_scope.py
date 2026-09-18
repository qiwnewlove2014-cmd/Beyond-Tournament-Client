"""Which speakers belong to which cabinet: the area, the reach, and the read-out.

A wide map holds several cinema setups in different places, and what decides
which speaker feeds which one is a *scope*: the map zone a cabinet stands in
(when the map has one), and the cabinet's own reach -- one number under two
names, since it is how far a speaker may stand from the cabinet to belong to
its room *and* how far from a listener that speaker is still heard.

These tests run against a real ``Map`` (the same speakers and zones the parser
builds) and a fake client-side payload, so what they pin is the resolution a
listener actually gets:

    * distance alone still decides when the map draws no zone around a cabinet
      (nothing about the maps that shipped changes);
    * a speaker standing in another hall's area is not a nearer cabinet's --
      and the cabinet whose area holds it takes it, even though a rival is
      closer, which is what keeps such a speaker from belonging to nobody;
    * a cabinet's own marker zone (the one the builder writes at its own
      bounds) is never mistaken for its area;
    * a cabinet's reach widens both the membership and the audible distance,
      and a value the map should never have written is ignored, not obeyed;
    * the read-outs (who owns a speaker, what a cabinet's reach is) say the
      same thing the resolver decided.
"""

import os
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from libs.world_map import Map, Zone
from libs.audio.cinema import (MAX_ROOM_REACH, MIN_ROOM_REACH, ROOM_MAX_DISTANCE,
                               ROOM_RADIUS, cabinet_area, cabinet_neighbours,
                               cabinet_reach, neighbour_line, neighbour_note,
                               plan_reach, preview_room, set_enabled,
                               speaker_owner)
from libs.audio.cinema import plugin as cinema_plugin
from libs.audio.cinema.placement import inside_area
from libs.audio.cinema.router import renderer_for
from libs import jukebox
from test_cinema_wiring import FakeGame, play_song


class Payload:
    """The two answers a cabinet's client-side state gives (mode and reach)."""

    def __init__(self, modes=None, reaches=None):
        self.modes = dict(modes or {})
        self.reaches = dict(reaches or {})

    def cinema_mode(self, jukebox_id):
        return self.modes.get(jukebox_id, "auto")

    def cinema_reach(self, jukebox_id):
        return self.reaches.get(jukebox_id)

    def set_local_cinema_mode(self, jukebox_id, mode):
        self.modes[jukebox_id] = str(mode)

    def set_local_cinema_reach(self, jukebox_id, reach):
        self.reaches[jukebox_id] = float(reach)


def build(*, cabinets=((10.0, 20.0),), speakers=(), zones=(), reaches=None,
          modes=None, payload=True):
    """A map with cabinets, speakers and zones, plus the client's own cache."""
    state = {"jukeboxes": {}}
    player = Payload(modes, reaches) if payload else None
    gameplay = SimpleNamespace(map=None, jukebox_player=player,
                              jukebox_state=state)
    game = SimpleNamespace(audio_mngr=SimpleNamespace(position=(0.0, 0.0, 0.0)),
                           gameplay=gameplay)
    map_obj = Map(game)
    for index, (x, y) in enumerate(cabinets):
        jid = f"j{index + 1}"
        map_obj.spawn_jukebox(minx=x - 0.5, maxx=x + 0.5, miny=y - 0.5,
                              maxy=y + 0.5, minz=0, maxz=1, id=jid)
        state["jukeboxes"][jid] = {"id": jid, "x": x, "y": y, "z": 0.0}
        if reaches and jid in reaches:
            state["jukeboxes"][jid]["cinema_reach"] = reaches[jid]
        if modes and jid in modes:
            state["jukeboxes"][jid]["cinema_mode"] = modes[jid]
    for index, spec in enumerate(speakers):
        spec = dict(spec)
        x, y = spec.pop("x"), spec.pop("y")
        z = spec.pop("z", 0.0)
        map_obj.spawn_cinemaSpeaker(minx=x - 0.5, maxx=x + 0.5, miny=y - 0.5,
                                    maxy=y + 0.5, minz=z, maxz=z + 1,
                                    id=f"spk{index}", **spec)
    for index, item in enumerate(zones):
        minx, maxx, miny, maxy, minz, maxz, name = item
        map_obj.spawn_zone(minx=minx, maxx=maxx, miny=miny, maxy=maxy,
                           minz=minz, maxz=maxz, innerText=name,
                           id=f"zone{index}")
    gameplay.map = map_obj
    return game, map_obj


def pair(centre_x, forward_y, spread=4.0):
    """A stereo front pair for a cabinet: left and right, standing ahead of it.

    ``forward_y`` is the speakers' own line, and the cabinet they belong to
    stands behind it (a smaller y): that is the shape the placement resolver
    reads as a front pair, and it is what makes these tests about *scope*
    rather than about geometry.
    """
    return [dict(x=centre_x - spread, y=forward_y, channel="front_l"),
            dict(x=centre_x + spread, y=forward_y, channel="front_r")]


def point(value):
    """A 3-D point from the 2-D ones these tests write (a map's floor is z=0).

    Production callers always hold an element's own ``(x, y, z)``; a test that
    writes "the speaker at (30, 20)" is naming a block, so its z is 0.
    """
    values = [float(item) for item in value]
    while len(values) < 3:
        values.append(0.0)
    return tuple(values[:3])


def spot(raw):
    """Where a raw speaker spec stands, read the way the resolver reads it."""
    spec = cinema_plugin.coerce_spec(raw)
    return (spec.position[0], spec.position[1])


def candidates(game, cabinet_id, x, y):
    """The speakers this cabinet would take here (the resolver's own list)."""
    kept, _reach = cinema_plugin.cabinet_candidates(game, cabinet_id,
                                                    point((x, y)))
    return {spot(raw) for raw in kept}


def slots(game, cabinet_id, anchor):
    plan = preview_room(game, point(anchor), room_id=cabinet_id)
    if plan is None or plan.placement is None:
        return None
    return sorted(plan.placement.slots)


class DistanceStillDecidesWhenNothingIsDrawnTests(unittest.TestCase):
    """The maps that shipped: no zones, so the radius is the only boundary."""

    def test_two_far_apart_halls_keep_their_own_speakers(self):
        game, _map = build(
            cabinets=((10.0, 20.0), (200.0, 20.0)),
            speakers=pair(10.0, 26.0) + pair(200.0, 26.0),
        )
        self.assertEqual(slots(game, "j1", (10.0, 20.0)), ["front_l", "front_r"])
        self.assertEqual(slots(game, "j2", (200.0, 20.0)), ["front_l", "front_r"])

    def test_a_speaker_near_a_shared_wall_goes_to_the_nearer_cabinet(self):
        game, _map = build(
            cabinets=((10.0, 20.0), (70.0, 20.0)),
            speakers=[dict(x=30.0, y=20.0, channel="front_r")],
        )
        self.assertIn((30.0, 20.0), candidates(game, "j1", 10.0, 20.0))
        self.assertNotIn((30.0, 20.0), candidates(game, "j2", 70.0, 20.0))


class TheAreaIsAHardBoundaryTests(unittest.TestCase):
    """Two halls drawn on one map: j1's hall ends at x=29, j2's starts at 30."""

    HALLS = [(0, 29, 0, 40, 0, 8, "Hall A"), (30, 110, 0, 40, 0, 8, "Hall B")]

    def build(self):
        return build(
            cabinets=((10.0, 20.0), (70.0, 20.0)),
            # Hall A holds j1's own pair, because a zone no speaker stands in
            # yet is not an area at all -- and the lone speaker in Hall B is
            # what the two cabinets are arguing over.
            speakers=pair(10.0, 26.0) + [dict(x=35.0, y=24.0,
                                              channel="front_r")],
            zones=self.HALLS,
        )

    def test_a_nearer_cabinet_does_not_take_the_other_halls_speaker(self):
        game, _map = self.build()
        # 26 m from j1 against 35 m from j2: distance alone gives it to j1,
        # and j1 stands in another hall.
        self.assertNotIn((35.0, 24.0), candidates(game, "j1", 10.0, 20.0))

    def test_the_cabinet_whose_area_holds_it_takes_it_anyway(self):
        """A rival's refusal must not leave a speaker owned by nobody."""
        game, _map = self.build()
        self.assertIn((35.0, 24.0), candidates(game, "j2", 70.0, 20.0))

    def test_the_refusal_names_the_area_it_stands_outside(self):
        game, _map = build(
            speakers=pair(10.0, 26.0) + [dict(x=35.0, y=24.0,
                                              channel="front_r")],
            zones=self.HALLS,
        )
        reasons = []
        cinema_plugin.cabinet_candidates(game, "j1", point((10.0, 20.0)),
                                         report=reasons)
        self.assertTrue(any("outside the area Hall A" in line for line in reasons),
                        reasons)

    def test_a_drawn_hall_still_lets_each_cabinet_have_its_room(self):
        game, _map = build(
            cabinets=((10.0, 20.0), (70.0, 20.0)),
            speakers=pair(10.0, 26.0) + pair(70.0, 26.0),
            zones=self.HALLS,
        )
        self.assertEqual(slots(game, "j1", (10.0, 20.0)), ["front_l", "front_r"])
        self.assertEqual(slots(game, "j2", (70.0, 20.0)), ["front_l", "front_r"])


class TheCabinetsOwnMarkerZoneTests(unittest.TestCase):
    """A builder-placed cabinet writes a zone at its own bounds."""

    HALL = (0, 60, 0, 60, 0, 8, "Main Hall")

    def test_the_venue_zone_wins_over_the_cabinets_marker(self):
        # The marker exactly as the builder writes it: the cabinet's own bounds.
        game, _map = build(
            speakers=pair(10.0, 26.0),
            zones=[(9.5, 10.5, 19.5, 20.5, 0, 1, "jukebox"), self.HALL],
        )
        self.assertEqual(cabinet_area(game, "j1"),
                         ("Main Hall", (0.0, 60.0, 0.0, 60.0, 0.0, 8.0)))

    def test_a_marker_a_rounding_error_larger_is_still_not_a_room(self):
        """A legacy or hand-written map rounds the marker's bounds.

        Taken as the area it would refuse every speaker but a monitor standing
        on the cabinet itself: a silent room, from a rounding error. The size
        test is what stops it, rather than comparing bounds exactly.
        """
        game, _map = build(
            speakers=pair(10.0, 26.0),
            zones=[(9, 11, 19, 21, 0, 1, "jukebox"), self.HALL],
        )
        self.assertEqual(cabinet_area(game, "j1")[0], "Main Hall")

    def test_a_zone_nothing_stands_in_is_not_an_area_yet(self):
        """A hall drawn before any speaker exists does not bound anything."""
        game, _map = build(
            speakers=pair(10.0, 86.0),        # the pair is outside the hall
            zones=[self.HALL],
        )
        self.assertEqual(cabinet_area(game, "j1"), (None, None))
        # …and it appears the moment one speaker stands inside it.
        game, _map = build(speakers=pair(10.0, 26.0), zones=[self.HALL])
        self.assertEqual(cabinet_area(game, "j1")[0], "Main Hall")

    def test_the_smallest_real_zone_wins(self):
        """The rule the map itself uses: the zone you are standing in."""
        game, _map = build(
            speakers=pair(10.0, 26.0) + [dict(x=10.0, y=22.0,
                                              channel="front_c")],
            zones=[self.HALL, (8, 12, 18, 24, 0, 4, "Booth")],
        )
        self.assertEqual(cabinet_area(game, "j1")[0], "Booth")

    def test_no_zone_around_it_means_distance_alone(self):
        game, _map = build(zones=[(100, 200, 100, 200, 0, 8, "Far Away")])
        self.assertEqual(cabinet_area(game, "j1"), (None, None))

    def test_a_map_with_no_zones_at_all_answers_none(self):
        game, _map = build()
        self.assertEqual(cabinet_area(game, "j1"), (None, None))

    def test_inside_area_agrees_with_the_maps_own_in_bound(self):
        """Floor-semantics containment, and the same answer a zone gives."""
        bounds = (0.0, 60.0, 0.0, 60.0, 0.0, 8.0)
        zone = Zone("z", 0, 60, 0, 60, 0, 8, "Main Hall")
        for point in ((-0.6, 10.0, 0.0), (0.0, 10.0, 0.0), (60.4, 10.0, 0.0),
                      (60.6, 10.0, 0.0), (30.0, 60.9, 3.0), (30.0, 10.0, 9.9)):
            self.assertEqual(inside_area(point, bounds), zone.in_bound(*point),
                             f"disagreed about {point}")


class ACabinetsOwnReachTests(unittest.TestCase):
    """How far this cabinet's room reaches: one number, two names."""

    def build(self, reaches=None, distance=75.0):
        # One cabinet, its pair 75 m ahead, no zones: only the reach decides.
        return build(speakers=pair(10.0, 20.0 + distance), reaches=reaches)

    def test_the_default_reach_is_still_the_room_radius(self):
        game, _map = self.build()
        self.assertEqual(cabinet_reach(game, "j1"), ROOM_MAX_DISTANCE)
        self.assertEqual(cabinet_reach(game, "j1"), ROOM_RADIUS)
        self.assertIsNone(slots(game, "j1", (10.0, 20.0)))

    def test_a_wider_reach_takes_the_hall_in(self):
        game, _map = self.build(reaches={"j1": 90})
        self.assertEqual(cabinet_reach(game, "j1"), 90.0)
        self.assertEqual(slots(game, "j1", (10.0, 20.0)), ["front_l", "front_r"])

    def test_the_room_carries_the_number_it_was_claimed_with(self):
        game, _map = self.build(reaches={"j1": 90})
        plan = preview_room(game, point((10.0, 20.0)), room_id="j1")
        self.assertEqual(plan.reach, 90.0)
        self.assertEqual(plan_reach(plan), 90.0)

    def test_a_value_the_map_should_not_hold_is_ignored(self):
        for bad in (5000, 5, 0, -20, "far", None):
            with self.subTest(value=bad):
                game, _map = self.build(reaches={"j1": bad})
                self.assertEqual(cabinet_reach(game, "j1"), ROOM_MAX_DISTANCE)

    def test_the_bounds_are_the_ones_the_server_enforces(self):
        game, _map = self.build(reaches={"j1": MAX_ROOM_REACH + 1})
        self.assertEqual(cabinet_reach(game, "j1"), ROOM_MAX_DISTANCE)
        game, _map = self.build(reaches={"j1": MAX_ROOM_REACH})
        self.assertEqual(cabinet_reach(game, "j1"), MAX_ROOM_REACH)

    def test_a_narrow_reach_keeps_a_room_to_its_own_corner(self):
        # 75 m away, and this cabinet says its room is 20 m: nothing to play.
        game, _map = self.build(reaches={"j1": 20})
        self.assertEqual(cabinet_reach(game, "j1"), 20.0)
        # 20 m is inside the bounds the server enforces, so it is obeyed
        # rather than ignored -- and it still does not reach the pair.
        self.assertGreaterEqual(20.0, MIN_ROOM_REACH)
        self.assertIsNone(slots(game, "j1", (10.0, 20.0)))


class TheRoomIsHeardAsFarAsItReachesTests(unittest.TestCase):
    """The wiring: what the resolver claimed with is what the bank plays with."""

    def build(self, **play_kwargs):
        game = FakeGame()
        set_enabled(game, True)
        map_obj = Map(game)
        map_obj.spawn_jukebox(minx=9.5, maxx=10.5, miny=19.5, maxy=20.5,
                              minz=0, maxz=1, id="j1")
        for index, (channel, x, y) in enumerate(
                (("front_l", 6.0, 26.0), ("front_r", 14.0, 26.0))):
            map_obj.spawn_cinemaSpeaker(minx=x - 0.5, maxx=x + 0.5,
                                        miny=y - 0.5, maxy=y + 0.5,
                                        minz=0, maxz=1, id=f"spk{index}",
                                        channel=channel)
        game.gameplay.map = map_obj
        player = jukebox.JukeboxPlayer(game)
        # The player is what the game asks for a cabinet's mode and reach (see
        # ``plugin.cabinet_reach``), so a harness that plays through one has to
        # register it exactly as ``event_handeler._jukebox_player`` does.
        game.gameplay.jukebox_player = player
        return game, player, play_song(game, player, **play_kwargs)

    def test_the_banks_audible_distance_is_the_cabinets_reach(self):
        game, player, streamer = self.build(cinema_reach=90)
        bank = streamer.call_args.kwargs["cinema"]
        self.assertIsNotNone(bank)
        self.assertEqual(bank.max_distance, 90.0)
        self.assertEqual(player.players["j1"]["cinema"].max_distance, 90.0)

    def test_a_play_without_a_payload_keeps_the_shipped_distance(self):
        game, player, streamer = self.build()
        bank = streamer.call_args.kwargs["cinema"]
        self.assertEqual(bank.max_distance, ROOM_MAX_DISTANCE)


class TheStatePayloadFeedsTheReadersTests(unittest.TestCase):
    """A cabinet is described before anybody hears a song from it."""

    def test_a_client_with_no_player_still_knows_the_reach(self):
        game, _map = build(reaches={"j1": 90}, payload=False)
        self.assertEqual(cabinet_reach(game, "j1"), 90.0)

    def test_the_player_cache_wins_over_the_last_state(self):
        game, _map = build(reaches={"j1": 90})
        game.gameplay.jukebox_player.set_local_cinema_reach("j1", 140)
        self.assertEqual(cabinet_reach(game, "j1"), 140.0)

    def test_a_map_with_no_reach_at_all_answers_the_default(self):
        game, _map = build()
        self.assertEqual(cabinet_reach(game, "j1"), ROOM_MAX_DISTANCE)

    def test_the_player_itself_reads_none_until_a_payload_arrives(self):
        player = jukebox.JukeboxPlayer(FakeGame())
        self.assertIsNone(player.cinema_reach("j1"))
        player.set_local_cinema_reach("j1", 90)
        self.assertEqual(player.cinema_reach("j1"), 90.0)


class SpeakerOwnerReadOutTests(unittest.TestCase):
    """The line a menu and a log can say about where a speaker belongs."""

    def test_a_claimed_speaker_names_its_cabinet_and_the_gap(self):
        game, _map = build(
            speakers=pair(10.0, 26.0),
            zones=[(0, 40, 0, 40, 0, 8, "Main Hall")],
        )
        owner = speaker_owner(game, point((6.0, 26.0)))
        self.assertTrue(owner.claimed)
        self.assertIn("jukebox j1 owns it", owner.describe())
        self.assertIn("Main Hall", owner.describe())

    def test_a_speaker_beyond_every_reach_says_how_far_it_is(self):
        game, _map = build(speakers=[dict(x=90.0, y=20.0, channel="front_l")])
        owner = speaker_owner(game, point((90.0, 20.0)))
        self.assertFalse(owner.claimed)
        self.assertEqual(owner.reason, "out_of_reach")
        self.assertIn("no cabinet owns it", owner.describe())
        self.assertIn("reach is 60 m", owner.describe())

    def test_a_speaker_in_another_hall_names_the_area_that_refused_it(self):
        game, _map = build(
            speakers=pair(10.0, 26.0) + [dict(x=35.0, y=24.0,
                                              channel="front_r")],
            zones=[(0, 29, 0, 40, 0, 8, "Hall A")],
        )
        owner = speaker_owner(game, point((35.0, 24.0)))
        self.assertFalse(owner.claimed)
        self.assertEqual(owner.reason, "out_of_area")
        self.assertIn("outside jukebox j1's area", owner.describe())

    def test_a_map_with_no_cabinet_says_so(self):
        game, _map = build(cabinets=(), speakers=pair(10.0, 26.0))
        self.assertEqual(speaker_owner(game, point((6.0, 26.0))).reason,
                         "no_cabinet")


class TheCabinetMenuReadOutTests(unittest.TestCase):
    """What staff read at the cabinet, and the choices offered there."""

    def build(self, reaches=None, zones=((0, 40, 0, 40, 0, 8, "Main Hall"),)):
        game, _map = build(reaches=reaches, speakers=pair(10.0, 26.0),
                           zones=zones)
        # The read-outs take the gameplay, which is where a cabinet's live
        # state (its mode, its reach) actually hangs.
        return game, game.gameplay

    def test_the_detail_names_the_reach_and_the_area(self):
        game, gp = self.build(reaches={"j1": 90})
        said = jukebox._cinema_detail(game, gp, "j1")
        self.assertIn("Its reach is 90 m", said)
        self.assertIn("'Main Hall' area", said)

    def test_a_map_with_no_zone_says_only_the_distance(self):
        game, gp = self.build(zones=())
        said = jukebox._cinema_detail(game, gp, "j1")
        self.assertIn("Its reach is 60 m", said)
        self.assertNotIn("area", said)

    def test_a_reach_the_map_never_set_reads_the_default(self):
        game, gp = self.build()
        self.assertEqual(jukebox._cabinet_cinema_reach(gp, "j1"), ROOM_MAX_DISTANCE)

    def test_the_choices_are_labelled_with_their_number(self):
        for value, _description in jukebox.CINEMA_REACHES:
            with self.subTest(value=value):
                self.assertTrue(
                    jukebox._cinema_reach_label(value).startswith(f"{value} m"))
        # A value the map set by hand is shown as such rather than hidden.
        self.assertIn("set by the map", jukebox._cinema_reach_label(75))

    def test_the_read_out_reads_the_state_the_server_sent(self):
        game, gp = self.build()
        game.gameplay.jukebox_state = {"jukeboxes": {"j1": {"cinema_reach": 140}}}
        self.assertEqual(jukebox._cabinet_cinema_reach(gp, "j1"), 140.0)


class TheCabinetsAroundACabinetTests(unittest.TestCase):
    """What the cabinet itself says about the cabinets standing near it."""

    HALLS = [(0, 29, 0, 40, 0, 8, "Hall A"), (30, 110, 0, 40, 0, 8, "Hall B")]

    class FakeMenu:
        def __init__(self, *args, **kwargs):
            self.items = []

        def add_items(self, items):
            self.items = list(items)

        def speak_current_item(self):
            pass

    def menu_game(self, **kwargs):
        """The same map, plus the little state a real cabinet menu needs."""
        game, _map = build(**kwargs)
        gp = game.gameplay
        gp.player = SimpleNamespace(name="Kanya", x=10.0, y=20.0, z=0.0)
        gp.substates = []
        gp.add_substate = gp.substates.append
        gp.pop_last_substate = lambda: None
        game.network = SimpleNamespace(send=lambda *args, **kwargs: None)
        return game

    def labels(self, game):
        with mock.patch("libs.menu.Menu", self.FakeMenu), \
                mock.patch("libs.menus.set_default_sounds"):
            jukebox.open_jukebox_menu(game, game.gameplay)
        menu = game.gameplay.substates[-1]
        return [label() if callable(label) else label for label, _action in menu.items]

    def test_a_lone_cabinet_says_nothing_about_neighbours(self):
        game, _map = build(speakers=pair(10.0, 26.0))
        self.assertEqual(neighbour_note(game, "j1"), "")
        self.assertEqual(neighbour_line(game, "j1"), "")
        self.assertEqual([line for line in self.labels(
            self.menu_game(speakers=pair(10.0, 26.0))) if "cabinet" in line], [])

    def test_a_distant_neighbour_is_named_with_its_distance(self):
        game, _map = build(cabinets=((10.0, 20.0), (150.0, 20.0)))
        self.assertEqual(neighbour_line(game, "j1"), "Another cabinet at 140 m (j2)")
        self.assertIn("nothing is drawn around either cabinet",
                      neighbour_note(game, "j1"))

    def test_two_cabinets_in_one_drawn_hall_split_the_speakers_by_distance(self):
        game, _map = build(
            cabinets=((10.0, 20.0), (40.0, 20.0)),
            speakers=pair(10.0, 26.0) + pair(40.0, 26.0),
            zones=[(0, 60, 0, 60, 0, 8, "Main Hall")],
        )
        nearest = cabinet_neighbours(game, "j1")[0]
        self.assertTrue(nearest.same_area)
        note = neighbour_note(game, "j1")
        self.assertIn("in this same area ('Main Hall')", note)
        self.assertIn("speakers closer to j2 than to this cabinet go to its room",
                      note)
        self.assertEqual(neighbour_line(game, "j1"),
                         "Another cabinet at 30 m (j2, this same area)")

    def test_a_hall_each_means_each_keeps_its_own(self):
        game, _map = build(
            cabinets=((10.0, 20.0), (70.0, 20.0)),
            speakers=pair(10.0, 26.0) + pair(70.0, 26.0),
            zones=self.HALLS,
        )
        note = neighbour_note(game, "j1")
        self.assertIn("in 'Hall B', another area", note)
        self.assertIn("belongs to it", note)
        self.assertEqual(neighbour_line(game, "j1"), "Another cabinet at 60 m (j2, in 'Hall B')")

    def test_one_hall_drawn_between_two_cabinets_is_said_as_it_is(self):
        """j1's hall exists, j2's does not (nothing stands in it yet)."""
        game, _map = build(cabinets=((10.0, 20.0), (70.0, 20.0)),
                           speakers=pair(10.0, 26.0), zones=self.HALLS)
        self.assertEqual(cabinet_area(game, "j2"), (None, None))
        note = neighbour_note(game, "j1")
        self.assertIn("with no hall drawn around it", note)
        self.assertIn("can be claimed by both", note)

    def test_the_line_counts_the_others_and_names_the_nearest(self):
        game, _map = build(cabinets=((10.0, 20.0), (50.0, 20.0), (90.0, 20.0)))
        self.assertEqual(neighbour_line(game, "j1"),
                         "2 other cabinets, nearest at 40 m (j2)")
        self.assertIn("j2", neighbour_note(game, "j1"))
        self.assertNotIn("j3", neighbour_note(game, "j1"))

    def test_the_menu_carries_the_line_and_speaks_the_whole_answer(self):
        game = self.menu_game(cabinets=((10.0, 20.0), (50.0, 20.0)))
        label = "Another cabinet at 40 m (j2)"
        self.assertIn(label, self.labels(game))
        action = dict((label() if callable(label) else label, act)
                      for label, act in game.gameplay.substates[-1].items)[label]
        with mock.patch("libs.jukebox.speak") as said:
            action()
        self.assertIn("shared out by distance", said.call_args.args[0])

    def test_the_cabinets_own_description_ends_with_its_neighbours(self):
        game, _map = build(cabinets=((10.0, 20.0), (50.0, 20.0)),
                           speakers=pair(10.0, 26.0))
        said = jukebox._cinema_detail(game, game.gameplay, "j1")
        self.assertIn("Its reach is 60 m", said)
        self.assertIn("shared out by distance", said)


class AReachThatCutsTheMapsSpeakersIsReportedTests(unittest.TestCase):
    """A reach set below a speaker the map placed: the room is not the map's.

    The reach is one number and it decides membership as well as audibility, so
    a reach picked below a speaker the map put there breaks the stereo pair the
    map built. What plays then is the ring behind the cabinet -- which uses
    *none* of the map's speakers, so their crossover, tone, level and delay are
    all unread while the song plays perfectly well. "My crossover does nothing"
    is what that sounds like, and nothing on any screen used to say it.
    """

    SPEAKERS = [dict(x=0.0, y=20.0, z=0.0, channel="front_l", crossover=60),
                dict(x=20.0, y=20.0, z=10.0, channel="front_r", crossover=-3000)]

    def build(self, reach):
        return build(cabinets=((10.0, 5.0),), speakers=self.SPEAKERS,
                     reaches=None if reach is None else {"j1": reach})

    def plan(self, reach, requested="front_only"):
        game, _map = self.build(reach)
        return game, cinema_plugin.room_plan(game, point((10.0, 5.0)),
                                             requested=requested, room_id="j1")

    def test_the_maps_pair_is_the_room_at_the_default_reach(self):
        _game, (plan, silent) = self.plan(None)
        self.assertIsNotNone(plan.placement)
        self.assertEqual(silent, [])
        self.assertEqual(plan.warnings, ())
        renderer = renderer_for(point((10.0, 5.0)), plan.profile,
                                specs=plan.specs, fill=plan.fill)
        self.assertEqual(renderer.layout.crossover("front_l"), 60.0)
        self.assertEqual(renderer.layout.crossover("front_r"), -3000.0)

    def test_a_reach_that_cuts_the_pair_says_so_and_the_room_is_the_ring(self):
        _game, (plan, silent) = self.plan(20)
        self.assertIsNone(plan.placement)
        self.assertTrue(plan.fill)
        # The speaker the reach kept is silent as well: the ring is not the
        # map, so nothing standing here is fed whatever its slot is called.
        self.assertEqual([slot for _name, slot in silent], ["front_l"])
        warning = " ".join(plan.warnings)
        self.assertIn("past its reach of 20 m", warning)
        self.assertIn("no stereo front pair", warning)
        self.assertIn("crossover", warning)
        self.assertIn("ring behind the cabinet", warning)

    def test_the_cut_speaker_is_named_by_the_rooms_own_reason_reader(self):
        game, _map = self.build(20)
        diagnosis = cinema_plugin.room_diagnosis(game, point((10.0, 5.0)),
                                                 room_id="j1")
        self.assertIn("spk1", diagnosis)
        self.assertIn("21 m", diagnosis)
        self.assertIn("reach of 20 m", diagnosis)

    def test_a_map_with_no_speakers_at_all_is_not_a_warning(self):
        game, _map = build(cabinets=((10.0, 5.0),), reaches={"j1": 20})
        plan, silent = cinema_plugin.room_plan(game, point((10.0, 5.0)),
                                               requested="front_only",
                                               room_id="j1")
        self.assertIsNone(plan.placement)
        self.assertEqual(silent, [])
        self.assertEqual(plan.warnings, ())

    def test_the_playing_path_reports_the_cut_speaker_when_it_builds_the_room(self):
        """The log a room's build writes is the other half of the report."""
        game = FakeGame()
        set_enabled(game, True)
        map_obj = Map(game)
        map_obj.spawn_jukebox(minx=9.5, maxx=10.5, miny=4.5, maxy=5.5,
                              minz=0, maxz=1, id="j1")
        for index, (x, y, z) in enumerate(((0.0, 20.0, 0), (20.0, 20.0, 10))):
            map_obj.spawn_cinemaSpeaker(minx=x - 0.5, maxx=x + 0.5, miny=y - 0.5,
                                        maxy=y + 0.5, minz=z, maxz=z + 1,
                                        id=f"spk{index}",
                                        channel=("front_l", "front_r")[index])
        game.gameplay.map = map_obj
        player = jukebox.JukeboxPlayer(game)
        game.gameplay.jukebox_player = player
        streamer = play_song(game, player, position=(10, 5, 0),
                             cinema_mode="front_only", cinema_reach=20)
        # The shape still plays (the ring), and the reason it is not the map's
        # speakers travels with it to the log the jukebox writes.
        self.assertIsNotNone(streamer.call_args.kwargs.get("cinema"))
        warned = " ".join(player._cinema_warned.get("j1", ()))
        self.assertIn("past its reach of 20 m", warned)
        self.assertIn("ring behind the cabinet", warned)

    def test_auto_leaves_the_cabinet_plain_instead_of_reaching_for_a_ring(self):
        game, _map = self.build(20)
        plan, silent = cinema_plugin.room_plan(game, point((10.0, 5.0)),
                                               room_id="j1")
        self.assertIsNone(plan)
        self.assertEqual(silent, [])


class TheReachMenuKnowsTheRoomsGeometryTests(unittest.TestCase):
    """A reach is a number; these are the two things it is measured against.

    The reach is both how far a speaker may stand to belong and how far a
    listener still hears one, so a small value does two different kinds of
    damage: it cuts a speaker somebody placed (whose crossover, tone, level
    and delay then stop being heard at all) and it leaves the far end of the
    room the map drew unheard -- the "I walked to the back and heard nothing"
    report. Neither number appeared anywhere a person choosing a reach could
    see, which is why a room that was working looked broken.
    """

    PAIR = [dict(x=0.0, y=20.0, channel="front_l"),
            dict(x=20.0, y=20.0, channel="front_r")]
    HALL = [(0, 20, 0, 20, 0, 20, "hall")]
    # A small hall, and a long one with a speaker down at the far end: the
    # speaker a preset cuts has to stand *inside* the drawn area to be this
    # cabinet's at all (the area rule runs first -- see ``exclusive_speakers``).
    LONG_HALL = [(0, 20, 0, 80, 0, 20, "hall")]
    FAR = [dict(x=0.0, y=20.0, channel="front_l"),
           dict(x=20.0, y=20.0, channel="front_r"),
           dict(x=10.0, y=70.0, channel="auto")]

    def extent(self, speakers=None, zones=None):
        game, _map = build(cabinets=((10.0, 5.0),), zones=zones or self.HALL,
                           speakers=self.PAIR if speakers is None else speakers)
        gp = game.gameplay
        return game, gp, cinema_plugin.room_extent(game, "j1", point((10.0, 5.0)))

    def test_the_extent_names_the_farthest_speaker_and_the_rooms_own_edge(self):
        _game, _gp, extent = self.extent()
        self.assertEqual(extent.area, "hall")
        self.assertAlmostEqual(extent.farthest, 18.0, delta=0.5)
        # The far corner of a 20x20x20 hall from a cabinet standing off centre:
        # the edge the map drew, not the speakers, is what a reach must cover.
        self.assertAlmostEqual(extent.corner, 26.9, delta=0.5)
        self.assertEqual([name for name, _distance in extent.speakers],
                         ["spk0", "spk1"])

    def test_a_reach_below_the_rooms_edge_says_what_goes_unheard(self):
        _game, _gp, extent = self.extent()
        note = extent.note(20)
        self.assertIn("reaches 27 m from the cabinet", note)
        self.assertIn("the far end of this room is silent", note)
        self.assertIn("'hall'", note)

    def test_a_reach_that_cuts_a_placed_speaker_names_it_and_why_it_matters(self):
        _game, _gp, extent = self.extent(self.FAR, zones=self.LONG_HALL)
        note = extent.note(60)
        self.assertIn("Leaves out spk2 (65 m)", note)
        self.assertIn("crossover, tone and level are not heard", note)

    def test_a_reach_that_covers_the_room_adds_nothing_at_all(self):
        _game, _gp, extent = self.extent()
        for value in (60, 90, 140):
            self.assertEqual(extent.note(value), "")

    def test_a_map_with_nothing_placed_here_states_no_geometry(self):
        game, _gp = build(cabinets=((10.0, 5.0),), zones=self.HALL)
        extent = cinema_plugin.room_extent(game, "j1", point((10.0, 5.0)))
        self.assertIsNone(extent.farthest)
        self.assertIsNone(extent.corner)
        self.assertEqual(extent.note(20), "")
        self.assertEqual(extent.summary(), "")

    def test_the_reach_menu_lines_carry_the_warning_and_the_cabinet_the_numbers(self):
        game, gp, _extent = self.extent()
        opened = []

        class FakeMenu:
            def __init__(self, game_, title, parrent=None):
                self.title = title
                self.items = []
                opened.append(self)

            def add_items(self, items):
                self.items.extend(items)

        gp.add_substate = lambda *_: None
        gp.pop_last_substate = lambda: None
        with mock.patch("libs.menu.Menu", FakeMenu), \
                mock.patch("libs.menus.set_default_sounds"):
            jukebox._open_cinema_reach_menu(game, gp, "j1")
        labels = {str(label)[:5]: str(label) for label, _call in opened[0].items}
        # 20 m is the menu's first preset and it is below this room's own edge,
        # so its line says so; 60 m covers the room and its line stays short.
        self.assertIn("far end of this room is silent", labels["20 m "])
        self.assertNotIn("far end of this room is silent", labels["60 m "])
        self.assertIn("Measured from the cabinet",
                      jukebox._cinema_reach_note(game, gp, "j1"))
        self.assertIn("reaches 27 m", jukebox._cinema_reach_note(game, gp, "j1"))

    def test_the_band_each_fed_speaker_keeps_is_said_next_to_the_room(self):
        """A sub and a tweeter and nothing between them is a hole, not a room."""
        speakers = [dict(x=0.0, y=20.0, channel="front_l", crossover=-1500),
                    dict(x=20.0, y=20.0, channel="front_r", crossover=80)]
        game, gp, _extent = self.extent(speakers)
        said = jukebox._cinema_room_sentence(game, gp, "j1", mode="front_only")
        self.assertIn("Speakers here keep:", said)
        self.assertIn("above 1.5 kHz", said)
        self.assertIn("below 80 Hz", said)
        self.assertIn("not heard at all", said)

    def test_a_room_of_full_range_speakers_says_nothing_about_bands(self):
        game, gp, _extent = self.extent()
        said = jukebox._cinema_room_sentence(game, gp, "j1", mode="front_only")
        self.assertNotIn("Speakers here keep:", said)


class TheStaffListsAreOrderedByDistanceTests(unittest.TestCase):
    """A wide map: which hall is this pick about?"""

    def test_the_nearest_cabinet_comes_first_and_every_line_carries_metres(self):
        from libs import cinema_pan_menu

        game, _map = build(cabinets=((10.0, 20.0), (200.0, 20.0), (90.0, 20.0)))
        gp = SimpleNamespace(player=SimpleNamespace(x=12.0, y=20.0, z=0.0))
        listed = cinema_pan_menu._cabinets_by_distance(game, gp)
        self.assertEqual([entry[0] for entry in listed], ["j1", "j3", "j2"])
        gaps = [entry[3] for entry in listed]
        self.assertLess(gaps[0], gaps[1])
        self.assertLess(gaps[1], gaps[2])
        self.assertEqual(cinema_pan_menu._gap_note(gaps[0]), " [2 m]")

    def test_nothing_is_hidden_for_being_far(self):
        from libs import cinema_pan_menu

        game, _map = build(cabinets=((10.0, 20.0), (200.0, 20.0)))
        gp = SimpleNamespace(player=SimpleNamespace(x=12.0, y=20.0, z=0.0))
        listed = cinema_pan_menu._cabinets_by_distance(game, gp)
        self.assertEqual(len(listed), 2)
        self.assertEqual(cinema_pan_menu._gap_note(190.0), " [190 m]")

    def test_an_unknown_position_lists_everything_anyway(self):
        from libs import cinema_pan_menu

        game, _map = build(cabinets=((10.0, 20.0), (200.0, 20.0)))
        listed = cinema_pan_menu._cabinets_by_distance(game, SimpleNamespace())
        self.assertEqual(len(listed), 2)
        self.assertTrue(all(entry[3] is None for entry in listed))
        self.assertEqual(cinema_pan_menu._gap_note(None), "")


class TheBandsTheRoomKeepsTests(unittest.TestCase):
    """What the room says about the bands its speakers keep -- and its holes.

    A crossover is silent about itself, and the read-out that names each fed
    speaker's band is the whole fix for a room that sounds thin. With a band in
    the vocabulary, the same read-out has a second job: the bands are read
    *together*, because between them they are the room -- and a three-way room
    whose edges do not meet loses exactly the octave nobody drew.
    """

    def plan(self, speakers, requested="auto"):
        game, _map = build(cabinets=((10.0, 5.0),), speakers=speakers)
        plan, _silent = cinema_plugin.room_plan(game, point((10.0, 5.0)),
                                                requested=requested, room_id="j1")
        return plan

    def note(self, speakers):
        return jukebox._cinema_band_note(self.plan(speakers))

    def test_a_room_of_full_range_speakers_says_nothing_at_all(self):
        said = self.note([dict(x=0.0, y=20.0, channel="front_l"),
                          dict(x=20.0, y=20.0, channel="front_r")])
        self.assertEqual(said, "")

    def test_a_mid_cabinet_is_said_as_the_middle_it_keeps(self):
        said = self.note([dict(x=0.0, y=20.0, channel="front_l", crossover=200,
                               crossover_high=3000),
                          dict(x=20.0, y=20.0, channel="front_r", crossover=200,
                               crossover_high=3000)])
        self.assertIn("between 200 Hz and 3 kHz", said)
        self.assertIn("keep", said)

    def test_a_sub_and_a_tweeter_leave_a_hole_the_line_names(self):
        said = self.note([dict(x=0.0, y=20.0, channel="front_l", crossover=120),
                          dict(x=20.0, y=20.0, channel="front_r", crossover=-1500)])
        self.assertIn("below 120 Hz", said)
        self.assertIn("above 1.5 kHz", said)
        self.assertIn("120 Hz to 1.5 kHz is not heard here at all", said)

    def test_a_three_way_room_whose_edges_meet_has_no_hole_to_report(self):
        said = self.note([dict(x=0.0, y=20.0, channel="front_l", crossover=120),
                          dict(x=20.0, y=20.0, channel="front_r", crossover=120,
                               crossover_high=1500),
                          dict(x=4.0, y=24.0, channel="front_c", crossover=-1500)])
        self.assertIn("between 120 Hz and 1.5 kHz", said)
        self.assertNotIn("not heard here", said)

    def test_the_octave_a_mid_cabinet_misses_is_named(self):
        # The sub stops at 120 and the mid starts at 200: nothing keeps the
        # band between them, which is heard as a thin room with every value on
        # screen looking deliberate.
        said = self.note([dict(x=0.0, y=20.0, channel="front_l", crossover=120),
                          dict(x=20.0, y=20.0, channel="front_r", crossover=200,
                               crossover_high=3000)])
        self.assertIn("120 Hz to 200 Hz is not heard here at all", said)

    def test_the_very_bottom_and_the_very_top_are_not_reported(self):
        # What a room keeps at the ends of hearing is the room's own business:
        # a room of two mids keeps no sub and no air, and saying so every time
        # would be noise on a read-out nobody asked to be lectured by.
        said = self.note([dict(x=0.0, y=20.0, channel="front_l", crossover=200,
                               crossover_high=3000),
                          dict(x=20.0, y=20.0, channel="front_r", crossover=200,
                               crossover_high=3000)])
        self.assertNotIn("20 Hz", said)
        self.assertNotIn("20 kHz", said)

    def test_one_full_range_speaker_keeps_the_whole_room(self):
        said = self.note([dict(x=0.0, y=20.0, channel="front_l", crossover=120),
                          dict(x=20.0, y=20.0, channel="front_r")])
        self.assertIn("below 120 Hz", said)
        self.assertNotIn("not heard here", said)


if __name__ == "__main__":
    unittest.main()
