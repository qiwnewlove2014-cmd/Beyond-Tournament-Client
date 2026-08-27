"""Bounded asynchronous map PCM loading with owner-only native buffers.

The worker receives no cache, resolver, upload callback, audio manager, or
native object. Inject a static decoder which bounds its own PCM allocation;
the post-decode validation here cannot undo an oversized decoder allocation.
Ready buffers are owned by the creating audio thread. LRU eviction drops only
cache references; a playing source must retain its own buffer reference.
"""

from collections import OrderedDict
from dataclasses import dataclass
import queue
import threading
import time


@dataclass(frozen=True)
class _Job:
    path: str
    cancelled: threading.Event


@dataclass
class _Result:
    job: _Job
    pcm: bytes = b""
    channels: int = 0
    rate: int = 0
    failed: bool = False


def _cancelled(job, shutdown):
    return shutdown.is_set() or job.cancelled.is_set()


def _prepare(job, decode, shutdown, max_sample_bytes):
    decoded = decode(job.path)
    if _cancelled(job, shutdown):
        return None
    channels, rate = decoded.channels, decoded.frequency
    if (type(channels) is not int or channels not in (1, 2)
            or type(rate) is not int or not 1 <= rate <= 384000):
        raise ValueError("unsupported map PCM format")
    raw = decoded.buffer
    view = memoryview(raw)
    if (not 0 < view.nbytes <= max_sample_bytes
            or view.nbytes % (2 * channels)):
        raise ValueError("invalid map PCM size")
    pcm = raw if type(raw) is bytes else view.tobytes()
    return _Result(job, pcm, channels, rate)


def _decode_worker(requests, completed, publish_lock, shutdown, decode,
                   max_sample_bytes):
    """Free function: daemon lifetime never retains a cache/audio owner."""
    while not shutdown.is_set():
        try:
            job = requests.get(timeout=0.05)
        except queue.Empty:
            continue
        result = None
        try:
            if job is None:
                return
            if _cancelled(job, shutdown):
                continue
            try:
                result = _prepare(job, decode, shutdown, max_sample_bytes)
            except Exception:
                # No exceptions/tracebacks or arbitrary decoder objects enter
                # the mailbox, even when a failed decode allocated large PCM.
                result = _Result(job, failed=True)
            while result is not None and not _cancelled(job, shutdown):
                # Cancellation and publication share a short lock. Never hold
                # it during decode, queue-space waits, or native operations.
                with publish_lock:
                    if _cancelled(job, shutdown):
                        break
                    try:
                        completed.put_nowait(result)
                    except queue.Full:
                        pass
                    else:
                        break
                shutdown.wait(0.01)
        finally:
            result = job = None
            requests.task_done()


class MapSoundCache:
    """Main-owner API; no file/audio/network libraries are imported here.

    resolve_path(path): cheap owner-side canonicalization, including VFS.
    decode(path): static worker callable returning buffer/channels/frequency.
    upload(pcm, channels, rate): owner-only native upload, returning a buffer.
    """

    def __init__(self, resolve_path, decode, upload, *, clock=time.monotonic,
                 max_pending=32, max_completed=1,
                 max_sample_bytes=64 * 1024 * 1024,
                 max_cache_bytes=96 * 1024 * 1024,
                 max_entries=64, max_failures=64):
        limits = (max_pending, max_completed, max_sample_bytes,
                  max_cache_bytes, max_entries, max_failures)
        if any(type(limit) is not int or limit < 1 for limit in limits):
            raise ValueError("map sound cache limits must be positive integers")
        if not all(callable(function) for function in (resolve_path, decode, upload, clock)):
            raise TypeError("cache dependencies must be callable")
        self._owner = threading.get_ident()
        self._resolve_path, self._decode, self._upload = resolve_path, decode, upload
        self._clock = clock
        self._max_pending, self._max_completed = max_pending, max_completed
        self._max_sample_bytes, self._max_cache_bytes = max_sample_bytes, max_cache_bytes
        self._max_entries, self._max_failures = max_entries, max_failures
        self._requests = queue.Queue(maxsize=max_pending)
        self._completed = queue.Queue(maxsize=max_completed)
        self._publish_lock = threading.Lock()
        self._shutdown = threading.Event()
        self._cancelled = threading.Event()
        self._worker = None
        self._pending = {}
        self._cache = OrderedDict()
        self._failures = OrderedDict()
        self._cache_bytes = 0
        self._closed = False

    def _check_owner(self):
        if threading.get_ident() != self._owner:
            raise RuntimeError("map sound cache must be used by its audio owner")

    def _canonical(self, path):
        try:
            canonical = self._resolve_path(path)
            return canonical if type(canonical) is str and canonical else None
        except Exception:
            return None

    def _fail(self, path):
        self._pending.pop(path, None)
        self._failures[path] = True
        self._failures.move_to_end(path)
        while len(self._failures) > self._max_failures:
            self._failures.popitem(last=False)

    def _start_worker(self):
        if self._worker is not None:
            return True
        try:
            worker = threading.Thread(
                target=_decode_worker,
                args=(self._requests, self._completed, self._publish_lock,
                      self._shutdown, self._decode, self._max_sample_bytes),
                name="map-sound-decoder", daemon=True,
            )
            worker.start()
        except Exception:
            return False
        self._worker = worker
        return True

    def get(self, path):
        """Return a ready buffer, or request a cold path without waiting."""
        self._check_owner()
        if self._closed:
            return None
        canonical = self._canonical(path)
        if canonical is None:
            return None
        entry = self._cache.get(canonical)
        if entry is not None:
            self._cache.move_to_end(canonical)
            return entry[0]
        if (canonical in self._failures or canonical in self._pending
                or len(self._pending) >= self._max_pending):
            return None
        if not self._start_worker():
            self._fail(canonical)
            return None
        job = _Job(canonical, self._cancelled)
        try:
            self._requests.put_nowait(job)
        except queue.Full:
            return None
        self._pending[canonical] = job
        return None

    def status(self, path):
        """Inspect without queueing, changing LRU order, or retrying failure."""
        self._check_owner()
        if self._closed:
            return "failed"
        canonical = self._canonical(path)
        if canonical is None or canonical in self._failures:
            return "failed"
        if canonical in self._cache:
            return "ready"
        return "pending" if canonical in self._pending else "cold"

    def retry(self, path):
        """Explicit refresh clears a failure; next get() requests the path."""
        self._check_owner()
        if self._closed:
            return False
        canonical = self._canonical(path)
        return self._failures.pop(canonical, None) is not None

    def _current(self, job):
        return (not self._closed and not job.cancelled.is_set()
                and self._pending.get(job.path) is job)

    def pump(self, max_uploads=1, budget_seconds=0.002):
        """Upload bounded results; return attempts, including upload failures.

        Time is checked between uploads: a single native call cannot be
        interrupted. Stale/failure scans are bounded separately from uploads.
        """
        self._check_owner()
        if self._closed or max_uploads <= 0 or budget_seconds <= 0:
            return 0
        deadline = self._clock() + budget_seconds
        uploads = scanned = 0
        while (uploads < max_uploads and self._clock() < deadline
               and scanned < self._max_pending + self._max_completed):
            try:
                result = self._completed.get_nowait()
            except queue.Empty:
                break
            self._completed.task_done()
            scanned += 1
            job = result.job
            if not self._current(job):
                continue
            size = len(result.pcm)
            if result.failed or not 0 < size <= self._max_cache_bytes:
                self._fail(job.path)
                continue
            # Reserve the cache-owned budget before the native allocation.
            # Active sources may independently retain evicted buffers.
            while self._cache and (len(self._cache) >= self._max_entries
                                   or self._cache_bytes + size > self._max_cache_bytes):
                _, old_entry = self._cache.popitem(last=False)
                self._cache_bytes -= old_entry[1]
                del old_entry  # Do not retain the evicted native buffer locally.
            uploads += 1
            try:
                buffer = self._upload(result.pcm, result.channels, result.rate)
                if buffer is None:
                    raise ValueError("map audio buffer upload failed")
            except Exception:
                if self._current(job):
                    self._fail(job.path)
                continue
            if self._current(job):
                self._cache[job.path] = (buffer, size)
                self._cache_bytes += size
                self._pending.pop(job.path, None)
            # Release temporary PCM/native references on the owner, including
            # re-entrant map cancellation inside an injected upload callback.
            result = buffer = None
        return uploads

    @staticmethod
    def _drain(mailbox):
        while True:
            try:
                mailbox.get_nowait()
            except queue.Empty:
                return
            mailbox.task_done()

    def begin_map(self):
        """Cancel pending work, retaining warm LRU and explicit failures."""
        self._check_owner()
        if self._closed:
            return
        with self._publish_lock:
            self._cancelled.set()
            self._cancelled = threading.Event()
            self._pending.clear()
            self._drain(self._requests)
            self._drain(self._completed)

    def close(self):
        """Cancel without joining; release all native cache refs on owner."""
        self._check_owner()
        if self._closed:
            return
        with self._publish_lock:
            self._closed = True
            self._shutdown.set()
            self._cancelled.set()
            self._pending.clear()
            self._drain(self._requests)
            self._drain(self._completed)
        self._cache.clear()
        self._cache_bytes = 0
        self._failures.clear()
        self._resolve_path = self._upload = self._decode = None

    def stats(self):
        """Owner-only bounded counters, without paths or object handles."""
        self._check_owner()
        return {"pending": len(self._pending), "queued": self._requests.qsize(),
                "completed": self._completed.qsize(), "entries": len(self._cache),
                "cache_bytes": self._cache_bytes, "failures": len(self._failures),
                "closed": self._closed}
