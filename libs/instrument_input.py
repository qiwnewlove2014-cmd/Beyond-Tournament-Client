"""Second capture device for instrument input (e.g. a guitar/bass line-in).

Kept fully independent from the voice-chat microphone: it reads its own
`audio_instrument_input_device` option and opens its own cyal capture
device, so both can run at the same time. Captured 20 ms frames land in a
bounded ring buffer that a future pitch detector / instrument session can
consume.
"""
import collections
import contextlib
import threading
import time

import cyal
import numpy as np

from . import options, pitch, speech

# Device-name hints that suggest a capture device is a guitar/bass line-in.
# This covers two common setups:
#   1. A dedicated guitar/bass audio interface (Focusrite, iRig, ...).
#   2. A USB multi-effects pedal / amp modeller: the guitar plugs into the
#      pedal and the pedal's USB audio output is the capture source (Boss GT,
#      Zoom G, Line 6 POD/Helix, Valeton, NUX, Mooer, Kemper, Axe-Fx, ...).
GUITAR_DEVICE_KEYWORDS = (
    # guitar / bass audio interfaces
    "guitar", "bass", "focusrite", "scarlett", "behringer", "irig",
    "rocksmith", "toneport", "guitar link", "audio interface", "usb guitar",
    "yamaha", "ux1", "ux2",
    # USB multi-effects pedals / amp modellers
    "line6", "line 6", "pod", "helix", "boss", "zoom", "korg", "vox",
    "digitech", "kemper", "fractal", "axe fx", "valeton", "nux", "mooer",
    "positive grid", "multi effects", "multi-fx", "multifx", "effects",
    "pedal", "amp modeller", "amp modeler", "gt-", "me-", "rp-",
)


def is_guitar_input(name):
    """True if a capture device name suggests a guitar/bass line-in or a USB
    multi-effects pedal (guitar plugged into the pedal)."""
    lower = name.lower()
    return any(k in lower for k in GUITAR_DEVICE_KEYWORDS)


class GuitarLocalMonitor:
    """Main-thread OpenAL source that plays captured guitar PCM back at the
    player's position (the "hear your own strum" monitor).

    Must be created and fed on the main thread only - the OpenAL context is
    only current there (same rule as remote piano notes).
    """

    def __init__(self, audio_mngr):
        self.audio_mngr = audio_mngr
        self.source = None

    def _ensure_source(self):
        if self.source is None:
            self.source = self.audio_mngr.context.gen_source()
            self.source.looping = False

    def set_position(self, x, y, z):
        if self.source is not None:
            with contextlib.suppress(Exception):
                self.source.position = (x, y, z)

    def feed(self, pcm):
        """Queue one mono16 20 ms frame; keep the source drained."""
        if not pcm:
            return
        self._ensure_source()
        try:
            buf = self.audio_mngr.context.gen_buffer()
            buf.set_data(pcm, sample_rate=48000,
                         format=cyal.BufferFormat.MONO16)
            self.source.queue_buffers(buf)
            if self.source.state in (cyal.SourceState.STOPPED,
                                     cyal.SourceState.INITIAL):
                self.source.play()
            while self.source.buffers_processed > 0:
                self.source.unqueue_buffers()
        except Exception:
            pass

    def close(self):
        if self.source is not None:
            with contextlib.suppress(Exception):
                self.source.stop()
            self.source = None


class InstrumentInput(threading.Thread):
    """Owns one OpenAL capture device used as an instrument line-in."""

    FRAME_SAMPLES = 960          # 20 ms at 48 kHz, same chunk as voice chat
    FRAME_BUFFER_FRAMES = 100    # ~2 s of 20 ms frames
    NOTE_BUFFER_NOTES = 64

    def __init__(self, game):
        super().__init__(daemon=True)
        self.game = game
        self.capture_ext = cyal.CaptureExtension()
        self.audio_input = None
        self.stereo = False
        self._open(options.get("audio_instrument_input_device", "system default"))
        self.frames = collections.deque(maxlen=self.FRAME_BUFFER_FRAMES)
        self.tracker = pitch.PitchTracker()
        self.notes = collections.deque(maxlen=self.NOTE_BUFFER_NOTES)
        # Raw guitar Opus streamed on the normal 3D voice channel (so chords
        # are heard near the player without needing the music bot broadcast).
        self._guitar_voice = None
        self.recording = False
        self.running = True
        self.start()

    def _open(self, device):
        if device == "system default":
            device = self.capture_ext.default_device.decode("utf-8")
        # Mono first (what the voice chat uses). Some USB effects pedals only
        # expose stereo capture, so fall back to STEREO16 and downmix in the
        # capture loop when the mono request is rejected.
        for fmt, stereo in ((cyal.BufferFormat.MONO16, False),
                            (cyal.BufferFormat.STEREO16, True)):
            try:
                self.audio_input = self.capture_ext.open_device(
                    name=device.encode(),
                    sample_rate=48000,
                    format=fmt,
                )
                self.stereo = stereo
                return
            except (cyal.exceptions.DeviceNotFoundError, TypeError):
                continue
        self.audio_input = None
        speech.speak(f"Failed to load instrument input device: {device}")

    def reopen(self, device):
        """Switch to another capture device (called from the in-game menu).

        ``device`` must already be resolved (the raw name, or the default
        device's name for "system default").
        """
        if self.audio_input is not None and getattr(self.audio_input, "name", None) == device:
            return
        self.audio_input = None
        self._open(device)

    def start_recording(self):
        """Begin capturing into the ring buffer (no-op if the device failed)."""
        if self.audio_input is None:
            return
        self.audio_input.start()
        self.recording = True

    def stop_recording(self):
        self.recording = False
        if self.audio_input is not None:
            self.audio_input.stop()

    def _find_music_bot(self):
        """Locate the active MapMusicBot (if any) in the game stack."""
        if not hasattr(self.game, "stack"):
            return None
        for st in reversed(self.game.stack):
            if hasattr(st, "music_bot") and st.music_bot:
                return st.music_bot
        return None

    def run(self):
        while self.running:
            time.sleep(0.0005)
            if not self.recording or self.audio_input is None:
                continue
            if self.audio_input.available_samples >= self.FRAME_SAMPLES:
                # cyal counts frames for both formats: mono16 frames are 2
                # bytes, stereo16 frames are 4 bytes (L+R pairs).
                buf = bytearray(self.FRAME_SAMPLES * (4 if self.stereo else 2))
                self.audio_input.capture_samples(buf)
                if self.stereo:
                    mono = _downmix_stereo(buf)
                    raw = mono
                    buf16 = bytearray(mono)
                else:
                    raw = bytes(buf)
                    buf16 = buf
                self.frames.append(raw)  # raw monitor stream (strums/chords)
                
                # Check for Megaphone routing
                gp = None
                if hasattr(self.game, 'stack'):
                    for st in reversed(self.game.stack):
                        if hasattr(st, 'player') and hasattr(st, 'megaphone'):
                            gp = st
                            break
                voice_using_mega = getattr(gp, 'voice_chat_using_megaphone', False) if gp else False

                if voice_using_mega and gp:
                    from . import voice_chat
                    if hasattr(voice_chat, '_feed_local_megaphone_direct'):
                        voice_chat._feed_local_megaphone_direct(gp, buf16)

                self._feed_guitar_voice(buf16, force_mega=voice_using_mega)
                frame = np.frombuffer(buf16, dtype=np.int16).astype(np.float32) / 32768.0
                result = self.tracker.feed(frame)
                if result is not None:
                    self.notes.append(result)

                # Route the raw guitar audio into the music bot broadcast, but
                # only while the music bot broadcast is enabled (the condition
                # the performer asked for): the streamer mixes it in and sends
                # it out 3D (music bot channel) or via the megaphone.
                music_bot = self._find_music_bot()
                if music_bot and getattr(music_bot, "broadcast_enabled", False):
                    if not hasattr(music_bot, "guitar_pcm_queue"):
                        music_bot.guitar_pcm_queue = collections.deque(maxlen=10)
                    music_bot.guitar_pcm_queue.append(raw)

    def _feed_guitar_voice(self, raw, force_mega=False):
        """Stream the raw guitar audio out on the normal 3D voice channel.

        Uses the game's own voice compression (Opus, CHANNEL_VOICECHAT); the
        server relays it on this player's voice channel so nearby players
        hear the strums/chords spatially - no music bot broadcast needed.
        """
        if self._guitar_voice is None:
            if self.game is None:
                return
            from . import consts, voice_chat
            try:
                self._guitar_voice = voice_chat.voice_chat_compression(
                    self.game, consts.CHANNEL_VOICECHAT)
            except Exception:
                self._guitar_voice = None
                
        if self._guitar_voice is not None:
            from . import consts
            target_channel = consts.CHANNEL_MEGAPHONE if force_mega else consts.CHANNEL_VOICECHAT
            if getattr(self._guitar_voice, 'channel', None) != target_channel:
                if hasattr(self._guitar_voice, 'set_channel'):
                    self._guitar_voice.set_channel(target_channel)
                else:
                    self._guitar_voice.channel = target_channel
                    
            self._guitar_voice.put(bytearray(raw))

    def drain_raw_frames(self):
        """Pop and return all raw mono16 frames captured since last call."""
        frames = list(self.frames)
        self.frames.clear()
        return frames

    def drain_notes(self):
        """Pop and return all detected (note_name, velocity) pairs so far."""
        notes = list(self.notes)
        self.notes.clear()
        return notes

    def close(self):
        self.running = False
        self.recording = False
        if self._guitar_voice is not None:
            try:
                self._guitar_voice.put(None)  # stop its encode/send thread
            except Exception:
                pass
            self._guitar_voice = None
        if self.audio_input is not None:
            self.audio_input.stop()
            self.audio_input = None


def _downmix_stereo(buf):
    """Downmix a stereo16 PCM buffer to mono16 (L+R averaged)."""
    arr = np.frombuffer(buf, dtype=np.int16).reshape(-1, 2)
    mono = (arr[:, 0].astype(np.int32) + arr[:, 1].astype(np.int32)) // 2
    return mono.astype(np.int16).tobytes()


# Signal scan: how long to listen to each capture device and the minimum RMS
# (0..1) for a device to count as carrying real signal. A strummed guitar
# through a pedal is far louder than an idle microphone's ambient room noise,
# so the scan finds the guitar input even when the device name is generic
# (e.g. plain "USB Audio Device" that could be a mic).
SIGNAL_SCAN_SECONDS = 0.5
SIGNAL_SCAN_THRESHOLD = 0.03


def _probe_device_signal(device, seconds=SIGNAL_SCAN_SECONDS):
    """Open one capture device briefly and measure the loudest RMS it carries.

    Returns (rms, stereo) or None if the device cannot be opened. Tries mono
    first and falls back to stereo (downmixed for the measurement), matching
    the instrument input's own open strategy.
    """
    try:
        cap = cyal.CaptureExtension()
        for fmt, stereo in ((cyal.BufferFormat.MONO16, False),
                            (cyal.BufferFormat.STEREO16, True)):
            try:
                inp = cap.open_device(name=device.encode(),
                                      sample_rate=48000, format=fmt)
            except (cyal.exceptions.DeviceNotFoundError, TypeError):
                continue
            try:
                inp.start()
                deadline = time.perf_counter() + seconds
                peak = 0.0
                frames = 0
                while time.perf_counter() < deadline:
                    if inp.available_samples >= 960:
                        buf = bytearray(960 * (4 if stereo else 2))
                        inp.capture_samples(buf)
                        if stereo:
                            buf = bytearray(_downmix_stereo(buf))
                        arr = np.frombuffer(buf, dtype=np.int16).astype(np.float32) / 32768.0
                        peak = max(peak, float(np.sqrt(np.mean(arr ** 2))))
                        frames += 1
                if frames > 0:
                    return peak, stereo
            finally:
                try:
                    inp.stop()
                except Exception:
                    pass
    except Exception:
        return None
    return None


def scan_for_signal_devices(devices=None, threshold=SIGNAL_SCAN_THRESHOLD):
    """Probe every capture device and return the ones carrying real signal.

    ``devices`` may be a list of device-name strings (for tests); otherwise the
    real cyal capture device list is used. Returns dicts
    ``{device, name, rms, stereo, guitar_pedal}`` sorted loudest-first with
    named guitar/pedal devices ranked ahead of equally loud generic ones, so a
    generic-named pedal ("USB Audio Device") is still found when the player
    strums during the scan.
    """
    if devices is None:
        try:
            cap = cyal.CaptureExtension()
            devices = list(cap.devices)
        except Exception:
            return []
    found = []
    for device in devices:
        result = _probe_device_signal(device)
        if result is None:
            continue
        rms, stereo = result
        if rms >= threshold:
            found.append({
                "device": device,
                "name": device[14:],
                "rms": rms,
                "stereo": stereo,
                "guitar_pedal": is_guitar_input(device),
            })
    found.sort(key=lambda d: (d["guitar_pedal"], d["rms"]), reverse=True)
    return found


def pick_best_signal_device(found):
    """Choose the device to use from a signal scan: a named guitar/pedal device
    with signal wins; otherwise the loudest device."""
    if not found:
        return None
    return found[0]["device"]


def instrument_menu_entries(devices):
    """Build (label, raw_device) entries for the instrument input menu.

    Likely guitar/bass interfaces and USB effects pedals are sorted first and
    tagged with ``(guitar/pedal)`` so they are easy to find when the pedal is
    plugged in next to the built-in mics.
    """
    ordered = sorted(devices, key=lambda d: not is_guitar_input(d[14:]))
    entries = []
    for device in ordered:
        label = device[14:]
        if is_guitar_input(label):
            label += " (guitar/pedal)"
        entries.append((label, device))
    return entries


def detect_guitar_inputs(devices=None):
    """Return capture devices that look like a guitar/bass line-in.

    ``devices`` may be a list of device-name strings (for tests); otherwise
    the real cyal capture device list is scanned. A generic "USB Audio
    Device" could be either a USB mic or a USB guitar - name-based detection
    only catches devices that say what they are, which is why the manual
    "Select instrument input device" menu also exists.
    """
    if devices is None:
        try:
            cap = cyal.CaptureExtension()
            devices = list(cap.devices)
        except Exception:
            return []
    return [d for d in devices if is_guitar_input(d)]
