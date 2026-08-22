import pygame
import os
import ctypes
import sys
import cyal.listener
from libs import yt_dlp_deps

active_game = None


def _complete_restart_parent_handoff():
    """Stop/wait for the old Client before initializing Pygame or OpenAL."""
    if len(sys.argv) <= 3 or sys.argv[1] != "restart_client":
        return
    try:
        old_pid = int(sys.argv[3])
    except (TypeError, ValueError):
        return
    if old_pid <= 0 or old_pid == os.getpid():
        return
    try:
        import signal
        os.kill(old_pid, signal.SIGTERM)
    except (OSError, ProcessLookupError):
        return
    try:
        import psutil
        import time
        deadline = time.monotonic() + 10.0
        while psutil.pid_exists(old_pid) and time.monotonic() < deadline:
            time.sleep(0.02)
    except Exception:
        # The termination request above is sufficient on Windows. Waiting is
        # best-effort and must never prevent the replacement from launching.
        pass


def _set_windows_timer_resolution(period_ms=1):
    """Raise the Windows multimedia timer resolution for accurate sleep()s.

    Python's time.sleep() on Windows is only accurate to the system timer
    quantum (~15.6ms by default), which wrecks the ~20ms audio send pacing
    used by the music bot and voice chat (sends land on 15.6/31.2ms
    boundaries instead of every 20ms, causing intermittent PA stutter).
    timeBeginPeriod(1) drops the quantum to 1ms for this process.
    """
    if sys.platform != "win32":
        return
    try:
        winmm = ctypes.windll.winmm
        winmm.timeBeginPeriod(period_ms)
        # Best-effort restore on process exit.
        import atexit
        atexit.register(winmm.timeEndPeriod, period_ms)
    except Exception:
        pass

# Ensure the working directory is the script's own directory,
# so relative paths (data/, libs/, etc.) work regardless of how
# the game is launched (double-click, shortcut, terminal, etc.).
os.chdir(os.path.dirname(os.path.abspath(__file__)))


def main():
    from libs import vfs
    vfs.init_vfs()

    from libs import logger
    from libs import crash_reporting
    crash_reporting.begin_session()
    logger.clear_log()
    logger.log("Starting Beyond Tournament Client...")

    # Compiled builds are single-instance: refuse a second copy before any
    # heavy initialization (pygame, audio, network). Running from source keeps
    # the normal multi-instance behavior for testing several accounts at once.
    # Updater relaunches carry extra CLI args: the previous process (argv[3])
    # is being replaced and is killed without running its atexit handlers, so
    # hand its single-instance lock over before the new copy starts up.
    if len(sys.argv) > 3:
        try:
            from libs import instance_manager as _im
            _im.release_lock_for_pid(int(sys.argv[3]))
        except Exception:
            pass
        _complete_restart_parent_handoff()
    else:
        from libs import instance_manager
        if instance_manager.InstanceManager.compiled_instance_blocked():
            _show_single_instance_message()
            return

    from libs import options

    options.initialize()
    from libs import consts, menus
    from libs.version import version, note

    pygame.init()

    # Raise the Windows timer resolution to 1ms for the whole process.  The
    # default ~15.6ms quantum makes time.sleep() in the 20ms audio send loops
    # (music bot / voice chat) land on 15.6/31.2ms boundaries, which makes PA
    # streaming "sometimes smooth, sometimes stuttery". timeBeginPeriod(1)
    # makes those sleeps accurate so the real-time audio cadence stays steady.
    _set_windows_timer_resolution()
    
    from libs import anti_cheat
    anti_cheat.start_speedhack_watchdog()
    
    pygame.display.set_caption(
        f"{consts.TITLE}, version {version.major}.{version.minor}.{version.patch} {note}"
    )
    screen = pygame.display.set_mode((900, 500))
    from libs import game

    global active_game
    g = game.Game(screen)
    active_game = g
    previous_thread_hook = threading.excepthook

    def log_thread_crash(args):
        error_text = "".join(traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback))
        logger.log(f"[FATAL THREAD] {args.thread.name}:\n{error_text}")
        # Pygame/OpenAL state belongs to the main thread, so only schedule recovery.
        g.put(lambda: g.recover_from_exception(args.exc_value, f"Thread {args.thread.name}"))
        previous_thread_hook(args)

    threading.excepthook = log_thread_crash
    g.start()
    g.loop()


import sys
import traceback
import threading

def _show_single_instance_message():
    """Tell the player the compiled build only allows one open instance."""
    msg = (
        "Beyond Tournament is already running.\n\n"
        "Only one instance of this build can be open at a time.\n"
        "Close the other window first, then try again."
    )
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(
            None, msg, "Beyond Tournament", 0x10  # MB_ICONERROR
        )
    except Exception:
        print(msg)


def show_crash_dialog(error_text):
    """
    Display a crash report dialog with the error traceback.
    Points the user to report this error.
    """
    try:
        import tkinter as tk
        from tkinter import scrolledtext
        
        root = tk.Tk()
        root.withdraw()  # Hide the root window

        # Create custom dialog
        dialog = tk.Toplevel(root)
        dialog.title("Beyond Tournament Client - Critical Error")
        dialog.geometry("800x600")
        
        # Label
        lbl = tk.Label(dialog, text="The game has crashed. Please report the text below to the developer:", font=("Arial", 10, "bold"), pady=10)
        lbl.pack(side=tk.TOP, fill=tk.X)

        # Scrolled Text Area for Traceback
        txt = scrolledtext.ScrolledText(dialog, font=("Consolas", 10))
        txt.insert(tk.END, error_text)
        txt.config(state=tk.DISABLED)  # Read-only
        txt.pack(expand=True, fill=tk.BOTH, padx=10, pady=5)

        # OK Button to Exit
        def on_ok():
            root.destroy()
            sys.exit(1)

        btn = tk.Button(dialog, text="OK (Close Game)", command=on_ok, height=2, font=("Arial", 10, "bold"))
        btn.pack(side=tk.BOTTOM, fill=tk.X, padx=10, pady=10)

        # Handle X button
        dialog.protocol("WM_DELETE_WINDOW", on_ok)
        
        # Center dialog
        dialog.update_idletasks()
        width = dialog.winfo_width()
        height = dialog.winfo_height()
        x = (dialog.winfo_screenwidth() // 2) - (width // 2)
        y = (dialog.winfo_screenheight() // 2) - (height // 2)
        dialog.geometry(f"{width}x{height}+{x}+{y}")

        root.mainloop()
    except Exception as e:
        # If GUI fails, fallback to console
        print("CRITICAL: Failed to show crash dialog:", e)
        print(error_text)

if __name__ == "__main__":
    try:
        main()
    except Exception:
        # Catch ALL unhandled exceptions
        error_msg = traceback.format_exc()
        # Keep the traceback beside the compiled client executable so a player can
        # send it to us even after closing the crash dialog.
        try:
            from libs import logger
            logger.log(f"[FATAL] Unhandled client exception:\n{error_msg}")
            from libs import crash_reporting
            crash_reporting.queue_exception(
                sys.exc_info()[1], "Client process entry point", "fatal", error_msg
            )
            if active_game:
                crash_reporting.send_pending(active_game)
        except Exception:
            pass
        print("Game Crashed! Showing dialog...")
        print(error_msg)
        show_crash_dialog(error_msg)
    finally:
        try:
            from libs import crash_reporting
            crash_reporting.finish_session()
        except Exception:
            pass

