import threading
import time
import queue
import cyal.exceptions
from pyogg import OpusEncoder, OpusDecoder
import cyal
from . import consts
from .speech import speak
from . import options
from . import logger

import audioop
import collections
import struct

# ============================================================================
# SOFT LIMITER - Prevents audio clipping when multiple speakers overlap
# Used by professional audio software to prevent distortion
# ============================================================================

# Per-sender smoothed limiter gain (attack/release). The OLD limiter derived
# its pre_scale from every packet's OWN peak, so adjacent 20ms packets got
# different scaling -> gain steps at frame boundaries (audible 'kee-kee'
# ticking on loud, continuous content like music and guitar). Now the gain
# reduction is smoothed across packets: fast attack (~1 frame) when a peak
# needs taming, slow release (~1s) back to unity, exactly like a hardware
# limiter. No more 50Hz gain pumping.
_limiter_gain_state = {}

def soft_limit_audio(audio_bytes, threshold=0.85, ratio=8.0, state_key=None):
    """
    Soft limiter with a smoothed per-stream gain.

    Computes the gain this packet needs to keep its peak under control, but
    applies it through a per-stream attack/release state so the scaling does
    not jump between 20ms packets. This removes the frame-boundary gain steps
    that caused a faint ticking/'kee-kee' noise on music and guitar, while
    still preventing clipping when multiple streams combine.

    Args:
        audio_bytes: Raw 16-bit PCM audio data (MONO16)
        threshold: Level (0.0-1.0) above which limiting starts
        ratio: Compression ratio above threshold
        state_key: Per-sender key for the smoothed gain state. When None, the
                   packet's own target gain is applied directly (stateless).

    Returns:
        Limited audio bytes
    """
    try:
        samples = list(struct.unpack(f'<{len(audio_bytes)//2}h', audio_bytes))
        max_val = 32767
        threshold_val = int(max_val * threshold)

        peak = max(abs(s) for s in samples) if samples else 1

        # Target gain for THIS packet: bring the peak down to
        # threshold + 1/ratio of the excess (soft knee).
        if peak > threshold_val:
            over = peak - threshold_val
            target_peak = threshold_val + over / ratio
            target_gain = min(target_peak / peak, 1.0)
        else:
            target_gain = 1.0

        if state_key is not None:
            prev = _limiter_gain_state.get(state_key, 1.0)
            if target_gain < prev:
                # Fast attack toward the (lower) target.
                gain = prev + (target_gain - prev) * 0.6
            else:
                # Slow release back to unity (~1s, no pumping).
                gain = prev + (target_gain - prev) * 0.02
            _limiter_gain_state[state_key] = gain
        else:
            gain = target_gain

        # Apply the smoothed gain to every sample (uniform scaling, no
        # per-sample knee steps that create harmonic distortion).
        limited = []
        for sample in samples:
            v = int(sample * gain)
            if v > max_val:
                v = max_val
            elif v < -max_val:
                v = -max_val
            limited.append(v)
        return struct.pack(f'<{len(limited)}h', *limited)
    except Exception:
        # If anything fails, return original
        return audio_bytes


# ============================================================================
# DE-CLICK HELPERS - short linear ramps so silence padding and source restarts
# don't create step discontinuities (audible 'กี่ๆ' clicks)
# ============================================================================

# 2ms at 48kHz mono = 96 samples.
FADE_SAMPLES = 96

def _fade_in_packet(packet, samples=FADE_SAMPLES):
    """Ramp the first `samples` samples of a MONO16 packet from 0 to full.

    Used when a source (re)starts so the first buffer doesn't click.
    """
    n = len(packet) // 2
    if n == 0:
        return packet
    samples = min(samples, n)
    data = bytearray(packet)
    for i in range(samples):
        pos = i * 2
        raw = struct.unpack_from('<h', data, pos)[0]
        struct.pack_into('<h', data, pos, int(raw * (i / samples)))
    return bytes(data)

def _fade_out_from_tail(packet, tail_sample, samples=FADE_SAMPLES):
    """Build a silence packet that ramps from `tail_sample` down to 0.

    The first silence frame after real audio starts at the last real sample
    value and fades to digital silence, so the audio->silence transition
    doesn't click.
    """
    n = len(packet) // 2
    if n == 0:
        return packet
    samples = min(samples, n)
    data = bytearray(b'\x00' * len(packet))
    for i in range(samples):
        pos = i * 2
        struct.pack_into('<h', data, pos, int(tail_sample * (1.0 - i / samples)))
    return bytes(data)

def _tail_sample(packet):
    """Last MONO16 sample value of a packet (for de-click ramps)."""
    n = len(packet) // 2
    if n == 0:
        return 0
    return struct.unpack_from('<h', packet, (n - 1) * 2)[0]

# ============================================================================
# PROFESSIONAL JITTER BUFFER FOR MEGAPHONE
# 
# How professional VoIP apps handle multiple speakers:
# 1. Jitter Buffer: Collect packets before playing (absorbs network jitter)
# 2. Fixed Playback Rate: Play at exact 20ms intervals using timer
# 3. Packet Dropping: Drop OLD packets, always play NEWEST audio
# 4. Pre-buffering: Wait for N packets before starting playback
# ============================================================================

class MegaphoneJitterBuffer:
    """
    Professional jitter buffer for megaphone voice chat.
    Uses adaptive buffering techniques common to real-time voice systems.
    """
    
    # === CONFIGURATION ===
    FRAME_SIZE = 1920           # 20ms at 48kHz mono (960 samples * 2 bytes)
    FRAME_DURATION_MS = 20      # Each Opus frame is 20ms
    # Smooth pre-buffer: FOUR 20ms frames (80ms) before first playback.  The
    # old 2-frame (40ms) buffer underran on steady streams like the music bot
    # broadcast (its sender cadence drifts a fraction of a ms per frame, which
    # drains the buffer after a few seconds -> a small 'chop' in the PA).  80ms
    # absorbs that drift; PA latency this small is inaudible for listeners.
    PRE_BUFFER_FRAMES = 4       # Wait for 4 frames (80ms) before playing
    # After an underrun, re-buffer a couple of frames before resuming instead
    # of streaming one frame at a time (which sounds like repeated tiny chops
    # on continuous music).
    RESUME_FRAMES = 2           # Re-buffer 2 frames (40ms) after an underrun
    MAX_BUFFER_FRAMES = 12      # Maximum frames in buffer (240ms) for network stability
    TARGET_BUFFER_FRAMES = 4    # Target buffer level (80ms latency)
    
    def __init__(self, game):
        self.game = game
        self.lock = threading.Lock()
        
        # Packet queue (deque for O(1) append/popleft)
        self.packet_queue = collections.deque(maxlen=self.MAX_BUFFER_FRAMES)
        
        # Playback state
        self.is_playing = False
        self._underrun = False
        self.frames_received = 0
        self.last_pop_time = 0.0
        
        # Timing
        self.last_output_time = 0
        
        # Statistics (for debugging)
        self.packets_received = 0
        self.packets_played = 0
        self.packets_dropped = 0
    
    def add_packet(self, audio_data):
        """
        Add a packet to the jitter buffer.
        Uses "tail drop" - when buffer is full, newest audio replaces oldest.
        """
        with self.lock:
            self.packets_received += 1
            self.frames_received += 1
            
            # If buffer is full, old packets are automatically dropped (maxlen)
            if len(self.packet_queue) >= self.MAX_BUFFER_FRAMES:
                self.packets_dropped += 1
            
            self.packet_queue.append(audio_data)
    
    def get_packet(self):
        """
        Get the next packet to play.
        Returns None if buffer is not ready (pre-buffering) or empty.
        """
        with self.lock:
            current_time = time.time()
            
            # If we have been silent/empty for a long time (>300ms), reset pre-buffering state
            if self.is_playing and current_time - self.last_pop_time > 0.3:
                # A new Megaphone transmission (or Music Bot resume) must not
                # inherit PCM from the previous segment.  Keeping those frames
                # lets old music play alongside the resumed stream and makes
                # the PA image sound as if cabinets have shifted.
                latest_packet = self.packet_queue[-1] if self.packet_queue else None
                self.packet_queue.clear()
                if latest_packet is not None:
                    self.packet_queue.append(latest_packet)
                self.is_playing = False
                self._underrun = False
                self.frames_received = 1 if latest_packet is not None else 0
                self.last_output_time = 0.0
            
            # Pre-buffering: Wait until we have enough packets
            if not self.is_playing:
                if len(self.packet_queue) >= self.PRE_BUFFER_FRAMES:
                    self.is_playing = True
                    logger.log(f"[JitterBuffer] Started playback after {self.frames_received} frames")
                else:
                    return None  # Still pre-buffering

            # Minor underrun: the queue ran dry while playing. Mark it so the
            # stream re-buffers a couple of frames before resuming, instead of
            # playing one lonely frame then chopping again (the 'ติดๆขัด' heard
            # on continuous music broadcasts). The first frames are held back
            # until RESUME_FRAMES accumulate, then playback picks up smoothly.
            if len(self.packet_queue) == 0:
                self._underrun = True
                return None
            if self._underrun:
                if len(self.packet_queue) < self.RESUME_FRAMES:
                    return None  # keep buffering for a smooth resume
                self._underrun = False
                logger.log(f"[JitterBuffer] Resumed playback after {len(self.packet_queue)} frames")

            # Get next packet
            self.packets_played += 1
            self.last_pop_time = current_time
            return self.packet_queue.popleft()
    
    def should_output(self):
        """
        Check if we should output a frame (fixed 20ms intervals).
        This ensures consistent playback regardless of when packets arrive.
        """
        current_time = time.time() * 1000
        if current_time - self.last_output_time >= self.FRAME_DURATION_MS:
            self.last_output_time = current_time
            return True
        return False
    
    def get_buffer_level(self):
        """Get current buffer level in frames"""
        return len(self.packet_queue)
    
    def reset(self):
        """Reset the jitter buffer"""
        with self.lock:
            self.packet_queue.clear()
            self.is_playing = False
            self._underrun = False
            self.frames_received = 0

# Per-source jitter buffers (one per megaphone speaker)
_jitter_buffers = {}
_speaker_delay_queues = {}
_last_play_times = {}
_last_packet_times = {}
# Last packet arrival time per normal-voice channel (key "vc:<channelID>") for
# the adaptive jitter margin in the shared-channel playback path.
_voice_last_pkt = {}

# Measured inter-arrival jitter (ms) per sender - fast-attack peak hold with
# time-based decay (the standard adaptive-jitter-buffer approach). Drives the
# adaptive PA margin: steady 20ms streams stay at the 20ms minimum, jittery
# networks grow the margin just enough to avoid crackle, and a clean network
# automatically returns to the minimum after a couple of seconds.
_speaker_jitter_ms = {}
_speaker_jitter_ts = {}

def _measure_speaker_jitter(sender_id, prev_time, now_time):
    """Update the jitter estimate (ms) for a sender and return it.

    The estimate is the largest recently-seen excess over the 20ms frame
    cadence (peak hold): the moment a packet arrives late, the estimate jumps
    to that excess so the next resync sizes the margin correctly. It then
    decays with a ~2s half-life, so an old spike is forgotten and the PA
    returns to the minimum latency on its own.
    """
    global _speaker_jitter_ms, _speaker_jitter_ts
    prev_est = _speaker_jitter_ms.get(sender_id, 0.0)
    prev_ts = _speaker_jitter_ts.get(sender_id, now_time)
    elapsed = max(0.0, now_time - prev_ts)
    decayed = prev_est * (0.5 ** (elapsed / 2.0))
    excess = 0.0
    if prev_time and (now_time - prev_time) <= 0.18:
        interval_ms = (now_time - prev_time) * 1000.0
        excess = max(0.0, interval_ms - 20.0)
    est = max(decayed, min(excess, 200.0))
    _speaker_jitter_ms[sender_id] = est
    _speaker_jitter_ts[sender_id] = now_time
    return est

def _adaptive_margin_frames(sender_id):
    """Map a sender's measured jitter to a silence-padding margin in 20ms frames.

    Minimum 2 frames (40ms) for smooth streams - absorbs minor OS and network
    jitter seamlessly. Grows to at most 6 frames (120ms) for high-jitter
    connections.
    """
    global _speaker_jitter_ms
    jitter = _speaker_jitter_ms.get(sender_id, 0.0)
    frames = 2 + int(jitter / 20.0)
    return max(2, min(6, frames))


# ============================================================================
# SONG + LIVE COVER SYNC COMPENSATION
# ============================================================================
# A cover performer hears the song one network leg late, then their playing
# travels a SECOND leg back to the song owner. At the owner's ears the remote
# performance arrives behind the owner's own zero-latency local monitor:
#
#   Piano/drums are MIDI NOTE EVENTS (synthesized at the listener, no jitter
#   buffer):   heard-song (RTT + 40ms jitter) + note travel (RTT) = 2RTT + 40ms
#   Guitar/audio mixes ride the audio stream (jitter floor both ways):
#              2 x (RTT + 40ms) = 2RTT + 80ms
#
# The fix delays the owner's LOCAL song monitor by the same amount so the song
# and the remote band line up. 2RTT + 40ms targets the note-event instruments
# (piano/drums - the common cover setup); audio-stream covers stay within one
# jitter floor (40ms). Only the local 'music' producer is delayed: the owner's
# own instruments stay on the instant path (their players anchor to the
# delayed song themselves, and remote instruments line up automatically). Any
# future producer can opt in by listing its tag here.
_COMP_NOTE_FLOOR_MS = 40.0     # one 40ms receive-path floor (the song B hears)
_COMP_MAX_FRAMES = 12          # 240ms cap on bad networks
_COMP_GAP_RESET_S = 0.5        # clear the FIFO after a feed gap (pause/stop)
_COMP_PRODUCERS = frozenset({"music"})
_measured_rtt_ms = None        # latest ping RTT (auto sampler + F3 key)
_rtt_sampler_started = False
_comp_fifos = {}
_comp_last_feed = {}


def _compensation_frames():
    """20ms frames to delay the local song monitor by (2RTT + 40ms).

    Note-event instruments (piano/drums) reach the owner at 2RTT + 40ms
    (song heard via the audio stream one leg, then the note travels one
    round trip with no jitter buffer), so the local song monitor is held
    back by exactly that. Audio-stream covers (guitar) arrive 40ms later
    and stay within one jitter floor.
    """
    rtt = _measured_rtt_ms or 0.0
    delay_ms = 2.0 * rtt + _COMP_NOTE_FLOOR_MS
    return max(1, min(_COMP_MAX_FRAMES, int(round(delay_ms / 20.0))))


def _ensure_rtt_sampler(game):
    """Lazily start a background ping sampler so compensation adapts to the RTT."""
    global _rtt_sampler_started
    if _rtt_sampler_started:
        return
    _rtt_sampler_started = True

    def _run():
        while True:
            time.sleep(5.0)
            try:
                if not hasattr(game, 'network'):
                    continue
                gp = None
                if hasattr(game, 'stack') and game.stack:
                    for st in reversed(game.stack):
                        if hasattr(st, 'player') and hasattr(st, 'megaphone'):
                            gp = st
                            break
                if gp is None or getattr(gp, 'pingging', False):
                    continue
                gp._auto_ping_inflight = True
                gp.pingging = True
                gp.last_ping_time = time.time()
                game.network.send(consts.CHANNEL_PING, "ping", {})
            except Exception:
                pass

    threading.Thread(target=_run, daemon=True).start()

def get_jitter_buffer(game, source_id):
    """Get or create jitter buffer for a specific audio source"""
    global _jitter_buffers
    if source_id not in _jitter_buffers:
        _jitter_buffers[source_id] = MegaphoneJitterBuffer(game)
    return _jitter_buffers[source_id]

def reset_jitter_buffers():
    """Reset all jitter buffers and delay queues"""
    global _jitter_buffers, _speaker_delay_queues, _last_play_times, _last_packet_times, _speaker_jitter_ms, _speaker_jitter_ts
    global _last_tail_sample, _just_padded, _limiter_gain_state, _voice_last_pkt
    _jitter_buffers = {}
    _speaker_delay_queues = {}
    _last_play_times = {}
    _last_packet_times = {}
    _voice_last_pkt = {}
    _speaker_jitter_ms = {}
    _speaker_jitter_ts = {}
    _last_tail_sample = {}
    _just_padded = {}
    _limiter_gain_state = {}

# Last real-audio tail sample per sender (for de-click ramps) and a flag
# marking that the sender's stream just resumed after silence padding.
_last_tail_sample = {}
_just_padded = {}

# Shared OpenAL Buffer Pool to recycle buffers and eliminate allocations / memory leaks
_shared_buffer_pool = []
_MAX_BUFFER_POOL_SIZE = 256

def _recycle_buffers(buffers):
    """Return one or more processed OpenAL buffers back into the shared pool."""
    if buffers is None:
        return
    global _shared_buffer_pool
    try:
        if isinstance(buffers, (list, tuple)):
            for b in buffers:
                if b is not None and len(_shared_buffer_pool) < _MAX_BUFFER_POOL_SIZE:
                    _shared_buffer_pool.append(b)
        else:
            if len(_shared_buffer_pool) < _MAX_BUFFER_POOL_SIZE:
                _shared_buffer_pool.append(buffers)
    except Exception:
        pass

def _get_buffer_from_pool(audio_mngr):
    """Retrieve an OpenAL buffer from the pool, or allocate a new one if empty."""
    global _shared_buffer_pool
    while _shared_buffer_pool:
        buf = _shared_buffer_pool.pop()
        if buf is not None:
            return buf
    try:
        if audio_mngr and hasattr(audio_mngr, 'context') and audio_mngr.context:
            return audio_mngr.context.gen_buffer()
    except Exception:
        pass
    return None

# Track active megaphone speakers for dynamic ducking
_active_megaphone_speakers = 0
_last_speaker_update = 0

def get_active_speaker_count():
    """Get number of currently active megaphone speakers"""
    global _active_megaphone_speakers
    return max(1, _active_megaphone_speakers)

def update_active_speakers(count):
    """Update active speaker count for dynamic volume ducking"""
    global _active_megaphone_speakers, _last_speaker_update
    import time
    current_time = time.time()
    _active_megaphone_speakers = count
    _last_speaker_update = current_time


class voice_chat_compression(threading.Thread):
    def __init__(self, game, channel=None):
        try:
            super().__init__(daemon=True)
            self.game = game
            self.channel = channel if channel is not None else consts.CHANNEL_VOICECHAT
            self.queue = queue.SimpleQueue()
            self.encoder = OpusEncoder()
            self.encoder.set_application('voip')
            self.encoder.set_channels(1)
            self.encoder.set_sampling_frequency(48000)
            self.decoder = OpusDecoder()
            self.decoder.set_channels(1)
            self.decoder.set_sampling_frequency(48000)
            self.start()
            logger.log(f"VoiceChatCompression initialized for channel {self.channel}")
        except Exception as e:
            logger.log_exception(e, "voice_chat_compression.__init__")
            
    def set_channel(self, channel):
        self.channel = channel
        logger.log(f"VoiceChatCompression switched to channel {self.channel}")

    def put(self, value):
        self.queue.put_nowait(value)
    
    def run(self):
        logger.log(f"VoiceChatCompression thread started: {self.channel}")
        while True:
            try:
                time.sleep(0.002)
                if self.queue.empty(): continue
                value = self.queue.get_nowait()
                if value is None: 
                    logger.log(f"VoiceChatCompression stopping: {self.channel}")
                    break
                if callable(value):
                    value()
                if isinstance(value, bytearray):
                    # Apply Mic Gain
                    mic_gain = options.get("megaphone_mic_volume", 100)
                    if mic_gain != 100:
                        try:
                            value = audioop.mul(bytes(value), 2, mic_gain / 100.0)
                        except Exception as e:
                            logger.log(f"[Voice] Error applying gain: {e}")
    
                    buf = self.encoder.encode(value)
                    self.game.network.send(
                        self.channel,
                        "n/a",
                        buf
                    )
            except Exception as e:
                logger.log_exception(e, f"voice_chat_compression.run (Channel {self.channel})")



    def recieve(self, data, vc_source, radio_source, channelID, gameplay, sender_id=None):
        self.put(lambda: self.recieve2(data, vc_source, radio_source, channelID, gameplay, sender_id))

    def recieve2(self, data, vc_source, radio_source, channelID, gameplay, sender_id=None):
        buffer = None
        data = bytearray(self.decoder.decode(bytearray(data)))
        
        with self.game.audio_mngr.context.batch():
            if not gameplay.player.dead:
                # Handle single source or list of sources (for Megaphone Quadraphonic)
                sources = vc_source if isinstance(vc_source, list) else [vc_source]
                
                # === MEGAPHONE: Use Jitter Buffer for smooth playback ===
                if channelID == consts.CHANNEL_MEGAPHONE:
                    # Filter out network echo for local speaker (handled via direct zero-latency sidechain feed)
                    local_name = getattr(getattr(gameplay, 'player', None), 'name', None)
                    local_id = getattr(getattr(gameplay, 'player', None), 'id', None)
                    if sender_id is not None and ((local_name and str(sender_id) == str(local_name)) or (local_id and str(sender_id) == str(local_id))):
                        return

                    # Safety-net limiter before the mixer sums this sender with others.
                    # Per-sender smoothed gain: attack/release so the scaling
                    # doesn't step between 20ms packets (no 'kee-kee' ticking).
                    limited_data = soft_limit_audio(bytes(data), threshold=0.85, ratio=8.0, state_key=sender_id)
                    
                    # Single jitter buffer per sender — ensures all speakers play the same frame simultaneously
                    buffer_key = sender_id if sender_id is not None else "megaphone_shared"
                    jb = get_jitter_buffer(self.game, buffer_key)
                    jb.add_packet(limited_data)

                    # Arrival bookkeeping runs for EVERY packet (even ones that
                    # stay buffered): the jitter estimate and "fresh burst"
                    # detection must see the real arrival cadence.
                    global _last_play_times, _speaker_delay_queues, _last_packet_times
                    current_time = time.time()
                    last_pkt_time = _last_packet_times.get(sender_id, 0.0)
                    _last_packet_times[sender_id] = current_time

                    if hasattr(gameplay, 'megaphone') and hasattr(gameplay.megaphone, 'player_sources'):
                        if sender_id in gameplay.megaphone.player_sources:
                            gameplay.megaphone.player_sources[sender_id]['last_active'] = current_time

                    if current_time - last_pkt_time > 0.18:
                        global _speaker_jitter_ms, _speaker_jitter_ts, _limiter_gain_state, _last_tail_sample, _just_padded
                        _speaker_jitter_ms[sender_id] = 0.0
                        _speaker_jitter_ts[sender_id] = current_time
                        # Fresh transmission: reset smoothed limiter gain and
                        # de-click state so playback starts clean.
                        _limiter_gain_state.pop(sender_id, None)
                        _last_tail_sample.pop(sender_id, None)
                        _just_padded.pop(sender_id, None)
                    else:
                        _measure_speaker_jitter(sender_id, last_pkt_time, current_time)

                    # CLOCK-DRIVEN OUTPUT: hand a frame to the speakers at most
                    # once per 20ms wall-clock (should_output), NOT once per
                    # packet arrival. A network burst leaves the extras buffered
                    # to play out at the steady cadence; a late packet is
                    # absorbed by the pre-buffer. Popping on arrival made the PA
                    # cadence track network jitter — the intermittent "ติดๆขัดๆ"
                    # chop heard on music broadcasts.
                    if not jb.should_output():
                        return
                    packet = jb.get_packet()
                    if packet is None:
                        return  # Still pre-buffering, no speaker plays yet

                    # Update last play time
                    _last_play_times[sender_id] = time.time()

                    # Queue and delay the frame for each speaker
                    queue_and_delay_frame(gameplay, sender_id, sources, packet)
                    return  # Megaphone handled, skip normal processing
                
                # === NORMAL VOICE CHAT: Direct playback with an adaptive
                # jitter margin (like the megaphone). The old fixed 100ms pad
                # (5 x 20ms) made every cold-start burst - a guitarist's first
                # strum after a pause, a new sentence - land 100ms late. The
                # pad is now 1 + margin frames: 20ms minimum, growing only
                # while the network actually shows jitter, decaying on its own.
                vc_key = "vc:%s" % channelID
                _now = time.time()
                _last_pkt = _voice_last_pkt.get(vc_key, 0.0)
                _voice_last_pkt[vc_key] = _now
                if _last_pkt and _now - _last_pkt > 0.18:
                    # Fresh burst after a silence gap: reset to the minimum
                    # instead of counting the silence as jitter (otherwise a
                    # guitarist's pause between strums would look like 300ms
                    # of jitter and inflate the margin for the next burst).
                    _speaker_jitter_ms[vc_key] = 0.0
                    _speaker_jitter_ts[vc_key] = _now
                elif _last_pkt:
                    _measure_speaker_jitter(vc_key, _last_pkt, _now)
                margin_frames = _adaptive_margin_frames(vc_key)

                sources_to_play = []
                for idx, src in enumerate(sources):
                    try:
                        while src.buffers_processed > 0:
                            result = src.unqueue_buffers()
                            _recycle_buffers(result)
                    except Exception:
                        pass
                    
                    buf = _get_buffer_from_pool(self.game.audio_mngr)
                    if buf is None:
                        continue
                    
                    try:
                        buf.set_data(data, sample_rate=48000, format=cyal.BufferFormat.MONO16)
                    except Exception:
                        continue

                    try: 
                        # Adaptive jitter padding at cold start: 1 + margin
                        # frames (20ms floor, up to 120ms under jitter) instead
                        # of the old fixed 100ms.
                        if src.buffers_queued == 0:
                            silence_data = bytes(len(data))
                            for _ in range(margin_frames):
                                s_buf = _get_buffer_from_pool(self.game.audio_mngr)
                                if s_buf is not None:
                                    s_buf.set_data(silence_data, sample_rate=48000, format=cyal.BufferFormat.MONO16)
                                    src.queue_buffers(s_buf)
                                
                        src.queue_buffers(buf)
                    except cyal.exceptions.InvalidOperationError: 
                        continue

                    if src.state == cyal.SourceState.STOPPED or src.state == cyal.SourceState.INITIAL:
                        sources_to_play.append((idx, src))
                
                for i, (idx, src) in enumerate(sources_to_play):
                    try:
                        src.play()
                    except Exception:
                        pass
            
            # Skip radio processing for CHANNEL_MEGAPHONE (no radio, global broadcast only)
            if channelID == consts.CHANNEL_MEGAPHONE: return
            
            if not gameplay.voice_channels[channelID].has_radio or not gameplay.player.has_radio: return
            try:
                if radio_source.buffers_processed > 0:
                    result = radio_source.unqueue_buffers()
                    _recycle_buffers(result)
            except Exception:
                pass
            buffer = _get_buffer_from_pool(self.game.audio_mngr)
            if buffer is not None:
                try:
                    buffer.set_data(data, sample_rate=48000, format=cyal.BufferFormat.MONO16)
                    radio_source.queue_buffers(buffer)
                except Exception:
                    pass
            if radio_source.state == cyal.SourceState.STOPPED or radio_source.state == cyal.SourceState.INITIAL: radio_source.play()





def _feed_local_megaphone_direct(gameplay, raw_buf, producer='producer'):
    """Feed zero-latency local mic/music/instrument PCM to the local Megaphone PA.

    Every local producer (music bot, mic, guitar, ...) gets its OWN per-player
    source set, keyed '<player>:<producer>'. Simultaneous local streams therefore
    play as SEPARATE OpenAL sources that OpenAL mixes together, instead of
    sharing one queue where 20ms slices interleaved: the shared queue received
    30ms of audio per 20ms (music 20 + mic 10), so the delay kept climbing
    while talking and both streams played stretched/squeezed with clicks at
    slice boundaries. Separate sources keep each stream on its own cadence —
    no interleaving, no queue growth, and zero added latency (each feed is
    queued immediately, exactly like the original direct path).

    producer: a tag identifying the caller ('mic', 'music', 'guitar', ...).
    """
    try:
        if not (gameplay and hasattr(gameplay, 'megaphone') and gameplay.megaphone):
            return
        if not hasattr(gameplay.megaphone, 'get_megaphone_player_sources'):
            return
        local_id = getattr(getattr(gameplay, 'player', None), 'id', None) or getattr(getattr(gameplay, 'player', None), 'name', 'local')
        # Separate source set per producer so concurrent local streams mix in
        # OpenAL instead of interleaving frames into one queue.
        local_key = f"{local_id}:{producer}"
        sources = gameplay.megaphone.get_megaphone_player_sources(local_key)
        if not sources:
            return



        # Force local player's volume to instantly reach target volume to avoid fade-in delay
        if hasattr(gameplay.megaphone, 'player_sources') and local_key in gameplay.megaphone.player_sources:
            entry = gameplay.megaphone.player_sources[local_key]
            for i in range(len(entry.get('currents_vol', []))):
                if entry['currents_vol'][i] <= 0.05 and i < len(entry.get('targets_vol', [])):
                    entry['currents_vol'][i] = entry['targets_vol'][i]

        # Per-producer smoothed limiter state (each stream limits independently).
        limited_data = soft_limit_audio(bytes(raw_buf), threshold=0.85, ratio=8.0, state_key=f"local_pa:{producer}")
        for idx, src in enumerate(sources):
            if src:
                # Set gain directly just in case update_megaphone_audio hasn't run yet
                if getattr(src, 'gain', 0.0) <= 0.05 and hasattr(gameplay.megaphone, 'player_sources') and local_key in gameplay.megaphone.player_sources:
                    entry = gameplay.megaphone.player_sources[local_key]
                    if idx < len(entry.get('targets_vol', [])):
                        src.gain = entry['targets_vol'][idx]
                _queue_packet_to_source(gameplay, idx, src, limited_data)
    except Exception:
        pass


class VoiceChatRecord(threading.Thread):
    def __init__(self, game, player):
        super().__init__(daemon=True)
        self.game = game
        self.player = player
        self.capture_ext = cyal.CaptureExtension()
        device = options.get("audio_input_device", 'system default')
        if device == 'system default': device = self.capture_ext.default_device.decode('utf-8')
        self.stereo = False
        self.audio_input = None
        device_encoded = device.encode()
        for fmt, is_stereo in ((cyal.BufferFormat.MONO16, False), (cyal.BufferFormat.STEREO16, True)):
            try:
                self.audio_input = self.capture_ext.open_device(name=device_encoded, sample_rate=48000, format=fmt)
                self.stereo = is_stereo
                break
            except (cyal.exceptions.DeviceNotFoundError, TypeError):
                pass
        
        if not self.audio_input:
            speak(f"Failed to load audio device: {device}")
        self.vc_compression = voice_chat_compression(self.game)
        self.recording = False
        self.running = True
        self.start()
    

    def run(self):
        accumulated_bytes = bytearray()
        while self.running:
            time.sleep(0.0005)
            if not self.recording:
                accumulated_bytes.clear()
                continue
            if self.audio_input is None or not options.get("microphone", True) or not options.get("voice_chat", True):
                accumulated_bytes.clear()
                continue
            samples = self.audio_input.available_samples
            if samples >= 480:  # 10ms ultra-fast hardware capture
                is_stereo = getattr(self, 'stereo', False)
                chunk = bytearray(samples * (4 if is_stereo else 2))
                self.audio_input.capture_samples(chunk)
                
                if is_stereo:
                    import numpy as np
                    mono_arr = np.frombuffer(chunk, dtype=np.int16).reshape(-1, 2).mean(axis=1).astype(np.int16)
                    chunk = bytearray(mono_arr.tobytes())
                
                # Resolve gameplay directly
                gp = getattr(self.player, 'gameplay', None)
                if gp is None and hasattr(self.game, 'stack'):
                    for st in reversed(self.game.stack):
                        if hasattr(st, 'player') and hasattr(st, 'megaphone'):
                            gp = st
                            break
                
                voice_using_mega = getattr(gp, 'voice_chat_using_megaphone', False) if gp else False

                # Check if Music Bot is streaming to Megaphone
                music_bot = None
                if hasattr(self.game, 'stack'):
                    for st in reversed(self.game.stack):
                        if hasattr(st, 'music_bot') and st.music_bot:
                            music_bot = st.music_bot
                            break

                # When the mic is routed into the music bot's broadcast mix, the
                # mixed stream is fed to the local PA sidechain by the streamer
                # (zero latency). Feeding the raw mic here as well would double
                # the broadcaster's own voice through the speakers.
                # Voice is only mixed into the music bot broadcast when the
                # megaphone is ACTUALLY in use (PA Test Mode or the megaphone
                # weapon): otherwise a music bot broadcasting to the PA would
                # hijack the performer's normal voice chat and blast it through
                # the speakers too.
                route_to_bot = bool(
                    music_bot and music_bot.playing
                    and music_bot.broadcast_enabled and music_bot.broadcast_to_megaphone
                    and voice_using_mega
                )

                # Feed local sidechain immediately with 10ms chunk for zero-latency response
                if voice_using_mega and gp and not route_to_bot:
                    _feed_local_megaphone_direct(gp, chunk, producer='mic')

                # Accumulate for Opus encoder (requires 20ms / 1920 bytes)
                accumulated_bytes.extend(chunk)
                while len(accumulated_bytes) >= 1920:
                    chunk_bytes = accumulated_bytes[:1920]
                    accumulated_bytes = accumulated_bytes[1920:]

                    if route_to_bot:
                        if not hasattr(music_bot, 'mic_pcm_queue'):
                            music_bot.mic_pcm_queue = collections.deque(maxlen=10)
                        music_bot.mic_pcm_queue.append(bytes(chunk_bytes))
                    else:
                        from . import consts
                        target_channel = consts.CHANNEL_MEGAPHONE if voice_using_mega else consts.CHANNEL_VOICECHAT
                        if getattr(self.vc_compression, 'channel', None) != target_channel:
                            if hasattr(self.vc_compression, 'set_channel'):
                                self.vc_compression.set_channel(target_channel)
                            else:
                                self.vc_compression.channel = target_channel
                        self.vc_compression.put(bytearray(chunk_bytes))

    def voice_chat_finish(self):
        self.voice_chat_finish2()
    
    def voice_chat_finish2(self):
        if self.audio_input.available_samples < 960: return self.audio_input.capture_samples(bytearray(self.audio_input.available_samples*2))
        buf = bytearray(1920)
        self.audio_input.capture_samples(buf)
        
        # Check if Music Bot is streaming to Megaphone
        music_bot = None
        if hasattr(self.game, 'stack'):
            for st in reversed(self.game.stack):
                if hasattr(st, 'music_bot') and st.music_bot:
                    music_bot = st.music_bot
                    break

        # Voice is mixed into the music bot broadcast only when this recording
        # session actually used the megaphone channel - otherwise a music bot
        # broadcasting to the PA would hijack normal voice chat. The compression
        # channel is the reliable per-session truth (PA Test Mode / megaphone
        # weapon = 30, normal = 20).
        route_to_bot = bool(
            music_bot and music_bot.playing
            and music_bot.broadcast_enabled and music_bot.broadcast_to_megaphone
            and getattr(self.vc_compression, 'channel', None) == consts.CHANNEL_MEGAPHONE
        )

        if route_to_bot:
            if not hasattr(music_bot, 'mic_pcm_queue'):
                music_bot.mic_pcm_queue = collections.deque(maxlen=10)
            music_bot.mic_pcm_queue.append(bytes(buf))
        else:
            # Send the tail chunk on the same channel this recording session used
            # (the compression's channel is already set to it by run()). Re-deriving
            # it from the music bot / voice_chat_using_megaphone here is wrong: by
            # the time finish2 runs (40ms after stop) the megaphone flag is already
            # reset to False, so the last 20ms would leak onto CHANNEL_VOICECHAT and
            # the megaphone compression would be permanently re-pointed at it.
            target_channel = getattr(self.vc_compression, 'channel', None) or consts.CHANNEL_VOICECHAT
            if getattr(self.vc_compression, 'channel', None) != target_channel:
                if hasattr(self.vc_compression, 'set_channel'):
                    self.vc_compression.set_channel(target_channel)
                else:
                    self.vc_compression.channel = target_channel
            self.vc_compression.put(bytearray(buf))
    
    def close(self):
        self.vc_compression.put(None)
        self.running = False


class MusicCompression(threading.Thread):
    PRE_BUFFER_FRAMES = 8   # 160ms before first play
    RESUME_FRAMES     = 3   # 60ms before resuming after underrun

    # Max gap between two packets before we treat the next packet as the start
    # of a brand-new broadcast.  When a broadcaster stops and restarts music,
    # this gap lets the receiver reset exactly like a fresh map load instead of
    # trying to resume a stale, stopped source (which stays silent).
    SESSION_RESET_GAP = 1.0

    def __init__(self, game):
        try:
            super().__init__(daemon=True)
            self.game = game
            self.queue = queue.SimpleQueue()
            from pyogg import OpusDecoder
            self.decoder = OpusDecoder()
            self.decoder.set_channels(1)
            self.decoder.set_sampling_frequency(48000)
            self._has_started = False
            self._last_recv_time = None
            self._running = True
            self.start()
        except Exception as e:
            logger.log_exception(e, "MusicCompression.__init__")

    def put(self, value):
        if getattr(self, '_running', True):
            self.queue.put_nowait(value)

    def close(self):
        self._running = False
        self.queue.put_nowait(None)

    def run(self):
        while getattr(self, '_running', True):
            try:
                time.sleep(0.002)
                if self.queue.empty(): continue
                value = self.queue.get_nowait()
                if value is None: break
                if callable(value):
                    value()
            except Exception as e:
                print(f"[ERROR MusicCompression.run] {e}")
                logger.log_exception(e, "MusicCompression.run")

    def recieve(self, data, music_source, radio_source, channelID, gameplay):
        if music_source is None:
            return
        self.put(lambda: self.recieve_actual(data, music_source, radio_source, channelID, gameplay))

    def recieve_actual(self, data, music_source, radio_source, channelID, gameplay):
        if music_source is None:
            return
            
        # Decode Opus packet OUTSIDE the batch lock to prevent GIL/OpenAL deadlocks!
        try:
            pcm = bytearray(self.decoder.decode(bytearray(data)))
        except Exception as e:
            if not hasattr(self, '_last_err'): self._last_err = 0
            if time.time() - self._last_err > 1.0:
                print(f"[ERROR MusicCompression] Opus decoding failed: {e}")
                self._last_err = time.time()
            return

        try:
            with self.game.audio_mngr.context.batch():
                if gameplay.player.dead:
                    return

                now = time.time()
                is_new_session = (
                    self._last_recv_time is None
                    or (now - self._last_recv_time) > self.SESSION_RESET_GAP
                )
                if is_new_session:
                    # New broadcast (or first after a stop): discard everything
                    # queued on the source — including silent keep-alive buffers
                    # that entity.loop() pushes when the queue runs empty — and
                    # reset the pre-buffer threshold so playback starts cleanly,
                    # mirroring the behaviour of a fresh map load.
                    try:
                        music_source.stop()
                        while getattr(music_source, 'buffers_processed', 0) > 0:
                            music_source.unqueue_buffers()
                    except Exception:
                        pass
                    self._has_started = False
                self._last_recv_time = now

                try:
                    state = music_source.state
                except Exception:
                    state = cyal.SourceState.STOPPED

                # If we were playing but just hit an underrun and STOPPED, we need to
                # flush out the old processed buffers and restart the pre-buffering phase.
                if state == cyal.SourceState.STOPPED and self._has_started:
                    try:
                        self._has_started = False
                        while getattr(music_source, 'buffers_processed', 0) > 0:
                            music_source.unqueue_buffers()
                    except Exception:
                        pass

                # Recycle or generate buffer
                # Only recycle if we are actively playing. If we are in STOPPED/INITIAL state,
                # all buffers are marked as "processed" by OpenAL, so unqueuing them would 
                # destroy our pre-buffer before it ever reaches the playback threshold!
                buf = None
                if self._has_started:
                    try:
                        while getattr(music_source, 'buffers_processed', 0) > 0:
                            result = music_source.unqueue_buffers()
                            if result is not None:
                                if isinstance(result, (list, tuple)):
                                    if buf is None and len(result) > 0:
                                        buf = result[0]
                                else:
                                    if buf is None:
                                        buf = result
                    except Exception:
                        pass

                if buf is None:
                    try:
                        buf = self.game.audio_mngr.context.gen_buffer()
                    except Exception as e:
                        print(f"[ERROR MusicCompression] gen_buffer failed: {e}")
                        return

                # Fill and queue
                try:
                    buf.set_data(bytes(pcm), sample_rate=48000, format=cyal.BufferFormat.MONO16)
                    music_source.queue_buffers(buf)
                except Exception as e:
                    if not hasattr(self, '_last_err2'): self._last_err2 = 0
                    if time.time() - self._last_err2 > 1.0:
                        print(f"[ERROR MusicCompression] queue_buffers failed: {e}")
                        self._last_err2 = time.time()
                    return

                queued = getattr(music_source, 'buffers_queued', 0)

                # Start or resume playback
                if state == cyal.SourceState.STOPPED or state == cyal.SourceState.INITIAL:
                    threshold = self.PRE_BUFFER_FRAMES if not self._has_started else self.RESUME_FRAMES
                    if queued >= threshold:
                        try:
                            if getattr(music_source, 'gain', 0.0) < 0.2:
                                music_source.gain = 1.0
                            music_source.play()
                            self._has_started = True
                        except Exception:
                            pass

        except Exception as e:
            logger.log_exception(e, "MusicCompression.recieve")


def _queue_packet_to_source(gameplay, idx, src, play_packet, force_concert_mode=None):
    # DE-CLICK: if this source is (re)starting, ramp the first samples up from
    # zero so the restart doesn't pop (underrun recovery clicks).
    try:
        if src.state == cyal.SourceState.STOPPED or src.state == cyal.SourceState.INITIAL:
            play_packet = _fade_in_packet(play_packet)
    except Exception:
        pass

    try:
        while src.buffers_processed > 0:
            result = src.unqueue_buffers()
            _recycle_buffers(result)
    except Exception:
        pass
    
    buf = _get_buffer_from_pool(gameplay.game.audio_mngr)
    if buf is None:
        return
    
    try:
        buf.set_data(play_packet, sample_rate=48000, format=cyal.BufferFormat.MONO16)
        src.queue_buffers(buf)
    except Exception:
        pass
    
    # Start playing if stopped
    if src.state == cyal.SourceState.STOPPED or src.state == cyal.SourceState.INITIAL:
        # Pre-buffer cushion: Ensure at least 2 buffers (40ms) are queued
        # before starting playback to prevent instant 20ms underrun jitter.
        try:
            if src.buffers_queued < 2:
                cushion_buf = _get_buffer_from_pool(gameplay.game.audio_mngr)
                if cushion_buf is not None:
                    cushion_buf.set_data(b'\x00' * len(play_packet), sample_rate=48000, format=cyal.BufferFormat.MONO16)
                    src.queue_buffers(cushion_buf)
        except Exception:
            pass

        # Re-apply EFX effects before playing using the source's unique filter
        is_concert = getattr(gameplay, 'concert_spectator_mode', False)
        
        if not is_concert:
            spk_idx = idx // 2
            is_reflection = (idx % 2 == 1)
            if hasattr(gameplay, 'megaphone') and hasattr(gameplay.megaphone, 'speaker_data') and spk_idx < len(gameplay.megaphone.speaker_data):
                speaker_data = gameplay.megaphone.speaker_data[spk_idx]
                
                # Lookup unique filter belonging to this source
                filter_to_apply = None
                if hasattr(gameplay, 'megaphone') and hasattr(gameplay.megaphone, 'player_sources'):
                    for entry in gameplay.megaphone.player_sources.values():
                        if 'sources' in entry and src in entry['sources']:
                            src_idx = entry['sources'].index(src)
                            if 'filters' in entry and src_idx < len(entry['filters']):
                                filter_to_apply = entry['filters'][src_idx]
                            break
                
                if filter_to_apply is None and hasattr(gameplay, 'megaphone') and hasattr(gameplay.megaphone, 'fading_sources'):
                    for fade_obj in gameplay.megaphone.fading_sources:
                        if 'sources' in fade_obj and src in fade_obj['sources']:
                            src_idx = fade_obj['sources'].index(src)
                            if 'filters' in fade_obj and src_idx < len(fade_obj['filters']):
                                filter_to_apply = fade_obj['filters'][src_idx]
                            break
                
                # Fallback to physical templates
                if filter_to_apply is None:
                    filter_to_apply = speaker_data.get('refl_filter' if is_reflection else 'filter')

                if hasattr(gameplay.game.audio_mngr, 'efx'):
                    if hasattr(gameplay, 'megaphone') and hasattr(gameplay.megaphone, 'eq_slot') and gameplay.megaphone.eq_slot:
                        gameplay.game.audio_mngr.efx.send(src, 0, gameplay.megaphone.eq_slot, filter=filter_to_apply)
                    if speaker_data.get('reverb_slot'):
                        gameplay.game.audio_mngr.efx.send(src, 1, speaker_data['reverb_slot'], filter=filter_to_apply)
                    if hasattr(gameplay, 'megaphone') and hasattr(gameplay.megaphone, 'compressor_slot') and gameplay.megaphone.compressor_slot:
                        gameplay.game.audio_mngr.efx.send(src, 2, gameplay.megaphone.compressor_slot, filter=filter_to_apply)
                
                if filter_to_apply:
                    try:
                        src.direct_filter = filter_to_apply
                    except:
                        pass
        try:
            src.play()
        except:
            pass


def _pad_frames_for_resync(target_active, current_active, needs_initial_delay, any_starved):
    """How many silence frames to pad for one speaker this packet.

    Always pad up to target_active (frames_delay + margin) to preserve the exact
    inter-speaker propagation delay stagger both on initial stream setup AND during
    underrun recovery. This permanently prevents the stereo soundstage from collapsing
    from wide stereo to merged mono.
    """
    if not needs_initial_delay and not any_starved:
        return 0
    return max(0, target_active - current_active)


def queue_and_delay_frame(gameplay, sender_id, sources, packet):

    global _speaker_delay_queues
    import math
    
    # Get player (listener) position from camera focus object
    try:
        player_pos = (gameplay.camera.focus_object.x, gameplay.camera.focus_object.y, gameplay.camera.focus_object.z)
    except AttributeError:
        player_pos = (0.0, 0.0, 0.0)
        
    global _speaker_last_calc_time, _speaker_current_delays, _speaker_initial_delays, _speaker_delay_cache_expires
    global _last_tail_sample, _just_padded
    if '_speaker_last_calc_time' not in globals():
        _speaker_last_calc_time = {}
        _speaker_current_delays = {}
        _speaker_initial_delays = {}
        _speaker_delay_cache_expires = {}

    try:
        player_pos = (gameplay.camera.focus_object.x, gameplay.camera.focus_object.y, gameplay.camera.focus_object.z)
    except AttributeError:
        player_pos = (0.0, 0.0, 0.0)

    now = time.time()

    # Sources are removed after a short idle period, such as a music Stop.
    # Keep their delay baseline briefly so a replay does not unexpectedly take
    # its origin from the listener's new position.  Expired entries are pruned
    # only when megaphone audio next arrives.
    for cached_sender, expires_at in list(_speaker_delay_cache_expires.items()):
        if expires_at > now:
            continue
        for cache in (
            _speaker_last_calc_time,
            _speaker_current_delays,
            _speaker_initial_delays,
        ):
            cache.pop(cached_sender, None)
        _speaker_delay_cache_expires.pop(cached_sender, None)
    _speaker_delay_cache_expires.pop(sender_id, None)

    # 1. Unqueue all processed buffers and count active buffers to get the true playhead position
    active_counts = []
    for idx, src in enumerate(sources):
        if src is None:
            active_counts.append(0)
            continue
        try:
            while src.buffers_processed > 0:
                result = src.unqueue_buffers()
                # Buffer recycling is handled by _queue_packet_to_source;
                # here we only need to unqueue so active_counts is accurate.
        except:
            pass
        active_counts.append(src.buffers_queued)

    # 2. Detect sources that ran dry.  A pause/resume can make every source run
    # dry, but that must not change where its propagation delay originated.
    any_starved = False
    
    for i, count in enumerate(active_counts):
        if sources[i] is not None and count == 0:
            any_starved = True
            break
            
    has_initial_delays = sender_id in _speaker_initial_delays
    needs_initial_delay = not has_initial_delays
    # The propagation-delay baseline is FROZEN at the stream's start position.
    # Re-basing it on every walk step and re-padding at the next recovery made
    # the inter-cabinet stagger flip between merged (same quantized 20ms frame)
    # and separated (one frame apart) as the listener moved - the PA image kept
    # 'แยกบ้าง รวมบ้าง' with no rhyme or reason. The listener's position is
    # still tracked continuously by the spatial GAINS (volume/occlusion refresh
    # + per-frame LERP); the delay stagger stays put so the image is
    # deterministic. Fresh streams and underrun recovery still rebuild the
    # stagger from the frozen baseline.
    needs_resync = needs_initial_delay or any_starved
    
    # 3. A source that ran dry needs silence padding again, but it keeps the
    # existing propagation delay.  Recalculate that delay only for a fresh
    # source (pause/resume intentionally retains the old delay).
    if needs_resync:
        _speaker_current_delays[sender_id] = []
        if needs_initial_delay:
            _speaker_initial_delays[sender_id] = {}
        
        for idx, src in enumerate(sources):
            if src is None:
                _speaker_current_delays[sender_id].append(0)
                continue
                
            spk_idx = idx // 2
            is_reflection = (idx % 2 == 1)
            
            static_delay = 0.0
            speaker_pos = (0.0, 0.0, 0.0)
            
            if hasattr(gameplay, 'megaphone') and hasattr(gameplay.megaphone, 'speaker_data') and spk_idx < len(gameplay.megaphone.speaker_data):
                spk_data = gameplay.megaphone.speaker_data[spk_idx]
                static_delay = spk_data.get('delay', 0.0)
                speaker_pos = spk_data.get('position', (0.0, 0.0, 0.0))
                
            if getattr(gameplay, 'concert_spectator_mode', False):
                _speaker_initial_delays[sender_id][idx] = 0.0
                propagation_delay = 0.0
                static_delay = 0.0
            elif needs_initial_delay or idx not in _speaker_initial_delays[sender_id]:
                # Recalculate propagation delay from the stream's ORIGIN position
                # (speed of sound = 343 m/s). Computed once for a fresh source;
                # pause/resume and listener movement intentionally retain it so
                # the spatial stagger never jumps on its own.
                if not is_reflection:
                    dx = player_pos[0] - speaker_pos[0]
                    dy = player_pos[1] - speaker_pos[1]
                    dz = player_pos[2] - speaker_pos[2]
                    distance = math.sqrt(dx*dx + dy*dy + dz*dz)
                    propagation_delay = distance / 343.0
                else:
                    ground_level = gameplay.map.minz if hasattr(gameplay, 'map') and hasattr(gameplay.map, 'minz') else 0.0
                    dist_spk_to_ground = abs(speaker_pos[2] - ground_level)
                    dx = player_pos[0] - speaker_pos[0]
                    dy = player_pos[1] - speaker_pos[1]
                    dz = player_pos[2] - ground_level
                    dist_ground_to_player = math.sqrt(dx*dx + dy*dy + dz*dz)
                    distance = dist_spk_to_ground + dist_ground_to_player
                    propagation_delay = distance / 343.0
                _speaker_initial_delays[sender_id][idx] = propagation_delay
            else:
                propagation_delay = _speaker_initial_delays[sender_id][idx]
                
            total_delay = static_delay + propagation_delay
            frames_delay = int(total_delay / 0.02)  # Convert to 20ms frames
            _speaker_current_delays[sender_id].append(frames_delay)
            
            # Adaptive jitter margin: start at the minimum (1 frame / 20ms) and
            # grow toward the cap (6 frames / 120ms) only when this sender's
            # packet arrival actually shows jitter (measured in recieve2). A
            # steady stream - music bot, live guitar relay, or voice at the
            # normal 20ms cadence - stays at 20ms so the PA feels as immediate
            # as normal voice chat; jittery networks get just enough extra
            # buffering to avoid crackle.
            margin_frames = _adaptive_margin_frames(sender_id)
            
            # Instantly push silence frames to restore perfect spatial stagger and jitter margin
            target_active = frames_delay + margin_frames
            current_active = active_counts[idx]
            pad_frames = _pad_frames_for_resync(target_active, current_active, needs_initial_delay, any_starved)

            if pad_frames > 0:
                # DE-CLICK: the first silence frame ramps from the last real
                # audio sample down to zero instead of jumping straight to
                # digital silence (which clicked at the audio->silence edge).
                prev_tail = _last_tail_sample.get(sender_id, 0)
                for p in range(pad_frames):
                    silence_packet = bytes(len(packet))
                    if p == 0:
                        silence_packet = _fade_out_from_tail(silence_packet, prev_tail)
                    _queue_packet_to_source(gameplay, idx, src, silence_packet)
                    
                    # Also pad fading sources
                    if hasattr(gameplay, 'megaphone') and hasattr(gameplay.megaphone, 'fading_sources'):
                        for fade_obj in gameplay.megaphone.fading_sources:
                            if fade_obj['sid'] == sender_id and idx < len(fade_obj['sources']):
                                f_src = fade_obj['sources'][idx]
                                if f_src:
                                    _queue_packet_to_source(gameplay, idx, f_src, silence_packet)
                # Remember that this sender just came out of a silence gap so
                # the next real packet can fade in instead of clicking.
                _just_padded[sender_id] = True
                    
    _speaker_last_calc_time[sender_id] = now
    
    # 4. Queue the actual audio packet to all sources
    # DE-CLICK: if the stream just resumed after silence padding, ramp the
    # first samples up from zero (silence->audio edge) to avoid a click.
    packet_to_queue = packet
    if _just_padded.pop(sender_id, False):
        packet_to_queue = _fade_in_packet(packet)
    for idx, src in enumerate(sources):
        if src is not None:
            _queue_packet_to_source(gameplay, idx, src, packet_to_queue)
    # Track the tail sample of the last real audio for the next de-click ramp.
    _last_tail_sample[sender_id] = _tail_sample(packet_to_queue)
            
    # Process Crossfade for fading sources
    if hasattr(gameplay, 'megaphone') and hasattr(gameplay.megaphone, 'fading_sources'):
        for fade_obj in gameplay.megaphone.fading_sources:
            if fade_obj['sid'] == sender_id:
                elapsed = now - fade_obj['fade_start']
                if elapsed <= fade_obj['fade_duration']:
                    t = elapsed / fade_obj['fade_duration']
                    for idx, f_src in enumerate(fade_obj['sources']):
                        if f_src:
                            start_vol = fade_obj['start_vols'][idx] if idx < len(fade_obj['start_vols']) else 1.0
                            f_src.gain = max(0.0, start_vol * (1.0 - t))
                            _queue_packet_to_source(gameplay, idx, f_src, packet)


def tick_megaphone_delay(gameplay):
    global _last_play_times, _speaker_delay_queues, _last_packet_times
    current_time = time.time()
    
    if not hasattr(gameplay, 'megaphone') or not hasattr(gameplay.megaphone, 'player_sources') or not gameplay.megaphone.player_sources:
        return
        
    for sender_id, entry in list(gameplay.megaphone.player_sources.items()):
        sources = entry['sources']
        last_time = _last_play_times.get(sender_id, 0)
        last_pkt_time = _last_packet_times.get(sender_id, 0)

        # Only tick if we haven't received a network packet for at least 40ms (flushing phase)
        # This prevents the tick loop from interfering with active network speech playback
        if current_time - last_pkt_time >= 0.04:
            if current_time - last_time >= 0.02:
                has_delayed_audio = False
                for idx, src in enumerate(sources):
                    if src is None: continue
                    queue_key = (sender_id, idx)
                    if queue_key in _speaker_delay_queues and len(_speaker_delay_queues[queue_key]) > 0:
                        has_delayed_audio = True
                        play_packet = _speaker_delay_queues[queue_key].popleft()
                        _queue_packet_to_source(gameplay, idx, src, play_packet)
                        
                        # Process Crossfade for fading sources
                        if hasattr(gameplay, 'megaphone') and hasattr(gameplay.megaphone, 'fading_sources'):
                            for fade_obj in gameplay.megaphone.fading_sources:
                                if fade_obj['sid'] == sender_id:
                                    elapsed = current_time - fade_obj['fade_start']
                                    if elapsed <= fade_obj['fade_duration']:
                                        t = elapsed / fade_obj['fade_duration']
                                        if idx < len(fade_obj['sources']):
                                            f_src = fade_obj['sources'][idx]
                                            if f_src:
                                                start_vol = fade_obj['start_vols'][idx] if idx < len(fade_obj['start_vols']) else 1.0
                                                f_src.gain = max(0.0, start_vol * (1.0 - t))
                                                _queue_packet_to_source(gameplay, idx, f_src, play_packet, force_concert_mode=fade_obj['is_concert'])
                        
                if has_delayed_audio:
                    _last_play_times[sender_id] = current_time
