"""Offline regressions for the cinema room wired into the jukebox transports.

The load-bearing test is the first one: with cinema off, and with cinema on
but no cabinet named by the server, ``play()`` must build exactly the same
two-source playback it always did. Everything else in this file exercises the
room that only exists once a cabinet is named.
"""

import os
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

import cyal

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from libs import jukebox
from libs.audio.cinema import (CinemaRenderer, CinemaSpeakerBank, host_for,
                               set_enabled)


class FakeSource:
    def __init__(self, context):
        self.context = context
        self.position = None
        self.rolloff_factor = None
        self.reference_distance = None
        self.max_distance = None
        self.spatialize = None
        self.direct_channels = None
        self.direct_filter = None
        self.gain = 0.0
        self.buffers_queued = 0
        self.buffers_processed = 0
        self.state = cyal.SourceState.STOPPED
        self.played = 0
        self.deleted = False

    def play(self):
        self.played += 1
        self.state = cyal.SourceState.PLAYING

    def stop(self):
        self.state = cyal.SourceState.STOPPED

    def delete(self):
        self.deleted = True
        self.context.deleted.append(self)

    def queue_buffers(self, buffer):
        self.buffers_queued += 1

    def unqueue_buffers(self):
        if self.buffers_processed <= 0:
            return None
        self.buffers_processed -= 1
        self.buffers_queued = max(0, self.buffers_queued - 1)
        return []


UPLOADED = []


class FakeBuffer:
    def __init__(self):
        self.data = None

    def set_data(self, data, sample_rate=None, format=None):
        self.data = bytes(data)
        self.sample_rate = sample_rate
        self.format = format
        UPLOADED.append(self.data)


class FakeContext:
    def __init__(self):
        self.created = []
        self.deleted = []

    def gen_source(self, **kwargs):
        source = FakeSource(self)
        self.created.append(source)
        return source

    def gen_buffer(self):
        return FakeBuffer()


class FakeEfx:
    def __init__(self):
        self.sends = []

    def send(self, source, index, slot, filter=None):
        self.sends.append((source, index, slot))


class FakeAudio:
    def __init__(self):
        self.context = FakeContext()
        self.efx = FakeEfx()
        self.filter = []
        self.position = (10.0, 25.0, 0.0)
        self.volume_categories = {"jukebox": [100]}

    def gen_filter(self, kind, *params):
        return ("filter", kind, params)

    def gen_effect(self, kind, *params):
        return ("effect", kind, params)


class FakeGame:
    def __init__(self):
        self.audio_mngr = FakeAudio()
        self.gameplay = SimpleNamespace(map=SimpleNamespace())


def play_song(game, player, **kwargs):
    options = {
        "title": "Song",
        "url": "http://example.com/a.mp3",
        "duration": 60,
        "transport": "direct",
    }
    options.update(kwargs)
    with mock.patch("libs.music_bot.AudioStreamer") as streamer:
        player.play("j1", 10, 20, 0,
                    options.pop("title"), options.pop("url"), options.pop("duration"),
                    **options)
    return streamer


class CinemaDisabledParityTests(unittest.TestCase):
    """The shipped default: nothing about the jukebox changes."""

    def test_unnamed_cabinet_keeps_the_two_source_pair(self):
        game = FakeGame()
        player = jukebox.JukeboxPlayer(game)
        set_enabled(game, True)
        streamer = play_song(game, player)
        kwargs = streamer.call_args.kwargs
        self.assertIsNone(kwargs.get("cinema"))
        self.assertIsNotNone(kwargs.get("spatial_pair"))
        self.assertEqual(len(game.audio_mngr.context.created), 2)
        self.assertIsNone(player.players["j1"].get("cinema"))

    def test_feature_off_never_builds_a_room(self):
        game = FakeGame()
        player = jukebox.JukeboxPlayer(game)
        streamer = play_song(game, player, cinema_profile="theatre")
        kwargs = streamer.call_args.kwargs
        self.assertIsNone(kwargs.get("cinema"))
        self.assertIsNotNone(kwargs.get("spatial_pair"))
        self.assertEqual(len(game.audio_mngr.context.created), 2)

    def test_off_by_default_never_attaches_a_host(self):
        game = FakeGame()
        player = jukebox.JukeboxPlayer(game)
        play_song(game, player, cinema_profile="theatre")
        self.assertIsNone(host_for(game, create=False))


class CinemaRoomTests(unittest.TestCase):
    """A named cabinet becomes a room, and stays one."""

    def make(self, profile="theatre"):
        game = FakeGame()
        set_enabled(game, True)
        player = jukebox.JukeboxPlayer(game)
        streamer = play_song(game, player, cinema_profile=profile)
        return game, player, streamer

    def test_named_cabinet_builds_one_source_per_speaker(self):
        game, player, streamer = self.make()
        kwargs = streamer.call_args.kwargs
        self.assertIsNone(kwargs.get("spatial_pair"))
        bank = kwargs.get("cinema")
        self.assertIsInstance(bank, CinemaSpeakerBank)
        self.assertEqual(len(bank.sources), 7)
        self.assertEqual(len(game.audio_mngr.context.created), 7)

    def test_speakers_are_placed_around_the_cabinet(self):
        game, player, streamer = self.make()
        bank = streamer.call_args.kwargs["cinema"]
        front = bank.slot_sources["front_c"].position
        left = bank.slot_sources["front_l"].position
        right = bank.slot_sources["front_r"].position
        # X is left to right, Y is backward to forward: the screen wall is
        # ahead of the cabinet and the front pair straddles it.
        self.assertGreater(front[1], 20.0)
        self.assertLess(left[0], front[0])
        self.assertGreater(right[0], front[0])
        self.assertEqual(front[0], 10.0)

    def test_front_pair_carries_the_original_channels(self):
        game, player, streamer = self.make(profile="front_only")
        bank = streamer.call_args.kwargs["cinema"]
        # front_only is the plain pair expressed as a plan, so the feeds are
        # the source channels untouched.
        left, right = b"\x01\x02\x03\x04", b"\x05\x06\x07\x08"
        feeds = dict(bank.renderer.render(left, right))
        self.assertIs(feeds["front_l"], left)
        self.assertIs(feeds["front_r"], right)

    def test_room_sources_carry_the_spatial_contract(self):
        game, player, streamer = self.make()
        bank = streamer.call_args.kwargs["cinema"]
        for source in bank.sources:
            self.assertTrue(source.spatialize)
            self.assertFalse(source.direct_channels)
            self.assertEqual(source.rolloff_factor, 0.0)
            self.assertEqual(source.reference_distance, 40.0)

    def test_cabinet_volume_and_eq_reach_every_speaker(self):
        game, player, streamer = self.make()
        bank = streamer.call_args.kwargs["cinema"]
        self.assertEqual(len(bank.slot_sources), 7)
        # The room refreshes itself from the cabinet's own settings.
        player.set_cabinet_volume("j1", 50)
        self.assertAlmostEqual(bank.cabinet_volume, 0.5)
        game.audio_mngr.efx.sends.clear()
        player.set_eq_profile("j1", "bass_boost")
        self.assertIsNotNone(bank.eq_slot)
        eq_sends = [source for source, index, _slot in game.audio_mngr.efx.sends if index == 1]
        self.assertEqual(len(eq_sends), 7)

    def test_entry_owns_every_speaker_source(self):
        game, player, streamer = self.make()
        entry = player.players["j1"]
        self.assertEqual(len(player._entry_sources(entry)), 7)

    def test_stop_releases_the_whole_room(self):
        game, player, streamer = self.make()
        bank = player.players["j1"]["cinema"]
        self.assertTrue(player.stop("j1"))
        self.assertEqual(bank.sources, ())
        self.assertTrue(bank._stopped)
        deleted = [source for source in game.audio_mngr.context.created if source.deleted]
        self.assertEqual(len(deleted), 7)
        self.assertIsNone(host_for(game, create=False).bank("j1"))

    def test_stopping_the_room_clears_its_buffers(self):
        game, player, streamer = self.make()
        bank = player.players["j1"]["cinema"]
        self.assertTrue(bank._pools)
        player.stop("j1")
        self.assertEqual(bank._pools, {})

    def test_moved_cabinet_reuses_the_room_and_its_sources(self):
        game, player, streamer = self.make()
        bank = player.players["j1"]["cinema"]
        first_source = bank.slot_sources["front_c"]
        with mock.patch("libs.music_bot.AudioStreamer"):
            player.play("j1", 30, 20, 0, "Song", "http://example.com/a.mp3", 60,
                        cinema_profile="theatre")
        self.assertIs(player.players["j1"]["cinema"], bank)
        self.assertIs(bank.slot_sources["front_c"], first_source)
        self.assertEqual(len(game.audio_mngr.context.created), 7)


class CinemaRelayTests(unittest.TestCase):
    def test_relay_receiver_is_handed_the_room(self):
        game = FakeGame()
        set_enabled(game, True)
        player = jukebox.JukeboxPlayer(game)
        with mock.patch("libs.jukebox.JukeboxRelayReceiver") as receiver:
            player.play("j1", 10, 20, 0, "Song", "http://example.com/a.mp3", 60,
                        transport="relay", relay_id=4, stream_epoch=2,
                        cinema_profile="surround")
        self.assertIsInstance(receiver.call_args.kwargs.get("cinema"), CinemaSpeakerBank)
        # A surround room owns exactly its five speakers.
        self.assertEqual(len(receiver.call_args.kwargs["cinema"].sources), 5)

    def test_receiver_drops_the_pair_and_pumps_the_room(self):
        from libs.jukebox_relay import JukeboxRelayReceiver
        game = FakeGame()
        renderer = CinemaRenderer((10.0, 20.0, 0.0), "surround")
        bank = CinemaSpeakerBank(game, renderer, occlusion_provider=lambda *a: 0)
        receiver = JukeboxRelayReceiver(game, bank.primary_source,
                                        bank.secondary_source, 100, 4, 2, 8.0, 40.0,
                                        cinema=bank)
        # The room owns the sources in cinema mode; the receiver keeps none.
        self.assertIsNone(receiver.source_l)
        self.assertIsNone(receiver.source_r)
        self.assertEqual(receiver._output_sources(), bank.sources)
        self.assertTrue(receiver._queue_pair(b"\x01\x00" * 4, b"\x02\x00" * 4))
        self.assertIsNotNone(receiver.last_audio_activity)
        self.assertTrue(receiver._queue_pair(b"\x01\x00" * 4, b"\x02\x00" * 4))
        bank.start_playback()
        self.assertTrue(bank.playing())


class CinemaBankTests(unittest.TestCase):
    """The bank itself, driven with fake OpenAL objects."""

    def make(self, profile="theatre", **kwargs):
        game = FakeGame()
        renderer = CinemaRenderer((0.0, 0.0, 0.0), profile)
        options = {"volume": 100, "cabinet_volume": 100,
                   "occlusion_provider": lambda *a: 0}
        options.update(kwargs)
        bank = CinemaSpeakerBank(game, renderer, **options)
        return game, renderer, bank

    def test_queue_frame_uploads_one_buffer_per_speaker(self):
        game, renderer, bank = self.make()
        UPLOADED.clear()
        left = b"\x10\x00" * 8
        right = b"\xf0\x00" * 8
        self.assertTrue(bank.queue_frame(left, right))
        for source in bank.sources:
            self.assertEqual(source.buffers_queued, 1)
        # One upload per speaker, in room order: the screen wall carries the
        # original channels untouched, the centre and the rest are mixed.
        self.assertEqual(len(UPLOADED), 7)
        self.assertEqual(UPLOADED[0], left)
        self.assertEqual(UPLOADED[2], right)

    def test_a_frame_can_never_land_on_half_the_room(self):
        game, renderer, bank = self.make()
        # Starve one speaker's pool: the next frame must be refused outright
        # rather than queued on the six speakers that still have room.
        bank._pools["front_l"].clear()
        self.assertFalse(bank.queue_frame(b"\x01\x00" * 4, b"\x02\x00" * 4))
        for source in bank.sources:
            self.assertEqual(source.buffers_queued, 0)

    def test_gain_ramps_with_distance_like_the_plain_jukebox(self):
        game, renderer, bank = self.make(profile="front_only")
        bank.configure(volume=100, cabinet_volume=100, fade=1.0)
        game.audio_mngr.position = (0.0, 0.0, 0.0)   # inside the reference
        bank.update_output()
        near = bank.slot_sources["front_l"].gain
        self.assertAlmostEqual(near, 1.0)
        game.audio_mngr.position = (0.0, 1000.0, 0.0)  # beyond max distance
        bank.update_output()
        self.assertGreaterEqual(bank.slot_sources["front_l"].gain, 0.0)
        self.assertLess(bank.slot_sources["front_l"].gain, near)

    def test_wall_occlusion_is_evaluated_per_speaker(self):
        seen = []

        def provider(position, listener, max_distance):
            seen.append(tuple(position))
            return 2

        game, renderer, bank = self.make(occlusion_provider=provider)
        bank.update_output()
        self.assertEqual(len(seen), len(bank.sources))
        self.assertEqual(len(set(seen)), len(bank.sources))

    def test_retire_fades_then_reports_zero(self):
        clock = [100.0]
        game, renderer, bank = self.make(clock=lambda: clock[0])
        bank.retire(duration=1.0)
        self.assertEqual(bank.current_fade(), 1.0)
        clock[0] += 0.5
        self.assertAlmostEqual(bank.current_fade(), 0.5)
        clock[0] += 1.0
        self.assertEqual(bank.current_fade(), 0.0)

    def test_reclaim_stamps_audible_progress(self):
        game, renderer, bank = self.make()
        bank.queue_frame(b"\x01\x00" * 4, b"\x02\x00" * 4)
        for source in bank.sources:
            source.buffers_processed = 1
        self.assertIsNone(bank.last_output_at)
        self.assertTrue(bank.reclaim())
        self.assertIsNotNone(bank.last_output_at)

    def test_start_playback_waits_for_the_whole_room_to_buffer(self):
        game, renderer, bank = self.make()
        self.assertEqual(bank.wanted_for_start(), 4)
        bank.start_playback()
        for source in bank.sources:
            self.assertEqual(source.played, 1)

    def test_configure_does_not_refresh_by_itself(self):
        game, renderer, bank = self.make(profile="front_only")
        bank.configure(volume=50, cabinet_volume=50, fade=1.0)
        self.assertEqual(bank.volume, 50)
        self.assertAlmostEqual(bank.cabinet_volume, 0.5)
        self.assertAlmostEqual(bank.current_fade(), 1.0)


if __name__ == "__main__":
    unittest.main()
