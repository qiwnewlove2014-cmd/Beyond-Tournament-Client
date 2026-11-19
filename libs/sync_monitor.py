"""Read-only timing telemetry for every audio path.

Phase 1 records what the shipping paths already know.  Nothing in this module
changes a buffer, a playout instant, an OpenAL source, a frame or a packet
decision: a caller may only add an observation, and a missing monitor must be
as harmless as a monitor that is present.

The clock half is a four-timestamp exchange around the existing ping:

    t1 client send, t2 server receive, t3 server send, t4 client receive

The minimum-RTT sample is kept as the best one.  ``offset`` is the server
clock minus the client clock; ``uncertainty`` is the part of the round trip
that cannot be attributed to the server's own turnaround.  This is a report,
not a clock correction: no other module reads it to change audio.

The path half stores one small aggregate per audio path (voice, PA, music,
the live jam leg, a cinema room and live-note scheduling).  A path is named by
the caller; a path that has not reported anything is simply absent from the
snapshot.  Counters are monotonic for the life of the monitor and reset only
through ``reset()``.
"""

from __future__ import annotations

import math
import threading
import time
from collections import deque

VOICE = "voice"
PA = "pa"
MUSIC = "music"
MUSIC_LIVE = "music_live"
CINEMA = "cinema"
JAM = "jam"

# One clock sample is enough for the report; keep a few so the minimum-RTT
# choice can change when the first sample was a jittery one.
CLOCK_HISTORY = 8

# A path aggregate is deliberately small: one player at 50 frames/s must not
# turn telemetry into a second audio queue.
EMA_ALPHA = 0.25

_PATH_ORDER = (VOICE, PA, MUSIC, MUSIC_LIVE, CINEMA, JAM)


def _finite(value):
    """A finite float, or None.  Telemetry never raises on a bad packet."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def _ema(current, sample, alpha=EMA_ALPHA):
    if current is None:
        return sample
    return current + (sample - current) * alpha


def _count(value):
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _scalar_extra(extra):
    """Keep report fields printable and JSON-friendly, never arbitrary objects."""
    clean = {}
    for key, value in extra.items():
        if value is None or isinstance(value, (bool, int, float, str)):
            clean[str(key)] = value
    return clean


class PathStats:
    """The last observation and the running counters for one audio path.

    The fields have one meaning across all paths:

    * ``buffer_ms`` -- playout delay/queue still held at this observation;
    * ``jitter_ms`` -- measured arrival jitter;
    * ``drift_ms`` -- ``actual_ms - expected_ms``, only when two playheads
      can be compared (jam/cinema). It is not a queue depth;
    * ``frame_ms`` -- one decoded music frame, carried as an extra.

    ``samples`` counts observations, not audio frames: voice/PA report once
    per received packet, music once per queued frame, and jam once per note.
    The last-observation fields are replaced by every sample: a field the
    latest sample did not measure is None, never the previous sample's value.
    ``*_ema`` and ``buffer_max_ms`` are the historical aggregates and remain.
    """

    __slots__ = (
        "samples", "last_at", "expected_ms", "actual_ms", "drift_ms",
        "drift_ema_ms", "jitter_ms", "jitter_ema_ms", "buffer_ms",
        "buffer_max_ms", "underruns", "late", "dropped", "extra",
    )

    def __init__(self):
        self.samples = 0
        self.last_at = None
        self.expected_ms = None
        self.actual_ms = None
        self.drift_ms = None
        self.drift_ema_ms = None
        self.jitter_ms = None
        self.jitter_ema_ms = None
        self.buffer_ms = None
        self.buffer_max_ms = None
        self.underruns = 0
        self.late = 0
        self.dropped = 0
        self.extra = {}

    def note(self, now, *, expected_ms=None, actual_ms=None, jitter_ms=None,
             buffer_ms=None, underruns=0, late=0, dropped=0, extra=None):
        self.samples += 1
        self.last_at = now

        expected = _finite(expected_ms)
        actual = _finite(actual_ms)
        self.expected_ms = expected
        self.actual_ms = actual
        if expected is not None and actual is not None:
            drift = actual - expected
            self.drift_ms = drift
            self.drift_ema_ms = _ema(self.drift_ema_ms, drift)
        else:
            self.drift_ms = None
            # A clock-aimed note has no second playhead; keeping the old stamp
            # would report drift for a mode that is not being measured.
            self.drift_ema_ms = None

        jitter = _finite(jitter_ms)
        self.jitter_ms = jitter
        if jitter is not None:
            self.jitter_ema_ms = _ema(self.jitter_ema_ms, jitter)

        buffer = _finite(buffer_ms)
        self.buffer_ms = buffer
        if buffer is not None:
            if self.buffer_max_ms is None or buffer > self.buffer_max_ms:
                self.buffer_max_ms = buffer

        self.underruns += _count(underruns)
        self.late += _count(late)
        self.dropped += _count(dropped)
        self.extra = _scalar_extra(extra or {})

    def snapshot(self, now):
        return {
            "samples": self.samples,
            "age_ms": None if self.last_at is None
            else round(max(0.0, now - self.last_at) * 1000.0, 3),
            "expected_ms": self.expected_ms,
            "actual_ms": self.actual_ms,
            "drift_ms": self.drift_ms,
            "drift_ema_ms": self.drift_ema_ms,
            "jitter_ms": self.jitter_ms,
            "jitter_ema_ms": self.jitter_ema_ms,
            "buffer_ms": self.buffer_ms,
            "buffer_max_ms": self.buffer_max_ms,
            "underruns": self.underruns,
            "late": self.late,
            "dropped": self.dropped,
            "extra": dict(self.extra),
        }


class SyncMonitor:
    """A thread-safe, read-only sink for clock and per-path timing samples.

    ``record`` is called from the network, decode and audio-owner threads, so
    every entry point takes the same lock.  The lock never covers OpenAL work
    and never calls back into the audio path.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._clock_samples = deque(maxlen=CLOCK_HISTORY)
        self._clock_count = 0
        self._best_clock = None
        self._paths = {}

    # ------------------------------------------------------------- clock
    def clock_sample(self, t1, t2, t3, t4, now=None):
        """Fold one four-timestamp ping exchange into the clock report.

        ``t1``/``t4`` are the client's milliseconds around the probe, and
        ``t2``/``t3`` are the Server's receive/send milliseconds.  Returns the
        sample, or None when the packet is not a usable exchange.  It never
        changes a local clock.
        """
        values = [_finite(value) for value in (t1, t2, t3, t4)]
        if any(value is None for value in values):
            return None
        t1, t2, t3, t4 = values
        if t4 < t1 or t3 < t2:
            return None
        rtt = t4 - t1
        server_turnaround = t3 - t2
        uncertainty = max(0.0, (rtt - server_turnaround) / 2.0)
        sample = {
            "at": time.monotonic() if now is None else float(now),
            "rtt_ms": rtt,
            "server_turnaround_ms": server_turnaround,
            "uncertainty_ms": uncertainty,
            "offset_ms": ((t2 + t3) / 2.0) - ((t1 + t4) / 2.0),
        }
        with self._lock:
            self._clock_count += 1
            self._clock_samples.append(sample)
            if (self._best_clock is None
                    or rtt < self._best_clock["rtt_ms"]):
                self._best_clock = dict(sample)
        return sample

    def clock_snapshot(self, now=None):
        now = time.monotonic() if now is None else float(now)
        with self._lock:
            best = dict(self._best_clock) if self._best_clock else None
            last = dict(self._clock_samples[-1]) if self._clock_samples else None
            count = self._clock_count
        for sample in (best, last):
            if sample is not None:
                sample["age_ms"] = round(
                    max(0.0, now - sample.pop("at")) * 1000.0, 3)
        return {"samples": count, "best": best, "last": last}

    # ------------------------------------------------------------- paths
    def record(self, path, *, expected_ms=None, actual_ms=None, jitter_ms=None,
               buffer_ms=None, underruns=0, late=0, dropped=0, now=None,
               **extra):
        """Observe one path.  Safe to call at audio cadence; changes nothing."""
        if not path:
            return None
        name = str(path)[:40]
        now = time.monotonic() if now is None else float(now)
        with self._lock:
            stats = self._paths.get(name)
            if stats is None:
                stats = self._paths[name] = PathStats()
            stats.note(
                now,
                expected_ms=expected_ms,
                actual_ms=actual_ms,
                jitter_ms=jitter_ms,
                buffer_ms=buffer_ms,
                underruns=underruns,
                late=late,
                dropped=dropped,
                extra=extra,
            )
        return stats

    def snapshot(self, path=None, now=None):
        now = time.monotonic() if now is None else float(now)
        with self._lock:
            if path is not None:
                stats = self._paths.get(str(path)[:40])
                return None if stats is None else stats.snapshot(now)
            paths = {
                name: stats.snapshot(now)
                for name, stats in self._paths.items()
            }
        return {"clock": self.clock_snapshot(now), "paths": paths}

    def report(self, now=None):
        """A compact human-readable report for a log line or a diagnostics page."""
        now = time.monotonic() if now is None else float(now)
        clock = self.clock_snapshot(now)
        lines = []
        best = clock.get("best")
        if best:
            lines.append(
                "clock: rtt={:.1f}ms offset={:.1f}ms uncertainty={:.1f}ms "
                "samples={}".format(
                    best["rtt_ms"], best["offset_ms"],
                    best["uncertainty_ms"], clock["samples"]))
        else:
            lines.append("clock: no samples")
        with self._lock:
            names = [name for name in _PATH_ORDER if name in self._paths]
            names.extend(sorted(name for name in self._paths
                                if name not in _PATH_ORDER))
            snapshots = {name: stats.snapshot(now)
                         for name, stats in self._paths.items()}
        for name in names:
            stats = snapshots[name]
            parts = ["{}: samples={}".format(name, stats["samples"])]
            labels = (("drift_ema_ms", "drift"),
                      ("jitter_ema_ms", "jitter"),
                      ("buffer_ms", "buffer"))
            for key, label in labels:
                if stats[key] is not None:
                    parts.append("{}={:.1f}ms".format(label, stats[key]))
            for key in ("underruns", "late", "dropped"):
                if stats[key]:
                    parts.append("{}={}".format(key, stats[key]))
            lines.append(" ".join(parts))
        return "\n".join(lines)

    def reset(self):
        with self._lock:
            self._clock_samples.clear()
            self._clock_count = 0
            self._best_clock = None
            self._paths.clear()


def clock_sample(game, t1, t2, t3, t4, now=None):
    """Module-level safe entry point: a game without a monitor is a no-op."""
    try:
        monitor = getattr(game, "sync_monitor", None)
        if monitor is not None:
            return monitor.clock_sample(t1, t2, t3, t4, now=now)
    except Exception:
        pass
    return None


def record(game, path, **fields):
    """Module-level safe entry point used by the audio paths."""
    try:
        monitor = getattr(game, "sync_monitor", None)
        if monitor is not None:
            return monitor.record(path, **fields)
    except Exception:
        pass
    return None
