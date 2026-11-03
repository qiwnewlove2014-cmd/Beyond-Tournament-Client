"""Audio from another program -- or the whole computer -- on the game's live legs.

A player who makes music outside the game (a DAW, a soft synth, a browser
player, a pedal's editor) had no way to let the people they listen with hear
it. The line-in path (`libs/instrument_input.py`) captures a *device* and
deliberately treats what it hears as an instrument: the harmony line, the pedal
board and the pitch tracker all shape it. A finished stereo mix from another
program must not be shaped like a guitar string, so it needs a source of its
own rather than a second device on that path.

This module is the source half of that pipe:

* it captures one process's output (Windows process loopback) or the whole
  output endpoint, as 48 kHz stereo s16 -- the recorder in
  `libs/game_audio_recorder.py` is the sibling that writes a WAV, and its COM
  plumbing is reused here instead of copied;
* it re-frames whatever the driver hands over into the game's own 20 ms frame,
  resampling a 44.1 kHz program on the way;
* it keeps MONO and STEREO as a real choice: ``mono`` averages the two sides
  into a centred pair (what a guitar wants), ``stereo`` carries the program's
  own left and right (what a song or a stereo mix wants);
* it pushes finished frames into a bounded queue the Music Bot's streamers
  already mix from, so the audio rides every leg the bot already owns -- the
  public broadcast, the PA, a cinema room and a Party Sync session -- with no
  new transport and no server change.

Two rules from the paths around this one:

* **Bounded, never growing.** The capture thread appends, the streamer drains;
  a stalled game must cost dropped audio, not a queue that replays the last
  minute. The queue keeps half a second and drops the oldest frame.
* **No OpenAL here.** This thread owns COM and bytes only. The ear is the
  streamer's (`libs/music_bot/streaming.py`), which mixes a stereo source into
  the pair without downmixing the song (``_mix_external_into_stereo``).

Rules and the pins: .agents/skills/instrument_creation/references/
external-audio.md.
"""

import ctypes
import threading
import time
from collections import deque

import numpy as np

from . import audio_trace

SAMPLE_RATE = 48_000
CHANNELS = 2
SAMPLE_WIDTH = 2
FRAME_MS = 20
#: One 20 ms stereo s16 frame -- the chunk every Music Bot leg carries.
FRAME_BYTES = SAMPLE_RATE * FRAME_MS // 1000 * CHANNELS * SAMPLE_WIDTH
#: The same frame as mono, for the legs that are mono by design (PA/voice).
MONO_FRAME_BYTES = FRAME_BYTES // 2
#: A live jam's own frame: 10 ms, the smallest Opus frame that still holds a
#: full note. The 20 ms frame above is what every song leg carries, so it stays;
#: the low-latency path takes this one and leaves the song path alone.
LIVE_FRAME_MS = 10
LIVE_FRAME_BYTES = SAMPLE_RATE * LIVE_FRAME_MS // 1000 * CHANNELS * SAMPLE_WIDTH

MODE_STEREO = "stereo"
MODE_MONO = "mono"
MODES = (MODE_STEREO, MODE_MONO)

KIND_APP = "app"
KIND_SYSTEM = "system"
KINDS = (KIND_APP, KIND_SYSTEM)

#: Loudest gain a source may be given (the mix headroom is the streamer's).
MAX_GAIN = 2.0
DEFAULT_GAIN = 0.85
#: How much captured audio waits for the streamer before the oldest is dropped.
BUFFER_FRAMES = 25          # 500 ms
#: A capture that has produced nothing for this long is reported as idle.
IDLE_REPORT_S = 2.0


class SourceUnavailable(RuntimeError):
    """Raised when a source cannot be opened on this machine."""


def clamped_gain(value, default=DEFAULT_GAIN):
    try:
        gain = float(value)
    except (TypeError, ValueError):
        return default
    if gain != gain:  # NaN
        return default
    return max(0.0, min(MAX_GAIN, gain))


def mode_label(mode):
    return MODE_MONO if str(mode or "").lower() == MODE_MONO else MODE_STEREO


def to_stereo48k(raw, channels=CHANNELS, rate=SAMPLE_RATE):
    """Any interleaved s16 packet -> 48 kHz interleaved stereo s16.

    One reader for both capture sources and for the tests: a driver may hand
    over mono or stereo, at the endpoint's rate or the program's old 44.1 kHz,
    and every leg downstream expects one shape. Resampling is linear and only
    runs when the rate actually differs -- the common 48 kHz path is a copy.
    """
    if not raw:
        return b""
    channels = max(1, int(channels or 1))
    rate = max(1, int(rate or SAMPLE_RATE))
    block = 2 * channels
    usable = len(raw) - (len(raw) % block)
    if usable <= 0:
        return b""
    samples = np.frombuffer(bytes(raw[:usable]), dtype=np.int16)
    if channels == 1:
        samples = np.repeat(samples, 2)
    elif channels > 2:
        frames = samples[: len(samples) // channels * channels]
        samples = frames.reshape(-1, channels)[:, :2].reshape(-1)
    if rate != SAMPLE_RATE:
        frames = samples.reshape(-1, 2)
        if len(frames) < 2:
            return b""
        target = int(round(len(frames) * SAMPLE_RATE / float(rate)))
        if target <= 0:
            return b""
        positions = np.arange(target, dtype=np.float64) * (rate / float(SAMPLE_RATE))
        low = np.floor(positions).astype(np.int64)
        np.clip(low, 0, len(frames) - 1, out=low)
        high = np.minimum(low + 1, len(frames) - 1)
        fraction = (positions - low).astype(np.float32)[:, None]
        blended = (frames[low].astype(np.float32) * (1.0 - fraction)
                   + frames[high].astype(np.float32) * fraction)
        samples = np.clip(np.round(blended), -32768, 32767).astype(np.int16).reshape(-1)
    return samples.tobytes()


def shape_frame(payload, mode=MODE_STEREO, gain=DEFAULT_GAIN):
    """One stereo frame with this source's channel mode and level applied.

    ``mono`` is not a downmix of the whole stream -- it is the source saying
    the material has no sides of its own (a guitar, a mono synth), so both
    sides carry the same average and the mix places it in the centre. ``stereo``
    leaves the program's own image alone.
    """
    if not payload:
        return b""
    usable = len(payload) - (len(payload) % (CHANNELS * SAMPLE_WIDTH))
    if usable <= 0:
        return b""
    frames = np.frombuffer(bytes(payload[:usable]), dtype=np.int16).reshape(-1, 2)
    if mode_label(mode) == MODE_MONO:
        mono = ((frames[:, 0].astype(np.int32) + frames[:, 1].astype(np.int32)) // 2)
        frames = np.repeat(mono.astype(np.int16), 2).reshape(-1, 2)
    gain = clamped_gain(gain)
    if gain != 1.0:
        frames = np.clip(
            np.round(frames.astype(np.float32) * gain), -32768, 32767
        ).astype(np.int16)
    return frames.tobytes()


def downmix_to_mono(payload):
    """Stereo s16 -> mono s16 (the legs that are mono by design)."""
    if not payload:
        return b""
    usable = len(payload) - (len(payload) % 4)
    if usable <= 0:
        return b""
    frames = np.frombuffer(bytes(payload[:usable]), dtype=np.int16).reshape(-1, 2)
    mono = ((frames[:, 0].astype(np.int32) + frames[:, 1].astype(np.int32)) // 2)
    return mono.astype(np.int16).tobytes()


#: How far back a peak is still "now" when the menu asks for a verdict.
SIGNAL_WINDOW_S = 2.0


class ExternalAudioSource(threading.Thread):
    """One running capture, framed for the Music Bot's streamers.

    ``backend`` only has to answer ``open()``, ``read(timeout)`` (returning
    ``(raw, channels, rate)`` or None) and ``close()``. The real one is
    :class:`LoopbackBackend`; the tests drive fakes, so the framing, the mode,
    the level and the queue are all pinned without a sound card.
    """

    def __init__(self, backend, sink=None, mode=MODE_STEREO,
                 gain=DEFAULT_GAIN, label="", live_sink=None, room_sink=None):
        super().__init__(daemon=True)
        self.backend = backend
        self.sink = sink
        self.live_sink = live_sink
        # Party Sync -> cinema: the same shaped 10 ms frame, handed to the
        # host's mixer. Kept separate from live_sink so the party relay and a
        # room can both receive it without one consuming the other's queue.
        self.room_sink = room_sink
        self.label = str(label or "")
        self.mode = mode_label(mode)
        self.gain = clamped_gain(gain)
        self.frames = deque(maxlen=BUFFER_FRAMES)
        self._pending = bytearray()
        self._live_pending = bytearray()
        self.lock = threading.Lock()
        self.running = True
        self.error = None
        self.dropped = 0
        self.frames_out = 0
        self.live_frames_out = 0
        self.song_frames_out = 0
        self.packets_in = 0
        self.bytes_in = 0
        self._trace_last_live = None
        self._trace_last_song = None
        self._trace_last_summary = time.monotonic()
        self.signal_log = deque()
        self.peak = 0
        self.last_frame_at = None
        self.started_at = time.monotonic()

    # ── framing ─────────────────────────────────────────────────────────
    def _stage(self, stereo):
        """Cut captured audio into the song frame and the live frame.

        Both are cut from the same bytes: the 20 ms frame is what a song, the
        PA or a room carries, and the 10 ms frame is the live jam's own. A
        caller that gave no ``live_sink`` pays for the second cut only in a
        byte buffer, never in a queue.
        """
        if not stereo:
            return
        self.packets_in += 1
        self.bytes_in += len(stereo)
        self._pending += stereo
        if self.live_sink is not None:
            self._live_pending += stereo
            while len(self._live_pending) >= LIVE_FRAME_BYTES:
                frame = bytes(self._live_pending[:LIVE_FRAME_BYTES])
                del self._live_pending[:LIVE_FRAME_BYTES]
                self._emit_live(frame)
        while len(self._pending) >= FRAME_BYTES:
            frame = bytes(self._pending[:FRAME_BYTES])
            del self._pending[:FRAME_BYTES]
            self._emit(frame)

    def _emit(self, frame):
        self._note_signal(frame)
        shaped = shape_frame(frame, self.mode, self.gain)
        if not shaped:
            return
        self.frames_out += 1
        self.song_frames_out += 1
        now = time.monotonic()
        self.last_frame_at = now
        if audio_trace.enabled():
            gap = audio_trace.gap_ms(now, self._trace_last_song)
            self._trace_last_song = now
            if gap is not None and gap > 40.0:
                audio_trace.event(
                    "ext_song_gap", label=self.label, gap_ms=gap,
                    song_frames=self.song_frames_out,
                )
            self._trace_source_summary(now)
        if self.sink is not None:
            try:
                self.sink(shaped)
                return
            except Exception:
                # A sink that is gone (the bot closed under the capture) must
                # not kill the worker; fall through to the local buffer so the
                # frames stay countable for the status line.
                pass
        if len(self.frames) == self.frames.maxlen:
            self.dropped += 1
        self.frames.append(shaped)

    def _emit_live(self, frame):
        """One 10 ms frame for the low-latency jam leg.

        The signal row measures the 20 ms copy, so a program is not counted
        twice; this path only shapes the audio the relay sends.
        """
        shaped = shape_frame(frame, self.mode, self.gain)
        if not shaped:
            return
        self.live_frames_out += 1
        now = time.monotonic()
        if audio_trace.enabled():
            gap = audio_trace.gap_ms(now, self._trace_last_live)
            self._trace_last_live = now
            if gap is not None and gap > 30.0:
                audio_trace.event(
                    "ext_live_gap", label=self.label, gap_ms=gap,
                    live_frames=self.live_frames_out,
                )
            self._trace_source_summary(now)
        if self.live_sink is None:
            return
        self.last_frame_at = now
        try:
            self.live_sink(shaped)
        except Exception:
            pass
        if self.room_sink is not None:
            try:
                self.room_sink(shaped, True)
            except Exception:
                pass

    def _trace_source_summary(self, now):
        """One line a second: is the capture itself still producing frames?"""
        if now - self._trace_last_summary < 1.0:
            return
        self._trace_last_summary = now
        audio_trace.event(
            "ext_source", label=self.label, mode=self.mode, gain=self.gain,
            live_frames=self.live_frames_out, song_frames=self.song_frames_out,
            packets=self.packets_in, bytes=self.bytes_in,
            dropped=self.dropped, peak=self.peak,
            idle_ms=round(self.idle_seconds() * 1000.0, 1),
        )

    def drain(self, limit=2):
        """Pop up to ``limit`` frames (the streamer's per-frame budget)."""
        out = []
        while self.frames and len(out) < max(0, int(limit)):
            out.append(self.frames.popleft())
        return out

    # ── settings ────────────────────────────────────────────────────────
    def set_mode(self, mode):
        self.mode = mode_label(mode)

    def set_gain(self, gain):
        self.gain = clamped_gain(gain)

    def idle_seconds(self):
        if self.last_frame_at is None:
            return time.monotonic() - self.started_at
        return time.monotonic() - self.last_frame_at

    def status_line(self):
        base = (f"{self.label or 'External audio'}: {self.mode}, "
                f"{int(self.gain * 100)}%")
        if self.error:
            return f"External audio stopped: {self.error}"
        if self.last_frame_at is None:
            if self.idle_seconds() > IDLE_REPORT_S:
                return (f"{self.label or 'External audio'} is selected but has "
                        "produced no sound yet.")
            return base
        return f"{base}; {self.signal_line().replace('Signal: ', 'signal ')}"

    # ── signal: is anything actually coming through? ─────────────────────
    def _note_signal(self, frame):
        """Remember how loud this frame is, for SIGNAL_WINDOW_S."""
        try:
            block = np.frombuffer(bytes(frame), dtype=np.int16)
            peak = int(np.max(np.abs(block.astype(np.int32)))) if block.size else 0
        except Exception:
            peak = 0
        now = time.monotonic()
        # A frame that reached the game is a frame the status line counts,
        # whether or not `_emit` gets to shape it afterwards.
        self.last_frame_at = now
        self.signal_log.append((now, peak))
        self.peak = peak
        while self.signal_log and now - self.signal_log[0][0] > SIGNAL_WINDOW_S:
            self.signal_log.popleft()

    def recent_peak(self, now=None):
        """The loudest sample captured in the last SIGNAL_WINDOW_S, or None.

        Measured before the level and the channel mode, so it answers "is the
        program making sound", not "how loud did we send it".
        """
        now = time.monotonic() if now is None else now
        peaks = [peak for stamp, peak in self.signal_log
                 if now - stamp <= SIGNAL_WINDOW_S]
        if not peaks:
            return None
        return max(peaks)

    @staticmethod
    def signal_word(peak):
        """A word for a peak, from the listener's side of the question."""
        if peak is None:
            return "nothing arriving"
        if peak <= 8:
            return "silent"
        if peak < 800:
            return "very quiet"
        if peak < 8000:
            return "quiet"
        if peak < 26000:
            return "good"
        if peak < 32767:
            return "loud"
        return "clipping"

    def signal_line(self):
        """One short line for the menu's own signal row."""
        if self.error:
            return "Signal: stopped"
        if self.last_frame_at is None:
            return "Signal: nothing arriving yet"
        peak = self.recent_peak()
        if peak is None:
            return (f"Signal: nothing arriving for "
                    f"{self.idle_seconds():.0f} seconds")
        percent = int(peak / 327.68)
        return f"Signal: {self.signal_word(peak)} (peak {percent}%)"

    def signal_report(self):
        """A verdict a blind player can act on, not just a number."""
        if self.error:
            return (f"The capture stopped: {self.error}. Choose the program "
                    "again, or use the whole computer.")
        if self.last_frame_at is None:
            return ("Nothing has arrived from this program yet. Check that it "
                    "is playing, that it is not muted in the Windows volume "
                    "mixer, and that it plays through a Windows output device: "
                    "a program on an ASIO driver (common in music programs) "
                    "never reaches the Windows audio engine, so Windows cannot "
                    "capture it at all until its output is switched to a "
                    "Windows device such as the primary sound driver.")
        peak = self.recent_peak()
        if peak is None:
            return ("The program was sending sound, but nothing has arrived "
                    f"for {self.idle_seconds():.0f} seconds. If it is still "
                    "playing, it may have changed its output device.")
        word = self.signal_word(peak)
        percent = int(peak / 327.68)
        if word == "silent":
            return ("The capture is running but every sample is silent. The "
                    "program may be paused or muted, or it may be using an "
                    "ASIO driver, which bypasses the Windows audio engine -- "
                    "switch its output to a Windows device (the primary sound "
                    "driver is one) and press this row again.")
        if word in ("very quiet", "quiet"):
            return (f"The capture is running and sound is arriving, quietly "
                    f"(peak {percent} percent). Turn the program up, or raise "
                    "the Level row, if listeners say they cannot hear it.")
        if word == "clipping":
            return ("Sound is arriving and it is clipping at the top. Turn "
                    "the program down a little; the level row cannot undo a "
                    "clip that already happened.")
        return (f"Sound is arriving from the program (peak {percent} "
                "percent), so it is being sent wherever your music goes.")

    # ── worker ──────────────────────────────────────────────────────────
    def run(self):
        try:
            self.backend.open()
            audio_trace.event(
                "ext_open", label=self.label, mode=self.mode, gain=self.gain,
                backend=type(self.backend).__name__,
            )
        except Exception as exc:
            self.error = str(exc) or exc.__class__.__name__
            audio_trace.event("ext_error", label=self.label,
                              error=self.error)
            self.running = False
            return
        try:
            while self.running:
                try:
                    packet = self.backend.read(0.1)
                except Exception as exc:
                    self.error = str(exc) or exc.__class__.__name__
                    break
                if not packet:
                    continue
                try:
                    raw, channels, rate = packet
                    self._stage(to_stereo48k(raw, channels, rate))
                except Exception as exc:
                    # One malformed packet is dropped, not the whole capture:
                    # a driver that changes format mid-stream must cost a frame.
                    self.error = str(exc) or exc.__class__.__name__
                    audio_trace.event("ext_error", label=self.label,
                                      error=self.error)
        finally:
            try:
                self.backend.close()
            except Exception:
                pass
            self.running = False
            audio_trace.event(
                "ext_close", label=self.label, error=self.error,
                live_frames=self.live_frames_out, song_frames=self.song_frames_out,
                packets=self.packets_in, bytes=self.bytes_in,
                dropped=self.dropped,
            )

    def stop(self):
        self.running = False
        audio_trace.event("ext_stop", label=self.label,
                          live_frames=self.live_frames_out,
                          song_frames=self.song_frames_out)
        try:
            if self.is_alive():
                self.join(timeout=1.5)
        except Exception:
            pass
        with self.lock:
            self._pending.clear()


#: Name fragments that mark a playback device as a routing cable rather
#: than a speaker. A DAW that cannot be captured directly is routed here.
VIRTUAL_DEVICE_HINTS = ("cable", "voicemeeter", "virtual", "vac ", "vb-audio",
                        "loopback")


def is_virtual_device(name):
    """True for a playback device that is really a cable between programs."""
    text = (name or "").casefold()
    return any(hint in text for hint in VIRTUAL_DEVICE_HINTS)


def render_devices():
    """Every active playback device, as ``[{"id", "name", "virtual"}]``.

    The device is the one handle Windows always offers: a program that keeps
    its audio out of the session list (ASIO, exclusive mode) still renders to
    *some* device, and a program a player cannot name can be routed to a cable
    whose device this lists. Empty off Windows or without COM.
    """
    import ctypes
    import os
    if os.name != "nt":
        return []
    try:
        from . import recording_watch as watch
        if not getattr(watch, "_COMTYPES_OK", False):
            return []
        import comtypes
    except Exception:
        return []
    found = []
    initialized = False
    enumerator = devices = device = None
    try:
        try:
            comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
            initialized = True
        except OSError:
            initialized = False
        enumerator = comtypes.CoCreateInstance(
            watch._CLSID_MMDEVICE_ENUMERATOR,
            interface=watch._IMMDeviceEnumerator,
            clsctx=watch.CLSCTX_ALL,
        )
        devices = enumerator.EnumAudioEndpoints(E_RENDER, DEVICE_STATE_ACTIVE)
        for index in range(int(devices.GetCount())):
            try:
                device = devices.Item(index)
                identifier = ctypes.wstring_at(device.GetId())
            except Exception:
                continue
            name = watch.endpoint_friendly_name(identifier) or identifier
            found.append({"id": identifier, "name": name,
                          "virtual": is_virtual_device(name)})
    except Exception:
        return found
    finally:
        device = devices = enumerator = None
        if initialized:
            try:
                comtypes.CoUninitialize()
            except Exception:
                pass
    found.sort(key=lambda item: (item["name"].lower(), item["id"]))
    return found


class DeviceLoopbackRecorder:
    """Endpoint loopback on a *named* playback device.

    `game_audio_recorder.SystemOutputLoopbackRecorder` is the default-device
    version of this; a program routed to a cable is on neither the default
    device nor any session that can be named, so the device is the only
    handle Windows offers.
    """

    uses_process_loopback = False

    def __init__(self, device_id):
        self.device_id = str(device_id or "")

    def _activate(self):
        import os
        if os.name != "nt":
            raise SourceUnavailable("Capturing an audio device requires Windows.")
        from . import game_audio_recorder as recorder
        import comtypes
        from ctypes import byref
        if not self.device_id:
            raise SourceUnavailable("No audio device was chosen to capture.")
        enumerator = comtypes.CoCreateInstance(
            recorder.CLSID_MMDEVICE_ENUMERATOR,
            interface=recorder.IMMDeviceEnumerator,
            clsctx=recorder.SystemOutputLoopbackRecorder.CLSCTX_ALL,
        )
        endpoint = enumerator.GetDevice(self.device_id)
        activated = endpoint.Activate(
            byref(recorder.IAudioClient._iid_),
            recorder.SystemOutputLoopbackRecorder.CLSCTX_ALL,
            None,
        )
        return activated.QueryInterface(recorder.IAudioClient)


class LoopbackBackend:
    """Windows WASAPI loopback as packets: one process tree, or the endpoint.

    The COM setup is `libs/game_audio_recorder.py`'s own recorder (its
    ``_activate`` returns the ``IAudioClient`` for either target), so there is
    one place in this client that knows how to ask Windows for a loopback
    stream. What is different here is the shape of the use: no file writer, no
    WAV, and the packet goes to a deque the source thread drains.
    """

    def __init__(self, process_id=None, whole_computer=False, device_id=""):
        self.process_id = int(process_id) if process_id else None
        self.whole_computer = bool(whole_computer)
        self.device_id = str(device_id or "")
        self._packets = deque(maxlen=64)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._error = None

    def open(self):
        import os
        if os.name != "nt":
            raise SourceUnavailable("Capturing another program's audio requires Windows.")
        from . import game_audio_recorder as recorder
        if not self.whole_computer and not self.device_id:
            if self.process_id is None:
                raise SourceUnavailable("No program was chosen to capture.")
            if not recorder.process_loopback_supported():
                raise SourceUnavailable(recorder.process_loopback_unavailable_message())
        self._stop.clear()
        self._error = None
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            if self._error:
                raise SourceUnavailable(self._error)
            with self._lock:
                if self._packets:
                    return
            if not self._thread.is_alive():
                break
            time.sleep(0.02)

    def read(self, timeout=0.1):
        deadline = time.monotonic() + max(0.0, float(timeout))
        while True:
            with self._lock:
                if self._packets:
                    return self._packets.popleft()
            if self._error is not None and not self._thread_alive():
                raise SourceUnavailable(self._error)
            if time.monotonic() >= deadline:
                return None
            time.sleep(0.002)

    def close(self):
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            try:
                thread.join(timeout=2.0)
            except Exception:
                pass
        with self._lock:
            self._packets.clear()

    def _thread_alive(self):
        return bool(self._thread and self._thread.is_alive())

    # ── the worker (owns this thread's COM apartment) ───────────────────
    def _run(self):
        from . import game_audio_recorder as recorder
        import comtypes

        kernel32 = recorder._kernel32
        audio_client = None
        capture = None
        sample_event = None
        initialized = False
        try:
            try:
                comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
                initialized = True
            except OSError:
                initialized = False
            if self.device_id:
                owner = DeviceLoopbackRecorder(self.device_id)
            elif self.whole_computer:
                owner = recorder.SystemOutputLoopbackRecorder()
            else:
                owner = recorder.ProcessLoopbackRecorder(self.process_id)
            audio_client = owner._activate()
            block_align = CHANNELS * SAMPLE_WIDTH
            audio_format = recorder.WAVEFORMATEX(
                1, CHANNELS, SAMPLE_RATE, SAMPLE_RATE * block_align,
                block_align, SAMPLE_WIDTH * 8, 0,
            )
            flags = (
                recorder.ProcessLoopbackRecorder.AUDCLNT_STREAMFLAGS_LOOPBACK
                | recorder.ProcessLoopbackRecorder.AUDCLNT_STREAMFLAGS_EVENTCALLBACK
                | recorder.ProcessLoopbackRecorder.AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM
            )
            audio_client.Initialize(
                recorder.ProcessLoopbackRecorder.AUDCLNT_SHAREMODE_SHARED,
                flags, 500_000, 0, ctypes.byref(audio_format), None,
            )
            sample_event = kernel32.CreateEventW(None, False, False, None)
            audio_client.SetEventHandle(sample_event)
            capture = audio_client.GetService(
                ctypes.byref(recorder.IAudioCaptureClient._iid_)
            )
            audio_client.Start()
            handles = (ctypes.c_void_p * 1)(ctypes.c_void_p(sample_event))
            while not self._stop.is_set():
                waited = kernel32.WaitForMultipleObjects(1, handles, False, 100)
                if waited != 0:
                    continue
                pending = capture.GetNextPacketSize()
                while pending:
                    data, frames, buffer_flags, _, _ = capture.GetBuffer()
                    try:
                        byte_count = int(frames) * block_align
                        if buffer_flags & recorder.ProcessLoopbackRecorder.AUDCLNT_BUFFERFLAGS_SILENT:
                            pcm = b"\0" * byte_count
                        else:
                            pcm = ctypes.string_at(data, byte_count)
                        if pcm:
                            with self._lock:
                                self._packets.append((pcm, CHANNELS, SAMPLE_RATE))
                    finally:
                        capture.ReleaseBuffer(frames)
                    pending = capture.GetNextPacketSize()
        except Exception as exc:
            self._error = str(exc) or exc.__class__.__name__
        finally:
            if audio_client is not None:
                try:
                    audio_client.Stop()
                except Exception:
                    pass
            if sample_event:
                try:
                    kernel32.CloseHandle(sample_event)
                except Exception:
                    pass
            capture = None
            audio_client = None
            if initialized:
                try:
                    comtypes.CoUninitialize()
                except Exception:
                    pass


# ── what the menu shows ─────────────────────────────────────────────────────

#: The COM pieces `recording_watch` already declares are reused here: one
#: description of the audio engine in this client, not two.
E_RENDER = 0
DEVICE_STATE_ACTIVE = 1
SESSION_STATE_ACTIVE = 1


def playing_apps(own_pid=None):
    """Programs rendering audio right now, as ``[{"pid", "name"}]``.

    This is the render-endpoint sibling of `recording_watch.capture_sessions`:
    the same session manager, asked on the playback side, so the menu can name
    the program a player just started instead of asking for a process id. Only
    *active* sessions count -- a paused player is not a source anybody wants to
    capture. Empty on any other platform or when COM is unavailable.
    """
    import os
    if os.name != "nt":
        return []
    try:
        from . import recording_watch as watch
        if not getattr(watch, "_COMTYPES_OK", False):
            return []
        import comtypes
        from ctypes import byref
    except Exception:
        return []
    if own_pid is None:
        own = {os.getpid()}
    elif isinstance(own_pid, int):
        own = {own_pid}
    else:
        own = set(own_pid)
    found = []
    seen = set()
    initialized = False
    enumerator = devices = device = activated = manager = sessions = session = None
    try:
        try:
            comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
            initialized = True
        except OSError:
            initialized = False
        enumerator = comtypes.CoCreateInstance(
            watch._CLSID_MMDEVICE_ENUMERATOR,
            interface=watch._IMMDeviceEnumerator,
            clsctx=watch.CLSCTX_ALL,
        )
        devices = enumerator.EnumAudioEndpoints(E_RENDER, DEVICE_STATE_ACTIVE)
        for index in range(int(devices.GetCount())):
            try:
                device = devices.Item(index)
                activated = device.Activate(
                    byref(watch._IAudioSessionManager2._iid_), watch.CLSCTX_ALL, None)
                manager = activated.QueryInterface(watch._IAudioSessionManager2)
                sessions = manager.GetSessionEnumerator()
            except Exception:
                continue
            for session_index in range(int(sessions.GetCount())):
                try:
                    session = sessions.GetSession(session_index)
                    if int(session.GetState()) != SESSION_STATE_ACTIVE:
                        continue
                    pid = int(session.GetProcessId())
                except Exception:
                    continue
                if pid <= 0 or pid in own or pid in seen:
                    continue
                name = watch._process_name_for_pid(pid)
                # A process psutil cannot name (elevated, or already gone) is
                # still a source worth offering: it has a live audio session,
                # and its pid is all process loopback needs.
                name = name or f"Process {pid}"
                seen.add(pid)
                found.append({"pid": pid, "name": name})
    except Exception:
        return found
    finally:
        activated = manager = sessions = device = devices = enumerator = session = None
        if initialized:
            try:
                comtypes.CoUninitialize()
            except Exception:
                pass
    found.sort(key=lambda item: item["name"].lower())
    return found


def _windowed_pids():
    """Process ids that own a visible top-level window (Windows only)."""
    import os
    if os.name != "nt":
        return []
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
    except Exception:
        return []
    pids = []
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND,
                                       wintypes.LPARAM)

    def collect(hwnd, _param):
        try:
            if user32.IsWindowVisible(hwnd) and user32.GetWindowTextLengthW(hwnd) > 0:
                pid = wintypes.DWORD()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                if pid.value:
                    pids.append(int(pid.value))
        except Exception:
            pass
        return True

    try:
        user32.EnumWindows(callback_type(collect), 0)
    except Exception:
        pass
    return pids


def running_apps(own_pid=None, playing=None, windows=None, namer=None):
    """Everything a player could mean, as ``[{"pid", "name", "playing"}]``.

    `playing_apps` alone was not enough in the field: a music program on an
    ASIO driver has no Windows audio session at all, so the picker showed the
    one program that did have one -- a screen reader -- and a search for the
    program the player wanted found nothing. This adds every process with a
    visible window, marks the ones that are already rendering, and sorts them
    first. Capturing a named process that is not rendering yet is fine: the
    capture opens and waits for it.

    The three inputs are injectable so the ordering and the merge are pinned
    without COM or a desktop.
    """
    import os
    if own_pid is None:
        own = {os.getpid()}
    elif isinstance(own_pid, int):
        own = {own_pid}
    else:
        own = set(own_pid)
    if playing is None:
        playing = playing_apps(own_pid=own_pid)
    apps = []
    seen = set()
    for item in playing or ():
        try:
            pid = int(item["pid"])
        except (KeyError, TypeError, ValueError):
            continue
        if pid <= 0 or pid in own or pid in seen:
            continue
        seen.add(pid)
        apps.append({"pid": pid, "name": item.get("name") or f"Process {pid}",
                     "playing": True})
    if windows is None:
        windows = _windowed_pids()
    if namer is None:
        try:
            from . import recording_watch as watch
            namer = watch._process_name_for_pid
        except Exception:
            namer = lambda pid: ""
    for pid in windows or ():
        try:
            pid = int(pid)
        except (TypeError, ValueError):
            continue
        if pid <= 0 or pid in own or pid in seen:
            continue
        try:
            name = namer(pid) or ""
        except Exception:
            name = ""
        if not name:
            continue
        seen.add(pid)
        apps.append({"pid": pid, "name": name, "playing": False})
    apps.sort(key=lambda item: (not item["playing"], item["name"].lower()))
    return apps
