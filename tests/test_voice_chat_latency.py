"""What live voice costs: the receive cushion, pinned.

Live voice (channel 20) is the one path whose latency this code chooses. Every
other link is the machine's (the capture endpoint hands audio over in 10 ms
periods, the mixer takes it back in 10 ms periods -- measured, see
``tools/instrument_monitor_latency_sim.py``) or the network's. The choice is
the silence cushion the receive path puts ahead of a burst: N x 20 ms, paid on
every frame of that burst, bought to absorb packet-arrival jitter.

It is the normal voice path's cushion only. The megaphone/PA path has its own
fixed six-frame reserve (``_megaphone_margin_frames``) behind a two-frame start
gate, because the server's PA leg is reliable and may pause for a
retransmission; the two must not drift back into one shared number, and neither
may the PA's gate and reserve (see ``tools/megaphone_latency_sim.py``).

The whole budget, with what each link is worth in ms, is printed by
``tools/voice_latency_sim.py`` (it drives this same ``recieve2`` against a
time-modelled source); this file pins the lever itself so a future edit that
raises the floor shows up as a failure rather than as 20 ms nobody notices.
"""
import os
import sys
import unittest
from types import SimpleNamespace

import cyal

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from libs import voice_chat as vc

FRAME_BYTES = vc.VOICE_FRAME_BYTES


class FakeBuffer:
    def __init__(self):
        self.queued_data = None

    def set_data(self, data, *args, **kwargs):
        self.queued_data = bytes(data)


class FakeSource:
    """The parts of ``cyal.Source`` the receive path touches."""

    def __init__(self):
        self.state = cyal.SourceState.STOPPED
        self.buffers_queued = 0
        self.buffers_processed = 0
        self.queued = []

    def queue_buffers(self, buf):
        self.buffers_queued += 1
        self.queued.append(buf.queued_data)

    def unqueue_buffers(self):
        if self.buffers_processed > 0:
            self.buffers_processed -= 1
            self.buffers_queued -= 1
            return FakeBuffer()
        return None

    def play(self):
        self.state = cyal.SourceState.PLAYING


class FakeContext:
    def gen_buffer(self):
        return FakeBuffer()


class FakeAudioManager:
    def __init__(self):
        self.context = FakeContext()

    def defer_audio(self, fn):
        fn()


class FakeGameplay:
    def __init__(self):
        self.player = SimpleNamespace(dead=False, has_radio=False)
        self.voice_channels = {}
        self.megaphone = None


def make_compression():
    comp = vc.voice_chat_compression.__new__(vc.voice_chat_compression)
    comp.game = SimpleNamespace(audio_mngr=FakeAudioManager())
    comp.decoder = SimpleNamespace(
        decode=lambda payload: bytes([payload[0]]) * FRAME_BYTES)
    comp.channel = 20
    return comp


def silence_frames(source):
    return sum(1 for data in source.queued
               if len(data) == FRAME_BYTES and not any(data))


def reset_state():
    vc._voice_last_pkt.clear()
    vc._speaker_jitter_ms.clear()
    vc._speaker_jitter_ts.clear()


def feed(comp, source, gameplay, t):
    vc.time.time = lambda: t
    comp.recieve2(bytearray([1]), source, None, 20, gameplay)


class TheCushionTests(unittest.TestCase):
    def setUp(self):
        reset_state()
        self.orig_time = vc.time.time
        self.addCleanup(self._restore)

    def _restore(self):
        vc.time.time = self.orig_time

    def test_a_clean_burst_pays_one_frame_twenty_ms(self):
        """The cold-start cushion is ONE 20 ms frame, not two (40 ms)."""
        source = FakeSource()
        comp = make_compression()
        feed(comp, source, FakeGameplay(), 0.0)
        self.assertEqual(silence_frames(source), 1)

    def test_a_running_stream_is_never_re_padded(self):
        """Frames after the burst queue 1:1: the cushion is paid once."""
        source = FakeSource()
        comp = make_compression()
        gameplay = FakeGameplay()
        feed(comp, source, gameplay, 0.0)
        for i in range(1, 6):
            feed(comp, source, gameplay, i * 0.020)
        self.assertEqual(silence_frames(source), 1)
        self.assertEqual(len(source.queued), 7)      # 1 cushion + 6 frames

    def test_a_pause_does_not_inflate_the_next_burst(self):
        """A >180 ms gap is a new sentence, not 300 ms of measured jitter."""
        vc._speaker_jitter_ms["vc:20"] = 0.0
        source = FakeSource()
        comp = make_compression()
        gameplay = FakeGameplay()
        feed(comp, source, gameplay, 0.0)
        feed(comp, source, gameplay, 0.020)
        feed(comp, source, gameplay, 0.400)      # a pause, then a new burst
        self.assertEqual(vc._adaptive_margin_frames("vc:20"), 1)

    def test_measured_jitter_buys_a_deeper_cushion(self):
        vc._speaker_jitter_ms["vc:20"] = 40.0
        self.assertEqual(vc._adaptive_margin_frames("vc:20"), 3)
        vc._speaker_jitter_ms["vc:20"] = 500.0
        self.assertEqual(vc._adaptive_margin_frames("vc:20"), 6)


class TheMarginMathTests(unittest.TestCase):
    def setUp(self):
        reset_state()

    def test_floor_is_one_frame(self):
        vc._speaker_jitter_ms["x"] = 0.0
        self.assertEqual(vc._adaptive_margin_frames("x"), 1)

    def test_an_unknown_sender_gets_the_floor(self):
        vc._speaker_jitter_ms.pop("never_seen", None)
        self.assertEqual(vc._adaptive_margin_frames("never_seen"), 1)

    def test_cap_is_six_frames(self):
        vc._speaker_jitter_ms["x"] = 5000.0
        self.assertEqual(vc._adaptive_margin_frames("x"), 6)

    def test_the_pa_path_keeps_its_own_fixed_reserve(self):
        """The megaphone's reliable listener leg needs its six-frame reserve;
        the normal voice cushion must not be read from it, or the PA and live
        voice drift into one number again."""
        self.assertEqual(vc._megaphone_margin_frames("x"), 6)


class TheFrameConstantsTests(unittest.TestCase):
    """The numbers the latency budget is built from, in one place."""

    def test_a_frame_is_twenty_ms_of_48k_mono16(self):
        self.assertEqual(vc.VOICE_FRAME_BYTES, 1920)
        self.assertEqual(vc.VOICE_FRAME_BYTES // 2, 960)          # samples
        self.assertEqual(vc.VOICE_FRAME_MS, 960 / 48.0)

    def test_the_capture_trigger_is_one_device_delivery_period(self):
        # 480 samples = 10 ms, the measured period the capture endpoint hands
        # audio over in (tools/instrument_monitor_latency_sim.py).
        self.assertEqual(vc.VOICE_CAPTURE_TRIGGER_SAMPLES, 480)

    def test_the_send_worker_poll_is_the_shipped_number(self):
        self.assertAlmostEqual(vc.VOICE_SEND_POLL_S, 0.002)


if __name__ == "__main__":
    unittest.main()
