import audioop
import struct
import threading
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from libs.music_bot.party_room import (
    FRAME_BYTES, MIXER_PREBUFFER_FRAMES, RECEIVER_PREBUFFER_FRAMES,
    PartyRoomJitter, PartyRoomMixer, PartyRoomReceiver,
    pack_room_packet, parse_room_packet,
)
from libs.party_sync import PartySyncState, parse_state


def frame(sample, ms=20):
    mono = struct.pack("<h", sample) * (48000 * ms // 1000)
    return audioop.tostereo(mono, 2, 1.0, 1.0)


def decoded(payload):
    return b"P" + bytes(payload)


class TestPartyRoomMix(unittest.TestCase):
    def _bare_mixer(self):
        # Construction starts a thread and an Opus encoder; the queueing half
        # is what these tests pin, so build only the fields push() uses.
        mixer = object.__new__(PartyRoomMixer)
        mixer.running = True
        mixer._lock = threading.Lock()
        mixer._buffers = {}
        mixer._pushed_at = {}
        mixer._primed = False
        return mixer

    def test_two_sources_are_summed_at_equal_gain(self):
        mixed = PartyRoomMixer.mix_frames([frame(1000), frame(3000)])
        self.assertEqual(len(mixed), FRAME_BYTES)
        self.assertEqual(struct.unpack("<h", mixed[:2])[0], 2000)

    def test_one_source_keeps_its_level(self):
        mixed = PartyRoomMixer.mix_frames([frame(1234)])
        self.assertEqual(struct.unpack("<h", mixed[:2])[0], 1234)

    def test_short_frame_is_padded_with_silence(self):
        mixed = PartyRoomMixer.mix_frames([struct.pack("<h", 7) * 10])
        self.assertEqual(len(mixed), FRAME_BYTES)
        self.assertEqual(struct.unpack("<h", mixed[:2])[0], 7)
        self.assertEqual(mixed[-2:], b"\x00\x00")

    def test_a_silent_source_does_not_lower_the_others(self):
        mixed = PartyRoomMixer.mix_frames([frame(1000), b"\x00" * FRAME_BYTES])
        self.assertEqual(struct.unpack("<h", mixed[:2])[0], 1000)

    def test_a_mono_source_is_duplicated_to_both_sides(self):
        mixer = self._bare_mixer()
        mono = struct.pack("<h", 1200) * 960       # 10 ms mono
        mixer.push("guest", mono, stereo=False)
        buffered = bytes(mixer._buffers["guest"])
        self.assertEqual(len(buffered), FRAME_BYTES)
        self.assertEqual(audioop.tomono(buffered, 2, 0.5, 0.5)[:2],
                         struct.pack("<h", 1200))

    def test_stop_makes_later_frames_ignored(self):
        mixer = self._bare_mixer()
        mixer.running = False
        mixer.push("guest", frame(1000), stereo=True)
        self.assertNotIn("guest", mixer._buffers)

    def test_a_partial_10ms_frame_is_held_not_dropped(self):
        mixer = self._bare_mixer()
        mixer.push("guest", frame(1000, ms=10), stereo=True)
        first = mixer._take_frame("guest", time.monotonic())
        self.assertEqual(first, b"\x00" * FRAME_BYTES)
        self.assertEqual(len(mixer._buffers["guest"]), FRAME_BYTES // 2)
        mixer.push("guest", frame(1000, ms=10), stereo=True)
        second = mixer._take_frame("guest", time.monotonic())
        self.assertEqual(struct.unpack("<h", second[:2])[0], 1000)

    def test_the_mixer_waits_for_its_starting_cushion(self):
        mixer = self._bare_mixer()
        for _ in range(MIXER_PREBUFFER_FRAMES - 1):
            mixer.push("guest", frame(1000, ms=20), stereo=True)
        self.assertFalse(mixer._ready_to_start(time.monotonic()))
        mixer.push("guest", frame(1000, ms=20), stereo=True)
        self.assertTrue(mixer._ready_to_start(time.monotonic()))


class TestPartyRoomPacing(unittest.TestCase):
    def test_a_slow_encoder_keeps_the_20ms_send_cadence(self):
        """A 15 ms encode must not turn every frame into a 31 ms frame.

        On Windows the short sleep left after encoding can overshoot to the
        system timer's 15.6 ms tick. The mixer therefore waits for an ideal
        deadline, not for "sleep just ended". This test uses a deliberately
        slow fake encoder so the remaining sleep is the dangerous short one.
        """
        class SlowEncoder:
            def encode(self, data):
                time.sleep(0.015)
                return b"x"

        class Network:
            def __init__(self):
                self.times = []

            def send(self, *args):
                self.times.append(time.monotonic())

        network = Network()
        mixer = object.__new__(PartyRoomMixer)
        threading.Thread.__init__(mixer, daemon=True)
        mixer.game = SimpleNamespace(network=network)
        mixer.room_key = 1033
        mixer.cabinet = "cab"
        mixer.running = True
        mixer._lock = threading.Lock()
        mixer._buffers = {}
        mixer._pushed_at = {}
        mixer._primed = False
        mixer.epoch = 1
        mixer.sequence = 0
        mixer.sent_frames = 0
        mixer.encode_errors = 0
        mixer._trace_last_send = None
        mixer._trace_sent = 0
        mixer._trace_last_idle = 0.0
        mixer._trace_started_at = time.monotonic()
        mixer.encoder = SlowEncoder()

        stop = threading.Event()
        payload = struct.pack("<h", 100) * (FRAME_BYTES // 2)

        def produce():
            while not stop.is_set():
                mixer.push("host", payload, stereo=True)
                time.sleep(0.010)

        producer = threading.Thread(target=produce, daemon=True)
        producer.start()
        mixer.start()
        try:
            time.sleep(0.7)
        finally:
            stop.set()
            mixer.running = False
            mixer.join(timeout=2.0)
            producer.join(timeout=1.0)

        gaps = [(b - a) * 1000.0 for a, b in zip(network.times, network.times[1:])]
        self.assertGreaterEqual(len(gaps), 20)
        self.assertLess(sorted(gaps)[len(gaps) // 2], 28.0)


class TestPartyRoomStreamHeader(unittest.TestCase):
    def test_header_carries_version_epoch_and_sequence(self):
        packet = pack_room_packet(1001, 7, 0x1234, b"opus")
        self.assertEqual(len(packet), 7 + 4)
        self.assertEqual(parse_room_packet(packet), (1001, 7, 0x1234, b"opus"))

    def test_a_previous_headerless_packet_is_still_readable(self):
        key, epoch, seq, opus = parse_room_packet(b"\x03\xe9opus")
        self.assertEqual((key, epoch, seq, opus), (1001, 0, None, b"opus"))


class TestPartyRoomJitter(unittest.TestCase):
    def test_prebuffer_orders_the_first_frames(self):
        jitter = PartyRoomJitter(prebuffer=3)
        out = []
        for index, payload in enumerate((b"a", b"b", b"c")):
            out.extend(jitter.push(7, index, payload, decoded))
        self.assertEqual(out, [b"Pa", b"Pb", b"Pc"])
        self.assertEqual(jitter.expected, 3)

    def test_a_late_packet_is_dropped_instead_of_reopening_the_sequence(self):
        jitter = PartyRoomJitter(prebuffer=1)
        jitter.push(7, 5, b"a", decoded)
        self.assertEqual(jitter.push(7, 4, b"b", decoded), [])

    def test_reordering_is_restored_without_concealment(self):
        jitter = PartyRoomJitter(prebuffer=1)
        jitter.push(7, 0, b"a", decoded)
        self.assertEqual(jitter.push(7, 2, b"c", decoded), [])
        self.assertEqual(jitter.push(7, 1, b"b", decoded), [b"Pb", b"Pc"])

    def test_a_hole_is_held_then_concealed_with_the_last_frame(self):
        jitter = PartyRoomJitter(prebuffer=1)
        out = list(jitter.push(7, 0, b"a", decoded))
        for seq, payload in ((2, b"c"), (3, b"d"), (4, b"e"), (5, b"f")):
            out.extend(jitter.push(7, seq, payload, decoded))
        self.assertEqual(out, [b"Pa", b"Pa", b"Pc", b"Pd", b"Pe", b"Pf"])
        self.assertEqual(jitter.expected, 6)

    def test_sequence_wraps_at_16_bits(self):
        jitter = PartyRoomJitter(prebuffer=1)
        self.assertEqual(jitter.push(7, 0xFFFF, b"a", decoded), [b"Pa"])
        self.assertEqual(jitter.push(7, 0, b"b", decoded), [b"Pb"])

    def test_a_new_epoch_resets_sequence_and_history(self):
        jitter = PartyRoomJitter(prebuffer=1)
        jitter.push(1, 100, b"a", decoded)
        self.assertEqual(jitter.push(2, 0, b"b", decoded), [b"Pb"])
        self.assertEqual(jitter.epoch, 2)
        self.assertEqual(jitter.expected, 1)

    def test_a_gap_waits_for_evidence_then_fills_the_whole_hole(self):
        jitter = PartyRoomJitter(prebuffer=1)
        jitter.push(7, 0, b"a", decoded)
        out = []
        for seq, payload in ((4, b"e"), (5, b"f"), (6, b"g"), (7, b"h")):
            out.extend(jitter.push(7, seq, payload, decoded))
        self.assertEqual(out[:3], [b"Pa", b"Pa", b"Pa"])
        self.assertEqual(out[3:], [b"Pe", b"Pf", b"Pg", b"Ph"])

    def test_one_callback_cannot_flood_the_room_with_repeats(self):
        jitter = PartyRoomJitter(prebuffer=1)
        jitter.push(7, 0, b"a", decoded)
        out = []
        for seq in range(20, 24):
            out.extend(jitter.push(7, seq, bytes([65 + seq]), decoded))
        self.assertEqual(len(out), 5)
        self.assertEqual(out[0], b"Pa")
        self.assertEqual(jitter.expected, 24)


class TestCinemaState(unittest.TestCase):
    def _payload(self, **extra):
        payload = {
            "session_id": "Alice:1",
            "host": {"name": "Alice", "voice_channel": 30},
            "guests": [{"name": "Carol", "voice_channel": 31}],
            "max_guests": 8,
            "jammers": ["CarOl"],
        }
        payload.update(extra)
        return payload

    def test_state_parses_the_cinema_link(self):
        parsed = parse_state(self._payload(
            cinema_cabinet="cab1", cinema_room_key=1030,
        ))
        self.assertEqual(parsed["cinema_cabinet"], "cab1")
        self.assertEqual(parsed["cinema_room_key"], 1030)

    def test_a_cabinet_without_a_key_is_not_announced(self):
        parsed = parse_state(self._payload(cinema_cabinet="cab1"))
        self.assertEqual(parsed["cinema_cabinet"], "cab1")
        self.assertEqual(parsed["cinema_room_key"], 0)

    def test_state_mirror_applies_and_clears_the_link(self):
        ps = PartySyncState()
        ps.apply_state(self._payload(
            cinema_cabinet="cab1", cinema_room_key=1030,
        ), "Alice")
        self.assertEqual(ps.role, "host")
        self.assertEqual(ps.cinema_cabinet, "cab1")
        self.assertEqual(ps.cinema_room_key, 1030)
        ps.end_session()
        self.assertEqual(ps.cinema_cabinet, "")
        self.assertEqual(ps.cinema_room_key, 0)


class TestRoomReceiver(unittest.TestCase):
    class Feed:
        def __init__(self):
            self.frames = []

        def push(self, pcm, stereo=False):
            self.frames.append(pcm)
            return True

    def _receiver(self):
        receiver = PartyRoomReceiver(None)
        receiver.decode = lambda key, opus: decoded(opus)
        return receiver

    def test_eight_frames_prime_the_room_before_any_audio_is_queued(self):
        feed = self.Feed()
        receiver = self._receiver()
        with mock.patch("libs.audio.cinema.peer.route", return_value=feed):
            for index in range(RECEIVER_PREBUFFER_FRAMES):
                accepted = receiver.play(1001, 7, index, bytes([65 + index]))
            self.assertTrue(accepted)
        self.assertEqual(feed.frames,
                         [decoded(bytes([65 + index]))
                          for index in range(RECEIVER_PREBUFFER_FRAMES)])

    def test_no_room_discards_the_stream_state(self):
        receiver = self._receiver()
        with mock.patch("libs.audio.cinema.peer.route", return_value=None):
            self.assertFalse(receiver.play(1001, 7, 0, b"a"))
        self.assertNotIn(1001, receiver._jitter)

    def test_a_missing_frame_is_filled_and_the_next_frames_keep_order(self):
        feed = self.Feed()
        receiver = self._receiver()
        with mock.patch("libs.audio.cinema.peer.route", return_value=feed):
            for index in range(RECEIVER_PREBUFFER_FRAMES):
                receiver.play(1001, 7, index, bytes([65 + index]))
            # Sequence 8 is lost. Four later packets are the evidence that it
            # is a hole, not a reordered packet, so it is repeated.
            for seq, payload in ((9, b"I"), (11, b"K"), (12, b"L"),
                                 (13, b"M"), (14, b"N")):
                receiver.play(1001, 7, seq, payload)
        self.assertEqual(feed.frames[8], decoded(b"H"))
        self.assertEqual(feed.frames[9], decoded(b"I"))
        self.assertEqual(feed.frames[10], decoded(b"I"))
        self.assertEqual(feed.frames[11], decoded(b"K"))


if __name__ == "__main__":
    unittest.main()
