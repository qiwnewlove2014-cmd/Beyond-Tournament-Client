"""Best-effort routine diagnostics without disk/console I/O on the caller.

Crash reports must continue to use logger.log/log_exception directly. This
bounded sink is only for routine traces: overload or shutdown may drop them.
The sole lazy daemon writes strings; it never retains exceptions, audio, or
gameplay objects. Closing discards queued traces and never joins the writer.
"""

import queue
import threading


class DeferredLog:
    def __init__(self, writer, max_pending=128, max_message_chars=2048):
        if not callable(writer):
            raise TypeError("writer must be callable")
        if any(type(value) is not int or value < 1
               for value in (max_pending, max_message_chars)):
            raise ValueError("log limits must be positive integers")
        self._writer = writer
        self._max_message_chars = max_message_chars
        self._queue = queue.Queue(maxsize=max_pending)
        self._lock = threading.Lock()
        self._closed = False
        self._worker = None
        self._worker_starts = 0
        self._accepted = 0
        self._dropped = 0
        self._written = 0
        self._failures = 0

    def submit(self, message):
        """Accept a bounded string or drop it; never wait for the sink or space."""
        # Do not invoke arbitrary __str__ methods or retain arbitrary objects.
        valid = isinstance(message, str)
        if valid:
            message = message[:self._max_message_chars]
        with self._lock:
            if self._closed or not valid:
                self._dropped += 1
                return False
            if self._worker is None:
                worker = threading.Thread(
                    target=self._run, name="routine-log-writer", daemon=True,
                )
                try:
                    worker.start()
                except Exception:
                    self._dropped += 1
                    return False
                self._worker = worker
                self._worker_starts += 1
            try:
                self._queue.put_nowait(message)
            except queue.Full:
                self._dropped += 1
                return False
            self._accepted += 1
            return True

    def _run(self):
        while True:
            with self._lock:
                if self._closed:
                    return
            try:
                message = self._queue.get(timeout=0.05)
            except queue.Empty:
                continue
            with self._lock:
                if self._closed:
                    self._dropped += 1
                    return
            try:
                # No state lock is held during console, file, or fsync work.
                self._writer(message)
            except Exception:
                # Never log a logging failure or retain its traceback.
                with self._lock:
                    self._failures += 1
            else:
                with self._lock:
                    self._written += 1
            finally:
                message = None

    def close(self):
        """Reject new traces and discard pending ones without waiting for I/O.

        A write already in progress may finish; it is never interrupted. The
        daemon exits after that write, or its short idle queue timeout.
        """
        with self._lock:
            if self._closed:
                return
            self._closed = True
            while True:
                try:
                    self._queue.get_nowait()
                except queue.Empty:
                    break
                self._dropped += 1

    def stats(self):
        """Bounded counters only, with no messages or caller-owned objects."""
        with self._lock:
            return {
                "pending": self._queue.qsize(), "accepted": self._accepted,
                "dropped": self._dropped, "written": self._written,
                "failures": self._failures, "worker_starts": self._worker_starts,
                "closed": self._closed,
            }


_default_log = None
_default_lock = threading.Lock()


def _write_diagnostic(message):
    # Import on the writer thread. In particular, importing this helper in a
    # headless test must not open or initialize a real log file.
    from .logger import log
    log(message)


def log_deferred(message):
    """Submit a routine trace to one process-lifetime bounded daemon sink."""
    global _default_log
    with _default_lock:
        if _default_log is None:
            _default_log = DeferredLog(_write_diagnostic)
        sink = _default_log
    return sink.submit(message)
