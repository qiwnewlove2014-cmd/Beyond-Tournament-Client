import traceback
import time
import os
import sys

LOG_FILE = "client_debug.log"

def clear_log():
    """Clears the log file on startup"""
    try:
        with open(LOG_FILE, "w", encoding="utf-8") as f:
            f.write(f"=== Client Log Started at {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n")
    except Exception:
        pass

def log(message):
    """Logs a message to file and console"""
    timestamp = time.strftime("%H:%M:%S")
    formatted = f"[{timestamp}] {message}"
    
    # Print to console (for immediate feedback)
    print(formatted)
    
    # Write to file
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
