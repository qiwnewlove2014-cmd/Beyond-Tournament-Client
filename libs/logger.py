import traceback
import time
import os
import sys

LOG_FILE = "client_debug.log"

def is_compiled():
    """Returns True if running as a compiled executable (PyInstaller/Nuitka)"""
    if hasattr(sys, 'frozen'):
        return True
    if '__compiled__' in globals():
        return True
    # If the main script doesn't end with .py, it's likely a compiled executable
    if sys.argv and not sys.argv[0].endswith('.py'):
        return True
    return False

def clear_log():
    """Clears the log file on startup, only if compiled"""
    if not is_compiled():
        return
    try:
        with open(LOG_FILE, "w", encoding="utf-8") as f:
            f.write(f"=== Client Log Started at {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n")
    except Exception:
        pass

def log(message):
    """Logs a message to file (if compiled) and console"""
    timestamp = time.strftime("%H:%M:%S")
    formatted = f"[{timestamp}] {message}"
    
    # Print to console (for immediate feedback)
    print(formatted)
    
    # Write to file only if compiled
    if not is_compiled():
        return
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(formatted + "\n")
    except Exception as e:
        print(f"Failed to write to log: {e}")

def log_exception(e, context=""):
    """Logs an exception with traceback"""
    tb = traceback.format_exc()
    msg = f"CRITICAL ERROR in {context}: {e}\n{tb}"
    log(msg)
