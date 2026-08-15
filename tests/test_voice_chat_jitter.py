"""Unit tests for the megaphone jitter buffer (music/voice broadcast smoothness)."""

import sys
import os
import contextlib
import queue
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from libs import voice_chat as vc
from libs.voice_chat import MegaphoneJitterBuffer


class FakeGame:
    pass


def frame(n=0):
    return bytes([n % 256]) * MegaphoneJitterBuffer.FRAME_SIZE


class TestMegaphoneJitterBuffer(unittest.TestCase):
    def setUp(self):
        self.jb = MegaphoneJitterBuffer(FakeGame())

    def test_pa_uses_stable_v16_reserve(self):
        self.assertEqual(MegaphoneJitterBuffer.PRE_BUFFER_FRAMES, 3)
        self.assertEqual(vc._megaphone_margin_frames(7), 6)

    def test_prebuffers_before_first_playback(self):
        # Fewer than PRE_BUFFER_FRAMES -> still pre-buffering (None).
        for _ in range(MegaphoneJitterBuffer.PRE_BUFFER_FRAMES - 1):
            self.jb.add_packet(frame())
            self.assertIsNone(self.jb.get_packet())
            self.assertFalse(self.jb.is_playing)
        # Enough frames -> playback starts.
        self.jb.add_packet(frame())
        out = self.jb.get_packet()
        self.assertIsNotNone(out)
        self.assertTrue(self.jb.is_playing)

    def test_underrun_rebuffers_before_resuming(self):
        # Fill and drain until the buffer runs dry mid-playback.
        for i in range(MegaphoneJitterBuffer.PRE_BUFFER_FRAMES):
            self.jb.add_packet(frame(i))
        while self.jb.get_packet() is not None:
            pass
        # Buffer is empty; the last get_packet must have marked an underrun.
        self.assertTrue(self.jb._underrun)
        self.assertTrue(self.jb.is_playing)

        # A single late frame must NOT resume playback (would chop again).
        self.jb.add_packet(frame(99))
        self.assertIsNone(self.jb.get_packet())
        self.assertTrue(self.jb._underrun)

        # After RESUME_FRAMES accumulate, playback resumes smoothly.
        for _ in range(MegaphoneJitterBuffer.RESUME_FRAMES - 1):
            self.jb.add_packet(frame())
        out = self.jb.get_packet()
        self.assertIsNotNone(out)
        self.assertFalse(self.jb._underrun)

    def test_steady_stream_never_underruns(self):
        # A normal 20ms cadence stream (one packet per get) must stay smooth.
        for i in range(20):
            self.jb.add_packet(frame(i))
            out = self.jb.get_packet()
            if self.jb.is_playing:
                self.assertIsNotNone(out)
        self.assertFalse(self.jb._underrun)

    def test_reset_clears_underrun(self):
        for _ in range(MegaphoneJitterBuffer.PRE_BUFFER_FRAMES):
            self.jb.add_packet(frame())
        while self.jb.get_packet() is not None:
            pass
        self.assertTrue(self.jb._underrun)
        self.jb.reset()
        self.assertFalse(self.jb.is_playing)
        self.assertFalse(self.jb._underrun)
        self.assertEqual(self.jb.last_output_time, 0.0)

    def test_should_output_limits_to_one_frame_per_20ms(self):
        # The megaphone receive path pops on the 20ms wall-clock (should_output)
        # instead of once per packet, so a burst of packets stays buffered and
        # plays out at the steady cadence instead of jittering the PA.
        self.assertTrue(self.jb.should_output(1000.0))
        self.assertFalse(self.jb.should_output(1001.0))
        self.assertFalse(self.jb.should_output(1019.9))

    def test_should_output_does_not_accumulate_scheduler_lateness(self):
        # A Windows polling loop commonly wakes about 1-2ms late. Advancing
        # from the sampled wake time turns each 20ms interval into roughly
        # 21ms; advancing the fixed deadline keeps the long-run rate at 50 Hz.
        outputs = 0
        now = 1000.0
        for _ in range(4762):  # approximately ten seconds at 2.1ms polling
            outputs += int(self.jb.should_output(now))
            now += 2.1

        self.assertGreaterEqual(outputs, 499)
        self.assertLessEqual(outputs, 501)
        last_poll = now - 2.1
        self.assertLess(
            last_poll - self.jb.last_output_time,
            self.jb.FRAME_DURATION_MS,
        )

    def test_should_output_resets_after_long_worker_stall(self):
        self.assertTrue(self.jb.should_output(1000.0))
        self.assertTrue(self.jb.should_output(1020.0))

        # A long suspension emits one frame and starts a fresh cadence. It
        # must not immediately burst queued frames to catch up old deadlines.
        self.assertTrue(self.jb.should_output(1100.0))
        self.assertFalse(self.jb.should_output(1100.0))
        self.assertFalse(self.jb.should_output(1119.9))
        self.assertTrue(self.jb.should_output(1120.0))

    def test_clock_gated_steady_stream_never_underruns(self):
        # Simulate the receive loop: add packets as they arrive, pop only when
        # the output clock says a frame is due. A burst of 3 arrivals then a
        # pause must drain at one frame per 20ms and never underrun mid-burst.
        # Pre-buffer enough to start playback.
        for i in range(MegaphoneJitterBuffer.PRE_BUFFER_FRAMES + 1):
            self.jb.add_packet(frame(i))
        # Drain one frame now (clock allows).
        self.assertTrue(self.jb.should_output(1000.0))
        self.assertIsNotNone(self.jb.get_packet())
        # Burst: three frames arrive together; only one may be emitted now.
        for i in range(3):
            self.jb.add_packet(frame(i))
        self.assertFalse(self.jb.should_output(1005.0))
        # After 20ms, the next frame is due and there is buffered audio.
        self.assertTrue(self.jb.should_output(1020.0))
        self.assertIsNotNone(self.jb.get_packet())
        self.assertFalse(self.jb._underrun)

    def test_audio_worker_drains_burst_without_another_packet_arrival(self):
        """A buffered second frame plays on the next clock tick by itself."""
        for i in range(MegaphoneJitterBuffer.PRE_BUFFER_FRAMES + 2):
            self.jb.add_packet(frame(i))
        gameplay = SimpleNamespace(
            player=SimpleNamespace(dead=False),
            megaphone=SimpleNamespace(player_sources={7: {}}),
        )
        game = SimpleNamespace(
            audio_mngr=SimpleNamespace(
                context=SimpleNamespace(batch=lambda: contextlib.nullcontext())
            )
        )
        worker = vc.voice_chat_compression.__new__(vc.voice_chat_compression)
        worker.game = game
        worker._megaphone_decoders = {}
        worker._megaphone_playouts = {
            7: {
                'gameplay': gameplay,
                'sources': [object()],
                'jitter_buffer': self.jb,
                'last_packet_monotonic': 10.0,
            }
        }
        with mock.patch.object(vc, 'queue_and_delay_frame') as output:
            worker._drain_megaphone_playout(1000.0, 10.0)
            self.assertEqual(output.call_count, 1)
            worker._drain_megaphone_playout(1005.0, 10.005)
            self.assertEqual(output.call_count, 1)
            # No receive callback occurred between these calls.
            worker._drain_megaphone_playout(1020.0, 10.020)
            self.assertEqual(output.call_count, 2)

    def test_short_arrival_gap_never_invents_silence(self):
        """Only decoded media frames may enter the PA source queue."""
        for i in range(MegaphoneJitterBuffer.PRE_BUFFER_FRAMES):
            self.jb.add_packet(frame(i + 1))
        gameplay = SimpleNamespace(
            player=SimpleNamespace(dead=False),
            megaphone=SimpleNamespace(player_sources={7: {}}),
        )
        worker = vc.voice_chat_compression.__new__(vc.voice_chat_compression)
        worker.game = SimpleNamespace(
            audio_mngr=SimpleNamespace(
                context=SimpleNamespace(batch=lambda: contextlib.nullcontext())
            )
        )
        worker._megaphone_decoders = {}
        worker._megaphone_playouts = {
            7: {
                'gameplay': gameplay,
                'sources': [object()],
                'jitter_buffer': self.jb,
                'last_packet_monotonic': 10.0,
            }
        }
        with mock.patch.object(vc, 'queue_and_delay_frame') as output:
            worker._drain_megaphone_playout(1000.0, 10.0)
            self.assertEqual(output.call_count, 1)
            first_packet = output.call_args.args[3]
            self.assertNotEqual(first_packet, bytes(self.jb.FRAME_SIZE))

            # A late arrival consumes the real reserve; the playout layer must
            # not manufacture a silence frame just because depth is below the
            # target. Reliable server delivery will replenish the reserve.
            worker._drain_megaphone_playout(1020.0, 10.020)
            self.assertEqual(output.call_count, 2)
            self.assertNotEqual(output.call_args.args[3], bytes(self.jb.FRAME_SIZE))

    def test_processed_openal_buffers_return_to_pool(self):
        first, second = object(), object()

        class Source:
            def __init__(self):
                self.processed = [first, second]

            @property
            def buffers_processed(self):
                return len(self.processed)

            def unqueue_buffers(self):
                return self.processed.pop(0)

        original_pool = vc._shared_buffer_pool
        try:
            vc._shared_buffer_pool = []
            vc._reclaim_source_buffers(Source())
            self.assertEqual(vc._shared_buffer_pool, [first, second])
        finally:
            vc._shared_buffer_pool = original_pool

    def test_worker_close_is_explicit_and_non_blocking(self):
        worker = vc.voice_chat_compression.__new__(vc.voice_chat_compression)
        worker.running = True
        worker.queue = queue.SimpleQueue()
        worker.close()
        self.assertFalse(worker.running)
        self.assertIsNone(worker.queue.get_nowait())


if __name__ == "__main__":
    unittest.main()
