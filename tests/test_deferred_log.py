"""No real logs: all sink I/O is replaced by controlled in-memory writers."""

import threading
import time
import unittest
from unittest.mock import Mock, patch

from libs import deferred_log
from libs.deferred_log import DeferredLog


class DeferredLogTests(unittest.TestCase):
    def setUp(self):
        self.sinks = []
        self.release_events = []

    def tearDown(self):
        for sink in self.sinks:
            sink.close()
        for event in self.release_events:
            event.set()
        # Tests may join their own fake-I/O workers; production close never does.
        for sink in self.sinks:
            if sink._worker is not None:
                sink._worker.join(timeout=1)
                self.assertFalse(sink._worker.is_alive())

    def sink(self, writer, **kwargs):
        sink = DeferredLog(writer, **kwargs)
        self.sinks.append(sink)
        return sink

    def wait_for(self, predicate):
        deadline = time.monotonic() + 2
        while not predicate():
            if time.monotonic() >= deadline:
                self.fail("fake log writer did not reach expected state")
            time.sleep(0.001)

    def blocked_sink(self, **kwargs):
        entered, release = threading.Event(), threading.Event()
        self.release_events.append(release)
        written = []

        def writer(message):
            written.append(message)
            entered.set()
            release.wait(timeout=2)

        sink = self.sink(writer, **kwargs)
        self.assertTrue(sink.submit("first"))
        self.assertTrue(entered.wait(timeout=1))
        return sink, written, release

    def test_submit_does_not_wait_for_a_blocked_writer(self):
        sink, _, release = self.blocked_sink()
        self.assertTrue(sink.submit("second"))
        self.assertFalse(release.is_set())
        self.assertEqual(sink.stats()["pending"], 1)
        self.assertEqual(sink.stats()["written"], 0)

    def test_full_queue_drops_new_traces_without_waiting(self):
        sink, _, release = self.blocked_sink(max_pending=2)
        self.assertTrue(sink.submit("second"))
        self.assertTrue(sink.submit("third"))
        for _ in range(20):
            self.assertFalse(sink.submit("discarded"))
        self.assertFalse(release.is_set())
        self.assertEqual(sink.stats()["pending"], 2)
        self.assertEqual(sink.stats()["accepted"], 3)
        self.assertEqual(sink.stats()["dropped"], 20)

    def test_accepted_messages_keep_order_and_use_one_worker(self):
        sink, written, release = self.blocked_sink(max_pending=12)
        for index in range(10):
            self.assertTrue(sink.submit(str(index)))
        release.set()
        self.wait_for(lambda: sink.stats()["written"] == 11)
        self.assertEqual(written, ["first"] + [str(index) for index in range(10)])
        self.assertEqual(sink.stats()["worker_starts"], 1)
        self.assertTrue(sink._worker.daemon)

    def test_long_messages_are_truncated_before_enqueue(self):
        written = []
        sink = self.sink(written.append, max_message_chars=4)
        self.assertTrue(sink.submit("abcdefghijklmnop"))
        self.wait_for(lambda: sink.stats()["written"] == 1)
        self.assertEqual(written, ["abcd"])

    def test_invalid_messages_do_not_call_str_or_start_a_worker(self):
        class UnsafeMessage:
            def __str__(self):
                raise AssertionError("must not convert arbitrary objects")

        writer = Mock()
        sink = self.sink(writer)
        self.assertFalse(sink.submit(UnsafeMessage()))
        self.assertFalse(sink.submit(None))
        self.assertEqual(sink.stats()["dropped"], 2)
        self.assertEqual(sink.stats()["worker_starts"], 0)
        writer.assert_not_called()

    def test_writer_failure_does_not_recurse_or_kill_the_worker(self):
        written = []

        def writer(message):
            written.append(message)
            if message == "bad":
                raise OSError("fake disk failure")

        sink = self.sink(writer)
        self.assertTrue(sink.submit("bad"))
        self.assertTrue(sink.submit("good"))
        self.wait_for(lambda: sink.stats()["written"] == 1)
        self.assertEqual(written, ["bad", "good"])
        self.assertEqual(sink.stats()["failures"], 1)
        self.assertEqual(sink.stats()["worker_starts"], 1)

    def test_close_discards_pending_and_does_not_join_blocked_writer(self):
        sink, written, release = self.blocked_sink()
        self.assertTrue(sink.submit("discard on close"))
        sink.close()
        sink.close()
        self.assertFalse(release.is_set())
        self.assertFalse(sink.submit("after close"))
        self.assertEqual(sink.stats()["pending"], 0)
        self.assertEqual(sink.stats()["dropped"], 2)
        self.assertTrue(sink.stats()["closed"])
        self.assertTrue(sink._worker.is_alive())
        release.set()
        self.wait_for(lambda: not sink._worker.is_alive())
        self.assertEqual(written, ["first"])

    def test_close_unused_sink_never_creates_a_worker(self):
        sink = self.sink(Mock())
        sink.close()
        self.assertFalse(sink.submit("no"))
        self.assertIsNone(sink._worker)
        self.assertEqual(sink.stats()["worker_starts"], 0)

    def test_concurrent_submitters_share_one_worker_and_stay_bounded(self):
        sink, _, release = self.blocked_sink(max_pending=8)
        barrier = threading.Barrier(5)

        def submit_many():
            barrier.wait(timeout=1)
            for _ in range(20):
                sink.submit("trace")

        submitters = [threading.Thread(target=submit_many) for _ in range(4)]
        for thread in submitters:
            thread.start()
        barrier.wait(timeout=1)
        for thread in submitters:
            thread.join(timeout=1)
            self.assertFalse(thread.is_alive())
        self.assertFalse(release.is_set())
        self.assertEqual(sink.stats()["worker_starts"], 1)
        self.assertEqual(sink.stats()["pending"], 8)
        self.assertEqual(sink.stats()["accepted"], 9)
        self.assertEqual(sink.stats()["dropped"], 72)

    def test_default_helper_creates_only_one_lazy_sink(self):
        sink = Mock()
        sink.submit.return_value = True
        with patch.object(deferred_log, "_default_log", None), \
                patch.object(deferred_log, "DeferredLog", return_value=sink) as factory:
            self.assertTrue(deferred_log.log_deferred("first"))
            self.assertTrue(deferred_log.log_deferred("second"))
        factory.assert_called_once_with(deferred_log._write_diagnostic)
        self.assertEqual(sink.submit.call_count, 2)

    def test_invalid_limits_are_rejected(self):
        for kwargs in ({"max_pending": 0}, {"max_pending": True},
                       {"max_message_chars": 0}, {"max_message_chars": 1.5}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                DeferredLog(Mock(), **kwargs)
        with self.assertRaises(TypeError):
            DeferredLog(None)


if __name__ == "__main__":
    unittest.main()
