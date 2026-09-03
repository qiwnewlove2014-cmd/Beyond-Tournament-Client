"""Tests for the diving water-audio transitions.

Covers the fixes for the reported "sound bends when diving / bubbles on
surfacing" symptoms:
- The camera (focused player) is the only owner of the swim splash sounds
  and of the focused player's voice source filter; Entity.water_check must
  not double-play the sound or fight over vc_source.
- The camera runs exactly ONE water filter task at a time and chains each
  new task from the current GAINHF (no overlapping automations = no wobble).
- Both filters share the same depth->GAINHF curve.
"""

import unittest
from types import SimpleNamespace

from libs.camera import Camera
from libs.objects.entity import Entity


class FakeFilter:
    def __init__(self):
        self.values = {}
        self.sets = []

    def set(self, name, value):
        self.values[name] = value
        self.sets.append((name, value))


class FakeSoundgroup:
    def __init__(self):
        self.applied = []
        self.effects = []

    def apply_filter(self, f, replace=True):
        self.applied.append((f, replace))

    def apply_effect(self, effect, slot):
        self.effects.append((effect, slot))


class FakeSource:
    def __init__(self):
        self.direct_filter = None


class FakeAutomationTask:
    def __init__(self, target, duration, step_callback, start_value):
        self.target = target
        self.duration = duration
        self.step_callback = step_callback
        self.start_value = start_value


class FakeMap:
    def __init__(self, tile):
        self.tile = tile

    def get_tile_at(self, x, y, z):
        return self.tile

    def get_reverb_at(self, x, y, z):
        return None

    def get_zone_at(self, x, y, z):
        return None

    def get_ambiences_at(self, x, y, z):
        return []

    def get_musics_at(self, x, y, z):
        return []


class FakeAudioMngr:
    def __init__(self):
        self.unbound = []
        self.filters = []
        self.apply_calls = []
        self.position = (0, 0, 0)
        self.orientation = (0, 0, 0)

    def play_unbound(self, path, x, y, z, **kw):
        self.unbound.append(path)

    def gen_filter(self, type="LOWPASS"):
        f = FakeFilter()
        self.filters.append(f)
        return f

    def release_filter(self, f):
        # Pooled-filter recycling contract (see AudioManager.release_filter).
        if f in self.filters:
            self.filters.remove(f)

    def apply_filter(self, f, exclude=None, replace=True, clear=False):
        self.apply_calls.append((f, list(exclude or []), replace))

    def create_soundgroup(self, direct):
        return FakeSoundgroup()


class FakeGame:
    def __init__(self, focus=None):
        self.audio_mngr = FakeAudioMngr()
        self.exclude_water = []
        self.automations = []
        self.ignore_others_water = False
        self.gameplay = SimpleNamespace(camera=SimpleNamespace(focus_object=focus))

    def automate(self, obj, attr, target, duration, callback=None,
                 time_step=20, step_callback=None, start_value=None, cancelable=True):
        task = FakeAutomationTask(target, duration, step_callback, start_value)
        self.automations.append(task)
        return task


class FakeEntity:
    def __init__(self, game, tile="air", focus=False, player=True,
                 depth=1.0, recorded_depth=1.0):
        self.game = game
        self.map = FakeMap(tile)
        self.x = 0
        self.y = 0
        self.z = 0
        self.in_water = False
        self.depth = depth
        self.recorded_depth = recorded_depth
        self.soundgroup = FakeSoundgroup()
        self.player = player
        self.vc_source = FakeSource()
        self.music_source = FakeSource()
        self.water_filter = None
        self._water_automation = None
        game.gameplay.camera.focus_object = self if focus else None

    @property
    def water_muffling(self):
        # Mirrors Entity.water_muffling exactly.
        return 0.02 + 0.33 * max(0.0, min(1.0, self.depth)) ** 1.5


def muffling_at(d):
    return 0.02 + 0.33 * max(0.0, min(1.0, d)) ** 1.5


class TestEntityWaterCheck(unittest.TestCase):
    def test_non_focus_enter_plays_splash_and_filters_own_sources(self):
        game = FakeGame()
        ent = FakeEntity(game, tile="underwater", focus=False)
        Entity.water_check(ent)

        self.assertEqual(game.audio_mngr.unbound, ["foley/swim/start/"])
        self.assertTrue(ent.in_water)
        self.assertEqual(game.exclude_water, [ent.soundgroup])
        self.assertEqual(len(game.automations), 1)
        task = game.automations[0]
        self.assertEqual(task.start_value, 1.0)
        self.assertAlmostEqual(task.target, muffling_at(1.0))
        self.assertEqual(task.duration, 250)  # fast ear-clamp on entry

        task.step_callback(0.3)
        self.assertIsNotNone(ent.vc_source.direct_filter)
        self.assertIs(ent.vc_source.direct_filter, game.audio_mngr.filters[-1])
        self.assertIs(ent.music_source.direct_filter, game.audio_mngr.filters[-1])

    def test_underwater_loudness_dips_with_the_muffle(self):
        game = FakeGame()
        ent = FakeEntity(game, tile="underwater", focus=False)
        Entity.water_check(ent)

        task = game.automations[0]
        flt = ent.water_filter
        task.step_callback(muffling_at(1.0))

        # Highs are cut AND overall loudness dips, both driven by one value:
        # realism pass makes submersion quieter as well as duller.
        self.assertAlmostEqual(flt.values["GAINHF"], muffling_at(1.0))
        self.assertAlmostEqual(flt.values["GAIN"], 0.35 + 0.65 * muffling_at(1.0))

        # Fully clear again (surfaced) must be perfectly neutral loudness.
        task.step_callback(1.0)
        self.assertAlmostEqual(flt.values["GAIN"], 1.0)

    def test_focus_enter_does_not_double_play_or_touch_vc_source(self):
        game = FakeGame()
        ent = FakeEntity(game, tile="underwater", focus=True)
        Entity.water_check(ent)

        # The camera owns the focused player's splash — no unbound duplicate.
        self.assertEqual(game.audio_mngr.unbound, [])
        self.assertTrue(ent.in_water)
        self.assertEqual(len(game.automations), 1)
        task = game.automations[0]

        task.step_callback(0.3)
        # vc_source is owned by the camera filter; entity must leave it alone.
        self.assertIsNone(ent.vc_source.direct_filter)
        # Own music source still muffled.
        self.assertIsNotNone(ent.music_source.direct_filter)

    def test_ignore_others_water_skips_automation_but_keeps_state(self):
        game = FakeGame()
        game.ignore_others_water = True
        ent = FakeEntity(game, tile="underwater", focus=False)
        Entity.water_check(ent)

        self.assertEqual(game.audio_mngr.unbound, ["foley/swim/start/"])
        self.assertTrue(ent.in_water)
        self.assertEqual(game.exclude_water, [ent.soundgroup])
        self.assertEqual(len(game.automations), 0)

    def test_exit_water_end_sound_and_ramp_back(self):
        game = FakeGame()
        ent = FakeEntity(game, tile="air", focus=False)
        ent.in_water = True
        game.exclude_water.append(ent.soundgroup)  # added when it entered
        Entity.water_check(ent)

        self.assertEqual(game.audio_mngr.unbound, ["foley/swim/end/"])
        self.assertFalse(ent.in_water)
        self.assertEqual(game.exclude_water, [])
        self.assertEqual(len(game.automations), 1)
        task = game.automations[0]
        self.assertEqual(task.target, 1.0)
        self.assertAlmostEqual(task.start_value, muffling_at(1.0))

    def test_focus_exit_plays_no_unbound_end_sound(self):
        game = FakeGame()
        ent = FakeEntity(game, tile="air", focus=True)
        ent.in_water = True
        game.exclude_water.append(ent.soundgroup)  # added when it entered
        Entity.water_check(ent)

        self.assertEqual(game.audio_mngr.unbound, [])
        self.assertFalse(ent.in_water)
        self.assertEqual(len(game.automations), 1)

    def test_none_sources_do_not_crash(self):
        game = FakeGame()
        ent = FakeEntity(game, tile="underwater", focus=False)
        ent.vc_source = None
        ent.music_source = None
        Entity.water_check(ent)
        game.automations[0].step_callback(0.3)  # must not raise

        ent2 = FakeEntity(game, tile="underwater", focus=True)
        ent2.vc_source = None
        ent2.music_source = None
        Entity.water_check(ent2)
        game.automations[-1].step_callback(0.3)  # must not raise

    def test_depth_change_applies_directly_and_cancels_enter_task(self):
        game = FakeGame()
        ent = FakeEntity(game, tile="underwater", focus=False)
        Entity.water_check(ent)
        self.assertEqual(len(game.automations), 1)

        ent.depth = 0.6
        ent.recorded_depth = 1.0
        Entity.water_check(ent)

        # The enter task was cancelled (direct set instead of a new task).
        self.assertEqual(len(game.automations), 0)
        filt = ent.water_filter
        self.assertIsNotNone(filt)
        self.assertAlmostEqual(filt.values.get("GAINHF"), muffling_at(0.6))
        self.assertIsNotNone(ent.vc_source.direct_filter)
        self.assertIsNotNone(ent.music_source.direct_filter)

    def test_focus_depth_change_skips_vc_source(self):
        game = FakeGame()
        ent = FakeEntity(game, tile="underwater", focus=True)
        ent.in_water = True
        ent.depth = 0.5
        ent.recorded_depth = 1.0
        Entity.water_check(ent)

        self.assertIsNone(ent.vc_source.direct_filter)
        self.assertIsNotNone(ent.music_source.direct_filter)


class TestCameraWaterTasks(unittest.TestCase):
    def make_camera(self, tile, depth=1.0):
        game = FakeGame()
        cam = Camera.__new__(Camera)
        cam.game = game
        cam.soundgroup = FakeSoundgroup()
        cam.reverb = None
        cam.currentzone = None
        cam.x = 0.0
        cam.y = 0.0
        cam.z = 0.0
        cam.sonar = False
        cam._water_automation = None
        cam._water_gainhf = 1.0
        focus = SimpleNamespace(
            map=FakeMap(tile),
            x=0,
            y=0,
            z=0,
            in_water=False,
            drownable=True,
            drown_clock=SimpleNamespace(restart=lambda: None),
            depth=depth,
            recorded_depth=depth,
            dead=False,
            vc_source=FakeSource(),
            soundgroup=FakeSoundgroup(),
            play_sound_calls=[],
        )
        focus.play_sound = lambda path, cat="": focus.play_sound_calls.append((path, cat))
        cam.focus_object = focus
        return cam, focus

    def test_enter_runs_single_task_with_shared_curve(self):
        cam, focus = self.make_camera("underwater")
        cam.move(0, 0, 0)

        self.assertEqual(focus.play_sound_calls, [("foley/swim/start/", "self")])
        self.assertTrue(focus.in_water)
        self.assertEqual(len(cam.game.automations), 1)
        task = cam.game.automations[0]
        self.assertAlmostEqual(task.target, muffling_at(1.0))
        self.assertEqual(task.start_value, 1.0)

    def test_depth_change_cancels_old_task_and_chains_from_current(self):
        cam, focus = self.make_camera("underwater")
        cam.move(0, 0, 0)
        self.assertEqual(len(cam.game.automations), 1)

        # Simulate the 500ms ramp progressing to 0.8 GAINHF.
        cam.game.automations[0].step_callback(0.8)
        self.assertEqual(cam._water_gainhf, 0.8)

        # Dive: depth 1.0 -> 0.6.
        focus.depth = 0.6
        focus.recorded_depth = 1.0
        cam.move(0, 0, 0)

        # Old task cancelled, exactly one task, chained from the current value.
        self.assertEqual(len(cam.game.automations), 1)
        task = cam.game.automations[0]
        self.assertAlmostEqual(task.target, muffling_at(0.6))
        self.assertEqual(task.start_value, 0.8)

        # Progress again, then surface.
        task.step_callback(0.4)
        self.assertEqual(cam._water_gainhf, 0.4)
        focus.map.tile = "air"
        cam.move(0, 0, 0)

        self.assertEqual(focus.play_sound_calls[-1], ("foley/swim/end/", "self"))
        self.assertEqual(len(cam.game.automations), 1)
        task2 = cam.game.automations[0]
        self.assertEqual(task2.target, 1.0)
        self.assertEqual(task2.start_value, 0.4)
        self.assertFalse(focus.in_water)

    def test_never_more_than_one_water_task(self):
        cam, focus = self.make_camera("underwater")
        for _ in range(5):
            focus.recorded_depth = focus.depth
            focus.depth = max(0.2, focus.depth - 0.2)
            cam.move(0, 0, 0)
            self.assertEqual(len(cam.game.automations), 1)

    def test_horizontal_walking_preserves_water_filter(self):
        cam, focus = self.make_camera("underwater")
        cam.move(0, 0, 0)
        flt = cam.get_water_filter()
        self.assertIsNotNone(flt)
        # Walking horizontally in X and Y without changing depth
        for step in range(1, 10):
            cam.move(step, step * 2, 0)
            # The filter must NOT be released or reset
            self.assertIs(cam.get_water_filter(), flt)
            self.assertTrue(focus.in_water)

    def test_curve_matches_entity_water_muffling(self):
        # The camera's depth->GAINHF curve must equal Entity.water_muffling so
        # the world and the player's own sounds clear at the same rate.
        ent = FakeEntity(FakeGame())
        for d in (0.0, 0.3, 0.7, 1.0):
            ent.depth = d
            self.assertAlmostEqual(muffling_at(d), ent.water_muffling)
        # At deepest bottom (depth = 0.0), muffling must reach 0.02
        ent.depth = 0.0
        self.assertAlmostEqual(ent.water_muffling, 0.02)
        # Realism pass: just-submerged (depth 1.0) must be far duller than
        # the old half-muffle (0.50).
        ent.depth = 1.0
        self.assertAlmostEqual(ent.water_muffling, 0.35)
        # Deep-dive pass: the ^1.5 curve must sink far harder than the old
        # straight line halfway down, so deep dives actually feel swallowed.
        # (Old linear value at 0.5 was 0.185.)
        ent.depth = 0.5
        self.assertAlmostEqual(ent.water_muffling, 0.02 + 0.33 * 0.5 ** 1.5)
        self.assertLess(ent.water_muffling, 0.15)
        ent.depth = 0.25
        self.assertLess(ent.water_muffling, 0.09)


class TestWaterMusicSources(unittest.TestCase):
    """The private music bot and jukeboxes use raw context.gen_source()
    sources that the apply_filter() sweep never sees — the camera's water
    filter must reach them explicitly (and new songs inherit it at birth).
    """

    def _camera_with_music(self):
        cam, focus = TestCameraWaterTasks().make_camera("underwater")
        game = cam.game
        stream_src = FakeSource()
        box_l = FakeSource()
        box_r = FakeSource()
        pending = object()  # placeholder entry with no sources yet
        game.gameplay.music_bot = SimpleNamespace(stream_source=stream_src)
        game.gameplay.jukebox_player = SimpleNamespace(players={
            "box1": {"transport": "direct",
                     "source": box_l, "secondary_source": box_r},
            "box2": {"transport": "relay_pending", "streamer": pending},
        })
        sources = [stream_src, box_l, box_r]
        return cam, focus, sources

    def test_dive_attaches_the_shared_filter_to_music_sources(self):
        cam, focus, sources = self._camera_with_music()
        cam.move(0, 0, 0)
        self.assertEqual(len(cam.game.automations), 1)
        cam.game.automations[0].step_callback(0.3)
        water = cam.get_water_filter()
        for src in sources:
            self.assertIs(src.direct_filter, water)

    def test_placeholder_entry_without_sources_does_not_crash(self):
        cam, focus, sources = self._camera_with_music()
        cam.move(0, 0, 0)
        cam.game.automations[0].step_callback(0.3)  # must not raise
        water = cam.get_water_filter()
        for src in sources:
            self.assertIs(src.direct_filter, water)

    def test_no_music_bot_returns_none(self):
        # Music-bot / jukebox-player attributes absent entirely (old state).
        cam, focus = TestCameraWaterTasks().make_camera("underwater")
        cam.move(0, 0, 0)
        cam.game.automations[0].step_callback(0.3)  # must not raise

    def test_surfacing_clears_the_filter_from_music_sources(self):
        from libs.camera import _apply_music_water_filter
        cam, focus, sources = self._camera_with_music()
        cam.move(0, 0, 0)
        cam.game.automations[0].step_callback(0.3)
        water = cam.get_water_filter()
        for src in sources:
            self.assertIs(src.direct_filter, water)
        # The camera's exit path clears the filter before releasing it.
        _apply_music_water_filter(cam.game, None)
        for src in sources:
            self.assertFalse(hasattr(src, "direct_filter"))


if __name__ == "__main__":
    unittest.main()
