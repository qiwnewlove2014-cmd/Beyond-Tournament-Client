"""Full-room simulation: direct-transport jam notes land on the heard beat.

Models a music jam in DIRECT jukebox mode (no server relay worker): the
server broadcasts one ``jukebox_play``; every machine resolves the YouTube
URL and starts its own local ffmpeg playback, held to the shared wall-clock
deadline by AudioStreamer's real alignment math (lead-in hold + mid-song
seek). Then a performer plays an instrument along with the beat they HEAR,
the server re-stamps the note (``server_time``), and each listener schedules
it through the REAL production path: EventHandeler._schedule_remote_note
with EventHandeler._active_jukebox_buffer_ms reading a fake direct streamer
that carries that listener's own late-start.

Invariant under test: the note becomes audible at (almost) the same song
position the performer played it, for every listener — regardless of ping,
yt-dlp resolve time or ffmpeg startup. The lead-in is held by EVERY machine,
so it cancels out of note timing; holding notes for the whole lead-in (the
pre-fix behavior) put them ~DIRECT_LEAD_IN_S behind the beat.

No game, OpenAL, ffmpeg or network is required.
"""

import contextlib
import os
import sys
import threading
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from libs.event_handeler import EventHandeler
from libs.music_bot import AudioStreamer


class DirectMachine:
    """One direct-transport jukebox machine, driven by the real alignment math.

    latency = one-way network delivery (seconds); resolve = yt-dlp time;
    startup = ffmpeg/prebuffer time. Mirrors the machine model in
    test_jukebox_direct_sync (which proves the MUSIC clock converges).
    """

    def __init__(self, latency=0.05, resolve=2.0, startup=0.8):
        self.latency = latency
        self.resolve = resolve
        self.startup = startup

    def receive(self, server_now, start_offset):
        received_at = server_now + self.latency
        spawn_at = received_at + self.resolve
        if start_offset > AudioStreamer.DIRECT_FRESH_MAX_S:
            seek = AudioStreamer.direct_seek_seconds(
                start_offset, received_at, spawn_at)
            self.seek_to = seek if seek > 0.5 else 0.0
        else:
            self.seek_to = 0.0
        ready_at = spawn_at + self.startup
        deadline = AudioStreamer.direct_start_deadline(
            start_offset, received_at, self.seek_to)
        hold = min(AudioStreamer.DIRECT_MAX_ALIGN_WAIT_S,
                   max(0.0, deadline - ready_at))
        self.audible_at = ready_at + hold
        # Seconds the audible start ran past the shared wall-clock deadline
        # (== what _hold_direct_start now stores as direct_late_s).
        self.late_s = max(0.0, self.audible_at - deadline)
        return self

    def hear_time(self, content_position):
        """Wall instant this machine hears content_position (seconds)."""
        return self.audible_at + (content_position - self.seek_to)


def _fake_direct_streamer(late_s, queued=5):
    ready = threading.Event()
    ready.set()
    return SimpleNamespace(
        _direct_anchor=True,
        running=True,
        ready_event=ready,
        direct_late_s=late_s,
        spatial_src_l=SimpleNamespace(buffers_queued=queued),
        source=SimpleNamespace(buffers_queued=queued),
    )


def _note_error(performer, listener, beat, buffer_override=None):
    """Song-position error (seconds) of a remote note at the listener.

    Performer presses the note at the beat they HEAR; the server re-stamps
    it after performer.latency; the listener schedules it through the real
    _schedule_remote_note. Returns heard position minus intended beat.
    """
    t_press = performer.hear_time(beat)
    t_stamp = t_press + performer.latency
    t_arrive = t_stamp + listener.latency

    handler = EventHandeler.__new__(EventHandeler)
    handler.gameplay = SimpleNamespace(jukebox_player=SimpleNamespace(
        players={"box": {"transport": "direct",
                         "streamer": _fake_direct_streamer(listener.late_s)}}))
    game = SimpleNamespace(after_calls=[])
    game.call_after = lambda ms, callback: game.after_calls.append((ms, callback))
    handler.game = game
    handler._clock_offset_ms = 0.0
    handler._clock_offset_samples = 10
    handler._last_jam_sync_log = 0.0

    enqueue = mock.Mock()
    patches = [
        mock.patch("libs.event_handeler.time.time", return_value=t_arrive),
        mock.patch("libs.logger.log"),
    ]
    if buffer_override is not None:
        patches.append(mock.patch.object(
            EventHandeler, "_active_jukebox_buffer_ms",
            return_value=buffer_override))
    with contextlib.ExitStack() as stack:
        for patch in patches:
            stack.enter_context(patch)
        handler._schedule_remote_note({"server_time": t_stamp * 1000.0}, enqueue)

    if enqueue.called or not game.after_calls:
        play_at = t_arrive
    else:
        play_at = t_arrive + game.after_calls[0][0] / 1000.0
    return play_at - listener.hear_time(beat)


class TestDirectJamBeatPlacement(unittest.TestCase):
    LEAD_MS = int(AudioStreamer.DIRECT_LEAD_IN_S * 1000)
    BEAT = 40.0  # song seconds; every machine below is audible long before this

    def _fresh_room(self, performer_kwargs=None, listener_kwargs=None):
        server_now = 1000.0
        performer = DirectMachine(**(performer_kwargs or {})).receive(
            server_now, 0.0)
        listener = DirectMachine(**(listener_kwargs or {})).receive(
            server_now, 0.0)
        return performer, listener

    def test_on_time_room_stays_on_the_beat(self):
        performer, listener = self._fresh_room(
            performer_kwargs={"latency": 0.02, "resolve": 1.8, "startup": 0.6},
            listener_kwargs={"latency": 0.06, "resolve": 2.2, "startup": 0.9},
        )
        error = _note_error(performer, listener, self.BEAT)
        self.assertLess(abs(error), 0.30)

    def test_high_ping_listener_stays_on_the_beat(self):
        performer, listener = self._fresh_room(
            performer_kwargs={"latency": 0.03},
            listener_kwargs={"latency": 0.25},
        )
        error = _note_error(performer, listener, self.BEAT)
        self.assertLess(abs(error), 0.30)

    def test_slow_starting_listener_self_corrects(self):
        # yt-dlp takes 6.5s: the local song starts ~3s past the shared
        # deadline and trails the room. The note must wait it out.
        performer, listener = self._fresh_room(
            performer_kwargs={"latency": 0.03},
            listener_kwargs={"latency": 0.05, "resolve": 6.5, "startup": 1.0},
        )
        self.assertGreater(listener.late_s, 2.0)
        error = _note_error(performer, listener, self.BEAT)
        self.assertLess(abs(error), 0.30)

    def test_mid_song_joiner_lands_on_the_rooms_beat(self):
        # Room starts at 1000; a new machine joins at 1240 (200s in).
        performer = DirectMachine(latency=0.03, resolve=2.0, startup=0.8)
        performer.receive(1000.0, 0.0)
        joiner = DirectMachine(latency=0.10, resolve=3.2, startup=1.1)
        joiner.receive(1240.0, 240.0)
        error = _note_error(performer, joiner, 230.0)
        self.assertLess(abs(error), 0.30)

    def test_slow_performer_shifts_the_room_but_is_irreducible(self):
        # A performer whose own song started late plays behind the room by
        # construction; no listener-side scheduling can know it. This test
        # documents that the error is the performer's own lateness (not the
        # listener's buffer) and nothing more.
        performer, listener = self._fresh_room(
            performer_kwargs={"latency": 0.03, "resolve": 6.5, "startup": 1.0},
            listener_kwargs={"latency": 0.05},
        )
        self.assertGreater(performer.late_s, 2.0)
        error = _note_error(performer, listener, self.BEAT)
        # Notes land late by about the performer's own lateness, never by a
        # lead-in on top of it.
        self.assertGreater(error, 0.0)
        self.assertLess(error, performer.late_s + 0.50)

    def test_room_of_three_listeners_hears_the_note_together(self):
        room = [
            DirectMachine(latency=0.02, resolve=1.6, startup=0.5),
            DirectMachine(latency=0.08, resolve=2.4, startup=1.0),
            DirectMachine(latency=0.05, resolve=6.0, startup=0.9),
        ]
        performer = DirectMachine(latency=0.03, resolve=2.0, startup=0.8)
        for machine in room + [performer]:
            machine.receive(1000.0, 0.0)
        heard_at = [performer.hear_time(self.BEAT) + _note_error(performer, m, self.BEAT)
                    for m in room]
        self.assertLess(max(heard_at) - min(heard_at), 0.30)


class TestOldLeadInHoldRegression(unittest.TestCase):
    """The pre-fix behavior (buffer = whole DIRECT_LEAD_IN_S) must FAIL these,
    proving the simulation would have caught the bug it now guards against."""

    def _error_with_old_buffer(self, listener_kwargs=None):
        performer = DirectMachine(latency=0.03, resolve=2.0, startup=0.8)
        listener = DirectMachine(**(listener_kwargs or {}))
        performer.receive(1000.0, 0.0)
        listener.receive(1000.0, 0.0)
        old_buffer_ms = int(AudioStreamer.DIRECT_LEAD_IN_S * 1000)
        return _note_error(performer, listener, 40.0,
                           buffer_override=old_buffer_ms)

    def test_on_time_listener_missed_the_beat_by_seconds(self):
        error = self._error_with_old_buffer(
            listener_kwargs={"latency": 0.05, "resolve": 2.2, "startup": 0.9})
        self.assertGreater(error, 3.0)

    def test_slow_listener_still_missed_by_a_second_plus(self):
        error = self._error_with_old_buffer(
            listener_kwargs={"latency": 0.05, "resolve": 4.6, "startup": 1.0})
        # ~1.6s late here: even a machine that started ~1.6s behind the room
        # heard the note far off the beat (only a machine slower than the
        # whole lead-in accidentally lined up).
        self.assertGreater(error, 1.0)


if __name__ == "__main__":
    unittest.main()
