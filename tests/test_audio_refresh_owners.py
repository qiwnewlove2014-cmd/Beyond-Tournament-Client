"""Owner-level recovery checks: no real devices, workers, files or network."""

import threading
import unittest
from types import SimpleNamespace
from unittest import mock

import cyal
from libs import consts
from libs.jukebox import JukeboxPlayer
from libs.music_bot import MapMusicBot
from libs.systems.megaphone_system import MegaphoneManager


class _Source:
    def __init__(self, broken=False):
        self.broken = broken
        self.position = (1.0, 2.0, 3.0)
        self.play = mock.Mock()
        self.stop = mock.Mock()
        self.delete = mock.Mock()
        self.buffers_processed = 0
        self.buffers_queued = 3
        self.buffer = None
        self.direct_filter = None

    @property
    def state(self):
        if self.broken:
            raise RuntimeError("source lost")
        return cyal.SourceState.PLAYING


class MegaphoneRefreshTests(unittest.TestCase):
    def make_manager(self):
        manager = MegaphoneManager.__new__(MegaphoneManager)
        manager.map = SimpleNamespace(megaphone_speakers=[object()])
        manager.game = SimpleNamespace(audio_mngr=SimpleNamespace(
            efx=SimpleNamespace(send=mock.Mock()), release_filter=mock.Mock(),
        ))
        manager.eq_slot = object()
        manager.compressor_slot = None
        manager.current_player_reverb_slot = object()
        manager.speaker_data = [{'source': _Source(), 'reverb_slot': object()}]
        manager.sources = [manager.speaker_data[0]['source']]
        worker = SimpleNamespace(is_alive=mock.Mock(return_value=True), close=mock.Mock(), join=mock.Mock())
        manager.voice_channels = {consts.CHANNEL_MEGAPHONE: SimpleNamespace(vc_compression=worker)}
        manager.player_sources = {'sender': {'sources': [_Source(), None], 'filters': [object(), None]}}
        manager.lock_owner = 'broadcaster'
        manager.lock_owners = {'broadcaster', 'pianist'}
        manager.setup_megaphone_speakers = mock.Mock()
        manager._spatial_refresh_requested = False
        return manager

    def test_healthy_sources_keep_playback_and_locks_and_rebind_effects(self):
        manager = self.make_manager()
        entry = manager.player_sources['sender']
        source = entry['sources'][0]
        self.assertTrue(manager.refresh_environment_audio())
        manager.setup_megaphone_speakers.assert_not_called()
        source.play.assert_not_called()
        source.stop.assert_not_called()
        self.assertIs(manager.player_sources['sender'], entry)
        self.assertEqual(manager.game.audio_mngr.efx.send.call_count, 4)
        manager.game.audio_mngr.efx.send.assert_any_call(
            source, 3, manager.current_player_reverb_slot, filter=entry['filters'][0])
        self.assertTrue(manager._spatial_refresh_requested)
        self.assertEqual(manager.lock_owner, 'broadcaster')
        self.assertEqual(manager.lock_owners, {'broadcaster', 'pianist'})

    def test_only_lost_clone_is_replaced_and_filters_detach_before_reuse(self):
        manager = self.make_manager()
        lost = _Source(broken=True)
        entry = manager.player_sources['sender']
        entry['sources'][0] = lost
        old_filter = entry['filters'][0]
        healthy = {'sources': [_Source(), None], 'filters': [None, None]}
        manager.player_sources['other'] = healthy
        fresh = {'sources': [_Source(), None], 'filters': [None, None]}
        def replace(sender):
            self.assertNotIn(sender, manager.player_sources)
            lost.stop.assert_called_once()
            self.assertFalse(hasattr(lost, 'direct_filter'))
            manager.game.audio_mngr.release_filter.assert_called_once_with(old_filter)
            manager.player_sources[sender] = fresh
            return fresh['sources']
        manager.get_megaphone_player_sources = mock.Mock(side_effect=replace)
        self.assertTrue(manager.refresh_environment_audio())
        manager.setup_megaphone_speakers.assert_not_called()
        manager.get_megaphone_player_sources.assert_called_once_with('sender')
        self.assertIs(manager.player_sources['other'], healthy)
        healthy['sources'][0].stop.assert_not_called()

    def test_missing_templates_use_existing_setup_and_verify_result(self):
        manager = self.make_manager()
        template = manager.speaker_data
        manager.speaker_data = []
        manager.voice_channels = {}
        def rebuild(**kwargs):
            self.assertEqual(kwargs, {'force': True})
            manager.speaker_data = template
            manager.voice_channels[consts.CHANNEL_MEGAPHONE] = SimpleNamespace(
                vc_compression=SimpleNamespace(is_alive=lambda: True))
        manager.setup_megaphone_speakers.side_effect = rebuild
        self.assertTrue(manager.refresh_environment_audio())
        manager.setup_megaphone_speakers.assert_called_once_with(force=True)

    def test_failed_setup_is_not_reported_as_ready(self):
        manager = self.make_manager()
        manager.speaker_data = []
        manager.voice_channels = {}
        self.assertFalse(manager.refresh_environment_audio())

    def test_busy_old_worker_prevents_unsafe_teardown(self):
        manager = self.make_manager()
        worker = manager.voice_channels[consts.CHANNEL_MEGAPHONE].vc_compression
        manager.speaker_data = []
        self.assertFalse(manager.refresh_environment_audio())
        worker.close.assert_called_once()
        worker.join.assert_called_once_with(timeout=0.05)
        manager.setup_megaphone_speakers.assert_not_called()

    def test_effect_failure_is_reported(self):
        manager = self.make_manager()
        manager.game.audio_mngr.efx.send.side_effect = RuntimeError('lost effect')
        self.assertFalse(manager.refresh_environment_audio())

    def test_map_without_speakers_is_a_successful_noop(self):
        manager = self.make_manager()
        manager.map.megaphone_speakers = []
        self.assertTrue(manager.refresh_environment_audio())
        manager.setup_megaphone_speakers.assert_not_called()


class MusicBotRefreshTests(unittest.TestCase):
    def make_bot(self):
        bot = MapMusicBot.__new__(MapMusicBot)
        bot.enabled = bot.playing = True
        bot.paused = False
        bot.is_loading_stream = False
        bot.current_local_sound = None
        bot._sync_map_reverb = mock.Mock(return_value=True)
        bot.streamer = SimpleNamespace(
            running=True, paused=False, resume_output_if_buffered=mock.Mock(),
            _all_playing=mock.Mock(return_value=True), _buffers_queued=mock.Mock(return_value=3),
            _all_sources=lambda: (_Source(),),
        )
        return bot

    def test_healthy_stream_is_retained(self):
        bot = self.make_bot()
        streamer = bot.streamer
        self.assertTrue(bot.refresh_environment_audio())
        self.assertIs(bot.streamer, streamer)

    def test_play_failure_is_not_hidden_by_buffered_resume_return_value(self):
        bot = self.make_bot()
        bot.streamer.resume_output_if_buffered.return_value = True
        bot.streamer._all_playing.return_value = False
        self.assertFalse(bot.refresh_environment_audio())

    def test_empty_live_stream_is_pending(self):
        bot = self.make_bot()
        bot.streamer._all_playing.return_value = False
        bot.streamer._buffers_queued.return_value = 0
        self.assertIsNone(bot.refresh_environment_audio())

    def test_dead_stream_fails(self):
        bot = self.make_bot()
        bot.streamer.running = False
        self.assertFalse(bot.refresh_environment_audio())

    def test_invalid_source_is_failure_not_infinite_buffering(self):
        bot = self.make_bot()
        bot.streamer._all_sources = lambda: (_Source(broken=True),)
        bot.streamer._all_playing.return_value = False
        bot.streamer._buffers_queued.return_value = 0
        self.assertFalse(bot.refresh_environment_audio())

    def test_deliberate_pause_is_preserved(self):
        bot = self.make_bot()
        bot.paused = True
        self.assertTrue(bot.refresh_environment_audio())
        bot.streamer.resume_output_if_buffered.assert_not_called()

    def test_effect_failure_is_reported(self):
        bot = self.make_bot()
        bot._sync_map_reverb.return_value = False
        self.assertFalse(bot.refresh_environment_audio())


class JukeboxRefreshTests(unittest.TestCase):
    def make_player(self):
        player = JukeboxPlayer.__new__(JukeboxPlayer)
        player._lock = threading.Lock()
        player._last_recovery_request_at = -100.0
        player.players = {'box': {'source': _Source(), 'secondary_source': _Source()}}
        player.game = SimpleNamespace(
            network=SimpleNamespace(send=mock.Mock()),
            audio_mngr=SimpleNamespace(efx=SimpleNamespace(send=mock.Mock())),
            gameplay=SimpleNamespace(map=SimpleNamespace(get_reverb_at=lambda *_: None)),
        )
        return player

    def test_resync_is_pending_and_preserves_routes(self):
        player = self.make_player()
        original = player.players['box']
        with mock.patch('libs.jukebox.time.monotonic', return_value=100.0), mock.patch('libs.jukebox.log_line'):
            self.assertIsNone(player.refresh_environment_audio())
            self.assertIsNone(player.refresh_environment_audio())
        player.game.network.send.assert_called_once_with(consts.CHANNEL_MISC, 'jukebox_resync')
        self.assertIs(player.players['box'], original)

    def test_network_failure_is_reported(self):
        player = self.make_player()
        player.game.network.send.side_effect = RuntimeError('offline')
        self.assertFalse(player.refresh_environment_audio())

    def test_reverb_failure_does_not_prevent_resync_attempt(self):
        player = self.make_player()
        player.game.audio_mngr.efx.send.side_effect = RuntimeError('lost slot')
        with mock.patch('libs.jukebox.log_line'):
            self.assertFalse(player.refresh_environment_audio())
        player.game.network.send.assert_called_once()

    def test_idle_map_does_not_send_a_request(self):
        player = self.make_player()
        player.players.clear()
        self.assertTrue(player.refresh_environment_audio())
        player.game.network.send.assert_not_called()


if __name__ == '__main__':
    unittest.main()
