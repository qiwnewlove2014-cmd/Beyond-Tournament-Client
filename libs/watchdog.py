"""Watchdog Heartbeat & Thread Stack Dumper for Beyond Tournament Client.

Monitors the main game loop's responsiveness. If the main loop freezes (e.g. GIL deadlock,
main thread blockage, or native C driver lockup) for longer than the timeout threshold,
this daemon thread captures and flushes the exact Python stack traces of ALL running threads
to disk (`client_debug.log` and `.client_crash_sessions/last_freeze.txt`).

It is a *witness*, never a judge: it never kills the process and never relaunches it. A
compiled Client that relaunched itself on a guessed deadlock cost the player the whole
session -- the new copy is refused by the single-instance guard (or starts at the login
menu) while the old one exits -- and the guess was wrong every time the "freeze" was a
slow first load of instrument samples from ``sounds.dat``. A real deadlock is better left
to the player's Task Manager than guessed at with the session as the stake; what this
thread must never lose is the stacks, and it keeps writing them.
"""

import os
import sys
import threading
import time
import traceback
from . import logger

_SESSION_DIR = ".client_crash_sessions"
FREEZE_STACK_FILE = os.path.join(_SESSION_DIR, "last_freeze.txt")

# A freeze that is still going is ONE report, not one per second: each dump
# writes the whole file and fsyncs it, and a dump loop on the diagnostic thread
# during a stall is load the stall does not need. The stacks are written again
# when the freeze has lasted another full window, so a long hang still leaves a
# record of how it progressed.
FATAL_REPORT_INTERVAL_S = 60.0


class GameWatchdog(threading.Thread):
    """Failsafe Watchdog thread that monitors the main loop's heartbeat."""

    def __init__(self, game=None, timeout=5.0):
        super().__init__(daemon=True, name="GameWatchdogThread")
        self.game = game
        self.timeout = timeout
        self.last_tick = time.time()
        self.running = True
        self.has_dumped = False
        # When the long-freeze stacks were last written (monotonic), so a hang
        # that lasts a minute is reported twice, not sixty times.
        self._fatal_reported_at = None

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
                
                # A freeze past 15 s may be a hard deadlock -- or a slow first
                # load. Either way the stacks are the deliverable; the process
                # is not touched (see this module's header).
                if elapsed > 15.0 and self._fatal_due():
                    self._dump_thread_stacks(elapsed, fatal=True)

            except Exception:
                # Failsafe: Watchdog errors must NEVER crash or interrupt the client process
                pass

    def _fatal_due(self):
        now = time.monotonic()
        if (self._fatal_reported_at is not None
                and now - self._fatal_reported_at < FATAL_REPORT_INTERVAL_S):
            return False
        self._fatal_reported_at = now
        return True

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

            # A long freeze is reported, never acted on. `recovered` is the
            # honest severity: the Client is still here and may recover by
            # itself, and a fabricated `fatal` would tell staff a crash
            # happened. No speech either -- the loop that would play it is the
            # thing that is stuck.
            if fatal:
                try:
                    from . import crash_reporting
                    crash_reporting.queue_exception(
                        Exception("Main loop frozen (watchdog)"),
                        context="Watchdog",
                        severity="recovered",
                        formatted_traceback=full_dump
                    )
                    # Best effort: the report is persisted first either way, so
                    # a dead network only defers it to the next login.
                    if getattr(self, 'game', None):
                        crash_reporting.send_pending(self.game)
                except Exception:
                    pass

        except Exception:
            pass
