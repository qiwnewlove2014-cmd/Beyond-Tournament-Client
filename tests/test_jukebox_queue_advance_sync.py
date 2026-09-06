"""Queue-advance jam-sync regression tests.

The reported bug: song 1 jams perfectly, but after the jukebox queue
advances to song 2, everyone's instrument notes land several seconds behind
the beat until the queue is reset.

Root cause (proven by the room model below): when ONE machine plays the song
via the client-side direct fallback (watchdog switch, sticky 10 min) while
the rest of the room hears the server relay, the direct anchor math holds a
full DIRECT_LEAD_IN_S before starting — a lead-in the relay room never
holds. The fallback machine's song trails the room by exactly one lead-in
for the WHOLE song, and its own anchor measures no lateness (it started on
time *by its own deadline*), so the sender-lag report cannot compensate:
its jam notes land ~3.5s behind the beat for everyone.

Fix: the per-listener direct fallback anchors with room_lead_in_s=0.0 — the
seek already aims past the projected audible start (DIRECT_STARTUP_EST_S),
and any resolve/startup overrun beyond it is measured by direct_late_s and
reported as sender lag, so the room hears the fallback machine on the beat.

These tests drive the REAL production math: AudioStreamer.direct_start_deadline
/ direct_seek_seconds and EventHandeler._schedule_remote_note with the real
_active_jukebox_buffer_ms formulas (fake OpenAL).
"""

import os
import sys
import threading
import time
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from libs.event_handeler import EventHandeler
from libs.jukebox import JukeboxPlayer, JukeboxRelayReceiver
from libs.music_bot import AudioStreamer
from libs.audio_diagnostics import probe as audio_probe


class _FakeGame:
    def __init__(self):
        self.after_calls = []

    def call_after(self, ms, callback):
        self.after_calls.append((ms, callback))


class _FakeSrc:
    pass


class _FakeAudio:
    def __init__(self):
        self.context = SimpleNamespace(gen_source=lambda: _FakeSrc())
        self.filter = [None]
        self.position = None
        self.efx = None
        self.volume_categories = {"jukebox": [100]}


class _FakeGameWithAudio:
    def __init__(self):
        self.after_calls = []
        self.audio_mngr = _FakeAudio()
        self.gameplay = None


class Machine:
    """One machine in the jukebox room.

    relay=True: hears the server relay (audible ~immediately after the ready
    broadcast; OpenAL backlog = queue_frames * 40ms). relay=False: anchored
    direct playback driven by the REAL alignment math; lead_in overrides the
    room lead-in the anchor holds (0.0 for the fixed per-listener fallback).
    """

    def __init__(self, name, latency=0.05, resolve=2.0, startup=0.8,
                 relay=False, queue_frames=10, lead_in=None, join=False):
        self.name = name
        self.latency = latency
        self.resolve = resolve
        self.startup = startup
        self.relay = relay
        self.queue_frames = queue_frames
        self.lead_in = lead_in
        # True = per-listener direct fallback into an already-playing room:
        # the stream must seek (join) instead of starting from position 0.
        self.join = join
        self.audible_at = None
        self.seek_to = 0.0
        self.late_s = 0.0
        self.play_started = False

    def receive_play(self, server_now, start_offset):
        received_at = server_now + self.latency
        if self.relay:
            self.audible_at = received_at + 0.05
            self.seek_to = 0.0
            self.late_s = 0.0
            self.play_started = True
            return
        spawn_at = received_at + self.resolve
        if self.join:
            # Joining a playing room: always seek (even offset 0). The seek
            # formula cancels the offset — the machine lands on the room's
            # clock, position = room position + startup estimate at the
            # audible start.
            seek = AudioStreamer.direct_seek_seconds(
                max(0.001, start_offset), received_at, spawn_at)
            self.seek_to = seek if seek > 0.5 else 0.0
        elif start_offset > AudioStreamer.DIRECT_FRESH_MAX_S:
            seek = AudioStreamer.direct_seek_seconds(
                start_offset, received_at, spawn_at)
            self.seek_to = seek if seek > 0.5 else 0.0
        else:
            self.seek_to = 0.0
        ready_at = spawn_at + self.startup
        deadline = AudioStreamer.direct_start_deadline(
            start_offset, received_at, self.seek_to, lead_in=self.lead_in)
        hold = min(AudioStreamer.DIRECT_MAX_ALIGN_WAIT_S,
                   max(0.0, deadline - ready_at))
        self.audible_at = ready_at + hold
        self.late_s = max(0.0, self.audible_at - deadline)
        self.play_started = True

    def hear_time(self, content_position):
        return self.audible_at + (content_position - self.seek_to)

    def buffer_ms(self):
        if not self.play_started:
            return None
        if self.relay:
            return max(self.queue_frames, 1) * 40
        return 5 * 20 + int(max(0.0, self.late_s) * 1000)


def _fake_streamer(machine):
    ready = threading.Event()
    if machine.play_started:
        ready.set()
    if machine.relay:
        cls = type("FakeReceiver", (JukeboxRelayReceiver,), {})
        streamer = cls.__new__(cls)
        streamer.running = True
        streamer._play_started = machine.play_started
        streamer.source_l = SimpleNamespace(
            buffers_queued=machine.queue_frames)
        return streamer
    return SimpleNamespace(
        _direct_anchor=True,
        running=True,
        ready_event=ready,
        direct_late_s=machine.late_s,
        spatial_src_l=SimpleNamespace(buffers_queued=5),
        source=SimpleNamespace(buffers_queued=5),
    )


def _note_error(performer, listener, beat):
    """Song-position error (seconds) of a remote note at the listener.

    Positive = the note lands late relative to the listener's heard beat.
    Runs the REAL _schedule_remote_note with the REAL measurement formula
    (fake OpenAL) and the sender's lag attached exactly like the production
    sender (lag > 0 only).
    """
    t_press = performer.hear_time(beat)
    t_stamp = t_press + performer.latency
    t_arrive = t_stamp + listener.latency

    handler = EventHandeler.__new__(EventHandeler)
    handler.gameplay = SimpleNamespace(jukebox_player=SimpleNamespace(
        players={"box": {"transport": "relay" if listener.relay else "direct",
                         "streamer": _fake_streamer(listener)}}))
    game = _FakeGame()
    handler.game = game
    handler._clock_offset_ms = 0.0
    handler._clock_offset_samples = 10
    handler._last_jam_sync_log = 0.0

    enqueue = []
    packet = {"server_time": t_stamp * 1000.0}
    lag = performer.buffer_ms()
    if lag is not None and lag > 0:
        packet["sender_lag_ms"] = int(lag)

    with mock.patch.object(EventHandeler, "_active_jukebox_buffer_ms",
                           return_value=listener.buffer_ms()), \
            mock.patch("libs.event_handeler.time.time",
                       return_value=t_arrive), \
            mock.patch("libs.logger.log"):
        handler._schedule_remote_note(packet, lambda: enqueue.append(1))

    if enqueue or not game.after_calls:
        play_at = t_arrive
    else:
        play_at = t_arrive + game.after_calls[0][0] / 1000.0
    return play_at - listener.hear_time(beat)


class TestMixedRoomFallbackAnchor(unittest.TestCase):
    BEAT = 60.0

    def _mixed_room(self, fallback_resolve=2.0, fallback_startup=0.8,
                    lead_in=None, join=False):
        server_now = 1000.0
        host = Machine("host", latency=0.03, relay=True)
        fallback = Machine("fallback", latency=0.06, relay=False,
                           resolve=fallback_resolve, startup=fallback_startup,
                           lead_in=lead_in, join=join)
        host.receive_play(server_now, 0.0)
        fallback.receive_play(server_now, 0.0)
        return host, fallback

    def test_lead_in_hold_trails_the_relay_room_by_a_lead_in(self):
        # The pre-fix behavior: the fallback machine anchors with the full
        # DIRECT_LEAD_IN_S hold, so its song trails the relay room by ~one
        # lead-in and the room hears its notes seconds off the beat.
        host, fallback = self._mixed_room(lead_in=AudioStreamer.DIRECT_LEAD_IN_S)
        error = _note_error(fallback, host, self.BEAT)
        self.assertGreater(error, 3.0)
        self.assertLess(error, AudioStreamer.DIRECT_LEAD_IN_S + 1.0)

    def test_zero_lead_in_lands_the_fallback_machine_on_the_room_beat(self):
        # The fix: anchor with room_lead_in_s=0.0 AND join the playing room
        # (seek) — the seek lands the machine on the room's clock even for
        # a fresh-song event whose offset is 0.
        host, fallback = self._mixed_room(lead_in=0.0, join=True)
        error = _note_error(fallback, host, self.BEAT)
        self.assertLess(abs(error), 0.5)

    def test_zero_lead_in_keeps_the_room_notes_on_the_fallback_beat(self):
        # The other direction: the room's notes must not stack the fallback
        # machine's (now-zero) trail either.
        host, fallback = self._mixed_room(lead_in=0.0, join=True)
        error = _note_error(host, fallback, self.BEAT)
        self.assertLess(abs(error), 0.5)

    def test_lead_in_hold_without_join_still_trails_a_fresh_song(self):
        # Regression guard: zero lead-in alone is NOT enough for a fresh
        # event — without the join seek, the machine starts at position 0
        # after its own resolve+startup and trails the room by that startup.
        host, fallback = self._mixed_room(lead_in=0.0, join=False)
        error = _note_error(fallback, host, self.BEAT)
        self.assertGreater(error, 2.0)

    def test_slow_fallback_reports_overrun_as_sender_lag(self):
        # Startup outruns the 4.5s estimate: the fallback machine starts
        # late, but direct_late_s measures the overrun and the sender lag
        # report stops the listeners stacking their own hold on top. The
        # residual error is the machine's own overrun (physics: the note is
        # struck late and arrives after the beat).
        host, fallback = self._mixed_room(fallback_resolve=2.0,
                                          fallback_startup=6.5,
                                          lead_in=0.0, join=True)
        self.assertGreater(fallback.late_s, 1.5)
        error = _note_error(fallback, host, self.BEAT)
        self.assertGreater(error, fallback.late_s - 0.40)
        self.assertLess(error, fallback.late_s + 0.50)

    def test_relay_room_queue_advance_stays_synced(self):
        # Baseline: a uniform relay room (the normal queue-advance case) has
        # no skew at all — the bug only appears with a mixed transport room.
        server_now = 1000.0
        host = Machine("host", latency=0.03, relay=True)
        guest = Machine("guest", latency=0.10, relay=True)
        host.receive_play(server_now, 0.0)
        guest.receive_play(server_now, 0.0)
        error = _note_error(host, guest, self.BEAT)
        self.assertLess(abs(error), 0.3)


class TestDirectFallbackPlumbing(unittest.TestCase):
    def _player_with_audio(self):
        return JukeboxPlayer(_FakeGameWithAudio())

    def _capture_direct_create(self, player):
        captured = {}
        real_call = audio_probe.call

        def fake_call(fn_name, *args, **kwargs):
            if fn_name == "jukebox.direct_create":
                captured["kwargs"] = kwargs
                return SimpleNamespace(
                    main_thread_audio=False,
                    start=lambda: None,
                    set_cabinet_volume=lambda value: None,
                )
            if fn_name == "jukebox.gen_source":
                return _FakeSrc()
            if fn_name == "jukebox.thread_start":
                return None
            return real_call(fn_name, *args, **kwargs)

        patch = mock.patch.object(audio_probe, "call", side_effect=fake_call)
        patch.start()
        self.addCleanup(patch.stop)
        return captured

    def test_sticky_fallback_play_anchors_with_zero_lead_in_and_join(self):
        # The sticky per-listener fallback converts a relay event to direct
        # while the rest of the room still hears the relay: it must anchor
        # with room_lead_in_s=0.0 (never the room's direct lead-in) AND join
        # the already-playing room via a seek.
        player = self._player_with_audio()
        player._direct_fallback_until["box"] = time.monotonic() + 600.0
        captured = self._capture_direct_create(player)
        player.play("box", 0, 0, 0, "Song", "http://example.com/x", 120,
                    transport="relay", relay_id=1, stream_epoch=1,
                    playback_id=7, received_at=time.monotonic() - 1.0)
        self.assertEqual(captured["kwargs"].get("room_lead_in_s"), 0.0)
        self.assertTrue(captured["kwargs"].get("join_playing_room"))
        # The anchor uses the network-arrival instant, not the deferred
        # main-thread time.
        self.assertAlmostEqual(
            captured["kwargs"].get("start_offset_received_at"),
            time.monotonic() - 1.0, delta=0.2)

    def test_sticky_fallback_play_without_received_at_still_anchors(self):
        # Backward compatibility: callers that do not pass received_at fall
        # back to the deferred stamp (the pre-fix behavior for the anchor).
        player = self._player_with_audio()
        player._direct_fallback_until["box"] = time.monotonic() + 600.0
        captured = self._capture_direct_create(player)
        player.play("box", 0, 0, 0, "Song", "http://example.com/x", 120,
                    transport="relay", relay_id=1, stream_epoch=1,
                    playback_id=7)
        self.assertEqual(captured["kwargs"].get("room_lead_in_s"), 0.0)
        self.assertTrue(captured["kwargs"].get("join_playing_room"))
        self.assertIsNotNone(captured["kwargs"].get("start_offset_received_at"))

    def test_normal_relay_play_keeps_default_lead_in(self):
        # A healthy relay event does not anchor direct at all; a normal
        # server-sent direct event keeps the default room lead-in.
        player = self._player_with_audio()
        captured = self._capture_direct_create(player)
        player.play("box", 0, 0, 0, "Song", "http://example.com/x", 120,
                    transport="direct", playback_id=8)
        self.assertIsNone(captured["kwargs"].get("room_lead_in_s"))

    def test_switch_to_direct_passes_zero_lead_in_and_join(self):
        # The watchdog's per-listener fallback must anchor on the relay
        # room's clock (no lead-in hold) and join the playing room.
        player = JukeboxPlayer.__new__(JukeboxPlayer)
        player._direct_fallback_until = {}
        with mock.patch.object(player, "play") as play_mock:
            player._switch_to_direct("box", {
                "x": 0.0, "y": 0.0, "z": 0.0, "title": "Song",
                "url": "http://example.com/x", "duration": 120,
                "start_offset": 0.0,
                "received_at": time.monotonic() - 3.0,
                "http_headers": None,
            })
        self.assertEqual(play_mock.call_args.kwargs.get("room_lead_in_s"), 0.0)
        self.assertTrue(play_mock.call_args.kwargs.get("join_playing_room"))
        offset = play_mock.call_args.kwargs.get("start_offset")
        self.assertGreater(offset, 2.0)  # continues at the room position

    def test_deadline_override_matches_room_lead_in(self):
        now = 100.0
        self.assertEqual(
            AudioStreamer.direct_start_deadline(0.0, now, 0.0),
            now + AudioStreamer.DIRECT_LEAD_IN_S)
        self.assertEqual(
            AudioStreamer.direct_start_deadline(0.0, now, 0.0, lead_in=0.0),
            now)
        # Mid-song join (offset=240, seek past the audible start): the room
        # lead-in only applies to a direct room; a relay-room fallback with
        # lead_in=0 starts right when the seek's startup estimate allows.
        seek = AudioStreamer.direct_seek_seconds(240.0, now, now)
        with_room = AudioStreamer.direct_start_deadline(240.0, now, seek)
        without = AudioStreamer.direct_start_deadline(240.0, now, seek,
                                                      lead_in=0.0)
        self.assertGreater(with_room, without)
        self.assertAlmostEqual(without, now + AudioStreamer.DIRECT_STARTUP_EST_S,
                               places=6)


if __name__ == "__main__":
    unittest.main()