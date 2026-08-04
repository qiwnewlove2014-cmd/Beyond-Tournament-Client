"""Watchdog Heartbeat & Thread Stack Dumper for Beyond Tournament Client.

Monitors the main game loop's responsiveness. If the main loop freezes (e.g. GIL deadlock,
main thread blockage, or native C driver lockup) for longer than the timeout threshold,
this daemon thread captures and flushes the exact Python stack traces of ALL running threads
to disk (`client_debug.log` and `.client_crash_sessions/last_freeze.txt`) before the process
is terminated.
"""

import os
import sys
import threading
import time
import traceback
from . import logger

_SESSION_DIR = ".client_crash_sessions"
FREEZE_STACK_FILE = os.path.join(_SESSION_DIR, "last_freeze.txt")


class GameWatchdog(threading.Thread):
    """Failsafe Watchdog thread that monitors the main loop's heartbeat."""

    def __init__(self, game=None, timeout=5.0):
        super().__init__(daemon=True, name="GameWatchdogThread")
        self.game = game
        self.timeout = timeout
        self.last_tick = time.time()
        self.running = True
        self.has_dumped = False

    def heartbeat(self):
        """Update the heartbeat timestamp. Fast, lockless operation called on main loop ticks."""
        self.last_tick = time.time()
        self.has_dumped = False

    def stop(self):
        """Cleanly stop the watchdog thread."""
        self.running = False

    def run(self):
        """Periodically check main loop responsiveness."""
        while self.running:
            try:
                time.sleep(1.0)
                if not self.running:
                    break

                elapsed = time.time() - self.last_tick

                # If the main loop hasn't ticked for longer than self.timeout (5.0s)
                if elapsed > self.timeout and not self.has_dumped:
                    self.has_dumped = True
                    self._dump_thread_stacks(elapsed, fatal=False)
                
                # If the freeze persists for over 15 seconds, it's a hard deadlock.
                if elapsed > 15.0:
                    self._dump_thread_stacks(elapsed, fatal=True)

            except Exception:
                # Failsafe: Watchdog errors must NEVER crash or interrupt the client process
                pass

    def _dump_thread_stacks(self, elapsed, fatal=False):
        """Capture stack traces of all Python threads and persist them synchronously to disk."""
        try:
            timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
            header = (
                f"\n========================================================================\n"
                f"⚠️ [WATCHDOG ALARM] MAIN GAME LOOP FROZEN ({elapsed:.1f}s elapsed at {timestamp})\n"
                f"Capturing live thread stack traces for diagnostic report...\n"
                f"========================================================================\n"
            )

            dump_lines = [header]

            # Read-only snapshot of all active thread frames
            frames = sys._current_frames()
            for thread_id, frame in frames.items():
                thread_name = "Unknown"
                for t in threading.enumerate():
                    if t.ident == thread_id:
                        thread_name = t.name
                        break

                dump_lines.append(f"\n--- Thread: '{thread_name}' (ID: {thread_id}) ---")
                formatted_stack = "".join(traceback.format_stack(frame))
                dump_lines.append(formatted_stack if formatted_stack else "  (No stack frames available)")

            dump_lines.append(
                f"========================================================================\n"
            )

            full_dump = "\n".join(dump_lines)

            # Log to client_debug.log (flushes and fsyncs immediately)
            logger.log(full_dump)

            # Also persist to freeze file in session directory for crash reporting
            try:
                os.makedirs(_SESSION_DIR, exist_ok=True)
                with open(FREEZE_STACK_FILE, "w", encoding="utf-8") as f:
                    f.write(full_dump)
                    f.flush()
                    try:
                        os.fsync(f.fileno())
                    except OSError:
                        pass
            except Exception:
                pass

            # If the freeze is severe (> 15 seconds), it's a deadlock.
            # We must report it and force close to save the user from Task Manager.
            if fatal:
                try:
                    from . import crash_reporting
                    # Queue it as a fatal error so the server/staff get the alert
                    crash_reporting.queue_exception(
                        Exception("Native Deadlock Detected"),
                        context="Watchdog",
                        severity="fatal",
                        formatted_traceback=full_dump
                    )
                    
                    # Try to send it immediately if network is still alive
                    if getattr(self, 'game', None):
                        crash_reporting.send_pending(self.game)
                        time.sleep(0.5) # Give it half a second to send
                except Exception:
                    pass
                
                print("CRITICAL: Main game loop deadlocked. Alerting player and restarting...")
                
                # Speak warning to the player
                try:
                    from .speech import speak
                    speak("A critical freeze was detected. A crash report has been sent to the developers. Restarting the game. Please wait.")
                    time.sleep(5.0)
                except Exception:
                    pass
                
                # Auto-restart the game process silently
                try:
                    import subprocess
                    subprocess.Popen([sys.executable, sys.argv[0]] + sys.argv[1:])
                except Exception:
                    pass
                
                os._exit(1)

        except Exception:
            pass
