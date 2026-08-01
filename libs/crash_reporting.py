"""Reliable, privacy-conscious client crash diagnostics.

Reports are intentionally separate from the anti-cheat protocol.  A client
failure must never result in a kick or a moderation action against a player.
"""

import json
import os
import tempfile
import time
import traceback
import uuid


_STATE_FILE = "client_crash_state.json"
_PENDING_FILE = "pending_crash_reports.json"
_MAX_REPORTS = 5
_MAX_CONTEXT = 160
_MAX_TYPE = 120
_MAX_MESSAGE = 900
_MAX_TRACEBACK = 7000


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
    data = _read_json(_PENDING_FILE, {"reports": []})
    reports = data.get("reports", []) if isinstance(data, dict) else []
    return [report for report in reports if isinstance(report, dict)][-_MAX_REPORTS:]


def _save_pending(reports):
    _write_json(_PENDING_FILE, {"reports": reports[-_MAX_REPORTS:]})


def _new_report(context, error_type, message, formatted_traceback, severity):
    return {
        "id": str(uuid.uuid4()),
        "occurred_at": int(time.time()),
        "severity": severity if severity in {"recovered", "fatal"} else "fatal",
        "context": _truncate(context, _MAX_CONTEXT),
        "error_type": _truncate(error_type, _MAX_TYPE),
        "error_message": _truncate(message, _MAX_MESSAGE),
        "traceback": _truncate(formatted_traceback, _MAX_TRACEBACK),
    }


def queue_exception(error, context, severity="fatal", formatted_traceback=None):
    """Store an exception locally first; network delivery is always optional."""
    trace = formatted_traceback or "".join(
        traceback.format_exception(type(error), error, error.__traceback__)
    )
    report = _new_report(context, type(error).__name__, str(error), trace, severity)
    reports = _pending_reports()
    reports.append(report)
    _save_pending(reports)
    return report


def begin_session():
    """Start a session marker without treating an unexplained exit as a crash."""
    _write_json(
        _STATE_FILE,
        {"active": True, "expected_shutdown": False, "started_at": int(time.time())},
    )


def mark_expected_shutdown(reason="normal"):
    _write_json(
        _STATE_FILE,
        {"active": False, "expected_shutdown": True, "reason": _truncate(reason, 80), "ended_at": int(time.time())},
    )


def finish_session():
    mark_expected_shutdown("normal")


def send_pending(game):
    """Send queued diagnostics after authenticated login; keep them until acknowledged."""
    network = getattr(game, "network", None)
    if not network:
        return 0
    try:
        from . import consts
        # Discard legacy generic shutdown reports.  Only a real exception with
        # diagnostic context should notify staff.
        reports = [
            report for report in _pending_reports()
            if report.get("severity") in {"recovered", "fatal"}
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
    reports = [report for report in _pending_reports() if report.get("id") != report_id]
    _save_pending(reports)
