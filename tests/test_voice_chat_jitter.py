"""Unit tests for the megaphone jitter buffer (music/voice broadcast smoothness)."""

import sys
import os
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from libs.voice_chat import MegaphoneJitterBuffer


class FakeGame:
    pass


def frame(n=0):
    return bytes([n % 256]) * MegaphoneJitterBuffer.FRAME_SIZE


class TestMegaphoneJitterBuffer(unittest.TestCase):
    def setUp(self):
        self.jb = MegaphoneJitterBuffer(FakeGame())

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

    def test_should_output_limits_to_one_frame_per_20ms(self):
        # The megaphone receive path pops on the 20ms wall-clock (should_output)
        # instead of once per packet, so a burst of packets stays buffered and
        # plays out at the steady cadence instead of jittering the PA.
        self.assertTrue(self.jb.should_output())  # first call always True
        self.assertFalse(self.jb.should_output())  # within same 20ms window
        self.assertFalse(self.jb.should_output())

    def test_clock_gated_steady_stream_never_underruns(self):
        # Simulate the receive loop: add packets as they arrive, pop only when
        # the output clock says a frame is due. A burst of 3 arrivals then a
        # pause must drain at one frame per 20ms and never underrun mid-burst.
        import time as _time
        # Pre-buffer enough to start playback.
        for i in range(MegaphoneJitterBuffer.PRE_BUFFER_FRAMES + 1):
            self.jb.add_packet(frame(i))
        # Drain one frame now (clock allows).
        self.assertTrue(self.jb.should_output())
        self.assertIsNotNone(self.jb.get_packet())
        # Burst: three frames arrive together; only one may be emitted now.
        for i in range(3):
            self.jb.add_packet(frame(i))
        self.assertFalse(self.jb.should_output())
        # After 20ms, the next frame is due and there is buffered audio.
        self.jb.last_output_time = _time.time() * 1000 - MegaphoneJitterBuffer.FRAME_DURATION_MS - 1
        self.assertTrue(self.jb.should_output())
        self.assertIsNotNone(self.jb.get_packet())
        self.assertFalse(self.jb._underrun)


if __name__ == "__main__":
    unittest.main()
