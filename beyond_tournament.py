import pygame
import os
import cyal.listener
from libs import yt_dlp_deps

# Ensure the working directory is the script's own directory,
# so relative paths (data/, libs/, etc.) work regardless of how
# the game is launched (double-click, shortcut, terminal, etc.).
os.chdir(os.path.dirname(os.path.abspath(__file__)))


def main():
    from libs import logger
    logger.clear_log()
    logger.log("Starting Beyond Tournament Client...")
    
    from libs import options

    options.initialize()
    from libs import consts, menus
    from libs.version import version, note

    pygame.init()
    
    from libs import anti_cheat
    anti_cheat.start_speedhack_watchdog()
    
    pygame.display.set_caption(
        f"{consts.TITLE}, version {version.major}.{version.minor}.{version.patch} {note}"
    )
    screen = pygame.display.set_mode((900, 500))
    from libs import game

    g = game.Game(screen)
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
        except Exception:
            pass
        print("Game Crashed! Showing dialog...")
        print(error_msg)
        show_crash_dialog(error_msg)

