"""Focused simulations for the in-game soft audio recovery path."""

import os
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

import cyal

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


class _Recorder:
    def __init__(self):
        self.calls = []
        self.sound = SimpleNamespace(source=SimpleNamespace(state=cyal.SourceState.PLAYING))
        self.playing = True

    def recover(self, *args):
        self.calls.append(("recover", args))
        return True

    def sync_reverb(self):
        self.calls.append(("sync_reverb", ()))


class _Device:
    def __init__(self, paused=True):
        self.paused = paused
        self.resume_calls = 0

    def resume(self):
        self.resume_calls += 1
        self.paused = False


class _Map:
    def __init__(self, ambience, music, source, pannable, remote):
        self.ambience = ambience
        self.music = music
        self.source_list = [source]
        self.pannable_list = [pannable]
        self.entities = {"remote": remote}

    def get_ambiences_at(self, x, y, z):
        return [self.ambience]

    def get_musics_at(self, x, y, z):
        return [self.music]


class TestGameplayAudioRefresh(unittest.TestCase):
    def make_gameplay(self, connected=True):
        from libs.gameplay import Gameplay

        gameplay = Gameplay.__new__(Gameplay)
        device = _Device()
        listener = SimpleNamespace(gain=0.0)
        audio = SimpleNamespace(
            context=SimpleNamespace(is_connected=connected, device=device),
            listener=listener,
            volume_categories={"master": [65, None]},
            muted=True,
        )
        gameplay.game = SimpleNamespace(audio_mngr=audio)
        gameplay._audio_refresh_in_progress = False
        gameplay._last_audio_refresh_at = -5.0

        focus = _Recorder()
        focus.x, focus.y, focus.z = 4.0, 5.0, 6.0
        remote = _Recorder()
        ambience = _Recorder()
        music = _Recorder()
        source = _Recorder()
        pannable = _Recorder()
        gameplay.player = focus
        gameplay.camera = SimpleNamespace(focus_object=focus)
        gameplay.map = _Map(ambience, music, source, pannable, remote)

        gameplay.music_bot = SimpleNamespace(
            refresh_environment_audio=mock.Mock(return_value=True)
        )
        gameplay.jukebox_player = SimpleNamespace(
            refresh_environment_audio=mock.Mock(return_value=True)
        )
        gameplay.megaphone = SimpleNamespace(refresh_environment_audio=mock.Mock(return_value=True))
        return gameplay, device, listener, (focus, remote, ambience, music, source, pannable)

    def test_refresh_recovers_active_sources_effects_streams_and_master_gain(self):
        gameplay, device, listener, objects = self.make_gameplay()
        focus, remote, ambience, music, source, pannable = objects

        with mock.patch("libs.gameplay.time.monotonic", return_value=100.0), \
                mock.patch("libs.gameplay.speak") as speak:
            self.assertTrue(gameplay.refresh_game_audio())

        self.assertEqual(ambience.calls, [("recover", ())])
        self.assertEqual(music.calls, [("recover", ())])
        self.assertEqual(source.calls, [("recover", (4.0, 5.0, 6.0))])
        self.assertEqual(pannable.calls, [("recover", ())])
        self.assertEqual(focus.calls, [("sync_reverb", ())])
        self.assertEqual(remote.calls, [("sync_reverb", ())])
        gameplay.music_bot.refresh_environment_audio.assert_called_once_with()
        gameplay.jukebox_player.refresh_environment_audio.assert_called_once_with()
        gameplay.megaphone.refresh_environment_audio.assert_called_once_with()
        self.assertEqual(device.resume_calls, 1)
        self.assertFalse(gameplay.game.audio_mngr.muted)
        self.assertEqual(listener.gain, 0.65)
        speak.assert_called_once_with("Audio refresh finished. If still silent, use Restart Client.")

    def test_refresh_has_five_second_cooldown(self):
        gameplay, _device, _listener, _objects = self.make_gameplay()
        with mock.patch("libs.gameplay.time.monotonic", side_effect=[100.0, 102.0]), \
                mock.patch("libs.gameplay.speak") as speak:
            self.assertTrue(gameplay.refresh_game_audio())
            self.assertFalse(gameplay.refresh_game_audio())

        self.assertEqual(
            speak.call_args_list[-1],
            mock.call("Audio refresh is cooling down. Please wait a moment."),
        )
        gameplay.music_bot.refresh_environment_audio.assert_called_once_with()

    def test_disconnected_device_uses_restart_fallback_without_mutating_sources(self):
        gameplay, device, listener, objects = self.make_gameplay(connected=False)
        ambience = objects[2]
        with mock.patch("libs.gameplay.time.monotonic", return_value=100.0), \
                mock.patch("libs.gameplay.speak") as speak:
            self.assertFalse(gameplay.refresh_game_audio())

        self.assertEqual(ambience.calls, [])
        self.assertEqual(device.resume_calls, 0)
        self.assertEqual(listener.gain, 0.0)
        self.assertIn("Restart Client", speak.call_args.args[0])

    def test_half_reloaded_map_cannot_trap_or_crash_the_options_menu(self):
        gameplay, _device, _listener, _objects = self.make_gameplay()
        gameplay.map.get_ambiences_at = mock.Mock(
            side_effect=RuntimeError("map is rebuilding")
        )
        with mock.patch("libs.gameplay.time.monotonic", return_value=100.0), \
                mock.patch("libs.gameplay.speak") as speak:
            self.assertFalse(gameplay.refresh_game_audio())

        self.assertFalse(gameplay._audio_refresh_in_progress)
        self.assertIn("incomplete: ambience", speak.call_args.args[0])
        gameplay.music_bot.refresh_environment_audio.assert_called_once_with()
        gameplay.megaphone.refresh_environment_audio.assert_called_once_with()

    def test_failed_owner_does_not_skip_other_recovery_or_claim_success(self):
        for failure in (False, RuntimeError("lost source")):
            with self.subTest(failure=failure):
                gp, _, listener, _ = self.make_gameplay()
                if isinstance(failure, Exception):
                    gp.music_bot.refresh_environment_audio.side_effect = failure
                else:
                    gp.music_bot.refresh_environment_audio.return_value = failure
                with mock.patch("libs.gameplay.speak") as speak:
                    self.assertFalse(gp.refresh_game_audio())
                self.assertIn("incomplete: Music Bot", speak.call_args.args[0])
                gp.megaphone.refresh_environment_audio.assert_called_once_with()
                self.assertEqual(listener.gain, 0.65)
                self.assertFalse(gp._audio_refresh_in_progress)

    def test_failed_device_resume_is_reported_and_does_not_clear_mute(self):
        gp, device, _, _ = self.make_gameplay()
        device.resume = mock.Mock(side_effect=RuntimeError("device failed"))
        with mock.patch("libs.gameplay.speak") as speak:
            self.assertFalse(gp.refresh_game_audio())
        self.assertTrue(gp.game.audio_mngr.muted)
        self.assertIn("audio device", speak.call_args.args[0])

    def test_jukebox_request_is_pending_not_a_playback_confirmation(self):
        gp, _, _, _ = self.make_gameplay()
        gp.jukebox_player.refresh_environment_audio.return_value = None
        with mock.patch("libs.gameplay.speak") as speak:
            self.assertTrue(gp.refresh_game_audio())
        speak.assert_called_once_with("Audio refresh requested. Waiting for Jukebox.")

    def test_unchanged_healthy_loop_is_not_reported_as_failure(self):
        gp, _, _, objects = self.make_gameplay()
        objects[2].recover = mock.Mock(return_value=False)
        with mock.patch("libs.gameplay.speak"):
            self.assertTrue(gp.refresh_game_audio())

    def test_missing_loop_after_recovery_is_reported(self):
        gp, _, _, objects = self.make_gameplay()
        objects[2].sound = None
        with mock.patch("libs.gameplay.speak") as speak:
            self.assertFalse(gp.refresh_game_audio())
        self.assertIn("ambience", speak.call_args.args[0])

    def test_silent_out_of_range_source_is_not_forced_audible(self):
        gp, _, _, objects = self.make_gameplay()
        objects[4].playing = False
        objects[4].current_gain = 0.0
        objects[4].sound = None
        with mock.patch("libs.gameplay.speak"):
            self.assertTrue(gp.refresh_game_audio())

    def test_nearby_pending_map_source_reports_waiting_not_success(self):
        gp, _, _, objects = self.make_gameplay()
        obj = objects[4]
        obj.sound, obj.playing, obj.current_gain = None, False, 0.0
        obj.is_audible_at = mock.Mock(return_value=True)
        obj.audio_pending = True
        with mock.patch("libs.gameplay.speak") as speak:
            self.assertTrue(gp.refresh_game_audio())
        speak.assert_called_once_with("Audio refresh requested. Waiting for map sounds.")

    def test_nearby_failed_map_source_is_not_mistaken_for_out_of_range(self):
        gp, _, _, objects = self.make_gameplay()
        obj = objects[4]
        obj.sound, obj.playing, obj.current_gain = None, False, 0.0
        obj.is_audible_at = mock.Mock(return_value=True)
        obj.audio_pending = False
        with mock.patch("libs.gameplay.speak") as speak:
            self.assertFalse(gp.refresh_game_audio())
        self.assertIn("incomplete: map sounds", speak.call_args.args[0])

    def test_far_pending_map_source_can_remain_silent(self):
        gp, _, _, objects = self.make_gameplay()
        obj = objects[4]
        obj.sound, obj.playing, obj.current_gain = None, False, 0.0
        obj.is_audible_at = mock.Mock(return_value=False)
        obj.audio_pending = True
        with mock.patch("libs.gameplay.speak") as speak:
            self.assertTrue(gp.refresh_game_audio())
        self.assertIn("Audio refresh finished", speak.call_args.args[0])

    def test_reentrant_call_is_rejected_without_touching_device(self):
        gp, device, _, _ = self.make_gameplay()
        gp._audio_refresh_in_progress = True
        with mock.patch("libs.gameplay.speak"):
            self.assertFalse(gp.refresh_game_audio())
        self.assertEqual(device.resume_calls, 0)


class _Source:
    def __init__(self, state):
        self.state = state
        self.gain = 0.0
        self.play_calls = 0
        self.spatialize = False
        self.direct_channels = True

    def play(self):
        self.play_calls += 1
        self.state = cyal.SourceState.PLAYING

    def pause(self):
        self.state = cyal.SourceState.PAUSED


class _Sound:
    def __init__(self, source):
        self.source = source
        self.muted = True
        self.destroy_calls = 0

    def destroy(self):
        self.destroy_calls += 1
        self.source = None


class TestMapLoopRecovery(unittest.TestCase):
    def test_ambience_resumes_existing_source_and_restores_category_gain(self):
        from libs import world_map

        ambience = world_map.Ambience.__new__(world_map.Ambience)
        source = _Source(cyal.SourceState.STOPPED)
        ambience.sound = _Sound(source)
        ambience.soundgroup = SimpleNamespace()
        ambience.file = "ambience/room.ogg"
        ambience.type = "ambience"
        ambience.volume = 80
        ambience.playing = True
        ambience.map = SimpleNamespace(
            game=SimpleNamespace(
                audio_mngr=SimpleNamespace(
                    volume_categories={"ambience": [50, None]}
                )
            )
        )

        self.assertTrue(ambience.recover())
        self.assertEqual(source.play_calls, 1)
        self.assertAlmostEqual(source.gain, 0.4)
        self.assertFalse(ambience.sound.muted)
        self.assertTrue(ambience.playing)

    def test_sound_source_reuses_paused_source_and_rebinds_room_effect(self):
        from libs import world_map

        source_obj = world_map.SoundSource.__new__(world_map.SoundSource)
        source = _Source(cyal.SourceState.PAUSED)
        source_obj.sound = _Sound(source)
        source_obj.playing = True
        source_obj.current_gain = 0.2
        source_obj.volume = 100
        source_obj.fade_range = 25.0
        source_obj.path = "source/fan.ogg"
        source_obj.minx = source_obj.maxx = 1.0
        source_obj.miny = source_obj.maxy = 2.0
        source_obj.minz = source_obj.maxz = 3.0
        source_obj.soundgroup = SimpleNamespace(position=None)
        efx = SimpleNamespace(send=mock.Mock())
        source_obj.map = SimpleNamespace(
            game=SimpleNamespace(
                audio_mngr=SimpleNamespace(
                    volume_categories={"sound_source": [100, None]}, efx=efx
                )
            ),
            get_reverb_at=lambda *_args: None,
        )

        self.assertTrue(source_obj.recover(1.0, 2.0, 3.0))
        self.assertEqual(source.play_calls, 1)
        self.assertTrue(source_obj.playing)
        efx.send.assert_called_once_with(source, 0, None)


class TestMusicBotEnvironmentRefresh(unittest.TestCase):
    def test_force_rebinds_same_reverb_slot_after_driver_loses_send(self):
        from libs.music_bot import MapMusicBot

        bot = MapMusicBot.__new__(MapMusicBot)
        stream_source = object()
        reverb_slot = object()
        bot.stream_source = stream_source
        bot._current_reverb_slot = reverb_slot
        efx = SimpleNamespace(send=mock.Mock())
        bot.game = SimpleNamespace(audio_mngr=SimpleNamespace(efx=efx))
        map_obj = SimpleNamespace(
            get_reverb_at=lambda *_args: SimpleNamespace(reverb=reverb_slot)
        )
        bot._find_gameplay = lambda: SimpleNamespace(
            map=map_obj,
            player=SimpleNamespace(x=1.0, y=2.0, z=3.0),
        )

        bot._sync_map_reverb(force=True)

        efx.send.assert_called_once_with(stream_source, 0, reverb_slot)


if __name__ == "__main__":
    unittest.main()
