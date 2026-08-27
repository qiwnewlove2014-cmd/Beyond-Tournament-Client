"""Tracking speech regressions, without audio devices or a live game/server."""

import ast
import math
import pathlib
import sys
import unittest
from functools import partial
from types import SimpleNamespace
from unittest.mock import patch

from libs.tracking_description import describe_tracking_direction
from libs.audio_diagnostics import AudioDiagnostics


def bearing(angle, distance=275, height=0):
    radians = math.radians(angle)
    return math.sin(radians) * distance, math.cos(radians) * distance, height


class TrackingDirectionTests(unittest.TestCase):
    def describe(self, angle, facing=0, height=0):
        return describe_tracking_direction(*bearing(angle, height=height), facing)

    def test_requested_front_phrases(self):
        expected = {
            -5: "in front and slightly off to the left",
            5: "in front and slightly off to the right",
            20: "in front a little ways off to the right",
            45: "in front and a fair distance off to the right",
            75: "slightly in front and a fair distance off to the right",
        }
        for angle, text in expected.items():
            with self.subTest(angle=angle):
                self.assertEqual(self.describe(angle), text)

    def test_requested_rear_phrases(self):
        expected = {
            175: "behind and slightly to the right",
            160: "behind and a little ways off to the right",
            135: "behind and a fair distance off to the right",
            105: "slightly behind and a fair distance off to the right",
        }
        for angle, text in expected.items():
            with self.subTest(angle=angle):
                self.assertEqual(self.describe(angle), text)

    def test_exact_axes_and_wraparound(self):
        for angle in (0, 360, -360, 720):
            self.assertEqual(self.describe(angle), "straight in front")
        for angle in (180, -180, 540):
            self.assertEqual(self.describe(angle), "straight behind")
        self.assertEqual(self.describe(90), "straight off to the right")
        self.assertEqual(self.describe(-90), "straight off to the left")

    def test_one_degree_offsets_are_not_called_straight(self):
        for angle in (1, -1, 14, -14, 166, -166, 179, -179):
            with self.subTest(angle=angle):
                self.assertNotIn("straight", self.describe(angle))

    def test_all_left_right_bearings_mirror(self):
        for angle in range(1, 180):
            with self.subTest(angle=angle):
                self.assertEqual(self.describe(-angle).replace("left", "right"), self.describe(angle))

    def test_angular_band_boundaries(self):
        cases = {
            14.999: "in front and slightly off to the right",
            15: "in front a little ways off to the right",
            29.999: "in front a little ways off to the right",
            30: "in front and a fair distance off to the right",
            59.999: "in front and a fair distance off to the right",
            60: "slightly in front and a fair distance off to the right",
            89.999: "slightly in front and a fair distance off to the right",
            90.001: "slightly behind and a fair distance off to the right",
            120: "slightly behind and a fair distance off to the right",
            120.001: "behind and a fair distance off to the right",
            150: "behind and a fair distance off to the right",
            150.001: "behind and a little ways off to the right",
            165: "behind and a little ways off to the right",
            165.001: "behind and slightly to the right",
        }
        for angle, text in cases.items():
            with self.subTest(angle=angle):
                self.assertEqual(self.describe(angle), text)

    def test_rotation_invariance_and_multiple_turns(self):
        for facing in (-720, -90, 0, 37, 90, 180, 270, 359, 720):
            for relative in (-175, -90, -20, 0, 5, 30, 60, 90, 135, 180):
                with self.subTest(facing=facing, relative=relative):
                    self.assertEqual(self.describe(facing + relative, facing), self.describe(relative))

    def test_angular_wording_does_not_depend_on_distance(self):
        for distance in (1, 4, 275, 20_000):
            self.assertEqual(describe_tracking_direction(*bearing(20, distance), 0), self.describe(20))

    def test_directly_vertical_targets_have_no_false_front_direction(self):
        for facing in (0, 90, 180, 270):
            self.assertEqual(describe_tracking_direction(0, 0, 4, facing), "directly above you")
            self.assertEqual(describe_tracking_direction(0, 0, -4, facing), "directly below you")
            self.assertEqual(describe_tracking_direction(0, 0, 0, facing), "right here")

    def test_horizontal_height_threshold_is_preserved(self):
        self.assertEqual(self.describe(0, height=2), "straight in front")
        self.assertEqual(self.describe(0, height=-2), "straight in front")
        self.assertEqual(self.describe(0, height=2.01), "straight in front and above you")
        self.assertEqual(self.describe(0, height=-2.01), "straight in front and below you")


def gameplay_harness():
    """Execute real Gameplay tracking methods, replacing only UI/audio owners."""
    source_path = pathlib.Path(__file__).resolve().parents[1] / "libs" / "gameplay.py"
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    gameplay = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "Gameplay")
    names = {
        "get_relative_direction_string", "_format_target_location", "_beacon_pitch",
        "open_tracking_menu", "_clean_name", "_get_target_label", "_gather_trackables",
        "start_tracking", "stop_tracking", "_is_trackable_entity", "_validate_tracking_target",
    }
    methods = [node for node in gameplay.body if isinstance(node, ast.FunctionDef) and node.name in names]
    assert len(methods) == len(names)
    update = next(node for node in gameplay.body if isinstance(node, ast.FunctionDef) and node.name == "update")
    beacon = next(node for node in update.body if isinstance(node, ast.If)
                  and ast.unparse(node.test) == "getattr(self, 'tracking_target', None) is not None")
    tick = ast.parse("def _tick_tracking(self):\n    pass").body[0]
    tick.body = [beacon]
    methods.append(tick)
    names.add("_tick_tracking")
    spoken = []

    class FakeMenu:
        def __init__(self, game, title, parrent):
            self.title, self.items = title, []

        def add_items(self, items):
            self.items.extend(items)

    namespace = {
        # Extracted tracking code uses the same no-op diagnostic path as an
        # opt-out Client; no frame, writer or audio device is created here.
        "audio_probe": AudioDiagnostics(enabled=False),
        "math": math, "partial": partial,
        "describe_tracking_direction": describe_tracking_direction,
        "movement": SimpleNamespace(get_3d_distance=lambda x, y, z, tx, ty, tz: math.dist((x, y, z), (tx, ty, tz))),
        "speak": spoken.append, "pygame": SimpleNamespace(KMOD_ALT=1),
        "menu": SimpleNamespace(Menu=FakeMenu), "menus": SimpleNamespace(set_default_sounds=lambda m: None),
    }
    exec(compile(ast.Module(body=methods, type_ignores=[]), str(source_path), "exec"), namespace)
    subject = type("TrackingHarness", (), {name: namespace[name] for name in names})()
    subject.player = SimpleNamespace(x=0, y=0, z=0, hfacing=0, name="local", dead=False)
    subject.beacons = []
    subject.game = SimpleNamespace(
        new_clock=lambda: SimpleNamespace(elapsed=1200, restart=lambda: None),
        audio_mngr=SimpleNamespace(play_unbound=lambda *a, **kw: subject.beacons.append((a, kw))),
    )
    subject.substates = []
    subject.add_substate = subject.substates.append
    subject.pop_last_substate = subject.substates.pop
    subject.tracking_target = None
    subject.map = SimpleNamespace(
        door_list=[], wallbuy_list=[], interactable_list=[], perk_machine_list=[],
        minigame_table_list=[], zone_list=[], entities={},
    )
    return subject, spoken


def object_entity(name="piano", **changes):
    values = dict(name=name, x=0, y=4, z=0, dead=False, player=False, object_tracking=True)
    values.update(changes)
    return SimpleNamespace(**values)


class TrackingGameplayTests(unittest.TestCase):
    def test_distance_is_last_and_single_tile_has_correct_grammar(self):
        gp, _ = gameplay_harness()
        self.assertEqual(gp._format_target_location(229, 0, 229, 0), "straight in front (229 tiles away)")
        self.assertEqual(gp._format_target_location(228, 0, -228, 0), "straight behind (228 tiles away)")
        self.assertEqual(gp._format_target_location(1, 0, 1, 0), "straight in front (1 tile away)")
        self.assertEqual(gp._format_target_location(0, 0.1, 0, 0), "right here")

    def test_height_keeps_distance_last(self):
        gp, _ = gameplay_harness()
        self.assertEqual(gp._format_target_location(4, 0, 0, 4), "directly above you (4 tiles away)")
        self.assertEqual(gp._format_target_location(5, 0, 3, -4), "straight in front and below you (5 tiles away)")

    def test_t_menu_selection_and_alt_t_share_wording(self):
        gp, spoken = gameplay_harness()
        gp.map.interactable_list = [SimpleNamespace(minx=0, maxx=0, miny=4, maxy=4, minz=0, maxz=0, label="jukebox")]
        gp.open_tracking_menu(0)
        menu = gp.substates[-1]
        self.assertEqual(menu.title, "Select object to track")
        self.assertEqual(menu.items[0][0], "jukebox: straight in front (4 tiles away)")
        menu.items[0][1]()
        self.assertEqual(spoken[-1], "jukebox: straight in front (4 tiles away)")
        self.assertEqual(len(gp.substates), 0)
        gp.open_tracking_menu(1)
        self.assertEqual(spoken[-1], "jukebox: straight in front (4 tiles away)")
        self.assertEqual(len(gp.substates), 0)

    def test_alt_t_recomputes_after_turning_and_moving(self):
        gp, spoken = gameplay_harness()
        gp.tracking_target = ("interactable", SimpleNamespace(label="jukebox"), (0, 4, 0))
        gp.player.hfacing = 90
        gp.open_tracking_menu(1)
        self.assertEqual(spoken[-1], "jukebox: straight off to the left (4 tiles away)")
        gp.player.x, gp.player.y, gp.player.hfacing = 0, 6, 0
        gp.open_tracking_menu(1)
        self.assertEqual(spoken[-1], "jukebox: straight behind (2 tiles away)")

    def test_dynamic_entity_uses_current_position_on_selection_and_status(self):
        gp, spoken = gameplay_harness()
        entity = object_entity("remote")
        gp.map.entities[entity.name] = entity
        gp.start_tracking(("entity", entity, (0, -8, 0)))
        self.assertEqual(spoken[-1], "remote: straight in front (4 tiles away)")
        self.assertEqual(gp.tracking_target[2], (0, 4, 0))
        entity.y = -5
        gp.open_tracking_menu(1)
        self.assertEqual(spoken[-1], "remote: straight behind (5 tiles away)")

    def test_alt_t_drops_tracking_prefix_without_adding_shift_shortcut(self):
        gp, spoken = gameplay_harness()
        gp.tracking_target = ("door", object(), (-38, 0, 0))
        gp.open_tracking_menu(2)  # A non-Alt modifier still opens the normal menu.
        self.assertFalse(spoken)
        self.assertEqual(gp.substates[-1].title, "Select object to track")
        gp.pop_last_substate()
        gp.open_tracking_menu(1)  # The existing Alt modifier still checks status.
        self.assertEqual(spoken[-1], "Door: straight off to the left (38 tiles away)")
        self.assertFalse(gp.substates)

    def test_all_target_types_keep_labels_and_use_the_shared_formatter(self):
        gp, _ = gameplay_harness()
        def box(**extra):
            return SimpleNamespace(minx=0, maxx=0, miny=4, maxy=4, minz=0, maxz=0, **extra)
        door = box(id="door1")
        gp.map.door_list = [door, door]
        gp.map.wallbuy_list = [box(weaponName="MP7", weaponCost=500)]
        gp.map.interactable_list = [box(label="jukebox")]
        gp.map.perk_machine_list = [box(label="Quick Revive")]
        gp.map.minigame_table_list = [box(label="Pong")]
        gp.map.zone_list = [box(zonename="Stage")]
        gp.map.entities = {"remote": object_entity("remote")}
        targets = gp._gather_trackables()
        self.assertEqual(len(targets), 7)
        self.assertEqual({row[1] for row in targets}, {"Door", "MP7, 500 points", "jukebox", "Quick Revive", "Pong", "Stage", "remote"})
        self.assertTrue(all(row[2] == "straight in front (4 tiles away)" for row in targets))

    def test_sort_stop_and_cancel_are_unchanged(self):
        gp, spoken = gameplay_harness()
        gp.map.entities = {"far": object_entity("far", y=8), "near": object_entity("near", y=2)}
        gp.open_tracking_menu(0)
        menu = gp.substates[-1]
        self.assertTrue(menu.items[0][0].startswith("near:"))
        menu.items[-1][1]()
        self.assertIsNone(gp.tracking_target)
        self.assertFalse(spoken)
        gp.start_tracking(("entity", gp.map.entities["near"], (0, 2, 0)))
        gp.open_tracking_menu(0)
        self.assertEqual(gp.substates[-1].items[0][0], "Stop Tracking")
        gp.substates[-1].items[0][1]()
        self.assertIsNone(gp.tracking_target)
        self.assertEqual(spoken[-1], "Tracking stopped.")

    def test_dead_player_cannot_open_tracking(self):
        gp, spoken = gameplay_harness()
        gp.player.dead = True
        gp.open_tracking_menu(0)
        self.assertFalse(gp.substates)
        self.assertFalse(spoken)

    def test_beacon_pitch_is_unchanged(self):
        gp, _ = gameplay_harness()
        self.assertAlmostEqual(gp._beacon_pitch(0, 4), 1.2)
        self.assertAlmostEqual(gp._beacon_pitch(4, 0), 1.0)
        self.assertAlmostEqual(gp._beacon_pitch(0, -4), 0.8)

    def test_objects_are_selected_by_metadata_not_actor_names(self):
        gp, _ = gameplay_harness()
        objects = [object_entity(name) for name in (
            "piano", "drumset", "radio", "PowerSwitch", "window", "motorcycle",
            "ball", "perkMachine", "grenade", "grappler", "flare", "dog-shaped-statue",
        )]
        actors = [object_entity(name, object_tracking=False) for name in (
            "alice", "horse", "boar", "zomby1", "dragon", "helper", "arbitrary_creature",
        )]
        actors += [object_entity("jukebox", player=True), object_entity("local"),
                   object_entity("destroyed", dead=True), SimpleNamespace(name="legacy_unknown")]
        gp.map.entities = {obj.name: obj for obj in objects + actors}
        rows = gp._gather_trackables()
        self.assertEqual({id(row[3][1]) for row in rows}, {id(obj) for obj in objects})

    def test_actor_only_map_has_no_trackable_objects(self):
        gp, spoken = gameplay_harness()
        gp.map.entities = {"person": object_entity("person", player=True),
                           "horse": object_entity("horse", object_tracking=False)}
        gp.open_tracking_menu(0)
        self.assertEqual(spoken, ["No trackable objects nearby."])
        self.assertFalse(gp.substates)

    def test_stale_menu_callback_cannot_select_an_actor(self):
        gp, spoken = gameplay_harness()
        obj = object_entity()
        gp.map.entities[obj.name] = obj
        gp.open_tracking_menu(0)
        callback = gp.substates[-1].items[0][1]
        obj.player = True
        callback()
        self.assertIsNone(gp.tracking_target)
        self.assertFalse(gp.substates)
        self.assertEqual(spoken, ["This target is no longer available for object tracking."])

    def test_removed_replaced_dead_or_reclassified_target_stops_once(self):
        for invalidation in ("removed", "replaced", "dead", "player", "creature"):
            for check in ("alt_t", "beacon"):
                with self.subTest(invalidation=invalidation, check=check):
                    gp, spoken = gameplay_harness()
                    obj = object_entity()
                    gp.map.entities[obj.name] = obj
                    gp.start_tracking(("entity", obj, (0, 4, 0)))
                    spoken.clear()
                    if invalidation == "removed":
                        gp.map.entities.clear()
                    elif invalidation == "replaced":
                        gp.map.entities[obj.name] = object_entity()
                    elif invalidation == "dead":
                        obj.dead = True
                    elif invalidation == "player":
                        obj.player = True
                    else:
                        obj.object_tracking = False
                    if check == "alt_t":
                        gp.open_tracking_menu(1)
                    else:
                        gp._tick_tracking()
                    self.assertIsNone(gp.tracking_target)
                    self.assertEqual(spoken, ["Tracking target lost."])
                    gp._tick_tracking()
                    self.assertEqual(spoken, ["Tracking target lost."])
                    self.assertFalse(gp.beacons)

    def test_object_beacon_tracks_movement(self):
        gp, _ = gameplay_harness()
        obj = object_entity()
        gp.map.entities[obj.name] = obj
        gp.start_tracking(("entity", obj, (0, 4, 0)))
        obj.y = 7
        gp._tick_tracking()
        self.assertEqual(gp.tracking_target[2], (0, 7, 0))
        self.assertEqual(gp.beacons[0][0], ("ui/facing.ogg", 0, 7, 0))


class TrackingSpawnMetadataTests(unittest.TestCase):
    def apply_packet(self, entity, packet):
        source = pathlib.Path(__file__).resolve().parents[1] / "libs" / "event_handeler.py"
        tree = ast.parse(source.read_text(encoding="utf-8"))
        cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "EventHandeler")
        method = next(node for node in cls.body if isinstance(node, ast.FunctionDef) and node.name == "_apply_spawn_entity")
        namespace = {"__package__": "libs", "__name__": "libs._tracking_test"}
        exec(compile(ast.Module(body=[method], type_ignores=[]), str(source), "exec"), namespace)
        handler = SimpleNamespace(
            gameplay=SimpleNamespace(
                map=SimpleNamespace(entities={entity.name: entity}, spawn_entity=lambda *a, **kw: entity),
                camera=SimpleNamespace(focus_object=object()),
            ),
            game=SimpleNamespace(put=lambda callback: None),
        )
        entity.apply_state = lambda *a, **kw: None
        logger = SimpleNamespace(log=lambda *a: None, log_exception=lambda *a: None)
        with patch.dict(sys.modules, {"libs.logger": logger}):
            namespace[method.name](handler, {"name": entity.name, **packet})

    def test_spawn_and_resync_refresh_metadata_strictly(self):
        for flag in (True, False, None, "true", 1):
            with self.subTest(flag=flag):
                ent = object_entity()
                self.apply_packet(ent, {"object_tracking": flag, "map_resync": True})
                self.assertEqual(ent.object_tracking, flag is True)

    def test_player_is_not_trackable_even_with_object_flag(self):
        ent = object_entity()
        self.apply_packet(ent, {"object_tracking": True, "player": True})
        self.assertFalse(ent.object_tracking)
        self.assertTrue(ent.player)

    def test_legacy_server_only_falls_back_to_typed_vehicle(self):
        for vehicle in (True, False):
            with self.subTest(vehicle=vehicle):
                ent = object_entity(is_vehicle=vehicle)
                self.apply_packet(ent, {})
                self.assertEqual(ent.object_tracking, vehicle)
                self.apply_packet(ent, {"object_tracking": False})
                self.assertFalse(ent.object_tracking)


if __name__ == "__main__":
    unittest.main()
