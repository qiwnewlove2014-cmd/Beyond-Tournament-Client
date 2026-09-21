"""Second capture device for instrument input (e.g. a guitar/bass line-in).

Kept fully independent from the voice-chat microphone: it reads its own
`audio_instrument_input_device` option and opens its own cyal capture
device, so both can run at the same time.

Two chunk sizes live in here and they answer two different questions. What
*leaves* the machine -- the voice channel, the megaphone, the music bot mix,
the pitch tracker -- is a 20 ms frame (``FRAME_SAMPLES``), the chunk Opus and
the voice path have always used. What the *player's own ear* gets is a 10 ms
chunk (``MONITOR_CHUNK_SAMPLES``), because that is the size the capture device
actually hands over (measured: 480 samples every 10 ms on a WASAPI shared-mode
endpoint) and asking for two of those before anything moved cost the player
about 15 ms they can hear when a string is struck. Everything read lands in
bounded ring buffers: ``frames`` for the monitor, assembled 20 ms frames for
everything that leaves.
"""
import collections
import contextlib
import threading
import time

import cyal
import numpy as np

from . import logger, options, pitch, speech

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

    A monitor is judged by *how soon* the player hears their own string, so the
    audio handed over but not yet played is bounded. The capture arrives at
    exactly the rate the source plays, which means a queue can only ever grow:
    a stalled main thread (a map reload, a slow frame) leaves frames waiting
    that are still heard the length of that stall later, for the rest of the
    session -- nothing later in the song makes the ear catch up. Past
    ``MONITOR_QUEUE_LIMIT_MS`` the wait is given up and the newest chunk is
    played instead: a skip heard once, rather than a guitar that keeps drifting
    away from the hand that plays it.
    """

    # How far behind the ear the monitor may fall before the backlog is let go.
    # The natural depth is one chunk (~10 ms), so this tolerates a couple of
    # dropped frames and gives up on anything worse.
    MONITOR_QUEUE_LIMIT_MS = 50
    SKIP_LOG_INTERVAL_S = 5.0

    def __init__(self, audio_mngr):
        self.audio_mngr = audio_mngr
        self.source = None
        self.queue_limit_samples = int(self.MONITOR_QUEUE_LIMIT_MS * 48000 // 1000)
        self._waiting = collections.deque()  # samples handed over, oldest first
        self._waiting_samples = 0
        self.skips = 0
        self._last_skip_log = 0.0

    def _ensure_source(self):
        if self.source is None:
            self.source = self.audio_mngr.context.gen_source()
            self.source.looping = False

    def set_position(self, x, y, z):
        if self.source is not None:
            with contextlib.suppress(Exception):
                self.source.position = (x, y, z)

    def feed(self, pcm):
        """Queue one mono16 chunk; keep the source drained."""
        if not pcm:
            return
        self._ensure_source()
        try:
            self._release_played()
            samples = len(pcm) // 2
            if self._waiting_samples + samples > self.queue_limit_samples:
                # The ear is already late for this audio: dropping the wait is
                # the only way back to the hand (there is no way to hurry it).
                self._let_backlog_go()
            buf = self.audio_mngr.context.gen_buffer()
            buf.set_data(pcm, sample_rate=48000,
                         format=cyal.BufferFormat.MONO16)
            self.source.queue_buffers(buf)
            self._waiting.append(samples)
            self._waiting_samples += samples
            if self.source.state in (cyal.SourceState.STOPPED,
                                     cyal.SourceState.INITIAL):
                self.source.play()
        except Exception:
            pass

    def _release_played(self):
        """Let go of the chunks the ear has already been given.

        The count is read *before* the release and the queue follows it: cyal's
        ``unqueue_buffers()`` hands back every processed buffer in one call
        (``max=INT_MAX``), so counting calls rather than buffers would leave
        the bookkeeping holding chunks that are long gone -- and a backlog that
        is really one chunk deep would look deeper than the limit forever.
        """
        played = self.source.buffers_processed
        if played:
            self.source.unqueue_buffers()
        for _ in range(played):
            if self._waiting:
                self._waiting_samples -= self._waiting.popleft()
        if self._waiting_samples < 0:
            self._waiting_samples = 0

    def _let_backlog_go(self):
        """Play the newest audio instead of trailing behind the player.

        ``stop()`` is what makes this possible: a queued buffer that has not
        played is not releasable (OpenAL answers InvalidOperation), and a
        stopped source has played everything, so its whole queue can be handed
        back in one go.
        """
        waiting = len(self._waiting)
        self._waiting.clear()
        self._waiting_samples = 0
        if not waiting:
            return
        self.source.stop()
        for _ in range(waiting):
            if self.source.buffers_queued <= 0:
                break
            self.source.unqueue_buffers()
        self.skips += 1
        now = time.monotonic()
        if now - self._last_skip_log >= self.SKIP_LOG_INTERVAL_S:
            self._last_skip_log = now
            logger.log("[INSTRUMENT] monitor fell behind and dropped the "
                       f"waiting audio to stay by the ear (skips: {self.skips})")

    def close(self):
        if self.source is not None:
            with contextlib.suppress(Exception):
                self.source.stop()
            self.source = None
        self._waiting.clear()
        self._waiting_samples = 0


class InstrumentInput(threading.Thread):
    """Owns one OpenAL capture device used as an instrument line-in."""

    FRAME_SAMPLES = 960          # 20 ms at 48 kHz, same chunk as voice chat
    MONITOR_CHUNK_SAMPLES = 480  # 10 ms: what the player's own ear is handed
    MIN_READ_SAMPLES = 48        # 1 ms: never make the driver wait longer
    FRAME_BUFFER_FRAMES = 200    # ~2 s of 10 ms monitor chunks
    NOTE_BUFFER_NOTES = 64

    def __init__(self, game):
        super().__init__(daemon=True)
        self.game = game
        self.capture_ext = cyal.CaptureExtension()
        self.audio_input = None
        self.stereo = False
        self._open(options.get("audio_instrument_input_device", "system default"))
        self.frames = collections.deque(maxlen=self.FRAME_BUFFER_FRAMES)
        # Bytes read but not yet a full chunk/frame. Two boundaries over one
        # stream: the monitor's 10 ms, the relay's 20 ms.
        self._monitor_pending = bytearray()
        self._relay_pending = bytearray()
        self.tracker = pitch.PitchTracker()
        self.notes = collections.deque(maxlen=self.NOTE_BUFFER_NOTES)
        # Raw guitar Opus streamed on the normal 3D voice channel (so chords
        # are heard near the player without needing the music bot broadcast).
        self._guitar_voice = None
        self.recording = False
        # Set when the capture handle dies under us (see :meth:`_device_died`):
        # the capture worker cannot speak, so the next main-thread frame does.
        self.device_error = None
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
            except (cyal.exceptions.CyalError, TypeError):
                # CyalError, not only DeviceNotFoundError: a device that is
                # listed but cannot be opened (in use, removed between the
                # list and the click) raises one of the other subclasses, and
                # the fallback to STEREO16 is worth trying for those too.
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
        """Begin capturing into the ring buffer. False if it could not start.

        A capture handle is only good for the call that used it: a USB
        interface unplugged while the game runs leaves an OpenAL error behind
        rather than a device, and letting that escape into the key press that
        asked for it (or into the toggle that switched the guitar on) is the
        same crash the microphone path had. A handle OpenAL refuses is retired
        here, so the next attempt opens a fresh one from the same name.
        """
        if self.audio_input is None:
            return False
        try:
            self.audio_input.start()
        except cyal.exceptions.CyalError as exc:
            self._device_died(exc, "start")
            return False
        self.recording = True
        return True

    def stop_recording(self):
        """Stop capturing; a device that died on the way out is let go."""
        self.recording = False
        if self.audio_input is None:
            return
        try:
            self.audio_input.stop()
        except cyal.exceptions.CyalError as exc:
            self._device_died(exc, "stop")

    def release_device(self):
        """Let go of the capture handle without caring whether it still lives.

        The one place that drops ``audio_input``, so nothing else has to call
        ``stop()`` on a handle that may already be dead (see
        :meth:`_device_died`). Safe to call from the main thread at any time.
        """
        device, self.audio_input = self.audio_input, None
        self.recording = False
        if device is not None:
            with contextlib.suppress(Exception):
                device.stop()

    def _device_died(self, exc, where):
        """Retire a capture handle OpenAL refused, and remember why.

        The reason is kept for the main thread: this runs on the capture
        worker (or inside a key press), and a session that goes quiet with
        nothing on screen is how a guitar "just stops working".
        """
        self.device_error = f"Instrument input device stopped working ({where})"
        logger.log(f"[INSTRUMENT] {self.device_error}: {exc}")
        self.release_device()

    def take_device_error(self):
        """The device failure nobody has reported yet, once (main thread)."""
        message, self.device_error = self.device_error, None
        return message

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
            try:
                ready = self.audio_input.available_samples
            except cyal.exceptions.CyalError as exc:
                # The handle is good only for the call that used it. Retire it
                # and stay alive: this thread is what every later session needs,
                # and dying here is a guitar that goes quiet for no stated
                # reason. The failure is reported by the next frame (see
                # take_device_error).
                self._device_died(exc, "capture")
                continue
            if ready >= self.MIN_READ_SAMPLES:
                # One monitor chunk at most per read; a read that finds a
                # backlog still hands it over 10 ms at a time and the rest is
                # drained on the next pass, half a millisecond later. The ear
                # wants the newest audio, never a large block of old audio.
                # cyal counts frames for both formats: mono16 frames are 2
                # bytes, stereo16 frames are 4 bytes (L+R pairs).
                take = min(ready, self.MONITOR_CHUNK_SAMPLES)
                buf = bytearray(take * (4 if self.stereo else 2))
                try:
                    self.audio_input.capture_samples(buf)
                except cyal.exceptions.CyalError as exc:
                    self._device_died(exc, "capture")
                    continue
                if self.stereo:
                    raw = _downmix_stereo(buf)
                else:
                    raw = bytes(buf)
                self._stage(raw)

    def _stage(self, raw):
        """Sort one capture read into what the ear gets and what leaves.

        Both buffers hold the same bytes; only the boundary differs, and the
        monitor's is the shorter one: it is handed a chunk the moment 10 ms of
        audio exists, without waiting for the 20 ms frame that chunk is part
        of -- the frame the voice channel, the megaphone, the music bot mix and
        the pitch tracker have always been sent.
        """
        self._monitor_pending += raw
        self._relay_pending += raw
        chunk_bytes = self.MONITOR_CHUNK_SAMPLES * 2
        while len(self._monitor_pending) >= chunk_bytes:
            self.frames.append(bytes(self._monitor_pending[:chunk_bytes]))
            del self._monitor_pending[:chunk_bytes]
        frame_bytes = self.FRAME_SAMPLES * 2
        while len(self._relay_pending) >= frame_bytes:
            buf16 = bytes(self._relay_pending[:frame_bytes])
            del self._relay_pending[:frame_bytes]
            self._emit_frame(buf16)

    def _emit_frame(self, buf16):
        """Send one full 20 ms frame on its way (the path that always existed).

        The local monitor is deliberately not fed from here: it was already
        handed the two 10 ms chunks this frame is made of, the moment each of
        them existed (see :meth:`_stage`).
        """
        # Check for Megaphone routing
        gp = None
        if hasattr(self.game, 'stack'):
            for st in reversed(self.game.stack):
                if hasattr(st, 'player') and hasattr(st, 'megaphone'):
                    gp = st
                    break
        voice_using_mega = getattr(gp, 'voice_chat_using_megaphone', False) if gp else False

        # Route the raw guitar audio into the music bot broadcast. The
        # guitar joins the mix when the music broadcast is enabled OR
        # when the performer turned on "Broadcast to Megaphone" in the
        # music bot menu - the megaphone routing is an independent
        # toggle (same rule piano and drums follow), so the guitar
        # reaches the PA speakers just like the other instruments.
        music_bot = self._find_music_bot()
        route_to_bot = bool(music_bot and (
            getattr(music_bot, "broadcast_enabled", False)
            or getattr(music_bot, "broadcast_to_megaphone", False)
        ))
        if route_to_bot:
            if not hasattr(music_bot, "guitar_pcm_queue"):
                music_bot.guitar_pcm_queue = collections.deque(maxlen=10)
            music_bot.guitar_pcm_queue.append(buf16)

        # Feed the raw guitar into the local PA sidechain only when it is
        # NOT being mixed into the music bot broadcast (the streamer feeds
        # the full mix locally itself) - otherwise the guitarist hears
        # their own strum twice through the speakers.
        if voice_using_mega and gp and not route_to_bot:
            from . import voice_chat
            if hasattr(voice_chat, '_feed_local_megaphone_direct'):
                voice_chat._feed_local_megaphone_direct(gp, buf16, producer='guitar')

        # When the guitar rides the bot broadcast mix, let the streamer
        # carry it (3D music bot channel or the megaphone/PA) - do NOT
        # also stream it on the raw voice channel, otherwise nearby
        # players and the PA would hear every strum twice.
        if not route_to_bot:
            self._feed_guitar_voice(buf16, force_mega=voice_using_mega)
        frame = np.frombuffer(buf16, dtype=np.int16).astype(np.float32) / 32768.0
        result = self.tracker.feed(frame)
        if result is not None:
            self.notes.append(result)

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
        """Pop and return all raw mono16 chunks captured since last call.

        These are the monitor's own chunks (``MONITOR_CHUNK_SAMPLES``, 10 ms),
        not the 20 ms frames the voice channel sends: the performer's own ear is
        the one consumer that cannot afford to wait for a whole frame to fill.
        """
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
        if self._guitar_voice is not None:
            try:
                self._guitar_voice.put(None)  # stop its encode/send thread
            except Exception:
                pass
            self._guitar_voice = None
        self.release_device()


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
