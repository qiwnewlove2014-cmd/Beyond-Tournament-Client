import os
import tempfile
import atexit
import psutil
import pygame
from .version import version, note
from . import consts

class InstanceManager:
    def __init__(self):
        self.instance_id = 1
        self.lock_file_path = None
        self.character_name = None
        self.last_instances_count = 0
        self.acquire_lock()

    def acquire_lock(self):
        temp_dir = tempfile.gettempdir()
        for i in range(1, 11):
            lock_path = os.path.join(temp_dir, f"beyond_tournament_instance_{i}.lock")
            is_stale = False
            if os.path.exists(lock_path):
                try:
                    with open(lock_path, "r") as f:
                        pid = int(f.read().strip())
                    if not psutil.pid_exists(pid):
                        is_stale = True
                    else:
                        proc = psutil.Process(pid)
                        proc_cmd = " ".join(proc.cmdline()).lower()
                        if "beyond_tournament" not in proc_cmd.lower() and "python" not in proc_cmd.lower():
                            is_stale = True
                except Exception:
                    is_stale = True

                if not is_stale:
                    continue # Lock is active, try next ID
            
            # Found a free or stale slot!
            self.instance_id = i
            self.lock_file_path = lock_path
            try:
                with open(self.lock_file_path, "w") as f:
                    f.write(str(os.getpid()))
                atexit.register(self.release_lock)
                break
            except Exception:
                pass

    def release_lock(self):
        if self.lock_file_path and os.path.exists(self.lock_file_path):
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
            if os.path.exists(lock_path):
                try:
                    with open(lock_path, "r") as f:
                        pid = int(f.read().strip())
                    if psutil.pid_exists(pid):
                        count += 1
                except Exception:
                    pass
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
