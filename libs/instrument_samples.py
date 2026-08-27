"""Bounded asynchronous preparation of instrument samples.

The creating thread owns paths, cache entries and every uploaded audio buffer.
One lazy daemon owns decoding and PCM conversion only. Call ``pump`` from the
audio owner's update loop; a cache miss never waits or replays a missed note.
Eviction drops cache references, not native buffers still owned by playing
sounds. ``clear``/``close`` must likewise run on the audio owner.
"""

from collections import OrderedDict
from dataclasses import dataclass, field
import audioop
import queue
import threading
import time


@dataclass
class _Job:
    path: str
    generation: int
    cancelled: threading.Event


@dataclass
class _Prepared:
    job: _Job
    # (PCM bytes, channel count); stereo, mono, left, right, or just mono.
    pieces: list = field(default_factory=list)
    rate: int = 0
    failed: bool = False


@dataclass
class _Uploading:
    prepared: _Prepared
    byte_size: int
    buffers: list = field(default_factory=list)


class InstrumentSampleCache:
    """Main-owner cache with dependency-injected local decoding and upload.

    ``resolve_path(path)`` runs on the owner and must be cheap/local.
    ``decode(canonical_path)`` runs only on the worker and returns an object
    with buffer/channels/frequency attributes. Inject a decoder with its own
    incremental size bound: the post-decode bound here cannot prevent a
    decoder from first allocating an oversized file.
    ``upload(pcm, channels, rate)`` runs ONLY inside the owner's pump.

    All representations share one decode. A stereo file takes four uploads;
    mono files share one buffer for stereo/mono and both split channels.
    Limits bound requests, completed PCM, strong LRU entries and failures.
    No file, audio, network or game subsystem is imported by this module.
    """

    def __init__(self, resolve_path, decode, upload, *, clock=time.monotonic,
                 max_pending=192, max_completed=2, max_sample_bytes=32 * 1024 * 1024,
                 max_cache_bytes=192 * 1024 * 1024, max_entries=256,
                 max_failures=256):
        limits = (max_pending, max_completed, max_sample_bytes,
                  max_cache_bytes, max_entries, max_failures)
        if any(type(limit) is not int or limit < 1 for limit in limits):
            raise ValueError("sample cache limits must be positive integers")
        self._owner = threading.get_ident()
        self._resolve_path = resolve_path
        self._decode = decode
        self._upload = upload
        self._clock = clock
        self._max_pending = max_pending
        self._max_sample_bytes = max_sample_bytes
        self._max_cache_bytes = max_cache_bytes
        self._max_entries = max_entries
        self._max_failures = max_failures
        self._requests = queue.Queue(maxsize=max_pending)
        self._completed = queue.Queue(maxsize=max_completed)
        self._pending = {}
        self._cache = OrderedDict()
        # Bounded metadata survives LRU eviction so an oversized warm-up batch
        # fails explicitly instead of endlessly evicting/re-decoding itself.
        self._sizes = OrderedDict()
        self._failures = OrderedDict()
        self._cache_bytes = 0
        self._generation = 0
        self._cancelled = threading.Event()
        self._shutdown = threading.Event()
        self._worker = None
        self._uploading = None
        self._closed = False

    def _check_owner(self):
        if threading.get_ident() != self._owner:
            raise RuntimeError("instrument sample cache must be used by its audio owner")

    def _canonical(self, path):
        try:
            canonical = self._resolve_path(path)
            if not isinstance(canonical, str) or not canonical:
                return None
            return canonical
        except Exception:
            return None

    @staticmethod
    def _paths(paths):
        return (paths,) if isinstance(paths, str) else paths

    def _request_one(self, canonical):
        if (self._closed or canonical in self._cache or canonical in self._pending
                or canonical in self._failures or len(self._pending) >= self._max_pending):
            return False
        job = _Job(canonical, self._generation, self._cancelled)
        try:
            self._requests.put_nowait(job)
        except queue.Full:
            return False
        self._pending[canonical] = job
        if self._worker is None:
            self._worker = threading.Thread(target=self._work, daemon=True,
                                            name="instrument-sample-decoder")
            self._worker.start()
        return True

    def request(self, paths):
        """Queue missing paths without waiting; return the number newly queued.

        Full queues defer admission rather than reporting a bad file. Repeating
        request/status/get on a later frame can admit the remaining paths.
        """
        self._check_owner()
        accepted = 0
        for path in self._paths(paths):
            canonical = self._canonical(path)
            if canonical is not None:
                accepted += self._request_one(canonical)
        return accepted

    def get(self, path, kind="stereo"):
        """Return a ready buffer (or split pair); request a miss and return None."""
        self._check_owner()
        if kind not in ("stereo", "mono", "split"):
            raise ValueError("unknown instrument sample representation")
        canonical = self._canonical(path)
        if canonical is None or self._closed:
            return None
        entry = self._cache.get(canonical)
        if entry is None:
            self._request_one(canonical)
            return None
        self._cache.move_to_end(canonical)
        return entry[0][kind]

    def status(self, paths):
        """Return ready/loading/failed for a batch, requesting missing samples."""
        self._check_owner()
        if self._closed:
            return "failed"
        canonical_paths = set()
        for path in self._paths(paths):
            canonical = self._canonical(path)
            if canonical is None:
                return "failed"
            canonical_paths.add(canonical)
            if len(canonical_paths) > self._max_entries:
                return "failed"
        if sum(self._sizes.get(path, 0) for path in canonical_paths) > self._max_cache_bytes:
            return "failed"
        state = "ready"
        for canonical in canonical_paths:
            if canonical in self._failures:
                state = "failed"
            elif canonical in self._cache:
                self._cache.move_to_end(canonical)
            else:
                self._request_one(canonical)
                if state != "failed":
                    state = "loading"
        return state

    def _is_cancelled(self, job):
        return self._shutdown.is_set() or job.cancelled.is_set()

    def _prepare(self, job):
        decoded = self._decode(job.path)
        if self._is_cancelled(job):
            return None
        channels, rate = decoded.channels, decoded.frequency
        if (type(channels) is not int or channels not in (1, 2)
                or type(rate) is not int or rate < 1 or rate > 384000):
            raise ValueError("unsupported instrument PCM format")
        view = memoryview(decoded.buffer)
        if not 0 < view.nbytes <= self._max_sample_bytes or view.nbytes % (2 * channels):
            raise ValueError("invalid instrument PCM size")
        pcm = bytes(view)
        del view, decoded
        if channels == 1:
            return _Prepared(job, [(pcm, 1)], rate)
        # Chunked native conversions avoid a Python per-sample loop and permit
        # generation cancellation between bounded chunks. audioop's half/half
        # conversion preserves the old signed (left + right) // 2 rounding.
        mono, left, right = bytearray(), bytearray(), bytearray()
        for offset in range(0, len(pcm), 64 * 1024):
            if self._is_cancelled(job):
                return None
            chunk = pcm[offset:offset + 64 * 1024]
            mono.extend(audioop.tomono(chunk, 2, 0.5, 0.5))
            left.extend(audioop.tomono(chunk, 2, 1.0, 0.0))
            right.extend(audioop.tomono(chunk, 2, 0.0, 1.0))
        return _Prepared(job, [(pcm, 2), (bytes(mono), 1),
                               (bytes(left), 1), (bytes(right), 1)], rate)

    def _work(self):
        while not self._shutdown.is_set():
            try:
                job = self._requests.get(timeout=0.05)
            except queue.Empty:
                continue
            if job is None:
                break
            if self._is_cancelled(job):
                continue
            try:
                prepared = self._prepare(job)
            except Exception:
                # Never retain decoder exceptions/tracebacks or raw paths in
                # logs: they can keep whole decoded files alive indefinitely.
                prepared = _Prepared(job, failed=True)
            if prepared is not None:
                while not self._is_cancelled(job):
                    try:
                        self._completed.put(prepared, timeout=0.05)
                        break
                    except queue.Full:
                        continue
            prepared = None

    def _fail(self, path):
        self._pending.pop(path, None)
        self._failures[path] = True
        self._failures.move_to_end(path)
        while len(self._failures) > self._max_failures:
            self._failures.popitem(last=False)

    def pump(self, max_uploads=4, budget_seconds=0.002):
        """Upload a bounded amount on the audio owner; return upload count.

        The budget is checked between uploads. One backend upload cannot be
        interrupted; its duration remains the backend's responsibility.
        """
        self._check_owner()
        if self._closed or max_uploads <= 0 or budget_seconds <= 0:
            return 0
        deadline = self._clock() + budget_seconds
        count = 0
        while count < max_uploads and self._clock() < deadline:
            if self._uploading is None:
                try:
                    prepared = self._completed.get_nowait()
                except queue.Empty:
                    break
                job = prepared.job
                if (job.generation != self._generation or job.cancelled.is_set()
                        or self._pending.get(job.path) is not job):
                    continue
                byte_size = sum(len(pcm) for pcm, _ in prepared.pieces)
                if not prepared.failed:
                    self._sizes[job.path] = byte_size
                    self._sizes.move_to_end(job.path)
                    while len(self._sizes) > self._max_entries + self._max_pending:
                        self._sizes.popitem(last=False)
                if prepared.failed or byte_size > self._max_cache_bytes:
                    self._fail(job.path)
                    continue
                # Reserve room before allocating any new native buffer; the
                # in-progress upload must not temporarily double the budget.
                while self._cache and (
                    len(self._cache) >= self._max_entries
                    or self._cache_bytes + byte_size > self._max_cache_bytes
                ):
                    _, (_, size) = self._cache.popitem(last=False)
                    self._cache_bytes -= size
                self._uploading = _Uploading(prepared, byte_size)
            current = self._uploading
            prepared = current.prepared
            index = len(current.buffers)
            pcm, channels = prepared.pieces[index]
            try:
                buffer = self._upload(pcm, channels, prepared.rate)
                if buffer is None:
                    raise ValueError("instrument buffer upload failed")
            except Exception:
                self._fail(prepared.job.path)
                self._uploading = None
                count += 1
                continue
            current.buffers.append(buffer)
            prepared.pieces[index] = (b"", channels)
            count += 1
            if len(current.buffers) == len(prepared.pieces):
                buffers = current.buffers
                representations = {
                    "stereo": buffers[0],
                    "mono": buffers[0] if len(buffers) == 1 else buffers[1],
                    "split": (buffers[0], buffers[0]) if len(buffers) == 1
                             else (buffers[2], buffers[3]),
                }
                while self._cache and (
                    len(self._cache) >= self._max_entries
                    or self._cache_bytes + current.byte_size > self._max_cache_bytes
                ):
                    _, (_, size) = self._cache.popitem(last=False)
                    self._cache_bytes -= size
                self._cache[prepared.job.path] = (representations, current.byte_size)
                self._cache_bytes += current.byte_size
                self._pending.pop(prepared.job.path, None)
                self._uploading = None
        return count

    @staticmethod
    def _drain(q):
        while True:
            try:
                q.get_nowait()
            except queue.Empty:
                return

    def clear(self):
        """Cancel this generation without waiting for an in-flight decoder."""
        self._check_owner()
        self._cancelled.set()
        self._generation += 1
        self._cancelled = threading.Event()
        self._pending.clear()
        self._drain(self._requests)
        self._drain(self._completed)
        self._uploading = None
        self._cache.clear()
        self._sizes.clear()
        self._cache_bytes = 0
        self._failures.clear()

    def close(self):
        """Cancel and signal the daemon; never join on the game/audio owner."""
        self._check_owner()
        if self._closed:
            return
        self._closed = True
        self._shutdown.set()
        self.clear()
        try:
            self._requests.put_nowait(None)
        except queue.Full:
            pass

    def stats(self):
        """Small owner-side counters; contains no paths, PCM or audio handles."""
        self._check_owner()
        return {"pending": len(self._pending), "queued": self._requests.qsize(),
                "completed": self._completed.qsize(), "entries": len(self._cache),
                "cache_bytes": self._cache_bytes, "failures": len(self._failures),
                "uploading": self._uploading is not None, "closed": self._closed}
