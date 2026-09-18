"""The copy of a live note a crossed cinema speaker plays.

A cinema speaker a builder gives a crossover is fed one side of the split and
none of the other, and the room does that itself rather than asking OpenAL for
it (see ``libs/audio/cinema/crossover.py``: no EFX filter can). A *note* is not
a stream of frames, though -- the piano and the drums play one short sample at
every speaker of the room, out of the sample the instrument already holds -- so
the room's own filter is run over that sample once, whole, and the result is
what the speaker plays.

Three things are deliberate:

* one copy per ``(sample, mark, half)``, because the filter is a Python loop
  and a note must never wait for it: a bass cabinet's own copy of one piano note
  costs about 80 ms to make, which is four frames of a live performance. The
  copy is made the first time that speaker is used (this module only ever
  answers "not ready yet"), so a band is heard unfiltered at that speaker for a
  note or two and correctly from then on;
* from silence rather than carrying state, because a note is not a stream.
  The room's carried state exists so consecutive frames of one song do not
  click; a sample struck out of nothing has no previous frame, and the two
  channels of a stereo sample are filtered independently -- which is exactly
  what the room's own per-channel state does frame by frame;
* at the *sample's* own rate, not the room's (instrument samples are 44.1 kHz,
  the room's frames are 48 kHz): both are two cascaded one-pole sections, and
  the corner has to land on the same frequency in whichever stream is being
  filtered.

Only the crossing is here: the trimmed speaker, the wall and the stereo half a
speaker plays stay with the room's own code and this module never sees them.

As in ``instrument_samples``, the decode and the upload are handed in: the
worker decodes and filters (no engine, no game module), and the audio owner
uploads inside its own pump. ``get`` never blocks -- a copy that is not ready
yet is simply not ready, and the caller plays the sample it played before.
"""

from array import array
from collections import OrderedDict
from dataclasses import dataclass
import queue
import sys
import threading
import time

from .audio.cinema.crossover import FULL_RANGE, apply, crossover_hz

# The halves of a stereo sample, as the room names them (``live.CHANNEL_*``).
# Kept as plain strings so this module never imports the room feature back.
CHANNEL_LEFT = "l"
CHANNEL_RIGHT = "r"

# One sample's own bound, the same one the instrument cache decodes under.
MAX_SAMPLE_BYTES = 32 * 1024 * 1024
# # Bounded so a map with many marked speakers and a pianist walking a keyboard
# cannot grow the copies without end: the least recently used one is the one
# given up. The bound is sized for the *working set* of one instrument in one
# room -- the whole keyboard times the marks that room feeds -- because an
# LRU smaller than the working set is not a graceful degradation but the worst
# case: every copy is evicted before its sample is played again, so the same
# notes are filtered over and over for the whole performance (and the worker
# never idles, competing with the game's own thread for the interpreter). A
# piano is 85 notes (about 474 KB each decoded) and a room feeds one copy per
# mark, so ~170-250 copies at 0.25-0.5 MB is the room a real band plays in.
# Nobody who never crosses a speaker and never plays a note pays any of it.
DEFAULT_MAX_ENTRIES = 384
DEFAULT_MAX_BYTES = 96 * 1024 * 1024
MAX_FAILURES = 64
# The upload queue: a copy is made on a whim of the map, so a burst of notes
# must not be able to grow a queue without bound.
MAX_DONE = 64


@dataclass
class _Job:
    """One copy being made: the sample, the mark, and the half asked for."""

    key: tuple
    path: str
    mark: float
    channel: object
    generation: int
    cancelled: threading.Event


@dataclass
class _Prepared:
    """A finished copy, waiting for the owner's pump to upload it."""

    job: _Job
    pcm: bytes = b""
    channels: int = 1
    rate: int = 0
    failed: bool = False


def _unpack(pcm):
    """s16le bytes as an array of samples, whatever this machine's byte order."""
    samples = array("h")
    samples.frombytes(pcm)
    if sys.byteorder != "little":
        samples.byteswap()
    return samples


def _pack(samples):
    """An array of samples back to s16le bytes."""
    if sys.byteorder != "little":
        samples.byteswap()
    return samples.tobytes()


def _interleave(left, right):
    """Two mono sample arrays as one stereo s16le buffer."""
    mixed = array("h", bytes(2 * len(left) * 2))
    mixed[0::2] = left
    mixed[1::2] = right
    return _pack(mixed)


def crossed_pcm(pcm, channels, rate, mark, channel=None):
    """``(pcm, channels)`` of one sample as a crossed speaker hears it.

    ``mark`` is the speaker's signed crossover (``crossover_hz`` reads it), and
    ``channel`` is the half the speaker carries (``None`` plays the whole
    sample, which is what every speaker that is not a screen wall plays). A
    mono sample has no halves, so every request answers with the one stream it
    has. The mono answers are per-channel byte strings, the way
    ``crossover.apply`` takes them; the stereo one is interleaved back into the
    single buffer a stereo source is given. Raises ``ValueError`` on PCM this
    cannot read: the caller remembers the failure and keeps playing the sample
    the speaker used to play.
    """
    mark = crossover_hz(mark)
    if mark == FULL_RANGE:
        raise ValueError("a full-range speaker has no crossed copy")
    samples = _unpack(pcm)
    if channels not in (1, 2) or not samples or len(samples) % channels:
        raise ValueError("unsupported instrument PCM")
    if channels == 1:
        # Mono has no halves: every request answers with the one stream it has.
        return apply((pcm,), None, mark, rate)[0][0], 1
    half = channel if channel in (CHANNEL_LEFT, CHANNEL_RIGHT) else None
    left = _pack(samples[0::2])
    right = _pack(samples[1::2])
    if half is None:
        filtered_left, filtered_right = apply((left, right), None, mark, rate)[0]
        return _interleave(_unpack(filtered_left), _unpack(filtered_right)), 2
    mono = left if half == CHANNEL_LEFT else right
    return apply((mono,), None, mark, rate)[0][0], 1


class CrossedSampleCache:
    """Every crossed copy this client has made, made once and kept.

    ``resolve_path(path)`` runs on the caller and must be cheap/local (it is the
    cache key). ``decode(canonical_path)`` runs only on the worker and returns an
    object with ``buffer``/``channels``/``frequency``. ``upload(pcm, channels,
    rate)`` runs ONLY on the audio owner, inside ``pump``.

    ``get`` is safe from any thread a sound is spawned on; ``pump`` and
    ``close`` belong to the audio owner, where the game's buffers are made.
    """

    def __init__(self, resolve_path, decode, upload, *, clock=time.monotonic,
                 max_entries=DEFAULT_MAX_ENTRIES,
                 max_bytes=DEFAULT_MAX_BYTES,
                 max_sample_bytes=MAX_SAMPLE_BYTES,
                 max_failures=MAX_FAILURES):
        limits = (max_entries, max_bytes, max_sample_bytes, max_failures)
        if any(type(limit) is not int or limit < 1 for limit in limits):
            raise ValueError("crossed sample limits must be positive integers")
        self._resolve_path = resolve_path
        self._decode = decode
        self._upload = upload
        self._clock = clock
        self._max_entries = max_entries
        self._max_bytes = max_bytes
        self._max_sample_bytes = max_sample_bytes
        self._max_failures = max_failures
        self._lock = threading.Lock()
        self._entries = OrderedDict()
        self._sizes = OrderedDict()
        self._pending = {}
        self._failures = OrderedDict()
        self._bytes = 0
        self._jobs = queue.Queue(maxsize=max_entries)
        self._done = queue.Queue(maxsize=MAX_DONE)
        self._generation = 0
        self._cancelled = threading.Event()
        self._shutdown = threading.Event()
        self._worker = None
        self._uploading = None
        self._closed = False

    def _canonical(self, path):
        try:
            canonical = self._resolve_path(path)
        except Exception:
            return None
        if not isinstance(canonical, str) or not canonical:
            return None
        return canonical

    @staticmethod
    def _half(channel):
        return channel if channel in (CHANNEL_LEFT, CHANNEL_RIGHT) else None

    def get(self, path, mark, channel=None):
        """The crossed buffer for one speaker, or None while it is being made.

        None is the same answer for "not ready" and "this sample cannot be
        crossed", and both mean the same thing to the caller: play the sample
        that speaker played before. A full-range mark never has a copy -- the
        room's own path is that speaker.
        """
        if self._closed or crossover_hz(mark) == FULL_RANGE:
            return None
        canonical = self._canonical(path)
        if canonical is None:
            return None
        mark = crossover_hz(mark)
        channel = self._half(channel)
        key = (canonical, mark, channel)
        with self._lock:
            if key in self._failures:
                return None
            buffer = self._entries.get(key)
            if buffer is not None:
                self._entries.move_to_end(key)
                return buffer
        self.request(canonical, mark, channel)
        return None

    def request(self, path, mark, channel=None):
        """Queue one copy without waiting; return whether it was queued.

        A path already cached, already queued, or already known to fail is not
        queued again, and a full queue defers admission rather than reporting a
        bad sample -- the note plays unfiltered and the next one asks again.
        """
        mark = crossover_hz(mark)
        if self._closed or mark == FULL_RANGE:
            return False
        canonical = self._canonical(path)
        if canonical is None:
            return False
        channel = self._half(channel)
        key = (canonical, mark, channel)
        with self._lock:
            if (key in self._failures or key in self._entries
                    or key in self._pending
                    or len(self._pending) >= self._max_entries):
                return False
            job = _Job(key, canonical, mark, channel, self._generation,
                       self._cancelled)
            self._pending[key] = job
        try:
            self._jobs.put_nowait(job)
        except queue.Full:
            with self._lock:
                self._pending.pop(key, None)
            return False
        with self._lock:
            if self._worker is None:
                self._worker = threading.Thread(target=self._work, daemon=True,
                                                name="crossed-sample-filter")
                self._worker.start()
        return True

    def _is_cancelled(self, job):
        return self._shutdown.is_set() or job.cancelled.is_set()

    def _prepare(self, job):
        """Worker side: decode the sample and run the room's filter over it."""
        decoded = self._decode(job.path)
        if self._is_cancelled(job):
            return None
        channels, rate = decoded.channels, decoded.frequency
        if (type(channels) is not int or channels not in (1, 2)
                or type(rate) is not int or rate < 1 or rate > 384000):
            raise ValueError("unsupported instrument PCM format")
        view = memoryview(decoded.buffer)
        if not 0 < view.nbytes <= self._max_sample_bytes:
            raise ValueError("invalid instrument PCM size")
        pcm = bytes(view)
        del view, decoded
        if self._is_cancelled(job):
            return None
        filtered, out_channels = crossed_pcm(pcm, channels, rate, job.mark,
                                             job.channel)
        del pcm
        return _Prepared(job, filtered, out_channels, rate)

    def _work(self):
        while not self._shutdown.is_set():
            try:
                job = self._jobs.get(timeout=0.05)
            except queue.Empty:
                continue
            if job is None:
                break
            if self._is_cancelled(job):
                continue
            try:
                prepared = self._prepare(job)
            except Exception:
                # Never keep a decoder's exception, traceback or a whole
                # decoded file alive: a failure is one bit per (sample, mark,
                # half), and the caller keeps playing the unfiltered sample.
                prepared = _Prepared(job, failed=True)
            if prepared is None:
                with self._lock:
                    self._pending.pop(job.key, None)
                continue
            while not self._is_cancelled(job):
                try:
                    self._done.put(prepared, timeout=0.05)
                    break
                except queue.Full:
                    continue
            prepared = None

    def _fail(self, key):
        with self._lock:
            self._pending.pop(key, None)
            self._failures[key] = True
            self._failures.move_to_end(key)
            while len(self._failures) > self._max_failures:
                self._failures.popitem(last=False)

    def _store(self, key, buffer, byte_size):
        with self._lock:
            self._pending.pop(key, None)
            self._entries[key] = buffer
            self._entries.move_to_end(key)
            self._sizes[key] = byte_size
            self._sizes.move_to_end(key)
            self._bytes += byte_size
            while len(self._entries) > 1 and (
                    len(self._entries) > self._max_entries
                    or self._bytes > self._max_bytes):
                _, size = self._sizes.popitem(last=False)
                self._entries.popitem(last=False)
                self._bytes -= size

    def pump(self, max_uploads=1, budget_seconds=0.002):
        """Upload a bounded amount of finished copies on the audio owner.

        The budget is checked between uploads; one backend upload cannot be
        interrupted, and the count alone would let a burst of short notes put
        four of them in one frame.
        """
        if self._closed or max_uploads <= 0 or budget_seconds <= 0:
            return 0
        deadline = self._clock() + budget_seconds
        count = 0
        while count < max_uploads and self._clock() < deadline:
            if self._uploading is None:
                try:
                    prepared = self._done.get_nowait()
                except queue.Empty:
                    break
                job = prepared.job
                with self._lock:
                    current = self._pending.get(job.key)
                if (job.generation != self._generation or job.cancelled.is_set()
                        or current is not job):
                    prepared = None
                    continue
                self._uploading = prepared
            prepared = self._uploading
            if prepared.failed:
                self._fail(prepared.job.key)
                self._uploading = None
                count += 1
                continue
            try:
                buffer = self._upload(prepared.pcm, prepared.channels,
                                      prepared.rate)
                if buffer is None:
                    raise ValueError("crossed buffer upload failed")
            except Exception:
                self._fail(prepared.job.key)
                self._uploading = None
                count += 1
                continue
            self._store(prepared.job.key, buffer, len(prepared.pcm))
            self._uploading = None
            count += 1
        return count

    @staticmethod
    def _drain(q):
        while True:
            try:
                q.get_nowait()
            except queue.Empty:
                return

    def clear(self):
        """Drop every copy, cancelling the copies being made for this map."""
        with self._lock:
            self._cancelled.set()
            self._generation += 1
            self._cancelled = threading.Event()
            self._pending.clear()
            self._entries.clear()
            self._sizes.clear()
            self._failures.clear()
            self._bytes = 0
        self._uploading = None
        self._drain(self._jobs)
        self._drain(self._done)

    def close(self):
        """Cancel and signal the worker; never join on the game's owner."""
        if self._closed:
            return
        self._closed = True
        self._shutdown.set()
        self.clear()
        try:
            self._jobs.put_nowait(None)
        except queue.Full:
            pass

    def stats(self):
        """Small owner-side counters; contains no paths, PCM or buffers."""
        with self._lock:
            return {"pending": len(self._pending), "entries": len(self._entries),
                    "bytes": self._bytes, "failures": len(self._failures),
                    "queued": self._jobs.qsize(), "done": self._done.qsize(),
                    "uploading": self._uploading is not None,
                    "closed": self._closed}
