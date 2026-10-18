"""Measure what Windows shows when a program captures the PC's own output.

The question this answers, before any code in the client trusts an answer:

    When a program opens a WASAPI *loopback* capture on the default playback
    (render) endpoint -- what OBS "Desktop Audio" and Audacity's Windows WASAPI
    loopback do -- does that program appear as an audio session on that render
    endpoint, and can its render-endpoint session be told apart from a program
    that is simply *playing* audio?

Microsoft documents that a session containing loopback streams is a capture
session, and that `IAudioSessionEnumerator` lists the sessions on a device, but
documents no direction flag on `IAudioSessionControl`/`IAudioSessionControl2`
(`GetProcessId`, `GetSessionIdentifier`, `GetSessionInstanceIdentifier`,
`IsSystemSoundsSession`, `GetState`).  So the distinction has to be measured,
not assumed.  This tool measures it and prints the verdict.

Run from `client/`:

    python tools/loopback_capture_probe.py            # 6 seconds
    python tools/loopback_capture_probe.py --seconds 10

It plays nothing, opens a real loopback capture on the default render device,
and while the capture is running lists every render endpoint's sessions with
the owning process name.  It reports:

    LOOPS  the loopback client's own pid was seen as an ACTIVE render session
    SAME   ... and that is the only thing that told it apart from playback
    NONE   ... it was not seen at all

Then it closes the capture and lists the same endpoints again, so the delta is
the loopback stream and nothing else.  Nothing here is imported by the game.
"""

from __future__ import annotations

import argparse
import ctypes
import os
import sys
import threading
import time
from ctypes import POINTER, byref, c_int, c_longlong, c_ulong, c_void_p, c_wchar

if os.name != "nt":  # pragma: no cover - the signal is Windows-only
    print("This probe only means anything on Windows.")
    raise SystemExit(0)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import comtypes  # noqa: E402
from comtypes import CLSCTX_ALL, COMMETHOD, GUID, HRESULT, IUnknown  # noqa: E402

from libs import recording_watch as watch  # noqa: E402

AUDCLNT_SHAREMODE_SHARED = 0
AUDCLNT_STREAMFLAGS_LOOPBACK = 0x00020000
AUDCLNT_STREAMFLAGS_EVENTCALLBACK = 0x00040000
WAVE_FORMAT_PCM = 1
WAVE_FORMAT_EXTENSIBLE = 0xFFFE


class WAVEFORMATEX(ctypes.Structure):
    _fields_ = [
        ("wFormatTag", ctypes.c_ushort),
        ("nChannels", ctypes.c_ushort),
        ("nSamplesPerSec", ctypes.c_ulong),
        ("nAvgBytesPerSec", ctypes.c_ulong),
        ("nBlockAlign", ctypes.c_ushort),
        ("wBitsPerSample", ctypes.c_ushort),
        ("cbSize", ctypes.c_ushort),
    ]


class IAudioClient(IUnknown):
    _iid_ = GUID("{1CB9AD4C-DBFA-4c32-B178-C2F568A703B2}")
    _methods_ = [
        COMMETHOD(
            [], HRESULT, "Initialize",
            (["in"], c_int, "ShareMode"),
            (["in"], c_ulong, "StreamFlags"),
            (["in"], c_longlong, "hnsBufferDuration"),
            (["in"], c_longlong, "hnsPeriodicity"),
            (["in"], POINTER(WAVEFORMATEX), "pFormat"),
            (["in"], POINTER(GUID), "AudioSessionGuid"),
        ),
        COMMETHOD([], HRESULT, "GetBufferSize", (["out"], POINTER(c_ulong), "p")),
        COMMETHOD([], HRESULT, "GetStreamLatency", (["out"], POINTER(c_longlong), "p")),
        COMMETHOD([], HRESULT, "GetCurrentPadding", (["out"], POINTER(c_ulong), "p")),
        COMMETHOD(
            [], HRESULT, "IsFormatSupported",
            (["in"], c_int, "ShareMode"),
            (["in"], POINTER(WAVEFORMATEX), "pFormat"),
            (["out"], POINTER(POINTER(WAVEFORMATEX)), "ppClosestMatch"),
        ),
        COMMETHOD([], HRESULT, "GetMixFormat", (["out"], POINTER(POINTER(WAVEFORMATEX)), "ppFormat")),
        COMMETHOD(
            [], HRESULT, "GetDevicePeriod",
            (["out"], POINTER(c_longlong), "pDefault"),
            (["out"], POINTER(c_longlong), "pMinimum"),
        ),
        COMMETHOD([], HRESULT, "Start"),
        COMMETHOD([], HRESULT, "Stop"),
        COMMETHOD([], HRESULT, "Reset"),
        COMMETHOD([], HRESULT, "SetEventHandle", (["in"], c_void_p, "h")),
        COMMETHOD(
            [], HRESULT, "GetService",
            (["in"], POINTER(GUID), "riid"),
            (["out"], POINTER(POINTER(IUnknown)), "ppv"),
        ),
    ]


IAudioCaptureClient_IID = GUID("{C8ADBD64-E71E-48a0-A4DE-185C395CD317}")
KSDATAFORMAT_SUBTYPE_PCM = GUID("{00000001-0000-0010-8000-00aa00389b71}")


class IAudioCaptureClient(IUnknown):
    _iid_ = IAudioCaptureClient_IID
    _methods_ = [
        COMMETHOD(
            [], HRESULT, "GetBuffer",
            (["out"], POINTER(c_void_p), "ppData"),
            (["out"], POINTER(c_ulong), "pNumFramesToRead"),
            (["out"], POINTER(c_ulong), "pdwFlags"),
            (["out"], POINTER(c_longlong), "pu64DevicePosition"),
            (["out"], POINTER(c_longlong), "pu64QPCPosition"),
        ),
        COMMETHOD([], HRESULT, "ReleaseBuffer", (["in"], c_ulong, "NumFramesRead")),
        COMMETHOD([], HRESULT, "GetNextPacketSize", (["out"], POINTER(c_ulong), "p")),
    ]


def default_render_device():
    enumerator = comtypes.CoCreateInstance(
        watch._CLSID_MMDEVICE_ENUMERATOR,
        interface=watch._IMMDeviceEnumerator,
        clsctx=CLSCTX_ALL,
    )
    # eRender = 0, eConsole = 0: the device the game would actually play to.
    return enumerator.GetDefaultAudioEndpoint(0, 0)


def endpoint_name(endpoint) -> str:
    """The endpoint's friendly name, read the way the watchdog reads it."""
    try:
        identifier = ctypes.wstring_at(endpoint.GetId())
    except Exception:
        return ""
    return watch.endpoint_friendly_name(identifier) or identifier


def render_sessions() -> list[tuple[str, list[tuple[int, int]]]]:
    """Every active render endpoint and the (pid, session state) on it."""
    enumerator = comtypes.CoCreateInstance(
        watch._CLSID_MMDEVICE_ENUMERATOR,
        interface=watch._IMMDeviceEnumerator,
        clsctx=CLSCTX_ALL,
    )
    devices = enumerator.EnumAudioEndpoints(watch.E_RENDER, watch.DEVICE_STATE_ACTIVE)
    result = []
    for index in range(int(devices.GetCount())):
        device = devices.Item(index)
        manager = device.Activate(byref(watch._IAudioSessionManager2._iid_), CLSCTX_ALL, None)
        manager = manager.QueryInterface(watch._IAudioSessionManager2)
        sessions = manager.GetSessionEnumerator()
        rows = []
        for session_index in range(int(sessions.GetCount())):
            session = sessions.GetSession(session_index)
            try:
                pid = int(session.GetProcessId())
            except Exception:
                pid = -1
            rows.append((pid, int(session.GetState())))
        result.append((endpoint_name(device), rows))
    return result


def session_pids(rows) -> list[tuple[int, str]]:
    pids = []
    for name, sessions in rows:
        for pid, state in sessions:
            pids.append((pid, f"state={state} on {name}"))
    return pids


def format_own_session(rows, pid: int) -> str:
    for name, sessions in rows:
        for session_pid, state in sessions:
            if session_pid == pid:
                return f"pid {pid} state={state} on {name}"
    return f"pid {pid} not listed"


def capture_in_thread(seconds: float, errors: list[str], ready: threading.Event) -> None:
    """Open a real WASAPI loopback capture and hold it for `seconds`."""
    try:
        try:
            comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
        except OSError:
            pass
        device = default_render_device()
        activated = device.Activate(byref(IAudioClient._iid_), CLSCTX_ALL, None)
        client = activated.QueryInterface(IAudioClient)
        wave_format = client.GetMixFormat()
        if not wave_format:
            errors.append("the render device reported no mix format")
            ready.set()
            return
        format_pointer = ctypes.cast(wave_format, POINTER(WAVEFORMATEX))
        client.Initialize(
            AUDCLNT_SHAREMODE_SHARED,
            AUDCLNT_STREAMFLAGS_LOOPBACK,
            10_000_000,  # 1 s shared-mode buffer
            0,
            format_pointer,
            None,
        )
        capture = client.GetService(byref(IAudioCaptureClient_IID))
        capture = capture.QueryInterface(IAudioCaptureClient)
        client.Start()
        ready.set()
        frames = 0
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            packet = int(capture.GetNextPacketSize())
            if packet <= 0:
                time.sleep(0.01)
                continue
            data, available, _flags, _pos, _qpc = capture.GetBuffer()
            capture.ReleaseBuffer(available)
            frames += int(available)
        client.Stop()
        print(f"loopback capture: read {frames} frames of the PC's output")
    except Exception as error:  # a COM refusal must not look like a verdict
        errors.append(f"{type(error).__name__}: {error}")
        ready.set()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds", type=float, default=6.0)
    arguments = parser.parse_args()

    try:
        comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
    except OSError:
        pass

    print(f"render endpoints before capture ({len(render_sessions())}):")
    before = session_pids(render_sessions())
    for pid, where in before:
        print(f"   {pid:>6}  {where}")
    pid = os.getpid()
    print(f"this probe is pid {pid}: {format_own_session(render_sessions(), pid)}")

    errors: list[str] = []
    ready = threading.Event()
    thread = threading.Thread(target=capture_in_thread, args=(arguments.seconds, errors, ready), daemon=True)
    thread.start()
    if not ready.wait(10):
        errors.append("the capture thread did not start")
    time.sleep(0.4)

    during = render_sessions()
    print("render endpoints during capture:")
    for pid_seen, where in session_pids(during):
        print(f"   {pid_seen:>6}  {where}")
    print(f"this probe: {format_own_session(during, pid)}")
    thread.join(arguments.seconds + 5)

    after = session_pids(render_sessions())
    print(f"render sessions after capture: {len(after)} (before: {len(before)})")
    if errors:
        for error in errors:
            print(f"CAPTURE FAILED  {error}")
        print("VERDICT: no measurement -- the loopback capture itself failed.")
        return 2

    during_pids = {seen for seen, _ in session_pids(during)}
    before_pids = {seen for seen, _ in before}
    if pid not in during_pids:
        print("VERDICT NONE: a WASAPI loopback client is NOT listed as a render session.")
        print("A render-endpoint sweep cannot see it, so it must not be used as a signal.")
        return 0
    if pid in before_pids:
        print("VERDICT PLAYBACK-ONLY: the probe was already listed before capturing,")
        print("so render-endpoint presence says nothing about capture. Not a signal.")
        return 0
    print("VERDICT LOOPS: a WASAPI loopback client IS listed as a render session.")
    print("But the same listing is what a program that merely plays audio looks like,")
    print("and no documented session method reports stream direction: a render sweep")
    print("would fire for every player with music playing. Still not a signal on its own.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
