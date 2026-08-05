import atexit
import os
import sys
import threading
import time
import traceback

LOG_FILE = "client_debug.log"

# Persistent log file handle kept open for the whole process.  Opening the file
# once and flushing every line means a native crash, task-kill, or power loss
# cannot lose the most recent entries — they are already on disk.
_LOG_HANDLE = None
_LOG_LOCK = threading.Lock()


def is_compiled():
    """Returns True if running as a compiled executable (PyInstaller/Nuitka)"""
    if hasattr(sys, 'frozen'):
        return True
    if '__compiled__' in globals():
        return True
    # If the main script doesn't end with .py, it's likely a compiled executable
    if sys.argv and not sys.argv[0].endswith(".py"):
        return True
    return False


def _close_handle():
    """Close the persistent log handle. Safe to call multiple times."""
    global _LOG_HANDLE
    with _LOG_LOCK:
        handle = _LOG_HANDLE
        _LOG_HANDLE = None
    if handle is not None:
        try:
            handle.flush()
            handle.close()
        except Exception:
            pass


# Ensure the handle is released on interpreter exit even during a clean shutdown.
atexit.register(_close_handle)


def clear_log():
    """Open (or reopen) the log file, truncating it, in every run mode.

    The handle is kept open for the lifetime of the process so that subsequent
    log() calls can flush synchronously without re-opening the file each time.
    """
    global _LOG_HANDLE
    try:
        with _LOG_LOCK:
            if _LOG_HANDLE is not None:
                try:
                    _LOG_HANDLE.close()
                except Exception:
                    pass
                _LOG_HANDLE = None
            _LOG_HANDLE = open(LOG_FILE, "w", encoding="utf-8")
            _LOG_HANDLE.write(
                f"=== Client Log Started at {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n"
            )
            _LOG_HANDLE.flush()
            try:
                os.fsync(_LOG_HANDLE.fileno())
            except OSError:
                # fsync may fail on some stdout-redirected descriptors; the
                # flush above is still enough for most cases.
                pass
    except Exception as e:
        print(f"Failed to open log file: {e}")


def log(message):
    """Logs a message to the persistent log file and console.

    Every write is flushed and fsync'd immediately so that a sudden process
    termination (native crash, task-kill, power loss) still leaves the last log
    line on disk — crash_reporting._debug_log_tail() can then recover it.
    """
    timestamp = time.strftime("%H:%M:%S")
    formatted = f"[{timestamp}] {message}"

    # Print to console (for immediate feedback)
    print(formatted)

    # Append to the persistent handle, flushing synchronously.
    handle = _LOG_HANDLE
    if handle is None:
        return
    try:
        with _LOG_LOCK:
            handle.write(formatted + "\n")
            handle.flush()
            try:
                os.fsync(handle.fileno())
            except OSError:
                pass
    except Exception as e:
        print(f"Failed to write to log: {e}")


def log_exception(e, context=""):
    """Logs an exception with traceback"""
    tb = traceback.format_exc()
    msg = f"CRITICAL ERROR in {context}: {e}\n{tb}"
    log(msg)
