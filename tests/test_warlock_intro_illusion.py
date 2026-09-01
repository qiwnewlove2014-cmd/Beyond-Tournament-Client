import math
import os
import sys
import unittest
from types import SimpleNamespace


CLIENT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if CLIENT_ROOT not in sys.path:
    sys.path.insert(0, CLIENT_ROOT)

from libs.systems.warlock_intro_illusion import WarlockIntroIllusion


class FakeClock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


class FakeSource:
    def __init__(self, position):
        self.position = position
        self.velocity = (0.0, 0.0, 0.0)
        self.gain = 2.0


class FakeSound:
    def __init__(self, position):
        self.source = FakeSource(position)
        self.destroyed = False

    def destroy(self, force=False):
        self.destroyed = bool(force)
        self.source = None


class FakeEfx:
    def __init__(self):
        self.sends = []

    def send(self, source, index, slot):
        self.sends.append((source, index, slot))


class FakeAudioManager:
    def __init__(self):
        self.unbound_sources = []
        self.efx = FakeEfx()
        self.plays = []

    def play_unbound_stereo_spatial(
            self, path, x, y, z, listener_x, listener_y, listener_z, **kwargs):
        sound = FakeSound((x, y, z))
        self.unbound_sources.append(sound)
        self.plays.append({
            "path": path,
            "position": (x, y, z),
            "listener": (listener_x, listener_y, listener_z),
            "kwargs": kwargs,
            "sound": sound,
        })
        return sound


class FakeMap:
    def __init__(self):
        self.slot = object()

    def get_reverb_at(self, _x, _y, _z):
        return SimpleNamespace(reverb=self.slot)


class WarlockIntroIllusionTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.audio = FakeAudioManager()
        self.player = SimpleNamespace(x=10.0, y=20.0, z=3.0, angle=0.0)
        self.map = FakeMap()
        self.gameplay = SimpleNamespace(
            player=self.player,
            map=self.map,
            game=SimpleNamespace(audio_mngr=self.audio),
        )
        self.manager = WarlockIntroIllusion(self.gameplay, time_source=self.clock)
        self.start_packet = {
            "name": "warlock_test",
            "sound": "entities/warlock/skill/intro/intro_speech.ogg",
            "volume": 220,
            "illusion": 1,
        }

    def test_main_sound_trembles_overhead_then_launches_far_on_one_source(self):
        self.assertTrue(self.manager.handle_packet(self.start_packet))
        self.assertEqual(len(self.audio.plays), 1)
        play = self.audio.plays[0]
        self.assertTrue(play["kwargs"]["as_mono"])
        self.assertEqual(play["listener"], (10.0, 20.0, 3.0))

        initial = play["sound"].source.position
        horizontal = math.hypot(initial[0] - self.player.x, initial[1] - self.player.y)
        self.assertLess(horizontal, 1.0)
        self.assertGreater(initial[2] - self.player.z, 3.0)

        self.clock.now += 0.25
        self.manager.update()
        moved = play["sound"].source.position
        self.assertNotEqual(moved, initial)
        self.assertEqual(len(self.audio.plays), 1, "updates must reuse the existing source")

        self.player.x += 8.0
        self.player.y -= 4.0
        self.clock.now += 0.25
        self.manager.update()
        followed = play["sound"].source.position
        followed_distance = math.hypot(followed[0] - self.player.x, followed[1] - self.player.y)
        self.assertLess(followed_distance, 1.0)

        self.clock.now += 4.7
        self.manager.update()
        launched = play["sound"].source.position
        launch_distance = math.hypot(
            launched[0] - self.player.x,
            launched[1] - self.player.y,
        )
        self.assertGreater(launch_distance, 10.0)
        self.assertLess(launch_distance, 25.0)
        self.assertEqual(len(self.audio.plays), 1, "launch must still reuse the existing source")

    def test_first_eighteen_seconds_move_forward_while_pattern_keeps_cycling(self):
        self.manager.handle_packet(self.start_packet)
        source = self.audio.plays[0]["sound"].source
        self.clock.now += 8.0
        self.manager.update()
        forward = source.position
        self.assertGreater(forward[1] - self.player.y, 4.0)
        self.assertEqual(len(self.audio.plays), 1)

    def test_final_eight_seconds_sweep_from_front_through_right_to_behind(self):
        self.manager.handle_packet(self.start_packet)
        source = self.audio.plays[0]["sound"].source
        arc_start = (
            WarlockIntroIllusion.INTRO_SPEECH_SECONDS
            - WarlockIntroIllusion.FINAL_HALF_CIRCLE_SECONDS
        )

        self.clock.now += arc_start + 1.0
        self.manager.update()
        front = source.position
        self.assertGreater(front[1] - self.player.y, 3.0)

        self.clock.now += 3.3
        self.manager.update()
        right = source.position
        self.assertGreater(right[0] - self.player.x, 3.0)

        self.clock.now += 3.4
        self.manager.update()
        behind = source.position
        self.assertLess(behind[1] - self.player.y, -3.0)

    def test_cue_uses_current_illusion_position_without_replacing_speech(self):
        self.manager.handle_packet(self.start_packet)
        main = self.audio.plays[0]["sound"]
        self.clock.now += 1.0
        self.assertTrue(self.manager.handle_packet({
            "name": "warlock_test",
            "sound": "entities/warlock/step/step1.ogg",
            "volume": 180,
            "illusion": 2,
        }))
        self.assertEqual(len(self.audio.plays), 2)
        self.assertIs(self.manager._states["warlock_test"].sound, main)
        self.assertIsNotNone(main.source)

    def test_stop_fades_before_destroying_owned_source(self):
        self.manager.handle_packet(self.start_packet)
        sound = self.audio.plays[0]["sound"]
        self.assertTrue(self.manager.handle_packet({
            "name": "warlock_test",
            "sound": "entities/warlock/step/step1.ogg",
            "illusion": 0,
        }))
        self.clock.now += WarlockIntroIllusion.STOP_FADE_SECONDS / 2
        self.manager.update()
        self.assertGreater(sound.source.gain, 0.0)
        self.assertLess(sound.source.gain, 2.0)

        self.clock.now += WarlockIntroIllusion.STOP_FADE_SECONDS
        self.manager.update()
        self.assertTrue(sound.destroyed)
        self.assertNotIn("warlock_test", self.manager._states)
        self.assertNotIn(sound, self.audio.unbound_sources)

    def test_reverb_updates_only_when_player_zone_changes(self):
        self.manager.handle_packet(self.start_packet)
        initial_sends = len(self.audio.efx.sends)
        self.clock.now += 0.1
        self.manager.update()
        self.assertEqual(len(self.audio.efx.sends), initial_sends)

        replacement = object()
        self.map.slot = replacement
        self.clock.now += 0.1
        self.manager.update()
        self.assertEqual(self.audio.efx.sends[-1][2], replacement)

    def test_invalid_profile_data_falls_back_to_normal_sound_handler(self):
        self.assertFalse(self.manager.handle_packet({
            "name": "warlock_test",
            "sound": "weapons/Mjolnir/fire/attack.ogg",
            "illusion": 1,
        }))
        self.assertFalse(self.manager.handle_packet({
            "name": "warlock_test",
            "sound": "entities/warlock/summon.ogg",
            "illusion": 9,
        }))
        self.assertEqual(self.audio.plays, [])

    def test_destroy_releases_all_owned_sources(self):
        self.manager.handle_packet(self.start_packet)
        sound = self.audio.plays[0]["sound"]
        self.manager.destroy()
        self.assertTrue(sound.destroyed)
        self.assertEqual(self.manager._states, {})
        self.assertEqual(self.audio.unbound_sources, [])


if __name__ == "__main__":
    unittest.main()
