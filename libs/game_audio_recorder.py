"""Private recording of the current Beyond Tournament process audio.

Windows process-loopback capture is the safe default. It keeps browsers,
media players, NVDA, and every other process out of the file while preserving
the final game mix (including spatial audio, reverb, received voice, Music Bot,
and Jukebox output). An explicit setting can instead capture the default
Windows output endpoint when screen-reader and other computer audio is wanted.
"""

from __future__ import annotations

import ctypes
from array import array
from collections import deque
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
DEFAULT_COUNTDOWN_SECONDS = 3
COUNTDOWN_CHOICES = (0, 3, 5)
SPLIT_MINUTE_CHOICES = (0, 30, 60, 120)


class ProcessLoopbackUnavailable(RuntimeError):
    """Raised when isolated application capture is unavailable or fails."""


class MicrophoneOverlayBuffer:
    """Bounded mono PCM handoff from Voice Chat to the recorder worker.

    The Voice Chat capture thread only performs a short append under a lock.
    Conversion, clipping and WAV I/O remain owned by the recorder thread.
    """

    MAX_FRAMES = SAMPLE_RATE * 2

    def __init__(self):
        self._lock = threading.Lock()
        self._chunks = deque()
        self._queued_frames = 0

    def clear(self):
        with self._lock:
            self._chunks.clear()
            self._queued_frames = 0

    def put_mono16(self, pcm):
        data = bytes(pcm)
        if len(data) < 2:
            return
        if len(data) % 2:
            data = data[:-1]
        frames = len(data) // 2
        with self._lock:
            self._chunks.append(data)
            self._queued_frames += frames
            while self._queued_frames > self.MAX_FRAMES and self._chunks:
                removed = self._chunks.popleft()
                self._queued_frames -= len(removed) // 2

    def take_mono16(self, frame_count):
        wanted = max(0, int(frame_count))
        if wanted <= 0:
            return None
        with self._lock:
            available = min(wanted, self._queued_frames)
            if available <= 0:
                return None
            remaining_bytes = available * 2
            output = bytearray()
            while remaining_bytes and self._chunks:
                chunk = self._chunks.popleft()
                if len(chunk) <= remaining_bytes:
                    output.extend(chunk)
                    self._queued_frames -= len(chunk) // 2
                    remaining_bytes -= len(chunk)
                else:
                    output.extend(chunk[:remaining_bytes])
                    self._chunks.appendleft(chunk[remaining_bytes:])
                    self._queued_frames -= remaining_bytes // 2
                    remaining_bytes = 0
        if available < wanted:
            output.extend(b"\0" * ((wanted - available) * 2))
        return bytes(output)


def _mix_mono16_into_stereo16(stereo_pcm, mono_pcm):
    """Mix mono microphone PCM into both stereo channels with clipping."""
    if not mono_pcm:
        return stereo_pcm
    stereo = array("h")
    stereo.frombytes(stereo_pcm)
    mono = array("h")
    mono.frombytes(mono_pcm)
    frame_count = min(len(mono), len(stereo) // 2)
    for frame in range(frame_count):
        sample = mono[frame]
        left = stereo[frame * 2] + sample
        right = stereo[frame * 2 + 1] + sample
        stereo[frame * 2] = max(-32768, min(32767, left))
        stereo[frame * 2 + 1] = max(-32768, min(32767, right))
    return stereo.tobytes()


class _SegmentedWaveWriter:
    """Write PCM to one or more collision-safe WAV segments."""

    def __init__(self, output_path, split_minutes=0):
        self.output_path = os.path.abspath(os.fspath(output_path))
        self.max_frames = max(0, int(split_minutes)) * 60 * SAMPLE_RATE
        self.paths = []
        self._wave = None
        self._raw = None
        self._part_frames = 0
        self._open_part(1)

    def _part_path(self, part_number):
        source = Path(self.output_path)
        if part_number <= 1:
            candidate = source
        else:
            candidate = source.with_name(f"{source.stem} Part {part_number}{source.suffix}")
        if not candidate.exists():
            return candidate
        suffix = 2
        while True:
            alternative = candidate.with_name(f"{candidate.stem} ({suffix}){candidate.suffix}")
            if not alternative.exists():
                return alternative
            suffix += 1

    def _open_part(self, part_number):
        path = self._part_path(part_number)
        self._raw = open(path, "xb")
        try:
            self._wave = wave.open(self._raw, "wb")
            self._wave.setnchannels(CHANNELS)
            self._wave.setsampwidth(SAMPLE_WIDTH)
            self._wave.setframerate(SAMPLE_RATE)
        except Exception:
            try:
                if self._wave is not None:
                    self._wave.close()
            finally:
                self._wave = None
                self._raw.close()
                self._raw = None
                try:
                    os.remove(path)
                except OSError:
                    pass
            raise
        self.paths.append(str(path))
        self._part_frames = 0

    def _close_part(self):
        if self._wave is not None:
            self._wave.close()
            self._wave = None
        if self._raw is not None:
            self._raw.close()
            self._raw = None

    def write(self, pcm):
        block_align = CHANNELS * SAMPLE_WIDTH
        offset = 0
        total_frames = len(pcm) // block_align
        while total_frames > 0:
            if self.max_frames:
                remaining = self.max_frames - self._part_frames
                if remaining <= 0:
                    self._close_part()
                    self._open_part(len(self.paths) + 1)
                    remaining = self.max_frames
                frame_count = min(total_frames, remaining)
            else:
                frame_count = total_frames
            byte_count = frame_count * block_align
            self._wave.writeframesraw(pcm[offset:offset + byte_count])
            self._part_frames += frame_count
            total_frames -= frame_count
            offset += byte_count

    def close(self):
        self._close_part()


def process_loopback_supported() -> bool:
    """Return whether this Windows build exposes process-loopback capture."""
    return os.name == "nt" and int(getattr(__import__("sys").getwindowsversion(), "build", 0)) >= MIN_PROCESS_LOOPBACK_BUILD


def system_loopback_supported() -> bool:
    """Return whether Windows endpoint-loopback capture is available."""
    return os.name == "nt"


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


    class IMMDevice(IUnknown):
        _iid_ = GUID("{D666063F-1587-4E43-81F1-B948E807363F}")
        _methods_ = [
            COMMETHOD(
                [], HRESULT, "Activate",
                (["in"], POINTER(GUID), "iid"),
                (["in"], c_ulong, "dwClsCtx"),
                (["in"], c_void_p, "pActivationParams"),
                (["out"], POINTER(POINTER(IUnknown)), "ppInterface"),
            ),
            COMMETHOD(
                [], HRESULT, "OpenPropertyStore",
                (["in"], c_ulong, "stgmAccess"),
                (["out"], POINTER(c_void_p), "ppProperties"),
            ),
            COMMETHOD([], HRESULT, "GetId", (["out"], POINTER(c_wchar_p), "ppstrId")),
            COMMETHOD([], HRESULT, "GetState", (["out"], POINTER(c_ulong), "pdwState")),
        ]


    class IMMDeviceEnumerator(IUnknown):
        _iid_ = GUID("{A95664D2-9614-4F35-A746-DE8DB63617E6}")
        _methods_ = [
            COMMETHOD(
                [], HRESULT, "EnumAudioEndpoints",
                (["in"], ctypes.c_int, "dataFlow"),
                (["in"], c_ulong, "stateMask"),
                (["out"], POINTER(c_void_p), "ppDevices"),
            ),
            COMMETHOD(
                [], HRESULT, "GetDefaultAudioEndpoint",
                (["in"], ctypes.c_int, "dataFlow"),
                (["in"], ctypes.c_int, "role"),
                (["out"], POINTER(POINTER(IMMDevice)), "ppEndpoint"),
            ),
            COMMETHOD(
                [], HRESULT, "GetDevice",
                (["in"], c_wchar_p, "pwstrId"),
                (["out"], POINTER(POINTER(IMMDevice)), "ppDevice"),
            ),
            COMMETHOD(
                [], HRESULT, "RegisterEndpointNotificationCallback",
                (["in"], c_void_p, "pClient"),
            ),
            COMMETHOD(
                [], HRESULT, "UnregisterEndpointNotificationCallback",
                (["in"], c_void_p, "pClient"),
            ),
        ]


    CLSID_MMDEVICE_ENUMERATOR = GUID("{BCDE0395-E52F-467C-8E3D-C4579291692E}")


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

    def record(
        self,
        output_path,
        stop_event: threading.Event,
        started=None,
        *,
        microphone_overlay=None,
        split_minutes=0,
    ):
        """Block on a worker thread until *stop_event* is set.

        Returns ``(frames_written, elapsed_seconds, output_paths)``. No
        game/OpenAL state is touched from this thread.
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
        writer = None
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
            writer = _SegmentedWaveWriter(output_path, split_minutes)

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
                            pcm = b"\0" * byte_count
                        else:
                            pcm = ctypes.string_at(data, byte_count)
                        if microphone_overlay is not None:
                            mic_pcm = microphone_overlay.take_mono16(frame_count)
                            pcm = _mix_mono16_into_stereo16(pcm, mic_pcm)
                        writer.write(pcm)
                        frames_written += int(frame_count)
                    finally:
                        capture_client.ReleaseBuffer(frame_count)
                    packet_frames = capture_client.GetNextPacketSize()
            writer.close()
            completed = True
        finally:
            if audio_client is not None:
                try:
                    audio_client.Stop()
                except Exception:
                    pass
            if writer is not None:
                try:
                    writer.close()
                except Exception:
                    pass
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
            if not completed and writer is not None and frames_written <= 0:
                for path in writer.paths:
                    try:
                        os.remove(path)
                    except OSError:
                        pass

        elapsed = max(0.0, time.monotonic() - started_at) if started_at else 0.0
        return frames_written, elapsed, tuple(writer.paths if writer is not None else ())


class SystemOutputLoopbackRecorder(ProcessLoopbackRecorder):
    """Capture the default Windows output, including screen readers.

    This mode intentionally captures every application rendered through the
    same default output endpoint. It is opt-in because browsers, media players
    and notification sounds can therefore enter the recording too.
    """

    CLSCTX_ALL = 23
    E_RENDER = 0
    E_CONSOLE = 0

    def _activate(self):
        if os.name != "nt":
            raise ProcessLoopbackUnavailable("Computer audio recording requires Windows.")
        enumerator = comtypes.CoCreateInstance(
            CLSID_MMDEVICE_ENUMERATOR,
            interface=IMMDeviceEnumerator,
            clsctx=self.CLSCTX_ALL,
        )
        endpoint = enumerator.GetDefaultAudioEndpoint(self.E_RENDER, self.E_CONSOLE)
        activated = endpoint.Activate(
            byref(IAudioClient._iid_),
            self.CLSCTX_ALL,
            None,
        )
        return activated.QueryInterface(IAudioClient)


class GameAudioRecorderManager:
    """Accessible Music Bot menu facade for isolated game-audio recording."""

    ACTIVE_STATES = frozenset(("selecting", "countdown", "recording", "stopping"))

    def __init__(
        self,
        game,
        parent_provider,
        *,
        backend_factory=ProcessLoopbackRecorder,
        system_backend_factory=SystemOutputLoopbackRecorder,
        countdown_interval=1.0,
    ):
        self.game = game
        self._parent_provider = parent_provider
        self._backend_factory = backend_factory
        self._system_backend_factory = system_backend_factory
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
        self._microphone_overlay = MicrophoneOverlayBuffer()
        self._session_include_microphone = False
        self._session_include_computer_audio = False
        self._session_countdown_seconds = DEFAULT_COUNTDOWN_SECONDS
        self._session_split_minutes = 0
        self._session_announce_details = True

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

    @staticmethod
    def _choice_option(key, choices, default):
        try:
            value = int(options.get(key, default))
        except (TypeError, ValueError):
            value = default
        return value if value in choices else default

    def include_microphone(self):
        return bool(options.get("music_bot_recording_include_microphone", False))

    def include_computer_audio(self):
        return bool(options.get("music_bot_recording_include_computer_audio", False))

    def countdown_seconds(self):
        return self._choice_option(
            "music_bot_recording_countdown_seconds",
            COUNTDOWN_CHOICES,
            DEFAULT_COUNTDOWN_SECONDS,
        )

    def split_minutes(self):
        return self._choice_option(
            "music_bot_recording_split_minutes",
            SPLIT_MINUTE_CHOICES,
            0,
        )

    def announce_details(self):
        return bool(options.get("music_bot_recording_announce_details", True))

    def microphone_setting_label(self):
        state = "On" if self.include_microphone() else "Off"
        return f"Include My Transmitted Voice: {state}"

    def computer_audio_setting_label(self):
        state = "On" if self.include_computer_audio() else "Off"
        return f"Include Screen Reader and Computer Audio: {state}"

    def countdown_setting_label(self):
        seconds = self.countdown_seconds()
        value = "Off" if seconds == 0 else f"{seconds} seconds"
        return f"Recording Countdown: {value}"

    def split_setting_label(self):
        minutes = self.split_minutes()
        value = "Off" if minutes == 0 else f"Every {minutes} minutes"
        return f"Split Long Recordings: {value}"

    def announce_setting_label(self):
        state = "On" if self.announce_details() else "Off"
        return f"Announce Completed Recording Details: {state}"

    def capture_scope_label(self):
        if self.include_computer_audio():
            return "Capture Scope: Default Windows Output, Including Screen Reader"
        return "Capture Scope: All Audio Rendered by Beyond Tournament"

    def speak_capture_scope(self):
        if self.include_computer_audio():
            self._announce(
                "The recording includes every application playing through the default Windows "
                "output device. Beyond Tournament and the screen reader must use that device."
            )
        else:
            self._announce(
                "The recording includes user interface, world, music, instruments and received "
                "Voice Chat rendered by Beyond Tournament. NVDA and other applications are excluded."
            )

    def _can_change_settings(self):
        if self.is_active():
            self._announce("Stop the current recording before changing recording settings.")
            return False
        return True

    def toggle_microphone(self):
        if not self._can_change_settings():
            return
        enabled = not self.include_microphone()
        options.set("music_bot_recording_include_microphone", enabled)
        self._announce(
            "Your transmitted voice will be included."
            if enabled else "Your transmitted voice will not be included."
        )

    def toggle_computer_audio(self):
        if not self._can_change_settings():
            return
        enabled = not self.include_computer_audio()
        options.set("music_bot_recording_include_computer_audio", enabled)
        if enabled:
            self._announce(
                "Screen reader and computer audio on. Every application playing through the "
                "default Windows output can be included. The game and screen reader must use that device."
            )
        else:
            self._announce("Screen reader and computer audio off. Only Beyond Tournament will be captured.")

    def cycle_countdown(self):
        if not self._can_change_settings():
            return
        current = self.countdown_seconds()
        value = COUNTDOWN_CHOICES[(COUNTDOWN_CHOICES.index(current) + 1) % len(COUNTDOWN_CHOICES)]
        options.set("music_bot_recording_countdown_seconds", value)
        self._announce("Recording countdown off." if value == 0 else f"Recording countdown {value} seconds.")

    def cycle_split_minutes(self):
        if not self._can_change_settings():
            return
        current = self.split_minutes()
        value = SPLIT_MINUTE_CHOICES[
            (SPLIT_MINUTE_CHOICES.index(current) + 1) % len(SPLIT_MINUTE_CHOICES)
        ]
        options.set("music_bot_recording_split_minutes", value)
        self._announce(
            "Long recording splitting off."
            if value == 0 else f"Long recordings will split every {value} minutes."
        )

    def toggle_announce_details(self):
        if not self._can_change_settings():
            return
        enabled = not self.announce_details()
        options.set("music_bot_recording_announce_details", enabled)
        self._announce("Completed recording details on." if enabled else "Completed recording details off.")

    def restore_setting_defaults(self):
        if not self._can_change_settings():
            return False
        options.set("music_bot_recording_include_microphone", False)
        options.set("music_bot_recording_include_computer_audio", False)
        options.set("music_bot_recording_countdown_seconds", DEFAULT_COUNTDOWN_SECONDS)
        options.set("music_bot_recording_split_minutes", 0)
        options.set("music_bot_recording_announce_details", True)
        self._announce("Recording settings restored to defaults.")
        return True

    def feed_transmitted_microphone(self, pcm, *, locally_rendered=False):
        """Non-blocking Voice Chat hook for the current PTT microphone PCM."""
        with self._lock:
            accepted = (
                self._state == "recording"
                and self._session_include_microphone
                and not locally_rendered
                and not self._closed
            )
        if accepted:
            self._microphone_overlay.put_mono16(pcm)
        return accepted

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
        include_computer_audio = self.include_computer_audio()
        if include_computer_audio:
            if not system_loopback_supported():
                self._announce("Screen reader and computer audio recording requires Windows.")
                return
        elif not process_loopback_supported():
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
            self._session_include_microphone = self.include_microphone()
            self._session_include_computer_audio = self.include_computer_audio()
            self._session_countdown_seconds = self.countdown_seconds()
            self._session_split_minutes = self.split_minutes()
            self._session_announce_details = self.announce_details()
            self._stop_event.clear()
            self._cancel_countdown.clear()
            self._microphone_overlay.clear()
        self._worker = threading.Thread(target=self._countdown_and_record, daemon=True)
        self._worker.start()
        return True

    def _countdown_and_record(self):
        with self._lock:
            countdown_seconds = self._session_countdown_seconds
        if countdown_seconds:
            self._announce(f"Ready, {countdown_seconds}.")
            for number in range(countdown_seconds - 1, 0, -1):
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
            include_microphone = self._session_include_microphone
            include_computer_audio = self._session_include_computer_audio
            split_minutes = self._session_split_minutes
            announce_details = self._session_announce_details
        try:
            backend_factory = (
                self._system_backend_factory if include_computer_audio else self._backend_factory
            )
            backend = backend_factory(os.getpid())

            def on_started():
                should_announce = False
                with self._lock:
                    if self._state == "countdown":
                        self._state = "recording"
                        self._recording_started_at = time.monotonic()
                        should_announce = True
                if should_announce:
                    self._announce("Recording.")

            result = backend.record(
                output_path,
                self._stop_event,
                on_started,
                microphone_overlay=self._microphone_overlay if include_microphone else None,
                split_minutes=split_minutes,
            )
            frames, elapsed = result[:2]
            output_paths = tuple(result[2]) if len(result) > 2 else (output_path,)
            if frames <= 0:
                for path in output_paths:
                    try:
                        if os.path.exists(path):
                            os.remove(path)
                    except OSError:
                        pass
                self._announce("No game audio was captured. The empty recording was removed.")
            elif not announce_details:
                self._announce("Recording saved.")
            elif len(output_paths) > 1:
                self._announce(
                    f"Recording saved in {len(output_paths)} files. {elapsed:.0f} seconds. "
                    f"First file: {os.path.basename(output_paths[0])}"
                )
            else:
                self._announce(
                    f"Recording saved. {elapsed:.0f} seconds. {os.path.basename(output_paths[0])}"
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
            self._session_include_microphone = False
            self._session_include_computer_audio = False
            self._stop_event.clear()
            self._cancel_countdown.clear()
            self._microphone_overlay.clear()

    def close(self):
        self._closed = True
        self._cancel_countdown.set()
        self._stop_event.set()
        self._microphone_overlay.clear()
