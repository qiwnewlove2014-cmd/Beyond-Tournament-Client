"""The room's own clock is what a jam note waits on, and what a note costs.

A note played into a cinema room has to land on the beat a listener is
*hearing*, and what a listener is hearing is the room's queue -- not the
server's live edge. The queue moves (frames drained, a shed, a realign, a
hold), so a wait measured once when the note arrived and then slept out walks
away from the song whenever the queue changes under it: two machines listening
to one room then disagree about whether the band is on the beat, which is the
"some computers hear it straight, some do not" report.

So the wait is anchored to the room's own content position instead
(``CinemaSpeakerBank.wait_advance`` / ``pump_waits``): the room projects the
instant its queue will have played the note's remaining music, re-projects it
on every frame, and bounds the correction to what a moving queue can really
explain -- at most ``JAM_WAIT_EARLY_FRAMES`` earlier than the wall-clock
target, at most ``JAM_WAIT_SLACK_MS`` later. The room also measures what one
of its own notes costs to sound on *this* machine (``note_spawn_ms``), so a
slow computer spends that before the beat rather than after it.

Everything here is offline: the OpenAL objects are the fakes ``test_cinema_live``
already uses, and the wall clock is a variable the test owns -- so "the queue
drained" and "the room stalled" are the physical events they are named after,
with the clock standing still or running exactly as each one implies.
"""

import os
import sys
import time
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from libs.audio.cinema import CinemaRenderer, CinemaSpeakerBank
from libs.audio.cinema import live
from libs.audio.cinema.crossover import FULL_RANGE
from libs.jukebox_clock import JAM_WAIT_EARLY_FRAMES, JAM_WAIT_SLACK_MS
from libs.event_handeler import EventHandeler

from test_cinema_live import FakeGame, frame, specs

ANCHOR = (10.0, 20.0, 0.0)
# 20 ms at 48 kHz: the frame size the direct streamer hands over, so every
# number below is in the milliseconds a player would actually hear.
FRAME_MS = 20.0


def make_bank(frames=8, slots=("front_l", "front_r"), playing=True):
    """A front pair holding ``frames`` frames of song."""
    game = FakeGame()
    renderer = CinemaRenderer(ANCHOR, "front_only", specs=specs(list(slots)))
    bank = CinemaSpeakerBank(game, renderer, volume=100, cabinet_volume=100,
                             occlusion_provider=lambda *a: 0)
    for index in range(frames):
        bank.queue_frame(*frame(index + 1, size=960))
    if playing:
        bank.start_playback()
    return bank


class Clock:
    """The wall clock, moved by the test and by nothing else."""

    def __init__(self, start=1000.0):
        self.now = start

    def tick(self, ms):
        self.now += ms / 1000.0


def at(clock):
    """Run the room against this clock instead of the real one."""
    return mock.patch("libs.audio.cinema.bank.time.monotonic",
                      side_effect=lambda: clock.now)


def register(bank, clock, ms, fire, **kwargs):
    with at(clock):
        return bank.wait_advance(ms, fire, **kwargs)


def pump(bank, clock):
    with at(clock):
        return bank.pump_waits()


def play(bank, clock, frames, wall_ms=None):
    """Play ``frames`` frames forward, moving the wall clock with them.

    ``wall_ms`` overrides how long that took: the default is the frames' own
    music, which is a healthy room. A smaller number is the room *catching
    up* (a realign or a shed emptied its queue faster than real time); frames
    and no wall time at all is a reading that cannot be explained, and is what
    a stopped speaker's "everything is finished" report looks like.
    """
    with at(clock):
        for _ in range(frames):
            for source in bank.sources:
                if source.buffers:
                    source.buffers.pop(0)
        clock.tick(frames * FRAME_MS if wall_ms is None else wall_ms)


class TheRoomIsTheClockTests(unittest.TestCase):
    """A note waits on the room's content position, not on the wall clock."""

    def test_the_note_fires_when_the_rooms_queue_reaches_it(self):
        # The correction band is two frames either side of the wall-clock
        # target, so "still waiting" is only a real statement outside it: 60 ms
        # of song is 40 ms short of the 100 ms the note needs, and that is
        # outside.
        clock = Clock()
        bank = make_bank(frames=8)
        fire = mock.Mock()
        self.assertTrue(register(bank, clock, 100.0, fire))
        pump(bank, clock)
        fire.assert_not_called()
        play(bank, clock, 3)             # 60 ms of the 100 ms still to come
        pump(bank, clock)
        fire.assert_not_called()
        play(bank, clock, 2)             # the music is there
        pump(bank, clock)
        fire.assert_called_once_with()

    def test_a_note_whose_music_is_not_there_yet_stays_held(self):
        clock = Clock()
        bank = make_bank(frames=24)
        fire = mock.Mock()
        register(bank, clock, 300.0, fire)
        play(bank, clock, 6)             # 120 ms of song, well short of 300
        pump(bank, clock)
        fire.assert_not_called()
        play(bank, clock, 14)
        pump(bank, clock)
        fire.assert_called_once_with()

    def test_a_queue_that_caught_up_fires_the_note_early_but_not_wildly(self):
        """Both halves of the early bound, on a room that drained fast.

        A realign (or a shed) empties the queue faster than real time, so the
        song really is further on than the note's arrival snapshot said -- and
        the note follows it, rather than firing a whole queue late. It cannot
        follow all the way, though: a reading that jumped a large amount at
        once may be a stopped speaker reporting everything as finished, and
        the note still never sounds more than two frames before the wall-clock
        instant it was going to sound at.
        """
        clock = Clock()
        bank = make_bank(frames=24)
        fire = mock.Mock()
        register(bank, clock, 300.0, fire)     # target = start + 300 ms
        play(bank, clock, 12, wall_ms=1.0)     # 240 ms of song in 1 ms
        pump(bank, clock)
        fire.assert_not_called()               # not a queue and a half early
        # The honest band starts two frames before the wall-clock target, and
        # the note sounds there -- 40 ms early rather than 240 ms, which is
        # what it would be if the room's catch-up were not read at all.
        clock.tick(300.0 - JAM_WAIT_EARLY_FRAMES * FRAME_MS - 10.0)
        pump(bank, clock)
        fire.assert_not_called()
        clock.tick(20.0)
        pump(bank, clock)
        fire.assert_called_once_with()

    def test_a_speaker_that_is_not_playing_is_not_a_clock(self):
        """A stopped speaker's "everything is finished" is not the song moving.

        The driver reports a buffer as processed the moment its source stops
        playing it, so a speaker that stopped mid-song hands in a jump that is
        nothing of the sort. That note keeps its wall-clock instant instead of
        being pulled to the front of the song.
        """
        clock = Clock()
        bank = make_bank(frames=24)
        fire = mock.Mock()
        register(bank, clock, 300.0, fire)
        for source in bank.sources:
            source.buffers.clear()
            source.stop()
        pump(bank, clock)
        fire.assert_not_called()
        clock.tick(300.0)
        pump(bank, clock)
        fire.assert_called_once_with()

    def test_a_room_that_stopped_moving_fires_at_its_wall_clock_target(self):
        """A stalled room must not hold a live player's note forever.

        Nothing is playing into the queue, so the content projection cannot
        say anything new; the note sounds at the instant it was always going
        to sound at instead of waiting for a song that is not moving.
        """
        clock = Clock()
        bank = make_bank(frames=4)
        fire = mock.Mock()
        register(bank, clock, 40.0, fire)
        clock.tick(20.0)
        pump(bank, clock)
        fire.assert_not_called()
        clock.tick(60.0)
        pump(bank, clock)
        fire.assert_called_once_with()

    def test_a_room_that_never_moves_again_is_bounded_by_the_slack(self):
        clock = Clock()
        bank = make_bank(frames=4)
        fire = mock.Mock()
        register(bank, clock, 300.0, fire)
        clock.tick(300.0)
        pump(bank, clock)
        fire.assert_not_called()
        clock.tick(JAM_WAIT_SLACK_MS)
        pump(bank, clock)
        fire.assert_called_once_with()

    def test_the_measured_spawn_cost_is_spent_before_the_beat(self):
        """What a note costs to sound here shifts it earlier, not later.

        Two identical notes on one room: the one whose machine measured its
        own spawn at 45 ms is due 55 ms into the song, the unmeasured one at
        the full 100 ms. At 60 ms of song the first has sounded and the second
        is still waiting.
        """
        clock = Clock()
        quick, plain = make_bank(frames=16), make_bank(frames=16)
        fast_fire, plain_fire = mock.Mock(), mock.Mock()
        register(quick, clock, 100.0, fast_fire, tail_ms=45.0)
        register(plain, clock, 100.0, plain_fire)
        play(quick, clock, 3)
        play(plain, clock, 3)
        pump(quick, clock)
        pump(plain, clock)
        fast_fire.assert_called_once_with()
        plain_fire.assert_not_called()
        play(plain, clock, 2)
        pump(plain, clock)
        plain_fire.assert_called_once_with()

    def test_a_note_waits_on_the_speaker_it_was_given(self):
        bank = make_bank(frames=4)
        first = sorted(bank.slot_sources)[0]
        self.assertTrue(register(bank, Clock(), 40.0, mock.Mock(), slot=first))
        self.assertEqual(bank.pending_waits(), 1)

    def test_a_room_with_no_speakers_refuses_the_wait(self):
        bank = make_bank(frames=4)
        bank.forget_sources()
        self.assertFalse(register(bank, Clock(), 50.0, mock.Mock()))

    def test_a_stopped_room_refuses_the_wait(self):
        bank = make_bank(frames=4)
        bank.stop()
        self.assertFalse(register(bank, Clock(), 50.0, mock.Mock()))

    def test_a_torn_down_room_drops_what_is_still_waiting(self):
        """A note played against a room that goes away is dropped, not fired.

        Firing it into a room being taken apart would sound it at an instant
        nobody asked for; one lost note is invisible next to that.
        """
        clock = Clock()
        bank = make_bank(frames=4)
        fire = mock.Mock()
        register(bank, clock, 100.0, fire)
        self.assertEqual(bank.pending_waits(), 1)
        bank.stop()
        self.assertEqual(bank.pending_waits(), 0)
        pump(bank, clock)
        fire.assert_not_called()

    def test_the_rooms_spawn_cost_is_averaged_and_bounded(self):
        bank = make_bank(frames=4)
        self.assertEqual(bank.note_spawn_ms(), 0.0)   # nothing measured yet
        bank.note_spawn_report(60.0)
        self.assertEqual(bank.note_spawn_ms(), 60.0)
        bank.note_spawn_report(100.0)
        self.assertAlmostEqual(bank.note_spawn_ms(), 70.0)   # one quarter on
        before = bank.note_spawn_ms()
        for junk in (-5.0, float("nan"), 100000.0, "no", None):
            bank.note_spawn_report(junk)
        self.assertEqual(bank.note_spawn_ms(), before)


class TheSchedulerHandsTheNoteToTheRoomTests(unittest.TestCase):
    """``_schedule_remote_note`` picks the room's clock when one is playing."""

    def _handler(self, bank):
        handler = EventHandeler.__new__(EventHandeler)
        handler.gameplay = SimpleNamespace(
            jukebox_player=SimpleNamespace(players={}))
        handler.game = SimpleNamespace(after_calls=[],
                                       call_after=lambda ms, fn:
                                       handler.game.after_calls.append((ms, fn)))
        handler._clock_offset_ms = 0.0
        handler._clock_offset_samples = 10
        handler._last_jam_sync_log = 0.0
        handler._jam_buffer_kind = None
        handler._jam_buffer_detail = None
        handler._note_song_cabinet = lambda position, peer=None: "box"
        handler._note_room_bank = lambda cabinet: bank
        handler._report_room_note_latency = mock.Mock()
        handler._active_jukebox_buffer_ms = lambda **kwargs: 200
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

    def _bank(self, took=True, spawn_ms=0.0):
        bank = mock.Mock()
        bank.note_spawn_ms.return_value = spawn_ms
        bank.wait_advance.return_value = took
        return bank

    def test_a_note_goes_to_the_room_when_the_room_is_playing(self):
        bank = self._bank()
        handler = self._handler(bank)
        enqueue = self._hit(handler)
        # The room took the note: no frame timer at all, and the hold it was
        # handed is this listener's own backlog (200 ms) less the allowance.
        self.assertEqual(handler.game.after_calls, [])
        enqueue.assert_not_called()
        held_ms, enqueue_arg = bank.wait_advance.call_args[0][:2]
        self.assertIs(enqueue_arg, enqueue)
        self.assertGreater(held_ms, 0)
        self.assertLessEqual(held_ms, 200)
        # Nothing measured here yet, and the target already allows for the
        # frame wait and the queue drain: the wait spends no allowance twice.
        self.assertEqual(bank.wait_advance.call_args[1], {})

    def test_a_slow_machines_measured_spawn_raises_the_allowance(self):
        """This computer measured notes costing 90 ms: the excess is spent early.

        Only the part over ``JAM_NOTE_ADVANCE_MS`` -- the same rule a plain
        cabinet's pair follows, so a room and a pair cannot land one band a
        constant apart (both measure the same stage of the same note).
        """
        bank = self._bank(spawn_ms=90.0)
        handler = self._handler(bank)
        self._hit(handler)
        self.assertEqual(bank.wait_advance.call_args[1].get("tail_ms"),
                         90.0 - handler.JAM_NOTE_ADVANCE_MS)

    def test_a_spawn_inside_the_allowance_is_not_spent_twice(self):
        """A room that measured less than the constant keeps the old timing."""
        bank = self._bank(spawn_ms=30.0)
        handler = self._handler(bank)
        self._hit(handler)
        self.assertEqual(bank.wait_advance.call_args[1], {})

    def test_a_room_that_cannot_take_the_note_keeps_the_frame_timer(self):
        bank = self._bank(took=False)
        handler = self._handler(bank)
        enqueue = self._hit(handler)
        self.assertEqual(len(handler.game.after_calls), 1)
        enqueue.assert_not_called()

    def test_no_room_keeps_the_frame_timer(self):
        handler = self._handler(None)
        enqueue = self._hit(handler)
        self.assertEqual(len(handler.game.after_calls), 1)
        enqueue.assert_not_called()


class TheRouteMeasuresWhatANoteCostsTests(unittest.TestCase):
    """``live.route_to_room`` times the copy that sounds immediately."""

    def _terms(self, delay_ms=0.0):
        # (slot, spot, gain, delay_ms, tier, channel, tone, crossover)
        return (("front_l", (1.0, 2.0, 0.0), 1.0, delay_ms, 0, "l", None,
                 FULL_RANGE),)

    def _route(self, bank, delay_ms=0.0, schedule=None):
        game = SimpleNamespace(gameplay=SimpleNamespace())
        with mock.patch.object(live, "room_terms_for",
                               return_value=self._terms(delay_ms)), \
                mock.patch.object(live, "room_runtime", return_value=bank), \
                mock.patch.object(live, "room_for", return_value=None):
            return live.route_to_room(game, (1.0, 2.0, 0.0),
                                      lambda *a: None, schedule=schedule)

    def test_the_immediate_copy_times_the_note(self):
        bank = mock.Mock()
        self.assertEqual(self._route(bank), 1)
        bank.note_spawn_report.assert_called_once()
        self.assertGreaterEqual(bank.note_spawn_report.call_args[0][0], 0.0)

    def test_a_note_that_only_waited_a_trim_reports_nothing(self):
        """Nothing has sounded yet, so there is no cost to report.

        Every copy is deferred to that speaker's own trim: this note has not
        reached a speaker at all, and the room keeps the number it had.
        """
        bank = mock.Mock()
        scheduled = []
        self.assertEqual(self._route(bank, delay_ms=40.0,
                                     schedule=lambda ms, fn:
                                     scheduled.append(ms)), 1)
        self.assertEqual(scheduled, [40.0])
        bank.note_spawn_report.assert_not_called()


if __name__ == "__main__":
    unittest.main()
