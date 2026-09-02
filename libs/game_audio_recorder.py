"""Private recording of the current Beyond Tournament process audio.

Windows process-loopback capture is deliberately used instead of endpoint
loopback.  This keeps browsers, media players, NVDA, and every other process
out of the file while preserving the final game mix (including spatial audio,
reverb, received voice, Music Bot, and Jukebox output).
"""

from __future__ import annotations

import ctypes
from ctypes import POINTER, Structure, Union, byref, c_longlong, c_ubyte
from ctypes import c_ulong, c_ulonglong, c_ushort, c_void_p, c_wchar_p
from ctypes import wintypes
import os
from pathlib import Path
import threading
import time
import wave

from . import options


MIN_PROCESS_LOOPBACK_BUILD = 20348
SAMPLE_RATE = 48_000
CHANNELS = 2
SAMPLE_WIDTH = 2


class ProcessLoopbackUnavailable(RuntimeError):
    """Raised when isolated application capture is unavailable or fails."""


def process_loopback_supported() -> bool:
    """Return whether this Windows build exposes process-loopback capture."""
    return os.name == "nt" and int(getattr(__import__("sys").getwindowsversion(), "build", 0)) >= MIN_PROCESS_LOOPBACK_BUILD


def _format_hresult(value: int) -> str:
    return f"0x{ctypes.c_ulong(value).value:08X}"


if os.name == "nt":
    import comtypes
    from comtypes import COMMETHOD, COMObject, GUID, HRESULT, IUnknown

    class WAVEFORMATEX(Structure):
        _fields_ = [
            ("wFormatTag", c_ushort),
            ("nChannels", c_ushort),
            ("nSamplesPerSec", c_ulong),
            ("nAvgBytesPerSec", c_ulong),
            ("nBlockAlign", c_ushort),
            ("wBitsPerSample", c_ushort),
            ("cbSize", c_ushort),
        ]


    class _AudioClientProcessLoopbackParams(Structure):
        _fields_ = [
            ("TargetProcessId", c_ulong),
            ("ProcessLoopbackMode", ctypes.c_int),
        ]


    class _AudioClientActivationUnion(Union):
        _fields_ = [("ProcessLoopbackParams", _AudioClientProcessLoopbackParams)]


    class _AudioClientActivationParams(Structure):
        _anonymous_ = ("u",)
        _fields_ = [
            ("ActivationType", ctypes.c_int),
            ("u", _AudioClientActivationUnion),
        ]


    class _Blob(Structure):
        _fields_ = [
            ("cbSize", c_ulong),
            ("pBlobData", POINTER(c_ubyte)),
        ]


    class _PropVariantValue(Union):
        _fields_ = [
            ("blob", _Blob),
            ("_alignment", c_ulonglong * 2),
        ]


    class _PropVariant(Structure):
        _anonymous_ = ("value",)
        _fields_ = [
            ("vt", c_ushort),
            ("wReserved1", c_ushort),
            ("wReserved2", c_ushort),
            ("wReserved3", c_ushort),
            ("value", _PropVariantValue),
        ]


    class IAudioCaptureClient(IUnknown):
        _iid_ = GUID("{C8ADBD64-E71E-48A0-A4DE-185C395CD317}")
        _methods_ = [
            COMMETHOD(
                [], HRESULT, "GetBuffer",
                (["out"], POINTER(POINTER(c_ubyte)), "ppData"),
                (["out"], POINTER(c_ulong), "pNumFramesToRead"),
                (["out"], POINTER(c_ulong), "pdwFlags"),
                (["out"], POINTER(c_ulonglong), "pu64DevicePosition"),
                (["out"], POINTER(c_ulonglong), "pu64QPCPosition"),
            ),
            COMMETHOD([], HRESULT, "ReleaseBuffer", (["in"], c_ulong, "NumFramesRead")),
            COMMETHOD([], HRESULT, "GetNextPacketSize", (["out"], POINTER(c_ulong), "pNumFramesInNextPacket")),
        ]


    class IAudioClient(IUnknown):
        _iid_ = GUID("{1CB9AD4C-DBFA-4C32-B178-C2F568A703B2}")
        _methods_ = [
            COMMETHOD(
                [], HRESULT, "Initialize",
                (["in"], ctypes.c_int, "ShareMode"),
                (["in"], c_ulong, "StreamFlags"),
                (["in"], c_longlong, "hnsBufferDuration"),
                (["in"], c_longlong, "hnsPeriodicity"),
                (["in"], POINTER(WAVEFORMATEX), "pFormat"),
                (["in"], POINTER(GUID), "AudioSessionGuid"),
            ),
            COMMETHOD([], HRESULT, "GetBufferSize", (["out"], POINTER(c_ulong), "pNumBufferFrames")),
            COMMETHOD([], HRESULT, "GetStreamLatency", (["out"], POINTER(c_longlong), "phnsLatency")),
            COMMETHOD([], HRESULT, "GetCurrentPadding", (["out"], POINTER(c_ulong), "pNumPaddingFrames")),
            COMMETHOD(
                [], HRESULT, "IsFormatSupported",
                (["in"], ctypes.c_int, "ShareMode"),
                (["in"], POINTER(WAVEFORMATEX), "pFormat"),
                (["out"], POINTER(POINTER(WAVEFORMATEX)), "ppClosestMatch"),
            ),
            COMMETHOD([], HRESULT, "GetMixFormat", (["out"], POINTER(POINTER(WAVEFORMATEX)), "ppDeviceFormat")),
            COMMETHOD(
                [], HRESULT, "GetDevicePeriod",
                (["out"], POINTER(c_longlong), "phnsDefaultDevicePeriod"),
                (["out"], POINTER(c_longlong), "phnsMinimumDevicePeriod"),
            ),
            COMMETHOD([], HRESULT, "Start"),
            COMMETHOD([], HRESULT, "Stop"),
            COMMETHOD([], HRESULT, "Reset"),
            COMMETHOD([], HRESULT, "SetEventHandle", (["in"], wintypes.HANDLE, "eventHandle")),
            COMMETHOD(
                [], HRESULT, "GetService",
                (["in"], POINTER(GUID), "riid"),
                (["out"], POINTER(POINTER(IAudioCaptureClient)), "ppv"),
            ),
        ]


    class IActivateAudioInterfaceAsyncOperation(IUnknown):
        _iid_ = GUID("{72A22D78-CDE4-431D-B8CC-843A71199B6D}")
        _methods_ = [
            COMMETHOD(
                [], HRESULT, "GetActivateResult",
                (["out"], POINTER(HRESULT), "activateResult"),
                (["out"], POINTER(POINTER(IUnknown)), "activatedInterface"),
            ),
        ]


    class IActivateAudioInterfaceCompletionHandler(IUnknown):
        _iid_ = GUID("{41D949AB-9862-444A-80F6-C261334DA5EB}")
        _methods_ = [
            COMMETHOD(
                [], HRESULT, "ActivateCompleted",
                (["in"], POINTER(IActivateAudioInterfaceAsyncOperation), "activateOperation"),
            ),
        ]


    class IAgileObject(IUnknown):
        _iid_ = GUID("{94EA2B94-E9CC-49E0-C0FF-EE64CA8F5B90}")
        _methods_ = []


    class _ActivationHandler(COMObject):
        _com_interfaces_ = [IActivateAudioInterfaceCompletionHandler, IAgileObject]

        def __init__(self):
            super().__init__()
            self.completed = threading.Event()

        def ActivateCompleted(self, activate_operation):
            self.completed.set()
            return 0


    _mmdevapi = ctypes.WinDLL("Mmdevapi.dll")
    _activate_audio_interface_async = _mmdevapi.ActivateAudioInterfaceAsync
    _activate_audio_interface_async.argtypes = [
        c_wchar_p,
        POINTER(GUID),
        POINTER(_PropVariant),
        POINTER(IActivateAudioInterfaceCompletionHandler),
        POINTER(POINTER(IActivateAudioInterfaceAsyncOperation)),
    ]
    _activate_audio_interface_async.restype = HRESULT

    _kernel32 = ctypes.WinDLL("kernel32.dll", use_last_error=True)
    _kernel32.CreateEventW.argtypes = [c_void_p, wintypes.BOOL, wintypes.BOOL, c_wchar_p]
    _kernel32.CreateEventW.restype = wintypes.HANDLE
    _kernel32.SetEvent.argtypes = [wintypes.HANDLE]
    _kernel32.SetEvent.restype = wintypes.BOOL
    _kernel32.WaitForMultipleObjects.argtypes = [
        c_ulong, POINTER(wintypes.HANDLE), wintypes.BOOL, c_ulong,
    ]
    _kernel32.WaitForMultipleObjects.restype = c_ulong
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.CloseHandle.restype = wintypes.BOOL


class ProcessLoopbackRecorder:
    """Capture the final audio rendered by one process tree into a WAV file."""

    AUDCLNT_SHAREMODE_SHARED = 0
    AUDCLNT_STREAMFLAGS_LOOPBACK = 0x00020000
    AUDCLNT_STREAMFLAGS_EVENTCALLBACK = 0x00040000
    AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM = 0x80000000
    AUDCLNT_BUFFERFLAGS_SILENT = 0x00000002
    VT_BLOB = 65
    WAIT_OBJECT_0 = 0
    WAIT_FAILED = 0xFFFFFFFF

    def __init__(self, process_id: int | None = None):
        self.process_id = int(process_id or os.getpid())

    @staticmethod
    def _new_event():
        handle = _kernel32.CreateEventW(None, False, False, None)
        if not handle:
            raise ProcessLoopbackUnavailable(
                f"Could not create a recording event ({ctypes.get_last_error()})."
            )
        return handle

    def _activate(self):
        activation = _AudioClientActivationParams()
        activation.ActivationType = 1
        activation.ProcessLoopbackParams.TargetProcessId = self.process_id
        activation.ProcessLoopbackParams.ProcessLoopbackMode = 0

        prop = _PropVariant()
        prop.vt = self.VT_BLOB
        prop.blob.cbSize = ctypes.sizeof(activation)
        prop.blob.pBlobData = ctypes.cast(byref(activation), POINTER(c_ubyte))

        handler = _ActivationHandler()
        handler_interface = handler.QueryInterface(IActivateAudioInterfaceCompletionHandler)
        operation = POINTER(IActivateAudioInterfaceAsyncOperation)()
        result = _activate_audio_interface_async(
            "VAD\\Process_Loopback",
            byref(IAudioClient._iid_),
            byref(prop),
            handler_interface,
            byref(operation),
        )
        if result < 0:
            raise ProcessLoopbackUnavailable(
                f"Windows rejected process audio capture ({_format_hresult(result)})."
            )
        if not handler.completed.wait(10.0):
            raise ProcessLoopbackUnavailable("Windows timed out while preparing process audio capture.")

        activation_result, activated = operation.GetActivateResult()
        if activation_result < 0 or not activated:
            raise ProcessLoopbackUnavailable(
                f"Windows could not activate process audio capture ({_format_hresult(activation_result)})."
            )
        return activated.QueryInterface(IAudioClient)

    def record(self, output_path, stop_event: threading.Event, started=None):
        """Block on a worker thread until *stop_event* is set.

        Returns ``(frames_written, elapsed_seconds)``. No game/OpenAL state is
        touched from this thread.
        """
        if not process_loopback_supported():
            raise ProcessLoopbackUnavailable(
                "Game-only recording requires Windows build 20348 or newer."
            )

        output_path = os.fspath(output_path)
        parent = os.path.dirname(os.path.abspath(output_path))
        os.makedirs(parent, exist_ok=True)

        sample_handle = None
        stop_handle = None
        audio_client = None
        capture_client = None
        wave_file = None
        raw_output = None
        frames_written = 0
        started_at = None
        completed = False
        comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
        try:
            audio_client = self._activate()
            block_align = CHANNELS * SAMPLE_WIDTH
            audio_format = WAVEFORMATEX(
                1,
                CHANNELS,
                SAMPLE_RATE,
                SAMPLE_RATE * block_align,
                block_align,
                SAMPLE_WIDTH * 8,
                0,
            )
            flags = (
                self.AUDCLNT_STREAMFLAGS_LOOPBACK
                | self.AUDCLNT_STREAMFLAGS_EVENTCALLBACK
                | self.AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM
            )
            audio_client.Initialize(
                self.AUDCLNT_SHAREMODE_SHARED,
                flags,
                2_000_000,
                0,
                byref(audio_format),
                None,
            )
            sample_handle = self._new_event()
            stop_handle = self._new_event()
            audio_client.SetEventHandle(sample_handle)
            capture_client = audio_client.GetService(byref(IAudioCaptureClient._iid_))

            # Exclusive creation is intentional: an automatic timestamp or a
            # late filesystem race must never overwrite an existing recording.
            raw_output = open(output_path, "xb")
            wave_file = wave.open(raw_output, "wb")
            wave_file.setnchannels(CHANNELS)
            wave_file.setsampwidth(SAMPLE_WIDTH)
            wave_file.setframerate(SAMPLE_RATE)

            audio_client.Start()
            started_at = time.monotonic()
            if started:
                started()

            handles = (wintypes.HANDLE * 2)(sample_handle, stop_handle)
            while True:
                if stop_event.is_set():
                    _kernel32.SetEvent(stop_handle)
                wait_result = _kernel32.WaitForMultipleObjects(2, handles, False, 100)
                if wait_result == self.WAIT_OBJECT_0 + 1:
                    break
                if wait_result == self.WAIT_FAILED:
                    raise ProcessLoopbackUnavailable(
                        f"Windows recording wait failed ({ctypes.get_last_error()})."
                    )
                if wait_result != self.WAIT_OBJECT_0:
                    continue

                packet_frames = capture_client.GetNextPacketSize()
                while packet_frames:
                    data, frame_count, buffer_flags, _, _ = capture_client.GetBuffer()
                    try:
                        byte_count = int(frame_count) * block_align
                        if buffer_flags & self.AUDCLNT_BUFFERFLAGS_SILENT:
                            wave_file.writeframesraw(b"\0" * byte_count)
                        else:
                            wave_file.writeframesraw(ctypes.string_at(data, byte_count))
                        frames_written += int(frame_count)
                    finally:
                        capture_client.ReleaseBuffer(frame_count)
                    packet_frames = capture_client.GetNextPacketSize()
            completed = True
        finally:
            if audio_client is not None:
                try:
                    audio_client.Stop()
                except Exception:
                    pass
            if wave_file is not None:
                wave_file.close()
            if raw_output is not None:
                raw_output.close()
            if sample_handle:
                _kernel32.CloseHandle(sample_handle)
            if stop_handle:
                _kernel32.CloseHandle(stop_handle)
            # Release every COM interface while this worker's MTA is still
            # initialized. Relying on later Python GC could call Release after
            # CoUninitialize and make shutdown nondeterministic.
            capture_client = None
            audio_client = None
            comtypes.CoUninitialize()
            if not completed and raw_output is not None and frames_written <= 0:
                try:
                    os.remove(output_path)
                except OSError:
                    pass

        elapsed = max(0.0, time.monotonic() - started_at) if started_at else 0.0
        return frames_written, elapsed


class GameAudioRecorderManager:
    """Accessible Music Bot menu facade for isolated game-audio recording."""

    ACTIVE_STATES = frozenset(("selecting", "countdown", "recording", "stopping"))

    def __init__(
        self,
        game,
        parent_provider,
        *,
        backend_factory=ProcessLoopbackRecorder,
        countdown_interval=1.0,
    ):
        self.game = game
        self._parent_provider = parent_provider
        self._backend_factory = backend_factory
        self._countdown_interval = max(0.0, float(countdown_interval))
        self._lock = threading.Lock()
        self._state = "idle"
        self._stop_event = threading.Event()
        self._cancel_countdown = threading.Event()
        self._worker = None
        self._output_path = ""
        self._closed = False
        self._recording_started_at = None
        self._configuring_folder = False

    def _dispatch(self, callback):
        if self._closed:
            return
        try:
            self.game.put(callback)
        except Exception:
            callback()

    @staticmethod
    def _speak(message):
        from .speech import speak
        speak(message)

    def _announce(self, message):
        self._dispatch(lambda message=message: self._speak(message))

    def state(self):
        with self._lock:
            return self._state

    def is_active(self):
        return self.state() in self.ACTIVE_STATES

    def menu_label(self):
        state = self.state()
        if state == "idle":
            return "Start Recording"
        if state == "recording":
            return "Stop Recording"
        if state == "stopping":
            return "Recording is stopping"
        return "Cancel Recording Setup"

    def status_menu_label(self):
        state = self.state()
        if state == "idle":
            return "Recording status: Not recording"
        if state == "selecting":
            return "Recording status: Selecting a recording file"
        if state == "countdown":
            return "Recording status: Countdown"
        if state == "stopping":
            return "Recording status: Stopping"
        with self._lock:
            started_at = self._recording_started_at
        elapsed = max(0, int(time.monotonic() - started_at)) if started_at else 0
        return f"Recording status: Recording, {elapsed} seconds"

    def speak_status(self):
        self._announce(self.status_menu_label().replace("Recording status: ", ""))

    def configured_folder(self):
        value = options.get("music_bot_recording_folder", "")
        if not isinstance(value, str) or not value.strip():
            return ""
        path = os.path.abspath(os.path.expanduser(value.strip()))
        return path if os.path.isdir(path) else ""

    def folder_menu_label(self):
        folder = self.configured_folder()
        return f"Recording folder: {folder}" if folder else "Recording folder: Not set"

    def speak_folder(self):
        self._announce(self.folder_menu_label())

    def _remember_folder(self, folder):
        folder = os.path.abspath(os.fspath(folder))
        options.set("music_bot_recording_folder", folder)

    def choose_folder(self):
        if self.is_active():
            self._announce("Stop the current recording before changing its folder.")
            return
        with self._lock:
            if self._configuring_folder:
                self._announce("The recording folder dialog is already open.")
                return
            self._configuring_folder = True
        initial = self.configured_folder() or str(Path.home() / "Music")
        threading.Thread(target=self._choose_folder_worker, args=(initial,), daemon=True).start()

    def _choose_folder_worker(self, initial):
        folder = ""
        choice_dispatched = False
        try:
            import tkinter as tk
            from tkinter import filedialog

            root = tk.Tk()
            root.withdraw()
            root.attributes("-topmost", True)
            try:
                folder = filedialog.askdirectory(
                    title="Select Beyond Tournament recording folder",
                    initialdir=initial,
                    mustexist=True,
                )
            finally:
                root.destroy()
            if folder:
                choice_dispatched = True
                self._dispatch(lambda folder=folder: self._accept_folder_choice(folder))
            else:
                self._announce("Recording folder was not changed.")
        except Exception as error:
            print(f"[GameAudioRecorder] Folder dialog failed: {error}")
            self._announce("The recording folder dialog could not be opened.")
        finally:
            if not choice_dispatched:
                with self._lock:
                    self._configuring_folder = False

    def _accept_folder_choice(self, folder):
        try:
            if self._closed:
                return
            self._remember_folder(folder)
            self._speak(f"Recording folder set to {folder}.")
        except Exception as error:
            print(f"[GameAudioRecorder] Could not save recording folder: {error}")
            self._speak("The recording folder could not be saved.")
        finally:
            with self._lock:
                self._configuring_folder = False

    def open_folder(self):
        folder = self.configured_folder()
        if not folder:
            self._announce("Set a recording folder first.")
            return
        try:
            os.startfile(folder)
        except Exception as error:
            print(f"[GameAudioRecorder] Could not open recording folder: {error}")
            self._announce("The recording folder could not be opened.")

    @staticmethod
    def _next_recording_path(folder):
        folder = Path(folder)
        base = time.strftime("Beyond Tournament Recording %Y-%m-%d %H-%M-%S")
        return GameAudioRecorderManager._collision_safe_path(folder / f"{base}.wav")

    @staticmethod
    def _collision_safe_path(path):
        candidate = Path(path)
        base = candidate.stem
        extension = candidate.suffix or ".wav"
        suffix = 2
        while candidate.exists():
            candidate = candidate.parent / f"{base} ({suffix}){extension}"
            suffix += 1
        return str(candidate)

    def menu_action(self):
        if self.state() == "idle":
            self.request_start()
        else:
            self.stop()

    def request_start(self):
        if not process_loopback_supported():
            self._announce("Game-only recording requires Windows build 20348 or newer.")
            return
        with self._lock:
            if self._state != "idle":
                self._announce("A game audio recording is already being prepared or recorded.")
                return
            if self._configuring_folder:
                self._announce("Finish selecting the recording folder first.")
                return
        folder = self.configured_folder()
        if folder:
            self.start_to_path(self._next_recording_path(folder))
            return
        with self._lock:
            if self._state != "idle":
                return
            self._state = "selecting"
            self._stop_event.clear()
            self._cancel_countdown.clear()
        threading.Thread(target=self._select_output_path, daemon=True).start()

    def _select_output_path(self):
        path = ""
        try:
            import tkinter as tk
            from tkinter import filedialog

            folder = Path.home() / "Music" / "Beyond Tournament Recordings"
            folder.mkdir(parents=True, exist_ok=True)
            default_name = time.strftime("Beyond Tournament Recording %Y-%m-%d %H-%M-%S.wav")
            root = tk.Tk()
            root.withdraw()
            root.attributes("-topmost", True)
            try:
                path = filedialog.asksaveasfilename(
                    title="Save Beyond Tournament game audio recording",
                    initialdir=str(folder),
                    initialfile=default_name,
                    defaultextension=".wav",
                    filetypes=[("WAV audio", "*.wav")],
                    confirmoverwrite=True,
                )
            finally:
                root.destroy()
        except Exception as error:
            print(f"[GameAudioRecorder] Save dialog failed: {error}")
            self._finish_idle()
            self._announce("The recording file dialog could not be opened.")
            return

        with self._lock:
            cancelled = self._state != "selecting" or self._closed
        if cancelled:
            self._finish_idle()
            return
        if not path:
            self._finish_idle()
            self._announce("Recording cancelled.")
            return
        if not path.lower().endswith(".wav"):
            path += ".wav"
        self._dispatch(lambda path=path: self._accept_selected_path(path))

    def _accept_selected_path(self, path):
        with self._lock:
            state = self._state
            closed = self._closed
        if state != "selecting" or closed:
            if state == "stopping":
                self._finish_idle()
            return
        path = self._collision_safe_path(path)
        try:
            self._remember_folder(os.path.dirname(os.path.abspath(path)))
        except Exception as error:
            # The selected file remains valid for this recording even if the
            # encrypted preference cannot be updated.
            print(f"[GameAudioRecorder] Could not remember recording folder: {error}")
        self.start_to_path(path)

    def start_to_path(self, output_path):
        """Begin the non-blocking countdown; exposed for focused tests."""
        with self._lock:
            if self._state not in ("idle", "selecting") or self._closed:
                return False
            self._state = "countdown"
            self._output_path = os.fspath(output_path)
            self._stop_event.clear()
            self._cancel_countdown.clear()
        self._worker = threading.Thread(target=self._countdown_and_record, daemon=True)
        self._worker.start()
        return True

    def _countdown_and_record(self):
        self._announce("Ready, 3.")
        for number in (2, 1):
            if self._cancel_countdown.wait(self._countdown_interval):
                self._finish_idle()
                return
            self._announce(f"{number}.")
        if self._cancel_countdown.wait(self._countdown_interval):
            self._finish_idle()
            return

        with self._lock:
            if self._state != "countdown" or self._closed:
                self._state = "idle"
                return
            output_path = self._output_path
        try:
            backend = self._backend_factory(os.getpid())

            def on_started():
                should_announce = False
                with self._lock:
                    if self._state == "countdown":
                        self._state = "recording"
                        self._recording_started_at = time.monotonic()
                        should_announce = True
                if should_announce:
                    self._announce("Recording.")

            frames, elapsed = backend.record(output_path, self._stop_event, on_started)
            if frames <= 0:
                try:
                    if os.path.exists(output_path):
                        os.remove(output_path)
                except OSError:
                    pass
                self._announce("No game audio was captured. The empty recording was removed.")
            else:
                self._announce(
                    f"Recording saved. {elapsed:.0f} seconds. {os.path.basename(output_path)}"
                )
        except Exception as error:
            print(f"[GameAudioRecorder] Recording failed: {error}")
            self._announce(f"Game audio recording failed. {error}")
        finally:
            self._finish_idle()

    def stop(self):
        state = self.state()
        if state == "idle":
            self._announce("No game audio recording is active.")
            return
        if state in ("selecting", "countdown"):
            with self._lock:
                self._state = "stopping"
            self._cancel_countdown.set()
            self._stop_event.set()
            self._announce("Recording setup cancelled.")
            return
        if state == "stopping":
            self._announce("The recording is already stopping.")
            return
        with self._lock:
            self._state = "stopping"
        self._stop_event.set()
        self._announce("Stopping recording.")

    def _finish_idle(self):
        with self._lock:
            self._state = "idle"
            self._output_path = ""
            self._worker = None
            self._recording_started_at = None
            self._stop_event.clear()
            self._cancel_countdown.clear()

    def close(self):
        self._closed = True
        self._cancel_countdown.set()
        self._stop_event.set()
