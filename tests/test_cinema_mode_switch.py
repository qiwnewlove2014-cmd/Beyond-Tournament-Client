"""A cinema mode changed while its song plays, without rebuilding the song.

The complaint this file answers was heard as a loading pause: picking a mode
on a cabinet that was already playing stopped the song, tore the stream down
(a new decode, a new relay receiver, a fresh pre-buffer) and started it again.
Only a cabinet's *output* changes when a mode changes, so nothing here may
touch the transport:

* a different shape is applied to the room that is playing (the bank a stream
  holds is the same object and ``reconfigure`` moves only the speakers that
  changed);
* a room hands its own front pair over to become the cabinet's plain pair --
  the pair keeps the frames the room was about to play, so the song does not
  step back and the room's other speakers stop where they stand;
* a plain pair asks a room to take over and waits for its pre-buffer, and a
  stream that cannot make that window leaves the room for the next song
  instead of cutting anything off.
"""

import os
import sys
import time
import unittest
from types import SimpleNamespace
from unittest import mock

import cyal

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from libs import jukebox
from libs.audio.cinema import CinemaSpeakerBank, host_for, set_enabled
from libs.audio.cinema.layout import CinemaSpeakerSpec
from libs.music_bot.streaming import AudioStreamer
from libs.world_map import Map

ANCHOR = (10.0, 20.0, 0.0)
ROOM = [("front_l", -30), ("front_r", 30), ("rear_l", 150), ("rear_r", 210)]


class FakeSource:
    """Minimal OpenAL source: a queue of buffers, a play state, a gain."""

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
        self.buffers = []
        self.buffers_processed = 0
        self.state = cyal.SourceState.STOPPED
        self.played = 0
        self.deleted = False

    @property
    def buffers_queued(self):
        return len(self.buffers)

    def play(self):
        self.played += 1
        self.state = cyal.SourceState.PLAYING

    def stop(self):
        self.state = cyal.SourceState.STOPPED

    def delete(self):
        self.deleted = True
        self.context.deleted.append(self)

    def queue_buffers(self, buffer):
        self.buffers.append(buffer)

    def unqueue_buffers(self):
        if self.buffers_processed <= 0:
            return None
        self.buffers_processed -= 1
        if self.buffers:
            self.buffers.pop(0)
        return []


class FakeBuffer:
    def __init__(self):
        self.data = None

    def set_data(self, data, sample_rate=None, format=None):
        self.data = bytes(data)


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


class FakeAudio:
    def __init__(self):
        self.context = FakeContext()
        self.efx = SimpleNamespace(send=lambda *a, **k: None)
        self.filter = []
        self.position = ANCHOR
        self.volume_categories = {"jukebox": [100], "music": [100]}

    def gen_filter(self, kind, *params):
        return ("filter", kind, params)


class FakeGame:
    def __init__(self):
        self.audio_mngr = FakeAudio()
        self.gameplay = SimpleNamespace(map=SimpleNamespace())


class FakeStreamer:
    """A stream this file can watch: the room, the pair and what was asked.

    Only the hand-over surface the jukebox uses is here: everything else a
    real stream does (a decode, a relay receiver, buffers) is exactly what
    these tests exist to prove is *not* touched.
    """

    def __init__(self, cinema=None, pair=None):
        self.cinema = cinema
        self.spatial_pair = pair
        self.requests = []
        self.cancels = []
        self.pairs = []
        self.swap_room_failed = False
        self.stopped = 0

    def request_room(self, bank):
        self.requests.append(bank)
        return True

    def cancel_room(self, bank=None):
        self.cancels.append(bank)
        return True

    def switch_to_pair(self, pair):
        self.pairs.append(pair)
        self.spatial_pair = pair
        self.cinema = None
        return True

    def stop(self):
        self.stopped += 1


def frame(tag, size=8):
    return bytes([tag, 0] * size)


def build_map(game, entries=ROOM, anchor=ANCHOR, radius=8.0, front=None):
    """The room the tests play through; ``front`` marks the front_l speaker."""
    from math import cos, radians, sin

    map_obj = Map(game)
    for index, (channel, bearing) in enumerate(entries):
        angle = radians(bearing)
        x = anchor[0] + sin(angle) * radius
        y = anchor[1] + cos(angle) * radius
        extra = dict(front) if (front and channel == "front_l") else {}
        map_obj.spawn_cinemaSpeaker(minx=x - 0.5, maxx=x + 0.5, miny=y - 0.5,
                                    maxy=y + 0.5, minz=0, maxz=1,
                                    id=f"spk{index}", channel=channel, **extra)
    map_obj.spawn_jukebox(minx=9, maxx=10, miny=19, maxy=20, minz=0, maxz=1,
                          id="j1")
    game.gameplay.map = map_obj
    return map_obj


def play_song(player, **kwargs):
    options = {"title": "Song", "url": "http://example.com/a.mp3",
               "duration": 60, "transport": "direct"}
    options.update(kwargs)
    jukebox_id = options.pop("jukebox_id", "j1")
    with mock.patch("libs.music_bot.AudioStreamer") as streamer:
        player.play(jukebox_id, 9.5, 19.5, 0.5,
                    options.pop("title"), options.pop("url"),
                    options.pop("duration"), **options)
    return streamer


class SameSongRoomReshapedTests(unittest.TestCase):
    """A new shape for a room that is playing: the room itself is re-shaped."""

    def build(self, mode="auto"):
        game = FakeGame()
        set_enabled(game, True)
        build_map(game)
        player = jukebox.JukeboxPlayer(game)
        play_song(player, cinema_mode=mode)
        entry = player.players["j1"]
        entry["streamer"] = FakeStreamer(cinema=entry["cinema"])
        return game, player, entry

    def test_a_mode_change_reshapes_the_room_that_is_playing(self):
        game, player, entry = self.build()
        room = entry["cinema"]
        streamer = entry["streamer"]
        self.assertEqual(room.renderer.profile.name, "theatre")

        play_song(player, cinema_mode="front_only")

        # The very same room, the very same stream: nothing was rebuilt, and
        # the speakers the room no longer uses are the only ones that stopped.
        self.assertIs(player.players["j1"]["cinema"], room)
        self.assertIs(player.players["j1"]["streamer"], streamer)
        self.assertFalse(room.spent)
        self.assertEqual(room.renderer.profile.name, "front_only")
        self.assertEqual(set(room.renderer.slots), {"front_l", "front_r"})
        self.assertEqual(player.players["j1"]["cinema_mode"], "front_only")
        self.assertFalse(any(source.deleted for source in room.sources))
        self.assertEqual(streamer.stopped, 0)

    def test_the_shape_of_the_pair_is_asked_for_by_the_map_again(self):
        """The plan is re-resolved, so a room follows the map it stands on."""
        game, player, entry = self.build()
        room = entry["cinema"]
        # A map that lost its rear speakers while the song was playing, put
        # back through the play the server re-offers with the map's own mode.
        game.gameplay.map = build_map(game, entries=ROOM[:2])

        play_song(player, cinema_mode="front_only")

        self.assertIs(player.players["j1"]["cinema"], room)
        self.assertEqual(set(room.renderer.slots), {"front_l", "front_r"})

    def test_a_mode_that_stops_resolving_leaves_the_pair_playing(self):
        """A room that is audible is not yanked out from under the song."""
        game, player, entry = self.build()
        room = entry["cinema"]
        # The map loses every speaker while the song plays.
        game.gameplay.map = SimpleNamespace()

        play_song(player, cinema_mode="theatre")

        self.assertIs(player.players["j1"]["cinema"], room)
        self.assertFalse(room.spent)

    def test_the_room_the_refresh_follows_is_the_new_mode(self):
        game, player, entry = self.build()
        play_song(player, cinema_mode="front_only")
        self.assertEqual(player.players["j1"]["cinema_mode"], "front_only")
        # A refresh re-resolves with the mode the output was built for.
        self.assertEqual(player.refresh_cinema_rooms(now=time.monotonic() + 30.0), 1)
        self.assertEqual(player.players["j1"]["cinema"].renderer.profile.name,
                         "front_only")


class RoomLetsGoOfItsOwnPairTests(unittest.TestCase):
    """The room's front pair is the plain pair, and a band is not."""

    def build_bank(self, front=None):
        game = FakeGame()
        set_enabled(game, True)
        build_map(game, front=front)
        player = jukebox.JukeboxPlayer(game)
        play_song(player, cinema_mode="auto")
        return game, player, player.players["j1"]["cinema"]

    def test_the_front_pair_comes_out_with_its_frames(self):
        game, player, room = self.build_bank()
        sources = list(room.sources)
        pair = room.detach_primary_pair()
        self.assertEqual(tuple(pair), tuple(sources[:2]))
        # The room keeps its own speakers, and the pair is no longer its.
        self.assertEqual(len(list(room.sources)), len(sources) - 2)
        self.assertNotIn(pair[0], list(room.sources))

    def test_a_crossed_front_speaker_is_refused(self):
        """A bass-only front channel is not the stereo a plain cabinet plays."""
        game, player, room = self.build_bank(front={"crossover": 80})
        self.assertIsNone(room.detach_primary_pair())
        self.assertEqual(len(list(room.sources)), 4)

    def test_a_trimmed_front_speaker_is_refused(self):
        game, player, room = self.build_bank(front={"delay": 40})
        self.assertIsNone(room.detach_primary_pair())


class PairTakesTheSongOverTests(unittest.TestCase):
    """``off`` mid-song: the room hands its pair over and stops."""

    def build(self):
        game = FakeGame()
        set_enabled(game, True)
        build_map(game)
        player = jukebox.JukeboxPlayer(game)
        play_song(player, cinema_mode="auto")
        entry = player.players["j1"]
        streamer = FakeStreamer(cinema=entry["cinema"])
        entry["streamer"] = streamer
        # The room is mid-song: its pair holds the frames it was about to play.
        for source in entry["cinema"].sources:
            source.state = cyal.SourceState.PLAYING
        return game, player, entry, streamer, entry["cinema"]

    def test_the_pair_keeps_playing_and_the_room_gives_its_slots_back(self):
        game, player, entry, streamer, room = self.build()
        pair_sources = list(room.sources)[:2]

        play_song(player, cinema_mode="off")

        playing = player.players["j1"]
        self.assertIsNone(playing["cinema"])
        self.assertEqual(playing["cinema_mode"], "off")
        # The pair is the room's own front pair, still carrying the song.
        self.assertEqual((playing["source"], playing["secondary_source"]),
                         tuple(pair_sources))
        self.assertEqual(streamer.pairs[-1], (pair_sources[0], pair_sources[1], 8.0, 40.0))
        self.assertFalse(pair_sources[0].deleted)
        self.assertEqual(pair_sources[0].state, cyal.SourceState.PLAYING)
        # ...and the speakers the room no longer feeds are gone, with the room.
        self.assertTrue(room.spent)
        self.assertIsNone(host_for(game).bank("j1"))
        self.assertEqual(len(list(room.sources)), 0)

    def test_a_crossed_front_pair_falls_back_to_the_rebuild(self):
        """The one case that cannot hand over: the old path still runs."""
        game, player, entry, streamer, room = self.build()
        room.detach_primary_pair = lambda: None
        rebuilt = play_song(player, cinema_mode="off")
        # play() rebuilt the song exactly as it did before this change: a new
        # stream with the plain pair. (The retired room is released by the
        # fade worker half a second later, which is the old path's own pace.)
        self.assertTrue(rebuilt.called)
        self.assertIsNone(rebuilt.call_args.kwargs.get("cinema"))
        self.assertIsNot(player.players["j1"]["streamer"], streamer)
        self.assertIsNone(player.players["j1"]["cinema"])
        self.assertIsNotNone(player.players["j1"]["source"])

    def test_a_pending_room_is_cancelled_by_an_off_pick(self):
        game, player, entry, streamer, room = self.build()
        entry["cinema"] = None
        entry["cinema_pending"] = room
        entry["cinema_pending_at"] = time.monotonic()
        streamer.cinema = None
        streamer.request_room(room)

        play_song(player, cinema_mode="off")

        self.assertIsNone(player.players["j1"]["cinema_pending"])
        self.assertEqual(streamer.cancels[-1], room)
        self.assertIsNone(host_for(game).bank("j1"))


class RoomTakesTheSongOverTests(unittest.TestCase):
    """A room asked for mid-song is committed by the stream, not by a rebuild."""

    def build(self):
        game = FakeGame()
        set_enabled(game, True)
        build_map(game)
        player = jukebox.JukeboxPlayer(game)
        with mock.patch("libs.music_bot.AudioStreamer"):
            # A cabinet that is playing plainly: this is the crossing the
            # tests below are about (a room asked for mid-song).
            player.play("j1", 9.5, 19.5, 0.5, "Song", "http://example.com/a.mp3",
                        60, transport="direct", cinema_mode="off")
        entry = player.players["j1"]
        pair = (FakeSource(game.audio_mngr.context),
                FakeSource(game.audio_mngr.context))
        for source in pair:
            source.state = cyal.SourceState.PLAYING
        streamer = FakeStreamer(cinema=None, pair=pair)
        entry["streamer"] = streamer
        entry["source"], entry["secondary_source"] = pair
        return game, player, entry, streamer, pair

    def test_the_room_is_only_requested_at_first(self):
        game, player, entry, streamer, pair = self.build()
        play_song(player, cinema_mode="theatre")
        pending = player.players["j1"]["cinema_pending"]
        self.assertIsInstance(pending, CinemaSpeakerBank)
        self.assertIn(pending, streamer.requests)
        # The pair is still the output until the stream commits it.
        self.assertEqual((player.players["j1"]["source"],
                          player.players["j1"]["secondary_source"]), pair)
        self.assertIsNone(player.players["j1"]["cinema"])

    def test_a_commit_is_finished_by_the_jukebox(self):
        game, player, entry, streamer, pair = self.build()
        play_song(player, cinema_mode="theatre")
        room = player.players["j1"]["cinema_pending"]
        # What the stream does when it can prime the room.
        streamer.cinema = room
        streamer.spatial_pair = None

        player._sweep_cinema_swaps(time.monotonic())

        playing = player.players["j1"]
        self.assertIs(playing["cinema"], room)
        self.assertIsNone(playing["cinema_pending"])
        self.assertEqual(playing["cinema_mode"], "theatre")
        self.assertEqual((playing["source"], playing["secondary_source"]),
                         (room.primary_source, room.secondary_source))
        # The pair that carried the song is gone; the room is the output.
        self.assertTrue(all(source.deleted for source in pair))

    def test_a_stream_that_could_not_prime_keeps_the_pair(self):
        game, player, entry, streamer, pair = self.build()
        play_song(player, cinema_mode="theatre")
        room = player.players["j1"]["cinema_pending"]
        streamer.swap_room_failed = True

        player._sweep_cinema_swaps(time.monotonic())

        playing = player.players["j1"]
        self.assertIsNone(playing["cinema_pending"])
        self.assertIsNone(playing["cinema"])
        self.assertIsNone(host_for(game).bank("j1"))
        self.assertFalse(any(source.deleted for source in pair))
        # The mode is still the map's for the next song.
        self.assertEqual(player.cinema_mode("j1"), "theatre")

    def test_a_room_that_is_never_taken_is_given_back(self):
        game, player, entry, streamer, pair = self.build()
        play_song(player, cinema_mode="theatre")
        room = player.players["j1"]["cinema_pending"]
        entry["cinema_pending_at"] = time.monotonic() - 60.0

        player._sweep_cinema_swaps(time.monotonic())

        self.assertIsNone(player.players["j1"]["cinema_pending"])
        self.assertIsNone(host_for(game).bank("j1"))
        self.assertIn(room, streamer.cancels)

    def test_a_stop_while_pending_releases_both_outputs(self):
        game, player, entry, streamer, pair = self.build()
        play_song(player, cinema_mode="theatre")
        room = player.players["j1"]["cinema_pending"]

        player.stop("j1")

        self.assertFalse(player.players)
        self.assertIsNone(host_for(game).bank("j1"))
        self.assertTrue(all(source.deleted for source in pair))
        self.assertTrue(all(source.deleted for source in room.sources))


class StreamHandsItsOutputOverTests(unittest.TestCase):
    """The real streamer: primed from its own ring, committed on a frame."""

    def build_streamer(self, cinema=None, pair=None):
        game = FakeGame()
        set_enabled(game, True)
        map_obj = build_map(game)
        source = game.audio_mngr.context.gen_source()
        streamer = AudioStreamer(game, "http://example.com/a.mp3", source,
                                 volume=80, bot=None, cinema=cinema,
                                 spatial_pair=pair)
        streamer._init_buffer_pool()
        return game, streamer, map_obj

    def build_pair(self, game):
        return (game.audio_mngr.context.gen_source(),
                game.audio_mngr.context.gen_source())

    def test_the_room_starts_level_with_the_pair_it_replaced(self):
        game, streamer, map_obj = self.build_streamer()
        pair = self.build_pair(game)
        pair_tuple = (pair[0], pair[1], 8.0, 40.0)
        streamer.switch_to_pair(pair_tuple)
        for tag in range(1, 7):
            self.assertTrue(streamer._queue_local(frame(tag)))
        self.assertEqual(streamer.pair_queued_frames(), 6)

        room = host_for(game).acquire_bank("j1", ANCHOR, profile="theatre",
                                           volume=80)
        self.assertTrue(streamer.request_room(room))

        self.assertTrue(streamer._queue_local(frame(7)))

        self.assertIs(streamer.cinema, room)
        self.assertIsNone(streamer.spatial_pair)
        self.assertIsNone(streamer.pending_room())
        self.assertTrue(room.playing())
        # The pair was cut at the same content instant, not left playing under
        # the room: two outputs playing the same song is comb filtering.
        self.assertEqual(pair[0].state, cyal.SourceState.STOPPED)
        self.assertEqual(pair[1].state, cyal.SourceState.STOPPED)
        # Every speaker got the whole window the pair was about to play.
        self.assertEqual({source.buffers_queued for source in room.sources}, {7})

    def test_a_deep_pair_queue_is_waited_out_not_dropped(self):
        game, streamer, map_obj = self.build_streamer()
        pair = self.build_pair(game)
        streamer.switch_to_pair((pair[0], pair[1], 8.0, 40.0))
        for tag in range(1, 13):
            self.assertTrue(streamer._queue_local(frame(tag)))
        room = host_for(game).acquire_bank("j1", ANCHOR, profile="front_only",
                                           volume=80)
        streamer.request_room(room)
        self.assertGreater(streamer.pair_queued_frames(), room.prime_frames())

        # The frame is held (nothing is queued into a room that cannot start
        # level with the pair), and the song keeps playing out of the pair.
        self.assertFalse(streamer._queue_local(frame(13)))
        self.assertIsNone(streamer.cinema)
        self.assertIsNone(streamer.pending_room() if streamer.swap_room_failed else None)
        self.assertFalse(streamer.swap_room_failed)

        # The queue comes down as the pair plays; the swap commits then.
        for source in pair:
            source.buffers = source.buffers[:-7]
        self.assertLessEqual(streamer.pair_queued_frames(), room.prime_frames())
        self.assertTrue(streamer._queue_local(frame(14)))
        self.assertIs(streamer.cinema, room)

    def test_a_stream_that_never_fed_refuses_the_room(self):
        game, streamer, map_obj = self.build_streamer()
        pair = self.build_pair(game)
        streamer.switch_to_pair((pair[0], pair[1], 8.0, 40.0))
        room = host_for(game).acquire_bank("j1", ANCHOR, profile="front_only",
                                           volume=80)
        streamer.request_room(room)

        self.assertTrue(streamer._queue_local(frame(1)))

        self.assertTrue(streamer.swap_room_failed)
        self.assertIsNone(streamer.pending_room())
        self.assertIsNone(streamer.cinema)
        self.assertIsNotNone(streamer.spatial_pair)

    def test_a_room_handed_over_stops_the_pair_it_replaced(self):
        game, streamer, map_obj = self.build_streamer()
        pair = self.build_pair(game)
        streamer.switch_to_pair((pair[0], pair[1], 8.0, 40.0))
        for tag in range(1, 5):
            streamer._queue_local(frame(tag))
        room = host_for(game).acquire_bank("j1", ANCHOR, profile="front_only",
                                           volume=80)
        # What ``jukebox._hand_song_to_pair`` does with the other direction:
        # the room gives its pair up and the stream plays through it.
        room_frame = room.queue_frame(frame(1), frame(2))
        self.assertTrue(room_frame)
        streamer.cinema = room
        streamer.spatial_pair = None
        detach = room.detach_primary_pair()
        self.assertIsNotNone(detach)
        before = detach[0].buffers_queued
        streamer.switch_to_pair((detach[0], detach[1], 8.0, 40.0))

        self.assertTrue(streamer._queue_local(frame(9)))

        self.assertIsNone(streamer.cinema)
        self.assertEqual(streamer.spatial_pair[0], detach[0])
        # The pair is fed again, on top of the frames the room was about to
        # play: the song has no gap to cross at all.
        self.assertEqual(detach[0].buffers_queued, before + 1)


if __name__ == "__main__":
    unittest.main()
