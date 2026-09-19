"""A song a Party Sync member hears is a clock for the band, like a cabinet's.

A note played along a song has to land on the beat a listener is *hearing*.
Inside a cabinet that beat is the cabinet's own audio (`libs/jukebox_clock.py`);
inside a Party Sync session the song is somebody else's stream, and it reaches
this client on a leg of its own -- a member's entity when they are standing
here, else the sink kept for them while they are on another map
(`libs/party_sync_audio.py`). That leg was never asked, so a band jamming with
the session's song was aligned for listeners standing on the performer's map and
played on arrival for everybody else: off the song by that listener's own jitter
buffer, which is a pre-buffer and up.

So the leg joins the cabinet's pair and the room's speakers as one more output
that answers the same question, through the same rule: the frames its own output
has already played, re-projected every frame. Both sides of a note ask it -- the
performer's client attaches its own answer as ``sender_lag_ms`` and the listener
holds the note for its own -- so the subtraction lands the note on the shared
beat rather than on either machine's buffer.

Everything here is offline: no OpenAL, no compression worker threads, and the
wall clock is a variable the test owns (``test_jam_note_room_clock.Clock``), so
"the queue drained" is the physical event it is named after.
"""

import os
import sys
import time
import unittest
from types import SimpleNamespace
from unittest import mock

import cyal

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from libs import party_sync_audio
from libs import voice_chat
from libs.event_handeler import EventHandeler
from libs.gameplay import Gameplay
from libs.jukebox_relay import JukeboxRelayReceiver
from libs.voice_chat import MusicCompression

from test_jam_note_pair_clock import PairSource, at
from test_jam_note_room_clock import Clock
from test_party_sync_cross_map import make_gameplay, make_state


def make_compression(source=None, fed=0, frame_ms=20.0, started=True,
                     cinema_feed=None):
    """A MusicCompression built without its worker thread (see ``__new__``)."""
    compression = MusicCompression.__new__(MusicCompression)
    compression.game = None
    compression._has_started = started
    compression.cinema_feed = cinema_feed
    compression._pair_source = source
    compression.pair_frames_fed = fed
    compression._pair_frame_ms = frame_ms
    return compression


class Leg:
    """One session member's music leg as the scheduler sees it.

    A sink's ``music_source`` with a queue on it, and the feed that queued the
    frames -- the two halves the clock reads (``pair_played_frames``).
    """

    def __init__(self, channel, queued=5, playing=True, frame_ms=20.0):
        self.channel = int(channel)
        self.source = PairSource(queued=queued, playing=playing)
        self.compression = make_compression(source=self.source, fed=queued,
                                            frame_ms=frame_ms)

    def play(self, frames, clock):
        """``frames`` more frames of the song have played (wall clock with it)."""
        with at(clock):
            self.source.buffers_queued = max(
                0, self.source.buffers_queued - frames)
            clock.tick(frames * self.compression.pair_frame_ms())

    def stop(self):
        self.source.state = cyal.SourceState.STOPPED

    def start(self):
        self.source.state = cyal.SourceState.PLAYING


class FakeVoiceCompression:
    """Stands in for the voice decoder thread a sink would otherwise start."""

    def __init__(self, *args, **kwargs):
        pass

    def close(self):
        pass


def attach(gameplay, leg):
    """Give a session member's sink a playing music leg (as the packets do)."""
    with mock.patch("libs.voice_chat.voice_chat_compression",
                    FakeVoiceCompression):
        sinks = party_sync_audio.sinks_for(gameplay)
        sink = sinks.ensure_sink(gameplay, leg.channel, name="Alice",
                                 is_host=True)
    sink.music_compression = leg.compression
    return sink


# ── the feed's own half ─────────────────────────────────────────────────

class TheFeedIsAnOutputLikeAnyOtherTests(unittest.TestCase):
    """What is a feed's own is the counter and the frame's measured size."""

    def test_the_frame_size_is_measured_from_the_audio(self):
        compression = make_compression()
        compression._note_pair_fed(bytes(960 * 2), stereo=False)    # 20 ms mono
        self.assertEqual(compression.pair_frame_ms(), 20.0)
        compression._note_pair_fed(bytes(1920 * 2), stereo=False)   # 40 ms mono
        self.assertEqual(compression.pair_frame_ms(), 40.0)

    def test_a_true_stereo_frame_is_still_one_frame_long(self):
        """Party guests receive two interleaved channels at the same rate."""
        compression = make_compression()
        compression._note_pair_fed(bytes(1920 * 2), stereo=True)
        self.assertEqual(compression.pair_frame_ms(), 20.0)

    def test_the_song_position_is_frames_fed_minus_frames_queued(self):
        source = PairSource(queued=6)
        compression = make_compression(source=source, fed=10)
        self.assertEqual(compression.pair_played_frames(), 4)
        source.buffers_queued = 2
        self.assertEqual(compression.pair_played_frames(), 8)

    def test_the_note_fires_when_the_feed_has_played_it(self):
        """The same rule as a cabinet: a wait, re-projected every frame."""
        clock = Clock()
        leg = Leg(20, queued=8)
        fire = mock.Mock()
        with at(clock):
            self.assertTrue(leg.compression.pair_clock.wait_advance(
                100.0, fire))
            leg.compression.pair_clock.pump_waits()
        fire.assert_not_called()
        leg.play(3, clock)                      # 60 ms of the 100 ms
        with at(clock):
            leg.compression.pair_clock.pump_waits()
        fire.assert_not_called()
        leg.play(2, clock)
        with at(clock):
            leg.compression.pair_clock.pump_waits()
        fire.assert_called_once_with()

    def test_a_stopped_feed_is_not_a_clock_at_all(self):
        """Not playing says nothing about where the song is (see SpotClock)."""
        clock = Clock()
        leg = Leg(20, queued=8, playing=False)
        self.assertFalse(leg.compression.pair_is_playing())
        fire = mock.Mock()
        with at(clock):
            leg.compression.pair_clock.wait_advance(100.0, fire)
            clock.tick(100.0)
            leg.compression.pair_clock.pump_waits()
        fire.assert_called_once_with()          # the instant it was aimed at

    def test_a_feed_still_building_its_pre_buffer_is_not_a_clock(self):
        leg = Leg(20, queued=12)
        leg.compression._has_started = False
        self.assertFalse(leg.compression.pair_is_playing())

    def test_a_room_fed_feed_has_no_output_of_its_own(self):
        """Those frames come out of the room's speakers: the room is the clock."""
        leg = Leg(20, queued=8)
        leg.compression.cinema_feed = object()
        self.assertFalse(leg.compression.pair_is_playing())

    def test_a_cut_queue_starts_the_clock_over_and_keeps_the_wait(self):
        """A note is not lost to a hiccup, and never held past the slack."""
        clock = Clock()
        leg = Leg(20, queued=8)
        fire = mock.Mock()
        with at(clock):
            leg.compression.pair_clock.wait_advance(200.0, fire)
        leg.compression._pair_clock_reset()
        leg.play(8, clock)          # the queue that was cut is fed again
        self.assertEqual(leg.compression.pair_frames_fed, 0)
        self.assertEqual(leg.compression.pair_clock.pending_waits(), 1)
        # The clock starts over with the song, so the wait sits at its
        # wall-clock instant -- and the slack is the ceiling on that.
        with at(clock):
            clock.tick(150.0)
            leg.compression.pair_clock.pump_waits()
        fire.assert_not_called()
        with at(clock):
            clock.tick(150.0)
            leg.compression.pair_clock.pump_waits()
        fire.assert_called_once_with()


# ── who plays a member's song here ──────────────────────────────────────

class TheSessionLegIsWhatTheListenerHearsTests(unittest.TestCase):
    def test_the_entity_wins_over_a_sink_for_the_same_channel(self):
        state = make_state(guests=[("Bob", 21)])
        gameplay, _audio = make_gameplay(state)
        leg = Leg(21)
        attach(gameplay, leg)
        entity = SimpleNamespace(music_compression=leg.compression)
        gameplay.voice_channels[21] = entity
        self.assertIs(party_sync_audio.receiver_for(gameplay, None, 21), entity)
        gameplay.voice_channels.clear()
        self.assertIsNot(party_sync_audio.receiver_for(gameplay, None, 21), None)
        self.assertTrue(isinstance(
            party_sync_audio.receiver_for(gameplay, None, 21),
            party_sync_audio.PartyMemberSink))

    def test_the_host_is_asked_first_and_a_silent_leg_is_skipped(self):
        state = make_state(host_channel=20, guests=[("Bob", 21)])
        gameplay, _audio = make_gameplay(state)
        host = Leg(20, queued=5)
        guest = Leg(21, queued=4)
        attach(gameplay, host)
        attach(gameplay, guest)
        self.assertEqual(party_sync_audio.session_music_leg(gameplay).channel, 20)
        # The host stops: the guest's song is what this client hears now.
        host.stop()
        self.assertEqual(party_sync_audio.session_music_leg(gameplay).channel, 21)
        # Nothing playing anywhere is not a song at all.
        guest.stop()
        self.assertIsNone(party_sync_audio.session_music_leg(gameplay))

    def test_a_leg_with_an_empty_queue_is_not_a_song(self):
        state = make_state(host_channel=20)
        gameplay, _audio = make_gameplay(state)
        attach(gameplay, Leg(20, queued=0))
        self.assertIsNone(party_sync_audio.session_music_leg(gameplay))

    def test_no_session_is_not_a_song(self):
        gameplay, _audio = make_gameplay(None)
        self.assertIsNone(party_sync_audio.session_music_leg(gameplay))

    def test_the_handler_reads_the_same_rule(self):
        state = make_state(guests=[("Bob", 21)])
        gameplay, _audio = make_gameplay(state)
        entity = SimpleNamespace(music_compression=Leg(21).compression)
        gameplay.voice_channels[21] = entity
        handler = EventHandeler.__new__(EventHandeler)
        handler.gameplay = gameplay
        handler.game = None
        self.assertIs(handler._party_audio_receiver(21), entity)


# ── the scheduler ───────────────────────────────────────────────────────

class TheSchedulerAsksTheSessionSongTests(unittest.TestCase):
    """A note on a session song waits on that song's own clock."""

    def _handler(self, state, leg=None, cheap=False):
        handler = EventHandeler.__new__(EventHandeler)
        handler.gameplay = make_gameplay(state)[0]
        handler.gameplay.jukebox_player = None
        handler.game = SimpleNamespace(
            after_calls=[],
            call_after=lambda ms, fn: handler.game.after_calls.append((ms, fn)),
            audio_mngr=SimpleNamespace(
                piano=SimpleNamespace(note_spawn_ms=lambda: 0.0),
                drums=SimpleNamespace(note_spawn_ms=lambda: 0.0)))
        handler._clock_offset_ms = 0.0
        handler._clock_offset_samples = 10
        handler._last_jam_sync_log = 0.0
        handler._jam_buffer_kind = None
        handler._jam_buffer_detail = None
        handler._jam_streamer = None
        handler._note_song_cabinet = lambda position, peer=None: None
        handler._note_room_bank = lambda cabinet: None
        handler._report_room_note_latency = mock.Mock()
        if leg is not None:
            attach(handler.gameplay, leg)
        # The gameplay's own ``game`` is the fake one, so what a handler passes
        # down to the leg reader is the same object the sink table was built on.
        handler.gameplay.game = handler.game
        return handler

    def _hit(self, handler, clock=None):
        """Play one remote note. ``clock`` puts its own wait on the test's time."""
        enqueue = mock.Mock()
        now_ms = time.time() * 1000.0
        payload = {"server_time": now_ms, "x": 1.0, "y": 2.0, "z": 0.0,
                   "peer_id": "Ann"}
        with mock.patch("libs.event_handeler.time.time",
                        return_value=now_ms / 1000.0):
            if clock is None:
                handler._schedule_remote_note(payload, enqueue,
                                              instrument="piano")
            else:
                with at(clock):
                    handler._schedule_remote_note(payload, enqueue,
                                                  instrument="piano")
        return enqueue

    def test_the_backlog_is_the_legs_own_frames_measured(self):
        handler = self._handler(make_state(host_channel=20), Leg(20, queued=5))
        self.assertEqual(handler._active_jukebox_buffer_ms(cabinet=None), 100)
        self.assertEqual(handler._jam_buffer_kind, "party")
        self.assertTrue(handler._jam_buffer_detail.startswith("party 5 frames"))

    def test_a_silent_session_is_not_a_backlog(self):
        """No song playing here means the note plays on arrival, as it always did."""
        handler = self._handler(make_state(host_channel=20))
        self.assertIsNone(handler._active_jukebox_buffer_ms(cabinet=None))
        self.assertIsNone(handler._jam_buffer_kind)

    def test_the_note_waits_on_the_leg_instead_of_the_frame_timer(self):
        leg = Leg(20, queued=5)
        handler = self._handler(make_state(host_channel=20), leg)
        enqueue = self._hit(handler)
        self.assertEqual(handler.game.after_calls, [])
        enqueue.assert_not_called()
        self.assertEqual(leg.compression.pair_clock.pending_waits(), 1)

    def test_the_pump_fires_a_leg_held_note_on_the_game_thread(self):
        """The per-frame party tick is the pump, whatever the leg is doing.

        The queue is drained past the note's beat *and* left empty, which is an
        underrun: the note still reaches its own instant, because the pump does
        not wait for a leg to look like a song to keep pumping it.
        """
        clock = Clock()
        leg = Leg(20, queued=5)
        handler = self._handler(make_state(host_channel=20), leg)
        enqueue = self._hit(handler, clock)
        with at(clock):
            self.assertEqual(
                party_sync_audio.pump_jam_clocks(handler.gameplay,
                                                 handler.game), 0)
        # The song plays its queue out: the leg's own clock takes the note with
        # it, and the pump (the per-frame party tick) is what fires it.
        with at(clock):
            leg.source.buffers_queued = 0
            clock.tick(300.0)
            self.assertEqual(
                party_sync_audio.pump_jam_clocks(handler.gameplay,
                                                 handler.game), 1)
        enqueue.assert_called_once_with()

    def test_a_note_with_no_shared_song_plays_on_arrival(self):
        """No session song at all is the low-latency jam it always was."""
        handler = self._handler(None)
        enqueue = self._hit(handler)
        self.assertEqual(handler.game.after_calls, [])
        enqueue.assert_called_once_with()

    def test_a_playing_cabinet_still_wins_over_the_session(self):
        """The map's own cabinet is tied to the note; the session is the fallback."""
        handler = self._handler(make_state(host_channel=20), Leg(20, queued=5))
        receiver = JukeboxRelayReceiver.__new__(JukeboxRelayReceiver)
        receiver.cinema = None
        receiver.running = True
        receiver._stopped = False
        receiver._play_started = True
        receiver.source_l = PairSource(queued=1)
        receiver.source_r = PairSource(queued=1)
        handler.gameplay.jukebox_player = SimpleNamespace(
            players={"box": {"streamer": receiver}})
        self.assertEqual(handler._active_jukebox_buffer_ms(cabinet="box"), 40)
        self.assertEqual(handler._jam_buffer_kind, "relay")

    def test_the_performer_attaches_their_own_leg_as_the_sender_lag(self):
        """Both sides of a note ask the same question, on the same leg."""
        leg = Leg(20, queued=5)
        handler = self._handler(make_state(host_channel=20), leg)
        gameplay = SimpleNamespace(
            game=SimpleNamespace(network=SimpleNamespace(event_handeler=handler)),
            player=SimpleNamespace(x=1.0, y=2.0, z=0.0, name="Bob"),
        )
        packet = Gameplay._attach_jukebox_sender_lag(gameplay, {"note": "C4"})
        self.assertEqual(packet["sender_lag_ms"], 100)

    def test_a_performer_with_no_shared_song_attaches_nothing(self):
        handler = self._handler(None)
        gameplay = SimpleNamespace(
            game=SimpleNamespace(network=SimpleNamespace(event_handeler=handler)),
            player=SimpleNamespace(x=1.0, y=2.0, z=0.0, name="Bob"),
        )
        packet = Gameplay._attach_jukebox_sender_lag(gameplay, {"note": "C4"})
        self.assertNotIn("sender_lag_ms", packet)


if __name__ == "__main__":
    unittest.main()
