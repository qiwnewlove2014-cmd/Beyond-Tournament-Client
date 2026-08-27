"""Offline startup/spatial regressions; no devices, subprocesses or network."""

from collections import deque
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import threading

from libs.jukebox import JukeboxPlayer


class JukeboxSpatialWorkTests(unittest.TestCase):
    def make(self, tier=1):
        world = SimpleNamespace(tile_list=[], occlusion_tier=Mock(return_value=tier), valid_straight_path=Mock())
        game = SimpleNamespace(gameplay=SimpleNamespace(map=world))
        return JukeboxPlayer(game), world

    def test_distant_cabinets_never_scan_map(self):
        player, world = self.make()
        for distance in (42.5, 100, 1000, 1000000):
            self.assertEqual(player.occlusion_tier((0, 0, 0), (distance, 0, 0)), 0)
        world.occlusion_tier.assert_not_called()

    def test_stereo_edge_is_still_audible_and_checked(self):
        player, world = self.make()
        self.assertEqual(player.occlusion_tier((0, 0, 0), (42.4, 0, 0)), 1)
        world.occlusion_tier.assert_called_once()

    def test_same_tile_ray_reused_but_movement_and_expiry_refresh(self):
        player, world = self.make()
        with patch("libs.jukebox.time.monotonic", return_value=10):
            for _ in range(100):
                self.assertEqual(player.occlusion_tier((0, 0, 0), (10.1, 0, 0)), 1)
            player.occlusion_tier((0, 0, 0), (10.9, 0, 0))
            self.assertEqual(world.occlusion_tier.call_count, 1)
            player.occlusion_tier((0, 0, 0), (11, 0, 0))
            self.assertEqual(world.occlusion_tier.call_count, 2)
        with patch("libs.jukebox.time.monotonic", return_value=10.16):
            player.occlusion_tier((0, 0, 0), (10, 0, 0))
        self.assertEqual(world.occlusion_tier.call_count, 3)

    def test_new_map_and_geometry_list_invalidate_cache(self):
        player, world = self.make()
        with patch("libs.jukebox.time.monotonic", return_value=10):
            player.occlusion_tier((0, 0, 0), (10, 0, 0))
            world.tile_list.append(object())
            player.occlusion_tier((0, 0, 0), (10, 0, 0))
            self.assertEqual(world.occlusion_tier.call_count, 2)
            other = SimpleNamespace(tile_list=[], occlusion_tier=Mock(return_value=2))
            player.game.gameplay.map = other
            self.assertEqual(player.occlusion_tier((0, 0, 0), (10, 0, 0)), 2)

    def test_cache_is_bounded_and_preserves_exact_wall_tiers(self):
        from libs.world_map import Map
        from test_wall_occlusion_ratio import _Tile
        player, _ = self.make()
        world = Map.__new__(Map)
        player.game.gameplay.map = world
        for thickness in (0, 1, 2, 3):
            world.tile_list = [] if not thickness else [_Tile(3, 2 + thickness, 0, 0, 0, 0, "wallwood")]
            self.assertEqual(player.occlusion_tier((0, 0, 0), (10, 0, 0)),
                             world.occlusion_tier((0, 0, 0), (10, 0, 0)))
        for position in range(100):
            player.occlusion_tier((position, 0, 0), (position + 10, 0, 0))
        self.assertLessEqual(len(player._occlusion_cache), 64)

    def test_direct_fallback_skips_distant_scan_and_uses_near_cache(self):
        from libs.music_bot import AudioStreamer
        player, world = self.make()
        player.game.audio_mngr = SimpleNamespace(position=(1000, 0, 0), volume_categories={"jukebox": [100]})
        stream = AudioStreamer.__new__(AudioStreamer)
        stream.game = player.game
        stream.spatial_src_l = SimpleNamespace(position=(-2.5, 0, 0))
        stream.spatial_src_r = SimpleNamespace(position=(2.5, 0, 0))
        stream.spatial_ref, stream.spatial_max, stream.spatial_base_gain = 8, 40, 1
        stream.jukebox_player = player
        stream._update_spatial_gain()
        world.occlusion_tier.assert_not_called()
        self.assertEqual(stream.spatial_src_l.gain, 0)
        player.game.audio_mngr.position = (10, 0, 0)
        with patch("libs.jukebox.time.monotonic", return_value=10):
            for _ in range(20):
                stream._update_spatial_gain()
        world.occlusion_tier.assert_called_once()
        self.assertGreater(stream.spatial_src_l.gain, 0)

    def test_direct_listening_does_not_create_broadcast_encoder(self):
        from libs.music_bot import AudioStreamer
        with patch("pyogg.OpusEncoder", side_effect=AssertionError("unused broadcast encoder")):
            stream = AudioStreamer(SimpleNamespace(), "https://example.invalid/song", object(), bot=None)
        self.assertIsNone(stream.encoder)


class JukeboxAudioSchedulerTests(unittest.TestCase):
    def test_shared_budget_rotates_cabinets_and_drops_stopped_receivers(self):
        from libs.audio_manager import AudioManager
        audio = AudioManager.__new__(AudioManager)
        audio._jukebox_receivers = deque()
        now = [10.0]
        calls = []
        class Receiver:
            running = True
            def __init__(self, name): self.name = name
            def pump_audio(self, **kwargs):
                calls.append(self.name)
                now[0] += .003
        receivers = [Receiver(str(index)) for index in range(3)]
        for receiver in receivers:
            audio.register_jukebox_receiver(receiver)
        with patch("libs.audio_manager.time.monotonic", side_effect=lambda: now[0]):
            for _ in range(3): audio._pump_jukebox_receivers()
        self.assertEqual(calls, ["0", "1", "2"])
        receivers[0].running = False
        with patch("libs.audio_manager.time.monotonic", side_effect=lambda: now[0]):
            audio._pump_jukebox_receivers()
        self.assertEqual(calls[-1], "1")
        self.assertEqual(len(audio._jukebox_receivers), 2)


class JukeboxRelayLifecycleIntegrationTests(unittest.TestCase):
    def setUp(self):
        from libs.audio_manager import AudioManager
        from test_jukebox_relay_audio import Source, Buffer
        owner = threading.get_ident()
        class ManagedSource(Source):
            deleted = False
            @Source.position.setter
            def position(self, value):
                self.check()
                self._position = value
            def delete(self):
                self.check()
                self.deleted = True
        self.sources, self.buffers = [], []
        def source():
            value = ManagedSource(owner, (0, 0, 0))
            self.sources.append(value)
            return value
        def buffer():
            value = Buffer(owner, [])
            self.buffers.append(value)
            return value
        self.audio = AudioManager.__new__(AudioManager)
        self.audio._jukebox_receivers = deque()
        self.audio.context = SimpleNamespace(gen_source=source, gen_buffer=buffer)
        self.audio.listener = SimpleNamespace(position=(0, 0, 0))
        self.audio.volume_categories = {"jukebox": [100]}
        self.audio.efx = SimpleNamespace(send=Mock())
        self.player = JukeboxPlayer(SimpleNamespace(audio_mngr=self.audio))
        self.start_patch = patch("libs.jukebox.JukeboxRelayReceiver.start")
        self.start_mock = self.start_patch.start()
        self.log_patch = patch("libs.jukebox.log_line")
        self.log_patch.start()
        self.addCleanup(self.start_patch.stop)
        self.addCleanup(self.log_patch.stop)
        self.addCleanup(self.player.stop_all)

    def play(self, relay=1):
        self.player.play("box", 1, 2, 3, "Song", "https://example.invalid/song", 60,
                         transport="relay", playback_id=1, relay_id=relay, stream_epoch=1)
        return self.player.players["box"]["streamer"]

    def test_play_allocates_no_buffers_until_owner_pump(self):
        receiver = self.play()
        self.assertEqual(len(self.sources), 2)
        self.assertEqual(len(self.buffers), 0)
        self.audio._pump_jukebox_receivers(budget_seconds=1)
        self.assertEqual(len(self.buffers), 4)
        self.assertTrue(receiver.running)

    def test_same_identity_preserves_receiver_and_updates_position(self):
        receiver = self.play()
        self.player.play("box", 10, 20, 30, "Song", "https://example.invalid/song", 60,
                         transport="relay", playback_id=1, relay_id=1, stream_epoch=1)
        self.assertIs(self.player.players["box"]["streamer"], receiver)
        self.assertEqual(receiver.box_pos, (10, 20, 30))
        self.assertEqual(len(self.sources), 2)

    def test_new_identity_retires_old_route_then_stop_all_cleans_both(self):
        old = self.play(1)
        new = self.play(2)
        self.assertEqual(list(self.player.relay_routes), [(2, 1)])
        self.assertIn(old, self.player._retired_relays)
        self.assertFalse(any(source.deleted for source in self.sources))
        self.player.stop_all()
        self.assertFalse(old.running)
        self.assertFalse(new.running)
        self.assertEqual(self.player._retired_relays, [])
        self.assertTrue(all(source.deleted for source in self.sources))

    def test_partial_source_creation_is_cleaned(self):
        original = self.audio.context.gen_source
        self.audio.context.gen_source = Mock(side_effect=[original(), RuntimeError("second source failed")])
        self.player.play("box", 1, 2, 3, "Song", "https://example.invalid", 60)
        self.assertTrue(self.sources[0].deleted)
        self.assertEqual(self.player.players, {})

    def test_map_reload_detaches_fading_and_active_slots_before_reuse(self):
        old = self.play(1)
        new = self.play(2)
        old.reverb_slot = new.reverb_slot = object()
        self.audio.efx.send.reset_mock()
        self.player.detach_reverb()
        self.assertIsNone(old.reverb_slot)
        self.assertIsNone(new.reverb_slot)
        for source in self.sources:
            self.audio.efx.send.assert_any_call(source, 0, None)
        # A later spatial tier change during the fade cannot reattach the old
        # slot. Active playback alone picks up the replacement map's slot.
        replacement_slot = object()
        self.player.game.gameplay = SimpleNamespace(map=SimpleNamespace(
            get_reverb_at=lambda *args: SimpleNamespace(reverb=replacement_slot)))
        self.assertTrue(self.player.sync_reverb())
        self.assertIs(new.reverb_slot, replacement_slot)
        self.assertIsNone(old.reverb_slot)

    def test_start_failure_cancels_registered_receiver_before_source_deletion(self):
        self.start_mock.side_effect = RuntimeError("thread start failed")
        with patch("libs.logger.log_exception"):
            self.player.play("box", 1, 2, 3, "Song", "https://example.invalid", 60,
                             transport="relay", relay_id=1, stream_epoch=1)
        self.audio._pump_jukebox_receivers(budget_seconds=1)
        self.assertEqual(self.player.players, {})
        self.assertEqual(self.buffers, [])
        self.assertTrue(all(source.deleted for source in self.sources))


if __name__ == "__main__":
    unittest.main()
