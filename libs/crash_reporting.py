"""Reliable, privacy-conscious client crash diagnostics.

Reports are intentionally separate from the anti-cheat protocol.  A client
failure must never result in a kick or a moderation action against a player.
"""

import contextlib
import json
import os
import tempfile
import threading
import time
import traceback
import uuid


_STATE_FILE = "client_crash_state.json"
_SESSION_DIR = ".client_crash_sessions"
_PENDING_FILE = "pending_crash_reports.json"
_PENDING_LOCK_FILE = "pending_crash_reports.lock"
_MAX_REPORTS = 5
_MAX_CONTEXT = 160
_MAX_TYPE = 120
_MAX_MESSAGE = 900
_MAX_TRACEBACK = 7000
_MAX_DIAGNOSTIC_FRAMES = 4
_MAX_DIAGNOSTIC_LOCALS = 4000
_SESSION_ID = str(uuid.uuid4())
_SESSION_FILE = os.path.join(_SESSION_DIR, f"{_SESSION_ID}.json")
_REPORT_LOCK = threading.RLock()
_SENSITIVE_LOCAL_PARTS = {
    "password", "passwd", "token", "secret", "key", "auth", "credential",
    "cookie", "message", "text", "chat", "content", "payload", "email",
    "username",
}
_DIAGNOSTIC_LOCAL_HINTS = {
    "path", "file", "filename", "kind", "mode", "state", "status", "index",
    "count", "size", "length", "channel", "duration", "sample_rate", "x", "y", "z",
}


@contextlib.contextmanager
def _pending_process_lock():
    """Serialize queue updates made by separate client processes."""
    lock_file = None
    locked = False
    lock_kind = ""
    try:
        lock_file = open(_PENDING_LOCK_FILE, "a+b")
        lock_file.seek(0, os.SEEK_END)
        if lock_file.tell() == 0:
            lock_file.write(b"\0")
            lock_file.flush()
        lock_file.seek(0)
        if os.name == "nt":
            import msvcrt
            deadline = time.monotonic() + 0.75
            while True:
                try:
                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(0.01)
            lock_kind = "windows"
        else:
            import fcntl
            deadline = time.monotonic() + 0.75
            while True:
                try:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(0.01)
            lock_kind = "posix"
        locked = True
    except (ImportError, OSError):
        # Diagnostics must never stop the game from launching. The in-process
        # RLock still protects threads if an OS-level lock is unavailable.
        locked = False
    try:
        yield
    finally:
        if locked and lock_file is not None:
            try:
                lock_file.seek(0)
                if lock_kind == "windows":
                    import msvcrt
                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            except (ImportError, OSError):
                pass
        if lock_file is not None:
            try:
                lock_file.close()
            except OSError:
                pass


def _truncate(value, limit):
    return str(value or "")[:limit]


def _read_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as report_file:
            value = json.load(report_file)
            return value if isinstance(value, type(default)) else default
    except (OSError, ValueError, TypeError):
        return default


def _write_json(path, value):
    """Atomically persist diagnostics so a sudden exit cannot corrupt them."""
    directory = os.path.dirname(os.path.abspath(path))
    try:
        fd, temporary_path = tempfile.mkstemp(prefix="bt_crash_", suffix=".tmp", dir=directory)
        with os.fdopen(fd, "w", encoding="utf-8") as report_file:
            json.dump(value, report_file, ensure_ascii=False, separators=(",", ":"))
        os.replace(temporary_path, path)
        return True
    except OSError:
        try:
            if "temporary_path" in locals() and os.path.exists(temporary_path):
                os.unlink(temporary_path)
        except OSError:
            pass
        return False


def _pending_reports():
    with _REPORT_LOCK:
        data = _read_json(_PENDING_FILE, {"reports": []})
        reports = data.get("reports", []) if isinstance(data, dict) else []
        return [report for report in reports if isinstance(report, dict)][-_MAX_REPORTS:]


def _save_pending(reports):
    with _REPORT_LOCK:
        return _write_json(_PENDING_FILE, {"reports": reports[-_MAX_REPORTS:]})


def _portable_filename(filename):
    try:
        return os.path.relpath(filename, os.getcwd())
    except (OSError, ValueError):
        return os.path.basename(filename)


def _safe_exception_frames(error):
    """Capture small primitive locals without collecting credentials or user text."""
    frames = []
    trace = error.__traceback__
    while trace is not None:
        frames.append(trace)
        trace = trace.tb_next

    diagnostics = []
    remaining = _MAX_DIAGNOSTIC_LOCALS
    for frame_trace in frames[-_MAX_DIAGNOSTIC_FRAMES:]:
        frame = frame_trace.tb_frame
        safe_locals = {}
        local_names = sorted(
            frame.f_locals,
            key=lambda item: (0 if item.lower() in _DIAGNOSTIC_LOCAL_HINTS else 1, item),
        )
        for name in local_names:
            lowered = name.lower()
            if name == "self" or name.startswith("__") or any(part in lowered for part in _SENSITIVE_LOCAL_PARTS):
                continue
            value = frame.f_locals[name]
            if value is not None and type(value) not in (str, int, float, bool):
                continue
            if isinstance(value, str):
                rendered = repr(value[:280])
            else:
                rendered = repr(value)
            rendered = _truncate(rendered, 300).replace("\r", " ").replace("\n", " ")
            cost = len(name) + len(rendered)
            if cost > remaining:
                break
            safe_locals[_truncate(name, 80)] = rendered
            remaining -= cost
            if len(safe_locals) >= 8:
                break
        diagnostics.append({
            "file": _truncate(_portable_filename(frame.f_code.co_filename), 260),
            "line": int(frame_trace.tb_lineno),
            "function": _truncate(frame.f_code.co_name, 120),
            "locals": safe_locals,
        })
    return diagnostics


def _origin_from_frames(frames):
    if not frames:
        return {}
    frame = frames[-1]
    return {
        "file": frame.get("file", ""),
        "line": frame.get("line", 0),
        "function": frame.get("function", ""),
    }


def _debug_log_tail(limit=5000):
    try:
        with open("client_debug.log", "rb") as log_file:
            log_file.seek(0, os.SEEK_END)
            size = log_file.tell()
            log_file.seek(max(0, size - limit), os.SEEK_SET)
            return log_file.read(limit).decode("utf-8", errors="replace")
    except OSError:
        return ""


def _get_freeze_trace():
    freeze_path = os.path.join(_SESSION_DIR, "last_freeze.txt")
    if os.path.exists(freeze_path):
        try:
            with open(freeze_path, "r", encoding="utf-8") as f:
                content = f.read()
            try:
                os.unlink(freeze_path)
            except OSError:
                pass
            return content
        except OSError:
            pass
    return None


def _process_matches_session(session):
    try:
        import psutil
        pid = int(session.get("pid", 0))
        created_at = float(session.get("process_created_at", 0))
        process = psutil.Process(pid)
        return process.is_running() and abs(process.create_time() - created_at) < 2
    except (ImportError, OSError, TypeError, ValueError):
        return False
    except Exception:
        return False


def _new_report(context, error_type, message, formatted_traceback, severity, frames=None):
    frames = frames or []
    report = {
        "id": str(uuid.uuid4()),
        "occurred_at": int(time.time()),
        "severity": severity if severity in {"recovered", "fatal", "unclean_exit"} else "fatal",
        "context": _truncate(context, _MAX_CONTEXT),
        "error_type": _truncate(error_type, _MAX_TYPE),
        "error_message": _truncate(message, _MAX_MESSAGE),
        "traceback": _truncate(formatted_traceback, _MAX_TRACEBACK),
    }
    if frames:
        report["origin"] = _origin_from_frames(frames)
        report["frames"] = frames
    return report


def queue_exception(error, context, severity="fatal", formatted_traceback=None):
    """Store an exception locally first; network delivery is always optional."""
    trace = formatted_traceback or "".join(
        traceback.format_exception(type(error), error, error.__traceback__)
    )
    frames = _safe_exception_frames(error)
    report = _new_report(context, type(error).__name__, str(error), trace, severity, frames)
    with _REPORT_LOCK, _pending_process_lock():
        reports = _pending_reports()
        reports.append(report)
        _save_pending(reports)
    return report


def begin_session():
    """Queue stale sessions as unclean exits, while preserving live parallel clients."""
    try:
        os.makedirs(_SESSION_DIR, exist_ok=True)
        session_tracking_available = True
    except OSError:
        session_tracking_available = False
    log_tail = _debug_log_tail()
    freeze_trace = _get_freeze_trace()
    trace_to_use = freeze_trace or log_tail or "No client debug log was available."

    # Migrate the single-session marker used by older clients once.
    with _REPORT_LOCK, _pending_process_lock():
        legacy = _read_json(_STATE_FILE, {})
        legacy_migrated = True
        if legacy.get("active") and not legacy.get("expected_shutdown"):
            report = _new_report(
                "Previous client session",
                "UncleanExit",
                "The previous client process ended without a clean shutdown.",
                trace_to_use,
                "unclean_exit",
            )
            reports = _pending_reports()
            reports.append(report)
            legacy_migrated = _save_pending(reports)
        if legacy_migrated:
            _write_json(_STATE_FILE, {"active": False, "migrated": True})

    try:
        if not session_tracking_available:
            raise OSError("session directory unavailable")
        session_files = [
            os.path.join(_SESSION_DIR, name)
            for name in os.listdir(_SESSION_DIR)
            if name.endswith(".json") and name != os.path.basename(_SESSION_FILE)
        ]
    except OSError:
        session_files = []

    for session_path in session_files[:20]:
        with _REPORT_LOCK, _pending_process_lock():
            if not os.path.exists(session_path):
                continue
            session = _read_json(session_path, {})
            if session and _process_matches_session(session):
                continue
            report = _new_report(
                "Previous client session",
                "UncleanExit",
                "A previous client process ended unexpectedly or was terminated.",
                trace_to_use,
                "unclean_exit",
            )
            reports = _pending_reports()
            reports.append(report)
            saved = _save_pending(reports)
            if saved:
                try:
                    os.unlink(session_path)
                except OSError:
                    pass

    try:
        import psutil
        process_created_at = psutil.Process(os.getpid()).create_time()
    except Exception:
        process_created_at = time.time()
    if session_tracking_available:
        _write_json(_SESSION_FILE, {
            "id": _SESSION_ID,
            "pid": os.getpid(),
            "process_created_at": process_created_at,
            "started_at": int(time.time()),
        })


def mark_expected_shutdown(reason="normal"):
    try:
        os.unlink(_SESSION_FILE)
    except OSError:
        pass
    _write_json(_STATE_FILE, {
        "active": False,
        "expected_shutdown": True,
        "reason": _truncate(reason, 80),
        "ended_at": int(time.time()),
    })


def finish_session():
    mark_expected_shutdown("normal")


def send_pending(game):
    """Send queued diagnostics after authenticated login; keep them until acknowledged."""
    network = getattr(game, "network", None)
    if not network:
        return 0
    try:
        from . import consts
        with _REPORT_LOCK, _pending_process_lock():
            # Discard legacy generic shutdown reports. Keep real exceptions and
            # stale session markers that can help diagnose native process exits.
            reports = [
                report for report in _pending_reports()
                if report.get("severity") in {"recovered", "fatal", "unclean_exit"}
            ]
            _save_pending(reports)
        for report in reports:
            network.send(consts.CHANNEL_MISC, "client_crash_report", report)
        return len(reports)
    except Exception:
        return 0


def acknowledge(report_id):
    if not isinstance(report_id, str):
        return
    with _REPORT_LOCK, _pending_process_lock():
        reports = [report for report in _pending_reports() if report.get("id") != report_id]
        _save_pending(reports)
