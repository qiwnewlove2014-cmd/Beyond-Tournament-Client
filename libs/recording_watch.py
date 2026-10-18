"""Report audio-recording software running beside the game, quietly, to staff.

Recording software is not cheating: a player recording their own match for a
video, or streaming it, is doing something entirely legitimate, and this game
ships its own recorder (`libs/game_audio_recorder.py`).  So nothing here knocks
on a recording, closes the client or punishes anybody -- it only tells staff
which recording tools are running, the same way the anti-cheat watchdog tells
them a cheat was opened.  That distinction is the whole design: an advisory is
allowed to be a little noisy, a punishment is not.

Five signals, cheapest first:

* a known recorder by process name (OBS, Audacity, REAPER ...), which catches
  desktop-audio recording that never touches the microphone;
* a visible window whose title carries a recorder's name, which catches a
  program somebody renamed (`obs64.exe` copied to `mine.exe`);
* Windows' audio-session list on every capture endpoint, which names *every*
  program capturing the microphone right now, whatever it is called.  This is
  the general net and the closest thing to asking the audio engine itself.
* a virtual audio cable or mixer among the machine's audio devices, which is
  how the game's sound is routed to a recorder running on another machine or in
  a sandbox that the process list never shows.
* a capture session on a *loopback* capture endpoint -- "Stereo Mix", "What U
  Hear", a virtual cable's output -- which is the game's own sound being
  recorded rather than the room.  The friendly name of the endpoint is what
  makes the difference, and it is resolved per device, not per process.

The obvious fourth answer, reading sessions off the *render* endpoints, was
measured and rejected: `tools/loopback_capture_probe.py` opens a real WASAPI
loopback capture and prints what Windows then shows.  A render endpoint lists
the programs capturing its output and the programs playing to it side by side,
and no documented session method reports stream direction, so a sweep there
would name every player with music playing.  Do not re-introduce it without a
new measurement.

The third one is the deep one, and it is why the registry was not used: the
`ConsentStore\\microphone` keys look like a live indicator (`LastUsedTimeStop`
is 0 while in use), but on Windows 11 26200 a recording that was actually
running left the store untouched -- it is only written when a session *ends*
(verified with a real OpenAL capture, the game's own voice-chat path).  The
audio-session enumeration below answers the actual question instead: which
process owns a live capture session on a microphone endpoint.  This process is
filtered out by PID, because the game captures the microphone for voice chat
and for line-in instruments.
"""

from __future__ import annotations

import ntpath
import os
import re
import sys
import time
from typing import Callable, Iterable, NamedTuple, Sequence


class RecordingFinding(NamedTuple):
    """One reason to believe a recording program is running."""

    app: str
    #: "process" (a known tool is running), "window" (a recorder's title is on
    #: screen), "microphone" (this program is capturing the microphone now) or
    #: "device" (a virtual audio cable or mixer is installed).
    source: str
    detail: str

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.source, self.app, self.detail)


# Executable names, lower case with the extension, as Windows reports them in
# psutil.  Deliberately conservative: a false entry here reports a player to
# staff for opening something that does not record at all, and a missed entry
# only costs the audio-session net, which cannot miss a program capturing the
# microphone by name.
KNOWN_RECORDING_PROCESSES: dict[str, str] = {
    "obs.exe": "OBS Studio",
    "obs32.exe": "OBS Studio",
    "obs64.exe": "OBS Studio",
    "streamlabs obs.exe": "Streamlabs Desktop",
    "streamlabs desktop.exe": "Streamlabs Desktop",
    "audacity.exe": "Audacity",
    "reaper.exe": "REAPER",
    "audition.exe": "Adobe Audition",
    "adobe audition.exe": "Adobe Audition",
    "bdcam.exe": "Bandicam",
    "bandicam.exe": "Bandicam",
    "camtasia.exe": "Camtasia",
    "camtasiastudio.exe": "Camtasia",
    "camtasiarecorder.exe": "Camtasia",
    "fraps.exe": "Fraps",
    "dxtory.exe": "Dxtory",
    "sharex.exe": "ShareX",
    "xsplit.core.exe": "XSplit",
    "xsplitbroadcaster.exe": "XSplit",
}

# Window titles for the renamed-executable case.  Every keyword is matched as a
# whole word so "Jobs - Google Chrome" is not OBS, and the bare three-letter
# spelling is left out on purpose: a browser tab mentioning OBS would match it.
# OBS's own title is caught by the shaped pattern below instead ("OBS 30.2.4 -
# Profile: ..."), which a search-result tab does not match.  A title is only
# ever an advisory, and the audio-session net catches the tools whose window
# this misses.
WINDOW_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"^obs\s+\d+(\.\d+)*", "OBS Studio"),
)

WINDOW_KEYWORDS: tuple[tuple[str, str], ...] = (
    ("obs studio", "OBS Studio"),
    ("streamlabs", "Streamlabs Desktop"),
    ("audacity", "Audacity"),
    ("adobe audition", "Adobe Audition"),
    ("reaper", "REAPER"),
    ("bandicam", "Bandicam"),
    ("camtasia", "Camtasia"),
    ("dxtory", "Dxtory"),
    ("fraps", "Fraps"),
    ("sharex", "ShareX"),
    ("xsplit", "XSplit"),
)

# Device-level signal.  A virtual audio cable or mixer is how somebody routes the
# game's sound to a recorder that never touches the microphone, so the routing
# itself is worth seeing.  It is a fact about the machine and not an accusation
# -- streamers and musicians use these devices for perfectly ordinary things --
# which is why it is reported once per session like everything else here, and
# named as a device rather than as a recording.
#: Numeric data flows from mmdeviceapi.h, kept named here because the session
#: code needs both: a capture endpoint is where the microphone net looks for a
#: capturing process, a render endpoint is what a loopback capture records.
E_CAPTURE = 1
E_RENDER = 0

#: Capture endpoints that carry the PC's own output instead of a room: the
#: capture half of a loopback route.  A session on one of these is somebody
#: recording what the machine is playing -- the match, in this game -- so it is
#: a different, more specific fact than "is capturing the microphone".
LOOPBACK_CAPTURE_DEVICES: tuple[tuple[str, str], ...] = (
    ("stereo mix", "Stereo Mix"),
    ("what u hear", "Stereo Mix"),
    ("what you hear", "Stereo Mix"),
    ("wave out mix", "Stereo Mix"),
    ("stereomix", "Stereo Mix"),
    ("cable output", "virtual cable output"),
    ("voicemeeter out", "VoiceMeeter output"),
    ("voicemeeter aux", "VoiceMeeter output"),
    ("loopback", "loopback device"),
)

#: Endpoint ids are stable while a device stays plugged in, and the friendly
#: name is a registry read, so it is cached.  Bounded: a machine that churns USB
#: audio devices must not grow this dict for the life of the client.
MAX_CACHED_ENDPOINT_NAMES = 64
_endpoint_name_cache: dict[str, str] = {}

#: Longest match first: every one of these vendors puts "VB-Audio" in the name
#: of its mixer as well as its cable, so the specific family is tested before
#: the generic word, or VoiceMeeter would be reported as a plain cable.
VIRTUAL_AUDIO_DEVICES: tuple[tuple[str, str], ...] = (
    ("voicemeeter", "VoiceMeeter"),
    ("hi-fi cable", "VB-Audio Hi-Fi Cable"),
    ("vb-audio", "VB-Audio virtual cable"),
    ("vbcable", "VB-Audio virtual cable"),
    ("cable input", "VB-Audio virtual cable"),
    ("cable output", "VB-Audio virtual cable"),
    ("virtual audio cable", "Virtual Audio Cable"),
    ("virtual cable", "Virtual Audio Cable"),
    ("voicemod", "Voicemod virtual device"),
)

#: Device names change only when somebody plugs something in; a minute of cache
#: keeps the OpenAL device query off the watchdog's five-second path.
VIRTUAL_DEVICE_CACHE_S = 60.0
_virtual_device_cache: dict[str, object] = {"at": float("-inf"), "names": ()}

# Windows' own audio engine, service host and system-sounds session appear in the
# session list without a player having opened anything; they are the system, not
# a recorder.
SYSTEM_AUDIO_PROCESSES = ("audiodg.exe", "svchost.exe", "backgroundtaskhost.exe")

# Holding the microphone is not the same as recording: a player talking in
# Discord, TeamTalk or a browser call is doing what those programs are for, and
# naming them would bury the advisory that matters.  This list is the noise
# floor, not a recorder list: a program that is not here and is capturing the
# microphone is worth a line, whatever it is called.
MICROPHONE_NOISE_PROCESSES = (
    "discord.exe",
    "discordptb.exe",
    "discordcanary.exe",
    "teamtalk5.exe",
    "teamtalk.exe",
    "zoom.exe",
    "teams.exe",
    "ms-teams.exe",
    "skype.exe",
    "skypeapp.exe",
    "slack.exe",
    "telegram.exe",
    "whatsapp.exe",
    "signal.exe",
    "line.exe",
    "chrome.exe",
    "msedge.exe",
    "firefox.exe",
    "brave.exe",
    "opera.exe",
    "vivaldi.exe",
    "steam.exe",
    "steamwebhelper.exe",
)

MAX_FINDINGS_PER_SCAN = 8

#: Whether the audio-engine net works on this machine.  None until the first
#: attempt.  A client that cannot enumerate capture sessions still reports the
#: two name-based signals, so the Server is told once per session -- otherwise
#: silence on that machine reads exactly like "nobody is recording".
_capture_signal: dict[str, bool | None] = {"available": None}

# Constants from mmdeviceapi.h / audiopolicy.h.
DEVICE_STATE_ACTIVE = 0x1
CLSCTX_ALL = 23
SESSION_STATE_ACTIVE = 1


def _basename(name: str) -> str:
    return ntpath.basename((name or "").replace("/", "\\")).strip().lower()


def classify_recording_process(process_name: str | None) -> str | None:
    """Return the recorder's friendly name for a process name, or None.

    Accepts a bare name or a full path, in any case, with or without ".exe".
    """
    name = _basename(process_name or "")
    if not name:
        return None
    app = KNOWN_RECORDING_PROCESSES.get(name)
    if app is None and not name.endswith(".exe"):
        app = KNOWN_RECORDING_PROCESSES.get(name + ".exe")
    return app


def classify_recording_window(title: str | None) -> str | None:
    """Return the recorder's friendly name for a window title, or None."""
    text = (title or "").strip().lower()
    if not text:
        return None
    for pattern, app in WINDOW_PATTERNS:
        if re.match(pattern, text):
            return app
    for keyword, app in WINDOW_KEYWORDS:
        if re.search(r"(?<![a-z0-9])" + re.escape(keyword) + r"(?![a-z0-9])", text):
            return app
    return None


def own_process_names() -> tuple[str, ...]:
    """Executable names that are this game, which must never be reported.

    The game captures the microphone for voice chat and for line-in
    instruments, so without this every player in voice chat would be reported
    as a recorder.  Running from source the executable is `python.exe`, which is
    also what a Python recorder would be called -- that trade is deliberate:
    never accuse the game itself.
    """
    names = []
    executable = os.path.basename(sys.executable or "")
    if executable:
        names.append(executable)
    if getattr(sys, "frozen", False):
        names.append(os.path.basename(sys.executable))
    return tuple(dict.fromkeys(names))


def running_process_names() -> tuple[str, ...]:
    """Every running process's executable name, lower case; empty elsewhere."""
    if os.name != "nt":
        return ()
    psutil = _psutil()
    if psutil is None:  # pragma: no cover - psutil ships with the client
        return ()
    names: set[str] = set()
    for proc in psutil.process_iter(["name"]):
        try:
            name = proc.info.get("name")
        except Exception:
            # A process that dies mid-scan must never take the watchdog down.
            continue
        if isinstance(name, str) and name.strip():
            names.add(name.strip().lower())
    return tuple(sorted(names))


def _psutil():
    try:
        import psutil
    except ImportError:  # pragma: no cover - psutil ships with the client
        return None
    return psutil


if os.name == "nt":
    # The same COM interfaces the game's own recorder activates
    # (libs/game_audio_recorder.py), extended to the session manager so this can
    # ask the audio engine which processes hold a capture stream.  Only the
    # methods actually called are declared, and only the ones before them in
    # each vtable, because their order *is* the vtable.
    try:
        import ctypes
        import comtypes
        from ctypes import POINTER, byref, c_int, c_ulong, c_void_p
        from comtypes import COMMETHOD, GUID, HRESULT, IUnknown

        class _IMMDevice(IUnknown):
            _iid_ = GUID("{D666063F-1587-4E43-81F1-B948E807363F}")
            _methods_ = [
                COMMETHOD(
                    [], HRESULT, "Activate",
                    (["in"], POINTER(GUID), "iid"),
                    (["in"], c_ulong, "dwClsCtx"),
                    (["in"], c_void_p, "pActivationParams"),
                    (["out"], POINTER(POINTER(IUnknown)), "ppInterface"),
                ),
                COMMETHOD([], HRESULT, "OpenPropertyStore", (["in"], c_ulong, "stgmAccess"),
                          (["out"], POINTER(c_void_p), "ppProperties")),
                COMMETHOD([], HRESULT, "GetId", (["out"], POINTER(c_void_p), "ppstrId")),
                COMMETHOD([], HRESULT, "GetState", (["out"], POINTER(c_ulong), "pdwState")),
            ]

        class _IMMDeviceCollection(IUnknown):
            _iid_ = GUID("{0BD7A1BE-7A1A-44DB-8397-CC5392387B5E}")
            _methods_ = [
                COMMETHOD([], HRESULT, "GetCount", (["out"], POINTER(c_ulong), "pcDevices")),
                COMMETHOD(
                    [], HRESULT, "Item",
                    (["in"], c_ulong, "nDevice"),
                    (["out"], POINTER(POINTER(_IMMDevice)), "ppDevice"),
                ),
            ]

        class _IMMDeviceEnumerator(IUnknown):
            _iid_ = GUID("{A95664D2-9614-4F35-A746-DE8DB63617E6}")
            _methods_ = [
                COMMETHOD(
                    [], HRESULT, "EnumAudioEndpoints",
                    (["in"], c_int, "dataFlow"),
                    (["in"], c_ulong, "stateMask"),
                    (["out"], POINTER(POINTER(_IMMDeviceCollection)), "ppDevices"),
                ),
                COMMETHOD(
                    [], HRESULT, "GetDefaultAudioEndpoint",
                    (["in"], c_int, "dataFlow"),
                    (["in"], c_int, "role"),
                    (["out"], POINTER(POINTER(_IMMDevice)), "ppEndpoint"),
                ),
            ]

        class _IAudioSessionControl2(IUnknown):
            _iid_ = GUID("{BFB7FF88-7239-4FC9-8FA2-07C950BE9C6D}")
            _methods_ = [
                COMMETHOD([], HRESULT, "GetState", (["out"], POINTER(c_int), "pRetVal")),
                COMMETHOD([], HRESULT, "GetDisplayName", (["out"], POINTER(c_void_p), "pRetVal")),
                COMMETHOD([], HRESULT, "SetDisplayName", (["in"], c_void_p, "pRetVal"),
                          (["in"], POINTER(GUID), "pEventContext")),
                COMMETHOD([], HRESULT, "GetIconPath", (["out"], POINTER(c_void_p), "pRetVal")),
                COMMETHOD([], HRESULT, "SetIconPath", (["in"], c_void_p, "pRetVal"),
                          (["in"], POINTER(GUID), "pEventContext")),
                COMMETHOD([], HRESULT, "GetGroupingParam", (["out"], POINTER(GUID), "pRetVal")),
                COMMETHOD([], HRESULT, "SetGroupingParam", (["in"], POINTER(GUID), "pRetVal"),
                          (["in"], POINTER(GUID), "pEventContext")),
                COMMETHOD([], HRESULT, "RegisterAudioSessionNotification", (["in"], c_void_p, "pNewNotifications")),
                COMMETHOD([], HRESULT, "UnregisterAudioSessionNotification", (["in"], c_void_p, "pNewNotifications")),
                COMMETHOD([], HRESULT, "GetSessionIdentifier", (["out"], POINTER(c_void_p), "pRetVal")),
                COMMETHOD([], HRESULT, "GetSessionInstanceIdentifier", (["out"], POINTER(c_void_p), "pRetVal")),
                COMMETHOD([], HRESULT, "GetProcessId", (["out"], POINTER(c_ulong), "pRetVal")),
                COMMETHOD([], HRESULT, "IsSystemSoundsSession"),
            ]

        class _IAudioSessionEnumerator(IUnknown):
            _iid_ = GUID("{E2F5BB11-0570-40CA-ACDD-3AA01277DEE8}")
            _methods_ = [
                COMMETHOD([], HRESULT, "GetCount", (["out"], POINTER(c_int), "SessionCount")),
                COMMETHOD(
                    [], HRESULT, "GetSession",
                    (["in"], c_int, "SessionIndex"),
                    (["out"], POINTER(POINTER(_IAudioSessionControl2)), "Session"),
                ),
            ]

        class _IAudioSessionManager2(IUnknown):
            _iid_ = GUID("{77AA99A0-1BD6-484F-8BC7-2C654C9A9B6F}")
            _methods_ = [
                COMMETHOD(
                    [], HRESULT, "GetAudioSessionControl",
                    (["in"], POINTER(GUID), "AudioSessionGuid"),
                    (["in"], c_ulong, "StreamFlags"),
                    (["out"], POINTER(c_void_p), "SessionControl"),
                ),
                COMMETHOD(
                    [], HRESULT, "GetSimpleAudioVolume",
                    (["in"], POINTER(GUID), "AudioSessionGuid"),
                    (["in"], c_ulong, "StreamFlags"),
                    (["out"], POINTER(c_void_p), "AudioVolume"),
                ),
                COMMETHOD(
                    [], HRESULT, "GetSessionEnumerator",
                    (["out"], POINTER(POINTER(_IAudioSessionEnumerator)), "SessionEnum"),
                ),
            ]

        _CLSID_MMDEVICE_ENUMERATOR = GUID("{BCDE0395-E52F-467C-8E3D-C4579291692E}")
        _COMTYPES_OK = True
    except ImportError:  # pragma: no cover - comtypes ships with the client
        _COMTYPES_OK = False
else:  # pragma: no cover - every non-Windows platform takes this branch
    _COMTYPES_OK = False


def classify_virtual_audio_device(name: str | None) -> str | None:
    """Return the virtual device family a device name belongs to, or None."""
    text = (name or "").strip().lower()
    if not text:
        return None
    for marker, app in VIRTUAL_AUDIO_DEVICES:
        if marker in text:
            return app
    return None


def device_label(name: str) -> str:
    """The device's own name, without OpenAL's "OpenAL Soft on" prefix."""
    return re.sub(r"^openal\s+soft\s+on\s+", "", (name or "").strip(), flags=re.IGNORECASE)


def classify_loopback_capture_device(name: str | None) -> str | None:
    """Which loopback route a capture endpoint is, or None for a plain input.

    Only the endpoint's own name is consulted, so a microphone is never a
    loopback because of what happens to be plugged into it.
    """
    text = (name or "").strip().lower()
    if not text:
        return None
    for marker, family in LOOPBACK_CAPTURE_DEVICES:
        if marker in text:
            return family
    return None


def endpoint_friendly_name(identifier: str) -> str:
    """The endpoint's friendly name from its Windows endpoint id, or "".

    The documented route is `IMMDevice::OpenPropertyStore` +
    `IPropertyStore::GetValue(PKEY_Device_FriendlyName)`, which needs a
    PROPVARIANT that comtypes does not provide; Windows keeps the same value in
    the registry under `MMDevices\\Audio\\<flow>\\<guid>\\Properties`, so this
    reads that copy.  Read-only, cached, and "" rather than an exception when
    the key is missing or denied -- the caller falls back to the endpoint id and
    still has a stable device to name.
    """
    identifier = (identifier or "").strip()
    if not identifier or os.name != "nt":
        return ""
    cached = _endpoint_name_cache.get(identifier)
    if cached is not None:
        return cached
    name = ""
    try:
        import winreg

        guid = identifier.split("}.")[-1].strip("{}")
        base = r"SOFTWARE\Microsoft\Windows\CurrentVersion\MMDevices\Audio"
        for flow in ("Capture", "Render"):
            try:
                with winreg.OpenKey(
                    winreg.HKEY_LOCAL_MACHINE,
                    base + "\\" + flow + "\\{" + guid + "}\\Properties",
                ) as key:
                    name = str(winreg.QueryValueEx(key, "{a45c254e-df1c-4efd-8020-67d146a850e0},2")[0])
                break
            except OSError:
                continue
    except Exception:
        name = ""
    if len(_endpoint_name_cache) < MAX_CACHED_ENDPOINT_NAMES:
        _endpoint_name_cache[identifier] = name
    return name


def audio_device_names(now: float | None = None) -> tuple[str, ...]:
    """Every playback and capture device name OpenAL can see, cached a minute.

    Empty when OpenAL cannot be asked at all, which only costs this one signal:
    the process, window and capture-session nets are unaffected.
    """
    if os.name != "nt":
        return ()
    moment = time.monotonic() if now is None else now
    cached_at = _virtual_device_cache["at"]
    if isinstance(cached_at, float) and moment - cached_at < VIRTUAL_DEVICE_CACHE_S:
        return tuple(_virtual_device_cache["names"])  # type: ignore[arg-type]
    names: list[str] = []
    try:
        import cyal
    except ImportError:  # pragma: no cover - cyal ships with the client
        return ()
    try:
        names.extend(str(name) for name in cyal.get_all_device_specifiers())
    except Exception:
        pass
    try:
        names.extend(str(name) for name in cyal.CaptureExtension().devices)
    except Exception:
        pass
    found = tuple(dict.fromkeys(name for name in names if name))
    _virtual_device_cache["at"] = moment
    _virtual_device_cache["names"] = found
    return found


def capture_signal_supported_here() -> bool:
    """Windows with the COM pieces present, i.e. a failure there means something."""
    return os.name == "nt" and _COMTYPES_OK


def capture_signal_available() -> bool | None:
    """True/False once the audio-engine net has been tried, None before that."""
    return _capture_signal["available"]


def windows_version_label() -> str:
    """A short "Windows 10.0 build 19044" for the degraded notice."""
    if os.name != "nt":
        return ""
    try:
        version = sys.getwindowsversion()
    except AttributeError:  # pragma: no cover - non-Windows never gets here
        return ""
    return f"Windows {version.major}.{version.minor} build {version.build}"


def capture_sessions() -> list[tuple[int, str]]:
    """(process id, endpoint friendly name) for every live capture session.

    Only *active* sessions count: a recorder that opened the microphone and
    paused is not capturing audio.  The endpoint name travels with the pid
    because the two nets need it: a session on a loopback endpoint is the PC's
    own output being recorded, and a session on anything else is the room, and
    the same process can hold both.  Empty on any other platform, or when COM
    is unavailable -- this is a net, not a requirement for the other signals.
    """
    if os.name != "nt" or not _COMTYPES_OK:
        return []
    found: list[tuple[int, str]] = []
    seen: set[tuple[int, str]] = set()
    initialized = False
    devices = None
    enumerator = None
    activated = sessions = manager = device = session = None
    try:
        try:
            comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
            initialized = True
        except OSError:
            # This thread already has an apartment (the game's recorder or
            # comtypes took one first); COM calls still work, only the matching
            # CoUninitialize is ours to skip.
            initialized = False
        enumerator = comtypes.CoCreateInstance(
            _CLSID_MMDEVICE_ENUMERATOR,
            interface=_IMMDeviceEnumerator,
            clsctx=CLSCTX_ALL,
        )
        devices = enumerator.EnumAudioEndpoints(E_CAPTURE, DEVICE_STATE_ACTIVE)
        # Reaching this line is the whole question: the audio-engine net works
        # here, even when it finds no sessions at all.
        _capture_signal["available"] = True
        for device_index in range(int(devices.GetCount())):
            try:
                device = devices.Item(device_index)
                activated = device.Activate(byref(_IAudioSessionManager2._iid_), CLSCTX_ALL, None)
                manager = activated.QueryInterface(_IAudioSessionManager2)
                sessions = manager.GetSessionEnumerator()
            except Exception:
                # A device that vanished mid-scan must not stop the others.
                continue
            # One registry read per device, then every session on it is known.
            try:
                identifier = ctypes.wstring_at(device.GetId())
            except Exception:
                identifier = ""
            device_name = endpoint_friendly_name(identifier) or identifier
            for session_index in range(int(sessions.GetCount())):
                try:
                    session = sessions.GetSession(session_index)
                    if int(session.GetState()) != SESSION_STATE_ACTIVE:
                        continue
                    pid = int(session.GetProcessId())
                except Exception:
                    continue
                if pid > 0 and (pid, device_name) not in seen:
                    seen.add((pid, device_name))
                    found.append((pid, device_name))
    except Exception:
        # No capture endpoint, COM refused, Windows changed: report nothing
        # rather than losing the watchdog thread, and remember that this client
        # is down to the name-based signals so Developers can be told.
        _capture_signal["available"] = False
        return []
    finally:
        # Release every interface while this thread's COM apartment is still
        # initialized (a Release after CoUninitialize is nondeterministic).
        activated = sessions = manager = device = session = devices = enumerator = None
        if initialized:
            comtypes.CoUninitialize()
    return found


def _process_name_for_pid(pid: int) -> str:
    psutil = _psutil()
    if psutil is None:
        return ""
    try:
        return psutil.Process(pid).name() or ""
    except Exception:
        return ""


def capture_app_names(
    pids: Iterable[int],
    own_pids: Sequence[int] | None = None,
    resolve: Callable[[int], str] | None = None,
) -> list[str]:
    """Turn capture-session PIDs into unique program names, own process aside."""
    own = {os.getpid()} if own_pids is None else set(own_pids)
    resolve = resolve or _process_name_for_pid
    ignored = {name.lower() for name in SYSTEM_AUDIO_PROCESSES}
    ignored.update(name.lower() for name in MICROPHONE_NOISE_PROCESSES)
    names: list[str] = []
    named: set[str] = set()
    for pid in pids:
        if pid <= 0 or pid in own:
            continue
        name = (resolve(pid) or "").strip()
        lowered = name.lower()
        if not name or lowered in ignored or lowered in named:
            continue
        named.add(lowered)
        names.append(name)
    return names


def split_capture_sessions(
    sessions: Iterable[tuple[int, str]],
) -> tuple[list[int], list[tuple[int, str]]]:
    """Split sessions into (microphone pids, [(pid, loopback device name)]).

    The endpoint name decides: a session on "Stereo Mix" or a virtual cable's
    output is the PC's own audio being copied, anything else is an input.  A
    process can appear in both lists, and then it is doing both.
    """
    microphone_pids: list[int] = []
    loopback: list[tuple[int, str]] = []
    for pid, device_name in sessions:
        if pid <= 0:
            continue
        if classify_loopback_capture_device(device_name):
            loopback.append((pid, device_name))
        else:
            microphone_pids.append(pid)
    return microphone_pids, loopback


def live_capture_app_names() -> list[str]:
    """Programs capturing a *microphone* right now, this game excluded.

    Loopback sessions are not here: naming "Stereo Mix" as a microphone would
    be untrue, and they are reported by `live_loopback_capture_apps` as the
    narrower fact they are.
    """
    microphone_pids, _loopback = split_capture_sessions(capture_sessions())
    return capture_app_names(microphone_pids)


def loopback_capture_apps(
    sessions: Iterable[tuple[int, str]],
    own_pids: Sequence[int] | None = None,
    resolve: Callable[[int], str] | None = None,
) -> list[tuple[str, str]]:
    """(program, endpoint name) for every program copying the PC's output.

    The own process and Windows' audio hosts are excluded, exactly as in the
    microphone net: the game's own recorder may well be recording computer
    audio, and that is reported through `report_in_game_recording` with its
    scope instead of being accused here.  `MICROPHONE_NOISE_PROCESSES` is *not*
    a floor on this net: a call or a browser is what holds a microphone, and
    nothing ordinary holds a copy of the PC's output for a legitimate everyday
    reason except the recorder this reports.
    """
    own = {os.getpid()} if own_pids is None else set(own_pids)
    resolve = resolve or _process_name_for_pid
    ignored = {name.lower() for name in SYSTEM_AUDIO_PROCESSES}
    found: list[tuple[str, str]] = []
    named: set[str] = set()
    _microphone, loopback = split_capture_sessions(sessions)
    for pid, device_name in loopback:
        if pid in own:
            continue
        name = (resolve(pid) or "").strip()
        lowered = name.lower()
        if not name or lowered in ignored or lowered in named:
            continue
        named.add(lowered)
        family = classify_loopback_capture_device(device_name) or "loopback device"
        found.append((name, device_label(device_name) or family))
    return found


def live_loopback_capture_apps() -> list[tuple[str, str]]:
    """Programs copying the PC's own output right now, this game excluded."""
    return loopback_capture_apps(capture_sessions())


#: The game's own recorder starting is reported under this event.  It is sent to
#: the Server, which tells Developers only (their rank is the top one), because
#: a player using the recorder is not a rule violation either -- nothing on this
#: path may stop a recording or close the client.
IN_GAME_RECORDING_EVENT = "in_game_recording_started"

#: Starts that could not be sent yet (opened before login, or a send failure).
#: Bounded: a long offline session should not turn into a burst of stale lines.
_pending_in_game_reports: list[dict] = []
MAX_PENDING_IN_GAME_REPORTS = 5


def _send_in_game_recording(game, payload: dict) -> bool:
    network = getattr(game, "network", None)
    if network is None:
        return False
    try:
        from . import consts
        network.send(consts.CHANNEL_MISC, IN_GAME_RECORDING_EVENT, payload)
        return True
    except Exception as e:
        print(f"Recording watch: could not report the in-game recorder: {e}")
        return False


def report_in_game_recording(
    game,
    *,
    include_microphone: bool = False,
    include_computer_audio: bool = False,
) -> bool:
    """Tell the Server that the in-game recorder started, and its scope.

    A start that cannot be sent is remembered and retried by the anti-cheat
    cycle (`flush_pending_in_game_recording`), so pressing record a moment
    before the network is ready still reaches Developers.  The recording itself
    is never touched either way.
    """
    payload = {
        "include_microphone": bool(include_microphone),
        "include_computer_audio": bool(include_computer_audio),
    }
    if _send_in_game_recording(game, payload):
        return True
    if len(_pending_in_game_reports) < MAX_PENDING_IN_GAME_REPORTS:
        _pending_in_game_reports.append(payload)
    return False


def flush_pending_in_game_recording(game) -> int:
    """Send the recorder starts that were queued while there was no network."""
    sent = 0
    while _pending_in_game_reports:
        if not _send_in_game_recording(game, _pending_in_game_reports[0]):
            break
        _pending_in_game_reports.pop(0)
        sent += 1
    return sent


def _identity(finding: RecordingFinding) -> tuple[str, str]:
    """What a finding is about, independent of a window title or a timestamp."""
    return (finding.source, finding.app.strip().lower())


class RecordingObserver:
    """Turns the scans into one advisory per tool *appearance*.

    The rule is "one line when a recorder starts", not "one line per client
    run": a tool that is running is reported once, and when it closes the
    finding is forgotten, so opening it again reports it again.  A player who
    leaves the game and comes back starts a new session (`reset`), which says an
    already-open recorder once more -- otherwise a player could keep recording
    while the Client remembers it already told somebody.

    A finding is reported once, and only after the caller confirms staff was
    told (`mark_reported`); until then it is offered again on the next poll, so
    a program opened in the main menu is still reported after login instead of
    being lost with no network to send it.

    Every scan is injectable, which is how the tests run this on any machine
    without touching a real process table, window list or audio engine.
    """

    def __init__(
        self,
        process_scan=None,
        window_scan=None,
        microphone_scan=None,
        device_scan=None,
        loopback_scan=None,
        own_names: Sequence[str] | None = None,
    ):
        self._process_scan = process_scan if process_scan is not None else running_process_names
        self._window_scan = window_scan if window_scan is not None else (lambda: ())
        self._device_scan = device_scan if device_scan is not None else audio_device_names
        self._microphone_scan = (
            microphone_scan if microphone_scan is not None else live_capture_app_names
        )
        self._loopback_scan = (
            loopback_scan if loopback_scan is not None else live_loopback_capture_apps
        )
        self._own_names = tuple(own_names) if own_names is not None else own_process_names()
        self._reported: set[tuple[str, str]] = set()

    def poll(self) -> list[RecordingFinding]:
        """Findings staff has not been told about yet.

        A tool that is no longer there is forgotten here, which is what makes
        closing and reopening it a new, reportable appearance.
        """
        findings = self._scan()
        live = {_identity(finding) for finding in findings}
        self._reported &= live
        return [finding for finding in findings if _identity(finding) not in self._reported]

    def mark_reported(self, finding: RecordingFinding) -> None:
        self._reported.add(_identity(finding))

    def reset(self) -> None:
        """Start a new game session: an open recorder is worth saying again."""
        self._reported.clear()

    def _scan(self) -> list[RecordingFinding]:
        findings: list[RecordingFinding] = []
        seen: set[tuple[str, str]] = set()
        recorder_apps: set[str] = set()

        def add(app: str, source: str, detail: str) -> bool:
            finding = RecordingFinding(app, source, detail)
            if _identity(finding) in seen or len(findings) >= MAX_FINDINGS_PER_SCAN:
                return False
            seen.add(_identity(finding))
            findings.append(finding)
            return True

        for name in self._process_scan():
            app = classify_recording_process(name)
            if app and add(app, "process", str(name)):
                recorder_apps.add(app)

        for title in self._window_scan():
            app = classify_recording_window(title)
            if app:
                add(app, "window", str(title).strip())

        for name in self._device_scan():
            app = classify_virtual_audio_device(name)
            if app:
                add(app, "device", device_label(str(name)))

        own_names = {name.lower() for name in self._own_names}
        for name, device in self._loopback_scan():
            label = str(name).strip()
            if not label or label.lower() in own_names:
                # The game's own recorder copying the PC's audio is reported
                # through `report_in_game_recording`, with its scope.
                continue
            # The scan names the executable (`obs64.exe`); the process net
            # names the tool.  Both must land on the same app, or the dedupe
            # below would say the same tool twice under two spellings.
            app = classify_recording_process(label) or label
            if add(app, "loopback", device_label(str(device))):
                # One line per tool per appearance, and this is the narrower
                # fact: the same program was already said as "running", so the
                # line that also says *what* it holds replaces it.
                findings[:] = [
                    finding for finding in findings
                    if not (finding.app == app and finding.source in ("process", "microphone"))
                ]
                seen.discard(("process", app.lower()))
                seen.discard(("microphone", app.lower()))

        for name in self._microphone_scan():
            if str(name).lower() in own_names:
                # The game's own capture, named rather than passed by pid.
                continue
            if classify_recording_process(name) in recorder_apps:
                # A known recorder already reported by name would otherwise be
                # said twice; the process line and its "capturing" line are the
                # same fact, and staff need it once.
                continue
            add(name, "microphone", "")

        return findings
