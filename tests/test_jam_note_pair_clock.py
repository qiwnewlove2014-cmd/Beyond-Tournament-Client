"""A plain cabinet's pair waits on the same clock a cinema room does.

A note played along to a jukebox is held until the beat arrives, and the beat
is the audio the listener is *hearing*. That hold used to be a figure measured
once, when the note's packet arrived: the queue depth of whatever is playing.
A queue drains and refills as the song plays, so a hold that does not follow it
walks away from the song -- and it walks away *differently on each machine*,
which is the "the band is straight here and behind there" report. A cinema room
was given the answer first (its own queue, re-projected every frame); this is
the same answer for an ordinary cabinet's stereo pair, and it is the same
implementation -- ``libs/jukebox_clock.py`` -- so the two can never disagree
about what a wait means.

What is the streamer's own is only the input: the frames it has handed to the
pair, the size of one of them, and whether the pair is playing at all.
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
from libs.drums import DrumAudio
from libs.event_handeler import EventHandeler
from libs.jukebox_clock import (JAM_WAIT_EARLY_FRAMES, PAIR_KEY, SpotClock,
                                mono_ms)
from libs.piano import PianoAudio
from libs.jukebox_relay import JukeboxRelayReceiver
from libs.music_bot.streaming import AudioStreamer

from test_jam_note_room_clock import Clock


def at(clock):
    """Run the clock under test against a wall clock the test owns."""
    return mock.patch("libs.jukebox_clock.time.monotonic",
                      side_effect=lambda: clock.now)


class PairSource:
    """The one thing the clock reads off a pair: its queue, and playing."""

    def __init__(self, queued=0, playing=True):
        self.buffers_queued = queued
        self.state = (cyal.SourceState.PLAYING if playing
                      else cyal.SourceState.STOPPED)


class Model:
    """A stand-in output for the shared rule: frames fed, frames still queued."""

    def __init__(self, fed=0, queued=0, frame_ms=20.0, playing=True):
        self.fed = fed
        self.queued = queued
        self.frame_ms = frame_ms
        self.playing = playing
        self.fire = mock.Mock()

    def played(self):
        return max(0, self.fed - self.queued)

    def clock(self):
        return SpotClock(
            progress_of=lambda key: self.played(),
            frame_ms_of=lambda: self.frame_ms,
            playing_of=lambda key: self.playing,
            keys_of=lambda: (PAIR_KEY,),
            lead_of=lambda key: self.queued,
        )


def register(pair_clock, clock, ms, fire, **kwargs):
    """Register a wait against the test's own clock, not the real one."""
    with at(clock):
        return pair_clock.wait_advance(ms, fire, **kwargs)


def advance(model, clock, frames, wall_ms=None):
    """Play ``frames`` of the pair forward, moving the wall clock with it."""
    with at(clock):
        model.queued = max(0, model.queued - frames)
        clock.tick(frames * model.frame_ms if wall_ms is None else wall_ms)


class TheSharedRuleTests(unittest.TestCase):
    """The pair's wait is the room's wait: same rule, same bounds, same file."""

    def test_the_note_fires_when_the_pair_has_played_it(self):
        clock = Clock()
        model = Model(fed=8, queued=8)          # nothing played yet
        pair_clock = model.clock()
        self.assertTrue(register(pair_clock, clock, 100.0, model.fire))
        with at(clock):
            pair_clock.pump_waits()
        model.fire.assert_not_called()
        advance(model, clock, 3)                # 60 ms of the 100 ms
        with at(clock):
            pair_clock.pump_waits()
        model.fire.assert_not_called()
        advance(model, clock, 2)
        with at(clock):
            pair_clock.pump_waits()
        model.fire.assert_called_once_with()

    def test_a_catch_up_pulls_the_note_in_but_never_wildly(self):
        clock = Clock()
        model = Model(fed=24, queued=24)
        pair_clock = model.clock()
        register(pair_clock, clock, 300.0, model.fire)  # target = start + 300
        advance(model, clock, 12, wall_ms=1.0)          # 240 ms of song in 1 ms
        with at(clock):
            pair_clock.pump_waits()
        model.fire.assert_not_called()                  # not a queue and a half early
        clock.tick(300.0 - JAM_WAIT_EARLY_FRAMES * 20.0 - 10.0)
        with at(clock):
            pair_clock.pump_waits()
        model.fire.assert_not_called()
        clock.tick(20.0)
        with at(clock):
            pair_clock.pump_waits()
        model.fire.assert_called_once_with()

    def test_a_pair_that_is_not_playing_is_not_a_clock(self):
        """Stopped sources report everything as finished: that says nothing."""
        clock = Clock()
        model = Model(fed=24, queued=24)
        pair_clock = model.clock()
        register(pair_clock, clock, 300.0, model.fire)
        model.playing = False
        advance(model, clock, 24, wall_ms=1.0)          # a jump nobody made
        with at(clock):
            pair_clock.pump_waits()
        model.fire.assert_not_called()                  # kept on the wall clock
        clock.tick(300.0)
        with at(clock):
            pair_clock.pump_waits()
        model.fire.assert_called_once_with()            # not dropped, not early

    def test_a_stalled_pair_fires_at_its_wall_clock_target(self):
        clock = Clock()
        model = Model(fed=4, queued=4)
        pair_clock = model.clock()
        register(pair_clock, clock, 40.0, model.fire)
        clock.tick(20.0)
        with at(clock):
            pair_clock.pump_waits()
        model.fire.assert_not_called()
        clock.tick(60.0)
        with at(clock):
            pair_clock.pump_waits()
        model.fire.assert_called_once_with()

    def test_a_torn_down_pair_drops_what_is_waiting(self):
        clock = Clock()
        model = Model(fed=4, queued=4)
        pair_clock = model.clock()
        register(pair_clock, clock, 100.0, model.fire)
        self.assertEqual(pair_clock.pending_waits(), 1)
        pair_clock.drop_waits()
        self.assertEqual(pair_clock.pending_waits(), 0)

    def test_a_frames_size_is_measured_from_the_audio(self):
        """The transports do not agree: 20 ms decoded, 40 ms received."""
        self.assertAlmostEqual(mono_ms(bytes(1920)), 20.0)     # 960 mono16
        self.assertAlmostEqual(mono_ms(bytes(3840)), 40.0)     # 1920 mono16
        self.assertEqual(mono_ms(b""), 0.0)
        self.assertEqual(mono_ms(None), 0.0)


class TheStreamersOwnHalfTests(unittest.TestCase):
    """Fed minus queued, the frame's size, and whether the pair is playing."""

    def direct(self, fed=0, queued=0, frame_ms=0.0, playing=True, pair=True,
               cinema=None, running=True, paused=False):
        streamer = AudioStreamer.__new__(AudioStreamer)
        streamer.cinema = cinema
        streamer.running = running
        streamer.paused = paused
        streamer.spatial_pair = ((object(), object(), 1.0, 40.0) if pair else None)
        streamer.spatial_src_l = PairSource(queued, playing)
        streamer.spatial_src_r = PairSource(queued, playing)
        streamer.source = PairSource(queued, playing)
        streamer.pair_frames_fed = fed
        streamer._pair_frame_ms = frame_ms
        return streamer

    def relay(self, fed=0, queued=0, frame_ms=0.0, playing=True,
              started=True, cinema=None, running=True, stopped=False):
        receiver = JukeboxRelayReceiver.__new__(JukeboxRelayReceiver)
        receiver.cinema = cinema
        receiver.running = running
        receiver._stopped = stopped
        receiver._play_started = started
        receiver.source_l = PairSource(queued, playing)
        receiver.source_r = PairSource(queued, playing)
        receiver.pair_frames_fed = fed
        receiver._pair_frame_ms = frame_ms
        return receiver

    def test_played_frames_are_what_the_pair_has_finished(self):
        streamer = self.direct(fed=10, queued=4)
        self.assertEqual(streamer.pair_played_frames(), 6)
        receiver = self.relay(fed=10, queued=4)
        self.assertEqual(receiver.pair_played_frames(), 6)

    def test_played_frames_never_go_below_zero(self):
        """A pair credited from a room can hold more than this stream has fed."""
        self.assertEqual(self.direct(fed=0, queued=3).pair_played_frames(), 0)
        self.assertEqual(self.relay(fed=0, queued=3).pair_played_frames(), 0)

    def test_the_frame_size_is_the_audios_own(self):
        streamer = self.direct(fed=1, frame_ms=mono_ms(bytes(1920)))
        self.assertAlmostEqual(streamer.pair_frame_ms(), 20.0)
        receiver = self.relay(fed=1, frame_ms=mono_ms(bytes(3840)))
        self.assertAlmostEqual(receiver.pair_frame_ms(), 40.0)

    def test_the_frame_size_falls_back_before_a_frame_is_queued(self):
        self.assertAlmostEqual(self.direct().pair_frame_ms(),
                               AudioStreamer.SAMPLES_PER_BUFFER / 48.0)
        self.assertAlmostEqual(self.relay().pair_frame_ms(),
                               JukeboxRelayReceiver.RELAY_FRAME_MS)

    def test_queuing_a_frame_is_what_counts_it(self):
        streamer = self.direct()
        streamer.spatial_src_l.buffers_queued = 2
        streamer.spatial_src_r.buffers_queued = 2
        streamer._note_pair_fed(bytes(1920))
        self.assertEqual(streamer.pair_frames_fed, 1)
        self.assertAlmostEqual(streamer.pair_frame_ms(), 20.0)
        self.assertEqual(streamer.pair_played_frames(), 0)   # held ahead of audible

    def test_a_pair_that_is_not_the_output_is_not_playing(self):
        self.assertFalse(self.direct(cinema=object()).pair_is_playing())
        self.assertFalse(self.direct(pair=False).pair_is_playing())
        self.assertFalse(self.direct(playing=False).pair_is_playing())
        self.assertFalse(self.direct(running=False).pair_is_playing())
        self.assertFalse(self.direct(paused=True).pair_is_playing())
        self.assertFalse(self.relay(cinema=object()).pair_is_playing())
        self.assertFalse(self.relay(started=False).pair_is_playing())
        self.assertFalse(self.relay(playing=False).pair_is_playing())
        self.assertFalse(self.relay(stopped=True).pair_is_playing())
        self.assertTrue(self.direct().pair_is_playing())
        self.assertTrue(self.relay().pair_is_playing())

    def test_the_clock_is_one_object_measured_on_the_pair(self):
        streamer = self.direct(fed=3, queued=1)
        clock = streamer.pair_clock
        self.assertIs(clock, streamer.pair_clock)
        self.assertEqual(clock.keys(), [PAIR_KEY])

    def test_resetting_the_pair_starts_its_clock_over(self):
        streamer = self.direct(fed=9, queued=2)
        clock = streamer.pair_clock
        clock.wait_advance(50.0, mock.Mock())
        streamer._pair_clock_reset()
        self.assertEqual(streamer.pair_frames_fed, 0)
        self.assertEqual(streamer.pair_frame_ms(),
                         AudioStreamer.SAMPLES_PER_BUFFER / 48.0)
        # The wait is kept (a note is not lost to a hiccup) ...
        self.assertEqual(clock.pending_waits(), 1)
        # ... and the pair reads as having played nothing yet.
        self.assertEqual(streamer.pair_played_frames(), 0)

    def test_a_pair_taken_from_a_room_starts_level_with_its_queue(self):
        """The detached pair holds the room's frames: nothing is un-played.

        ``switch_to_pair`` credits the frames the room already fed to those
        two sources, so the pair's clock starts at zero *played* instead of
        reading its own queue as a standing debt (which would hold every note
        at its wall-clock instant for the length of that queue).
        """
        streamer = self.direct(queued=5)
        streamer._pair_credit_from_room()
        self.assertEqual(streamer.pair_frames_fed, streamer.pair_queued_frames())
        self.assertEqual(streamer.pair_played_frames(), 0)
        receiver = self.relay(queued=5)
        receiver._pair_credit_from_room()
        self.assertEqual(receiver.pair_frames_fed, receiver.pair_queued_frames())
        self.assertEqual(receiver.pair_played_frames(), 0)


class TheSchedulerChoosesThePairClockTests(unittest.TestCase):
    """A note on a plain cabinet waits on the pair; a room's own clock wins."""

    def _handler(self, kind, streamer, backlog=200, room=None):
        handler = EventHandeler.__new__(EventHandeler)
        handler.gameplay = SimpleNamespace(jukebox_player=SimpleNamespace(players={}))
        handler.game = SimpleNamespace(
            after_calls=[],
            call_after=lambda ms, fn: handler.game.after_calls.append((ms, fn)))
        handler._clock_offset_ms = 0.0
        handler._clock_offset_samples = 10
        handler._last_jam_sync_log = 0.0
        handler._jam_buffer_kind = None
        handler._jam_buffer_detail = None
        handler._jam_streamer = None
        handler._note_song_cabinet = lambda position, peer=None: "box"
        handler._note_room_bank = lambda cabinet: room
        handler._report_room_note_latency = mock.Mock()

        def measure(**kwargs):
            # The real measurement is what records both of these on the way.
            handler._jam_buffer_kind = kind
            handler._jam_streamer = streamer
            return backlog

        handler._active_jukebox_buffer_ms = measure
        return handler

    def _hit(self, handler):
        enqueue = mock.Mock()
        now_ms = time.time() * 1000.0
        payload = {"server_time": now_ms, "x": 1.0, "y": 2.0, "z": 0.0,
                   "peer_id": "Ann"}
        with mock.patch("libs.event_handeler.time.time",
                        return_value=now_ms / 1000.0):
            handler._schedule_remote_note(payload, enqueue)
        return enqueue

    def _pair_clock(self, took=True):
        clock = mock.Mock()
        clock.wait_advance.return_value = took
        return clock

    def test_a_plain_relay_note_waits_on_the_pairs_own_clock(self):
        clock = self._pair_clock()
        handler = self._handler("relay", SimpleNamespace(pair_clock=clock))
        enqueue = self._hit(handler)
        self.assertEqual(handler.game.after_calls, [])
        enqueue.assert_not_called()
        held_ms, handed = clock.wait_advance.call_args[0][:2]
        self.assertIs(handed, enqueue)
        self.assertGreater(held_ms, 0)
        self.assertLessEqual(held_ms, 200)
        # The pair's own allowance is the constant inside ``delay`` already:
        # nothing extra is asked for on the room path's terms.
        self.assertEqual(clock.wait_advance.call_args[1], {})

    def test_a_plain_direct_note_waits_on_the_same_clock(self):
        clock = self._pair_clock()
        handler = self._handler("direct", SimpleNamespace(pair_clock=clock))
        self._hit(handler)
        clock.wait_advance.assert_called_once()
        self.assertEqual(handler.game.after_calls, [])

    def test_the_clock_gets_the_instant_the_frame_timer_would_have_used(self):
        """Only *when* it is re-checked differs, never the instant it aims for."""
        clock = self._pair_clock(took=False)
        handler = self._handler("relay", SimpleNamespace(pair_clock=clock))
        self._hit(handler)
        self.assertEqual(len(handler.game.after_calls), 1)
        self.assertEqual(int(clock.wait_advance.call_args[0][0]),
                         handler.game.after_calls[0][0])

    def test_a_room_that_took_the_note_never_asks_the_pair(self):
        room = mock.Mock()
        room.note_spawn_ms.return_value = 0.0
        room.wait_advance.return_value = True
        clock = self._pair_clock()
        handler = self._handler("room", SimpleNamespace(pair_clock=clock),
                                room=room)
        self._hit(handler)
        room.wait_advance.assert_called_once()
        clock.wait_advance.assert_not_called()

    def test_a_room_that_refused_the_note_keeps_the_frame_timer(self):
        """A room's hold is a room's queue: the pair was never measured."""
        room = mock.Mock()
        room.note_spawn_ms.return_value = 0.0
        room.wait_advance.return_value = False
        clock = self._pair_clock()
        handler = self._handler("room", SimpleNamespace(pair_clock=clock),
                                room=room)
        self._hit(handler)
        clock.wait_advance.assert_not_called()
        self.assertEqual(len(handler.game.after_calls), 1)

    def test_a_streamer_with_no_clock_keeps_the_frame_timer(self):
        """An older build's streamer (or none at all) is unchanged."""
        handler = self._handler("relay", SimpleNamespace())
        self._hit(handler)
        self.assertEqual(len(handler.game.after_calls), 1)
        handler = self._handler("direct", None)
        self._hit(handler)
        self.assertEqual(len(handler.game.after_calls), 1)

    def test_a_clock_that_refuses_the_wait_keeps_the_frame_timer(self):
        clock = self._pair_clock(took=False)
        handler = self._handler("relay", SimpleNamespace(pair_clock=clock))
        self._hit(handler)
        self.assertEqual(len(handler.game.after_calls), 1)


class ThePumpSiteTests(unittest.TestCase):
    """``JukeboxPlayer.update`` is the one thread a wait may fire on."""

    def _player(self, streamer, room=None):
        game = SimpleNamespace(network=SimpleNamespace(send=lambda *a: None),
                               audio_mngr=None)
        player = jukebox.JukeboxPlayer(game)
        player._last_recovery_request_at = 0.0
        if room is not None:
            streamer.cinema = room
        player.players["box"] = {
            "source": None, "secondary_source": None, "streamer": streamer,
            "title": "Song", "url": "https://youtu.be/x", "transport": "relay",
            "playback_key": ("id", 4), "relay_key": (1001, 2001),
            "created_at": time.monotonic(), "play_params": {},
        }
        return player

    def _streamer(self, clock):
        return SimpleNamespace(is_alive=lambda: True, pair_clock=clock,
                               last_packet_at=time.monotonic())

    def test_the_pairs_clock_is_pumped_every_frame(self):
        clock = mock.Mock()
        player = self._player(self._streamer(clock))
        player.update()
        self.assertIn("box", player.players)
        self.assertEqual(clock.pump_waits.call_count, 1)

    def test_a_room_and_a_pair_are_both_pumped(self):
        clock = mock.Mock()
        room = SimpleNamespace(pump_waits=mock.Mock(), sources=())
        player = self._player(self._streamer(clock), room=None)
        player.players["box"]["cinema"] = room
        player.players["box"]["streamer"].cinema = room
        player.update()
        self.assertIn("box", player.players)
        self.assertEqual(clock.pump_waits.call_count, 1)
        self.assertEqual(room.pump_waits.call_count, 1)

    def test_an_entry_with_no_clock_is_skipped(self):
        player = self._player(SimpleNamespace(is_alive=lambda: True,
                                              last_packet_at=time.monotonic()))
        player.update()          # no clock, no crash
        self.assertIn("box", player.players)


class ThePairSpendsWhatANoteCostsTests(unittest.TestCase):
    """The pair's own spawn cost, measured by the instrument that plays it.

    A cinema room measures what one of its notes costs to reach a speaker on
    this machine and spends that before the beat
    (``CinemaSpeakerBank.note_spawn_ms``). An ordinary cabinet has exactly the
    same problem -- a slow computer sounds the band behind the beat -- and the
    measurement belongs to the object that *places* the note: the instrument,
    whose own work (occlusion, the PA and venue copies, the source itself) is
    what the wait has to cover.
    """

    # --------------------------------------------------------- instruments

    def _instrument(self, cls):
        am = SimpleNamespace(instrument_samples=SimpleNamespace(
            status=lambda path: "ready"))
        instrument = cls(am)
        instrument.gameplay = SimpleNamespace(
            player=SimpleNamespace(x=0.0, y=0.0, z=0.0), map=None)
        return instrument

    def test_the_piano_times_its_own_note_on_the_way_out(self):
        piano = self._instrument(PianoAudio)
        self.assertEqual(piano.note_spawn_ms(), 0.0)     # nothing sounded yet
        with mock.patch.object(piano, "play_note",
                               side_effect=lambda **kw: time.sleep(0.03)):
            piano._play_queued_note({"peer_id": "Ann", "note": "C4",
                                     "x": 1.0, "y": 2.0, "z": 0.0})
        self.assertGreaterEqual(piano.note_spawn_ms(), 25.0)
        self.assertLessEqual(piano.note_spawn_ms(), 150.0)

    def test_the_kit_times_its_own_hit_on_the_way_out(self):
        drums = self._instrument(DrumAudio)
        self.assertEqual(drums.note_spawn_ms(), 0.0)
        with mock.patch.object(drums, "play_hit",
                               side_effect=lambda *a, **kw: time.sleep(0.03)):
            drums._play_remote_hit({"peer_id": "Ann", "pad": 0,
                                    "x": 1.0, "y": 2.0, "z": 0.0})
        self.assertGreaterEqual(drums.note_spawn_ms(), 25.0)
        self.assertLessEqual(drums.note_spawn_ms(), 150.0)

    def test_a_note_that_only_waited_for_its_sample_reports_nothing(self):
        """Nothing has sounded, so there is no cost to report.

        A deferred note's sample is still loading: the instrument keeps the
        number it had instead of learning a figure from a note that never
        played.
        """
        piano = self._instrument(PianoAudio)
        piano.am.instrument_samples.status = lambda path: "loading"
        piano._play_queued_note({"peer_id": "Ann", "note": "C4",
                                 "x": 1.0, "y": 2.0, "z": 0.0})
        self.assertEqual(piano.note_spawn_ms(), 0.0)

    # ----------------------------------------------------------- scheduler

    def _handler(self, spawn_ms=0.0, has_method=True):
        handler = EventHandeler.__new__(EventHandeler)
        piano = SimpleNamespace()
        if has_method:
            piano.note_spawn_ms = lambda: spawn_ms
        clock = mock.Mock()
        clock.wait_advance.return_value = True

        def measure(**kwargs):
            handler._jam_buffer_kind = "relay"
            handler._jam_buffer_detail = "relay 5 frames"
            handler._jam_streamer = SimpleNamespace(pair_clock=clock)
            return 200

        handler._active_jukebox_buffer_ms = measure
        handler._clock_offset_ms = 0.0
        handler._clock_offset_samples = 10
        handler._last_jam_sync_log = 0.0
        handler._jam_buffer_kind = None
        handler._jam_buffer_detail = None
        handler._jam_streamer = None
        handler._note_song_cabinet = lambda position, peer=None: "box"
        handler._note_room_bank = lambda cabinet: None
        handler._report_room_note_latency = mock.Mock()
        handler.gameplay = SimpleNamespace(
            jukebox_player=SimpleNamespace(players={}))
        handler.game = SimpleNamespace(
            audio_mngr=SimpleNamespace(piano=piano, drums=SimpleNamespace()),
            after_calls=[],
            call_after=lambda ms, fn: handler.game.after_calls.append((ms, fn)))
        return handler, clock

    def _hit(self, handler, instrument="piano", report=True):
        enqueue = mock.Mock()
        now_ms = time.time() * 1000.0
        payload = {"server_time": now_ms, "x": 1.0, "y": 2.0, "z": 0.0,
                   "peer_id": "Ann"}
        with mock.patch("libs.event_handeler.time.time",
                        return_value=now_ms / 1000.0):
            handler._schedule_remote_note(payload, enqueue,
                                          instrument=instrument)
        return enqueue

    def test_the_measured_spawn_is_spent_before_the_beat(self):
        handler, clock = self._handler(spawn_ms=90.0)
        self._hit(handler)
        self.assertEqual(clock.wait_advance.call_args[1].get("tail_ms"), 45.0)
        self.assertEqual(handler.game.after_calls, [])

    def test_a_spawn_inside_the_allowance_is_not_spent_twice(self):
        """The target already allows for the frame wait and the queue drain.

        Spending part of that allowance again would fire the note early, and
        "unchanged unless this machine is slow" is the promise: a machine that
        measured less than the constant keeps the timing it always had.
        """
        handler, clock = self._handler(spawn_ms=30.0)
        self._hit(handler)
        self.assertEqual(clock.wait_advance.call_args[1], {})

    def test_nothing_measured_keeps_the_timing_it_always_had(self):
        for has_method in (True, False):          # measured 0, or no number at all
            handler, clock = self._handler(spawn_ms=0.0, has_method=has_method)
            self._hit(handler)
            self.assertEqual(clock.wait_advance.call_args[1], {})

    def test_the_measured_number_is_readable_in_the_line(self):
        handler, _clock = self._handler(spawn_ms=90.0)
        # The real report, not the stub: this test is about what a person reads.
        handler._report_room_note_latency = (
            EventHandeler._report_room_note_latency.__get__(handler))
        now_ms = time.time() * 1000.0
        with mock.patch("libs.event_handeler.time.time",
                        return_value=now_ms / 1000.0), \
                mock.patch("libs.deferred_log.log_deferred") as log_line:
            handler._schedule_remote_note(
                {"server_time": now_ms, "x": 1.0, "y": 2.0, "z": 0.0,
                 "peer_id": "Ann"}, mock.Mock(), instrument="piano")
        line = log_line.call_args_list[0].args[0]
        self.assertIn("[Jam] a live note is heard", line)
        self.assertIn("relay 5 frames spawn=90ms", line)

    def test_the_number_is_asked_of_the_instrument_that_plays_the_note(self):
        """Piano and kit measure their own work: no figure shared between them."""
        handler, _clock = self._handler()
        handler.game.audio_mngr.drums.note_spawn_ms = lambda: 120.0
        self.assertEqual(handler._instrument_note_spawn_ms("drums"), 120)
        self.assertEqual(handler._instrument_note_spawn_ms("piano"), 0)
        self.assertEqual(handler._instrument_note_spawn_ms(None), 0)
        self.assertEqual(handler._instrument_note_spawn_ms("nothing"), 0)


if __name__ == "__main__":
    unittest.main()
