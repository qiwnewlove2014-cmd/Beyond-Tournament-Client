"""Relay ownership/pacing regressions with fake decoder and native sources."""

import gc
import struct
import sys
import threading
import time
from types import SimpleNamespace
import unittest
from unittest import mock
import weakref

from libs.jukebox_relay import JukeboxRelayReceiver
from libs import jukebox_relay


class Buffer:
    def __init__(self, owner, disposed):
        self.owner, self.disposed = owner, disposed
        self.data = b""

    def set_data(self, data, sample_rate, format):
        assert threading.get_ident() == self.owner
        self.data = bytes(data)

    def __del__(self):
        self.disposed.append(threading.get_ident())


class Source:
    def __init__(self, owner, position):
        self.owner = owner
        self._position = position
        self._gain = 1.0
        self._state = jukebox_relay.cyal.SourceState.STOPPED
        self.queued = []
        self.processed = 0
        self.play_calls = 0
        self.stop_calls = 0
        self.history = []
        self.fail_queue = False
        self.list_unqueue = False

    def check(self):
        assert threading.get_ident() == self.owner, "off-owner OpenAL access"

    @property
    def position(self):
        self.check()
        return self._position

    @property
    def gain(self):
        self.check()
        return self._gain

    @gain.setter
    def gain(self, value):
        self.check()
        self._gain = value

    @property
    def state(self):
        self.check()
        return self._state

    @property
    def buffers_queued(self):
        self.check()
        return len(self.queued)

    @property
    def buffers_processed(self):
        self.check()
        return self.processed

    def queue_buffers(self, buffer):
        self.check()
        if self.fail_queue:
            raise RuntimeError("fake queue failure")
        self.queued.append(buffer)
        self.history.append(buffer.data)

    def unqueue_buffers(self):
        self.check()
        if not self.processed:
            return None
        if self.list_unqueue:
            result = self.queued[:self.processed]
            del self.queued[:self.processed]
            self.processed = 0
            return result
        self.processed -= 1
        return self.queued.pop(0)

    def play(self):
        self.check()
        self.play_calls += 1
        self._state = jukebox_relay.cyal.SourceState.PLAYING

    def stop(self):
        self.check()
        self.stop_calls += 1
        self.processed = len(self.queued)
        self._state = jukebox_relay.cyal.SourceState.STOPPED

    def consume(self, frames=None):
        self.check()
        self.processed = len(self.queued) if frames is None else min(frames, len(self.queued))
        if self.processed == len(self.queued):
            self._state = jukebox_relay.cyal.SourceState.STOPPED


class RelayAudioTests(unittest.TestCase):
    def setUp(self):
        self.owner = threading.get_ident()
        self.now = [100.0]
        self.receivers = []
        self.releases = []
        self.decode_calls = []
        self.decoder_threads = []
        self.decoder_entered = threading.Event()
        self.block_decode = None
        self.init_error = False
        self.disposed = []
        self.buffer_refs = []
        self.allocations = 0
        self.allocation_cost = 0.0
        test = self

        class Decoder:
            def __init__(self):
                test.decoder_threads.append(threading.get_ident())
                if test.init_error:
                    raise RuntimeError("fake decoder unavailable")

            def set_channels(self, value):
                test.assertEqual(value, 2)
                test.assertNotEqual(threading.get_ident(), test.owner)

            def set_sampling_frequency(self, value):
                test.assertEqual(value, 48000)

            def decode(self, payload):
                test.decode_calls.append(bytes(payload))
                test.decoder_entered.set()
                if test.block_decode is not None:
                    test.block_decode.wait(2)
                value = payload[0]
                return struct.pack("<hh", value, -value)

        self.decoder_patch = mock.patch.dict(sys.modules, {
            "pyogg": SimpleNamespace(OpusDecoder=Decoder),
        })
        self.decoder_patch.start()

    def tearDown(self):
        for event in self.releases:
            event.set()
        for receiver in self.receivers:
            receiver.stop()
        for receiver in self.receivers:
            if receiver.ident is not None:
                receiver.join(timeout=2)
                self.assertFalse(receiver.is_alive())
        self.decoder_patch.stop()

    def new(self, **kwargs):
        def gen_buffer():
            self.assertEqual(threading.get_ident(), self.owner)
            self.allocations += 1
            self.now[0] += self.allocation_cost
            buffer = Buffer(self.owner, self.disposed)
            self.buffer_refs.append(weakref.ref(buffer))
            return buffer

        left = Source(self.owner, (-2.5, 0, 0))
        right = Source(self.owner, (2.5, 0, 0))
        self.registered = []
        self.audio = SimpleNamespace(context=SimpleNamespace(gen_buffer=gen_buffer),
                                     position=(0, 0, 0), volume_categories={"jukebox": [100]},
                                     register_jukebox_receiver=self.registered.append,
                                     efx=None)
        self.player = SimpleNamespace(occlusion_tier=mock.Mock(return_value=0))
        game = SimpleNamespace(audio_mngr=self.audio)
        receiver = JukeboxRelayReceiver(game, left, right, 100, 1, 2, 8, 40,
                                        box_pos=(0, 0, 0), player=self.player,
                                        clock=lambda: self.now[0], **kwargs)
        self.receivers.append(receiver)
        return receiver, left, right

    def wait(self, condition):
        deadline = time.monotonic() + 2
        while not condition():
            if time.monotonic() >= deadline:
                self.fail("fake relay worker did not complete")
            threading.Event().wait(0.001)

    def send(self, receiver, values):
        for value in values:
            self.assertTrue(receiver.receive(value, bytes([value & 255])))

    def test_constructor_is_cheap_and_registers_without_touching_decoder_or_AL(self):
        receiver, left, right = self.new()
        self.assertEqual(self.allocations, 0)
        self.assertEqual(self.decoder_threads, [])
        self.assertEqual(self.registered, [receiver])
        self.assertEqual(receiver._pool, [])
        self.assertIsInstance(receiver._started, threading.Event)
        self.assertTrue(receiver.main_thread_audio)

    def test_worker_only_decodes_and_owner_preserves_intro_and_stereo_channels(self):
        receiver, left, right = self.new()
        self.send(receiver, range(1, 5))
        receiver.start()
        self.wait(lambda: receiver._pcm_frames.qsize() == 4)
        self.assertEqual(self.allocations, 0)
        self.assertEqual(left.history, [])
        self.assertNotEqual(self.decoder_threads[0], self.owner)
        self.assertEqual(receiver.pump_audio(), 2)
        self.assertFalse(receiver._play_started)
        self.assertEqual(receiver.pump_audio(), 2)
        self.assertTrue(receiver._play_started)
        self.assertEqual([struct.unpack("<h", b)[0] for b in left.history], [1, 2, 3, 4])
        self.assertEqual([struct.unpack("<h", b)[0] for b in right.history], [-1, -2, -3, -4])
        self.assertEqual((left.play_calls, right.play_calls), (1, 1))
        self.assertEqual(receiver.last_audio_activity, self.now[0])

    def test_incremental_fixed_pool_stops_at_32_and_recycles(self):
        receiver, left, right = self.new()
        for index in range(12):
            before = self.allocations
            receiver.pump_audio(max_new_buffers=3)
            self.assertLessEqual(self.allocations - before, 3)
        self.assertEqual(self.allocations, 32)
        receiver.start()
        for group in range(4):
            self.send(receiver, range(1 + 4 * group, 5 + 4 * group))
            self.wait(lambda: receiver._pcm_frames.qsize() == 4)
            self.assertEqual(receiver.pump_audio(), 4)
            left.consume()
            right.consume()
            right.list_unqueue = True
            receiver.pump_audio()
        self.assertEqual(self.allocations, 32)
        self.assertEqual(len(receiver._pool), 32)
        self.assertEqual(receiver.last_output_at, self.now[0])

    def test_deadline_and_frame_caps_are_checked_between_work(self):
        receiver, left, right = self.new()
        self.allocation_cost = 0.003
        receiver.pump_audio(deadline=self.now[0] + 0.002, max_new_buffers=4)
        self.assertEqual(self.allocations, 1)
        receiver.pump_audio(deadline=self.now[0])
        self.assertEqual(self.allocations, 1)
        self.allocation_cost = 0
        receiver.pump_audio(max_new_buffers=31)
        receiver.start()
        self.send(receiver, range(1, 5))
        self.wait(lambda: receiver._pcm_frames.qsize() == 4)
        self.assertEqual(receiver.pump_audio(max_frames=1), 1)
        self.assertEqual(receiver._pcm_frames.qsize(), 3)

    def test_underrun_requires_three_new_frames_before_resume(self):
        receiver, left, right = self.new()
        receiver.pump_audio(max_new_buffers=32)
        receiver.start()
        self.send(receiver, range(1, 5))
        self.wait(lambda: receiver._pcm_frames.qsize() == 4)
        receiver.pump_audio()
        left.consume()
        right.consume()
        receiver.pump_audio()
        self.send(receiver, [5, 6])
        self.wait(lambda: receiver._pcm_frames.qsize() == 2)
        receiver.pump_audio()
        self.assertEqual(left.play_calls, 1)
        self.send(receiver, [7])
        self.wait(lambda: receiver._pcm_frames.qsize() == 1)
        receiver.pump_audio()
        self.assertEqual((left.play_calls, right.play_calls), (2, 2))

    def test_raw_and_pcm_queues_are_bounded_and_backlog_is_shed(self):
        receiver, left, right = self.new()
        self.send(receiver, range(1, 81))
        self.assertEqual(receiver.frames.qsize(), 32)
        receiver.start()
        self.wait(lambda: receiver._pcm_frames.qsize() == 32)
        self.assertEqual(receiver._pcm_frames.qsize(), 32)
        receiver.pump_audio(max_new_buffers=32, max_frames=4)
        for _ in range(10):
            receiver.pump_audio(max_frames=4)
        self.assertLessEqual(left.buffers_queued, receiver.MAX_QUEUED_BUFFERS)
        self.assertLessEqual(right.buffers_queued, receiver.MAX_QUEUED_BUFFERS)
        self.assertEqual(receiver._pcm_frames.qsize(), 0)
        self.assertEqual(self.allocations, 32)

    def test_sequence_duplicates_old_packets_invalid_flags_and_wrap(self):
        receiver, left, right = self.new()
        self.assertTrue(receiver.receive(65535, b"a"))
        self.assertFalse(receiver.receive(65535, b"a"))
        self.assertFalse(receiver.receive(65534, b"a"))
        self.assertTrue(receiver.receive(0, b"b"))
        self.assertFalse(receiver.receive(1, b"b", flags=4))
        self.assertFalse(receiver.receive(1, b""))
        self.assertFalse(receiver.receive(1, b"x" * 1276))
        self.assertEqual(receiver.received_frames, 2)

    def test_reset_generation_discards_inflight_decode(self):
        receiver, left, right = self.new()
        receiver.pump_audio(max_new_buffers=32)
        self.block_decode = threading.Event()
        self.releases.append(self.block_decode)
        receiver.start()
        self.send(receiver, [1])
        self.assertTrue(self.decoder_entered.wait(1))
        self.assertTrue(receiver.receive(2, b"\x02", flags=1))
        self.block_decode.set()
        self.wait(lambda: receiver._pcm_frames.qsize() == 1)
        receiver.pump_audio()
        self.assertEqual(left.history, [struct.pack("<h", 2)])
        self.assertEqual(receiver._audio_generation, 1)
        self.assertFalse(receiver._play_started)

    def test_generation_reset_preserves_healthy_queued_audio(self):
        receiver, left, right = self.new()
        receiver.pump_audio(max_new_buffers=32)
        receiver.start()
        self.send(receiver, range(1, 5))
        self.wait(lambda: receiver._pcm_frames.qsize() == 4)
        receiver.pump_audio()
        self.assertTrue(receiver._play_started)
        self.assertTrue(receiver.receive(5, b"\x05", flags=1))
        self.wait(lambda: receiver._pcm_frames.qsize() == 1)
        receiver.pump_audio()
        self.assertEqual((left.stop_calls, right.stop_calls), (0, 0))
        self.assertEqual((left.play_calls, right.play_calls), (1, 1))
        self.assertEqual(left.buffers_queued, 5)
        self.assertTrue(receiver._play_started)
        receiver._reset_queue()
        receiver.pump_audio()
        self.assertEqual(left.stop_calls, 0)
        self.assertEqual(left.buffers_queued, 5)

    def test_stop_during_blocked_decode_never_waits_or_leaves_native_refs(self):
        receiver, left, right = self.new()
        receiver.pump_audio(max_new_buffers=32)
        self.block_decode = threading.Event()
        self.releases.append(self.block_decode)
        receiver.start()
        self.send(receiver, [1])
        self.assertTrue(self.decoder_entered.wait(1))
        receiver.stop()
        self.assertFalse(self.block_decode.is_set())
        self.assertTrue(receiver.is_alive())
        self.assertEqual(receiver._pool, [])
        self.assertEqual(receiver._all_buffers, [])
        self.assertIsNone(receiver.source_l)
        self.assertIsNone(receiver.game)
        self.assertFalse(receiver.receive(2, b"b"))
        self.assertEqual(receiver.pump_audio(), 0)
        self.block_decode.set()
        receiver.join(1)
        self.assertEqual(receiver._pcm_frames.qsize(), 0)
        gc.collect()
        self.assertEqual(len(self.disposed), 32)
        self.assertEqual(set(self.disposed), {self.owner})

    def test_fade_runs_on_owner_and_cleanup_exactly_once(self):
        receiver, left, right = self.new(cabinet_volume=50)
        receiver.pump_audio()
        self.assertEqual(left.gain, 0.5)
        calls = []
        receiver.retire(duration=0.5,
                        cleanup_callback=lambda: calls.append((threading.get_ident(), receiver.game)))
        self.now[0] += 0.25
        receiver.pump_audio()
        self.assertAlmostEqual(left.gain, 0.25)
        receiver.set_volume(50)
        self.assertAlmostEqual(left.gain, 0.125)
        self.now[0] += 0.25
        receiver.pump_audio()
        receiver.stop()
        receiver.pump_audio()
        self.assertEqual(calls, [(self.owner, None)])

    def test_explicit_stop_finishes_retire_cleanup_immediately(self):
        receiver, left, right = self.new()
        cleanup = mock.Mock()
        receiver.retire(cleanup_callback=cleanup)
        receiver.stop()
        receiver.stop()
        cleanup.assert_called_once_with()

    def test_gain_preserves_categories_cabinet_and_distance_but_skips_far_occlusion(self):
        receiver, left, right = self.new(cabinet_volume=50)
        self.audio.volume_categories["jukebox"][0] = 50
        receiver.set_volume(80)
        self.assertAlmostEqual(left.gain, 0.2)
        self.player.occlusion_tier.assert_called_with((0, 0, 0), (0, 0, 0), 40.0)
        self.player.occlusion_tier.reset_mock()
        self.audio.position = (100, 0, 0)
        receiver.pump_audio()
        self.assertEqual(left.gain, 0)
        self.assertEqual(right.gain, 0)
        self.player.occlusion_tier.assert_not_called()

    def test_off_owner_audio_methods_fail_but_receive_is_network_safe(self):
        receiver, left, right = self.new()
        errors = []

        def network():
            self.assertTrue(receiver.receive(1, b"a"))
            for action in (receiver.pump_audio, receiver.stop, receiver.retire,
                           lambda: receiver.set_volume(50), receiver._update_gain):
                try:
                    action()
                except RuntimeError:
                    errors.append(True)

        thread = threading.Thread(target=network)
        thread.start()
        thread.join(1)
        self.assertEqual(len(errors), 5)
        self.assertEqual(self.allocations, 0)

    def test_partial_channel_queue_failure_does_not_desynchronize_pair(self):
        receiver, left, right = self.new()
        receiver.pump_audio(max_new_buffers=8)
        receiver.start()
        self.send(receiver, [1])
        self.wait(lambda: receiver._pcm_frames.qsize() == 1)
        right.fail_queue = True
        self.assertEqual(receiver.pump_audio(max_new_buffers=0), 0)
        self.assertEqual(left.buffers_queued, 0)
        self.assertEqual(right.buffers_queued, 0)
        self.assertEqual(len(receiver._pool), 8)

    def test_decoder_initialization_failure_is_cleaned_by_owner(self):
        receiver, left, right = self.new()
        self.init_error = True
        receiver.start()
        receiver.join(1)
        self.assertFalse(receiver.running)
        self.assertEqual(receiver.failure_reason, "relay decoder initialization failed")
        receiver.pump_audio()
        self.assertIsNone(receiver.source_l)
        self.assertEqual(self.allocations, 0)

    def test_repeated_initial_buffer_allocation_failure_is_reported(self):
        receiver, left, right = self.new()
        self.audio.context.gen_buffer = mock.Mock(side_effect=RuntimeError("fake exhausted driver"))
        for _ in range(3):
            receiver.pump_audio()
        self.assertEqual(receiver.failure_reason, "relay initial audio buffer allocation failed")
        self.assertEqual(receiver._allocated_buffers, 0)


if __name__ == "__main__":
    unittest.main()
