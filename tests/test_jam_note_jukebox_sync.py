"""Offline tests: jam-note scheduling against relay and direct jukebox clocks.

Remote instrument notes must land on the beat of the song the listener is
HEARING. A relay receiver aligns via its 40ms OpenAL frame backlog. An
anchored direct stream holds the same lead-in as every other listener, so
the lead-in cancels out of note timing: a direct-mode note waits only for
this machine's own residual distance behind the room clock — its 20ms OpenAL
staging queue plus any audible start that ran past the shared deadline
(slow yt-dlp/ffmpeg startup makes the local song trail the room). Holding
notes for the whole lead-in used to put them ~DIRECT_LEAD_IN_S behind the
beat.
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


def _room(buffered_ms=120, extra_ms=60, spawn_ms=None):
    """A cinema room's own measurement: its frame queue and its delay trims.

    ``spawn_ms`` is what the room measured one of its own notes to cost to
    sound here (``CinemaSpeakerBank.note_spawn_ms``). A room that has not
    played a note of its own yet has no number at all -- which is not the same
    as a free one.
    """
    room = SimpleNamespace(sources=(object(), object()),
                           buffered_ms=lambda: buffered_ms,
                           extra_latency_ms=lambda: extra_ms)
    if spawn_ms is not None:
        room.note_spawn_ms = lambda: spawn_ms
    return room


def _relay_entry(queued=4, started=True):
    # A real receiver subclass built via __new__ (its __init__ needs live
    # OpenAL sources) so the isinstance check in the scheduler recognizes it.
    fake_cls = type("FakeReceiver", (JukeboxRelayReceiver,), {})
    streamer = fake_cls.__new__(fake_cls)
    streamer.running = True
    streamer._play_started = started
    streamer.source_l = SimpleNamespace(buffers_queued=queued)
    return {"transport": "relay", "streamer": streamer}


def _direct_entry(anchor=True, started=True, queued=5, late_s=0.0):
    ready = threading.Event()
    if started:
        ready.set()
    streamer = SimpleNamespace(
        _direct_anchor=anchor,
        running=True,
        ready_event=ready,
        direct_late_s=late_s,
        # Anchored direct workers stage one 20ms OpenAL buffer per frame on
        # their spatial pair (jukeboxes always use the stereo pair).
        spatial_src_l=SimpleNamespace(buffers_queued=queued),
        source=SimpleNamespace(buffers_queued=queued),
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

    def test_anchored_direct_reports_queue_plus_lateness(self):
        # Five staged 20ms buffers = 100ms of underrun runway, no lateness.
        handler = _handler_with_jukebox(_direct_entry(queued=5, late_s=0.0))
        self.assertEqual(handler._active_jukebox_buffer_ms(), 100)

    def test_anchored_direct_adds_own_late_start(self):
        # A machine whose ffmpeg/yt-dlp startup ran 1.5s past the shared
        # deadline hears the song 1.5s behind the room; its notes must wait.
        handler = _handler_with_jukebox(_direct_entry(queued=5, late_s=1.5))
        self.assertEqual(handler._active_jukebox_buffer_ms(), 1600)

    def test_anchored_direct_queue_never_reports_less_than_one_frame(self):
        handler = _handler_with_jukebox(_direct_entry(queued=0, late_s=0.0))
        self.assertEqual(handler._active_jukebox_buffer_ms(), 20)

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

    def test_a_relay_room_reports_the_rooms_own_queue(self):
        """A cinema room replaces the receiver's pair, so its queue is the lag.

        The 40ms-per-frame backlog belongs to the two plain sources, and in
        cinema mode those are deliberately None: measuring them reported the
        floor of one frame for a room that is a whole queue behind, so every
        remote note landed ahead of the beat.
        """
        entry = _relay_entry(queued=4)
        entry["streamer"].cinema = _room(buffered_ms=140, extra_ms=60)
        handler = _handler_with_jukebox(entry)
        self.assertEqual(handler._active_jukebox_buffer_ms(), 140)

    def test_a_rooms_trims_are_not_held_for_on_top_of_its_queue(self):
        """The room plays each trim at its own speaker when the note is spawned.

        Holding the note for the DEEPEST trim as well delayed every speaker,
        the untrimmed ones included, by that trim -- and it is the same note
        that ``live.route_to_room`` then spawns at each speaker with that
        speaker's own delay.
        """
        entry = _relay_entry(queued=4)
        entry["streamer"].cinema = _room(buffered_ms=140, extra_ms=60)
        handler = _handler_with_jukebox(entry)
        handler._active_jukebox_buffer_ms()
        self.assertEqual(handler._jam_buffer_kind, "room")
        self.assertIn("trims=60ms", handler._jam_buffer_detail)

    def test_a_direct_room_reports_queue_plus_lateness(self):
        """The room's queue and this machine's own late start."""
        entry = _direct_entry(queued=5, late_s=1.5)
        entry["streamer"].cinema = _room(buffered_ms=140, extra_ms=60)
        handler = _handler_with_jukebox(entry)
        self.assertEqual(handler._active_jukebox_buffer_ms(), 1640)

    def test_an_untrimmed_room_reports_only_its_queue(self):
        # The shipped jukebox has no trims, so nothing is added for them.
        entry = _direct_entry(queued=5, late_s=0.0)
        entry["streamer"].cinema = _room(buffered_ms=100, extra_ms=0)
        handler = _handler_with_jukebox(entry)
        self.assertEqual(handler._active_jukebox_buffer_ms(), 100)

    def test_a_room_that_has_no_speakers_is_ignored(self):
        """A released room must not be measured: the plain pair takes over."""
        entry = _direct_entry(queued=5, late_s=0.0)
        room = SimpleNamespace(sources=())
        entry["streamer"].cinema = room
        handler = _handler_with_jukebox(entry)
        self.assertEqual(handler._active_jukebox_buffer_ms(), 100)


class TheSongTheNoteIsPlayedAgainstTests(unittest.TestCase):
    """A note is synced to the cabinet its own room belongs to.

    A map can play two cabinets at once, and the sync used to measure
    whichever played first: a band jamming into one room was held by the other
    song's queue -- heard as the band trailing by a whole queue, or "playing
    along to the previous song", and only on some machines.
    """

    def _two_cabinet_handler(self):
        handler = EventHandeler.__new__(EventHandeler)
        handler.gameplay = SimpleNamespace(jukebox_player=SimpleNamespace(players={
            "boxA": _relay_entry(queued=4),    # 160ms
            "boxB": _relay_entry(queued=9),    # 360ms
        }))
        handler.game = _FakeGame()
        handler._clock_offset_ms = 0.0
        handler._clock_offset_samples = 10
        handler._last_jam_sync_log = 0.0
        return handler

    def _room_patch(self, cabinet):
        room = (cabinet, object()) if cabinet else None
        return (mock.patch("libs.audio.cinema.live.room_for", return_value=room),
                mock.patch("libs.audio.cinema.pan.target_for_name", return_value=None))

    def test_naming_a_cabinet_measures_that_cabinet_only(self):
        handler = self._two_cabinet_handler()
        self.assertEqual(handler._active_jukebox_buffer_ms(cabinet="boxB"), 360)

    def test_unnamed_keeps_the_old_first_playing_stream(self):
        handler = self._two_cabinet_handler()
        self.assertEqual(handler._active_jukebox_buffer_ms(), 160)

    def test_a_cabinet_that_is_not_playing_here_answers_none(self):
        """The beat it was played against is not audible here: play on arrival.

        Being held by a queue -- any queue -- for a song nobody hears on this
        machine is what made a jam drift on some machines and not others.
        """
        handler = self._two_cabinet_handler()
        self.assertIsNone(handler._active_jukebox_buffer_ms(cabinet="boxC"))

    def test_the_notes_cabinet_is_the_room_the_performer_stands_in(self):
        handler = self._two_cabinet_handler()
        first, second = self._room_patch("boxB")
        with first as room_for, second:
            cabinet = handler._note_song_cabinet((1.0, 2.0, 0.0), peer="Ann")
        self.assertEqual(cabinet, "boxB")
        self.assertEqual(room_for.call_args[0][1], (1.0, 2.0, 0.0))

    def test_a_note_that_belongs_to_no_room_has_no_cabinet(self):
        handler = self._two_cabinet_handler()
        first, second = self._room_patch(None)
        with first, second:
            self.assertIsNone(handler._note_song_cabinet((1.0, 2.0, 0.0), peer="Ann"))
        # No position at all (a legacy packet): nothing to resolve, no crash.
        self.assertIsNone(handler._note_song_cabinet(None, peer="Ann"))

    def test_the_room_imposes_its_cabinet_on_the_hold(self):
        handler = self._two_cabinet_handler()
        enqueue = mock.Mock()
        now_ms = time.time() * 1000.0
        data = {"server_time": now_ms, "x": 1.0, "y": 2.0, "z": 0.0,
                "peer_id": "Ann"}
        first, second = self._room_patch("boxB")
        with first, second, mock.patch("libs.event_handeler.time.time",
                                       return_value=now_ms / 1000.0):
            handler._schedule_remote_note(data, enqueue)
        enqueue.assert_not_called()
        scheduled_ms, _ = handler.game.after_calls[0]
        # boxB's own 360ms backlog (minus the advance), never boxA's 160.
        self.assertGreater(scheduled_ms, 200)
        self.assertLessEqual(scheduled_ms, 360)

    def test_a_room_whose_song_is_not_playing_here_is_immediate(self):
        handler = self._two_cabinet_handler()
        enqueue = mock.Mock()
        data = {"server_time": time.time() * 1000.0,
                "x": 1.0, "y": 2.0, "z": 0.0, "peer_id": "Ann"}
        first, second = self._room_patch("boxC")
        with first, second:
            handler._schedule_remote_note(data, enqueue)
        enqueue.assert_called_once_with()
        self.assertEqual(handler.game.after_calls, [])


class ScheduleRemoteNoteTests(unittest.TestCase):
    def _schedule(self, handler, server_time_ms_ahead, buffer_ms,
                  sender_lag_ms=None):
        enqueue = mock.Mock()
        now_ms = time.time() * 1000.0
        data = {"server_time": now_ms + server_time_ms_ahead}
        if sender_lag_ms is not None:
            data["sender_lag_ms"] = sender_lag_ms
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

    def test_direct_note_hold_is_the_small_queue_not_the_lead_in(self):
        handler = _handler_with_jukebox(_direct_entry(queued=5))
        # A note hit ~now: the hold sits just under the 100ms staging queue
        # (minus the advance). It must NEVER approach the shared lead-in,
        # which every machine holds and which therefore cancels.
        enqueue, log_mock = self._schedule(handler, server_time_ms_ahead=0,
                                           buffer_ms=100)
        enqueue.assert_not_called()
        self.assertEqual(len(handler.game.after_calls), 1)
        scheduled_ms, _ = handler.game.after_calls[0]
        self.assertGreater(scheduled_ms, 0)
        self.assertLess(scheduled_ms, int(AudioStreamer.DIRECT_LEAD_IN_S * 1000))
        self.assertLessEqual(scheduled_ms, 100 + 1000)
        log_mock.assert_not_called()

    def test_direct_late_start_widens_the_note_hold(self):
        # A machine that started 1.5s late hears the song 1.5s behind the
        # room; its remote notes wait out that lateness to hit the beat.
        handler = _handler_with_jukebox(_direct_entry(queued=5, late_s=1.5))
        enqueue, log_mock = self._schedule(handler, server_time_ms_ahead=0,
                                           buffer_ms=1600)
        enqueue.assert_not_called()
        scheduled_ms, _ = handler.game.after_calls[0]
        self.assertGreater(scheduled_ms, 1000)
        self.assertLess(scheduled_ms, 1600 + 1000)
        log_mock.assert_not_called()

    def test_far_future_note_is_clamped_relative_to_the_buffer(self):
        handler = _handler_with_jukebox(_direct_entry(queued=5))
        enqueue, log_mock = self._schedule(handler, server_time_ms_ahead=90_000,
                                           buffer_ms=100)
        enqueue.assert_not_called()
        scheduled_ms, _ = handler.game.after_calls[0]
        self.assertEqual(scheduled_ms, 100 + 1000)
        log_mock.assert_called_once()

    def test_past_due_note_plays_immediately(self):
        handler = _handler_with_jukebox(_direct_entry(queued=5))
        enqueue, _ = self._schedule(handler, server_time_ms_ahead=-90_000,
                                    buffer_ms=100)
        enqueue.assert_called_once_with()
        self.assertEqual(handler.game.after_calls, [])

    def test_trailing_sender_note_plays_immediately(self):
        # A performer whose audible song trails the room (mid-song join
        # whose seek ran past the alignment slack) strikes after the beat;
        # the note is already due when it arrives, so the listener must NOT
        # hold it further. sender_lag_ms makes that explicit.
        handler = _handler_with_jukebox(_direct_entry(queued=5))
        enqueue, _ = self._schedule(handler, server_time_ms_ahead=0,
                                    buffer_ms=100, sender_lag_ms=1500)
        enqueue.assert_called_once_with()
        self.assertEqual(handler.game.after_calls, [])

    def test_sender_lag_inside_listener_backlog_holds_to_own_beat(self):
        # The listener's own song trails the room by 2.0s while the sender
        # trails by 1.5s: the note waits the DIFFERENCE so it lands on the
        # listener's heard beat, not the sender's.
        handler = _handler_with_jukebox(_direct_entry(queued=5))
        enqueue, _ = self._schedule(handler, server_time_ms_ahead=0,
                                    buffer_ms=2000, sender_lag_ms=1500)
        enqueue.assert_not_called()
        scheduled_ms, _ = handler.game.after_calls[0]
        # 2000 − 1500 − JAM_NOTE_ADVANCE_MS, no extra clamp.
        self.assertGreater(scheduled_ms, 300)
        self.assertLess(scheduled_ms, 600)

    def test_a_note_held_by_a_room_says_how_late_it_will_be_heard(self):
        """The one trace a listener has for "the band feels late".

        A room's hold is its queue plus this machine's own late start, and a
        performer who reports no lag of their own makes it bigger; without
        the components there is no telling which of them it was, and the note
        itself sounds at the right place either way.
        """
        entry = _relay_entry(queued=4)
        entry["streamer"].cinema = _room(buffered_ms=400, extra_ms=60)
        handler = _handler_with_jukebox(entry)
        now_ms = time.time() * 1000.0
        with mock.patch("libs.event_handeler.time.time",
                        return_value=now_ms / 1000.0), \
                mock.patch("libs.deferred_log.log_deferred") as log_line:
            handler._schedule_remote_note({"server_time": now_ms}, mock.Mock())
            self.assertEqual(len(log_line.call_args_list), 1)
            line = log_line.call_args_list[0].args[0]
            # The hold the note actually got: the room's queue, minus the
            # note's own advance, and nothing for its trims.
            self.assertIn("heard 355ms", line)
            self.assertIn("room queue=400ms trims=60ms", line)
            self.assertIn("sender_lag=0ms", line)
            # A drum roll is twenty notes a second and they all answer the
            # same; the listener needs the line once.
            handler._schedule_remote_note({"server_time": now_ms}, mock.Mock())
        self.assertEqual(len(log_line.call_args_list), 1)

    def test_the_rooms_own_measurement_of_a_notes_cost_is_readable(self):
        """The number that says whether a slower machine needs anything more.

        A room measures what one of its own notes costs to sound here, and the
        wait already spends that before the beat; reading it back is what tells
        a person whether the measurement is doing anything on this machine at
        all. A room that has never played a note of its own says nothing rather
        than "free", and neither does an object that is not a room.
        """
        entry = _relay_entry(queued=4)
        entry["streamer"].cinema = _room(buffered_ms=400, extra_ms=60, spawn_ms=90.0)
        handler = _handler_with_jukebox(entry)
        now_ms = time.time() * 1000.0
        with mock.patch("libs.event_handeler.time.time",
                        return_value=now_ms / 1000.0), \
                mock.patch("libs.deferred_log.log_deferred") as log_line:
            handler._schedule_remote_note({"server_time": now_ms}, mock.Mock())
            self.assertIn("spawn=90ms", log_line.call_args_list[0].args[0])
        self.assertEqual(handler._room_note_spawn_ms(_room()), 0)
        self.assertEqual(handler._room_note_spawn_ms(SimpleNamespace()), 0)

    def test_a_plain_pair_note_is_not_reported_as_a_room(self):
        """The report is about the output a listener stands beside."""
        entry = _relay_entry(queued=10)          # 400ms of plain pair backlog
        handler = _handler_with_jukebox(entry)
        now_ms = time.time() * 1000.0
        with mock.patch("libs.event_handeler.time.time",
                        return_value=now_ms / 1000.0), \
                mock.patch("libs.deferred_log.log_deferred") as log_line:
            handler._schedule_remote_note({"server_time": now_ms}, mock.Mock())
        log_line.assert_not_called()

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
