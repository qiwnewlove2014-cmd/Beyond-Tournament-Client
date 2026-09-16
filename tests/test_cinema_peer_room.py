"""A song another player sends into a cabinet's room.

The Music Bot's frames have always reached listeners at a 3D source on the
sender's back (the receiving entity's own ``music_source``, 5 m loud and 50 m
silent). When the sender routed their bot into a cabinet's room, only their own
client knew: the room was heard by whoever was testing it and by nobody else.

These cover the half that was missing -- the sender announcing which cabinet it
is feeding, and every listener playing those same frames out of that room's
speakers -- and, just as importantly, that a song nobody routed, or one played
for a listener who turned rooms off, still takes the shipped path untouched.
"""

import contextlib
import os
import queue
import sys
import time
import unittest
from types import SimpleNamespace
from unittest import mock

import cyal

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from libs import consts
from libs.audio.cinema import peer
from libs.audio.cinema.plugin import OPTION_ENABLED
from libs.event_handeler import EventHandeler
from libs.music_bot import AudioStreamer
from libs.voice_chat import MusicCompression
from libs.world_map import Map

ANCHOR = (10.0, 20.0, 0.0)
FRAME = b"\x01\x00" * 960


class FakeBuffer:
    def __init__(self):
        self.data = None

    def set_data(self, data, sample_rate=None, format=None):
        self.data = bytes(data)


class FakeSource:
    def __init__(self, context):
        self.context = context
        self.position = None
        self.gain = 1.0
        self.direct_filter = None
        self.rolloff_factor = None
        self.reference_distance = None
        self.max_distance = None
        self.state = cyal.SourceState.INITIAL
        self.queued = []
        self.played = 0
        self.processed = 0
        self.destroyed = False

    @property
    def buffers_queued(self):
        return len(self.queued)

    @property
    def buffers_processed(self):
        # OpenAL reports everything a source that is not playing holds as
        # processed, which is what makes the flush below empty a queue.
        if self.state == cyal.SourceState.PLAYING:
            return self.processed
        return len(self.queued)

    def queue_buffers(self, buffer):
        self.queued.append(buffer)

    def unqueue_buffers(self, *, max=2 ** 31 - 1):
        # OpenAL hands back exactly what it calls processed, so the queue and
        # the count can never disagree (a source that is not playing gives up
        # its whole queue, which is what makes a flush empty it).
        count = min(int(self.buffers_processed), int(max))
        if count <= 0:
            return []
        taken = self.queued[:count]
        del self.queued[:count]
        self.processed = 0
        return taken

    def play(self):
        self.played += 1
        self.state = cyal.SourceState.PLAYING

    def stop(self):
        self.state = cyal.SourceState.STOPPED

    def delete(self):
        self.destroyed = True
        self.context.destroyed.append(self)


class FakeContext:
    def __init__(self):
        self.created = []
        self.destroyed = []
        self.buffers = 0

    def gen_source(self, **kwargs):
        source = FakeSource(self)
        self.created.append(source)
        return source

    def gen_buffer(self):
        self.buffers += 1
        return FakeBuffer()

    def batch(self):
        return contextlib.nullcontext()


class FakeAudio:
    def __init__(self, position=ANCHOR, music_volume=100, jukebox_volume=100):
        self.position = position
        self.volume_categories = {"jukebox": [jukebox_volume], "music": [music_volume]}
        self.filter = []
        self.context = FakeContext()
        self.efx = SimpleNamespace(send=lambda *args, **kwargs: None)

    def gen_filter(self, kind, *params):
        return ("filter", kind, params)


class FakeReverb:
    def __init__(self, reverb, bounds=(0.0, 100.0, 0.0, 100.0, -10.0, 10.0)):
        self.reverb = reverb
        self.bounds = bounds

    def in_bound(self, x, y, z):
        minx, maxx, miny, maxy, minz, maxz = self.bounds
        return minx <= x <= maxx and miny <= y <= maxy and minz <= z <= maxz


def make_game(speakers=(("front_l", 6.5, 26.5, 100.0, 0.0),
                        ("front_r", 13.5, 26.5, 100.0, 0.0)),
              cabinets=(("j1", ANCHOR),), listener=ANCHOR, reverb=None,
              tier=0, music_volume=100):
    """A map with cabinets, cinema speakers around them, and one listener.

    The speakers are spawned with the labels a room expects, so the real
    resolver runs; nothing here hand-builds a placement.
    """
    game = SimpleNamespace()
    audio = FakeAudio(listener, music_volume)
    game.audio_mngr = audio
    map_obj = Map(game)
    if reverb is not None:
        map_obj.reverb_list.append(FakeReverb(
            reverb, bounds=(ANCHOR[0] - 20, ANCHOR[0] + 20,
                            ANCHOR[1] - 20, ANCHOR[1] + 20, -10.0, 10.0)))
    for index, (name, x, y, level, delay) in enumerate(speakers):
        map_obj.spawn_cinemaSpeaker(minx=x - 0.5, maxx=x + 0.5,
                                    miny=y - 0.5, maxy=y + 0.5,
                                    minz=0, maxz=1, id=f"spk{index}",
                                    channel=name, level=level, delay=delay)
    map_obj.jukebox_list = [SimpleNamespace(id=cabinet_id, center=center)
                            for cabinet_id, center in cabinets]
    player = SimpleNamespace(
        cinema_mode=lambda cabinet_id: "auto",
        occlusion_tier=lambda *args: tier,
    )
    gameplay = SimpleNamespace(
        map=map_obj, jukebox_player=player, voice_channels={},
        player=SimpleNamespace(dead=False), game=game,
    )
    game.gameplay = gameplay
    game.audio_mngr.defer_audio = lambda fn: fn()
    return game


def two_room_game():
    """Two cabinets, each with the front pair that makes its own room."""
    return make_game(
        speakers=(("front_l", 6.5, 26.5, 100.0, 0.0),
                  ("front_r", 13.5, 26.5, 100.0, 0.0),
                  ("front_l", 6.5, 66.5, 100.0, 0.0),
                  ("front_r", 13.5, 66.5, 100.0, 0.0)),
        cabinets=(("j1", ANCHOR), ("j2", (10.0, 60.0, 0.0))),
    )


def push(feed, count=6, epoch=None, stereo=False):
    """Feed the room ``count`` frames and answer how many it took."""
    taken = 0
    for _ in range(count):
        if feed.push(FRAME, stereo=stereo, epoch=epoch):
            taken += 1
    return taken


class RoomsOffTests(unittest.TestCase):
    """The listener's own switch still decides what they hear."""

    def setUp(self):
        from libs import options
        self._saved = options.prefs.pop(OPTION_ENABLED, None)

    def tearDown(self):
        from libs import options
        options.prefs.pop(OPTION_ENABLED, None)
        if self._saved is not None:
            options.prefs[OPTION_ENABLED] = self._saved
        peer.release_all(getattr(self, "game", None))

    def test_a_listener_who_turned_rooms_off_keeps_the_plain_feed(self):
        from libs import options
        options.prefs[OPTION_ENABLED] = False
        self.game = make_game()
        peer.note_routing(self.game, 7, "j1")
        self.assertIsNone(peer.route(self.game, 7))
        self.assertEqual(self.game.audio_mngr.context.created, [])

    def test_the_switch_releases_a_room_already_playing(self):
        from libs import options
        self.game = make_game()
        peer.note_routing(self.game, 7, "j1")
        feed = peer.route(self.game, 7)
        self.assertIsNotNone(feed)
        bank = feed.bank
        options.prefs[OPTION_ENABLED] = False
        self.assertIsNone(peer.route(self.game, 7))
        self.assertTrue(bank._stopped)
        self.assertEqual(bank.sources, ())


class RoutedSongTests(unittest.TestCase):
    """A song the sender routed into a room is heard out of that room."""

    def tearDown(self):
        peer.release_all(getattr(self, "game", None))

    def routed(self, **kwargs):
        self.game = make_game(**kwargs)
        peer.note_routing(self.game, 7, "j1")
        return self.game, peer.route(self.game, 7)

    def test_a_routed_song_is_played_by_the_cabinet_speakers(self):
        game, feed = self.routed()
        self.assertIsNotNone(feed)
        self.assertEqual(feed.cabinet_id, "j1")
        self.assertEqual(len(feed.bank.sources), 2)
        self.assertEqual([source.played for source in feed.bank.sources], [0, 0])

        # The room waits for its pre-buffer, then every speaker starts.
        wanted = feed.bank.wanted_for_start()
        self.assertEqual(push(feed, count=wanted), wanted)
        self.assertTrue(feed.playing)
        for source in feed.bank.sources:
            self.assertEqual(source.state, cyal.SourceState.PLAYING)
            self.assertEqual(source.buffers_queued, wanted)

    def test_the_room_holds_the_frames_of_the_song(self):
        game, feed = self.routed()
        push(feed, count=8)
        for source in feed.bank.sources:
            self.assertEqual(source.buffers_queued, 8)

    def test_the_room_keeps_playing_longer_than_one_pool_holds(self):
        """A speaker's buffer pool is a handful of frames deep; a room that
        never takes finished buffers back would fall silent after a quarter of
        a second of song. 80 frames is 1.6 s, several pools over."""
        game, feed = self.routed()
        taken = 0
        for _ in range(80):
            for source in feed.bank.sources:
                source.processed += 1  # the device finished one frame
            if feed.push(FRAME):
                taken += 1
        self.assertEqual(taken, 80)
        self.assertTrue(feed.playing)
        for source in feed.bank.sources:
            self.assertLessEqual(source.buffers_queued, feed.bank.buffers_per_slot)

    def test_nothing_routed_builds_nothing(self):
        self.game = make_game()
        self.assertIsNone(peer.route(self.game, 7))
        self.assertEqual(self.game.audio_mngr.context.created, [])
        self.assertEqual(peer.describe(self.game), [])

    def test_the_room_is_where_the_cabinet_is_not_where_the_listener_stands(self):
        """The routing names a cabinet, so the sender can stand anywhere on
        the map and the room stays where the cabinet is."""
        game, feed = self.routed(listener=(60.0, 60.0, 0.0))
        self.assertIsNotNone(feed)
        self.assertEqual(tuple(feed.bank.anchor), ANCHOR)

    def test_a_room_the_map_cannot_make_keeps_the_plain_feed(self):
        self.game = make_game(speakers=(("front_l", 6.5, 26.5, 100.0, 0.0),))
        peer.note_routing(self.game, 7, "j1")
        self.assertIsNone(peer.route(self.game, 7))

    def test_a_cabinet_that_is_not_on_the_map_keeps_the_plain_feed(self):
        self.game = make_game()
        peer.note_routing(self.game, 7, "gone")
        self.assertIsNone(peer.route(self.game, 7))

    def test_turning_the_routing_off_hands_the_sources_back(self):
        game, feed = self.routed()
        push(feed, count=4)
        bank = feed.bank
        sources = list(bank.sources)
        peer.note_routing(game, 7, "")
        self.assertIsNone(peer.routing_for(game, 7))
        self.assertIsNone(peer.route(game, 7))
        self.assertTrue(bank._stopped)
        self.assertEqual(bank.sources, ())
        for source in sources:
            self.assertTrue(source.destroyed)

    def test_re_routing_to_the_same_cabinet_keeps_playing(self):
        game, feed = self.routed()
        push(feed, count=4)
        peer.note_routing(game, 7, "j1")
        self.assertIs(peer.route(game, 7), feed)
        self.assertTrue(feed.playing)

    def test_a_song_change_re_forms_the_room(self):
        game, feed = self.routed()
        push(feed, count=8, epoch=1)
        self.assertTrue(feed.playing)
        push(feed, count=1, epoch=2)
        self.assertFalse(feed.playing)
        for source in feed.bank.sources:
            self.assertEqual(source.buffers_queued, 1)

    def test_a_stale_routing_is_swept_and_rebuilt(self):
        game, feed = self.routed()
        push(feed, count=4)
        bank = feed.bank
        feed.last_push -= peer.STALE_AFTER + 1.0
        rebuilt = peer.route(game, 7)
        self.assertIsNotNone(rebuilt)
        self.assertIsNot(rebuilt, feed)
        self.assertTrue(bank._stopped)

    def test_a_party_sync_guest_keeps_the_private_feed(self):
        self.game = make_game()
        peer.note_routing(self.game, 7, "j1")
        entity = SimpleNamespace(_party_sync_direct=True)
        self.assertIsNone(peer.route(self.game, 7, entity))

    def test_two_peers_are_two_rooms(self):
        game = two_room_game()
        peer.note_routing(game, 7, "j1")
        peer.note_routing(game, 9, "j2")
        first = peer.route(game, 7)
        second = peer.route(game, 9)
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        self.assertIsNot(first.bank, second.bank)
        self.assertEqual(sorted(peer.describe(game)), sorted([
            "7: jukebox j1 (front_only, 2 speaker(s))",
            "9: jukebox j2 (front_only, 2 speaker(s))",
        ]))
        push(first, count=4)
        self.assertEqual(second.bank.queued_frames(), 0)

    def test_the_room_follows_a_speaker_placed_mid_song(self):
        game, feed = self.routed()
        push(feed, count=4)
        # On the room's own centre line: a centre speaker dropped 2 m off it
        # is what the placement resolver calls a mislabelled front, and a room
        # whose front pair it cannot pair is not a room at all.
        self.game.gameplay.map.spawn_cinemaSpeaker(
            minx=9.5, maxx=10.5, miny=25.5, maxy=26.5, minz=0, maxz=1,
            id="extra", channel="front_c", level=100.0, delay=0.0)
        feed.last_push = time.monotonic()
        peer.rooms_for(game)._reshaped_at = 0.0
        peer.route(game, 7)
        self.assertEqual(len(feed.bank.sources), 3)

    def test_the_room_follows_a_mark_dialled_mid_song(self):
        """A bass cabinet set on the map reaches a routed peer's room too.

        The feed re-acquires its room at most once a second and skips that when
        the resolved plan still *is* the room it is playing (``matches``), so a
        mark has to be part of what makes a plan the same room -- left out of
        that signature, the peer kept playing the room the map no longer
        described until the track changed.
        """
        game, feed = self.routed()
        push(feed, count=4)
        self.assertEqual(feed.bank._slot_crossover["front_l"], 0.0)
        source = feed.bank.slot_sources["front_l"]
        next(spec for spec in game.gameplay.map.cinema_speaker_list
             if spec.channel == "front_l").crossover = 120
        feed.last_push = time.monotonic()
        peer.rooms_for(game)._reshaped_at = 0.0
        peer.route(game, 7)
        self.assertEqual(feed.bank._slot_crossover["front_l"], 120.0,
                         "the mark never reached the peer's room")
        self.assertIs(feed.bank.slot_sources["front_l"], source,
                      "the room was rebuilt for a mark")

    def test_the_room_answers_to_the_music_slider(self):
        game, feed = self.routed(music_volume=50)
        self.assertEqual(feed.bank.category, "music")

    def test_the_cabinet_reverb_reaches_the_song(self):
        game, feed = self.routed(reverb="slot-3")
        self.assertEqual(feed.bank.reverb_slot, "slot-3")

    def test_a_map_with_no_reverb_plays_the_song_dry(self):
        game, feed = self.routed()
        self.assertIsNone(feed.bank.reverb_slot)

    def test_release_all_forgets_every_routing_and_room(self):
        game = make_game()
        peer.note_routing(game, 7, "j1")
        peer.note_routing(game, 9, "j1")
        first = peer.route(game, 7)
        bank = first.bank
        self.assertEqual(peer.release_all(game), 1)
        self.assertEqual(peer.routing_for(game, 7), None)
        self.assertEqual(peer.routing_for(game, 9), None)
        self.assertTrue(bank._stopped)

    def test_the_frames_a_mono_song_sends_are_the_same_on_both_sides(self):
        left, right = peer.split_frame(FRAME, False)
        self.assertIs(left, right)
        self.assertEqual((left, right), (FRAME, FRAME))

    def test_a_stereo_frame_keeps_its_two_sides(self):
        stereo = bytes([1, 0, 2, 0, 3, 0, 4, 0])
        left, right = peer.split_frame(stereo, True)
        self.assertEqual(left, bytes([1, 0, 3, 0]))
        self.assertEqual(right, bytes([2, 0, 4, 0]))


class ReceivePathTests(unittest.TestCase):
    """The routing reaches the stream that is playing, and nothing else does."""

    def make_handler(self, game):
        handler = EventHandeler.__new__(EventHandeler)
        handler.game = game
        handler.gameplay = game.gameplay
        return handler

    def test_the_announcement_is_what_the_receive_path_reads(self):
        game = make_game()
        handler = self.make_handler(game)
        handler.music_bot_cinema({"channel": 7, "cabinet": "j1"})
        self.assertEqual(peer.routing_for(game, 7), "j1")
        handler.music_bot_cinema({"channel": 7, "cabinet": ""})
        self.assertIsNone(peer.routing_for(game, 7))

    def test_a_malformed_announcement_changes_nothing(self):
        game = make_game()
        handler = self.make_handler(game)
        handler.music_bot_cinema({"channel": "seven", "cabinet": "j1"})
        handler.music_bot_cinema(None)
        handler.music_bot_cinema({"cabinet": "j1"})
        self.assertEqual(peer.routing_for(game, 7), None)
        self.assertEqual(len(peer.rooms_for(game, create=True)), 0)

    def test_a_routed_channel_points_the_stream_at_the_room(self):
        game = make_game()
        handler = self.make_handler(game)
        handler.music_bot_cinema({"channel": 7, "cabinet": "j1"})
        compression = SimpleNamespace(set_cinema_channel=lambda channel: None)
        recorded = []
        compression.set_cinema_channel = recorded.append
        entity = SimpleNamespace(music_source=object(), music_compression=compression)
        handler.gameplay.voice_channels[7] = entity
        packet = (bytes([1, 7]) + (5).to_bytes(4, "big") + (6).to_bytes(4, "big")
                  + b"opus")
        compression.recieve_timeline = lambda *args: None
        handler.process_music_timeline_data(packet)
        self.assertEqual(recorded, [7])

    def test_an_unrouted_channel_points_the_stream_back_at_the_entity(self):
        game = make_game()
        handler = self.make_handler(game)
        compression = SimpleNamespace(recieve_timeline=lambda *args: None)
        recorded = []
        compression.set_cinema_channel = recorded.append
        entity = SimpleNamespace(music_source=object(), music_compression=compression)
        handler.gameplay.voice_channels[7] = entity
        packet = (bytes([1, 7]) + (5).to_bytes(4, "big") + (6).to_bytes(4, "big")
                  + b"opus")
        handler.process_music_timeline_data(packet)
        self.assertEqual(recorded, [None])

    def test_a_party_sync_guest_points_the_stream_back_at_the_entity(self):
        game = make_game()
        handler = self.make_handler(game)
        handler.music_bot_cinema({"channel": 7, "cabinet": "j1"})
        compression = SimpleNamespace(recieve_timeline=lambda *args: None)
        recorded = []
        compression.set_cinema_channel = recorded.append
        entity = SimpleNamespace(music_source=object(), music_compression=compression,
                                 _party_sync_direct=True)
        handler.gameplay.voice_channels[7] = entity
        packet = (bytes([1, 7]) + (5).to_bytes(4, "big") + (6).to_bytes(4, "big")
                  + b"opus")
        handler.process_music_timeline_data(packet)
        self.assertEqual(recorded, [None])

    def test_an_old_entity_without_the_seam_is_left_alone(self):
        """A compression object from another build must not be an error."""
        game = make_game()
        handler = self.make_handler(game)
        handler.music_bot_cinema({"channel": 7, "cabinet": "j1"})
        played = []
        entity = SimpleNamespace(music_source=object(), music_compression=SimpleNamespace(
            recieve=lambda *args: played.append(args)))
        handler.gameplay.voice_channels[7] = entity
        packet = bytes([7]) + b"opus"
        handler.process_music_data(packet)
        self.assertEqual(len(played), 1)


def make_compression(game):
    """A MusicCompression built without its worker thread (see __new__)."""
    compression = MusicCompression.__new__(MusicCompression)
    compression.game = game
    compression._format_generation = 0
    compression._pending_format_flush = False
    compression._stereo = False
    compression._has_started = False
    compression._last_recv_time = None
    compression.cinema_channel = None
    compression.cinema_feed = None
    compression._timeline_epoch = None
    compression._timeline_last_received_seq = None
    compression._timeline_first_queued_seq = None
    compression._timeline_anchor_seq = None
    compression._timeline_anchor_time = None
    compression._timeline_pending = []
    return compression


class RoomOutputTests(unittest.TestCase):
    """What the decoded frame does when a room owns the output."""

    def make_compression(self, game):
        return make_compression(game)

    def test_a_routed_stream_never_touches_the_entity_source(self):
        game = make_game()
        peer.note_routing(game, 7, "j1")
        compression = self.make_compression(game)
        compression.set_cinema_channel(7)
        entity_source = FakeSource(game.audio_mngr.context)
        for _ in range(6):
            compression._play_music_frame(entity_source, FRAME, None, None,
                                          game.gameplay)
        self.assertEqual(entity_source.buffers_queued, 0)
        feed = compression.cinema_feed
        self.assertIsNotNone(feed)
        for source in feed.bank.sources:
            self.assertEqual(source.buffers_queued, 6)

    def test_an_unrouted_stream_still_plays_on_the_entity_source(self):
        game = make_game()
        compression = self.make_compression(game)
        entity_source = FakeSource(game.audio_mngr.context)
        for _ in range(6):
            compression._play_music_frame(entity_source, FRAME, None, None,
                                          game.gameplay)
        self.assertIsNone(compression.cinema_feed)
        self.assertEqual(entity_source.buffers_queued, 6)

    def test_handing_the_stream_to_a_room_flushes_the_other_output(self):
        game = make_game()
        compression = self.make_compression(game)
        entity_source = FakeSource(game.audio_mngr.context)
        for _ in range(6):
            compression._play_music_frame(entity_source, FRAME, None, None,
                                          game.gameplay)
        self.assertEqual(entity_source.buffers_queued, 6)
        peer.note_routing(game, 7, "j1")
        compression.set_cinema_channel(7)
        wanted = 4
        for _ in range(wanted):
            compression._play_music_frame(entity_source, FRAME, None, None,
                                          game.gameplay)
        self.assertEqual(entity_source.buffers_queued, 0)
        self.assertTrue(compression.cinema_feed.playing)

    def test_the_clock_starts_on_the_frame_the_room_starts_playing(self):
        """The anchor frame is the room's first audible one, taken at once.

        ``start_playback`` plays the oldest queued frame the moment it
        returns, and every later frame follows one frame later -- so the
        queue's depth is already in the sequence delta. Moving the anchor out
        by the queue as well put this clock a whole pre-buffer ahead of what
        the speakers were really playing.
        """
        game = make_game()
        peer.note_routing(game, 7, "j1")
        compression = self.make_compression(game)
        compression.set_cinema_channel(7)
        entity_source = FakeSource(game.audio_mngr.context)
        for _ in range(4):
            compression._play_music_frame(entity_source, FRAME, 5, 100, game.gameplay)
        self.assertTrue(compression.cinema_feed.playing)
        self.assertGreater(compression.cinema_feed.latency_s(), 0)
        # The room is a queue deep, and none of it is in the anchor: the
        # anchor is audible now.
        self.assertIsNotNone(compression._timeline_anchor_time)
        self.assertLess(compression._timeline_anchor_time,
                        time.perf_counter() + 0.05)
        self.assertEqual(compression._timeline_anchor_seq, 100)

    def test_a_note_waits_only_until_the_room_reaches_its_frame(self):
        """Six frames after the room starts, the frame it is playing is past.

        Measured from when the speakers actually started -- not from the
        anchor the code happens to keep -- so an anchor that is a pre-buffer
        out holds the note back here and fails.
        """
        game = make_game()
        peer.note_routing(game, 7, "j1")
        compression = self.make_compression(game)
        compression.set_cinema_channel(7)
        started = time.perf_counter()
        for _ in range(4):
            compression._play_music_frame(FakeSource(game.audio_mngr.context), FRAME,
                                          5, 100, game.gameplay)
        compression._timeline_epoch = 5
        fired = []
        compression._queue_timeline_event(5, 105, lambda: fired.append("note"))
        compression._dispatch_timeline_events(started + 0.13)
        self.assertEqual(fired, ["note"])

    def test_two_peers_play_through_two_rooms(self):
        game = two_room_game()
        peer.note_routing(game, 7, "j1")
        peer.note_routing(game, 9, "j2")
        first = self.make_compression(game)
        second = self.make_compression(game)
        first.set_cinema_channel(7)
        second.set_cinema_channel(9)
        for _ in range(6):
            first._play_music_frame(FakeSource(game.audio_mngr.context), FRAME,
                                    None, None, game.gameplay)
            second._play_music_frame(FakeSource(game.audio_mngr.context), FRAME,
                                     None, None, game.gameplay)
        self.assertIsNot(first.cinema_feed, second.cinema_feed)
        self.assertEqual(first.cinema_feed.cabinet_id, "j1")
        self.assertEqual(second.cinema_feed.cabinet_id, "j2")


class FakeNetwork:
    def __init__(self):
        self.sent = []

    def send(self, *args, **kwargs):
        self.sent.append((args, kwargs))


class FakeGameNetwork:
    def __init__(self, gameplay=None):
        self.network = FakeNetwork()
        self.gameplay = gameplay


class AnnouncementTests(unittest.TestCase):
    """The sender tells the map which room the song is playing in."""

    def make_bot(self, cabinet="j1", allowed=True, network=True):
        from libs.music_bot.controller import MapMusicBot
        bot = MapMusicBot.__new__(MapMusicBot)
        bot.game = FakeGameNetwork(SimpleNamespace(
            can_use_cinema_speakers=allowed))
        if not network:
            bot.game.network = None
        # The real lookup walks the game's state stack for the Gameplay state;
        # this harness has the state itself, which is all the routing asks it.
        bot._find_gameplay = lambda: bot.game.gameplay
        bot.cinema_target = cabinet
        bot._cinema_announced = None
        bot._cinema_announced_at = 0.0
        return bot

    def test_a_routing_is_announced_at_once(self):
        bot = self.make_bot()
        self.assertTrue(bot.announce_cinema_target(force=True))
        args, _ = bot.game.network.sent[-1]
        self.assertEqual(args[0], consts.CHANNEL_MISC)
        self.assertEqual(args[1], peer.ANNOUNCE_EVENT)
        self.assertEqual(args[2], {"cabinet": "j1"})

    def test_saying_it_twice_is_not_said_twice(self):
        bot = self.make_bot()
        bot.announce_cinema_target(force=True)
        self.assertFalse(bot.announce_cinema_target())
        self.assertEqual(len(bot.game.network.sent), 1)

    def test_a_repeat_arrives_within_the_interval(self):
        bot = self.make_bot()
        bot.announce_cinema_target(force=True)
        bot._cinema_announced_at -= bot.CINEMA_ANNOUNCE_INTERVAL + 0.1
        self.assertTrue(bot.announce_cinema_target())
        self.assertEqual(len(bot.game.network.sent), 2)

    def test_an_account_that_never_had_the_routing_announces_off(self):
        bot = self.make_bot(allowed=False)
        self.assertFalse(bot.cinema_force_upload)
        bot.announce_cinema_target(force=True)
        args, _ = bot.game.network.sent[-1]
        self.assertEqual(args[2], {"cabinet": ""})

    def test_a_room_routed_song_uploads_without_the_broadcast_switch(self):
        bot = self.make_bot()
        self.assertTrue(bot.cinema_force_upload)
        bot.cinema_target = None
        self.assertFalse(bot.cinema_force_upload)

    def test_no_network_is_not_an_error(self):
        bot = self.make_bot(network=False)
        self.assertFalse(bot.announce_cinema_target(force=True))


class FakeBot:
    broadcast_enabled = False
    broadcast_to_megaphone = False
    cinema_force_upload = False

    def _find_gameplay(self):
        return None


class UploadGateTests(unittest.TestCase):
    """The frames reach a room even with the bot in private mode."""

    def make(self, cinema=False):
        from libs import music_bot as mb
        bot = FakeBot()
        bot.cinema_force_upload = cinema
        game = FakeGameNetwork()
        return mb.AudioStreamer(game, "http://example.com/a.mp3", object(),
                                volume=50, bot=bot, channels=2), game

    def test_a_routed_song_uploads(self):
        streamer, game = self.make(cinema=True)
        streamer._send_to_network_actual(b"\x00" * 3840)
        self.assertEqual(len(game.network.sent), 1)
        self.assertEqual(game.network.sent[0][0][0], consts.CHANNEL_MUSICBOT)

    def test_a_private_bot_that_is_not_routed_stays_private(self):
        streamer, game = self.make(cinema=False)
        streamer._send_to_network_actual(b"\x00" * 3840)
        self.assertEqual(game.network.sent, [])


class TimelineMarkerTests(unittest.TestCase):
    """A routed song is a broadcast as far as live notes are concerned."""

    def make_bot(self, cabinet="j1"):
        from libs.music_bot.controller import MapMusicBot
        bot = MapMusicBot.__new__(MapMusicBot)
        bot.broadcast_enabled = False
        bot.broadcast_to_megaphone = False
        bot.paused = False
        bot.playing = True
        bot.cinema_target = cabinet
        bot.game = FakeGameNetwork(SimpleNamespace(can_use_cinema_speakers=True))
        bot._find_gameplay = lambda: bot.game.gameplay
        bot.streamer = SimpleNamespace(performance_timeline_marker=lambda: {"v": 1})
        return bot

    def test_the_marker_travels_when_a_room_needs_the_clock(self):
        bot = self.make_bot()
        self.assertEqual(bot.performance_timeline_marker(), {"v": 1})

    def test_a_private_bot_with_no_routing_has_no_timeline(self):
        bot = self.make_bot(cabinet=None)
        self.assertIsNone(bot.performance_timeline_marker())


class RoomNoteReportTests(unittest.TestCase):
    """What a note over a room-fed song cost, said out loud.

    The note waits for the frame the performer heard to reach this client's
    own room, so the wait is the room's queue plus this machine's distance
    behind the broadcast -- and "the band feels late" cannot be told apart
    from a room that is simply long without those numbers. Only a room is
    reported; the plain pair keeps its own anomaly line.
    """

    CHANNEL = 7
    EPOCH = 5

    def make_handler(self, game, compression):
        game.gameplay.voice_channels = {
            self.CHANNEL: SimpleNamespace(music_compression=compression,
                                          music_source=object())}
        handler = EventHandeler.__new__(EventHandeler)
        handler.gameplay = game.gameplay
        handler.game = game
        handler._clock_offset_ms = 0.0
        handler._last_jam_sync_log = 0.0
        return handler

    def make_feed(self, game, routed=True):
        """A compression with (optionally) a room playing, and a worker queue."""
        if routed:
            peer.note_routing(game, self.CHANNEL, "j1")
        compression = make_compression(game)
        compression.queue = queue.Queue()
        compression._running = True
        compression._timeline_epoch = self.EPOCH
        compression.cinema_channel = self.CHANNEL if routed else None
        if routed:
            for _ in range(4):
                compression._play_music_frame(
                    FakeSource(game.audio_mngr.context), FRAME, self.EPOCH, 100,
                    game.gameplay)
        return compression

    def schedule(self, handler, compression, data):
        """Hand one note to the timeline and let it land on this thread."""
        data = dict(data, music_sync={"version": 1, "voice_channel": self.CHANNEL,
                                      "epoch": self.EPOCH, "frame_seq": 100})
        callback = mock.Mock()
        self.assertTrue(handler._schedule_music_synced(data, callback))
        while not compression.queue.empty():
            compression.queue.get_nowait()()     # the worker's own turn
        # Past the event timeout: the note plays, which is what is reported.
        compression._dispatch_timeline_events(time.perf_counter() + 2.0)
        return callback

    def test_a_note_over_a_room_says_how_late_it_is_heard(self):
        game = make_game()
        compression = self.make_feed(game)
        handler = self.make_handler(game, compression)
        now_ms = time.time() * 1000.0
        with mock.patch("libs.event_handeler.time.time",
                        return_value=now_ms / 1000.0), \
                mock.patch("libs.deferred_log.log_deferred") as log_line:
            callback = self.schedule(handler, compression,
                                     {"server_time": now_ms - 200.0,
                                      "sender_lag_ms": 0})
        callback.assert_called_once_with()
        line = log_line.call_args_list[0].args[0]
        self.assertIn("heard 200ms", line)
        self.assertIn("music timeline through a room", line)
        self.assertIn("room queue=", line)

    def test_a_note_over_the_plain_feed_is_not_reported_as_a_room(self):
        game = make_game()
        compression = self.make_feed(game, routed=False)
        handler = self.make_handler(game, compression)
        now_ms = time.time() * 1000.0
        with mock.patch("libs.event_handeler.time.time",
                        return_value=now_ms / 1000.0), \
                mock.patch("libs.deferred_log.log_deferred") as log_line:
            self.schedule(handler, compression, {"server_time": now_ms - 200.0})
        log_line.assert_not_called()


class DiagnosisTests(unittest.TestCase):
    """A listener who hears nothing is told why, in the log.

    The room is resolved from the listener's *own* map, so the sender's menu
    (which describes the sender's client) cannot explain it: a cabinet that is
    not here, a speaker set that makes no usable room, or a room that plays
    are three different lines, and this is the only place any of them exists.
    """

    def tearDown(self):
        peer.release_all(getattr(self, "game", None))

    def spoken(self, game, channel=7):
        with mock.patch.object(peer, "log_line") as log:
            feed = peer.route(game, channel)
        lines = [call.args[0] for call in log.call_args_list]
        return feed, lines

    def test_a_cabinet_that_is_not_on_this_map_says_which_one(self):
        self.game = make_game()
        peer.note_routing(self.game, 7, "gone")
        feed, lines = self.spoken(self.game)
        self.assertIsNone(feed)
        self.assertTrue(any("jukebox gone is not on this map" in line
                            for line in lines), lines)

    def test_a_map_that_cannot_make_the_room_says_which_speaker_is_missing(self):
        # One front speaker: the resolver's own reason names the missing half
        # of the pair, which is what tells a tester what to go and place.
        self.game = make_game(speakers=(("front_l", 6.5, 26.5, 100.0, 0.0),))
        peer.note_routing(self.game, 7, "j1")
        feed, lines = self.spoken(self.game)
        self.assertIsNone(feed)
        self.assertTrue(any("no room around jukebox j1" in line
                            and "front_r" in line for line in lines), lines)

    def test_a_room_that_plays_says_which_room(self):
        self.game = make_game()
        peer.note_routing(self.game, 7, "j1")
        feed, lines = self.spoken(self.game)
        self.assertIsNotNone(feed)
        self.assertTrue(any("music bot 7 -> jukebox j1" in line
                            and "2 speaker(s)" in line for line in lines), lines)

    def test_the_announcement_arriving_is_said_out_loud(self):
        """With no line here the routing never reached this client, so the
        plain feed is not a room problem at all - which is the one thing a
        silent console could not tell apart."""
        self.game = make_game()
        with mock.patch.object(peer, "log_line") as log:
            peer.note_routing(self.game, 7, "j1")
            for _ in range(4):                       # the repeat while it plays
                peer.note_routing(self.game, 7, "j1")
        self.assertEqual([call.args[0] for call in log.call_args_list],
                         ["[Cinema] music bot 7 is routed to jukebox j1"])
        with mock.patch.object(peer, "log_line") as log:
            peer.note_routing(self.game, 7, "")
        self.assertEqual([call.args[0] for call in log.call_args_list],
                         ["[Cinema] music bot 7 is back on the plain feed"])

    def test_the_listeners_own_switch_is_named_as_the_reason(self):
        from libs import options
        saved = options.prefs.pop(OPTION_ENABLED, None)
        try:
            options.prefs[OPTION_ENABLED] = False
            self.game = make_game()
            peer.note_routing(self.game, 7, "j1")
            feed, lines = self.spoken(self.game)
            self.assertIsNone(feed)
            self.assertTrue(any("Cinema rooms switch is off" in line
                                for line in lines), lines)
        finally:
            options.prefs.pop(OPTION_ENABLED, None)
            if saved is not None:
                options.prefs[OPTION_ENABLED] = saved

    def test_the_same_problem_is_not_said_over_and_over(self):
        # A routing whose room cannot be built is retried every couple of
        # seconds for as long as the song plays: the reason is news once.
        self.game = make_game()
        peer.note_routing(self.game, 7, "gone")
        with mock.patch.object(peer, "log_line") as log:
            for _ in range(6):
                peer.route(self.game, 7)
        self.assertEqual(log.call_count, 1)


if __name__ == "__main__":
    unittest.main()
