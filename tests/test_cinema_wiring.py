"""Offline regressions for the cinema room wired into the jukebox transports.

The load-bearing test is the first one: an unmarked cabinet with no speakers
around it must build exactly the same two-source playback it always did, and
the player's own opt-out must kill the room even when the map asked for one.
Everything else in this file exercises the room that only exists once a
cabinet is named.
"""

import os
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

import cyal

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from libs import jukebox
from libs.audio.cinema import (ROOM_MAX_DISTANCE, ROOM_RADIUS,
                               ROOM_REFERENCE_DISTANCE, CinemaRenderer,
                               CinemaSpeakerBank, host_for, set_enabled)
from libs.world_map import CinemaSpeakerZone, Map


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
    jukebox_id = options.pop("jukebox_id", "j1")
    position = options.pop("position", (10, 20, 0))
    with mock.patch("libs.music_bot.AudioStreamer") as streamer:
        player.play(jukebox_id, position[0], position[1], position[2],
                    options.pop("title"), options.pop("url"), options.pop("duration"),
                    **options)
    return streamer


class CinemaDisabledParityTests(unittest.TestCase):
    """The shipped default: nothing about the jukebox changes."""

    def test_unnamed_cabinet_keeps_the_two_source_pair(self):
        # No speakers on this map and no mode on the cabinet: "auto" has
        # nothing to resolve, so this is the shipped two-source playback.
        game = FakeGame()
        player = jukebox.JukeboxPlayer(game)
        set_enabled(game, True)
        streamer = play_song(game, player)
        kwargs = streamer.call_args.kwargs
        self.assertIsNone(kwargs.get("cinema"))
        self.assertIsNotNone(kwargs.get("spatial_pair"))
        self.assertEqual(len(game.audio_mngr.context.created), 2)
        self.assertIsNone(player.players["j1"].get("cinema"))

    def test_the_player_opt_out_never_builds_a_room(self):
        """A cabinet the map marked as a room, for a player who said no."""
        game = FakeGame()
        set_enabled(game, False)
        player = jukebox.JukeboxPlayer(game)
        streamer = play_song(game, player, cinema_mode="theatre")
        kwargs = streamer.call_args.kwargs
        self.assertIsNone(kwargs.get("cinema"))
        self.assertIsNotNone(kwargs.get("spatial_pair"))
        self.assertEqual(len(game.audio_mngr.context.created), 2)

    def test_the_option_off_never_attaches_a_host(self):
        """The opt-out has to be free: no host means no room code at all."""
        game = FakeGame()
        player = jukebox.JukeboxPlayer(game)
        with mock.patch("libs.audio.cinema.plugin._option_enabled", lambda: False):
            play_song(game, player, cinema_mode="theatre")
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
        # The room's own scale, not the plain pair's 8/40: a room is heard
        # from the back row, which the pair's falloff could not reach. OpenAL's
        # own falloff stays off because the room fades each speaker per frame.
        self.assertEqual(ROOM_REFERENCE_DISTANCE, 8.0)
        self.assertGreater(ROOM_MAX_DISTANCE, 40.0)
        for source in bank.sources:
            self.assertTrue(source.spatialize)
            self.assertFalse(source.direct_channels)
            self.assertEqual(source.rolloff_factor, 0.0)
            self.assertEqual(source.reference_distance, ROOM_MAX_DISTANCE)
            self.assertEqual(source.max_distance, ROOM_MAX_DISTANCE)

    def test_inside_the_room_and_audible_are_one_distance(self):
        """A speaker the resolver keeps must be one the listener can hear.

        These were two separate numbers once (one to belong to the room, one
        to be audible in it), so the room could accept a speaker and then play
        it into silence. They are the same number now, and this pins that.
        """
        self.assertEqual(ROOM_RADIUS, ROOM_MAX_DISTANCE)

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


def place_speakers(map_obj, entries):
    """Give a map cinema speakers, exactly as the parse loop would."""
    from math import cos, radians, sin
    anchor = (10.0, 20.0, 0.0)
    for index, (channel, bearing, *rest) in enumerate(entries):
        level = rest[0] if rest else 100
        angle = radians(bearing)
        x = anchor[0] + sin(angle) * 8.0
        y = anchor[1] + cos(angle) * 8.0
        # A one-block box centred on the intended point, the way a builder
        # places a point element.
        map_obj.spawn_cinemaSpeaker(minx=x - 0.5, maxx=x + 0.5,
                                    miny=y - 0.5, maxy=y + 0.5,
                                    minz=0, maxz=1, id=f"spk{index}",
                                    channel=channel, level=level)
    return map_obj


class CinemaRoomFromMapTests(unittest.TestCase):
    """A cabinet surrounded by placed speakers, with no server help at all."""

    def build(self, entries, **play_kwargs):
        game = FakeGame()
        set_enabled(game, True)
        game.gameplay.map = place_speakers(Map(game), entries)
        player = jukebox.JukeboxPlayer(game)
        streamer = play_song(game, player, **play_kwargs)
        return game, player, streamer

    def test_the_map_alone_builds_the_room_and_its_profile(self):
        game, player, streamer = self.build([("front_l", -30), ("front_r", 30)])
        kwargs = streamer.call_args.kwargs
        bank = kwargs.get("cinema")
        self.assertIsInstance(bank, CinemaSpeakerBank)
        self.assertIsNone(kwargs.get("spatial_pair"))
        self.assertEqual(bank.renderer.profile.name, "front_only")
        self.assertEqual(len(bank.sources), 2)
        # The speakers stand where the builder put them, not at the cabinet's
        # own +/-2.5 pair: the map is the authority on placement.
        self.assertAlmostEqual(bank.slot_sources["front_l"].position[0], 6.0, places=3)
        self.assertAlmostEqual(bank.slot_sources["front_r"].position[0], 14.0, places=3)
        self.assertAlmostEqual(bank.slot_sources["front_l"].position[1], 26.928, places=3)

    def test_a_full_map_room_becomes_a_theatre(self):
        game, player, streamer = self.build([
            ("front_l", -30), ("front_c", 0), ("front_r", 30),
            ("side_l", -90), ("side_r", 90), ("rear_l", -150), ("rear_r", 150)])
        bank = streamer.call_args.kwargs["cinema"]
        self.assertEqual(bank.renderer.profile.name, "theatre")
        self.assertEqual(len(bank.sources), 7)
        host = host_for(game, create=False)
        self.assertEqual(host.warnings("j1"), ())
        self.assertEqual(len(host.placement("j1").slots), 7)

    def test_a_swapped_map_is_repaired_rather_than_mirrored(self):
        game, player, streamer = self.build(
            [("front_r", -30), ("front_l", 30), ("front_c", 0)])
        bank = streamer.call_args.kwargs["cinema"]
        self.assertEqual(bank.renderer.profile.name, "front_stage")
        # The speaker the builder mislabelled still sits on the audience's
        # left, and now it plays the left channel.
        self.assertLess(bank.slot_sources["front_l"].position[0], 10.0)
        self.assertGreater(bank.slot_sources["front_r"].position[0], 10.0)
        host = host_for(game, create=False)
        self.assertTrue(any("trusting the position" in warning
                            for warning in host.warnings("j1")))

    def test_a_room_that_cannot_be_resolved_stays_a_plain_jukebox(self):
        game, player, streamer = self.build([("front_l", -30)])
        kwargs = streamer.call_args.kwargs
        self.assertIsNone(kwargs.get("cinema"))
        self.assertIsNotNone(kwargs.get("spatial_pair"))
        self.assertEqual(len(game.audio_mngr.context.created), 2)

    def test_no_map_speakers_and_no_server_profile_stays_plain(self):
        game = FakeGame()
        set_enabled(game, True)
        player = jukebox.JukeboxPlayer(game)
        streamer = play_song(game, player)
        self.assertIsNone(streamer.call_args.kwargs.get("cinema"))
        self.assertEqual(len(game.audio_mngr.context.created), 2)

    def test_a_server_marked_cabinet_in_an_empty_map_gets_a_ring(self):
        game = FakeGame()
        set_enabled(game, True)
        player = jukebox.JukeboxPlayer(game)
        streamer = play_song(game, player, cinema_profile="theatre")
        bank = streamer.call_args.kwargs["cinema"]
        self.assertEqual(len(bank.sources), 7)
        self.assertFalse(bank.renderer.layout._specs)

    def test_map_speaker_levels_reach_the_feeds(self):
        game, player, streamer = self.build([("front_l", -30, 50), ("front_r", 30)])
        bank = streamer.call_args.kwargs["cinema"]
        feeds = dict(bank.renderer.render(b"\x00\x40" * 4, b"\x00\x40" * 4))
        # A trimmed speaker no longer passes the source channel through.
        self.assertIsNot(feeds["front_l"], b"\x00\x40" * 4)

    def test_two_cabinets_do_not_share_one_room(self):
        """The nearer cabinet wins each speaker; the other stays a jukebox."""
        game = FakeGame()
        set_enabled(game, True)
        map_obj = place_speakers(Map(game),
                                 [("front_l", -30), ("front_r", 30)])
        # A second cabinet across the map, with no speakers of its own.
        map_obj.spawn_jukebox(minx=80, maxx=81, miny=20, maxy=21, minz=0, maxz=1,
                              id="j2")
        game.gameplay.map = map_obj
        player = jukebox.JukeboxPlayer(game)
        first = play_song(game, player)
        self.assertIsInstance(first.call_args.kwargs.get("cinema"), CinemaSpeakerBank)
        second = play_song(game, player, jukebox_id="j2", position=(80, 20, 0))
        self.assertIsNone(second.call_args.kwargs.get("cinema"))
        self.assertIsNotNone(second.call_args.kwargs.get("spatial_pair"))

    def test_the_room_can_say_what_the_listener_is_facing(self):
        """The debug readout the turn-around behaviour is checked with."""
        game, player, streamer = self.build([
            ("front_l", -30), ("front_r", 30), ("rear_l", -150), ("rear_r", 150)])
        host = host_for(game, create=False)
        # FakeAudio has no listener, so the reading is the room without a pose.
        self.assertIn("theatre", host.describe("j1"))
        self.assertIn("front_l", host.describe("j1"))
        self.assertEqual(host.describe("nope"), "no cinema room")

        from libs.audio.cinema import ListenerPose, facing_report
        listener = SimpleNamespace(
            orientation=(0.0, 1.0, 0.0, 0.0, 0.0, 1.0), position=(10.0, 20.0, 0.0))
        audio = SimpleNamespace(listener=listener)
        report = dict((slot, (quadrant, in_front))
                      for slot, quadrant, in_front, _ in
                      facing_report(ListenerPose.from_audio_manager(audio),
                                    host.placement("j1")))
        self.assertTrue(report["front_l"][1])
        self.assertFalse(report["rear_l"][1])
        self.assertIn("ahead", host.describe("j1", audio))

    def test_the_room_is_remembered_per_cabinet(self):
        game, player, streamer = self.build([("front_l", -30), ("front_r", 30)])
        host = host_for(game, create=False)
        placement = host.placement("j1")
        self.assertEqual(set(placement.slots), {"front_l", "front_r"})
        self.assertEqual(len(host.renderers), 1)
        player.stop("j1")
        self.assertIsNone(host.placement("j1"))

    def test_the_server_map_payload_becomes_a_room(self):
        """The real path: ``parse_map`` elements in, a room out, no profile."""
        from libs.map import Map_parser
        game = FakeGame()
        set_enabled(game, True)
        world = Map(game)
        data = {
            "minx": -30, "maxx": 100, "miny": -30, "maxy": 100,
            "minz": 0, "maxz": 30,
            "elements": [
                {"type": "jukebox", "data": {
                    "id": "j1", "minx": 10, "maxx": 11, "miny": 20,
                    "maxy": 21, "minz": 0, "maxz": 1}},
                {"type": "cinemaSpeaker", "data": {
                    "id": "a", "minx": 6.0, "maxx": 7.0, "miny": 26.0,
                    "maxy": 27.0, "minz": 0, "maxz": 1, "channel": "front_l"}},
                {"type": "cinemaSpeaker", "data": {
                    "id": "b", "x": 13.0, "y": 26.0, "z": 0.0,
                    "minx": 13.0, "maxx": 14.0, "miny": 26.0, "maxy": 27.0,
                    "minz": 0, "maxz": 1, "channel": "front_r", "level": 100}},
            ],
        }
        Map_parser(game, world).load(data)
        self.assertEqual(len(world.get_cinema_speakers()), 2)
        game.gameplay.map = world
        player = jukebox.JukeboxPlayer(game)
        streamer = play_song(game, player)
        bank = streamer.call_args.kwargs["cinema"]
        self.assertIsInstance(bank, CinemaSpeakerBank)
        self.assertEqual(bank.renderer.profile.name, "front_only")
        self.assertEqual(len(bank.sources), 2)
        # A payload that also carries an explicit point uses it verbatim.
        self.assertAlmostEqual(bank.slot_sources["front_r"].position[0], 13.0, places=3)

    def test_a_cinema_speaker_map_element_becomes_a_room(self):
        """The parse path itself: element attributes in, a room out."""
        zone = CinemaSpeakerZone("a", 6.0, 7.0, 26.0, 27.0, 0, 1, channel="front_l")
        self.assertEqual(zone.position, (6.5, 26.5, 0.5))
        spec = zone.as_spec()
        self.assertEqual(spec["channel"], "front_l")
        self.assertEqual(spec["room"], "")
        game = FakeGame()
        map_obj = Map(game)
        map_obj.spawn_cinemaSpeaker(minx=6.0, maxx=7.0, miny=26.0, maxy=27.0,
                                    minz=0, maxz=1, id="a", channel="front_l")
        map_obj.spawn_cinemaSpeaker(minx=13.0, maxx=14.0, miny=26.0, maxy=27.0,
                                    minz=0, maxz=1, id="b", channel="front_r")
        self.assertEqual(len(map_obj.get_cinema_speakers()), 2)
        # Re-spawning the same id replaces rather than duplicates the element.
        map_obj.spawn_cinemaSpeaker(minx=6.0, maxx=7.0, miny=26.0, maxy=27.0,
                                    minz=0, maxz=1, id="a", channel="front_l")
        self.assertEqual(len(map_obj.get_cinema_speakers()), 2)


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

    def test_a_frame_shed_for_the_live_edge_is_counted(self):
        """A dropped frame is audio nobody hears: it is never silent about it.

        The queue is kept at the live edge by dropping what arrives while it is
        already deep (a burst after a stall), and a burst of drops is heard as
        the song jumping forward. Counting them is what turns "it speeds up
        for a moment sometimes" into a number a report can carry; the routine
        line is rate-limited so a bad channel cannot fill the log with it.
        """
        from libs.jukebox_relay import JukeboxRelayReceiver
        game = FakeGame()
        renderer = CinemaRenderer((10.0, 20.0, 0.0), "front_only")
        bank = CinemaSpeakerBank(game, renderer, occlusion_provider=lambda *a: 0)
        receiver = JukeboxRelayReceiver(game, bank.primary_source,
                                        bank.secondary_source, 100, 4, 2, 8.0, 40.0,
                                        cinema=bank, clock=lambda: 7.5)
        # Not playing yet: nothing is dropped, however deep the queue is.
        self.assertFalse(receiver._shed_if_deep(40))
        self.assertEqual(receiver.shed_frames, 0)
        receiver._play_started = True
        self.assertFalse(receiver._shed_if_deep(receiver.MAX_QUEUED_BUFFERS - 1))
        self.assertTrue(receiver._shed_if_deep(receiver.MAX_QUEUED_BUFFERS))

        self.assertEqual(receiver.shed_frames, 1)
        self.assertEqual(receiver.last_shed_at, 7.5)


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


class CinemaModeTests(unittest.TestCase):
    """What a cabinet plays through is the map's decision, per cabinet.

    The mode arrives with the play event. ``auto`` is the default every map
    shipped before this feature gets, ``off`` is a cabinet that keeps its own
    stereo even standing in a room, and a profile id builds that room shape
    whether or not anyone placed a speaker.
    """

    def build(self, mode, entries=(('front_l', -30), ('front_r', 30)), **play_kwargs):
        game = FakeGame()
        set_enabled(game, True)
        map_obj = place_speakers(Map(game), entries)
        map_obj.spawn_jukebox(minx=9.5, maxx=10.5, miny=19.5, maxy=20.5,
                              minz=0, maxz=1, id="j1")
        game.gameplay.map = map_obj
        player = jukebox.JukeboxPlayer(game)
        play_kwargs["cinema_mode"] = mode
        streamer = play_song(game, player, **play_kwargs)
        return game, player, streamer

    def test_off_refuses_the_room_the_map_built(self):
        game, player, streamer = self.build("off")
        kwargs = streamer.call_args.kwargs
        self.assertIsNone(kwargs.get("cinema"))
        self.assertIsNotNone(kwargs.get("spatial_pair"))
        self.assertEqual(len(game.audio_mngr.context.created), 2)
        self.assertEqual(player.cinema_mode("j1"), "off")

    def test_auto_uses_the_speakers_around_the_cabinet(self):
        game, player, streamer = self.build("auto")
        bank = streamer.call_args.kwargs.get("cinema")
        self.assertIsInstance(bank, CinemaSpeakerBank)
        self.assertEqual(bank.renderer.profile.name, "front_only")

    def test_no_mode_at_all_is_auto(self):
        """Every map that shipped before this feature sends no mode."""
        game = FakeGame()
        set_enabled(game, True)
        game.gameplay.map = place_speakers(Map(game), [('front_l', -30), ('front_r', 30)])
        player = jukebox.JukeboxPlayer(game)
        streamer = play_song(game, player)
        self.assertIsInstance(streamer.call_args.kwargs.get("cinema"), CinemaSpeakerBank)
        self.assertEqual(player.cinema_mode("j1"), "auto")

    def test_a_mode_nobody_recognises_reads_as_auto(self):
        """A typo must leave a cabinet like every other cabinet, not mute."""
        game, player, streamer = self.build("front_left_typo")
        self.assertIsInstance(streamer.call_args.kwargs.get("cinema"), CinemaSpeakerBank)
        self.assertEqual(player.cinema_mode("j1"), "auto")

    def test_a_profile_mode_builds_a_room_with_no_speakers_at_all(self):
        game, player, streamer = self.build("theatre", entries=())
        bank = streamer.call_args.kwargs.get("cinema")
        self.assertIsInstance(bank, CinemaSpeakerBank)
        self.assertEqual(bank.renderer.profile.name, "theatre")
        self.assertEqual(len(bank.sources), 7)

    def test_each_cabinet_keeps_its_own_mode(self):
        game, player, streamer = self.build("off")
        play_song(game, player, jukebox_id="j2", cinema_mode="theatre",
                  position=(11, 21, 0), url="http://example.com/b.mp3")
        self.assertEqual(player.cinema_mode("j1"), "off")
        self.assertEqual(player.cinema_mode("j2"), "theatre")

    def test_the_legacy_profile_field_still_names_a_room(self):
        """An older server sends ``cinema_profile`` and nothing else."""
        game = FakeGame()
        set_enabled(game, True)
        player = jukebox.JukeboxPlayer(game)
        streamer = play_song(game, player, cinema_profile="theatre")
        self.assertIsInstance(streamer.call_args.kwargs.get("cinema"), CinemaSpeakerBank)
        self.assertEqual(player.cinema_mode("j1"), "theatre")

    def test_a_mode_changed_mid_song_applies_to_the_next_one(self):
        """No listener is cut off mid-verse to apply a mode change."""
        game, player, streamer = self.build("auto")
        self.assertIsNotNone(player.players["j1"].get("cinema"))
        player.set_local_cinema_mode("j1", "off")
        player.refresh_cinema_rooms(now=1e6)
        # The room that is audible keeps playing...
        self.assertIsNotNone(player.players["j1"].get("cinema"))
        # ...and the next song honours the new mode.
        next_stream = play_song(game, player, cinema_mode="off",
                                url="http://example.com/b.mp3")
        self.assertIsNone(next_stream.call_args.kwargs.get("cinema"))
        self.assertIsNotNone(next_stream.call_args.kwargs.get("spatial_pair"))

    def test_a_refresh_on_an_auto_cabinet_keeps_its_room(self):
        game, player, streamer = self.build("auto")
        player.refresh_cinema_rooms(now=1e6)
        self.assertIsNotNone(player.players["j1"].get("cinema"))


class CabinetCinemaMenuTests(unittest.TestCase):
    """The cabinet's own menu: what it plays through, and staff changing it."""

    def state(self, mode="auto", jukebox_id="j1"):
        box = {"id": jukebox_id, "x": 10, "y": 20, "z": 0}
        if mode is not None:
            box["cinema_mode"] = mode
        return SimpleNamespace(
            player=SimpleNamespace(x=10, y=20, z=0),
            jukebox_state={"jukeboxes": {jukebox_id: box}},
        )

    def room_game(self, speakers=(('front_l', -30), ('front_r', 30))):
        game = FakeGame()
        set_enabled(game, True)
        map_obj = place_speakers(Map(game), speakers)
        map_obj.spawn_jukebox(minx=9.5, maxx=10.5, miny=19.5, maxy=20.5,
                              minz=0, maxz=1, id="j1")
        game.gameplay.map = map_obj
        return game

    def test_the_menu_reads_the_mode_from_the_server_state(self):
        self.assertEqual(jukebox._cabinet_cinema_mode(self.state("off"), "j1"), "off")
        self.assertEqual(jukebox._cabinet_cinema_mode(self.state("theatre"), "j1"), "theatre")
        self.assertEqual(jukebox._cabinet_cinema_mode(self.state("auto"), "j1"), "auto")

    def test_a_state_without_a_mode_reads_as_auto(self):
        self.assertEqual(jukebox._cabinet_cinema_mode(self.state(None), "j1"), "auto")
        self.assertEqual(jukebox._cabinet_cinema_mode(SimpleNamespace(), "j1"), "auto")

    def test_an_unknown_mode_in_the_state_reads_as_auto(self):
        self.assertEqual(jukebox._cabinet_cinema_mode(self.state("front_left"), "j1"), "auto")
        self.assertEqual(jukebox._cabinet_cinema_mode(self.state(""), "j1"), "auto")

    def test_the_menu_line_names_the_mode(self):
        self.assertIn("Off", jukebox._cinema_mode_label("off"))
        self.assertIn("Auto", jukebox._cinema_mode_label("auto"))
        self.assertIn("theatre", jukebox._cinema_mode_label("theatre"))

    def test_the_detail_says_what_the_room_is_without_building_one(self):
        game = self.room_game()
        detail = jukebox._cinema_detail(game, self.state("auto"), "j1")
        self.assertIn("room speakers", detail)
        # Answering the question must not acquire a speaker: preview only.
        self.assertEqual(len(game.audio_mngr.context.created), 0)

    def test_the_detail_explains_a_cabinet_with_no_room(self):
        game = self.room_game(speakers=())
        detail = jukebox._cinema_detail(game, self.state("auto"), "j1")
        self.assertIn("own stereo", detail)

    def test_the_detail_says_when_the_map_turned_the_room_off(self):
        game = self.room_game()
        detail = jukebox._cinema_detail(game, self.state("off"), "j1")
        self.assertIn("turned its cinema room off", detail)

    def test_the_detail_says_a_forced_shape_needs_no_speakers(self):
        game = self.room_game(speakers=())
        detail = jukebox._cinema_detail(game, self.state("theatre"), "j1")
        self.assertIn("ring of speakers", detail)

    def test_picking_a_mode_at_the_cabinet_asks_the_server(self):
        game = FakeGame()
        sent = []
        game.network = SimpleNamespace(send=lambda *args: sent.append(args))
        picked = SimpleNamespace(mode=None)
        game.gameplay = SimpleNamespace(
            jukebox_state=self.state("auto").jukebox_state,
            jukebox_player=SimpleNamespace(
                set_local_cinema_mode=lambda jid, mode: setattr(picked, "mode", mode)),
            add_substate=lambda *_: None,
            pop_last_substate=lambda: None,
        )
        opened = []

        class FakeMenu:
            def __init__(self, game_, title, parrent=None):
                self.title = title
                self.items = []
                opened.append(self)

            def add_items(self, items):
                self.items.extend(items)

        with mock.patch("libs.menu.Menu", FakeMenu), \
                mock.patch("libs.menus.set_default_sounds"), \
                mock.patch("libs.jukebox.speak"):
            jukebox._open_cinema_mode_menu(game, game.gameplay, "j1")
            labels = [item[0] for item in opened[0].items]
            self.assertEqual(opened[0].title, "Jukebox Cinema Mode")
            self.assertTrue(any(label.startswith("Auto -") for label in labels))
            self.assertTrue(any(label.startswith("Off -") for label in labels))
            self.assertTrue(any("theatre" in label for label in labels))
            self.assertTrue(any("mono_spread" in label for label in labels))
            self.assertTrue(any("(now)" in label for label in labels))
            off_item = next(item for item in opened[0].items if item[0].startswith("Off -"))
            off_item[1]()

        self.assertEqual(sent[0][1], "jukebox_cinema_mode")
        self.assertEqual(sent[0][2], {"id": "j1", "mode": "off"})
        # Answered immediately, even though the map write comes back later.
        self.assertEqual(picked.mode, "off")


if __name__ == "__main__":
    unittest.main()
