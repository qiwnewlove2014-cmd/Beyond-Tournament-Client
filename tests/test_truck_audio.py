"""Tests for the truck's long-vehicle engine audio layout.

Verifies the multi-source design: 4 engine sources form a long rectangle
(front axle + rear axle), rear sources trail the driven path so the rumble
follows the wheels around corners (front first, back follows), and all
sources share the same speed-driven gain/pitch as the classic motorcycle.

The pure geometry/history helpers are exercised directly; heavier methods
(apply_state/loop) run on a bare Vehicle instance with stubbed audio.
"""

import math
import time
import unittest
from types import SimpleNamespace

from libs.objects.vehicle import Vehicle


class FakeSource:
    def __init__(self):
        self.gain = 0.0
        self.pitch = 1.0
        self.position = (0, 0, 0)
        self.paused = True

    def pause(self):
        self.paused = True

    def play(self):
        self.paused = False


class FakeSound:
    def __init__(self, source, soundgroup, label):
        self.source = source
        self._sg = soundgroup
        self._label = label

    def destroy(self, force=False):
        self._sg.labeled_sources.pop(self._label, None)


class FakeSoundgroup:
    def __init__(self):
        self.labeled_sources = {}
        self.position = (0, 0, 0)
        self.last_played = None
        self.played_paths = []

    def apply_effect(self, *args, **kw):
        pass

    def play(self, path, looping=False, id="", cat="miscelaneous", volume=100,
             pitch=1.0, rel_x=0, rel_y=0, rel_z=0, **kw):
        src = FakeSource()
        src.pitch = pitch
        src.looping = looping
        snd = FakeSound(src, self, id)
        self.labeled_sources[id] = snd
        self.last_played = {"path": path, "id": id, "rel_x": rel_x, "rel_y": rel_y}
        self.played_paths.append(path)
        return snd


class FakeAudioMngr:
    def __init__(self):
        self.position = (0, 0, 0)
        self.volume_categories = {"miscelaneous": [100]}

    def create_soundgroup(self, radius=0.5):
        return FakeSoundgroup()


def make_vehicle(source_length=5.0, source_width=1.6, trailer_lag_ms=300.0, interior=False):
    v = Vehicle.__new__(Vehicle)
    v.game = SimpleNamespace(
        audio_mngr=FakeAudioMngr(),
        direct_soundgroup=FakeSoundgroup(),
        gameplay=SimpleNamespace(player=SimpleNamespace(
            name="driver",
            falling=False,
            fall_clock=SimpleNamespace(restart=lambda: None),
        )),
    )
    v.map = SimpleNamespace(
        get_tile_at=lambda x, y, z: "ground",
        get_reverb_at=lambda x, y, z: None,
        entities={},
    )
    v.x, v.y, v.z = 10.0, 10.0, 1.0
    v.hfacing = 0.0
    v.vfacing = 0.0
    v.bfacing = 0.0
    v.on_turn = None
    v.soundgroup = FakeSoundgroup()
    v.name = "truck_1"
    v._player = False
    v.sound_profile = "truck2" if interior else "truck_mit_arcade_car_physics"
    v.dead = False
    v.falling = False
    v.surface = "ground"
    v.engine_on = False
    v.rider_name = ""
    v.target_speed = 0.0
    v.current_speed = 0.0
    v.engine_idle_pitch = 0.55
    v.engine_max_pitch = 1.15
    v.engine_idle_gain = 0.5
    v.engine_max_gain = 0.95
    v.wind_max_gain = 0.6
    v.engine_crossfade_start = 1.2
    v._last_audio_update = time.monotonic()
    v._wind_id = "vehicle_wind_truck_1"
    v._wind_reverb_slot = None
    v._engine_start_at = 0.0
    v._engine_waiting_to_start = False
    v._next_terrain_sound_at = 0.0
    v.source_length = source_length
    v.source_width = source_width
    v.trailer_lag_ms = trailer_lag_ms
    v.brake_gain = 0.85
    v.brake_pitch_min = 0.55
    v.brake_pitch_max = 1.3
    # Cabin interior audio (truck2) fields.
    v.interior_audio = interior
    v.interior_ext_scale = 0.25 if interior else 1.0
    v.interior_ext_drive_scale = 0.0 if interior else 1.0
    v.interior_gain = 0.9 if interior else 1.0
    v._cabin_fade = 0.0
    v.cabin_fade_rate = 1.5
    v.engine_idle_ext = "truck_idle_ext.ogg" if interior else "engine.ogg"
    v.engine_drive_ext = "truck_drive_ext.ogg" if interior else "engine.ogg"
    v.engine_idle_int = "truck_idle_int.ogg" if interior else "engine.ogg"
    v.engine_drive_int = "truck_drive_int.ogg" if interior else "engine.ogg"
    v.start_ext = "truck_start_ext.ogg" if interior else "start.ogg"
    v.start_int = "truck_start_int.ogg" if interior else "start.ogg"
    v.stop_ext = "truck_stop_ext.ogg" if interior else "stop.ogg"
    v.stop_int = "truck_stop_int.ogg" if interior else "stop.ogg"
    v._int_idle_id = "vehicle_int_idle_truck_1"
    v._int_drive_id = "vehicle_int_drive_truck_1"
    v._pos_history = []
    v._engine_sources = v._build_engine_layout()
    v._horn_id = "vehicle_horn_truck_1"
    v._brake_id = "vehicle_brake_truck_1"
    v.horn_on = False
    v.brake_on = False
    v.revving = False
    return v


def near(a, b, eps=0.001):
    return abs(a - b) <= eps


def pos(soundgroup, label):
    return soundgroup.labeled_sources[label].source.position


def engine_labels(v):
    return [cfg["label"] for cfg in v._engine_sources]


class TestEngineLayout(unittest.TestCase):
    def test_truck_has_four_sources_long_rectangle(self):
        v = make_vehicle()
        self.assertEqual(len(v._engine_sources), 4)
        labels = engine_labels(v)
        self.assertEqual(labels, [
            "vehicle_engine_fl", "vehicle_engine_fr",
            "vehicle_engine_rl", "vehicle_engine_rr",
        ])
        for cfg in v._engine_sources:
            self.assertEqual(cfg["fwd"], 2.5 if cfg["fwd"] > 0 else -2.5)
            self.assertIn(cfg["lat"], (-0.8, 0.8))
        front_lags = [c["lag_ms"] for c in v._engine_sources if c["fwd"] > 0]
        rear_lags = [c["lag_ms"] for c in v._engine_sources if c["fwd"] < 0]
        self.assertEqual(front_lags, [0.0, 0.0])
        self.assertEqual(rear_lags, [300.0, 300.0])

    def test_motorcycle_layout_is_single_point(self):
        v = make_vehicle(source_length=0.0)
        self.assertEqual(v._engine_sources, [])


class TestGeometry(unittest.TestCase):
    def test_offset_position_facing_north(self):
        # facing 0 = +y. fwd 2.5 -> (10, 12.5); right lat 0.8 -> (10.8, 10).
        p = Vehicle._offset_position(10, 10, 1, 0, 2.5, 0.0)
        self.assertTrue(near(p[0], 10.0) and near(p[1], 12.5))
        p = Vehicle._offset_position(10, 10, 1, 0, 0.0, 0.8)
        self.assertTrue(near(p[0], 10.8) and near(p[1], 10.0))

    def test_offset_position_facing_east(self):
        # facing 90 = +x.
        p = Vehicle._offset_position(10, 10, 1, 90, 2.5, 0.0)
        self.assertTrue(near(p[0], 12.5) and near(p[1], 10.0))

    def test_sample_position_picks_lagged_sample(self):
        v = make_vehicle()
        now = time.monotonic()
        v._pos_history = [
            (now - 0.5, 10.0, 10.0, 1.0, 0.0),
            (now - 0.1, 10.0, 11.0, 1.0, 0.0),
        ]
        sample = v._sample_position(300.0)
        self.assertIsNotNone(sample)
        self.assertTrue(near(sample[1], 10.0) and near(sample[2], 10.0))
        # Zero lag -> no lagged sample (current position is used).
        self.assertIsNone(v._sample_position(0.0))


class TestMultiSourceEngine(unittest.TestCase):
    def test_engine_start_creates_all_sources_on_straight_line(self):
        v = make_vehicle()
        v.apply_state(speed=0.5, engine_on=True, rider="driver", facing=0.0, initial=True)
        for label in engine_labels(v):
            self.assertIn(label, v.soundgroup.labeled_sources)
        # Front pair at (9.2/10.8, 12.5), rear pair at (9.2/10.8, 7.5) - long
        # rectangle: lat +-0.8 around x=10, fwd +-2.5 around y=10 (facing 0).
        self.assertTrue(near(pos(v.soundgroup, "vehicle_engine_fl")[0], 9.2))
        self.assertTrue(near(pos(v.soundgroup, "vehicle_engine_fl")[1], 12.5))
        self.assertTrue(near(pos(v.soundgroup, "vehicle_engine_fr")[0], 10.8))
        self.assertTrue(near(pos(v.soundgroup, "vehicle_engine_rr")[0], 10.8))
        self.assertTrue(near(pos(v.soundgroup, "vehicle_engine_rr")[1], 7.5))
        # Front/rear separation equals the truck length.
        front_y = pos(v.soundgroup, "vehicle_engine_fl")[1]
        rear_y = pos(v.soundgroup, "vehicle_engine_rr")[1]
        self.assertTrue(near(front_y - rear_y, 5.0))

    def test_rear_sources_follow_the_driven_path_around_corner(self):
        v = make_vehicle()
        v.apply_state(speed=0.5, engine_on=True, rider="driver", facing=0.0, initial=True)
        # Drive straight north to the corner, pushing path samples.
        v.x, v.y = 10.0, 11.5
        v.apply_state(speed=0.5, engine_on=True, rider="driver", facing=0.0)
        # Backdate the history so the 300ms trailer lag can pick an old sample
        # (real-time pushes in a fast test are all younger than the lag).
        now = time.monotonic()
        for i, s in enumerate(v._pos_history):
            v._pos_history[i] = (now - 1.5 + i * 0.5, s[1], s[2], s[3], s[4])
        # Turn east at (11, 11.5) - the truck nose swings to +x.
        v.x, v.y = 11.0, 11.5
        v.apply_state(speed=0.5, engine_on=True, rider="driver", facing=90.0)
        front = pos(v.soundgroup, "vehicle_engine_fl")
        rear = pos(v.soundgroup, "vehicle_engine_rl")
        # Front is on the new east-facing heading (ahead in +x, slight left swing).
        self.assertTrue(front[0] > 12.0)
        self.assertTrue(near(front[1], 12.3))
        # Rear still parks on the old north path (trailer cut-in behind the corner).
        self.assertTrue(near(rear[0], 9.2) and rear[1] < 11.0)

    def test_gain_and_pitch_drive_all_sources(self):
        v = make_vehicle()
        v.apply_state(speed=0.8, engine_on=True, rider="driver", facing=0.0, initial=True)
        v.current_speed = 0.8
        for _ in range(40):
            v._last_audio_update = time.monotonic() - 0.1
            v.loop()
        for label in engine_labels(v):
            src = v.soundgroup.labeled_sources[label].source
            self.assertGreater(src.gain, 0.3)
            self.assertGreater(src.pitch, v.engine_idle_pitch)
            self.assertLessEqual(src.pitch, v.engine_max_pitch + 0.001)

    def test_stop_after_waiting_destroys_all_sources(self):
        v = make_vehicle()
        # Non-initial start: engine sources created paused while waiting.
        v.apply_state(speed=0.5, engine_on=True, rider="driver", facing=0.0)
        for label in engine_labels(v):
            self.assertIn(label, v.soundgroup.labeled_sources)
            self.assertTrue(v.soundgroup.labeled_sources[label].source.paused)
        # Stop while still waiting -> all four destroyed immediately.
        v.apply_state(speed=0.0, engine_on=False, rider="", facing=0.0)
        for label in engine_labels(v):
            self.assertNotIn(label, v.soundgroup.labeled_sources)

    def test_start_sound_offset_toward_cab(self):
        v = make_vehicle()
        v.apply_state(speed=0.5, engine_on=True, rider="driver", facing=0.0)
        played = v.soundgroup.last_played
        self.assertEqual(played["path"], "vehicles/truck_mit_arcade_car_physics/start.ogg")
        self.assertTrue(near(played["rel_x"], 0.0))
        self.assertTrue(near(played["rel_y"], 2.5))  # cab sits forward (+y at facing 0)

    def test_spawn_apply_then_state_apply_keeps_engine(self):
        # The spawn packet now carries engine_on=true (autoIdle) and the client
        # applies it on spawn; the later vehicle_state broadcast re-applies the
        # same state. Neither the double-start nor the paused sources may end
        # up destroying the engine.
        v = make_vehicle()
        v.apply_state(speed=0.0, engine_on=True, rider="", facing=0.0)  # spawn packet
        self.assertTrue(v._engine_waiting_to_start)
        v.apply_state(speed=0.0, engine_on=True, rider="", facing=0.0)  # state broadcast
        self.assertTrue(v._engine_waiting_to_start)  # not re-triggered, not killed
        labels = engine_labels(v)
        for label in labels:
            self.assertIn(label, v.soundgroup.labeled_sources)
        # Let the crossfade elapse and confirm the engine actually runs.
        v._engine_start_at = time.monotonic() - 0.1
        for _ in range(30):
            v._last_audio_update = time.monotonic() - 0.1
            v.loop()
        for label in labels:
            snd = v.soundgroup.labeled_sources[label]
            self.assertFalse(snd.source.paused)
            self.assertGreater(snd.source.gain, 0.3)

    def test_horn_press_loops_and_release_destroys(self):
        v = make_vehicle()
        v.apply_state(speed=0.0, engine_on=False, rider="", facing=0.0, horn_on=True)
        self.assertTrue(v.horn_on)
        snd = v.soundgroup.labeled_sources.get(v._horn_id)
        self.assertIsNotNone(snd)
        self.assertTrue(snd.source.looping)
        # Pressing again (repeated state packets while held) must not churn.
        v.apply_state(speed=0.0, engine_on=False, rider="", facing=0.0, horn_on=True)
        self.assertIn(v._horn_id, v.soundgroup.labeled_sources)
        # Release destroys the looping blast.
        v.apply_state(speed=0.0, engine_on=False, rider="", facing=0.0, horn_on=False)
        self.assertFalse(v.horn_on)
        self.assertNotIn(v._horn_id, v.soundgroup.labeled_sources)

    def test_brake_loop_pitch_follows_wheel_speed(self):
        v = make_vehicle()
        v.apply_state(speed=1.0, engine_on=True, rider="", facing=0.0, brake_on=True)
        for _ in range(40):
            v._last_audio_update = time.monotonic() - 0.1
            v.loop()
        snd = v.soundgroup.labeled_sources.get(v._brake_id)
        self.assertIsNotNone(snd)
        # Full speed -> highest brake pitch.
        self.assertTrue(near(snd.source.pitch, v.brake_pitch_max, 0.01))
        # Wheels slow -> the brake note drops toward its minimum.
        v.apply_state(speed=0.0, engine_on=True, rider="", facing=0.0, brake_on=True)
        for _ in range(40):
            v._last_audio_update = time.monotonic() - 0.1
            v.loop()
        self.assertTrue(near(snd.source.pitch, v.brake_pitch_min, 0.01))
        # Releasing the pedal destroys the loop.
        v.apply_state(speed=0.0, engine_on=True, rider="", facing=0.0, brake_on=False)
        v._last_audio_update = time.monotonic() - 0.1
        v.loop()
        self.assertNotIn(v._brake_id, v.soundgroup.labeled_sources)

    def test_burnout_holds_rev_at_freewheel_level(self):
        v = make_vehicle()
        v.apply_state(speed=0.5, engine_on=True, rider="", facing=0.0, initial=True)
        for _ in range(40):
            v._last_audio_update = time.monotonic() - 0.1
            v.loop()
        # Engine has settled mid-way (not idle, not max).
        mid_pitch = v.soundgroup.labeled_sources["vehicle_engine_fl"].source.pitch
        self.assertGreater(mid_pitch, v.engine_idle_pitch + 0.05)
        self.assertLess(mid_pitch, v.engine_max_pitch - 0.05)
        # Burnout: server reports revving with 0 wheel speed. The rev must
        # hold at the level it had (free-wheel), neither decaying to idle nor
        # jumping to max.
        v.apply_state(speed=0.0, engine_on=True, rider="", facing=0.0, revving=True)
        frozen = v.current_speed
        for _ in range(40):
            v._last_audio_update = time.monotonic() - 0.1
            v.loop()
        self.assertTrue(near(v.current_speed, frozen, 0.001))  # speed frozen
        for label in engine_labels(v):
            snd = v.soundgroup.labeled_sources[label]
            self.assertTrue(near(snd.source.pitch, mid_pitch, 0.02))  # rev held
            self.assertLess(snd.source.pitch, v.engine_max_pitch - 0.01)  # not max
        # Releasing the brake resumes acceleration from the same level.
        v.apply_state(speed=1.0, engine_on=True, rider="", facing=0.0, revving=False)
        for _ in range(20):
            v._last_audio_update = time.monotonic() - 0.1
            v.loop()
        self.assertGreater(v.current_speed, frozen)

def make_truck2(source_length=0.0):
    return make_vehicle(source_length=source_length, interior=True)


class TestInteriorAudio(unittest.TestCase):
    def test_interior_spawn_creates_exterior_idle_and_drive_loops(self):
        v = make_truck2()
        # truck2 is single-point (sourceLength 0 in the registry): the world
        # engine is rendered as idle + drive loops at the same point.
        v.apply_state(speed=0.0, engine_on=True, rider="", facing=0.0, initial=True)
        self.assertIn("vehicle_engine", v.soundgroup.labeled_sources)
        self.assertIn("vehicle_engine_drive", v.soundgroup.labeled_sources)

    def test_no_interior_loops_without_local_rider(self):
        v = make_truck2()
        v.apply_state(speed=0.0, engine_on=True, rider="other", facing=0.0, initial=True)
        # No cabin layer for a non-local rider.
        self.assertNotIn(v._int_idle_id, v.game.direct_soundgroup.labeled_sources)
        self.assertNotIn(v._int_drive_id, v.game.direct_soundgroup.labeled_sources)

    def test_local_rider_gets_interior_loops_and_muffled_exterior(self):
        v = make_truck2()
        v.apply_state(speed=0.0, engine_on=True, rider="driver", facing=0.0, initial=True)
        self.assertIn(v._int_idle_id, v.game.direct_soundgroup.labeled_sources)
        self.assertIn(v._int_drive_id, v.game.direct_soundgroup.labeled_sources)
        for _ in range(40):
            v._last_audio_update = time.monotonic() - 0.1
            v.loop()
        # Interior idle audible, exterior muffled to interiorExtScale.
        int_snd = v.game.direct_soundgroup.labeled_sources[v._int_idle_id].source
        self.assertGreater(int_snd.gain, 0.2)
        ext_snd = v.soundgroup.labeled_sources["vehicle_engine"].source
        self.assertLessEqual(ext_snd.gain, 0.3)
        self.assertTrue(near(ext_snd.gain, v.interior_ext_scale * v.engine_idle_gain, 0.05))

    def test_crossfade_switches_idle_to_drive_with_speed(self):
        v = make_truck2()
        v.apply_state(speed=0.0, engine_on=True, rider="", facing=0.0, initial=True)
        v.current_speed = 0.0
        for _ in range(40):
            v._last_audio_update = time.monotonic() - 0.1
            v.loop()
        idle_gain = v.soundgroup.labeled_sources["vehicle_engine"].source.gain
        drive_gain = v.soundgroup.labeled_sources["vehicle_engine_drive"].source.gain
        self.assertGreater(idle_gain, drive_gain)
        # Accelerate: drive loop takes over.
        v.apply_state(speed=1.0, engine_on=True, rider="", facing=0.0)
        v.current_speed = 1.0
        for _ in range(40):
            v._last_audio_update = time.monotonic() - 0.1
            v.loop()
        idle_gain = v.soundgroup.labeled_sources["vehicle_engine"].source.gain
        drive_gain = v.soundgroup.labeled_sources["vehicle_engine_drive"].source.gain
        self.assertGreater(drive_gain, idle_gain)

    def test_local_rider_start_uses_interior_sound(self):
        v = make_truck2()
        # Non-initial start while the local player is the rider -> start_int
        # plays through the direct soundgroup (cabin stereo).
        v.apply_state(speed=0.0, engine_on=True, rider="driver", facing=0.0)
        self.assertIn(
            "vehicles/truck2/truck_start_int.ogg",
            v.game.direct_soundgroup.played_paths,
        )

    def test_non_rider_start_uses_exterior_sound(self):
        v = make_truck2()
        v.apply_state(speed=0.0, engine_on=True, rider="other", facing=0.0)
        self.assertIn(
            "vehicles/truck2/truck_start_ext.ogg",
            v.soundgroup.played_paths,
        )
        # A non-rider never hears the cabin start.
        self.assertNotIn(
            "vehicles/truck2/truck_start_int.ogg",
            v.game.direct_soundgroup.played_paths,
        )

    def test_destroy_cleans_interior_loops(self):
        v = make_truck2()
        v.apply_state(speed=0.0, engine_on=True, rider="driver", facing=0.0, initial=True)
        self.assertIn(v._int_idle_id, v.game.direct_soundgroup.labeled_sources)
        v._destroy_interior_loops()
        self.assertNotIn(v._int_idle_id, v.game.direct_soundgroup.labeled_sources)
        self.assertNotIn(v._int_drive_id, v.game.direct_soundgroup.labeled_sources)

    def test_interior_loops_unpause_at_crossfade_seam(self):
        v = make_truck2()
        # Non-initial start: everything created paused while waiting.
        v.apply_state(speed=0.0, engine_on=True, rider="driver", facing=0.0)
        int_snd = v.game.direct_soundgroup.labeled_sources[v._int_idle_id].source
        self.assertTrue(int_snd.paused)
        # Let the crossfade elapse; interior + exterior must both unpause.
        v._engine_start_at = time.monotonic() - 0.1
        for _ in range(30):
            v._last_audio_update = time.monotonic() - 0.1
            v.loop()
        self.assertFalse(int_snd.paused)
        world_snd = v.soundgroup.labeled_sources["vehicle_engine"].source
        self.assertFalse(world_snd.paused)

    def test_engine_off_destroys_interior_loops(self):
        v = make_truck2()
        v.apply_state(speed=0.0, engine_on=True, rider="driver", facing=0.0, initial=True)
        self.assertIn(v._int_idle_id, v.game.direct_soundgroup.labeled_sources)
        v.apply_state(speed=0.0, engine_on=False, rider="", facing=0.0)
        for _ in range(10):
            v._last_audio_update = time.monotonic() - 0.1
            v.loop()
        self.assertNotIn(v._int_idle_id, v.game.direct_soundgroup.labeled_sources)
        self.assertNotIn(v._int_drive_id, v.game.direct_soundgroup.labeled_sources)

    def test_rider_drive_ext_killed_but_idle_muffled(self):
        v = make_truck2()
        v.apply_state(speed=1.0, engine_on=True, rider="driver", facing=0.0, initial=True)
        v.current_speed = 1.0
        for _ in range(60):
            v._last_audio_update = time.monotonic() - 0.1
            v.loop()
        # Cabin fade has completed: the exterior DRIVE loop is silent for the
        # rider (interiorExtDriveScale=0) while an outside listener still hears
        # it at full volume.
        drive = v.soundgroup.labeled_sources["vehicle_engine_drive"].source
        self.assertLess(drive.gain, 0.02)
        v2 = make_truck2()
        v2.apply_state(speed=1.0, engine_on=True, rider="other", facing=0.0, initial=True)
        v2.current_speed = 1.0
        for _ in range(60):
            v2._last_audio_update = time.monotonic() - 0.1
            v2.loop()
        drive2 = v2.soundgroup.labeled_sources["vehicle_engine_drive"].source
        self.assertGreater(drive2.gain, 0.2)
        # At idle, the rider hears the muffled exterior idle (interiorExtScale)
        # instead of nothing at all.
        v3 = make_truck2()
        v3.apply_state(speed=0.0, engine_on=True, rider="driver", facing=0.0, initial=True)
        v3.current_speed = 0.0
        for _ in range(60):
            v3._last_audio_update = time.monotonic() - 0.1
            v3.loop()
        idle = v3.soundgroup.labeled_sources["vehicle_engine"].source
        self.assertGreater(idle.gain, 0.01)
        self.assertLessEqual(idle.gain, v3.interior_ext_scale * v3.engine_idle_gain + 0.02)

    def test_interior_idle_pitch_ramps_with_speed(self):
        v = make_truck2()
        v.apply_state(speed=0.0, engine_on=True, rider="driver", facing=0.0, initial=True)
        v.current_speed = 0.0
        for _ in range(60):
            v._last_audio_update = time.monotonic() - 0.1
            v.loop()
        int_idle = v.game.direct_soundgroup.labeled_sources[v._int_idle_id].source
        low_pitch = int_idle.pitch
        # Accelerate: the interior idle loop must pitch up (not sit at idle).
        v.apply_state(speed=1.0, engine_on=True, rider="driver", facing=0.0)
        v.current_speed = 1.0
        for _ in range(60):
            v._last_audio_update = time.monotonic() - 0.1
            v.loop()
        int_idle = v.game.direct_soundgroup.labeled_sources[v._int_idle_id].source
        self.assertGreater(int_idle.pitch, low_pitch + 0.1)
        self.assertLess(int_idle.pitch, v.engine_max_pitch + 0.01)

    def test_cabin_fade_smooths_enter_and_exit(self):
        v = make_truck2()
        # Enter: exterior starts at full volume, fades toward muffled over time.
        v.apply_state(speed=0.0, engine_on=True, rider="driver", facing=0.0, initial=True)
        v._cabin_fade = 0.0
        for _ in range(3):
            v._last_audio_update = time.monotonic() - 0.1
            v.loop()
        self.assertGreater(v._cabin_fade, 0.0)
        self.assertLess(v._cabin_fade, 0.6)
        for _ in range(60):
            v._last_audio_update = time.monotonic() - 0.1
            v.loop()
        self.assertGreater(v._cabin_fade, 0.99)
        # Exit: interior loops fade out instead of being cut instantly.
        v.apply_state(speed=0.0, engine_on=True, rider="", facing=0.0)
        for _ in range(3):
            v._last_audio_update = time.monotonic() - 0.1
            v.loop()
        self.assertLess(v._cabin_fade, 1.0)
        self.assertIn(v._int_idle_id, v.game.direct_soundgroup.labeled_sources)
        for _ in range(120):
            v._last_audio_update = time.monotonic() - 0.1
            v.loop()
        self.assertNotIn(v._int_idle_id, v.game.direct_soundgroup.labeled_sources)


    def test_exterior_idle_pitch_ramps_with_speed(self):
        v = make_truck2()
        v.apply_state(speed=0.0, engine_on=True, rider="other", facing=0.0, initial=True)
        v.current_speed = 0.0
        for _ in range(60):
            v._last_audio_update = time.monotonic() - 0.1
            v.loop()
        ext_idle = v.soundgroup.labeled_sources["vehicle_engine"].source
        low_pitch = ext_idle.pitch
        # Accelerate (outside listener): the exterior idle loop must pitch up
        # with the engine, not sit at idle pitch.
        v.apply_state(speed=1.0, engine_on=True, rider="other", facing=0.0)
        v.current_speed = 1.0
        for _ in range(60):
            v._last_audio_update = time.monotonic() - 0.1
            v.loop()
        ext_idle = v.soundgroup.labeled_sources["vehicle_engine"].source
        self.assertGreater(ext_idle.pitch, low_pitch + 0.1)
        self.assertLessEqual(ext_idle.pitch, v.engine_max_pitch + 0.01)

    def test_rider_never_hears_ext_drive_while_revving(self):
        # The driver must never hear truck_drive_ext even at full throttle —
        # only outside listeners get the full drive loop.
        v = make_truck2()
        v.apply_state(speed=1.0, engine_on=True, rider="driver", facing=0.0, initial=True)
        v.current_speed = 1.0
        for _ in range(60):
            v._last_audio_update = time.monotonic() - 0.1
            v.loop()
        drive = v.soundgroup.labeled_sources["vehicle_engine_drive"].source
        self.assertLess(drive.gain, 0.02)
        # While the rider hears the interior idle revving up...
        int_idle = v.game.direct_soundgroup.labeled_sources[v._int_idle_id].source
        self.assertGreater(int_idle.pitch, v.engine_idle_pitch + 0.1)


    def test_drive_loop_pitch_ramps_with_speed_too(self):
        # Every loop (idle AND drive, exterior) revs up with speed — the drive
        # loop no longer sits fixed at max pitch, so acceleration is heard as
        # a pitch rise rather than just getting louder.
        v = make_truck2()
        v.apply_state(speed=0.0, engine_on=True, rider="other", facing=0.0, initial=True)
        v.current_speed = 0.0
        for _ in range(60):
            v._last_audio_update = time.monotonic() - 0.1
            v.loop()
        drive = v.soundgroup.labeled_sources["vehicle_engine_drive"].source
        low_pitch = drive.pitch
        v.apply_state(speed=1.0, engine_on=True, rider="other", facing=0.0)
        v.current_speed = 1.0
        for _ in range(60):
            v._last_audio_update = time.monotonic() - 0.1
            v.loop()
        drive = v.soundgroup.labeled_sources["vehicle_engine_drive"].source
        self.assertGreater(drive.pitch, low_pitch + 0.1)
        self.assertLessEqual(drive.pitch, v.engine_max_pitch + 0.01)



if __name__ == "__main__":
    unittest.main()
