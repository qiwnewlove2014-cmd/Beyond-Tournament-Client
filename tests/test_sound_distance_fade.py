"""Tests for the smooth distance fade applied to one-shot entity sounds.

Entity footsteps/vocals (horses, boars, zombies, other players) must fade out
gradually with distance instead of staying faintly audible far away and then
cutting off abruptly at the broadcast edge.
"""

import unittest

from libs.objects.object import Object


class FakeSoundGroup:
    def __init__(self):
        self.position = (0, 0, 0)
        self.played = []

    def play(self, sound, looping=False, cat="miscelaneous", id="",
             rel_x=0, rel_y=0, rel_z=0, volume=100, pitch=1.0):
        self.played.append((sound, volume))


class FakeAudioMngr:
    def __init__(self, listener=(0, 0, 0)):
        self.position = listener

    def create_soundgroup(self, radius=0.5):
        return FakeSoundGroup()


class FakeGame:
    def __init__(self, listener=(0, 0, 0)):
        self.audio_mngr = FakeAudioMngr(listener)
        self.clock = 0

    def new_clock(self):
        return self


class FakeMap:
    pass


def make_object(x, y, z, listener=(0, 0, 0)):
    game = FakeGame(listener)
    obj = Object(game, FakeMap(), x, y, z)
    return game, obj


class SoundDistanceFadeTest(unittest.TestCase):
    def test_local_sound_full_volume(self):
        # Same position as the listener: never attenuated.
        _, obj = make_object(0, 0, 0, listener=(0, 0, 0))
        self.assertEqual(obj._distance_faded_volume(100), 100)
        self.assertEqual(obj._distance_faded_volume(150, 0, 0, -1), 150)

    def test_within_fade_start_full_volume(self):
        # 8 tiles or closer: unchanged.
        _, obj = make_object(5, 0, 0, listener=(0, 0, 0))
        self.assertEqual(obj._distance_faded_volume(100), 100)
        _, obj2 = make_object(8, 0, 0, listener=(0, 0, 0))
        self.assertEqual(obj2._distance_faded_volume(100), 100)

    def test_beyond_fade_end_silenced(self):
        # 40 tiles or further: silent (skipped).
        _, obj = make_object(40, 0, 0, listener=(0, 0, 0))
        self.assertEqual(obj._distance_faded_volume(100), 0)
        _, obj2 = make_object(0, 0, 60, listener=(0, 0, 0))
        self.assertEqual(obj2._distance_faded_volume(100), 0)

    def test_fade_is_monotonic_decreasing(self):
        # Volumes strictly decrease as distance grows, never jump.
        game, obj = make_object(0, 0, 0, listener=(0, 0, 0))
        volumes = []
        for dist in range(8, 41):
            obj.soundgroup.position = (dist, 0, 0)
            volumes.append(obj._distance_faded_volume(100))
        for i in range(1, len(volumes)):
            self.assertLessEqual(volumes[i], volumes[i - 1])
        self.assertEqual(volumes[0], 100)
        self.assertEqual(volumes[-1], 0)
        # Mid-way (24 tiles) should be noticeably quieter but not silent.
        self.assertGreater(volumes[16], 0)
        self.assertLess(volumes[16], 100)

    def test_rel_offsets_shift_the_distance(self):
        # rel_z=-1 is used for footsteps: distance measured from the actual
        # source position, not the object centre.
        game, obj = make_object(0, 0, 0, listener=(0, 0, 0))
        far = obj._distance_faded_volume(100, rel_x=0, rel_y=0, rel_z=-1)
        self.assertEqual(far, 100)
        obj.soundgroup.position = (0, 0, 0)
        near_edge = obj._distance_faded_volume(100, rel_x=30, rel_y=0, rel_z=0)
        self.assertGreater(near_edge, 0)
        self.assertLess(near_edge, 100)

    def test_play_sound_applies_fade(self):
        # play_sound routes the faded volume into the soundgroup and skips
        # entirely once beyond the fade end.
        game, obj = make_object(0, 0, 0, listener=(0, 0, 0))
        obj.soundgroup.position = (20, 0, 0)
        obj.play_sound("steps/floor/walk", volume=150)
        self.assertEqual(len(obj.soundgroup.played), 1)
        played_vol = obj.soundgroup.played[0][1]
        self.assertGreater(played_vol, 0)
        self.assertLess(played_vol, 150)

        obj.soundgroup.position = (41, 0, 0)
        obj.play_sound("steps/floor/walk", volume=150)
        self.assertEqual(len(obj.soundgroup.played), 1, "far sound must not play")

    def test_local_sounds_still_play(self):
        game, obj = make_object(0, 0, 0, listener=(0, 0, 0))
        obj.play_sound("steps/floor/walk", volume=100)
        self.assertEqual(obj.soundgroup.played[0][1], 100)


if __name__ == "__main__":
    unittest.main()
