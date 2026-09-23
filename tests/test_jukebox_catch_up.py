"""A direct start that ran past the shared deadline is re-aimed at the room.

A machine whose own resolve+startup outran the shared start deadline becomes
audible late and, before this, played the WHOLE song that far behind the room
(``AudioStreamer.direct_late_s``). Its music is then ahead of everybody else's,
so a note played on it reaches every other machine after the beat -- and no
rule on the listening side can pull that back. The only cure is to start the
decode somewhere else, so the streamer gets one re-aim: it drops the flight
that started late and launches again *joining* the room at the position the
room will have reached by then.

These tests hold the three halves of that:

* the aim lands on the room's clock (pure arithmetic, both for the default
  join and for a re-aim), and the default is unchanged to the byte,
* the decision fires only where it should -- anchored jukebox streams, a
  lateness the ear can actually hear, and at most once,
* the flight loop really does it: a second ffmpeg launch with a further-aimed
  ``-ss``, the late flight's process killed, its frames dropped (nothing of
  them reaches the output), and ``direct_late_s`` re-measured from the flight
  that stands.

No OpenAL, no ffmpeg, no network: the decode is a fake pipe and the sources
are fakes that report what the code reads.
"""

import io
import os
import queue
import sys
import threading
import time as real_time
import types
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import cyal

from libs.music_bot import AudioStreamer
from libs.music_bot import streaming as streaming_module

FRAME = b"\x2a" * 3840          # one 20 ms stereo frame at 48 kHz
FRAME_SECONDS = 0.02


class FakeClock:
    """A monotonic clock the flights can be moved along by hand."""

    def __init__(self, now=1000.0):
        self.now = float(now)

    def monotonic(self):
        return self.now

    def time(self):
        return 1_700_000_000.0 + self.now

    def sleep(self, seconds):
        self.now += max(0.0, float(seconds))

    def advance(self, seconds):
        self.now += max(0.0, float(seconds))


class FakeBuffer:
    def __init__(self):
        self.data = None

    def set_data(self, data, sample_rate=None, format=None):
        self.data = data


class FakeSource:
    """An OpenAL source as the streamer reads one: instant sink, real states."""

    def __init__(self):
        self.queued = []
        self.playing = False
        self.stopped = False

    def queue_buffers(self, buf):
        self.queued.append(buf)

    def unqueue_buffers(self):
        released = list(self.queued)
        self.queued = []
        return released

    @property
    def buffers_queued(self):
        return len(self.queued)

    @property
    def buffers_processed(self):
        # This sink plays as fast as it is fed, so everything it holds is
        # finished: the streamer must drain it and never stall on it.
        return len(self.queued)

    @property
    def state(self):
        return cyal.SourceState.PLAYING if self.playing else cyal.SourceState.STOPPED

    def play(self):
        self.playing = True

    def stop(self):
        self.stopped = True
        self.playing = False


class FakePipe:
    def __init__(self, frames):
        self._frames = [bytes(frame) for frame in frames]
        self.reads = 0

    def read(self, size):
        self.reads += 1
        if not self._frames:
            return b""
        return self._frames.pop(0)


class FakeProcess:
    """One ffmpeg launch: a pipe of PCM frames and a kill switch."""

    def __init__(self, frames):
        self.stdout = FakePipe(frames)
        self.stderr = io.BytesIO(b"")
        self.killed = False
        self.returncode = None

    def poll(self):
        return self.returncode

    def kill(self):
        self.killed = True
        self.returncode = -9

    def wait(self, timeout=None):
        return self.returncode


def build_streamer(clock, *, late_s=0.0, start_offset=0.0, received_at=None,
                   anchored=True, bot=None, cinema=None, lead_in=None):
    """A real streamer over fakes, with the launch path stubbed at the edges."""
    source = FakeSource()
    streamer = AudioStreamer(
        game=None,
        audio_url="https://example.invalid/song.ogg",
        source=source,
        bot=bot,
        channels=2,
        start_offset=start_offset,
        start_offset_received_at=received_at,
        canonical_url=None,
        timeline_anchor=anchored,
        room_lead_in_s=lead_in,
    )
    streamer.cinema = cinema
    streamer.direct_late_s = late_s
    streamer._fake_source = source
    return streamer


class TheAimLandsOnTheRoomClockTests(unittest.TestCase):
    """The arithmetic: the seek names the content the room will be playing."""

    def test_the_default_join_aim_is_the_slack_it_always_was(self):
        # A mid-song join with nothing measured aims with the alignment slack
        # (lead-in + startup estimate) -- byte for byte what it used to.
        seek = AudioStreamer.direct_seek_seconds(48.0, 1000.0, 1002.0)
        self.assertAlmostEqual(
            seek, 48.0 + 2.0 + AudioStreamer.DIRECT_STARTUP_EST_S, places=9)

    def test_the_relay_fallbacks_own_lead_in_keeps_its_number(self):
        # A machine joining a RELAY room (lead-in 0) aims with the estimate
        # alone, exactly as the per-listener fallback always did.
        seek = AudioStreamer.direct_seek_seconds(48.0, 1000.0, 1002.0, lead_in=0.0)
        self.assertAlmostEqual(
            seek, 48.0 + 2.0 + AudioStreamer.DIRECT_STARTUP_EST_S, places=9)

    def test_a_measured_aim_lands_the_machine_on_the_room(self):
        # A re-aim of H seconds named from the rebuild instant: the deadline
        # falls exactly H after it, and the content it plays is the position
        # the room has at that instant.
        received_at, offset = 1000.0, 48.0
        rebuild_at, aim = 1010.0, 5.25
        seek = AudioStreamer.direct_seek_seconds(
            offset, received_at, rebuild_at, aim_ahead_s=aim)
        deadline = AudioStreamer.direct_start_deadline(
            offset, received_at, seek)
        self.assertAlmostEqual(deadline, rebuild_at + aim, places=9)
        # The room's position: its clock runs one lead-in behind t_zero.
        room_at_deadline = (deadline - (received_at - offset)
                            - AudioStreamer.DIRECT_LEAD_IN_S)
        self.assertAlmostEqual(seek, room_at_deadline, places=9)

    def test_the_aim_is_the_same_shape_for_a_fresh_song(self):
        # A fresh broadcast (offset ~0) is re-aimed by joining the room too:
        # the seek formula takes the 0.001 the join path hands it.
        received_at, rebuild_at, aim = 1000.0, 1006.0, 4.5
        seek = AudioStreamer.direct_seek_seconds(
            0.001, received_at, rebuild_at, aim_ahead_s=aim)
        deadline = AudioStreamer.direct_start_deadline(0.001, received_at, seek)
        self.assertAlmostEqual(deadline, rebuild_at + aim, places=9)

    def test_an_aim_shorter_than_the_rooms_lead_in_is_still_exact(self):
        # A quick machine catching up its own small drift aims shorter than the
        # room's lead-in, and the deadline simply lands that much sooner -- it
        # is not made to wait the room's intro out a second time.
        received_at, offset = 1000.0, 48.0
        rebuild_at, aim = 1010.0, 1.0
        self.assertLess(aim, AudioStreamer.DIRECT_LEAD_IN_S)
        seek = AudioStreamer.direct_seek_seconds(
            offset, received_at, rebuild_at, aim_ahead_s=aim)
        deadline = AudioStreamer.direct_start_deadline(offset, received_at, seek)
        self.assertAlmostEqual(deadline, rebuild_at + aim, places=9)
        self.assertAlmostEqual(
            seek, (rebuild_at + aim) - (received_at - offset)
            - AudioStreamer.DIRECT_LEAD_IN_S, places=9)


class TheDecisionTests(unittest.TestCase):
    """When a late start may be re-aimed -- and when it may not."""

    def setUp(self):
        self.clock = FakeClock()
        self.patcher = mock.patch.object(
            streaming_module, "time", SimpleNamespace(
                monotonic=self.clock.monotonic, time=self.clock.time,
                sleep=self.clock.sleep))
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def _slow(self, *, late_s=1.2, startup_s=4.4, **kwargs):
        """A stream whose last flight took `startup_s` and started `late_s` late."""
        streamer = build_streamer(
            self.clock, start_offset=0.0, received_at=1000.0, **kwargs)
        streamer._attempt_started_at = self.clock.monotonic() - startup_s
        streamer.direct_late_s = late_s
        return streamer

    def test_a_late_anchored_flight_is_re_aimed_at_its_own_startup(self):
        streamer = self._slow(late_s=1.2, startup_s=4.4)
        aim = streamer.direct_catch_up_aim_s()
        self.assertIsNotNone(aim)
        self.assertAlmostEqual(aim, 4.4 + AudioStreamer.DIRECT_CATCH_UP_MARGIN_S)

    def test_a_lateness_nobody_can_hear_is_left_alone(self):
        for late_s in (0.0, 0.5, AudioStreamer.DIRECT_CATCH_UP_MIN_S - 0.01):
            streamer = self._slow(late_s=late_s)
            self.assertIsNone(streamer.direct_catch_up_aim_s())
            self.assertFalse(streamer._catch_up_used)

    def test_a_restart_more_expensive_than_the_slack_is_refused(self):
        # The silence the restart costs is this machine's own startup again:
        # a machine slower than the whole alignment slack would pay more for
        # the cure than the drift it removes.
        streamer = self._slow(
            late_s=1.2, startup_s=AudioStreamer.DIRECT_ALIGN_SLACK_S + 0.01)
        self.assertIsNone(streamer.direct_catch_up_aim_s())

    def test_a_large_lateness_is_cured_when_the_restart_is_cheap(self):
        # A slow resolve leaves a machine minutes behind, and the second
        # launch does not pay the resolve again: the drift is worth fixing
        # however large it is when the price is one ffmpeg startup.
        streamer = self._slow(late_s=45.0, startup_s=0.9)
        self.assertAlmostEqual(streamer.direct_catch_up_aim_s(),
                               0.9 + AudioStreamer.DIRECT_CATCH_UP_MARGIN_S)

    def test_it_is_spent_once_and_never_loops(self):
        streamer = self._slow()
        self.assertIsNotNone(streamer.direct_catch_up_aim_s())
        streamer._catch_up_used = True
        self.assertIsNone(streamer.direct_catch_up_aim_s())

    def test_a_stream_with_no_shared_timeline_is_never_re_aimed(self):
        # The personal music bot and a livestream have no room to be late
        # for: an anchored jukebox stream is the only thing that has one.
        unanchored = self._slow(anchored=False)
        self.assertIsNone(unanchored.direct_catch_up_aim_s())
        bot_stream = self._slow(bot=SimpleNamespace())
        self.assertIsNone(bot_stream.direct_catch_up_aim_s())

    def test_a_cinema_room_is_left_to_its_own_recovery(self):
        # A room is fed frame by frame and cannot be emptied and refilled
        # (stop() is its teardown), so a decode that jumped under it would
        # hand it frames from two places in the song at once.
        streamer = self._slow(cinema=SimpleNamespace())
        self.assertIsNone(streamer.direct_catch_up_aim_s())

    def test_a_flight_that_was_never_launched_is_left_alone(self):
        streamer = build_streamer(
            self.clock, start_offset=0.0, received_at=1000.0)
        streamer.direct_late_s = 1.2
        streamer._attempt_started_at = None
        self.assertIsNone(streamer.direct_catch_up_aim_s())


class TheFlightLoopTests(unittest.TestCase):
    """`run()` itself: a second launch aimed at the room, and the first dropped."""

    def setUp(self):
        self.clock = FakeClock()
        self.patcher = mock.patch.object(
            streaming_module, "time", SimpleNamespace(
                monotonic=self.clock.monotonic, time=self.clock.time,
                sleep=self.clock.sleep))
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.launches = []

    def _run(self, *, late_first=1.2, late_second=0.0, startup=4.4,
             frames=8, **kwargs):
        """Drive the real run() over fake flights. Returns (streamer, holds)."""
        streamer = build_streamer(
            self.clock, start_offset=0.0, received_at=1000.0, **kwargs)
        holds = []

        def fake_popen(cmd, **opts):
            process = FakeProcess([FRAME] * frames)
            self.launches.append((list(cmd), process))
            # The launch itself costs a startup before the pre-buffer fills.
            self.clock.advance(startup if len(self.launches) == 1 else 1.5)
            return process

        def fake_hold(running_stream, leftover):
            # Patched onto the class, so this is the streamer, not the test.
            # The measured lateness belongs to the flight that just filled
            # its pre-buffer; the real hold also waits out an early start,
            # which is irrelevant to the re-aim itself.
            late = late_first if len(holds) == 0 else late_second
            holds.append({
                "late": late,
                "frames_fed": running_stream.pair_frames_fed,
                "pause_buffer": len(running_stream._pause_buffer),
                "seek_to": running_stream._direct_seek_to,
                "process": running_stream.process,
                # A flight's ffmpeg is killed by the cleanup at the very end
                # of run(), so "which flight is alive" only reads at the
                # instant the hold runs.
                "killed": [proc.killed for _, proc in self.launches],
            })
            running_stream.direct_late_s = max(0.0, late)
            # A late machine reaches the deadline already past it, so the
            # real hold waits nothing and the flight's own startup is what
            # the re-aim measures: nothing moves the clock here.
            return leftover

        with mock.patch.object(streaming_module, "FFMPEG_PATH", "ffmpeg"), \
                mock.patch.object(AudioStreamer, "_diagnostic_startup_call",
                                  lambda self, label, function, *a, **kw: function(*a, **kw)), \
                mock.patch.object(AudioStreamer, "_init_buffer_pool",
                                  lambda self: self._buffer_pool.extend(
                                      [FakeBuffer() for _ in range(32)])), \
                mock.patch.object(AudioStreamer, "_hold_direct_start", fake_hold), \
                mock.patch.object(streaming_module.subprocess, "Popen", fake_popen):
            streamer.run()
        return streamer, holds

    def test_a_late_flight_is_dropped_and_the_next_one_joins_the_room(self):
        streamer, holds = self._run()
        self.assertEqual(len(self.launches), 2)
        self.assertEqual(len(holds), 2)

        first_cmd, first_process = self.launches[0]
        second_cmd, second_process = self.launches[1]
        # A fresh broadcast starts at position 0...
        self.assertNotIn("-ss", first_cmd)
        # ...and the re-aimed flight seeks past the intro, at the room.
        self.assertIn("-ss", second_cmd)
        seek = float(second_cmd[second_cmd.index("-ss") + 1])
        self.assertGreater(seek, 0.5)
        deadline = AudioStreamer.direct_start_deadline(
            streamer.start_offset, streamer.start_offset_received_at, seek,
            streamer.room_lead_in_s)
        room_at_deadline = (deadline
                            - (streamer.start_offset_received_at
                               - streamer.start_offset)
                            - streamer.room_lead_in_s)
        self.assertAlmostEqual(seek, room_at_deadline, places=6)

        # The flight that started late is gone: killed, and its frames with it.
        self.assertEqual(holds[1]["killed"], [True, False])
        self.assertIs(holds[1]["process"], second_process)
        self.assertEqual(holds[1]["frames_fed"], 0)
        self.assertEqual(holds[1]["pause_buffer"], 0)

        # The price is named, and the stream plays out of the aimed flight.
        self.assertAlmostEqual(
            streamer.direct_catch_up_s,
            4.4 + AudioStreamer.DIRECT_CATCH_UP_MARGIN_S)
        self.assertAlmostEqual(streamer.direct_late_s, 0.0)
        self.assertTrue(streamer.ready_event.is_set())
        self.assertTrue(streamer.completed_normally)

    def test_a_machine_slow_twice_keeps_its_one_restart(self):
        # Chronically slow: the second flight is late as well, and the loop
        # must not spend a third launch -- the old behavior (trail the room,
        # report it through direct_late_s) stands.
        streamer, holds = self._run(late_first=1.2, late_second=1.4)
        self.assertEqual(len(self.launches), 2)
        self.assertAlmostEqual(streamer.direct_late_s, 1.4)
        # The second flight stands (the re-aim was already spent): the old
        # behavior is what a machine this slow gets.
        self.assertEqual(holds[1]["killed"], [True, False])

    def test_an_on_time_flight_is_never_restarted(self):
        streamer, holds = self._run(late_first=0.0)
        self.assertEqual(len(self.launches), 1)
        self.assertEqual(streamer.direct_catch_up_s, 0.0)
        self.assertFalse(streamer._catch_up_used)

    def test_an_unanchored_stream_is_never_restarted(self):
        streamer, holds = self._run(anchored=False)
        self.assertEqual(len(self.launches), 1)
        self.assertFalse(streamer._catch_up_used)

    def test_the_late_flights_frames_never_reach_the_output(self):
        # What the late flight decoded is dropped, not played: the pair's
        # clock starts over with the queue it can no longer count, and the
        # frames standing in the output are the aimed flight's own.
        streamer, _ = self._run()
        self.assertTrue(streamer._fake_source.stopped)
        self.assertTrue(all(buf.data == FRAME
                            for buf in streamer._fake_source.queued))



if __name__ == "__main__":
    unittest.main()
