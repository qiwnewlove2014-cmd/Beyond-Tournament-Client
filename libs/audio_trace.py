"""JSONL tracing for the two-machine Party Sync / cinema audio investigation.

The trace is deliberately opt-in and separate from ``logger``: a frame is
written at 50-100 Hz, which must never put a synchronous flush/fsync on an
audio thread. Events go through a bounded queue to one daemon writer, so a
stalled disk drops trace lines rather than audio.

Enable it with a marker file named ``audio_trace.on`` next to the client (or
in the working directory), or with ``BT_AUDIO_TRACE=1``. Each process writes
one ``audio_trace_<timestamp>_<pid>.jsonl`` beside that marker. Tests call
``start(path=...)`` directly; no test run starts the sink by itself.
"""

from __future__ import annotations

import atexit
import json
import os
import queue
import socket
import sys
import threading
import time


_ENV = "BT_AUDIO_TRACE"
_MARKER = "audio_trace.on"

_LOCK = threading.RLock()
_WRITER = None
_PATH = None
_SESSION = None
_THREAD = None
_CLOSED = False
_DROPPED = 0
_QUEUE_LIMIT = 32768
try:
    _HOST = str(socket.gethostname())[:80]
except Exception:
    _HOST = ""


def _console(line):
    try:
        print(line)
    except UnicodeEncodeError:
        print(line.encode("ascii", "replace").decode("ascii"))


def _candidate_markers():
    """Every place a marker may live, nearest to the real launch first."""
    found = []
    seen = set()

    def add(path):
        try:
            path = os.path.abspath(path)
        except Exception:
            return
        if path not in seen:
            seen.add(path)
            found.append(path)

    try:
        add(os.path.join(os.getcwd(), _MARKER))
    except Exception:
        pass
    try:
        # libs/audio_trace.py -> the client directory that owns beyond_tournament.
        add(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         _MARKER))
    except Exception:
        pass
    try:
        if sys.argv and sys.argv[0]:
            add(os.path.join(os.path.dirname(os.path.abspath(sys.argv[0])),
                             _MARKER))
    except Exception:
        pass
    return found


def _marker_path():
    for path in _candidate_markers():
        try:
            if os.path.isfile(path):
                return path
        except OSError:
            continue
    return None


def _env_enabled():
    value = str(os.environ.get(_ENV, "")).strip().lower()
    if value in ("0", "false", "no", "off"):
        return False
    return value in ("1", "true", "yes", "on")


def _start_path():
    marker = _marker_path()
    if marker is not None:
        return marker
    if _env_enabled():
        return os.path.join(os.getcwd(), _MARKER)
    return None


def enabled():
    """True once this process has an open trace sink."""
    return _WRITER is not None


def path():
    return _PATH


def session():
    return _SESSION


def _clean(value):
    """Keep a JSON field small and made of primitives only."""
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            return None
        return round(value, 3)
    if isinstance(value, str):
        return value.replace("\r", " ").replace("\n", " ")[:160]
    try:
        return str(value)[:160]
    except Exception:
        return None


def event(name, context=None, **fields):
    """Queue one trace event; never wait for disk and never raise.

    ``context`` is a dict from :func:`party_context`. It is merged here, not
    expanded at the call site, so an explicit event field such as ``cabinet``
    can never collide with the same key in the session context.
    """
    sink = _WRITER
    if sink is None:
        return False
    if isinstance(context, dict) and context:
        merged = dict(context)
        merged.update(fields)
        fields = merged
    data = {
        "session": _SESSION,
        "t": round(time.perf_counter(), 6),
        "wall": round(time.time(), 3),
        "pid": os.getpid(),
        "host": _HOST,
        "thread": threading.current_thread().name,
        "event": str(name),
    }
    for key, value in fields.items():
        clean_key = str(key)
        if clean_key in data:
            clean_key = "f_" + clean_key
        data[clean_key] = _clean(value)
    try:
        line = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    except Exception:
        return False
    try:
        sink.put_nowait(line)
        return True
    except queue.Full:
        global _DROPPED
        with _LOCK:
            _DROPPED += 1
        return False
    except Exception:
        return False


def _writer_loop(sink, handle):
    pending = 0
    last_flush = time.monotonic()
    try:
        while True:
            try:
                item = sink.get(timeout=0.25)
            except queue.Empty:
                if pending:
                    try:
                        handle.flush()
                    except Exception:
                        pass
                    pending = 0
                    last_flush = time.monotonic()
                continue
            if item is None:
                break
            try:
                handle.write(item)
                handle.write("\n")
                pending += 1
            except Exception:
                break
            if pending >= 32 or time.monotonic() - last_flush >= 0.25:
                try:
                    handle.flush()
                except Exception:
                    break
                pending = 0
                last_flush = time.monotonic()
    finally:
        try:
            handle.flush()
        except Exception:
            pass
        try:
            handle.close()
        except Exception:
            pass


def start(path=None):
    """Open a trace file, or return None when no marker/environment enabled it.

    ``path`` is for tests and for a deliberate one-off run. No caller other
    than the game entry point and tests should pass it.
    """
    global _WRITER, _PATH, _SESSION, _THREAD, _CLOSED
    with _LOCK:
        if _WRITER is not None:
            return _PATH
        if path is None:
            marker = _start_path()
            if marker is None:
                return None
            directory = os.path.dirname(marker) or os.getcwd()
        else:
            directory = os.path.dirname(os.path.abspath(path)) or os.getcwd()
        try:
            os.makedirs(directory, exist_ok=True)
        except Exception:
            return None
        if path is None:
            stamp = time.strftime("%Y%m%d_%H%M%S")
            path = os.path.join(
                directory, "audio_trace_%s_%s.jsonl" % (stamp, os.getpid())
            )
        path = os.path.abspath(path)
        try:
            handle = open(path, "a", encoding="utf-8", newline="\n",
                          buffering=1)
        except Exception as exc:
            _console("[AUDIO TRACE] cannot open %s: %s" % (path, exc))
            return None
        sink = queue.Queue(maxsize=_QUEUE_LIMIT)
        _SESSION = "%s-%s" % (time.strftime("%Y%m%d-%H%M%S"), os.getpid())
        _PATH = path
        _WRITER = sink
        _CLOSED = False
        thread = threading.Thread(
            target=_writer_loop, args=(sink, handle),
            name="audio-trace-writer", daemon=True,
        )
        _THREAD = thread
        thread.start()
        event(
            "trace_start",
            app="beyond-tournament",
            version=_version(),
            python=sys.version.split()[0],
            host=_HOST,
            cwd=os.getcwd(),
            trace=_PATH,
        )
        _console("[AUDIO TRACE] %s" % _PATH)
        return _PATH


def _version():
    try:
        from .version import version
        return "%s.%s.%s" % (version.major, version.minor, version.patch)
    except Exception:
        return ""


def close():
    """Stop accepting events, flush what was queued, and join briefly."""
    global _WRITER, _THREAD, _CLOSED
    with _LOCK:
        sink, thread = _WRITER, _THREAD
        if sink is None:
            return
        _WRITER = None
        _THREAD = None
        _CLOSED = True
    try:
        sink.put(None, timeout=1.0)
    except queue.Full:
        try:
            sink.get_nowait()
            sink.put_nowait(None)
        except Exception:
            pass
    except Exception:
        pass
    if thread is not None:
        try:
            thread.join(timeout=2.0)
        except Exception:
            pass


def party_context(game=None):
    """The session fields every event can carry: role, id, room and cabinet."""
    out = {}
    try:
        gameplay = getattr(game, "gameplay", None) if game is not None else None
        target = gameplay if gameplay is not None else game
        state = getattr(target, "party_sync", None)
        if state is not None:
            out["party_role"] = str(getattr(state, "role", "") or "")
            out["party_id"] = str(getattr(state, "session_id", "") or "")
            out["cabinet"] = str(getattr(state, "cinema_cabinet", "") or "")
            out["room"] = int(getattr(state, "cinema_room_key", 0) or 0)
    except Exception:
        pass
    return out


def gap_ms(now, previous):
    """Milliseconds since ``previous``, or None for the first sample."""
    if previous is None:
        return None
    try:
        return round(max(0.0, float(now) - float(previous)) * 1000.0, 2)
    except Exception:
        return None


atexit.register(close)
