"""The watchdog's blind spot: a cabinet the Server plays that this client does not.

Reported: the queue moved on to the next song, the cabinet said it was playing,
and nothing came out -- until the cabinet menu was opened (which re-offers the
play event, now from the middle of the track) or the song was stopped and queued
again. Nothing in the scan could see it: every phase asked an *entry* how it was,
and a cabinet whose play event was missed, or whose entry a rebuild had removed,
had no entry at all. The recovery ladder could not help either, because
``request_resync`` refused to ask the Server whenever this client knew of no
output -- which is exactly that state.

The other half is the same complaint heard differently: a cinema room that ends a
refill hold re-forms (realign, then _play_ready) before it is audible again, and
for a frame or two every speaker is stopped with audio queued. One snapshot of
that was read as a dead room and rebuilt, which cut the song and restarted it at
the room's own position -- "it starts in the middle of the song".

These drive the real watchdog with fake OpenAL sources and a fake network.
"""

import sys
import os
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import cyal

from libs import jukebox


class FakeSource:
    def __init__(self):
        self.state = cyal.SourceState.STOPPED
        self.buffers = []
        self.buffers_processed = 0
        self.position = None
        self.rolloff_factor = None
        self.reference_distance = None
        self.max_distance = None
        self.spatialize = None
        self.direct_channels = None
        self.direct_filter = None
        self.gain = 0.0

    @property
    def buffers_queued(self):
        return len(self.buffers)

    def queue_buffers(self, buffer):
        self.buffers.append(buffer)

    def unqueue_buffers(self):
        if self.buffers_processed <= 0 or not self.buffers:
            return None
        self.buffers_processed -= 1
        return self.buffers.pop(0)

    def play(self):
        self.state = cyal.SourceState.PLAYING

    def pause(self):
        self.state = cyal.SourceState.PAUSED

    def stop(self):
        self.state = cyal.SourceState.STOPPED
        self.buffers_processed = len(self.buffers)

    def delete(self):
        pass


class FakeContext:
    def gen_source(self, **kwargs):
        return FakeSource()

    def gen_buffer(self):
        return SimpleNamespace(set_data=lambda *a, **k: None)


class FakeAudio:
    def __init__(self):
        self.context = FakeContext()
        self.efx = SimpleNamespace(send=lambda *a, **k: None)
        self.filter = []
        self.position = (0.0, 0.0, 0.0)
        self.volume_categories = {"jukebox": [100], "music": [100]}


class FakeNetwork:
    def __init__(self):
        self.sent = []

    def send(self, *args, **kwargs):
        self.sent.append(args)


class FakeGame:
    def __init__(self, jukeboxes=None):
        self.audio_mngr = FakeAudio()
        self.network = FakeNetwork()
        self.gameplay = SimpleNamespace(
            map=SimpleNamespace(),
            jukebox_state={"jukeboxes": dict(jukeboxes or {})},
        )


def playing_box(**overrides):
    box = {
        "id": "box",
        "x": 10.0, "y": 5.0, "z": 0.0,
        "current": {"title": "Song", "url": "https://youtu.be/abc",
                    "duration": 180, "queuedBy": "someone"},
        "queue": [],
        "paused": False,
        "position": 42.0,
    }
    box.update(overrides)
    return box


class FakeRelayStreamer:
    """Just enough of JukeboxRelayReceiver for _relay_fault's questions."""

    failure_reason = None
    _play_started = True
    received_frames = 250

    def __init__(self, now):
        self.now = now
        self.last_packet_at = now
        self.last_audio_activity = now
        self.last_output_at = now

    def is_alive(self):
        return True


class RequestResyncTests(unittest.TestCase):
    def test_a_resync_is_allowed_when_the_server_is_playing_and_we_are_not(self):
        game = FakeGame({"box": playing_box()})
        player = jukebox.JukeboxPlayer(game)

        self.assertTrue(player.request_resync("test"))
        self.assertEqual(game.network.sent[-1][1], "jukebox_resync")

    def test_a_quiet_map_still_answers_a_ui_close_without_a_round_trip(self):
        game = FakeGame({"box": playing_box(current=None)})
        player = jukebox.JukeboxPlayer(game)

        self.assertFalse(player.request_resync("options closed"))
        self.assertEqual(game.network.sent, [])


class UnheardCabinetTests(unittest.TestCase):
    def setUp(self):
        self.now = [100.0]
        self.time_patch = mock.patch("libs.jukebox.recovery.time.monotonic",
                                     side_effect=lambda: self.now[0])
        self.time_patch.start()
        self.addCleanup(self.time_patch.stop)

    def build(self, box=None):
        game = FakeGame({"box": box or playing_box()})
        return game, jukebox.JukeboxPlayer(game)

    def tick(self, seconds):
        self.now[0] += seconds
        self.player.update()

    def test_a_missed_song_is_asked_for_once_the_grace_passes(self):
        game, self.player = self.build()
        self.tick(0.0)
        self.assertEqual(game.network.sent, [])

        self.tick(self.player.UNHEARD_RESYNC_AFTER + 0.5)
        self.assertEqual([sent[1] for sent in game.network.sent],
                         ["jukebox_resync"])

        # One request, not one per frame.
        self.tick(0.1)
        self.assertEqual(len(game.network.sent), 1)

    def test_a_dated_needle_is_where_the_song_is_joined(self):
        read_at = self.now[0] - 10.0
        game, self.player = self.build(playing_box(
            position=42.0, position_at=read_at))
        self.tick(0.0)
        with mock.patch("libs.music_bot.AudioStreamer") as streamer:
            self.tick(self.player.UNHEARD_DIRECT_AFTER + 0.5)

        entry = self.player.players.get("box")
        self.assertIsNotNone(entry, "the cabinet was never adopted")
        self.assertEqual(entry["transport"], "direct")
        called = streamer.call_args.kwargs
        # The room kept playing between the payload being read and the adoption.
        self.assertAlmostEqual(called["start_offset"],
                               42.0 + (self.now[0] - read_at), places=3)
        self.assertEqual(called["canonical_url"], "https://youtu.be/abc")
        self.assertTrue(called["join_playing_room"])

    def test_an_undated_needle_plays_the_song_from_its_intro(self):
        # No stamp: the number may have been the previous song's. Joining there
        # is how a fresh song started in the middle, so it plays from the top.
        game, self.player = self.build(playing_box(position=152.0))
        self.tick(0.0)
        with mock.patch("libs.music_bot.AudioStreamer") as streamer:
            self.tick(self.player.UNHEARD_DIRECT_AFTER + 0.5)

        called = streamer.call_args.kwargs
        self.assertAlmostEqual(called["start_offset"], 0.0, places=3)
        self.assertFalse(called["join_playing_room"])

    def test_adopting_a_song_is_not_a_sticky_direct_fallback(self):
        game, self.player = self.build()
        self.tick(0.0)
        with mock.patch("libs.music_bot.AudioStreamer"):
            self.tick(self.player.UNHEARD_DIRECT_AFTER + 0.5)

        # The next relay offer must still be able to bring this cabinet back
        # onto the room's stream: only a cabinet whose own relay keeps failing
        # earns the ten-minute direct window.
        self.assertNotIn("box", self.player._direct_fallback_until)

    def test_a_paused_or_finished_cabinet_is_left_alone(self):
        game, self.player = self.build(playing_box(paused=True))
        self.tick(0.0)
        with mock.patch("libs.music_bot.AudioStreamer") as streamer:
            self.tick(self.player.UNHEARD_DIRECT_AFTER + 1.0)

        self.assertEqual(game.network.sent, [])
        self.assertEqual(self.player.players, {})
        streamer.assert_not_called()

    def test_an_entry_of_our_own_stops_the_adoption(self):
        game, self.player = self.build()
        self.tick(0.0)
        self.player.players["box"] = {"title": "Song", "transport": "relay",
                                      "streamer": None, "playback_key": ("id", 1)}
        with mock.patch("libs.music_bot.AudioStreamer") as streamer:
            self.tick(self.player.UNHEARD_DIRECT_AFTER + 1.0)

        self.assertEqual(game.network.sent, [])
        streamer.assert_not_called()


class StoppedRoomTests(unittest.TestCase):
    """A room re-forming is not a dead room; only a persistent stop is."""

    def build(self):
        game = FakeGame()
        player = jukebox.JukeboxPlayer(game)
        return player

    def entry(self, source_l, source_r):
        return {
            "transport": "relay",
            "streamer": self.streamer,
            "source": source_l,
            "secondary_source": source_r,
        }

    def fault(self, now, entry):
        return self.player._relay_fault("box", entry, self.streamer, now, 9.0)

    def test_a_refill_hold_that_has_just_ended_does_not_rebuild_the_room(self):
        self.player = self.build()
        self.streamer = FakeRelayStreamer(100.0)
        source_l, source_r = FakeSource(), FakeSource()
        for source in (source_l, source_r):
            source.queue_buffers(object())
            source.pause()
        entry = self.entry(source_l, source_r)

        # Frame one: every speaker stopped with audio queued (mid re-form).
        rebuilds, _, _ = self.fault(100.0, entry)
        self.assertEqual(rebuilds, [])

        # A tenth of a second later the room plays again: the reading is gone.
        for source in (source_l, source_r):
            source.play()
        rebuilds, _, _ = self.fault(100.1, entry)
        self.assertEqual(rebuilds, [])
        self.assertNotIn("stopped_since", entry)

    def test_a_room_that_stays_stopped_is_rebuilt(self):
        self.player = self.build()
        self.streamer = FakeRelayStreamer(100.0)
        source_l, source_r = FakeSource(), FakeSource()
        for source in (source_l, source_r):
            source.queue_buffers(object())
            source.pause()
        entry = self.entry(source_l, source_r)

        rebuilds, _, _ = self.fault(100.0, entry)
        self.assertEqual(rebuilds, [])
        rebuilds, _, _ = self.fault(
            100.0 + self.player.STOPPED_SOURCES_PERSIST + 0.1, entry)
        self.assertEqual([reason for _, reason in rebuilds],
                         ["sources stopped with queued audio"])

    def test_a_room_holding_on_purpose_never_counts(self):
        self.player = self.build()
        self.streamer = FakeRelayStreamer(100.0)
        source_l, source_r = FakeSource(), FakeSource()
        for source in (source_l, source_r):
            source.queue_buffers(object())
            source.pause()
        entry = self.entry(source_l, source_r)
        entry["cinema"] = SimpleNamespace(awaiting_refill=True, sources=[source_l, source_r])

        for step in (0.0, 1.0, 5.0):
            rebuilds, _, _ = self.fault(100.0 + step, entry)
            self.assertEqual(rebuilds, [])
        self.assertNotIn("stopped_since", entry)


class CachedNeedleTests(unittest.TestCase):
    """The cached needle belongs to the song it was read with."""

    def handler(self):
        from libs.event_handeler import EventHandeler
        handler = EventHandeler.__new__(EventHandeler)
        handler.game = SimpleNamespace(put=lambda cb: None)
        handler.gameplay = SimpleNamespace(jukebox_state={"jukeboxes": {}}, map=None)
        return handler

    def test_a_state_sync_stamps_the_needle_it_carries(self):
        handler = self.handler()
        handler.jukebox_state({"jukeboxes": {"box": playing_box(position=12.0)}})

        box = handler.gameplay.jukebox_state["jukeboxes"]["box"]
        self.assertIn("position_at", box)

    def test_a_queue_update_after_the_song_changed_drops_the_old_needle(self):
        handler = self.handler()
        handler.jukebox_state({"jukeboxes": {"box": playing_box(position=152.0)}})

        handler.jukebox_queue_update({
            "id": "box",
            "current": {"title": "Next", "url": "https://youtu.be/next",
                        "duration": 200, "queuedBy": "someone"},
            "queue": [],
        })

        box = handler.gameplay.jukebox_state["jukeboxes"]["box"]
        self.assertEqual(box["current"]["url"], "https://youtu.be/next")
        self.assertNotIn("position", box)
        self.assertNotIn("position_at", box)


if __name__ == "__main__":
    unittest.main()
