"""Isolated, bounded stream-URL extraction without importing game/audio code.

The calling worker owns one loopback listener and one exact child process.
IPC never uses console handles and carries only authenticated, size-bounded
JSON. URLs, headers and errors are never printed. There is no in-process
yt-dlp fallback or result cache. worker_main is for the private child ONLY.
"""

import contextlib
import hmac
import json
import os
import re
import secrets
import socket
import struct
import subprocess
import sys
import threading
import time
from urllib.parse import urlsplit


RESOLVER_FLAG = "--bt-youtube-resolver"
_TOTAL_TIMEOUT = 25.0
_CHILD_LIFETIME = 30.0
_POLL = 0.05
_MAX_FRAME = 65536
_MAX_URL = 16384
_MAX_HEADERS = 32
_MAX_HEADER_BYTES = 32768
_SLOTS = threading.BoundedSemaphore(2)
_TOKEN = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_HEADER = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]{1,128}\Z", re.ASCII)


class _Cancelled(Exception):
    pass


def _valid_url(value):
    if type(value) is not str or not 1 <= len(value) <= _MAX_URL:
        return False
    if any(ord(char) <= 32 or ord(char) == 127 for char in value):
        return False
    try:
        parsed = urlsplit(value)
        return (parsed.scheme in ("http", "https") and bool(parsed.hostname)
                and parsed.port != 0)
    except (ValueError, UnicodeError):
        return False


def _validated_info(info):
    if type(info) is not dict or not _valid_url(info.get("url")):
        return None
    headers = info.get("http_headers", {})
    if headers is None:
        headers = {}
    if type(headers) is not dict or len(headers) > _MAX_HEADERS:
        return None
    validated, seen, size = {}, set(), 0
    for name, value in headers.items():
        if (type(name) is not str or _HEADER.fullmatch(name) is None
                or type(value) is not str or len(value) > 4096
                or any(ord(char) < 32 or ord(char) == 127 for char in value)
                or name.lower() in seen):
            return None
        try:
            size += len(name.encode("ascii")) + len(value.encode("utf-8"))
        except UnicodeError:
            return None
        if size > _MAX_HEADER_BYTES:
            return None
        seen.add(name.lower())
        validated[name] = value  # Preserve exact paired values, especially UA.
    hostname = urlsplit(info["url"]).hostname.lower()
    if (hostname == "googlevideo.com" or hostname.endswith(".googlevideo.com")) and not validated:
        return None  # Signed Google media URLs cannot lose their paired headers.
    result = {"url": info["url"], "http_headers": validated}
    # Optional track duration (seconds). Used by the personal Music Bot to
    # schedule crossfades; ignored by the jukebox media cache. Malformed or
    # out-of-range values are dropped, never fatal.
    duration = info.get("duration")
    if type(duration) in (int, float) and not isinstance(duration, bool):
        if 0.0 < float(duration) <= 86400.0:
            result["duration"] = float(duration)
    return result


def _check(deadline, cancelled=None):
    if cancelled is not None and cancelled():
        raise _Cancelled()
    if time.monotonic() >= deadline:
        raise TimeoutError()


def _recv_exact(connection, size, deadline, cancelled=None):
    data = bytearray()
    while len(data) < size:
        _check(deadline, cancelled)
        try:
            chunk = connection.recv(min(8192, size - len(data)))
        except socket.timeout:
            continue
        if not chunk:
            raise EOFError()
        data.extend(chunk)
    return bytes(data)


def _recv_json(connection, deadline, cancelled=None):
    size = struct.unpack("!I", _recv_exact(connection, 4, deadline, cancelled))[0]
    if not 0 < size <= _MAX_FRAME:
        raise ValueError("invalid resolver frame")
    return json.loads(_recv_exact(connection, size, deadline, cancelled).decode("utf-8"))


def _send_json(connection, payload, deadline, cancelled=None):
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"),
                         allow_nan=False).encode("utf-8")
    if not 0 < len(encoded) <= _MAX_FRAME:
        raise ValueError("invalid resolver frame")
    remaining = memoryview(struct.pack("!I", len(encoded)) + encoded)
    while remaining:
        _check(deadline, cancelled)
        try:
            sent = connection.send(remaining)
        except socket.timeout:
            continue
        if sent <= 0:
            raise EOFError()
        remaining = remaining[sent:]


def _command(port, token):
    args = [sys.executable]
    if not (getattr(sys, "frozen", False) or "__compiled__" in globals()):
        args.append(os.path.abspath(os.path.join(os.path.dirname(__file__),
                                                os.pardir, "beyond_tournament.py")))
    return args + [RESOLVER_FLAG, str(port), token]


def _working_directory():
    if getattr(sys, "frozen", False) or "__compiled__" in globals():
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))


def _child_environment():
    # Use the known interpreter/executable and its installed dependencies, not
    # an inherited arbitrary import override or interactive startup script.
    excluded = {"PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "PYTHONINSPECT", "PYTHONBREAKPOINT"}
    environment = {key: value for key, value in os.environ.items() if key.upper() not in excluded}
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    return environment


def _reap_owned(process):
    # Never enumerate processes, use a PID from IPC, or terminate another game.
    if process is None:
        return
    with contextlib.suppress(Exception):
        if process.poll() is None:
            process.kill()
    with contextlib.suppress(Exception):
        process.wait(timeout=1.0)


def _accept_authenticated(listener, token, process, deadline, cancelled):
    while True:
        _check(deadline, cancelled)
        if process.poll() is not None:
            raise EOFError()
        try:
            connection, _ = listener.accept()
        except socket.timeout:
            continue
        connection.settimeout(_POLL)
        try:
            hello = _recv_json(connection, min(deadline, time.monotonic() + 1.0), cancelled)
            if (type(hello) is dict and set(hello) == {"token"}
                    and type(hello["token"]) is str
                    and _TOKEN.fullmatch(hello["token"]) is not None
                    and hmac.compare_digest(token, hello["token"])):
                return connection
        except _Cancelled:
            connection.close()
            raise
        except Exception:
            pass
        connection.close()


def resolve_stream_info(url, *, cancelled=None):
    """Return paired stream URL/headers, or None; NEVER run on the main thread.

    The 25-second deadline includes waiting for one of two subprocess slots.
    Cancellation is polled at most every 50 ms except OS process creation and
    final bounded reap. Failures never fall back to extraction in this process.
    """
    if threading.current_thread() is threading.main_thread() or not _valid_url(url):
        return None
    deadline = time.monotonic() + _TOTAL_TIMEOUT
    acquired = False
    process = listener = connection = None
    try:
        while not acquired:
            _check(deadline, cancelled)
            acquired = _SLOTS.acquire(timeout=min(_POLL, max(0.0, deadline - time.monotonic())))
        _check(deadline, cancelled)
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(2)
        listener.settimeout(_POLL)
        token = secrets.token_hex(32)
        process = subprocess.Popen(
            _command(listener.getsockname()[1], token), shell=False,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            close_fds=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            cwd=_working_directory(), env=_child_environment(),
        )
        connection = _accept_authenticated(listener, token, process, deadline, cancelled)
        _send_json(connection, {"url": url}, deadline, cancelled)
        response = _recv_json(connection, deadline, cancelled)
        _check(deadline, cancelled)
        if type(response) is not dict or set(response) != {"result"}:
            return None
        return _validated_info(response["result"])
    except Exception:
        return None
    finally:
        if connection is not None:
            with contextlib.suppress(Exception):
                connection.close()
        if listener is not None:
            with contextlib.suppress(Exception):
                listener.close()
        _reap_owned(process)
        if acquired:
            _SLOTS.release()


class _QuietLogger:
    def debug(self, *_args, **_kwargs):
        pass

    warning = error = debug


def _extract(url, ydl_factory=None):
    try:
        if ydl_factory is None:
            import yt_dlp
            ydl_factory = yt_dlp.YoutubeDL
        options = {
            "format": "best[acodec!=none][vcodec!=none][height<=360]/bestaudio/best",
            "quiet": True, "no_warnings": True, "noprogress": True,
            "extractor_args": {"youtube": {"player_client": ["android", "web"]}},
            "noplaylist": True, "skip_download": True, "socket_timeout": 6,
            "retries": 1, "extractor_retries": 1, "fragment_retries": 0,
            "file_access_retries": 0, "cachedir": False, "logger": _QuietLogger(),
            "js_runtimes": {}, "remote_components": [], "postprocessors": [],
            "enable_file_urls": False,
        }
        with ydl_factory(options) as ydl:
            info = ydl.extract_info(url, download=False)
            if type(info) is not dict:
                return None
            headers = info.get("http_headers")
            # yt-dlp returns HTTPHeaderDict, not an exact dict. Normalize only
            # at this library boundary; keep the JSON validator strict and all
            # paired header values intact. Do not copy unbounded header sets.
            if isinstance(headers, dict) and len(headers) <= _MAX_HEADERS:
                headers = dict(headers)
            return _validated_info({
                "url": info.get("url"),
                "http_headers": headers,
                "duration": info.get("duration"),
            })
    except Exception:
        return None


def _deny_helpers(event, args):
    if (event == "subprocess.Popen" or event in ("os.system", "os.fork", "os.forkpty")
            or event.startswith(("os.exec", "os.spawn", "os.posix_spawn"))):
        raise RuntimeError("resolver external helpers are disabled")


def _watch_parent(connection, finished):
    # Once the request is received the parent sends nothing else. EOF means
    # parent crash/restart/cancel: do not leave the same-exe helper orphaned.
    while not finished.is_set():
        try:
            connection.recv(1)
        except socket.timeout:
            continue
        except OSError:
            if finished.is_set():
                return
        if not finished.is_set():
            os._exit(125)


def worker_main(argv):
    """Private early-entry child dispatch, argv=[port, 64-hex auth token]."""
    if (type(argv) not in (list, tuple) or len(argv) != 2
            or type(argv[0]) is not str or not argv[0].isascii()
            or not argv[0].isdigit() or not 1 <= len(argv[0]) <= 5
            or not 1 <= int(argv[0]) <= 65535 or type(argv[1]) is not str
            or _TOKEN.fullmatch(argv[1]) is None):
        return 2
    deadline = time.monotonic() + _CHILD_LIFETIME
    finished = threading.Event()
    watchdog = threading.Timer(_CHILD_LIFETIME, os._exit, args=(124,))
    watchdog.daemon = True
    connection = None
    try:
        watchdog.start()
        connection = socket.create_connection(("127.0.0.1", int(argv[0])), timeout=_POLL)
        connection.settimeout(_POLL)
        _send_json(connection, {"token": argv[1]}, deadline)
        request = _recv_json(connection, deadline)
        if type(request) is not dict or set(request) != {"url"} or not _valid_url(request["url"]):
            return 2
        watcher = threading.Thread(target=_watch_parent, args=(connection, finished), daemon=True,
                                   name="resolver-parent-watch")
        watcher.start()
        # This permanent hook is CHILD-only. Extraction cannot spawn ffmpeg,
        # ffprobe, JS runtimes, or another executable, even through an extractor.
        sys.addaudithook(_deny_helpers)
        result = _extract(request["url"])
        _send_json(connection, {"result": result}, deadline)
        return 0
    except Exception:
        return 2
    finally:
        finished.set()
        watchdog.cancel()
        if connection is not None:
            with contextlib.suppress(Exception):
                connection.close()
