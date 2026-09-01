"""Native accessible progress reporting for the Windows game window.

Pygame exposes an SDL window, but changing that window's caption does not
create a progress-bar accessibility object. This helper owns a standard
Windows progress control so screen readers can apply the user's configured
progress reporting (speech, beeps, or silence).
"""

import sys


class AccessibleProgressBar:
    """Main-thread-owned native progress bar with a no-op fallback."""

    _WM_USER = 0x0400
    _PBM_SETPOS = _WM_USER + 2
    _PBM_SETRANGE32 = _WM_USER + 6
    _EVENT_OBJECT_VALUECHANGE = 0x800E
    _OBJID_CLIENT = -4
    _CHILDID_SELF = 0
    _ICC_PROGRESS_CLASS = 0x20
    _WS_CHILD = 0x40000000
    _WS_VISIBLE = 0x10000000
    _PBS_SMOOTH = 0x01
    _HEIGHT = 18

    def __init__(self, name="Downloading update"):
        self.name = str(name)
        self.hwnd = None
        self._user32 = None

    @property
    def active(self):
        return bool(self.hwnd)

    def create(self):
        """Create the control under the current Pygame window, if possible."""
        if self.active:
            return True
        if sys.platform != "win32":
            return False

        try:
            import ctypes
            from ctypes import wintypes
            import pygame

            parent = pygame.display.get_wm_info().get("window")
            if not parent:
                return False

            class INITCOMMONCONTROLSEX(ctypes.Structure):
                _fields_ = [
                    ("dwSize", wintypes.DWORD),
                    ("dwICC", wintypes.DWORD),
                ]

            user32 = ctypes.WinDLL("user32", use_last_error=True)
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            comctl32 = ctypes.WinDLL("comctl32", use_last_error=True)

            init_common_controls = comctl32.InitCommonControlsEx
            init_common_controls.argtypes = [ctypes.POINTER(INITCOMMONCONTROLSEX)]
            init_common_controls.restype = wintypes.BOOL

            get_client_rect = user32.GetClientRect
            get_client_rect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
            get_client_rect.restype = wintypes.BOOL

            create_window = user32.CreateWindowExW
            create_window.argtypes = [
                wintypes.DWORD,
                wintypes.LPCWSTR,
                wintypes.LPCWSTR,
                wintypes.DWORD,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_int,
                wintypes.HWND,
                wintypes.HMENU,
                wintypes.HINSTANCE,
                wintypes.LPVOID,
            ]
            create_window.restype = wintypes.HWND

            send_message = user32.SendMessageW
            send_message.argtypes = [
                wintypes.HWND,
                wintypes.UINT,
                wintypes.WPARAM,
                wintypes.LPARAM,
            ]
            send_message.restype = wintypes.LPARAM

            set_window_text = user32.SetWindowTextW
            set_window_text.argtypes = [wintypes.HWND, wintypes.LPCWSTR]
            set_window_text.restype = wintypes.BOOL

            notify_win_event = user32.NotifyWinEvent
            notify_win_event.argtypes = [
                wintypes.DWORD,
                wintypes.HWND,
                wintypes.LONG,
                wintypes.LONG,
            ]
            notify_win_event.restype = None

            destroy_window = user32.DestroyWindow
            destroy_window.argtypes = [wintypes.HWND]
            destroy_window.restype = wintypes.BOOL

            get_module_handle = kernel32.GetModuleHandleW
            get_module_handle.argtypes = [wintypes.LPCWSTR]
            get_module_handle.restype = wintypes.HMODULE

            controls = INITCOMMONCONTROLSEX(
                ctypes.sizeof(INITCOMMONCONTROLSEX), self._ICC_PROGRESS_CLASS
            )
            if not init_common_controls(ctypes.byref(controls)):
                return False

            rect = wintypes.RECT()
            if not get_client_rect(parent, ctypes.byref(rect)):
                return False
            width = max(1, int(rect.right - rect.left))
            height = max(1, int(rect.bottom - rect.top))

            hwnd = create_window(
                0,
                "msctls_progress32",
                self.name,
                self._WS_CHILD | self._WS_VISIBLE | self._PBS_SMOOTH,
                0,
                max(0, height - self._HEIGHT),
                width,
                self._HEIGHT,
                parent,
                None,
                get_module_handle(None),
                None,
            )
            if not hwnd:
                return False

            self._user32 = user32
            self.hwnd = hwnd
            set_window_text(hwnd, self.name)
            send_message(hwnd, self._PBM_SETRANGE32, 0, 100)
            self.set_value(0)
            return True
        except Exception:
            self.destroy()
            return False

    def set_value(self, value):
        """Set a clamped percentage and notify accessibility clients."""
        if not self.active or self._user32 is None:
            return False
        try:
            value = max(0, min(100, int(value)))
            self._user32.SendMessageW(self.hwnd, self._PBM_SETPOS, value, 0)
            self._user32.NotifyWinEvent(
                self._EVENT_OBJECT_VALUECHANGE,
                self.hwnd,
                self._OBJID_CLIENT,
                self._CHILDID_SELF,
            )
            return True
        except Exception:
            return False

    def destroy(self):
        """Destroy the native child control; safe to call repeatedly."""
        hwnd, self.hwnd = self.hwnd, None
        user32, self._user32 = self._user32, None
        if not hwnd or user32 is None:
            return
        try:
            user32.DestroyWindow(hwnd)
        except Exception:
            pass
