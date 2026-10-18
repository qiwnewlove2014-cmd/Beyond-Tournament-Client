import threading
import time
import os
import pygame
import struct
from . import crash_reporting


def _exit_for_cheat(reason):
    """Avoid reporting deliberate anti-cheat shutdowns as client crashes."""
    crash_reporting.mark_expected_shutdown(reason)
    os._exit(1)

class SecureInt:
    """
    A protected integer that prevents memory modification via Cheat Engine.
    It stores the real value alongside a XOR'd shadow value.
    If the real value is modified in memory, it will not match the shadow value,
    triggering an immediate game exit.
    """
    def __init__(self, initial_value=0):
        # Generate a unique key for this instance based on memory address and real time
        self._key = int(time.perf_counter() * 1000000) ^ id(self)
        self._value = int(initial_value)
        self._shadow = self._value ^ self._key

    def get(self):
        # Verification
        if self._value != (self._shadow ^ self._key):
            # Tampering detected!
            print("CRITICAL: Memory tampering detected on SecureInt! Exiting...")
            _exit_for_cheat("memory_tampering")
        return self._value

    def set(self, new_value):
        # Verification before setting
        if self._value != (self._shadow ^ self._key):
            print("CRITICAL: Memory tampering detected on SecureInt! Exiting...")
            _exit_for_cheat("memory_tampering")
        
        self._value = int(new_value)
        self._shadow = self._value ^ self._key

class SecureFloat:
    """
    Similar to SecureInt but for floats.
    Since bitwise operations don't work directly on floats, we use struct to pack/unpack to 64-bit int.
    """
    def __init__(self, initial_value=0.0):
        self._key = int(time.perf_counter() * 1000000) ^ id(self)
        self._value = float(initial_value)
        self._shadow = self._float_to_int(self._value) ^ self._key

    def _float_to_int(self, f):
        return struct.unpack('<Q', struct.pack('<d', f))[0]

    def get(self):
        current_int = self._float_to_int(self._value)
        if current_int != (self._shadow ^ self._key):
            print("CRITICAL: Memory tampering detected on SecureFloat! Exiting...")
            _exit_for_cheat("memory_tampering")
        return self._value

    def set(self, new_value):
        current_int = self._float_to_int(self._value)
        if current_int != (self._shadow ^ self._key):
            print("CRITICAL: Memory tampering detected on SecureFloat! Exiting...")
            _exit_for_cheat("memory_tampering")
            
        self._value = float(new_value)
        self._shadow = self._float_to_int(self._value) ^ self._key

import psutil
import ctypes
import ctypes.wintypes

_game = None

def set_game_reference(game_ref):
    global _game, _degraded_reported, _last_watch_heartbeat
    _game = game_ref
    # A new game session is a new report: an already-open recorder is said again
    # when the player comes back, instead of being remembered from last time.
    if _recording_observer is not None:
        _recording_observer.reset()
    _logged_recording_findings.clear()
    _degraded_reported = False
    # Say "the watch is up" once right away, so a Server that kept nothing from
    # the last session does not have to wait a whole interval for this one.
    _last_watch_heartbeat = 0.0

def visible_window_titles():
    """Titles of every visible top-level window, empty off Windows.

    Shared by the cheat scan and the recording watch: one Win32 callback is
    easier to keep correct than two copies of it (the signatures below are the
    part that was wrong once already).
    """
    if os.name != "nt":
        return []

    EnumWindows = ctypes.windll.user32.EnumWindows
    # Correct Win32 signature: BOOL CALLBACK EnumWindowsProc(HWND, LPARAM)
    # c_bool (1 byte) is WRONG for BOOL (4 bytes) — causes stack corruption.
    # POINTER(c_int) is WRONG for HWND/LPARAM — causes invalid dereferences.
    EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM)
    GetWindowText = ctypes.windll.user32.GetWindowTextW
    GetWindowText.argtypes = [ctypes.wintypes.HWND, ctypes.c_wchar_p, ctypes.c_int]
    GetWindowText.restype = ctypes.c_int
    GetWindowTextLength = ctypes.windll.user32.GetWindowTextLengthW
    GetWindowTextLength.argtypes = [ctypes.wintypes.HWND]
    GetWindowTextLength.restype = ctypes.c_int
    IsWindowVisible = ctypes.windll.user32.IsWindowVisible
    IsWindowVisible.argtypes = [ctypes.wintypes.HWND]
    IsWindowVisible.restype = ctypes.wintypes.BOOL

    titles = []

    def foreach_window(hwnd, lParam):
        if IsWindowVisible(hwnd):
            length = GetWindowTextLength(hwnd)
            if length > 0:
                buff = ctypes.create_unicode_buffer(length + 1)
                GetWindowText(hwnd, buff, length + 1)
                titles.append(buff.value)
        return True

    # Store callback in a local variable to prevent GC from collecting the
    # ctypes thunk while EnumWindows is still invoking it.
    callback = EnumWindowsProc(foreach_window)
    EnumWindows(callback, 0)
    return titles


def detect_cheat_engine():
    """Returns True if Cheat Engine process or window is found."""
    # 1. Process Name Detection
    forbidden_processes = [
        "cheatengine-x86_64.exe", 
        "cheatengine-i386.exe", 
        "cheatengine-x86_64-seh.exe"
    ]
    
    for proc in psutil.process_iter(['name']):
        try:
            if proc.info['name'] and proc.info['name'].lower() in forbidden_processes:
                return True
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            pass

    # 2. Window Title Detection (Catches renamed executables)
    return any("Cheat Engine" in title for title in visible_window_titles())


# Recording software is reported to staff, never punished: a player recording
# their own match is not cheating (see libs/recording_watch.py).  One observer
# per run; the observer itself announces a tool once per appearance, and a new
# game session starts that over.
_recording_observer = None
_logged_recording_findings = set()
_degraded_reported = False

#: How often the watch says "still running" to the Server.  The Server reads a
#: watch that stops while its player is online as *unknown*, never as "nobody is
#: recording" -- otherwise blocking the packet would look like innocence.
RECORDING_WATCH_HEARTBEAT_S = 60.0

#: Seconds between recording scans.  Five is enough in the menu; a match scan
#: runs twice as often, because that is when a short recording is worth seeing.
RECORDING_SCAN_SECONDS_IDLE = 5
RECORDING_SCAN_SECONDS_MATCH = 2

_last_watch_heartbeat = 0.0


def _in_match() -> bool:
    """True while a match is running (exploration does not count)."""
    gameplay = getattr(_game, "gameplay", None) if _game is not None else None
    return bool(getattr(gameplay, "game_started", False))


def _recording_scan_seconds() -> int:
    return RECORDING_SCAN_SECONDS_MATCH if _in_match() else RECORDING_SCAN_SECONDS_IDLE



def _recording_observer_for_session():
    global _recording_observer
    if _recording_observer is None:
        from . import recording_watch
        _recording_observer = recording_watch.RecordingObserver(window_scan=visible_window_titles)
    return _recording_observer


def report_recording_watch_coverage():
    """Say once per session when this client cannot see live microphone capture.

    A machine where the audio-engine net fails still reports known recorder
    names, and "no findings" then means much less.  Developers are told which
    players are in that state instead of trusting a silence that is not theirs.
    """
    global _degraded_reported
    if _degraded_reported or not (_game and getattr(_game, "network", None)):
        return
    try:
        from libs import consts
        from . import recording_watch
        if not recording_watch.capture_signal_supported_here():
            return
        if recording_watch.capture_signal_available() is not False:
            return
        _game.network.send(
            consts.CHANNEL_MISC,
            "recording_watch_degraded",
            {
                "reason": "capture_sessions_unavailable",
                "os_version": recording_watch.windows_version_label(),
            },
        )
        _degraded_reported = True
        try:
            from libs import logger
            logger.log("Recording watch: live microphone capture is unavailable; name detection only.")
        except:
            pass
    except Exception as e:
        print(f"Recording watch: could not report coverage: {e}")


def report_recording_watch_heartbeat():
    """Tell the Server this watch is still running (once a minute).

    Silence from a watch is ambiguous: a player who is simply not recording and
    a player whose reports never arrive look identical from the Server.  The
    heartbeat is what lets a Developer tell those apart, so this is sent on the
    same cycle as the scan and throttled by time rather than by cycle count.
    """
    global _last_watch_heartbeat
    if not (_game and getattr(_game, "network", None)):
        return
    now = time.monotonic()
    if _last_watch_heartbeat and now - _last_watch_heartbeat < RECORDING_WATCH_HEARTBEAT_S:
        return
    try:
        from libs import consts
        _game.network.send(consts.CHANNEL_MISC, "recording_watch_alive", {})
        _last_watch_heartbeat = now
    except Exception as e:
        print(f"Recording watch: could not send the heartbeat: {e}")


def report_recording_tools():
    """Tell staff which recording software is running.  Never exits the game.

    A finding is only marked as handled once the packet actually left this
    process; until then it is offered again on the next cycle, so a recorder
    opened in the main menu (no network yet) is still reported after login.
    """
    try:
        from . import recording_watch
        # A recorder started before the network was ready is sent now.
        recording_watch.flush_pending_in_game_recording(_game)
        observer = _recording_observer_for_session()
        findings = observer.poll()
        report_recording_watch_coverage()
    except Exception as e:
        print(f"Recording watch: scan failed: {e}")
        return

    for finding in findings:
        if not (_game and getattr(_game, "network", None)):
            continue
        try:
            from libs import consts
            _game.network.send(
                consts.CHANNEL_MISC,
                "recording_tool_detected",
                {
                    "app": finding.app,
                    "source": finding.source,
                    "detail": finding.detail,
                },
            )
        except Exception as e:
            print(f"Recording watch: could not report {finding.app}: {e}")
            continue
        observer.mark_reported(finding)
        if finding.key not in _logged_recording_findings:
            _logged_recording_findings.add(finding.key)
            try:
                from libs import logger
                detail = f": {finding.detail}" if finding.detail else ""
                logger.log(f"Recording watch: {finding.app} ({finding.source}{detail})")
            except:
                pass


def _speedhack_watchdog():
    """
    Background thread to detect Speedhack and Cheat Engine presence globally.
    """
    print("Anti-Cheat: Speedhack & Process watchdog initialized.")
    # Give the game some time to stabilize
    time.sleep(2.0)
    
    last_real_time = time.time()
    last_game_time = pygame.time.get_ticks() / 1000.0

    # Developer diagnostic gate: set BT_DISABLE_ANTICHEAT_SCAN=1 to skip the
    # psutil/Win32 process scan (the remaining CRT-heap corruption suspect)
    # while KEEPING the speedhack check below. Local debugging only — remove
    # once the corrupter is confirmed and the scan is moved off-process.
    skip_process_scan = os.environ.get("BT_DISABLE_ANTICHEAT_SCAN") == "1"
    if skip_process_scan:
        print("Anti-Cheat: process scan DISABLED via BT_DISABLE_ANTICHEAT_SCAN (diagnostic mode).")

    cycles = 0
    last_recording_cycle = 0
    while True:
        time.sleep(1.0)
        cycles += 1

        # 1. Global Process Check
        if not skip_process_scan and detect_cheat_engine():
            print("CRITICAL: Cheat Engine process detected! Exiting...")
            try:
                from libs import logger
                logger.error("CRITICAL: Cheat Engine process detected! Client is shutting down for competitive integrity.")
            except: pass

            global _game
            if _game and hasattr(_game, "network") and _game.network:
                try:
                    from libs import consts
                    _game.network.send(consts.CHANNEL_MISC, "cheat_detected", {"reason": "Cheat Engine Process Detected"})
                    time.sleep(0.5)
                except: pass
            _exit_for_cheat("cheat_engine_detected")
        
        # 2. Recording Software (advisory only -- nothing is blocked or kicked)
        # The scan is the expensive part, so it runs on a timer instead of every
        # cycle: every five seconds in the menu, every two in a match, where a
        # short recording is the one worth catching.  The heartbeat rides along
        # (throttled to once a minute) so the Server can tell a quiet watch from
        # a silent one.
        if not skip_process_scan:
            report_recording_watch_heartbeat()
            if cycles - last_recording_cycle >= _recording_scan_seconds():
                last_recording_cycle = cycles
                report_recording_tools()

        # 3. Speedhack Check
        current_real_time = time.time()
        current_game_time = pygame.time.get_ticks() / 1000.0
        
        real_delta = current_real_time - last_real_time
        game_delta = current_game_time - last_game_time
        
        if game_delta > (real_delta * 1.5):
            print(f"CRITICAL: Speedhack detected! (Real: {real_delta:.2f}s | Game: {game_delta:.2f}s). Exiting...")
            try:
                from libs import logger
                logger.error(f"CRITICAL: Speedhack detected! (Real: {real_delta:.2f}s | Game: {game_delta:.2f}s). Client is shutting down.")
            except: pass

            if _game and hasattr(_game, "network") and _game.network:
                try:
                    from libs import consts
                    _game.network.send(consts.CHANNEL_MISC, "cheat_detected", {"reason": f"Speedhack Detected (Game Time > Real Time)"})
                    time.sleep(0.5)
                except: pass
            _exit_for_cheat("speedhack_detected")
            
        last_real_time = current_real_time
        last_game_time = current_game_time

def start_speedhack_watchdog():
    """
    Starts the anti-cheat speedhack watchdog in a daemon thread.
    Must be called after pygame.init().
    """
    t = threading.Thread(target=_speedhack_watchdog, daemon=True)
    t.start()
