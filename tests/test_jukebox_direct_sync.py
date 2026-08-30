"""Offline mock-server tests for direct jukebox timeline synchronization.

A fake server broadcasts jukebox_play events on a simulated monotonic
clock exactly the way the server's jukeboxPlayEvent does (start_offset
derived from audioStartedAt). Every MockDirectClient then applies its own
delivery latency, yt-dlp resolve time and ffmpeg startup, and follows
AudioStreamer's real alignment math to decide when its audible playback
starts and at which content position.

The invariant under test: at any wall instant, every anchored listener in
the room hears the same content position — per-machine resolve variance
must become wait time, never audible skew.

No game, OpenAL, ffmpeg, or network is required.
"""

import os
import sys
import time
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from libs.music_bot import AudioStreamer
from libs import jukebox


class MockJukeboxServer:
    """Broadcasts jukebox_play like the server's world_map.jukeboxPlayEvent.

    audio_started_at is the server wall-clock instant the room's song
    position was 0 (Date.now() at direct broadcast; backdated on resume),
    and the broadcast start_offset is derived from it at send time.
    """

    def __init__(self):
        self.now = 1000.0
        self.audio_started_at = None

    def advance(self, seconds):
        self.now += seconds

    def broadcast_play(self, resumed_offset=0.0):
        """Server sends jukebox_play; returns the start_offset it carries."""
        if resumed_offset > 0.0:
            self.audio_started_at = self.now - resumed_offset
        else:
            self.audio_started_at = self.now
        return max(0.0, self.now - self.audio_started_at)


class MockDirectClient:
    """One listener's direct-transport timeline, driven by the real math.

    Models the per-machine variance that used to desync the room: network
    delivery latency, yt-dlp resolve time and ffmpeg startup. Applies
    AudioStreamer's pure alignment decisions exactly where run() and
    _build_cmd apply them.
    """

    def __init__(self, *, latency=0.05, resolve=2.0, startup=0.8, anchored=True):
        self.latency = latency
        self.resolve = resolve
        self.startup = startup
        self.anchored = anchored

    def receive(self, server, start_offset):
        self.received_at = server.now + self.latency
        self.start_offset = start_offset
        # _build_cmd runs after the local resolve completes:
        spawn_at = self.received_at + self.resolve
        if self.anchored and self.start_offset > AudioStreamer.DIRECT_FRESH_MAX_S:
            seek = AudioStreamer.direct_seek_seconds(
                self.start_offset, self.received_at, spawn_at)
            self.seek_to = seek if seek > 0.5 else 0.0
        elif not self.anchored and self.start_offset > 0.0:
            # Legacy unanchored compensation (music-bot / livestream path).
            self.seek_to = self.start_offset + (spawn_at - self.received_at)
        else:
            self.seek_to = 0.0
        # Prebuffer completes one startup after spawn; the hold then aligns
        # the audible start to the shared deadline.
        ready_at = spawn_at + self.startup
        if self.anchored:
            deadline = AudioStreamer.direct_start_deadline(
                self.start_offset, self.received_at, self.seek_to)
            hold = min(AudioStreamer.DIRECT_MAX_ALIGN_WAIT_S,
                       max(0.0, deadline - ready_at))
        else:
            hold = 0.0
        self.audible_at = ready_at + hold
        self.audible_position = self.seek_to
        # Wall instant this client hears content position 0. Two clients
        # are synchronized with each other iff these agree.
        self.zero_heard_at = self.audible_at - self.audible_position
        return self


def _spread(values):
    values = list(values)
    return max(values) - min(values)


class TestFreshSongRoomSync(unittest.TestCase):
    def test_room_of_five_listeners_converges(self):
        server = MockJukeboxServer()
        start_offset = server.broadcast_play()
        clients = [
            MockDirectClient(latency=0.02, resolve=1.0, startup=0.4),
            MockDirectClient(latency=0.08, resolve=2.2, startup=0.9),
            MockDirectClient(latency=0.12, resolve=2.3, startup=1.0),
            MockDirectClient(latency=0.05, resolve=2.0, startup=1.3),
            MockDirectClient(latency=0.09, resolve=2.6, startup=0.7),
        ]
        for client in clients:
            client.receive(server, start_offset)
        # Everyone still hears the true intro...
        for client in clients:
            self.assertEqual(client.audible_position, 0.0)
        # ...and everyone's content-0 lands on the same wall instant,
        # within the room's network delivery jitter.
        self.assertLessEqual(_spread(c.zero_heard_at for c in clients), 0.25)
        # The intro is heard one shared lead-in after the server's zero,
        # never before it.
        for client in clients:
            self.assertGreaterEqual(
                client.zero_heard_at,
                server.audio_started_at + AudioStreamer.DIRECT_LEAD_IN_S - 0.001,
            )

    def test_resolve_variance_becomes_wait_not_skew(self):
        server = MockJukeboxServer()
        start_offset = server.broadcast_play()
        fast = MockDirectClient(latency=0.05, resolve=1.0, startup=0.5).receive(server, start_offset)
        slow = MockDirectClient(latency=0.05, resolve=2.7, startup=0.7).receive(server, start_offset)
        self.assertAlmostEqual(fast.zero_heard_at, slow.zero_heard_at, places=6)

    def test_unanchored_clients_desync(self):
        """Documents the bug the anchoring fixes (pre-fix behavior)."""
        server = MockJukeboxServer()
        start_offset = server.broadcast_play()
        fast = MockDirectClient(latency=0.05, resolve=1.0, startup=0.5, anchored=False).receive(server, start_offset)
        slow = MockDirectClient(latency=0.05, resolve=4.6, startup=1.2, anchored=False).receive(server, start_offset)
        self.assertGreater(_spread([fast.zero_heard_at, slow.zero_heard_at]), 2.0)


class TestMidSongJoin(unittest.TestCase):
    def test_late_joiner_lands_on_the_rooms_clock(self):
        server = MockJukeboxServer()
        start_offset = server.broadcast_play()
        room = MockDirectClient(latency=0.06, resolve=2.0, startup=0.8).receive(server, start_offset)
        # 240s later the server re-broadcasts (late join / map reload /
        # relay-to-direct switch) with the elapsed position.
        server.advance(240.0)
        rejoined_offset = server.broadcast_play(resumed_offset=240.0)
        joiner = MockDirectClient(latency=0.10, resolve=3.2, startup=1.1).receive(server, rejoined_offset)
        self.assertLessEqual(_spread([room.zero_heard_at, joiner.zero_heard_at]), 0.25)

    def test_joiners_with_different_variance_agree(self):
        server = MockJukeboxServer()
        server.broadcast_play()
        server.advance(200.0)
        offset = server.broadcast_play(resumed_offset=200.0)
        joiners = [
            MockDirectClient(latency=0.03, resolve=1.2, startup=0.5),
            MockDirectClient(latency=0.11, resolve=3.0, startup=1.3),
            MockDirectClient(latency=0.07, resolve=4.4, startup=1.5),
        ]
        for joiner in joiners:
            joiner.receive(server, offset)
        self.assertLessEqual(_spread(j.zero_heard_at for j in joiners), 0.25)
        # A mid-song join never rewinds into already-heard content beyond
        # the aim-ahead allowance.
        for joiner in joiners:
            self.assertGreaterEqual(joiner.audible_position, 200.0)

    def test_slow_client_joins_late_but_deterministically(self):
        """A client slower than the lead-in cannot catch the deadline (PCM
        skipping costs real time under '-re'), but its lateness is exact
        and bounded — it joins the shared timeline content-consistently."""
        server = MockJukeboxServer()
        start_offset = server.broadcast_play()
        client = MockDirectClient(latency=0.05, resolve=8.0, startup=1.5).receive(server, start_offset)
        expected_late = (client.latency + client.resolve + client.startup
                         - AudioStreamer.DIRECT_LEAD_IN_S)
        late = client.audible_at - (server.audio_started_at + AudioStreamer.DIRECT_LEAD_IN_S)
        self.assertAlmostEqual(late, expected_late, places=6)
        self.assertGreater(late, 0.0)
        self.assertEqual(client.audible_position, 0.0)


class TestEmergencySwitchMath(unittest.TestCase):
    """_switch_to_direct re-plays with start_offset advanced by the elapsed
    wall time; the re-derived deadline must equal the original one (no
    double compensation)."""

    def test_fresh_song_switch_preserves_deadline(self):
        original_deadline = AudioStreamer.direct_start_deadline(0.0, 1000.05, 0.0)
        now = 1003.0
        offset = 0.0 + (now - 1000.05)
        switched_deadline = AudioStreamer.direct_start_deadline(offset, now, 0.0)
        self.assertEqual(original_deadline, switched_deadline)

    def test_mid_song_switch_preserves_deadline(self):
        original_deadline = AudioStreamer.direct_start_deadline(180.0, 1000.05, 0.0)
        now = 1004.2
        offset = 180.0 + (now - 1000.05)
        switched_deadline = AudioStreamer.direct_start_deadline(offset, now, 0.0)
        self.assertEqual(original_deadline, switched_deadline)


class TestAnchorGate(unittest.TestCase):
    def _streamer(self, **kwargs):
        return AudioStreamer(
            SimpleNamespace(), "https://youtu.be/x", None,
            bot=kwargs.get("bot"), start_offset=kwargs.get("start_offset", 0.0),
            start_offset_received_at=kwargs.get("start_offset_received_at"),
            timeline_anchor=kwargs.get("timeline_anchor"),
        )

    def test_jukebox_direct_anchors(self):
        streamer = self._streamer(start_offset_received_at=1234.5)
        self.assertTrue(streamer._direct_anchor)

    def test_personal_music_bot_never_anchors(self):
        streamer = self._streamer(
            bot=SimpleNamespace(), start_offset_received_at=1234.5)
        self.assertFalse(streamer._direct_anchor)

    def test_livestream_never_anchors(self):
        streamer = self._streamer(
            start_offset_received_at=1234.5, timeline_anchor=False)
        self.assertFalse(streamer._direct_anchor)

    def test_missing_receipt_timestamp_never_anchors(self):
        streamer = self._streamer()
        self.assertFalse(streamer._direct_anchor)

    def test_alignment_constants_stay_self_consistent(self):
        # The hold clamp must never bite a healthy fresh-song hold (which
        # can legitimately be up to the full lead-in).
        self.assertGreaterEqual(
            AudioStreamer.DIRECT_MAX_ALIGN_WAIT_S, AudioStreamer.DIRECT_LEAD_IN_S)
        self.assertGreater(AudioStreamer.DIRECT_STARTUP_EST_S, 0.0)
        self.assertGreater(AudioStreamer.DIRECT_LEAD_IN_S, 0.0)
        # The fresh-broadcast window must sit inside the lead-in: a song
        # younger than the lead-in has not been heard by the room yet.
        self.assertLess(AudioStreamer.DIRECT_FRESH_MAX_S, AudioStreamer.DIRECT_LEAD_IN_S)


class TestHoldLoop(unittest.TestCase):
    def _streamer_with_pipe(self, start_offset, received_at, chunks):
        streamer = AudioStreamer(
            SimpleNamespace(), "https://youtu.be/x", None,
            start_offset=start_offset, start_offset_received_at=received_at)
        pipe = SimpleNamespace(read1=lambda n: chunks.pop(0) if chunks else b"")
        exited = {"value": False}
        streamer.process = SimpleNamespace(
            stdout=pipe, poll=lambda: 0 if not chunks else None)
        return streamer

    def test_past_deadline_returns_leftover_unchanged(self):
        streamer = self._streamer_with_pipe(
            0.0, time.monotonic() - 60.0, [b"abc"])
        streamer._direct_seek_to = 0.0
        self.assertEqual(streamer._hold_direct_start(b"abc"), b"abc")
        self.assertEqual(len(streamer._pause_buffer), 0)

    def test_hold_drains_pipe_into_pause_buffer(self):
        received_at = time.monotonic()
        size = AudioStreamer.BUFFER_SIZE
        chunks = [b"x" * size, b"y" * size, b"z" * size]
        streamer = self._streamer_with_pipe(0.0, received_at, chunks)
        streamer._direct_seek_to = 0.0
        with mock.patch.object(AudioStreamer, "DIRECT_LEAD_IN_S", 0.15):
            started = time.monotonic()
            leftover = streamer._hold_direct_start(b"")
            elapsed = time.monotonic() - started
        self.assertLess(elapsed, 5.0)
        self.assertGreaterEqual(len(streamer._pause_buffer), 2)
        self.assertLess(len(leftover), AudioStreamer.BUFFER_SIZE)

    def test_content_position_tracks_seek_and_fed_bytes(self):
        streamer = AudioStreamer(
            SimpleNamespace(), "https://youtu.be/x", None,
            start_offset=200.0, start_offset_received_at=time.monotonic())
        streamer._direct_seek_to = 201.5
        streamer._note_fed_content(b"\0" * AudioStreamer.BUFFER_SIZE)
        # One stereo 48kHz buffer is exactly 20ms of content.
        self.assertAlmostEqual(streamer.content_position(), 201.52, places=6)


class TestDirectRetire(unittest.TestCase):
    def _player_with_entry(self, duration, position, alive=True):
        player = jukebox.JukeboxPlayer(SimpleNamespace())
        stopped = []
        streamer = SimpleNamespace(
            is_alive=lambda: alive,
            failure_reason=None,
            content_position=lambda: position,
            reverb_slot=object(),
            stop=lambda: stopped.append(True),
        )
        source_l, source_r = SimpleNamespace(name="l"), SimpleNamespace(name="r")
        player.players["box"] = {
            "transport": "direct",
            "streamer": streamer,
            "source": source_l,
            "secondary_source": source_r,
            "title": "Song",
            "playback_key": ("id", 7),
            "play_params": {"duration": duration},
        }
        return player, streamer, source_l, source_r, stopped

    def test_short_tail_retires_instead_of_stopping(self):
        player, streamer, source_l, source_r, stopped = self._player_with_entry(
            duration=210.0, position=204.0)
        player._retire_or_stop("box")
        self.assertEqual(stopped, [])
        self.assertNotIn("box", player.players)
        self.assertEqual(len(player._retiring_direct), 1)
        entry = player._retiring_direct[0]
        self.assertIs(entry["streamer"], streamer)
        self.assertIsNone(streamer.reverb_slot)
        self.assertGreater(entry["deadline"], time.monotonic())

    def test_long_tail_falls_back_to_faded_stop(self):
        player, streamer, *_ = self._player_with_entry(
            duration=210.0, position=100.0)
        calls = []
        player.stop = lambda jukebox_id, fade=False: calls.append((jukebox_id, fade)) or True
        player._retire_or_stop("box")
        self.assertEqual(calls, [("box", True)])
        self.assertEqual(player._retiring_direct, [])

    def test_dead_streamer_falls_back_to_stop(self):
        player, *_ = self._player_with_entry(
            duration=210.0, position=204.0, alive=False)
        calls = []
        player.stop = lambda jukebox_id, fade=False: calls.append((jukebox_id, fade)) or True
        player._retire_or_stop("box")
        self.assertEqual(calls, [("box", True)])

    def test_sweep_releases_sources_when_tail_drains(self):
        player, streamer, source_l, source_r, stopped = self._player_with_entry(
            duration=210.0, position=209.0)
        player._retire_or_stop("box")
        released = []
        player._release_sources = lambda sources: released.append(sources)
        player._sweep_retiring_direct()
        # Still draining: nothing released yet.
        self.assertEqual(released, [])
        # The streamer thread exits once its buffers play out.
        streamer.is_alive = lambda: False
        player._sweep_retiring_direct()
        self.assertEqual(stopped, [True])
        self.assertEqual(released, [[source_l, source_r]])
        self.assertEqual(player._retiring_direct, [])

    def test_sweep_deadline_expiry_stops_a_stuck_tail(self):
        player, streamer, source_l, source_r, stopped = self._player_with_entry(
            duration=210.0, position=209.0)
        player._retire_or_stop("box")
        player._retiring_direct[0]["deadline"] = time.monotonic() - 0.1
        released = []
        player._release_sources = lambda sources: released.append(sources)
        player._sweep_retiring_direct()
        self.assertEqual(stopped, [True])
        self.assertEqual(len(released), 1)
        self.assertEqual(player._retiring_direct, [])

    def test_stop_all_hard_stops_retired_tails(self):
        player, streamer, source_l, source_r, stopped = self._player_with_entry(
            duration=210.0, position=209.0)
        player._retire_or_stop("box")
        released = []
        player._release_sources = lambda sources: released.append(sources)
        player.stop_all()
        self.assertEqual(stopped, [True])
        self.assertEqual(released, [[source_l, source_r]])
        self.assertEqual(player._retiring_direct, [])


if __name__ == "__main__":
    unittest.main()
