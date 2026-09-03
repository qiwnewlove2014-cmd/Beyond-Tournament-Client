"""Map-loop integration without OpenAL, network or live configuration."""

import os
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import weakref

import cyal
from libs.audio_manager import AudioManager
from libs.audio.soundgroup import SoundGroup
from libs.world_map import Ambience, Map, Pannable, SoundSource, _play_map_loop


class Cache:
    def __init__(self):
        self.ready = {}
        self.failed = set()
        self.requests = []
        self.begin_map = Mock()
    def get(self, path):
        self.requests.append(path)
        return self.ready.get(path)
    def status(self, path):
        return "failed" if path in self.failed else "ready" if path in self.ready else "pending"
    def retry(self, path):
        self.failed.discard(path)


class FakeSound:
    def __init__(self, gain=0):
        self.source = SimpleNamespace(gain=gain, spatialize=False, direct_channels=True,
            radius=0, reference_distance=0, state=cyal.SourceState.PLAYING,
            play=Mock(), pause=Mock())
        self.muted = False
        self.destroy = Mock(side_effect=lambda: setattr(self, "source", None))


class MapSoundIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.cache = Cache()
        self.groups = []
        self.audio = SimpleNamespace(map_sounds=self.cache,
            volume_categories={"ambience": [50], "music": [40], "sound_source": [70]},
            efx=SimpleNamespace(send=Mock()), create_soundgroup=self.group)
        self.game = SimpleNamespace(audio_mngr=self.audio, automate=Mock())
        self.world = SimpleNamespace(game=self.game, get_reverb_at=Mock(return_value=None))

    def group(self, **kwargs):
        group = SimpleNamespace(position=(0, 0, 0), sounds=[])
        def play(path, *args, **options):
            self.assertIs(options["prepared_buffer"], self.cache.ready[path])
            sound = FakeSound(options.get("initial_gain", options.get("volume", 100) / 100))
            group.sounds.append(sound)
            return sound
        group.play = Mock(side_effect=play)
        group.destroy = Mock()
        self.groups.append(group)
        return group

    def ambience(self, kind="ambience"):
        return Ambience(self.world, "a", 0, 10, 0, 10, 0, 10, "forest.ogg", 80, type=kind)

    def source(self):
        return SoundSource(self.world, "s", 0, 10, 0, 10, 0, 10, "area.ogg", 80)

    def test_cold_ambience_returns_without_creating_or_loading_native_sound(self):
        ambient = self.ambience()
        self.assertIsNone(ambient.sound)
        self.assertTrue(ambient.audio_pending)
        self.groups[0].play.assert_not_called()
        ambient.enter()
        self.assertTrue(ambient.playing)
        ambient.poll_audio()
        self.groups[0].play.assert_not_called()

    def test_stationary_listener_starts_ready_ambience_once_with_silent_fade(self):
        ambient = self.ambience()
        ambient.enter()
        self.cache.ready["forest.ogg"] = object()
        world = Map.__new__(Map)
        world.ambience_list, world.music_list, world.pannable_list = [ambient], [], []
        world.entities, world.source_list = {}, []
        world.player = SimpleNamespace(dead=False)
        world.loop()
        self.assertTrue(ambient.playing)
        self.assertEqual(ambient.sound.source.gain, 0)
        self.assertTrue(ambient.sound.muted)
        self.game.automate.assert_called_once()
        args, kwargs = self.game.automate.call_args
        self.assertEqual(args[1:], ("gain", .4, 500))
        kwargs["callback"]()
        self.assertFalse(ambient.sound.muted)
        world.loop()
        self.groups[0].play.assert_called_once()

    def test_leaving_zone_before_decode_prevents_late_play(self):
        ambient = self.ambience()
        ambient.enter()
        ambient.leave()
        self.cache.ready["forest.ogg"] = object()
        ambient.poll_audio()
        self.assertIsNone(ambient.sound)
        ambient.enter()
        ambient.poll_audio()
        self.assertIsNotNone(ambient.sound)

    def test_destroy_pending_and_loaded_ambience_prevents_resurrection(self):
        ambient = self.ambience()
        ambient.enter()
        ambient.destroy()
        self.cache.ready["forest.ogg"] = object()
        ambient.enter()
        ambient.poll_audio()
        self.assertFalse(ambient.playing)
        self.assertIsNone(ambient.sound)
        self.assertFalse(ambient.audio_pending)
        self.groups[0].destroy.assert_called_once()
        ready = self.ambience()
        ready.enter()
        callback = self.game.automate.call_args.kwargs["callback"]
        sound = ready.sound
        ready.destroy()
        callback()  # Old fade cannot dereference a destroyed sound or unmute it.
        self.assertIsNone(ready.sound)
        sound.destroy.assert_called_once()

    def test_map_music_uses_its_existing_category_and_volume(self):
        music = self.ambience("music")
        music.enter()
        self.cache.ready["forest.ogg"] = object()
        music.poll_audio()
        self.assertEqual(self.groups[0].play.call_args.kwargs["cat"], "music")
        self.assertAlmostEqual(self.game.automate.call_args.args[2], .32)

    def test_ambience_destroy_clears_ownership_even_if_sound_stop_fails(self):
        self.cache.ready["forest.ogg"] = object()
        ambient = self.ambience()
        ambient.sound.destroy.side_effect = RuntimeError("invalid native source")
        ambient.destroy()
        self.assertIsNone(ambient.sound)
        self.assertFalse(ambient.playing)
        self.groups[0].destroy.assert_called_once()

    def test_cold_source_retries_without_latching_playing(self):
        source = self.source()
        self.assertIn("area.ogg", self.cache.requests)
        for _ in range(3):
            source.loop(1, 1, 0)
            self.assertFalse(source.playing)
            self.assertEqual(source.current_gain, 0)
        self.groups[0].play.assert_not_called()
        self.cache.ready["area.ogg"] = object()
        source.loop(1, 1, 0)
        self.assertTrue(source.playing)
        self.assertEqual(source.sound.source.gain, .05)
        self.assertEqual(source.sound.source.reference_distance, 10)
        self.audio.efx.send.assert_called_with(source.sound.source, 0, None)
        source.loop(1, 1, 0)
        self.groups[0].play.assert_called_once()

    def test_pending_source_does_not_start_when_listener_has_left_range(self):
        source = self.source()
        source.loop(1, 1, 0)
        self.cache.ready["area.ogg"] = object()
        source.loop(1000, 1000, 0)
        self.assertIsNone(source.sound)
        self.groups[0].play.assert_not_called()

    def test_source_audibility_matches_range_altitude_and_muted_volume(self):
        source = self.source()
        self.assertTrue(source.is_audible_at(1, 1, 0))
        self.assertFalse(source.is_audible_at(35, 1, 0))
        self.assertTrue(source.is_audible_at(1, 1, 17))
        self.assertFalse(source.is_audible_at(1, 1, 18))
        self.audio.volume_categories["sound_source"][0] = 0
        self.assertFalse(source.is_audible_at(1, 1, 0))
        self.audio.volume_categories["sound_source"][0] = 70
        source.volume = 0
        self.assertFalse(source.is_audible_at(1, 1, 0))

    def test_source_preserves_fade_pause_resume_without_reloading(self):
        source = self.source()
        self.cache.ready["area.ogg"] = object()
        source.loop(1, 1, 0)
        source.loop(1000, 1000, 0)
        self.assertFalse(source.playing)
        self.assertEqual(source.sound.source.gain, 0)
        source.sound.source.pause.assert_called_once()
        source.loop(1, 1, 0)
        source.sound.source.play.assert_called_once()
        self.groups[0].play.assert_called_once()

    def test_source_destroy_blocks_late_decode(self):
        source = self.source()
        source.destroy()
        self.cache.ready["area.ogg"] = object()
        source.loop(1, 1, 0)
        self.assertIsNone(source.sound)
        self.assertFalse(source.audio_pending)

    def test_failed_asset_stays_failed_without_explicit_retry(self):
        ambient = self.ambience()
        ambient.enter()
        self.cache.failed.add("forest.ogg")
        ambient.poll_audio()
        self.assertFalse(ambient.audio_pending)
        # The manual refresh action is gone, so nothing revives a failed
        # asset: repeated polls keep it failed instead of claiming audio.
        ambient.poll_audio()
        self.assertFalse(ambient.audio_pending)

    def test_pannable_polls_then_preserves_source_and_position(self):
        obj = Pannable(self.game, 1, 2, 3, "point.ogg", 90)
        self.assertEqual((obj.minx, obj.maxx, obj.miny, obj.maxy, obj.minz, obj.maxz),
                         (1, 1, 2, 2, 3, 3))
        self.assertTrue(obj.audio_pending)
        self.cache.ready["point.ogg"] = object()
        obj.poll_audio()
        self.assertEqual(obj.soundgroup.position, (1, 2, 3))
        self.assertIsNotNone(obj.sound)
        obj.poll_audio()
        obj.soundgroup.play.assert_called_once()
        obj.destroy()
        obj.poll_audio()
        obj.soundgroup.play.assert_called_once()

    def test_map_helper_never_uses_sync_loader_on_a_cache_miss(self):
        group = SimpleNamespace(play=Mock(side_effect=AssertionError("synchronous fallback")))
        self.assertIsNone(_play_map_loop(self.audio, group, "cold.ogg", True))
        group.play.assert_not_called()

    def test_path_resolution_does_not_probe_disk_and_honors_vfs_root(self):
        with patch("libs.audio_manager.consts.SOUNDPREPEND", "V:/mounted/sounds/"), \
             patch("libs.audio_manager.os.path.exists", side_effect=AssertionError("main-thread I/O")), \
             patch("libs.audio_manager.os.path.isdir", side_effect=AssertionError("main-thread I/O")):
            path = AudioManager._resolve_map_sound_path("ambience/forest.ogg")
            self.assertEqual(path, os.path.normcase(os.path.normpath("V:/mounted/sounds/ambience/forest.ogg")))
            self.assertEqual(AudioManager._resolve_map_sound_path(path), path)
            self.assertIsNone(AudioManager._resolve_map_sound_path("server_sounds:../../escape.ogg"))

    def test_decoder_is_bounded_and_relative_paths_support_thai_install(self):
        decoded = SimpleNamespace(buffer=b"abcd", channels=1, frequency=48000)
        with patch("libs.safe_vorbis.load_vorbis_pcm", return_value=decoded) as decoder:
            path = os.path.abspath("data/ambience/forest.ogg")
            self.assertIs(AudioManager._decode_map_sound(path), decoded)
        decoder.assert_called_once_with(os.path.relpath(path), max_pcm_bytes=64 * 1024 * 1024)

    def test_remote_download_publishes_complete_asset_and_cleans_failed_write(self):
        for fail_publish in (False, True):
            with self.subTest(fail_publish=fail_publish), tempfile.TemporaryDirectory() as root:
                path = os.path.join(root, "remote.ogg")
                response = Mock()
                response.iter_content.return_value = [b"OggS", b"payload"]
                request = Mock()
                request.__enter__ = Mock(return_value=response)
                request.__exit__ = Mock(return_value=False)
                replace = os.replace
                with patch("libs.audio_manager.consts.SOUNDPREPEND", root), \
                     patch("libs.audio_manager.requests.get", return_value=request) as get, \
                     patch("libs.safe_vorbis.load_vorbis_pcm", return_value=object()), \
                     patch("libs.audio_manager.os.replace", side_effect=OSError("write failed") if fail_publish else replace):
                    if fail_publish:
                        with self.assertRaises(OSError):
                            AudioManager._decode_map_sound("server_sounds:" + path)
                        self.assertEqual(os.listdir(root), [])
                    else:
                        AudioManager._decode_map_sound("server_sounds:" + path)
                        with open(path, "rb") as asset:
                            self.assertEqual(asset.read(), b"OggSpayload")
                        self.assertEqual(os.listdir(root), ["remote.ogg"])
                    self.assertEqual(get.call_args.kwargs, {"timeout": (5, 10), "stream": True})

    def test_actual_soundgroup_uses_prepared_buffer_without_sync_decode(self):
        buffer = SimpleNamespace(channels=2)
        source = SimpleNamespace(play=Mock(), set=Mock())
        group = SoundGroup.__new__(SoundGroup)
        group.parent = SimpleNamespace(muted=False, load_buffer=Mock(side_effect=AssertionError("sync")),
            volume_categories={"master": [100, weakref.WeakSet()], "ambience": [50, weakref.WeakSet()]})
        group.context = SimpleNamespace(gen_source=Mock(return_value=source))
        group._orientation = group._position = group._velocity = (0, 0, 0)
        group.direct = True
        group.sends = []
        group.filter = []
        group.unlabeled_sources = []
        group.labeled_sources = {}
        group.mute_if_far = Mock()
        sound = group.play("forest.ogg", True, cat="ambience", volume=80,
                           prepared_buffer=buffer, initial_gain=0.0)
        self.assertIs(sound.source, source)
        self.assertIs(source.buffer, buffer)
        self.assertEqual(sound.volume, 80)
        self.assertEqual(group.context.gen_source.call_args.kwargs["gain"], 0)
        source.play.assert_called_once()

    def test_spatial_sound_starts_silent_after_distance_muting(self):
        buffer = SimpleNamespace(channels=1)
        source = SimpleNamespace(gain=0.0, set=Mock())
        gains_at_play = []
        source.play = lambda: gains_at_play.append(source.gain)
        group = SoundGroup.__new__(SoundGroup)
        group.parent = SimpleNamespace(muted=False, position=(0, 0, 0), max_distance=59,
            load_buffer=Mock(side_effect=AssertionError("sync")),
            volume_categories={"master": [100, weakref.WeakSet()], "ambience": [50, weakref.WeakSet()]})
        group.context = SimpleNamespace(gen_source=Mock(return_value=source))
        group._orientation = group._position = group._velocity = (0, 0, 0)
        group._inner_cone_angle = group._outer_cone_angle = 360
        group._cone_outer_gain = .4
        group.direct = group.muted = False
        group.sends, group.filter, group.unlabeled_sources = [], [], []
        group.labeled_sources = {}
        sound = group.play("forest.ogg", True, cat="ambience", volume=80,
                           prepared_buffer=buffer, initial_gain=0.0)
        self.assertEqual(gains_at_play, [0.0])
        self.assertEqual(sound.volume, 80)


if __name__ == "__main__":
    unittest.main()
