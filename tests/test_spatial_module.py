"""Characterization tests for the extracted spatial output module.

The move out of `AudioStreamer` must not change what a listener hears: the
distance ramp, the jukebox category, the cabinet trim and the wall-occlusion
tiers are pinned directly against `libs.music_bot.spatial`.
"""

import unittest
from types import SimpleNamespace


class _Source:
    def __init__(self, position):
        self.position = tuple(position)
        self.gain = 0.0
        self.direct_filter = None


class _Audio:
    def __init__(self, position=(0.0, 0.0, 0.0), categories=None):
        self.position = position
        self.volume_categories = categories or {"jukebox": [100]}
        self.filter = []
        self.efx = None
        self.filters = []

    def gen_filter(self, kind, *params):
        made = {"kind": kind, "params": params}
        self.filters.append(made)
        return made


class _Map:
    def __init__(self):
        self.tier = 0

    def valid_straight_path(self, start, end):
        return self.tier == 0

    def occlusion_tier(self, start, end):
        return self.tier


def _stream(audio, sources, ref=8.0, maximum=40.0, cabinet=1.0):
    from libs.music_bot import spatial

    stream = SimpleNamespace(
        cinema=None,
        game=SimpleNamespace(audio_mngr=audio, gameplay=None),
        spatial_src_l=sources[0],
        spatial_src_r=sources[1],
        spatial_ref=ref,
        spatial_max=maximum,
        spatial_base_gain=1.0,
        cabinet_gain=cabinet,
        jukebox_player=None,
    )
    stream._update_spatial_gain = lambda: spatial.update_spatial_gain(stream)
    return stream


class SpatialGainTests(unittest.TestCase):
    def test_distance_ramp_is_full_then_linear_then_silent(self):
        from libs.music_bot import spatial

        audio = _Audio(position=(0.0, 0.0, 0.0))
        left, right = _Source((8.0, 0.0, 0.0)), _Source((8.0, 0.0, 0.0))
        stream = _stream(audio, (left, right))
        spatial.update_spatial_gain(stream)
        self.assertAlmostEqual(left.gain, 1.0)
        self.assertAlmostEqual(right.gain, 1.0)

        audio.position = (32.0, 0.0, 0.0)  # dist 24: 16 m past the 8 m reference
        spatial.update_spatial_gain(stream)
        self.assertAlmostEqual(left.gain, 0.5)

        audio.position = (48.0, 0.0, 0.0)  # past max_distance
        spatial.update_spatial_gain(stream)
        self.assertAlmostEqual(left.gain, 0.0)

    def test_jukebox_category_and_cabinet_trim_multiply_the_ramp(self):
        from libs.music_bot import spatial

        audio = _Audio(categories={"jukebox": [50], "music": [10]})
        left, right = _Source((8.0, 0.0, 0.0)), _Source((8.0, 0.0, 0.0))
        stream = _stream(audio, (left, right), cabinet=0.5)
        spatial.update_spatial_gain(stream)
        # 100% stream x 50% jukebox category x 50% cabinet, at full ramp.
        self.assertAlmostEqual(left.gain, 0.25)
        self.assertAlmostEqual(right.gain, 0.25)

    def test_a_silent_cabinet_does_not_raycast(self):
        from libs.music_bot import spatial

        audio = _Audio(position=(1000.0, 0.0, 0.0))
        left, right = _Source((0.0, 0.0, 0.0)), _Source((0.0, 0.0, 0.0))
        stream = _stream(audio, (left, right))
        calls = []

        class Map(_Map):
            def occlusion_tier(self, start, end):
                calls.append((start, end))
                return 2

        stream.game.gameplay = SimpleNamespace(map=Map())
        spatial.update_spatial_gain(stream)
        self.assertEqual(calls, [])
        self.assertEqual(left.gain, 0.0)


class OcclusionTests(unittest.TestCase):
    def _occluded_stream(self, tier):
        from libs.music_bot import spatial

        audio = _Audio()
        global_filter = {"kind": "water"}
        audio.filter = [global_filter]
        left = _Source((10.0, 0.0, 0.0))
        right = _Source((10.0, 0.0, 0.0))
        stream = _stream(audio, (left, right))
        world = _Map()
        world.tier = tier
        stream.game.gameplay = SimpleNamespace(map=world)
        return spatial, audio, stream, left, right, world, global_filter

    def test_a_thick_wall_installs_the_heavy_lowpass(self):
        spatial, audio, stream, left, right, _world, _global = self._occluded_stream(2)
        spatial.update_spatial_gain(stream)
        self.assertEqual(left.direct_filter["params"], (("GAINHF", 0.05), ("GAIN", 0.22)))
        self.assertIs(left.direct_filter, right.direct_filter)

    def test_a_light_wall_installs_the_light_lowpass(self):
        spatial, audio, stream, left, _right, _world, _global = self._occluded_stream(1)
        spatial.update_spatial_gain(stream)
        self.assertEqual(left.direct_filter["params"], (("GAINHF", 0.45), ("GAIN", 0.75)))

    def test_a_cleared_wall_restores_the_active_global_filter(self):
        spatial, audio, stream, left, right, world, global_filter = self._occluded_stream(2)
        spatial.update_spatial_gain(stream)
        self.assertIsNot(left.direct_filter, global_filter)

        world.tier = 0
        spatial.update_spatial_gain(stream)
        self.assertIs(left.direct_filter, global_filter)
        self.assertIs(right.direct_filter, global_filter)

    def test_the_filter_objects_are_reused_across_tiers(self):
        spatial, audio, stream, left, right, world, _global = self._occluded_stream(2)
        spatial.update_spatial_gain(stream)
        heavy = left.direct_filter

        world.tier = 0
        spatial.update_spatial_gain(stream)
        world.tier = 2
        spatial.update_spatial_gain(stream)
        self.assertIs(left.direct_filter, heavy)
        self.assertEqual(len(audio.filters), 1)


class CinemaOutputTests(unittest.TestCase):
    def test_a_room_owns_its_own_shaping(self):
        from libs.music_bot import spatial

        class Cinema:
            def __init__(self):
                self.updates = 0
                self.volumes = []

            def update_output(self):
                self.updates += 1

            def set_cabinet_volume(self, volume):
                self.volumes.append(volume)

        cinema = Cinema()
        left, right = _Source((10.0, 0.0, 0.0)), _Source((10.0, 0.0, 0.0))
        left.gain = right.gain = 0.25
        stream = _stream(_Audio(), (left, right))
        stream.cinema = cinema

        spatial.update_spatial_gain(stream)
        self.assertEqual(cinema.updates, 1)
        self.assertEqual((left.gain, right.gain), (0.25, 0.25))

        spatial.set_cabinet_volume(stream, 40)
        self.assertAlmostEqual(stream.cabinet_gain, 0.4)
        self.assertEqual(cinema.volumes, [40])
        self.assertEqual(cinema.updates, 2)


if __name__ == "__main__":
    unittest.main()
