"""Characterization tests for the music bot's network sender.

These pin the loop's pacing and its gates -- paused, crossfade-muted, a
megaphone slot owned by another performer, and the live-mix fill -- so the
move out of `streaming.py` cannot change what listeners receive.
"""

import queue
import unittest
from collections import deque
from types import SimpleNamespace
from unittest import mock


def _bot(**overrides):
    bot = SimpleNamespace(
        broadcast_enabled=True,
        broadcast_to_megaphone=False,
        party_sync_force_upload=False,
        cinema_force_upload=False,
        duck_multiplier=1.0,
        mic_pcm_queue=deque(),
        guitar_pcm_queue=deque(),
        external_pcm_queue=deque(),
        external_live_queue=deque(),
        party_room_mixer=None,
        _is_music_owner=lambda: True,
        _find_gameplay=lambda: None,
    )
    for name, value in overrides.items():
        setattr(bot, name, value)
    return bot


class _StoppingQueue(queue.Queue):
    """A queue that stops the loop after the first item it hands out."""

    def __init__(self, stream, items=()):
        super().__init__()
        self.stream = stream
        for item in items:
            self.put(item)

    def get(self, timeout=None):
        item = super().get(timeout=timeout)
        self.stream.running = False
        return item


class _EmptyQueue(queue.Queue):
    def get(self, timeout=None):
        raise queue.Empty


def _stream(bot=None, items=(), stop_on_get=False, **overrides):
    from libs.music_bot import streaming

    stream = streaming.AudioStreamer.__new__(streaming.AudioStreamer)
    stream.running = True
    stream.network_muted = False
    stream._network_mix = None
    stream.paused = False
    stream.bot = bot
    stream.last_send_time = None
    stream.network_queue = (
        _StoppingQueue(stream, items) if stop_on_get else queue.Queue()
    )
    if not stop_on_get:
        for item in items:
            stream.network_queue.put(item)
    stream.sends = []
    stream.claims = []

    def send(data, epoch=None, seq=None):
        stream.sends.append((data, epoch, seq))
        stream.running = False

    def claim():
        stream.claims.append(True)
        return (7, 11)

    stream._send_to_network_actual = send
    stream._claim_timeline_marker = claim
    for name, value in overrides.items():
        setattr(stream, name, value)
    return stream


def _run(stream):
    from libs.music_bot import network
    network.network_sender_loop(stream)


class PacingTests(unittest.TestCase):
    def test_a_small_overshoot_keeps_the_ideal_deadline(self):
        stream = _stream(items=[(b"frame", None, None)])
        stream.last_send_time = 1.000
        with mock.patch(
                "libs.music_bot.network.time.perf_counter",
                side_effect=[1.005, 1.020]), \
                mock.patch("libs.music_bot.network.time.sleep") as sleep:
            _run(stream)

        sleep.assert_called_once()
        self.assertAlmostEqual(sleep.call_args[0][0], 0.014, places=6)
        self.assertAlmostEqual(stream.last_send_time, 1.020, places=6)
        self.assertEqual(len(stream.sends), 1)

    def test_a_long_stall_starts_a_fresh_cadence(self):
        stream = _stream(items=[(b"frame", None, None)])
        stream.last_send_time = 1.000
        with mock.patch(
                "libs.music_bot.network.time.perf_counter",
                return_value=1.250), \
                mock.patch("libs.music_bot.network.time.sleep") as sleep:
            _run(stream)

        sleep.assert_not_called()
        self.assertAlmostEqual(stream.last_send_time, 1.250, places=6)

    def test_a_suspended_worker_does_not_burst(self):
        stream = _stream(items=[(b"frame", None, None)])
        stream.last_send_time = 1.000
        with mock.patch(
                "libs.music_bot.network.time.perf_counter",
                return_value=1.150), \
                mock.patch("libs.music_bot.network.time.sleep") as sleep:
            _run(stream)

        sleep.assert_not_called()
        self.assertAlmostEqual(stream.last_send_time, 1.150, places=6)


class GatingTests(unittest.TestCase):
    def test_paused_drops_the_queued_frame(self):
        stream = _stream(items=[(b"frame", None, None)], stop_on_get=True)
        stream.paused = True
        _run(stream)
        self.assertEqual(stream.sends, [])

    def test_a_muted_leg_sends_nothing(self):
        stream = _stream()
        stream.network_muted = True

        def stop_after_sleep(_seconds):
            stream.running = False

        with mock.patch(
                "libs.music_bot.network.time.sleep",
                side_effect=stop_after_sleep) as sleep:
            _run(stream)

        sleep.assert_called_once_with(0.05)
        self.assertEqual(stream.sends, [])

    def test_another_performers_pa_slot_keeps_the_music_private(self):
        bot = _bot(broadcast_to_megaphone=True,
                   _is_music_owner=lambda: False)
        stream = _stream(bot=bot, items=[(b"frame", None, None)],
                         stop_on_get=True)
        _run(stream)
        self.assertEqual(stream.sends, [])

    def test_live_input_creates_the_broadcast_when_no_song_frame_is_ready(self):
        bot = _bot(party_sync_force_upload=True,
                   external_live_queue=deque([b"live"]))
        stream = _stream(bot=bot)
        stream.network_queue = _EmptyQueue()
        _run(stream)

        self.assertEqual(len(stream.sends), 1)
        data, epoch, seq = stream.sends[0]
        self.assertEqual(data, b"\x00" * 3840)
        self.assertEqual((epoch, seq), (7, 11))

    def test_live_input_on_the_megaphone_leg_is_not_timeline_stamped(self):
        bot = _bot(broadcast_to_megaphone=True,
                   external_live_queue=deque([b"live"]))
        stream = _stream(bot=bot)
        stream.network_queue = _EmptyQueue()
        _run(stream)

        self.assertEqual(len(stream.sends), 1)
        self.assertEqual(stream.sends[0][1:], (None, None))


class CrossfadeTests(unittest.TestCase):
    def test_the_blend_ramps_the_outgoing_and_incoming_legs(self):
        partner = SimpleNamespace()
        partner.network_queue = queue.Queue()
        partner.network_queue.put((b"partner", None, None))
        stream = _stream(items=[(b"own", None, None)])
        stream._network_mix = (partner, 0.0, 2.0)
        calls = []

        def mix(own, other, own_gain, partner_gain):
            calls.append((own, other, own_gain, partner_gain))
            return b"blended"

        stream._mix_network_frames = mix
        with mock.patch(
                "libs.music_bot.network.time.monotonic",
                return_value=0.5):
            _run(stream)

        self.assertEqual(calls, [(b"own", b"partner", 0.75, 0.25)])
        self.assertEqual(stream.sends[0][0], b"blended")

    def test_the_leg_retires_when_the_window_elapses(self):
        partner = SimpleNamespace()
        partner.network_queue = queue.Queue()
        stream = _stream()
        stream._network_mix = (partner, 0.0, 2.0)

        def stop_after_sleep(_seconds):
            stream.running = False

        with mock.patch(
                "libs.music_bot.network.time.monotonic",
                return_value=3.0), \
                mock.patch(
                    "libs.music_bot.network.time.sleep",
                    side_effect=stop_after_sleep):
            _run(stream)

        self.assertTrue(stream.network_muted)
        self.assertIsNone(stream._network_mix)
        self.assertEqual(stream.sends, [])


if __name__ == "__main__":
    unittest.main()
