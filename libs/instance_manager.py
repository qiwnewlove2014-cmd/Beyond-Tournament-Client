import os
import tempfile
import atexit
import psutil
import pygame
from .version import version, note
from . import consts
from .logger import is_compiled

# Compiled builds allow exactly one running instance.  The lock lives in its
# own file so the compiled game and source-code runs never fight over slots:
# source keeps the multi-instance dev slots below (up to 10 for testing many
# accounts at once), while a compiled exe refuses to start a second copy.
COMPILED_LOCK_NAME = "beyond_tournament_compiled.lock"


def release_lock_for_pid(pid):
    """Remove the compiled lock if it is held by ``pid``.

    Used by an updater relaunch: the new process replaces the previous one
    (which is killed via TerminateProcess, so its atexit never runs), and the
    hand-off must happen before the new instance acquires the lock.
    """
    lock_path = os.path.join(tempfile.gettempdir(), COMPILED_LOCK_NAME)
    if not os.path.exists(lock_path):
        return
    try:
        with open(lock_path, "r") as f:
            holder = int(f.read().strip())
        if holder == pid:
            os.remove(lock_path)
    except Exception:
        pass


def _cmdline_is_game(cmd):
    """True when a process command line belongs to a Beyond Tournament client.

    Covers the compiled executable ("Beyond Tournament.exe" — renamed by
    build.bat with a space) and source runs (python beyond_tournament.py).
    """
    cmd = (cmd or "").lower()
    return (
        "beyond_tournament" in cmd
        or "beyond tournament" in cmd
        or "python" in cmd
    )


def _lock_is_active(lock_path):
    """True if the lock file belongs to a live Beyond Tournament process.

    A leftover file from a crashed/killed process (dead PID) or a file that
    some unrelated process happens to own is treated as stale, so it never
    blocks a fresh launch.
    """
    if not os.path.exists(lock_path):
        return False
    try:
        with open(lock_path, "r") as f:
            pid = int(f.read().strip())
        if not psutil.pid_exists(pid):
            return False
        proc = psutil.Process(pid)
        return _cmdline_is_game(" ".join(proc.cmdline()))
    except Exception:
        pass
    return False


class InstanceManager:
    def __init__(self):
        self.instance_id = 1
        self.lock_file_path = None
        self.character_name = None
        self.last_instances_count = 0
        self.acquired = self.acquire_lock()

    def acquire_lock(self):
        """Take the instance lock. Returns True when this process may run.

        Compiled builds are single-instance: only one process may hold the
        compiled lock.  Running from source keeps the old multi-instance
        behavior (up to 10 simultaneous clients) for testing many accounts.
        """
        if is_compiled():
            return self._acquire_compiled_lock()
        return self._acquire_dev_lock()

    def _acquire_dev_lock(self):
        """Source runs: grab the first free dev slot (1..10)."""
        temp_dir = tempfile.gettempdir()
        for i in range(1, 11):
            lock_path = os.path.join(temp_dir, f"beyond_tournament_instance_{i}.lock")
            if _lock_is_active(lock_path):
                continue  # Lock is active, try next ID

            # Found a free or stale slot!
            self.instance_id = i
            self.lock_file_path = lock_path
            try:
                with open(self.lock_file_path, "w") as f:
                    f.write(str(os.getpid()))
                atexit.register(self.release_lock)
                return True
            except Exception:
                pass
        return False

    def _acquire_compiled_lock(self):
        """Compiled builds: exactly one instance, one dedicated lock file."""
        lock_path = os.path.join(tempfile.gettempdir(), COMPILED_LOCK_NAME)
        if _lock_is_active(lock_path):
            # Another compiled copy is already running — refuse.
            self.lock_file_path = None
            return False
        self.instance_id = 1
        self.lock_file_path = lock_path
        try:
            with open(self.lock_file_path, "w") as f:
                f.write(str(os.getpid()))
            atexit.register(self.release_lock)
            return True
        except Exception:
            self.lock_file_path = None
            return False

    @staticmethod
    def compiled_instance_blocked():
        """True when a compiled build is already running (single-instance rule).

        Used by the entry point to refuse a second compiled copy BEFORE any
        heavy initialization (pygame, audio, network) starts.
        """
        return _lock_is_active(
            os.path.join(tempfile.gettempdir(), COMPILED_LOCK_NAME)
        )

    def release_lock(self):
        if self.lock_file_path and os.path.exists(self.lock_file_path):
            try:
                with open(self.lock_file_path, "r") as f:
                    holder = int(f.read().strip())
                if holder != os.getpid():
                    # Another process took over this lock file (updater
                    # relaunch). Never delete a lock we no longer own.
                    self.lock_file_path = None
                    return
            except Exception:
                pass
            try:
                os.remove(self.lock_file_path)
            except Exception:
                pass
            self.lock_file_path = None

    def get_active_instances_count(self):
        temp_dir = tempfile.gettempdir()
        count = 0
        for i in range(1, 11):
            lock_path = os.path.join(temp_dir, f"beyond_tournament_instance_{i}.lock")
            if _lock_is_active(lock_path):
                count += 1
        if _lock_is_active(os.path.join(temp_dir, COMPILED_LOCK_NAME)):
            count += 1
        return max(1, count)

    def set_character(self, name):
        self.character_name = name
        self.update_title()

    def update_title(self):
        if not pygame.display.get_init():
            return

        instances_count = self.get_active_instances_count()
        self.last_instances_count = instances_count

        version_str = f"version {version.major}.{version.minor}.{version.patch} {note}"

        if instances_count > 1:
            # Multiple instances are active
            if self.character_name:
                title = f"{self.character_name} | {consts.TITLE}, {version_str}"
            else:
                title = f"[{self.instance_id}] {consts.TITLE}, {version_str}"
        else:
            # Only one instance active
            title = f"{consts.TITLE}, {version_str}"

        # Update pygame display caption if it has changed
        if pygame.display.get_caption()[0] != title:
            pygame.display.set_caption(title)
