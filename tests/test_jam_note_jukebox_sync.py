"""Offline tests: jam-note scheduling against relay and direct jukebox clocks.

Remote instrument notes must land on the beat of the song the listener is
HEARING. A relay receiver aligns via its 40ms OpenAL frame backlog; an
anchored direct stream runs exactly one DIRECT_LEAD_IN_S behind the
server's song clock — and without that branch every direct-mode note used
to play on arrival, late by a full round trip and audibly off the beat.
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
from libs.jukebox import JukeboxRelayReceiver
from libs.music_bot import AudioStreamer


class _FakeGame:
    def __init__(self):
        self.after_calls = []

    def call_after(self, ms, callback):
        self.after_calls.append((ms, callback))


def _handler_with_jukebox(entries):
    handler = EventHandeler.__new__(EventHandeler)
    handler.gameplay = SimpleNamespace(
        jukebox_player=SimpleNamespace(players={"box": entries}))
    handler.game = _FakeGame()
    handler._clock_offset_ms = 0.0
    handler._clock_offset_samples = 10
    handler._last_jam_sync_log = 0.0
    return handler


def _relay_entry(queued=4, started=True):
    # A real receiver subclass built via __new__ (its __init__ needs live
    # OpenAL sources) so the isinstance check in the scheduler recognizes it.
    fake_cls = type("FakeReceiver", (JukeboxRelayReceiver,), {})
    streamer = fake_cls.__new__(fake_cls)
    streamer.running = True
    streamer._play_started = started
    streamer.source_l = SimpleNamespace(buffers_queued=queued)
    return {"transport": "relay", "streamer": streamer}


def _direct_entry(anchor=True, started=True):
    ready = threading.Event()
    if started:
        ready.set()
    streamer = SimpleNamespace(
        _direct_anchor=anchor,
        running=True,
        ready_event=ready,
    )
    return {"transport": "direct", "streamer": streamer}


class ActiveJukeboxBufferTests(unittest.TestCase):
    def test_no_jukebox_player_returns_none(self):
        handler = EventHandeler.__new__(EventHandeler)
        handler.gameplay = SimpleNamespace(jukebox_player=None)
        self.assertIsNone(handler._active_jukebox_buffer_ms())

    def test_relay_receiver_reports_40ms_per_queued_frame(self):
        handler = _handler_with_jukebox(_relay_entry(queued=4))
        self.assertEqual(handler._active_jukebox_buffer_ms(), 160)

    def test_anchored_direct_reports_the_lead_in(self):
        handler = _handler_with_jukebox(_direct_entry(anchor=True, started=True))
        self.assertEqual(
            handler._active_jukebox_buffer_ms(),
            int(AudioStreamer.DIRECT_LEAD_IN_S * 1000))

    def test_direct_before_audible_start_reports_none(self):
        handler = _handler_with_jukebox(_direct_entry(anchor=True, started=False))
        self.assertIsNone(handler._active_jukebox_buffer_ms())

    def test_unanchored_direct_keeps_the_immediate_path(self):
        handler = _handler_with_jukebox(_direct_entry(anchor=False, started=True))
        self.assertIsNone(handler._active_jukebox_buffer_ms())

    def test_stopped_direct_stream_reports_none(self):
        entry = _direct_entry(anchor=True, started=True)
        entry["streamer"].running = False
        handler = _handler_with_jukebox(entry)
        self.assertIsNone(handler._active_jukebox_buffer_ms())


class ScheduleRemoteNoteTests(unittest.TestCase):
    def _schedule(self, handler, server_time_ms_ahead, buffer_ms):
        enqueue = mock.Mock()
        now_ms = time.time() * 1000.0
        data = {"server_time": now_ms + server_time_ms_ahead}
        with mock.patch.object(EventHandeler, "_active_jukebox_buffer_ms",
                               return_value=buffer_ms), \
                mock.patch("libs.event_handeler.time.time", return_value=now_ms / 1000.0), \
                mock.patch("libs.logger.log") as log_mock:
            handler._schedule_remote_note(data, enqueue)
        return enqueue, log_mock

    def test_no_buffer_plays_immediately(self):
        handler = _handler_with_jukebox({})
        enqueue = mock.Mock()
        with mock.patch.object(EventHandeler, "_active_jukebox_buffer_ms",
                               return_value=None):
            handler._schedule_remote_note({"server_time": 123.0}, enqueue)
        enqueue.assert_called_once_with()
        self.assertEqual(handler.game.after_calls, [])

    def test_direct_note_holds_for_the_lead_in_without_clamping(self):
        handler = _handler_with_jukebox(_direct_entry())
        lead_ms = int(AudioStreamer.DIRECT_LEAD_IN_S * 1000)
        # A note hit ~now: the hold sits just under the lead-in (minus the
        # one-way travel already elapsed). The old flat 500ms cap would have
        # clamped this and played the note ~3s early against the heard song.
        enqueue, log_mock = self._schedule(handler, server_time_ms_ahead=0,
                                           buffer_ms=lead_ms)
        enqueue.assert_not_called()
        self.assertEqual(len(handler.game.after_calls), 1)
        scheduled_ms, _ = handler.game.after_calls[0]
        self.assertGreater(scheduled_ms, lead_ms - 500)
        self.assertLessEqual(scheduled_ms, lead_ms + 1000)
        log_mock.assert_not_called()

    def test_far_future_note_is_clamped_relative_to_the_buffer(self):
        handler = _handler_with_jukebox(_direct_entry())
        lead_ms = int(AudioStreamer.DIRECT_LEAD_IN_S * 1000)
        enqueue, log_mock = self._schedule(handler, server_time_ms_ahead=90_000,
                                           buffer_ms=lead_ms)
        enqueue.assert_not_called()
        scheduled_ms, _ = handler.game.after_calls[0]
        self.assertEqual(scheduled_ms, lead_ms + 1000)
        log_mock.assert_called_once()

    def test_past_due_note_plays_immediately(self):
        handler = _handler_with_jukebox(_direct_entry())
        lead_ms = int(AudioStreamer.DIRECT_LEAD_IN_S * 1000)
        enqueue, _ = self._schedule(handler, server_time_ms_ahead=-90_000,
                                    buffer_ms=lead_ms)
        enqueue.assert_called_once_with()
        self.assertEqual(handler.game.after_calls, [])

    def test_relay_hold_stays_uncapped_below_its_backlog(self):
        handler = _handler_with_jukebox(_relay_entry(queued=4))
        enqueue, log_mock = self._schedule(handler, server_time_ms_ahead=0,
                                           buffer_ms=160)
        enqueue.assert_not_called()
        scheduled_ms, _ = handler.game.after_calls[0]
        self.assertGreater(scheduled_ms, 0)
        self.assertLessEqual(scheduled_ms, 160 + 1000)
        log_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
