"""Bounded, opt-in main-frame audio hitch diagnostics.

No arguments, return values, exceptions, paths, PCM, or game objects are
recorded. Timings are inclusive: nested labels must not be added together.
Only the deferred sink performs I/O. Disabled during normal play; explicitly
set BT_AUDIO_DIAGNOSTICS=1 only for a requested diagnostic session.
"""

import functools
import math
import os
import queue
import re
import threading
import time

from .deferred_log import log_deferred


_LABEL = re.compile(r"[a-z][a-z0-9_.-]{0,47}\Z", re.ASCII)
_COUNTER_LIMIT = 1_000_000_000
_WORKER_LABELS = frozenset((
    "direct.resolve", "direct.buffers", "direct.launch", "direct.prebuffer",
    "direct.read", "direct.queue", "direct.spatial", "direct.first_play",
))


class _Frame:
    __slots__ = ("start", "cpu", "gap", "gap_over", "sequence", "labels", "events",
                 "pace", "expected", "overflow")

    def __init__(self, start, cpu, gap, gap_over, sequence):
        self.start, self.cpu = start, cpu
        self.gap, self.gap_over = gap, gap_over
        self.sequence = sequence
        self.labels = {}
        self.events = []
        self.pace = self.expected = 0.0
        self.overflow = 0


class _Span:
    __slots__ = ("probe", "label", "record", "start")

    def __init__(self, probe, label):
        self.probe, self.label = probe, label
        self.record = None
        self.start = 0.0

    def __enter__(self):
        try:
            self.record = self.probe._active()
            if self.record is not None and self.probe._valid_label(self.label):
                self.start = self.probe._wall()
            else:
                self.record = None
        except Exception:
            self.record = None
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        record, self.record = self.record, None
        try:
            if record is not None and self.probe._active() is record:
                self.probe._duration(record, self.label, self.probe._wall() - self.start)
        except Exception:
            pass
        return False


class AudioDiagnostics:
    """One main-frame owner, with injected clocks/sink for device-free tests."""

    WINDOW_SECONDS = 15.0
    REPORT_INTERVAL = 0.5
    SLOW_SECONDS = 0.016

    def __init__(self, *, wall_clock=None, cpu_clock=None, sink=None,
                 enabled=None, max_labels=64):
        if type(max_labels) is not int or not 1 <= max_labels <= 64:
            raise ValueError("max_labels must be between 1 and 64")
        self.enabled = (os.environ.get("BT_AUDIO_DIAGNOSTICS", "0") == "1"
                        if enabled is None else bool(enabled))
        self._wall = wall_clock or time.perf_counter
        self._cpu = cpu_clock or time.thread_time
        self._sink = sink or log_deferred
        self._max_labels = max_labels
        self._local = threading.local()
        self._owner = None
        self._owner_lock = threading.Lock()
        self._window_until = float("-inf")
        self._previous_start = None
        self._previous_expected = 0.0
        self._first_frame_at = None
        self._frame_sequence = 0
        self._last_report = float("-inf")
        self._pending = None
        self._suppressed = 0
        # Workers publish numbers only; the main-frame owner aggregates and
        # submits to the deferred sink. Never retain callables/PCM/native objects.
        self._worker_samples = queue.Queue(maxsize=64)
        self._worker_drop_lock = threading.Lock()
        self._worker_dropped = 0
        self._worker_pending_dropped = 0
        self._worker_pending = {}
        self._last_worker_report = float("-inf")

    @staticmethod
    def _valid_label(label):
        # Reject, never stringify, arbitrary objects or user-provided text.
        return type(label) is str and _LABEL.fullmatch(label) is not None

    def _active(self):
        if not self.enabled:
            return None
        return getattr(self._local, "record", None)

    def _entry(self, record, label):
        entry = record.labels.get(label)
        if entry is None:
            if len(record.labels) >= self._max_labels:
                record.overflow = min(_COUNTER_LIMIT, record.overflow + 1)
                return None
            entry = record.labels[label] = [0.0, 0.0, 0, 0]
        return entry

    def _duration(self, record, label, duration):
        entry = self._entry(record, label)
        if entry is not None:
            duration = max(0.0, duration)
            entry[0] += duration
            entry[1] = max(entry[1], duration)
            entry[2] = min(_COUNTER_LIMIT, entry[2] + 1)

    def frame(self, function):
        @functools.wraps(function)
        def wrapped(*args, **kwargs):
            if not self.enabled or self._active() is not None:
                return function(*args, **kwargs)
            ident = threading.get_ident()
            if self._owner is None:
                with self._owner_lock:
                    if self._owner is None:
                        self._owner = ident
            if ident != self._owner:
                return function(*args, **kwargs)
            try:
                start = self._wall()
                gap = (max(0.0, start - self._previous_start)
                       if self._previous_start is not None else 0.0)
                gap_over = max(0.0, gap - self._previous_expected)
                if self._first_frame_at is None:
                    self._first_frame_at = start
                self._frame_sequence = min(_COUNTER_LIMIT, self._frame_sequence + 1)
                record = _Frame(start, self._cpu(), gap, gap_over, self._frame_sequence)
                self._local.record = record
            except Exception:
                self._local.record = None
                return function(*args, **kwargs)
            try:
                return function(*args, **kwargs)
            finally:
                # Never leave the frame attached to TLS after an exception.
                self._local.record = None
                try:
                    self._finish(record)
                except Exception:
                    # Instrumentation must never replace a gameplay exception.
                    pass
        return wrapped

    def measured(self, label, trigger=False):
        def decorate(function):
            @functools.wraps(function)
            def wrapped(*args, **kwargs):
                if trigger:
                    self.event(label)
                return self.call(label, function, *args, **kwargs)
            return wrapped
        return decorate

    def span(self, label):
        return _Span(self, label)

    def call(self, label, function, *args, **kwargs):
        record = self._active()
        if record is None or not self._valid_label(label):
            return function(*args, **kwargs)
        try:
            start = self._wall()
        except Exception:
            return function(*args, **kwargs)
        try:
            return function(*args, **kwargs)
        finally:
            try:
                self._duration(record, label, self._wall() - start)
            except Exception:
                pass

    def event(self, label):
        record = self._active()
        if record is None or not self._valid_label(label):
            return
        try:
            self._window_until = self._wall() + self.WINDOW_SECONDS
        except Exception:
            return
        self._append_event(record.events, label)

    def worker_call(self, label, function, *args, **kwargs):
        """Measure a direct startup attempt, separately from main-frame spans.

        Includes failed/cancelled attempts, not proof that sound was audible.
        Full queues drop new samples; no I/O or waits for queue space here.
        """
        if (not self.enabled or type(label) is not str
                or label not in _WORKER_LABELS):
            return function(*args, **kwargs)
        try:
            start, cpu_start = self._wall(), self._cpu()
        except Exception:
            return function(*args, **kwargs)
        try:
            return function(*args, **kwargs)
        finally:
            try:
                end, cpu_end = self._wall(), self._cpu()
                if all(math.isfinite(value) for value in (start, end, cpu_start, cpu_end)):
                    self._worker_samples.put_nowait((
                        label, start, end, max(0.0, end - start),
                        max(0.0, cpu_end - cpu_start),
                    ))
            except queue.Full:
                try:
                    with self._worker_drop_lock:
                        self._worker_dropped = min(_COUNTER_LIMIT, self._worker_dropped + 1)
                except Exception:
                    pass
            except Exception:
                pass

    def _finish_workers(self, now):
        # At most 64 samples and 8 labels, even with many simultaneous jukeboxes.
        # This state belongs exclusively to the main-frame owner.
        for _ in range(64):
            try:
                label, start, end, wall, cpu = self._worker_samples.get_nowait()
            except queue.Empty:
                break
            self._worker_samples.task_done()
            self._window_until = max(self._window_until, now + self.WINDOW_SECONDS)
            entry = self._worker_pending.get(label)
            if entry is None:
                entry = self._worker_pending[label] = [start, end, 0.0, 0.0, 0.0, 0]
            entry[0], entry[1] = min(entry[0], start), max(entry[1], end)
            entry[2] = min(1e9, entry[2] + wall)
            entry[3] = max(entry[3], min(1e9, wall))
            entry[4] = min(1e9, entry[4] + cpu)
            entry[5] = min(_COUNTER_LIMIT, entry[5] + 1)
        with self._worker_drop_lock:
            dropped, self._worker_dropped = self._worker_dropped, 0
        self._worker_pending_dropped = min(_COUNTER_LIMIT, self._worker_pending_dropped + dropped)
        if (not self._worker_pending and not self._worker_pending_dropped) or now - self._last_worker_report < 1.0:
            return
        self._last_worker_report = now
        origin = self._first_frame_at
        items = []
        for label, (start, end, total, maximum, cpu, calls) in sorted(self._worker_pending.items()):
            # Negative start_ms is possible when a worker predates frame one.
            items.append(f"{label}={total*1000:.2f}/{maximum*1000:.2f}/{cpu*1000:.2f}x{calls}"
                         f"@{(start-origin)*1000:.2f}..{(end-origin)*1000:.2f}")
        line = ("[AudioDiagWorker] scope=direct_startup_attempts_all_streams "
                f"dropped={self._worker_pending_dropped} separate_from_frame=true "
                "inclusive_ms(sum/max/cpu xcalls@start_ms..end_ms; "
                "nested, do not add; time_origin=AudioDiag.capture_ms): " + ";".join(items))[:2048]
        try:
            accepted = self._sink(line)
        except Exception:
            return
        if accepted is not False:
            self._worker_pending.clear()
            self._worker_pending_dropped = 0

    @staticmethod
    def _append_event(events, label):
        if label in events:
            return
        if len(events) < 8:
            events.append(label)
        elif label == "relay.first_play":
            # This exact marker is the diagnostic's key boundary. A burst of
            # map/start events must not hide it in a rate-limited report.
            events[-1] = label

    def count(self, label, integer=1):
        record = self._active()
        if (record is None or not self._valid_label(label)
                or type(integer) is not int):
            return
        entry = self._entry(record, label)
        if entry is not None:
            entry[3] = max(-_COUNTER_LIMIT,
                           min(_COUNTER_LIMIT, entry[3] + integer))

    def pace(self, tick, fps):
        record = self._active()
        if record is None:
            return tick(fps)
        try:
            if type(fps) in (int, float) and fps > 0:
                record.expected = 1.0 / fps
            start = self._wall()
        except Exception:
            return tick(fps)
        try:
            return tick(fps)
        finally:
            try:
                record.pace += max(0.0, self._wall() - start)
            except Exception:
                pass

    def _finish(self, record):
        now = self._wall()
        wall = max(0.0, now - record.start)
        cpu = max(0.0, self._cpu() - record.cpu)
        work = max(0.0, wall - record.pace)
        total_over = max(0.0, wall - record.expected)
        self._previous_start = record.start
        self._previous_expected = record.expected
        score = max(work, total_over, record.gap_over)
        # Record frame end before draining diagnostics so worker durations can
        # never inflate this frame's work/cpu totals. Worker reports have their
        # own <=1/s cap; ordinary frame reports remain <=2/s.
        self._finish_workers(now)
        if record.events or (record.start <= self._window_until
                             and score >= self.SLOW_SECONDS):
            report = {
                "start": record.start, "sequence": record.sequence,
                "work": work, "wall": wall, "cpu": cpu,
                "pace": record.pace, "gap": record.gap,
                "gap_over": record.gap_over, "total_over": total_over,
                "labels": record.labels, "events": record.events,
                "overflow": record.overflow, "score": score,
            }
            self._remember(report)
        if self._pending is None or now - self._last_report < self.REPORT_INTERVAL:
            return
        report = self._pending
        self._last_report = now  # Failed sinks also obey the attempt rate limit.
        try:
            accepted = self._sink(self._format(report, now))
        except Exception:
            return
        if accepted is not False:
            self._pending = None
            self._suppressed = 0

    def _remember(self, report):
        if self._pending is None:
            self._pending = report
            return
        self._suppressed = min(_COUNTER_LIMIT, self._suppressed + 1)
        previous = self._pending
        events = list(previous["events"])
        for label in report["events"]:
            self._append_event(events, label)
        if report["score"] > previous["score"] + 1e-9:
            self._pending = report
        self._pending["events"] = events

    def _format(self, report, now):
        metrics = " ".join(
            f"{key}_ms={report[key] * 1000:.2f}"
            for key in ("work", "wall", "cpu", "pace", "gap", "gap_over", "total_over")
        )
        reason = "slow" if report["score"] >= self.SLOW_SECONDS else "event"
        events = ",".join(report["events"]) or "-"
        timings = sorted(report["labels"].items(), key=lambda item: item[1][0], reverse=True)
        span_items = [
            f"{label}={total * 1000:.2f}/{maximum * 1000:.2f}x{calls}"
            for label, (total, maximum, calls, _) in timings if calls
        ][:18]
        counted = sorted(((label, entry[3]) for label, entry in timings if entry[3]),
                         key=lambda item: abs(item[1]), reverse=True)
        counts = ";".join(
            f"{label}={count}" for label, count in counted[:12]
        ) or "-"
        capture = max(0.0, report["start"] - self._first_frame_at) * 1000
        age = max(0.0, now - report["start"]) * 1000
        head = (f"[AudioDiag] reason={reason} events={events} events_scope=since_last_report "
                f"frame={report['sequence']} capture_ms={capture:.2f} age_ms={age:.2f} "
                f"{metrics} suppressed={self._suppressed} overflow={report['overflow']} "
                "inclusive_ms(sum/max xcalls; nested, do not add): ")
        tail = f" counts: {counts}"
        available = 2048 - len(head) - len(tail)
        selected = []
        used = 0
        for item in span_items:
            needed = len(item) + (1 if selected else 0)
            if used + needed > available:
                break
            selected.append(item)
            used += needed
        line = head + (";".join(selected) or "-") + tail
        return line[:2048]


probe = AudioDiagnostics()
