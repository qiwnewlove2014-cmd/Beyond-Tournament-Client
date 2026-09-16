"""Ask the shell for a folder or a file name without stopping the game.

The Tk dialogs behind "Set Recording Folder", "Set Download Folder" and the
recorder's save prompt are not free, and the price is not paid where it looks
like it is. Building Tk -- loading Tcl/Tk and creating the first window -- holds
the interpreter for about 80 ms, and that work is on the Python side: a worker
thread doing it starves the game loop and the audio pump for the same 80 ms,
heard as one hitch in the sound. Measured on the machine this was written on,
with a 20 ms frame loop: worst frame delay 0.7 ms idle, 81.7 ms while a worker
thread built Tk (``import tkinter`` alone, which releases the interpreter for
its file I/O, was only 40 ms of wall time and no delay at all).

So the dialog runs in a child process, the way the YouTube resolver already
does. It has its own interpreter, so the game keeps playing while the browser
is open, and only the chosen path comes back -- as UTF-8 bytes, because a
text-mode pipe would decode with the console's own ANSI codepage (cp874 on one
machine, cp1252 on another) and a folder named in Thai would arrive as
mojibake.

Nothing is left behind when the game goes away with a dialog still open: the
child holds a pipe from this process and exits the moment that pipe closes.
"""

import os
import subprocess
import sys
import threading

FILE_DIALOG_FLAG = "--bt-file-dialog"

FOLDER = "folder"
SAVE = "save"

# The title of a game dialog; a longer one is a bug in the caller, not a reason
# to hand the child an argument list of whatever size.
MAX_TITLE = 200


def ask_folder(title, initial="", *, timeout=None):
    """Let the player pick a folder; ``(folder, problem)``.

    An empty folder with an empty problem is a cancelled dialog -- the two are
    different answers and every caller says something different for each.
    ``timeout`` bounds only how long this call waits for the child; the dialog
    itself is the player's to close.
    """
    return _ask([FOLDER, _normalized(title, "Select a folder"), str(initial or "")],
                timeout)


def ask_save_file(title, initial_dir="", initial_file="", *, timeout=None):
    """Let the player name a WAV file; ``(path, problem)``.

    The recorder's export prompt: the only save dialog the game shows, so the
    WAV filter and the ``.wav`` default live here with it.
    """
    return _ask([SAVE, _normalized(title, "Save a file"), str(initial_dir or ""),
                 str(initial_file or "")], timeout)


def _normalized(title, fallback):
    text = str(title or fallback).strip() or fallback
    return text[:MAX_TITLE]


def _command(parts):
    args = [sys.executable]
    if not (getattr(sys, "frozen", False) or "__compiled__" in globals()):
        args.append(os.path.abspath(os.path.join(os.path.dirname(__file__),
                                                 os.pardir, "beyond_tournament.py")))
    return args + [FILE_DIALOG_FLAG] + list(parts)


def _working_directory():
    if getattr(sys, "frozen", False) or "__compiled__" in globals():
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))


def _child_environment():
    # Use the known interpreter and its installed dependencies, not an
    # inherited arbitrary import override or interactive startup script.
    excluded = {"PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "PYTHONINSPECT",
                "PYTHONBREAKPOINT"}
    environment = {key: value for key, value in os.environ.items()
                   if key.upper() not in excluded}
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    return environment


def _ask(parts, timeout):
    """Run one dialog in its own process and read back what it chose."""
    process = None
    try:
        process = subprocess.Popen(
            _command(parts), shell=False,
            stdin=subprocess.PIPE,        # Our lifetime is the child's: see below.
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            close_fds=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            cwd=_working_directory(), env=_child_environment(),
        )
    except Exception as error:
        return "", "could not start the file dialog: %s" % (error,)
    try:
        raw = _read_choice(process, timeout)
        if raw is None:
            _reap(process)
            return "", "the file dialog did not answer"
        code = process.wait(timeout=_REAP_TIMEOUT)
    except Exception as error:
        _reap(process)
        return "", "the file dialog failed: %s" % (error,)
    finally:
        _close_pipes(process)
    if code != 0:
        return "", "the file dialog exited with code %s" % code
    return raw.decode("utf-8", "replace").rstrip("\r\n"), ""


_REAP_TIMEOUT = 1.0


def _read_choice(process, timeout):
    """The child's whole stdout, or None if it outlived ``timeout``.

    Reading to the end is what waits for the player: the child writes its
    answer once and exits. A bound is still offered because a machine with a
    broken Tcl/Tk install must not leave a caller waiting for ever.
    """
    if timeout is None:
        return process.stdout.read() or b""
    done = []

    def read():
        try:
            done.append(process.stdout.read() or b"")
        except Exception:
            done.append(b"")

    reader = threading.Thread(target=read, name="file-dialog-reader", daemon=True)
    reader.start()
    reader.join(timeout)
    return done[0] if done else None


def _close_pipes(process):
    for stream in (process.stdin, process.stdout):
        try:
            if stream is not None:
                stream.close()
        except Exception:
            pass


def _reap(process):
    # Never enumerate processes, use a PID from IPC, or terminate another game.
    if process is None:
        return
    try:
        if process.poll() is None:
            process.kill()
    except Exception:
        pass
    try:
        process.wait(timeout=_REAP_TIMEOUT)
    except Exception:
        pass


def worker_main(argv):
    """The child: show one dialog, write the choice to stdout, exit.

    Started by ``beyond_tournament.py`` before it imports any game module, so
    this half must stay importable on a bare interpreter (standard library
    only) and must never touch the game.
    """
    mode = argv[0] if argv else FOLDER
    title = argv[1] if len(argv) > 1 else "Select a folder"
    initial_dir = argv[2] if len(argv) > 2 else ""
    initial_file = argv[3] if len(argv) > 3 else ""
    _exit_with_parent()
    try:
        choice = _show(mode, title, initial_dir, initial_file)
    except Exception as error:
        sys.stderr.write("file dialog failed: %s\n" % (error,))
        return 1
    sys.stdout.buffer.write(str(choice).encode("utf-8", "replace"))
    sys.stdout.buffer.flush()
    return 0


def _show(mode, title, initial_dir, initial_file):
    """One Tk dialog in this (child) process. Its cost is this process's."""
    import tkinter as tk
    from tkinter import filedialog

    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    try:
        if mode == SAVE:
            options = {
                "title": title,
                "defaultextension": ".wav",
                "filetypes": [("WAV audio", "*.wav")],
                "confirmoverwrite": True,
            }
            if initial_dir:
                options["initialdir"] = initial_dir
            if initial_file:
                options["initialfile"] = initial_file
            return filedialog.asksaveasfilename(**options) or ""
        options = {"title": title, "mustexist": True}
        if initial_dir:
            options["initialdir"] = initial_dir
        return filedialog.askdirectory(**options) or ""
    finally:
        root.destroy()


def _exit_with_parent():
    """Take the dialog down when the game it belongs to goes away.

    The caller keeps the write end of our stdin open for as long as it lives,
    so a read that ends means the game closed -- with the browser still open on
    screen. A dialog nothing is waiting for must not outlive the game that
    asked for it.

    The wait reads the descriptor raw rather than through ``sys.stdin``: a
    buffered read holds the buffer's own lock for as long as it waits, and a
    daemon thread still holding it when the interpreter shuts down aborts the
    child -- ``Fatal Python error: _enter_buffered_busy ... possibly due to
    daemon threads`` -- so a dialog that answered correctly still came back to
    the game as a failure. ``os.read`` takes no such lock.
    """
    try:
        descriptor = sys.stdin.fileno() if sys.stdin is not None else None
    except Exception:
        descriptor = None
    if descriptor is None:
        return

    def watch():
        try:
            while os.read(descriptor, 1):
                # Nothing but the caller's own lifetime was expected here; a
                # byte means it is still alive, so keep waiting.
                pass
        except Exception:
            return
        os._exit(0)

    threading.Thread(target=watch, name="file-dialog-parent-watch", daemon=True).start()
