"""What the performer's own ear gets, and how soon it gets it.

The line-in path has two consumers with different tolerances. Everything that
*leaves* the machine -- the voice channel, the megaphone, the music bot mix, the
pitch tracker -- is a 20 ms frame, the chunk Opus and the voice path have always
used. The player's own monitor cannot afford to wait for one: the capture
device hands audio over 10 ms at a time (measured; see
``tools/instrument_monitor_latency_sim.py``), so a monitor fed in 20 ms frames
was holding delay the player hears on every string.

The other property pinned here is the queue. The capture arrives at exactly the
rate the source plays, so a stalled main thread used to leave frames waiting
that were still being heard that much later for the rest of the session: the
ear never catches up on its own. Past the limit the wait is let go instead.

Everything is driven through the real ``InstrumentInput`` capture loop and the
real ``GuitarLocalMonitor``, with fakes only for the device and the OpenAL
source.
"""
import collections
import unittest
from types import SimpleNamespace
from unittest import mock

import cyal

from libs import guitar_handler, instrument_input

RATE = 48000


class FakeBuffer:
    def __init__(self):
        self.pcm = None
        self.samples = 0

    def set_data(self, pcm, sample_rate=None, format=None):
        self.pcm = bytes(pcm)
        self.samples = len(pcm) // 2


class FakeSource:
    """``cyal.Source`` as the monitor uses it, with measurable OpenAL habits.

    Two behaviours are copied from the shipped library on purpose, because the
    monitor's logic depends on both: ``unqueue_buffers()`` hands back *every*
    processed buffer in one call, and a ``stop()``ped source counts everything
    it held as processed (which is what lets a backlog be dropped at all).
    """

    def __init__(self):
        self.looping = None
        self.position = None
        self.queued = []          # FakeBuffer, in queue order
        self.finished = 0         # how many of the queued have played out
        self.stopped = 0
        self.plays = 0
        self._state = cyal.SourceState.INITIAL

    def queue_buffers(self, *buffers):
        self.queued.extend(buffers)

    def unqueue_buffers(self):
        released = self.queued[:self.buffers_processed]
        del self.queued[:len(released)]
        if not self.queued and self._state == cyal.SourceState.PLAYING:
            self._state = cyal.SourceState.STOPPED
        return released

    def play(self):
        self.plays += 1
        self._state = cyal.SourceState.PLAYING

    def stop(self):
        self.stopped += 1
        self.finished = len(self.queued)
        self._state = cyal.SourceState.STOPPED

    def finish(self, count):
        """Test-side: let ``count`` queued buffers play out."""
        self.finished = min(len(self.queued), self.finished + count)

    @property
    def buffers_processed(self):
        return self.finished

    @property
    def buffers_queued(self):
        return len(self.queued)

    @property
    def state(self):
        return self._state


class FakeContext:
    def __init__(self, source=None):
        self.source = source or FakeSource()
        self.buffers = []

    def gen_source(self, **kwargs):
        return self.source

    def gen_buffer(self):
        buffer = FakeBuffer()
        self.buffers.append(buffer)
        return buffer


def monitor(source=None):
    context = FakeContext(source)
    audio = SimpleNamespace(context=context)
    return instrument_input.GuitarLocalMonitor(audio), context.source


class FakeCapture:
    """A capture device that hands over one delivery period per read.

    ``available_samples`` grows by ``period`` every time the loop looks, which
    is how a real device behaves when the loop keeps up with it. It stops the
    worker after ``reads`` reads so ``run()`` can be exercised in a test.
    """

    def __init__(self, reader, period=480, reads=4):
        self.reader = reader
        self.period = period
        self.reads = reads
        self.available = 0
        self.received = []
        self.name = "fake"

    def start(self):
        pass

    def stop(self):
        pass

    @property
    def available_samples(self):
        self.available += self.period
        return self.available

    def capture_samples(self, buf):
        self.available -= len(buf) // 2
        self.received.append(len(buf) // 2)
        if len(self.received) >= self.reads:
            self.reader.running = False


def make_input(device):
    """An ``InstrumentInput`` with no worker started and no device opened."""
    rec = instrument_input.InstrumentInput.__new__(instrument_input.InstrumentInput)
    rec.game = None
    rec.audio_input = device
    rec.stereo = False
    rec.recording = True
    rec.running = True
    rec.device_error = None
    rec.frames = collections.deque(maxlen=200)
    rec.notes = collections.deque(maxlen=8)
    rec._monitor_pending = bytearray()
    rec._relay_pending = bytearray()
    rec._guitar_voice = None
    rec.tracker = instrument_input.pitch.PitchTracker()
    return rec


class TheEarsChunkSizeTests(unittest.TestCase):
    def test_one_delivery_moves_the_ear(self):
        """10 ms of audio is handed to the monitor without waiting for 20."""
        rec = make_input(FakeCapture(None, reads=1))
        chunk = bytes(rec.MONITOR_CHUNK_SAMPLES * 2)
        rec._stage(chunk)
        self.assertEqual(len(rec.frames), 1)
        self.assertEqual(len(rec.frames[0]), len(chunk))
        self.assertEqual(len(rec.frames[0]) // 2, rec.MONITOR_CHUNK_SAMPLES)

    def test_the_ear_is_not_made_to_wait_for_the_relay_frame(self):
        """The 20 ms frame is what leaves; the monitor already has both halves."""
        rec = make_input(FakeCapture(None, reads=1))
        emitted = []
        rec._emit_frame = emitted.append
        rec._stage(bytes(rec.MONITOR_CHUNK_SAMPLES * 2))
        self.assertEqual(len(rec.frames), 1)
        self.assertEqual(emitted, [])          # the frame is still incomplete
        rec._stage(bytes(rec.MONITOR_CHUNK_SAMPLES * 2))
        self.assertEqual(len(rec.frames), 2)   # both chunks are already the ear's
        self.assertEqual(len(emitted), 1)

    def test_a_relay_frame_is_still_twenty_milliseconds(self):
        rec = make_input(FakeCapture(None, reads=1))
        emitted = []
        rec._emit_frame = emitted.append
        for _ in range(4):
            rec._stage(bytes(rec.MONITOR_CHUNK_SAMPLES * 2))
        self.assertEqual(len(emitted), 2)
        for frame in emitted:
            self.assertEqual(len(frame), rec.FRAME_SAMPLES * 2)

    def test_what_leaves_is_the_same_bytes_it_always_was(self):
        """Splitting the stream at two boundaries must not change the stream."""
        rec = make_input(FakeCapture(None, reads=1))
        emitted = []
        rec._emit_frame = emitted.append
        reads = [bytes([i % 251]) * (rec.MONITOR_CHUNK_SAMPLES * 2)
                 for i in range(6)]
        for raw in reads:
            rec._stage(raw)
        self.assertEqual(len(emitted), 3)      # 6 x 10 ms is 3 x 20 ms
        self.assertEqual(b"".join(emitted), b"".join(reads))

    def test_a_read_smaller_than_a_chunk_still_makes_a_chunk(self):
        """A device that ticks finer than 10 ms is drained, not waited on."""
        rec = make_input(FakeCapture(None, reads=1))
        half = bytes((rec.MONITOR_CHUNK_SAMPLES // 2) * 2)
        rec._stage(half)
        self.assertEqual(len(rec.frames), 0)
        rec._stage(half)
        self.assertEqual(len(rec.frames), 1)
        self.assertEqual(len(rec.frames[0]) // 2, rec.MONITOR_CHUNK_SAMPLES)


class TheCaptureLoopTests(unittest.TestCase):
    def test_the_loop_reads_one_chunk_at_a_time(self):
        """The real ``run()`` body: one 10 ms read per pass, never a 20 ms one."""
        rec = make_input(None)
        device = FakeCapture(rec, period=rec.MONITOR_CHUNK_SAMPLES, reads=4)
        rec.audio_input = device
        rec.run()
        self.assertEqual(device.received, [rec.MONITOR_CHUNK_SAMPLES] * 4)
        self.assertEqual(len(rec.frames), 4)
        for chunk in rec.frames:
            self.assertEqual(len(chunk) // 2, rec.MONITOR_CHUNK_SAMPLES)

    def test_a_backlog_is_drained_a_chunk_at_a_time(self):
        """A read that finds a backlog still hands over the newest 10 ms."""
        rec = make_input(None)
        device = FakeCapture(rec, period=rec.MONITOR_CHUNK_SAMPLES * 4, reads=2)
        rec.audio_input = device
        rec.run()
        self.assertEqual(device.received, [rec.MONITOR_CHUNK_SAMPLES] * 2)

    def test_a_dead_handle_retires_instead_of_killing_the_worker(self):
        rec = make_input(None)
        device = FakeCapture(rec, reads=99)
        device.name = "dead"

        def explode(buf):
            rec.running = False
            raise cyal.exceptions.InvalidDeviceError("gone")

        device.capture_samples = explode
        rec.audio_input = device
        rec.run()                                  # must not raise
        self.assertIsNone(rec.audio_input)
        self.assertIsNotNone(rec.device_error)


class TheMonitorQueueTests(unittest.TestCase):
    def test_a_running_ear_never_loses_a_chunk(self):
        mon, source = monitor()
        for _ in range(20):
            mon.feed(bytes(480 * 2))
            source.finish(1)
        self.assertEqual(mon.skips, 0)
        self.assertEqual(mon._waiting_samples, 480)
        self.assertEqual(source.buffers_queued, 1)

    def test_a_stall_does_not_leave_the_ear_behind_forever(self):
        """Past the limit the wait is dropped, not trailed for the session."""
        mon, source = monitor()
        chunks = mon.queue_limit_samples // 480 + 1
        for _ in range(chunks):
            mon.feed(bytes(480 * 2))       # nothing plays out: the thread stalled
        self.assertEqual(mon.skips, 1)
        self.assertLessEqual(mon._waiting_samples, 480)
        self.assertEqual(source.buffers_queued, 1)

    def test_the_chunk_that_survives_the_drop_is_the_newest_one(self):
        mon, source = monitor()
        feeds = mon.queue_limit_samples // 480 + 1
        for index in range(feeds):
            mon.feed(bytes([index % 251]) * 960)
        self.assertEqual(len(source.queued), 1)
        self.assertEqual(source.queued[0].pcm[:1], bytes([(feeds - 1) % 251]))

    def test_played_chunks_are_released_in_order(self):
        mon, source = monitor()
        mon.feed(bytes(480 * 2))
        mon.feed(bytes(480 * 2))
        mon.feed(bytes(480 * 2))
        self.assertEqual(mon._waiting_samples, 480 * 3)
        source.finish(2)
        mon.feed(bytes(480 * 2))
        self.assertEqual(source.buffers_queued, 2)
        self.assertEqual(mon._waiting_samples, 480 * 2)

    def test_a_source_that_will_not_cooperate_is_never_an_error(self):
        class Dead(FakeSource):
            def queue_buffers(self, *buffers):
                raise RuntimeError("no")

        mon, _source = monitor(Dead())
        mon.feed(bytes(480 * 2))                    # must not raise
        mon.feed(b"")                               # nothing to queue
        self.assertEqual(mon.skips, 0)

    def test_closing_lets_the_queue_go(self):
        mon, _source = monitor()
        mon.feed(bytes(480 * 2))
        mon.close()
        self.assertIsNone(mon.source)
        self.assertEqual(mon._waiting_samples, 0)


class TheHandlerTests(unittest.TestCase):
    def test_the_handler_feeds_the_monitor_every_chunk_it_drains(self):
        """``feed_monitor`` is the wire between the worker and the ear."""
        fed = []

        class RecordingMonitor:
            def set_position(self, *position):
                pass

            def feed(self, pcm):
                fed.append(len(pcm) // 2)

        class FakeInput:
            def take_device_error(self):
                return None

            def drain_raw_frames(self):
                return [bytes(480 * 2), bytes(480 * 2)]

        handler = guitar_handler.GuitarHandler.__new__(guitar_handler.GuitarHandler)
        handler._gp = SimpleNamespace(player=SimpleNamespace(x=0, y=0, z=0))
        handler.active = True
        handler.monitor = RecordingMonitor()
        handler.instrument_input = FakeInput()
        handler.feed_monitor()
        self.assertEqual(fed, [480, 480])

    def test_a_dead_device_still_turns_the_monitor_off_with_a_reason(self):
        class DeadInput:
            def take_device_error(self):
                return "Instrument input device stopped working (capture)"

        handler = guitar_handler.GuitarHandler.__new__(guitar_handler.GuitarHandler)
        handler._gp = SimpleNamespace(player=SimpleNamespace(x=0, y=0, z=0))
        handler.active = True
        handler.monitor = None
        handler.instrument_input = DeadInput()
        with mock.patch.object(guitar_handler, "speak") as spoken:
            handler.feed_monitor()
        self.assertFalse(handler.active)
        self.assertTrue(spoken.called)


if __name__ == "__main__":
    unittest.main()
