import random
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
# A note held for a song waits on the audio the listener is *hearing*, and that
# rule has one home (``libs/jukebox_clock.py``): a member's music feed is an
# output exactly as a cabinet's pair is, so it builds the same clock.
from .jukebox_clock import PAIR_KEY, SpotClock, mono_ms
# A cinema cabinet's room, for the megaphone frames that belong to one (see
# libs/audio/cinema/speech.py). Aliased because libs/speech.py above is the
# text-to-speech announcer and has nothing to do with this.
from .audio.cinema import speech as cinema_speech

import audioop
import collections
import struct

# SOFT LIMITER - prevents clipping when several speakers overlap
# Voice chat, the megaphone PA path and every song feed. The rules, the thread
# ownership and every measured number live in .agents/skills/chat_systems/.

# Per-sender smoothed limiter gain: attack ~1 frame, release ~1 s. Sizing each
# packet from its own peak stepped the gain at 20 ms boundaries ('kee-kee'
# ticking on loud continuous content); smoothing removes the 50 Hz pumping.
_limiter_gain_state = {}
_limiter_gain_lock = threading.Lock()


def audio_limiter_key(namespace, owner):
    """A stable, per-owner limiter key shared by that owner's audio paths."""
    return (str(namespace), id(owner))


def reset_audio_limiter(state_key):
    """Forget smoothing state when the owner of a limited stream is closed."""
    with _limiter_gain_lock:
        _limiter_gain_state.pop(state_key, None)


def _smoothed_limiter_gain(state_key, target_gain):
    if state_key is None:
        return target_gain
    with _limiter_gain_lock:
        prev = _limiter_gain_state.get(state_key, 1.0)
        if target_gain < prev:
            gain = prev + (target_gain - prev) * 0.6
        else:
            gain = prev + (target_gain - prev) * 0.02
        _limiter_gain_state[state_key] = gain
    return gain


def mix_audio_frames(frames, threshold=0.85, ratio=8.0, state_key=None):
    """Sum equally sized mono16 frames in wide precision, then limit once.

    ``audioop.add`` saturates at int16 on every addition, so a limiter applied
    afterwards cannot undo clipping that already occurred in the mix. Keeping
    the sum in Python precision lets the limiter see the true peak first.
    ``frames`` contains ``(pcm_bytes, gain)`` pairs.
    """
    try:
        frames = [(bytes(pcm), float(gain)) for pcm, gain in frames if pcm]
        if not frames:
            return b""
        sample_count = min(len(pcm) // 2 for pcm, _gain in frames)
        if sample_count <= 0:
            return b""
        mixed = [0.0] * sample_count
        for pcm, gain in frames:
            samples = struct.unpack(f"<{sample_count}h", pcm[:sample_count * 2])
            for index, sample in enumerate(samples):
                mixed[index] += sample * gain

        peak = max((abs(sample) for sample in mixed), default=0.0)
        threshold = max(0.0, min(1.0, float(threshold)))
        ratio = max(1.0, float(ratio))
        threshold_val = 32767 * threshold
        if peak > threshold_val:
            target_peak = threshold_val + (peak - threshold_val) / ratio
            target_gain = min(target_peak / peak, 1.0)
        else:
            target_gain = 1.0
        gain = _smoothed_limiter_gain(state_key, target_gain)
        # The smoothed attack is deliberately gradual, but an instantaneous
        # ceiling still prevents a single transient from wrapping/clipping.
        if peak > 0:
            gain = min(gain, 32767 / peak)
        limited = [max(-32768, min(32767, int(sample * gain)))
                   for sample in mixed]
        return struct.pack(f"<{sample_count}h", *limited)
    except Exception:
        # Malformed frames are not allowed to kill the audio worker.
        return b""


def soft_limit_audio(audio_bytes, threshold=0.85, ratio=8.0, state_key=None):
    """Soft limiter with a smoothed per-stream gain.

        `state_key=None` applies the packet's own target gain directly (stateless).
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

        gain = _smoothed_limiter_gain(state_key, target_gain)

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
    """Build a silence packet that ramps from `tail_sample` down to 0, so the
        audio -> silence transition does not click."""
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

# THE SHIPPED VOICE FRAME: 20 ms of 48 kHz mono16 PCM. The mic capture cuts a
# frame the moment this much audio exists (VoiceChatRecord.run), the receive
# path pads in the same unit, and the capture device hands audio over in 10 ms
# periods on this project's machines (measured, tools/instrument_monitor_latency_sim.py),
# which is why the capture trigger is one such period and not a smaller number.
# One home for the numbers the latency budget is made of (tools/voice_latency_sim.py).
VOICE_FRAME_BYTES = 1920                # 20 ms mono16 at 48 kHz
VOICE_FRAME_MS = 20.0
VOICE_CAPTURE_TRIGGER_SAMPLES = 480     # one 10 ms device delivery period
VOICE_SEND_POLL_S = 0.002               # the encode worker's poll (voice_chat_compression.run)

# THE PA'S SEQUENCED UPLOAD (channel 31): version(1) + epoch(4) + frameSeq(4)
# in front of the Opus frame. The Server relays the same header to every
# listener that advertised `pa_timeline_v1`, which is what lets a listener
# CONCEAL a missing frame (Opus PLC) instead of holding a reserve big enough to
# sit through it. tools/megaphone_latency_sim.py measures both sides of that
# trade; the reserve is the number that moves (see _megaphone_margin_frames).
# The wire format itself lives in consts (both ends of channel 31 read it there).
# Frames of a hole that are concealed rather than treated as a restart: the
# same bound the music timeline's upload repair uses.
PA_TIMELINE_MAX_GAP = 9
OPUS_FRAME_MS = 20.0                    # what decode_missing_packet is asked for
# Concealment is a bridge, not a substitute for a stream: after this many
# consecutive concealed frames (100 ms) the playout goes quiet and lets the
# reserve/ re-pad path take over, the way a VoIP receiver stops extrapolating a
# talk spurt that never came back.
PA_TIMELINE_MAX_CONCEAL = 5


def pa_timeline_epoch(epoch=None):
    """The epoch to stamp on a PA upload: the sender's own, made once and kept.

    Random rather than the wall clock, because a listener only re-bases its
    sequence bookkeeping when the epoch CHANGES: two streams can start inside
    the same second (one song replacing another), and a repeated epoch would
    leave the listener refusing the new stream's frames as reordered copies.
    """
    if epoch is None:
        return random.getrandbits(32)
    return int(epoch) & 0xFFFFFFFF


def pa_timeline_upload(opus_frame, epoch, frame_seq):
    """One PA upload framed for channel 31: version + epoch + frameSeq, then Opus.

    The Server inserts the sender's voice channel at offset 1, so the listener
    reads the same header with one extra byte (consts.PA_TIMELINE_HEADER_BYTES).
    This is the only place either end builds it - the player's own PA upload
    (``voice_chat_compression._outgoing_packet``) and the music bot's live
    broadcast both call here, so the two can never drift apart.
    """
    header = bytearray(consts.PA_TIMELINE_UPLOAD_BYTES)
    header[0] = consts.PA_TIMELINE_VERSION
    struct.pack_into('>I', header, 1, int(epoch) & 0xFFFFFFFF)
    struct.pack_into('>I', header, 5, int(frame_seq) & 0xFFFFFFFF)
    return bytes(header) + bytes(opus_frame)


# PROFESSIONAL JITTER BUFFER FOR MEGAPHONE: pre-buffer N packets, then play at a
# fixed 20 ms cadence and always drop old packets in favour of the newest audio.

class MegaphoneJitterBuffer:
    """
    Professional jitter buffer for megaphone voice chat.
    Uses adaptive buffering techniques common to real-time voice systems.
    """
    
    # === CONFIGURATION ===
    FRAME_SIZE = 1920           # 20ms at 48kHz mono (960 samples * 2 bytes)
    FRAME_DURATION_MS = 20      # Each Opus frame is 20ms
    # START GATE ONLY. The PA's stall cover is the fixed source margin that
    # ``queue_and_delay_frame`` queues ahead of the audio in the OpenAL source
    # (``_megaphone_margin_frames``), not this queue depth: a stall is paid out
    # of the source's queued frames, while every frame waited for here is pure
    # start latency in front of the speakers. The old six-frame gate made the
    # two cushions one reserve counted twice (120 + 120 ms).
    # tools/megaphone_latency_sim.py has the candidate table: with the six-frame
    # margin kept, two frames here hold the same starvation counts on every
    # pattern at 160 ms instead of 240 ms (240 -> 160 ms one-way).
    PRE_BUFFER_FRAMES = 2       # Wait for 2 frames (40ms) before playing
    # After an underrun, re-buffer more frames before resuming to prevent
    # rapid re-underrun cycles.
    RESUME_FRAMES = 4           # Re-buffer 4 frames (80ms) after an underrun
    MAX_BUFFER_FRAMES = 16      # Maximum frames in buffer (320ms) for network stability
    TARGET_BUFFER_FRAMES = 4    # Target buffer level (80ms latency)
    
    def __init__(self, game):
        self.game = game
        self.lock = threading.Lock()
        # True once this sender's frames carry a sequence (channel 31). The
        # playout then conceals a missing frame in place, so the re-buffer gate
        # in get_packet must NOT hold the real frames that arrive after a hole.
        self.sequenced = False
        
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

            # Minor underrun: the queue ran dry while playing, so hold the first frames
            # until RESUME_FRAMES accumulate and playback picks up smoothly.
            if len(self.packet_queue) == 0:
                if self.sequenced:
                    # A sequenced stream's missing frame is concealed in place
                    # by the playout, which knows which position is due. This is
                    # not an underrun to wait out: setting the flag would hold
                    # the real frames arriving afterwards for another
                    # RESUME_FRAMES ticks and play all of them late.
                    return None
                self._underrun = True
                return None
            if self._underrun and not self.sequenced:
                if len(self.packet_queue) < self.RESUME_FRAMES:
                    return None  # keep buffering for a smooth resume
                self._underrun = False
                logger.log(f"[JitterBuffer] Resumed playback after {len(self.packet_queue)} frames")

            # Get next packet
            self.packets_played += 1
            self.last_pop_time = current_time
            return self.packet_queue.popleft()
    
    def should_output(self, current_time_ms=None):
        # Output at a fixed 20 ms cadence: advance the deadline by the frame duration instead of the
        # sampled time, so a polling loop that wakes late on Windows cannot accumulate that lateness
        # until the PA source underruns. (Monotonic; tests may inject a deterministic timestamp.)
        current_time = (
            time.perf_counter() * 1000
            if current_time_ms is None else float(current_time_ms)
        )
        if self.last_output_time <= 0:
            self.last_output_time = current_time
            return True

        lateness = current_time - self.last_output_time
        if lateness >= self.FRAME_DURATION_MS:
            if lateness >= self.FRAME_DURATION_MS * 2:
                # The worker was suspended for at least one whole frame. Start
                # a fresh cadence rather than draining queued audio in a burst.
                self.last_output_time = current_time
            else:
                # Preserve the 50 Hz average despite normal scheduler jitter.
                self.last_output_time += self.FRAME_DURATION_MS
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
            self.last_output_time = 0.0

# Per-source jitter buffers (one per megaphone speaker)
_jitter_buffers = {}
_speaker_delay_queues = {}
_last_play_times = {}
_last_packet_times = {}
# Last packet arrival time per normal-voice channel (key "vc:<channelID>") for
# the adaptive jitter margin in the shared-channel playback path.
_voice_last_pkt = {}

# Measured inter-arrival jitter (ms) per sender: a fast-attack peak hold with
# time-based decay, driving the adaptive PA margin (20 ms floor, 6-frame cap).
_speaker_jitter_ms = {}
_speaker_jitter_ts = {}

def _measure_speaker_jitter(sender_id, prev_time, now_time):
    """Update the jitter estimate (ms) for a sender and return it: a peak hold over
            the 20 ms cadence that decays with a ~2 s half-life."""
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
    """Map a sender's measured jitter to a silence-padding margin in 20 ms frames:
            ONE frame (20 ms) on a clean link, growing with measured jitter to at most
            six (120 ms).

            This is the NORMAL voice chat cushion only - the megaphone path keeps its
            fixed v1.6 six-frame reserve (``_megaphone_margin_frames``). A cushion is
            latency paid for every frame of the burst, so the floor is one frame: the
            measured jitter decides when the stream needs a deeper one (the estimate
            attacks on the first late packet), and a >180 ms gap resets it.
            """
    global _speaker_jitter_ms
    jitter = _speaker_jitter_ms.get(sender_id, 0.0)
    frames = 1 + int(jitter / 20.0)
    return max(1, min(6, frames))


# The PA's reserve in 20 ms frames, queued AHEAD of the audio in the source.
# Two numbers, because the two transports have different information:
#
#   SEQUENCED (channel 31) - the frame's position is known, so a hole can be
#   replaced by a concealment frame (Opus PLC) in place. Nothing is paid out of
#   the reserve for a lost 20 ms, so it only has to cover arrival jitter, and
#   tools/megaphone_latency_sim.py measures ZERO starvations for it at 1/3/5%
#   loss on the lossy table, against 0/9/17 for the same pair with no
#   concealment. That is the whole reason channel 31 exists.
#
#   LEGACY (channel 30) - the packet carries only sender id + opus. There is
#   nothing to conceal a hole with, the server relays the listener leg
#   RELIABLY so it can pause for a retransmission, and the reserve is the only
#   thing keeping the speakers fed. It must not be trimmed: four frames or
#   three double and triple the starvation counts on the sim's spikey patterns.
MEGAPHONE_MARGIN_SEQUENCED = 2          # 40 ms
MEGAPHONE_MARGIN_LEGACY = 6             # 120 ms

# Senders whose PA frames carry a sequence (marked by the receive path).
_megaphone_sequenced_senders = set()


def _megaphone_margin_frames(sender_id):
    """Return the PA's silence reserve for one sender, in 20 ms frames.

                    A sender on the sequenced transport has its holes concealed, so it pays the small
                    reserve; a legacy sender keeps the six-frame one. The keys are the sender ids the
                    receive path already uses for per-player sources.
                    """
    if sender_id in _megaphone_sequenced_senders:
        return MEGAPHONE_MARGIN_SEQUENCED
    return MEGAPHONE_MARGIN_LEGACY


# SONG + LIVE COVER SYNC COMPENSATION
# A cover performer hears the song one leg late and their playing travels a second leg back, so
# at the owner's ears the remote performance arrives 2RTT + 40 ms behind the owner's own
# zero-latency local monitor (note-event instruments; audio mixes ride one more 40 ms jitter
# floor). The owner's local song monitor is delayed by that amount. Only the local 'music'
# producer is delayed - a future producer opts in by listing its tag here.
_COMP_NOTE_FLOOR_MS = 40.0     # one 40ms receive-path floor (the song B hears)
_COMP_MAX_FRAMES = 12          # 240ms cap on bad networks
_COMP_GAP_RESET_S = 0.5        # clear the FIFO after a feed gap (pause/stop)
_COMP_PRODUCERS = frozenset({"music"})
_measured_rtt_ms = None        # latest ping RTT (auto sampler + F3 key)
_rtt_sampler_started = False
_comp_fifos = {}
_comp_last_feed = {}


def _compensation_frames():
    """20 ms frames to delay the local song monitor by (2RTT + 40 ms): the delay at
            which a note-event cover (piano/drums) reaches the owner. Audio-stream covers
            (guitar) arrive 40 ms later and stay inside one jitter floor."""
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
    global _megaphone_sequenced_senders
    _megaphone_sequenced_senders = set()
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


def _reclaim_source_buffers(src):
    """Unqueue processed OpenAL buffers and return them to the shared pool."""
    try:
        while src.buffers_processed > 0:
            _recycle_buffers(src.unqueue_buffers())
    except Exception:
        pass


def _play_voice_frame(mngr, sources, data, margin_frames, radio_source, channelID, gameplay):
    """Queue one decoded 20 ms voice frame (and its radio copy) for playback.

                    MAIN THREAD ONLY - called via AudioManager.defer_audio() from
                    voice_chat_compression.recieve2, so no OpenAL call runs on a worker thread.
                    """
    sources_to_play = []
    for idx, src in enumerate(sources):
        try:
            while src.buffers_processed > 0:
                result = src.unqueue_buffers()
                _recycle_buffers(result)
        except Exception:
            pass

        buf = _get_buffer_from_pool(mngr)
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
                    s_buf = _get_buffer_from_pool(mngr)
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
    if channelID == consts.CHANNEL_MEGAPHONE:
        return

    voice_channel = gameplay.voice_channels.get(channelID) if hasattr(gameplay, 'voice_channels') else None
    if not voice_channel or not voice_channel.has_radio or not gameplay.player.has_radio:
        return
    try:
        if radio_source.buffers_processed > 0:
            result = radio_source.unqueue_buffers()
            _recycle_buffers(result)
    except Exception:
        pass
    buffer = _get_buffer_from_pool(mngr)
    if buffer is not None:
        try:
            buffer.set_data(data, sample_rate=48000, format=cyal.BufferFormat.MONO16)
            radio_source.queue_buffers(buffer)
        except Exception:
            pass
    if radio_source.state == cyal.SourceState.STOPPED or radio_source.state == cyal.SourceState.INITIAL: radio_source.play()

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
    def __init__(self, game, channel=None, max_pending_frames=None):
        try:
            super().__init__(daemon=True)
            self.game = game
            self.channel = channel if channel is not None else consts.CHANNEL_VOICECHAT
            self._max_pending_frames = (
                max(1, int(max_pending_frames))
                if max_pending_frames is not None else None
            )
            self.queue = (
                queue.Queue(maxsize=self._max_pending_frames)
                if self._max_pending_frames is not None
                else queue.SimpleQueue()
            )
            self.dropped_frames = 0
            self.encoder = OpusEncoder()
            self.encoder.set_application('voip')
            self.encoder.set_channels(1)
            self.encoder.set_sampling_frequency(48000)
            self.decoder = OpusDecoder()
            self.decoder.set_channels(1)
            self.decoder.set_sampling_frequency(48000)
            # One channel carries several independent Opus senders. This audio
            # worker owns their decoder and clock-driven playout state.
            self._megaphone_decoders = {}
            self._megaphone_playouts = {}
            # Per-sender sequenced-stream state (channel 31): which epoch this
            # stream is, and the frame sequence the playout expects next, so a
            # hole can be concealed instead of paid for out of the reserve.
            self._megaphone_timeline = {}
            # This sender's own PA upload identity (see _outgoing_packet).
            self._pa_epoch = None
            self._pa_seq = 0
            self.running = True
            self.start()
            logger.log(f"VoiceChatCompression initialized for channel {self.channel}")
        except Exception as e:
            logger.log_exception(e, "voice_chat_compression.__init__")
            
    def set_channel(self, channel):
        self.channel = channel
        logger.log(f"VoiceChatCompression switched to channel {self.channel}")

    def _outgoing_packet(self, opus_frame):
        """Return ``(channel, payload)`` for one encoded frame.

                    On the PA, and once the Server has advertised ``pa_timeline_v1``, the upload carries the
                    frame's POSITION (version + epoch + frameSeq) so every listener can tell a missing
                    frame from a late one and conceal it (see ``_megaphone_margin_frames``). Normal voice,
                    and a Server that predates the channel, keep the legacy payload - an old Server would
                    route a channel-31 packet as ordinary proximity voice.
                    """
        if (self.channel != consts.CHANNEL_MEGAPHONE
                or not getattr(self.game, 'pa_timeline_supported', False)):
            return self.channel, opus_frame
        # One epoch per sender session: a listener resets its sequence
        # bookkeeping on a change, and a talker's silence is a gap in the
        # sequence rather than a new stream.
        self._pa_epoch = pa_timeline_epoch(self._pa_epoch)
        payload = pa_timeline_upload(opus_frame, self._pa_epoch, self._pa_seq)
        self._pa_seq = (self._pa_seq + 1) & 0xFFFFFFFF
        return consts.CHANNEL_MEGAPHONE_TIMELINE, payload

    def put(self, value):
        if not getattr(self, 'running', True):
            return
        try:
            self.queue.put_nowait(value)
            return
        except queue.Full:
            pass
        # A guitar is a live edge: if encoding/networking stalls, old frames
        # are less useful than the current strum. Keep its handoff bounded and
        # replace the oldest queued PCM without blocking the capture thread.
        if (getattr(self, "_max_pending_frames", None) is None
                or not isinstance(value, (bytes, bytearray))):
            return
        try:
            self.queue.get_nowait()
        except queue.Empty:
            pass
        else:
            self.dropped_frames = getattr(self, "dropped_frames", 0) + 1
        try:
            self.queue.put_nowait(value)
        except queue.Full:
            self.dropped_frames = getattr(self, "dropped_frames", 0) + 1

    def discard_pending(self):
        """Drop queued live PCM while keeping the reusable worker alive."""
        if getattr(self, "_max_pending_frames", None) is None:
            return
        while True:
            try:
                self.queue.get_nowait()
            except queue.Empty:
                break

    def close(self):
        if not getattr(self, 'running', True):
            return
        self.running = False
        self.discard_pending()
        self.queue.put_nowait(None)

    def _megaphone_decoder(self, sender_id):
        key = sender_id if sender_id is not None else "megaphone_shared"
        decoder = self._megaphone_decoders.get(key)
        if decoder is None:
            decoder = OpusDecoder()
            decoder.set_channels(1)
            decoder.set_sampling_frequency(48000)
            self._megaphone_decoders[key] = decoder
        return decoder

    def _mark_megaphone_sequenced(self, sender_id):
        """Remember that this sender's PA frames carry a sequence.

                    Read by ``_megaphone_margin_frames`` (the reserve the source is padded with) and by
                    ``MegaphoneJitterBuffer.sequenced`` (the re-buffer gate), so the two move together.
                    """
        _megaphone_sequenced_senders.add(sender_id)
        jb = _jitter_buffers.get(sender_id)
        if jb is not None:
            jb.sequenced = True

    def _conceal_frame(self, sender_id):
        """One 20 ms frame of Opus concealment for a frame that never arrived.

                    ``decode_missing_packet`` extrapolates from that sender's own decoder history, which
                    is why it must be their decoder; the result goes through the same limiter the real
                    frames do so the sender's gain stays stable across the boundary.
                    """
        try:
            pcm = self._megaphone_decoder(sender_id).decode_missing_packet(OPUS_FRAME_MS)
        except Exception as e:
            logger.log(f"[Voice] PA concealment failed for {sender_id}: {e}")
            return None
        if not pcm:
            return None
        return soft_limit_audio(bytes(pcm), threshold=0.85, ratio=8.0, state_key=sender_id)

    def _sequenced_pa_gap(self, sender_id, epoch, frame_seq):
        """Return ``(accept, conceal_frames)`` for one sequenced PA arrival."""
        state = self._megaphone_timeline.get(sender_id)
        if state is None:
            state = {'epoch': None, 'next_seq': None, 'concealed': 0}
            self._megaphone_timeline[sender_id] = state
        epoch = None if epoch is None else (int(epoch) & 0xFFFFFFFF)
        frame_seq = int(frame_seq) & 0xFFFFFFFF
        if state['epoch'] != epoch or state['next_seq'] is None:
            # A new stream, or a new epoch: nothing to conceal and nothing to
            # carry over from the stream before it.
            state['epoch'] = epoch
            state['next_seq'] = (frame_seq + 1) & 0xFFFFFFFF
            return True, 0
        delta = (frame_seq - state['next_seq']) & 0xFFFFFFFF
        if delta == 0:
            state['next_seq'] = (frame_seq + 1) & 0xFFFFFFFF
            return True, 0
        if delta >= 0x80000000:
            # Behind the playout: that slot already played a concealment frame
            # (or this is a reordered copy). Rewinding would replay audio.
            return False, 0
        if delta > PA_TIMELINE_MAX_GAP:
            # Too far ahead to be a hole in this stream - the sender restarted
            # its counter, or the playout retired the stream and this is a new
            # one. Re-base instead of concealing a second of audio.
            state['next_seq'] = (frame_seq + 1) & 0xFFFFFFFF
            return True, 0
        state['next_seq'] = (frame_seq + 1) & 0xFFFFFFFF
        return True, delta

    def _drain_megaphone_playout(self, now_ms=None, now_monotonic=None):
        """Drain PA frames at 20 ms cadence independently of packet arrivals."""
        if not self._megaphone_playouts:
            return
        clock_ms = time.perf_counter() * 1000 if now_ms is None else float(now_ms)
        mono_now = time.monotonic() if now_monotonic is None else float(now_monotonic)
        stale = []
        for sender_id, stream in list(self._megaphone_playouts.items()):
            gameplay = stream['gameplay']
            player_sources = getattr(
                getattr(gameplay, 'megaphone', None), 'player_sources', {}
            )
            # A talker standing in a cabinet's room is heard from that room
            # instead of the map's PA, so their stream has no PA sources to
            # check for -- and on a map with no PA speakers at all, that check
            # used to drop the whole stream before a single frame played.
            in_room = cinema_speech.routed(self.game, gameplay, sender_id)
            if (mono_now - stream['last_packet_monotonic'] > 1.0
                    or (sender_id not in player_sources and not in_room)):
                stream['jitter_buffer'].reset()
                stale.append(sender_id)
                continue
            jb = stream['jitter_buffer']
            if not jb.should_output(clock_ms):
                continue
            # Do not invent silence for a packet that merely arrived a little
            # late. A legacy PA payload has no sequence/timestamp, so that guess
            # can mute good music frames: its reserve and the reliable listener
            # leg are what cover it.
            packet = jb.get_packet()
            if packet is None:
                # SEQUENCED (channel 31): this sender's frames carry their
                # position, so the slot that is due right now can be concealed
                # instead of draining the reserve. This - not a bigger cushion -
                # is the difference the loss table in
                # tools/megaphone_latency_sim.py measures.
                if not stream.get('sequenced') or not jb.is_playing:
                    continue
                state = self._megaphone_timeline.get(sender_id)
                if state is None or state.get('next_seq') is None:
                    continue
                if state.get('concealed', 0) >= PA_TIMELINE_MAX_CONCEAL:
                    # A hole this long is not a hole: stop extrapolating and go
                    # quiet until a real frame arrives.
                    continue
                packet = self._conceal_frame(sender_id)
                if packet is None:
                    continue
                state['next_seq'] = (state['next_seq'] + 1) & 0xFFFFFFFF
                state['concealed'] = state.get('concealed', 0) + 1
            try:
                if gameplay.player.dead:
                    continue
                if in_room:
                    # The room's own speakers, not the PA: same cadence, same
                    # frame, shaped by the room's numbers. Still deferred --
                    # source creation, buffer upload and play() are OpenAL.
                    _gp, _sid, _pkt = gameplay, sender_id, packet
                    self.game.audio_mngr.defer_audio(
                        lambda gp=_gp, sid=_sid, pkt=_pkt:
                        cinema_speech.feed(self.game, gp, sid, pkt)
                    )
                    _last_play_times[sender_id] = time.time()
                    continue
                # Hand the frame to the MAIN thread via the audio inbox: OpenAL must only ever
                # be touched from the main thread (cross-thread calls, especially a nested
                # context.batch(), caused native heap corruption and hard crashes). The playout
                # CADENCE stays clock-driven here - only the AL execution moves, one frame later.
                _gp, _sid, _srcs, _pkt = gameplay, sender_id, stream['sources'], packet
                # Default arguments, not a closure over the loop's variables:
                # two talkers handing a frame to the inbox in the same pass
                # would otherwise both fire with the LAST pair's values, and
                # one speaker would get the other's audio.
                self.game.audio_mngr.defer_audio(
                    lambda gp=_gp, sid=_sid, srcs=_srcs, pkt=_pkt:
                    queue_and_delay_frame(gp, sid, srcs, pkt)
                )
                _last_play_times[sender_id] = time.time()
            except Exception as exc:
                logger.log_exception(
                    exc, f"megaphone playout sender={sender_id!r}"
                )
        for sender_id in stale:
            self._megaphone_playouts.pop(sender_id, None)
            self._megaphone_decoders.pop(sender_id, None)
            # The stream is over, so its sequence bookkeeping is too: a later
            # transmission re-reads the epoch and starts clean.
            self._megaphone_timeline.pop(sender_id, None)
            # Destroying the room's sources is OpenAL work too, so it rides
            # the same inbox; a leg that outlived its voice would leave the
            # speakers queued and quiet, not free.
            self.game.audio_mngr.defer_audio(
                lambda sid=sender_id: cinema_speech.drop(self.game, sid)
            )
    
    def run(self):
        logger.log(f"VoiceChatCompression thread started: {self.channel}")
        while getattr(self, 'running', True):
            try:
                time.sleep(VOICE_SEND_POLL_S)
                if not self.queue.empty():
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
                        channel, payload = self._outgoing_packet(buf)
                        self.game.network.send(
                            channel,
                            "n/a",
                            payload
                        )
                self._drain_megaphone_playout()
            except Exception as e:
                logger.log_exception(e, f"voice_chat_compression.run (Channel {self.channel})")
        self.running = False
        self._megaphone_playouts.clear()
        self._megaphone_decoders.clear()



    def recieve(self, data, vc_source, radio_source, channelID, gameplay, sender_id=None,
                epoch=None, frame_seq=None):
        self.put(lambda: self.recieve2(data, vc_source, radio_source, channelID, gameplay,
                                       sender_id, epoch, frame_seq))

    def recieve2(self, data, vc_source, radio_source, channelID, gameplay, sender_id=None,
                 epoch=None, frame_seq=None):
        buffer = None
        decoder = (
            self._megaphone_decoder(sender_id)
            if channelID == consts.CHANNEL_MEGAPHONE else self.decoder
        )
        data = bytearray(decoder.decode(bytearray(data)))
        
        # No context.batch() and no OpenAL calls here: the AL work runs on the main
        # thread via audio_mngr.defer_audio (cross-thread AL usage corrupted memory).
        if gameplay.player.dead:
            return

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
            if frame_seq is None:
                jb.add_packet(limited_data)
            else:
                # SEQUENCED (channel 31): the frame's position is known, so the
                # frames a hole skipped are concealed HERE, in order, before the
                # arrival - they go through the same playout cadence, so the
                # audio content stays whole and nothing is paid out of the
                # reserve (see _megaphone_margin_frames).
                self._mark_megaphone_sequenced(sender_id)
                accept, missing = self._sequenced_pa_gap(sender_id, epoch, frame_seq)
                if not accept:
                    # A reordered copy, or a frame the playout already covered
                    # with concealment: queueing it would replay audio.
                    return
                for _ in range(missing):
                    conceal = self._conceal_frame(sender_id)
                    if conceal is None:
                        break
                    jb.add_packet(conceal)
                jb.add_packet(limited_data)

            # Arrival bookkeeping runs for EVERY packet (even ones that
            # stay buffered): the jitter estimate and "fresh burst"
            # detection must see the real arrival cadence — which is why
            # this stays on the receiving thread, not deferred.
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

            # CLOCK-DRIVEN OUTPUT: at most one frame per 20 ms wall-clock, not one per packet arrival, so a
            # network burst plays out at a steady cadence (popping on arrival tracked the network and caused
            # the intermittent "ติดๆขัดๆ" chop on music broadcasts). The worker drains this jitter buffer
            # every 20 ms even when ENet delivered the packets in a burst.
            state = self._megaphone_timeline.get(sender_id)
            if state is not None and frame_seq is not None:
                # A real frame ends any run of concealment.
                state['concealed'] = 0
            self._megaphone_playouts[sender_id] = {
                'gameplay': gameplay,
                'sources': sources,
                'jitter_buffer': jb,
                'last_packet_monotonic': time.monotonic(),
                'sequenced': frame_seq is not None,
            }
            return  # Megaphone handled, skip normal processing
                
        # NORMAL VOICE CHAT: direct playback with an adaptive jitter margin. The old fixed 5 x 20 ms pad
        # made every cold-start burst - a first strum, a new sentence - land 100 ms late; the pad is now
        # 1 + margin frames (20 ms minimum, growing only while the network shows jitter).
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

        # All OpenAL below (recycle/fill/queue/play for voice + radio) runs
        # on the MAIN thread via the audio inbox.
        mngr = self.game.audio_mngr
        frame_data = bytes(data)
        mngr.defer_audio(lambda: _play_voice_frame(
            mngr, sources, frame_data, margin_frames, radio_source, channelID, gameplay
        ))





def _feed_local_megaphone_direct(gameplay, raw_buf, producer='producer'):
    """Hand local mic/music/instrument PCM to the main-thread PA path.

                    Producers run on capture workers but OpenAL is owned by AudioManager.loop(); the frame is
                    copied before deferring so the producer can reuse its input buffer.
                    """
    try:
        if not gameplay or not getattr(gameplay, 'game', None):
            return
        audio_mngr = getattr(gameplay.game, 'audio_mngr', None)
        if audio_mngr is None:
            return
        frame = bytes(raw_buf)
        # PCM math stays on the producer worker; only cyal/OpenAL ownership is
        # transferred to the main thread. Running the limiter's 960-sample
        # unpack/scan/multiply loop inside AudioManager.loop() would add avoidable
        # work to every render frame.
        frame = soft_limit_audio(
            frame, threshold=0.85, ratio=8.0,
            state_key=f"local_pa:{producer}",
        )
        if threading.current_thread() is threading.main_thread():
            _feed_local_megaphone_main(gameplay, frame, producer)
        else:
            audio_mngr.defer_audio(
                lambda gp=gameplay, pcm=frame, tag=producer:
                    _feed_local_megaphone_main(gp, pcm, tag)
            )
    except Exception:
        pass


def _feed_local_megaphone_main(gameplay, raw_buf, producer='producer'):
    """MAIN THREAD ONLY: queue one local producer frame to the PA sources.

                    Each producer gets its OWN source set, keyed '<player>:<producer>': a shared queue
                    received 30 ms of audio per 20 ms (music 20 + mic 10), so the delay climbed while talking
                    and both streams played stretched with clicks at slice boundaries. Music uses three real
                    PCM frames as a 60 ms start/resume reserve.
                    """
    sources = []
    try:
        if not (gameplay and hasattr(gameplay, 'megaphone') and gameplay.megaphone):
            return
        if not hasattr(gameplay.megaphone, 'get_megaphone_player_sources'):
            return
        local_id = getattr(getattr(gameplay, 'player', None), 'id', None) or getattr(getattr(gameplay, 'player', None), 'name', 'local')
        # Separate source set per producer so concurrent local streams mix in
        # OpenAL instead of interleaving frames into one queue.
        local_key = f"{local_id}:{producer}"
        # A player standing in a cabinet's room hears their own broadcast from that room
        # - the same speakers and numbers everyone else hears it from, and on a map with
        # no PA speakers it is the only thing that can play it. The installer's trims are
        # skipped for the owner (see cinema/speech.py::feed_local).
        if cinema_speech.feed_local(getattr(gameplay, 'game', None), gameplay,
                                    local_key, raw_buf):
            return
        sources = gameplay.megaphone.get_megaphone_player_sources(local_key)
        if not sources:
            return



        # Force local player's volume to instantly reach target volume to avoid fade-in delay
        if hasattr(gameplay.megaphone, 'player_sources') and local_key in gameplay.megaphone.player_sources:
            entry = gameplay.megaphone.player_sources[local_key]
            for i in range(len(entry.get('currents_vol', []))):
                if entry['currents_vol'][i] <= 0.05 and i < len(entry.get('targets_vol', [])):
                    entry['currents_vol'][i] = entry['targets_vol'][i]

        # Set gain directly just in case update_megaphone_audio hasn't run yet
        if hasattr(gameplay.megaphone, 'player_sources') and local_key in gameplay.megaphone.player_sources:
            entry = gameplay.megaphone.player_sources[local_key]
            for idx, src in enumerate(sources):
                if src and idx < len(entry.get('targets_vol', [])):
                    if getattr(src, 'gain', 0.0) <= 0.05:
                        src.gain = entry['targets_vol'][idx]

        # The local monitor rides the SAME per-speaker propagation-delay stagger (distance / 343 m/s) as
        # remote listeners, or every cabinet starts in sync, the precedence effect fuses them, and the
        # owner hears their own broadcast as one speaker. Local frames have no network leg, so no jitter
        # margin is needed.
        #
        # ignore_speaker_delay: an installer's per-speaker `delay` (up to 0.5 s) is an alignment offset
        # for the people standing out there, so it is skipped for the owner's own ears - every local
        # producer, so the owner's voice and the song they sing over stay aligned with each other.
        queue_and_delay_frame(
            gameplay,
            local_key,
            sources,
            raw_buf,
            margin_frames=0,
            real_prebuffer_frames=3 if producer == 'music' else None,
            ignore_speaker_delay=True,
        )
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

    def _find_music_bot(self, gameplay=None):
        """Resolve the map-owned Music Bot independently of open submenus."""
        gp = gameplay or getattr(self.player, 'gameplay', None)
        direct = getattr(gp, 'music_bot', None) if gp else None
        if direct is not None:
            return direct
        if hasattr(self.game, 'stack'):
            try:
                stack_snapshot = tuple(self.game.stack)
            except Exception:
                stack_snapshot = ()
            for state in reversed(stack_snapshot):
                candidate = getattr(state, 'music_bot', None)
                if candidate is not None:
                    return candidate
        return None
    

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
            try:
                samples = self.audio_input.available_samples
            except cyal.exceptions.CyalError:
                # The microphone vanished mid-recording (device unplugged or
                # disabled). Stop recording quietly; the next voice key press
                # reports the problem and rebuilds the capture device.
                self.recording = False
                accumulated_bytes.clear()
                continue
            if samples >= VOICE_CAPTURE_TRIGGER_SAMPLES:  # one 10ms hardware delivery period
                is_stereo = getattr(self, 'stereo', False)
                chunk = bytearray(samples * (4 if is_stereo else 2))
                try:
                    self.audio_input.capture_samples(chunk)
                except cyal.exceptions.CyalError:
                    self.recording = False
                    accumulated_bytes.clear()
                    continue
                
                if is_stereo:
                    import numpy as np
                    mono_arr = np.frombuffer(chunk, dtype=np.int16).reshape(-1, 2).mean(axis=1).astype(np.int16)
                    chunk = bytearray(mono_arr.tobytes())
                
                # Resolve gameplay directly
                gp = getattr(self.player, 'gameplay', None)
                if gp is None and hasattr(self.game, 'stack'):
                    try:
                        stack_snap = tuple(self.game.stack)
                    except Exception:
                        stack_snap = ()
                    for st in reversed(stack_snap):
                        if hasattr(st, 'player') and hasattr(st, 'megaphone'):
                            gp = st
                            break
                
                voice_using_mega = getattr(gp, 'voice_chat_using_megaphone', False) if gp else False

                # Is Music Bot streaming to the megaphone? Gameplay owns MapMusicBot for the whole
                # map session, so resolve it directly: game.stack may expose only a nested menu,
                # which made the recorder microphone hook vanish when leaving that menu.
                music_bot = self._find_music_bot(gp)

                # The mic joins the music bot's broadcast mix only when the megaphone is ACTUALLY
                # in use (PA Test Mode or the megaphone weapon): the streamer feeds the mixed
                # stream to the local PA sidechain itself, so feeding the raw mic here too would
                # double the broadcaster's own voice through the speakers.
                route_to_bot = bool(
                    music_bot and music_bot.playing
                    and music_bot.broadcast_enabled and music_bot.broadcast_to_megaphone
                    and voice_using_mega
                )

                # The private game recorder captures only what the Client renders, and normal
                # outgoing voice chat is not played back locally, so hand its mono PCM to the
                # recorder here without doing file or mixing work on this capture thread - the
                # megaphone voice is already rendered through the local PA sidechain.
                audio_recorder = getattr(music_bot, 'audio_recorder', None) if music_bot else None
                if audio_recorder is not None:
                    audio_recorder.feed_transmitted_microphone(
                        chunk,
                        locally_rendered=bool(voice_using_mega),
                    )

                # Feed local sidechain immediately with 10ms chunk for zero-latency response
                if voice_using_mega and gp and not route_to_bot:
                    _feed_local_megaphone_direct(gp, chunk, producer='mic')

                # Accumulate for the Opus encoder (a frame is exactly VOICE_FRAME_BYTES)
                accumulated_bytes.extend(chunk)
                while len(accumulated_bytes) >= VOICE_FRAME_BYTES:
                    chunk_bytes = accumulated_bytes[:VOICE_FRAME_BYTES]
                    accumulated_bytes = accumulated_bytes[VOICE_FRAME_BYTES:]

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
        try:
            if self.audio_input.available_samples < VOICE_FRAME_BYTES // 2: return self.audio_input.capture_samples(bytearray(self.audio_input.available_samples*2))
            buf = bytearray(VOICE_FRAME_BYTES)
            self.audio_input.capture_samples(buf)
        except cyal.exceptions.CyalError:
            # The microphone can die between the key release and this delayed
            # final drain; a dead handle is not worth interrupting the game.
            return
        
        # Check if Music Bot is streaming to Megaphone
        music_bot = self._find_music_bot()

        # Voice joins the music bot broadcast only when this recording session used the
        # megaphone channel - the compression's channel is the reliable per-session truth
        # (30 = PA Test Mode / megaphone weapon, 20 = normal).
        route_to_bot = bool(
            music_bot and music_bot.playing
            and music_bot.broadcast_enabled and music_bot.broadcast_to_megaphone
            and getattr(self.vc_compression, 'channel', None) == consts.CHANNEL_MEGAPHONE
        )

        audio_recorder = getattr(music_bot, 'audio_recorder', None) if music_bot else None
        if audio_recorder is not None:
            audio_recorder.feed_transmitted_microphone(
                buf,
                locally_rendered=(
                    getattr(self.vc_compression, 'channel', None) == consts.CHANNEL_MEGAPHONE
                ),
            )

        if route_to_bot:
            if not hasattr(music_bot, 'mic_pcm_queue'):
                music_bot.mic_pcm_queue = collections.deque(maxlen=10)
            music_bot.mic_pcm_queue.append(bytes(buf))
        else:
            # Send the tail chunk on the session's own channel (already set by run()).
            # Re-deriving it from the music bot here is wrong: by the time finish2 runs
            # (40 ms after stop) the megaphone flag is reset, so the last 20 ms would leak
            # onto CHANNEL_VOICECHAT and re-point the megaphone compression at it for good.
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


# ── Handing one output over to another (Party Sync seams) ───────────────
# A session member has two outputs: their own entity on this map and their party sink on another
# (libs/party_sync_audio.py). The seam is silent when what the old output still holds travels
# across it - otherwise the new leg owes its whole pre-buffer again (12 frames of music = 240 ms
# of nothing) while the old leg's frames are thrown away. These two helpers move the queue.

def drain_source_queue(source, guard=512):
    """MAIN THREAD ONLY: take every frame `source` still holds, in order.

                    A PLAYING source reports only the buffers it has finished, so it is stopped first and
                    OpenAL then hands everything back; one that never played needs no stop for the same
                    disclosure. The mid-playback buffer comes back with the rest and is replayed from its start
                    by whoever takes it (<= one frame, 20-40 ms). Returns ``(buffers, was_playing)``.
                    """
    buffers = []
    if source is None:
        return buffers, False
    try:
        was_playing = source.state == cyal.SourceState.PLAYING
    except Exception:
        was_playing = False
    if was_playing:
        try:
            source.stop()
        except Exception:
            pass
    try:
        while len(buffers) < guard and getattr(source, 'buffers_processed', 0) > 0:
            result = source.unqueue_buffers()
            if result is None:
                break
            if isinstance(result, (list, tuple)):
                buffers.extend(result)
            else:
                buffers.append(result)
    except Exception:
        pass
    return buffers, was_playing


def carry_output(old_source, new_source, guard=512):
    """MAIN THREAD ONLY: move `old_source`'s queued frames onto `new_source`.

                    Whatever the NEW output holds is dropped first: idle silence or the tail of an older song,
                    and a source refuses a buffer whose format differs from buffers still queued on it
                    (AL_INVALID_OPERATION). Returns ``(moved, was_playing)``; `new_source` is started here when
                    the old one was playing, so playback does not wait.
                    """
    if old_source is None or new_source is None or old_source is new_source:
        return 0, False
    drain_source_queue(new_source, guard)
    buffers, was_playing = drain_source_queue(old_source, guard)
    moved = 0
    for buf in buffers:
        try:
            new_source.queue_buffers(buf)
        except Exception:
            break
        moved += 1
    if moved and was_playing:
        try:
            new_source.play()
        except Exception:
            pass
    return moved, was_playing


class MusicCompression(threading.Thread):
    PRE_BUFFER_FRAMES = 12  # 240ms before first play (increased from 8 to
                            # prevent underruns on real networks)
    RESUME_FRAMES     = 8   # 160ms before resuming after underrun (increased
                            # from 5 to prevent rapid re-underrun cycles)
    TIMELINE_EVENT_TIMEOUT = 1.0
    MAX_TIMELINE_EVENTS = 256

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
            # Party Sync guests receive TRUE STEREO frames from the host; the
            # decoder/source format follows the entity's direct-mode flag.
            self._stereo = False
            # The cabinet's room this stream should play out of instead of the entity's own
            # source (see libs/audio/cinema/peer.py), or None for the shipped feed. The room
            # owns one source per speaker, so only the decode and the music timeline are shared.
            self.cinema_channel = None
            self.cinema_feed = None
            # Format-switch coordination (see set_output_stereo): the decoder generation lets
            # _play_music_frame drop frames decoded with a stale format, and the pending flag
            # flushes the source on the main thread when the first new-format frame lands.
            self._format_generation = 0
            self._pending_format_flush = False
            self._has_started = False
            self._last_recv_time = None
            self._timeline_epoch = None
            self._timeline_last_received_seq = None
            self._timeline_first_queued_seq = None
            self._timeline_anchor_seq = None
            self._timeline_anchor_time = None
            self._timeline_pending = []
            # This feed's own clock (see the section below): the frames it has
            # handed to its output, the size of one measured from the audio, and
            # the source those landed on.
            self.pair_frames_fed = 0
            self._pair_frame_ms = 0.0
            self._pair_source = None
            self._running = True
            self.start()
        except Exception as e:
            logger.log_exception(e, "MusicCompression.__init__")

    def put(self, value):
        if getattr(self, '_running', True):
            self.queue.put_nowait(value)

    def close(self):
        self._running = False
        self._timeline_pending = []
        self.queue.put_nowait(None)

    def run(self):
        while getattr(self, '_running', True):
            try:
                time.sleep(0.002)
                if not self.queue.empty():
                    value = self.queue.get_nowait()
                    if value is None: break
                    if callable(value):
                        value()
                # Timeline events fire OpenAL calls (stop_note fades etc.) and
                # mutate _timeline_pending — defer to the main thread so the
                # state stays single-threaded with _play_music_frame.
                self.game.audio_mngr.defer_audio(self._dispatch_timeline_events)
            except Exception as e:
                print(f"[ERROR MusicCompression.run] {e}")
                logger.log_exception(e, "MusicCompression.run")

    def recieve(self, data, music_source, radio_source, channelID, gameplay):
        if music_source is None:
            return
        self.put(lambda: self.recieve_actual(data, music_source, radio_source, channelID, gameplay))

    def set_cinema_channel(self, channel):
        """Point this feed at a cabinet's room, or at nothing (None).

                                Called once per frame from the receive path, so it only records which speaker's song
                                this is; the room is resolved in ``_room_feed`` on the main thread, because building
                                one creates OpenAL sources.
                                """
        self.cinema_channel = None if channel is None else int(channel)

    def _room_feed(self):
        """MAIN THREAD ONLY: the room this stream should play through now.

                                Decided per frame, because the sender can route, re-route or un-route the song at any
                                moment. A change flushes the other output: a queue's worth of its audio (240 ms of the
                                previous song, or of the previous position after a seek) arriving from the wrong place
                                is heard as an echo.
                                """
        if self.cinema_channel is None:
            feed = None
        else:
            try:
                from .audio.cinema import peer as cinema_peer
                feed = cinema_peer.route(self.game, self.cinema_channel)
            except Exception:
                feed = None
        if feed is not self.cinema_feed:
            self.cinema_feed = feed
            self._pending_format_flush = True
        return feed

    def set_output_stereo(self, stereo):
        """Switch this music feed between mono and true stereo output.

                                Called by the receive path when the entity enters/leaves Party Sync direct mode; the
                                decoder swap runs on the decode thread and queued buffers are flushed on the main thread.
                                """
        stereo = bool(stereo)
        if getattr(self, "_stereo", False) == stereo:
            return
        self.put(lambda: self._switch_output_stereo(stereo))

    def _switch_output_stereo(self, stereo):
        self._stereo = stereo
        self._format_generation += 1
        self._pending_format_flush = True
        try:
            from pyogg import OpusDecoder
            decoder = OpusDecoder()
            decoder.set_channels(2 if stereo else 1)
            decoder.set_sampling_frequency(48000)
            self.decoder = decoder
        except Exception:
            return

    # What a handover copies: these describe the SONG, not the output it comes
    # out of (see carry_over).
    CARRY_FIELDS = (
        "_has_started",
        "_last_recv_time",
        "_timeline_epoch",
        "_timeline_last_received_seq",
        "_timeline_first_queued_seq",
        "_timeline_anchor_seq",
        "_timeline_anchor_time",
    )

    def carry_over(self, other, old_source, new_source):
        """MAIN THREAD ONLY: continue `other`'s song on this leg's output.

                                Used at a Party Sync seam: the entity and the party sink are two outputs for the same
                                song, so whichever appears picks the song up where the other left it - the queue
                                (``carry_output``) and the clock the remote jam notes are scheduled against, which
                                would otherwise be re-pinned a pre-buffer late.

                                The OUTPUT FORMAT travels too, synchronously: `set_output_stereo` would arm the flush
                                that empties the very queue this call just moved, so the decoder is swapped here and
                                the flush flag is left clear. A leg whose two formats cannot be agreed keeps nothing
                                and starts fresh - mixing mono and stereo buffers on one source is an error OpenAL
                                refuses. Returns the number of frames carried (0 when this could not help).
                                """
        if other is None or other is self:
            return 0
        old_stereo = bool(getattr(other, "_stereo", False))
        moved, _playing = carry_output(old_source, new_source)
        for name in self.CARRY_FIELDS:
            if hasattr(other, name):
                setattr(self, name, getattr(other, name))
        self._timeline_pending = list(
            getattr(other, "_timeline_pending", None) or []
        )
        generation = getattr(other, "_format_generation", None)
        if generation is not None:
            self._format_generation = generation
        if bool(getattr(self, "_stereo", False)) != old_stereo:
            self._set_format_now(old_stereo)
        self._pending_format_flush = False
        return moved

    def _set_format_now(self, stereo):
        """MAIN THREAD ONLY: put this leg on `stereo` without arming a flush.

                                `set_output_stereo` would also arm `_pending_format_flush`, which empties the queue a
                                handover just carried. The decoder is rebuilt here and the generation bumped, so a
                                frame this leg decoded with its old format is dropped instead of queued beside the
                                carried buffers.
                                """
        self._stereo = bool(stereo)
        self._format_generation += 1
        try:
            from pyogg import OpusDecoder
            decoder = OpusDecoder()
            decoder.set_channels(2 if stereo else 1)
            decoder.set_sampling_frequency(48000)
            self.decoder = decoder
        except Exception:
            pass

    def _flush_source(self, src):
        """MAIN THREAD ONLY: empty a source that may hold old-format buffers.

                                OpenAL refuses a buffer whose format differs from buffers still queued
                                (AL_INVALID_OPERATION). A source that never played can hold the entity's MONO16 idle
                                silence, and alSourceStop has NO effect on an INITIAL source, so play (buffers become
                                processed), stop, then unqueue everything.
                                """
        if src is None:
            return
        try:
            src.gain = 0.0  # silent flush: never audibly click
            src.play()
            src.stop()
            guard = 0
            while getattr(src, 'buffers_processed', 0) > 0 and guard < 512:
                src.unqueue_buffers()
                guard += 1
        except Exception:
            pass
        self._has_started = False
        self._timeline_first_queued_seq = None
        self._timeline_anchor_seq = None
        self._timeline_anchor_time = None
        self._timeline_epoch = None
        self._timeline_last_received_seq = None
        self._last_recv_time = None
        self._pair_clock_reset()

    # --------------------------------------------------- this feed's clock
    # A note played along a song someone else streams waits on the frames this feed has *played*,
    # re-projected every frame, exactly as a cabinet's pair and a room's speakers do: the rule has one
    # home (``libs/jukebox_clock.py``), and what is this feed's own is the counter and the frame size.

    def _note_pair_fed(self, pcm, stereo=False):
        """Record one frame handed to this feed's own output (its clock input)."""
        self.pair_frames_fed = int(getattr(self, "pair_frames_fed", 0)) + 1
        # A true-stereo frame carries two interleaved channels at the same
        # rate, so measuring it as one channel of double the rate is the same
        # duration -- and the duration is what a wait is counted in.
        measured = mono_ms(pcm, 48000 * (2 if stereo else 1))
        if measured > 0.0:
            self._pair_frame_ms = measured

    def _pair_clock_reset(self):
        """Start this feed's clock over (a new session, or a queue that was cut): the epochs are forgotten
                                rather than the waits dropped, so a note already waiting keeps the instant it was going
                                to sound at (``SpotClock.forget``)."""
        self.pair_frames_fed = 0
        self._pair_frame_ms = 0.0
        clock = getattr(self, "_pair_spot_clock", None)
        if clock is not None:
            clock.forget()

    def pair_frame_ms(self):
        """How long one frame of this feed is (ms): latched from the frames actually handed over, falling
                                back to 20 ms until one frame has been queued."""
        measured = float(getattr(self, "_pair_frame_ms", 0.0) or 0.0)
        return measured if measured > 0.0 else 20.0

    def pair_queued_frames(self):
        """Frames this feed's own output still holds (OpenAL's own count)."""
        source = getattr(self, "_pair_source", None)
        try:
            return max(0, int(getattr(source, "buffers_queued", 0) or 0))
        except Exception:
            return 0

    def pair_played_frames(self):
        """Frames this feed has already played: handed over minus still queued (``pair_queued_frames`` is
                                what OpenAL reports ahead of the listener, so the difference is the content instant
                                playing right now)."""
        fed = int(getattr(self, "pair_frames_fed", 0) or 0)
        return max(0, fed - self.pair_queued_frames())

    def pair_is_playing(self):
        """True while this feed's own output is playing (see ``SpotClock``): a room-fed feed has no output of
                                its own to measure, a source that is not playing reports everything it holds as
                                finished, and a queue still building its pre-buffer says nothing either."""
        if getattr(self, "cinema_feed", None) is not None:
            return False
        if not getattr(self, "_has_started", False):
            return False
        source = getattr(self, "_pair_source", None)
        if source is None:
            return False
        try:
            return source.state == cyal.SourceState.PLAYING
        except Exception:
            return False

    @property
    def pair_clock(self):
        """This feed's own clock, made on first use; named like a cabinet's pair on purpose, because the
                                scheduler asks every output the same one question
                                (``EventHandeler._pair_clock_for_note``)."""
        clock = getattr(self, "_pair_spot_clock", None)
        if clock is None:
            clock = SpotClock(
                progress_of=lambda key: self.pair_played_frames(),
                frame_ms_of=self.pair_frame_ms,
                playing_of=lambda key: self.pair_is_playing(),
                keys_of=lambda: (PAIR_KEY,),
                lead_of=lambda key: self.pair_queued_frames(),
            )
            self._pair_spot_clock = clock
        return clock

    def recieve_timeline(self, data, music_source, radio_source, channelID,
                         gameplay, epoch, frame_seq):
        if music_source is None:
            return
        self.put(lambda: self.recieve_actual(
            data, music_source, radio_source, channelID, gameplay,
            epoch=epoch, frame_seq=frame_seq,
        ))

    @staticmethod
    def _sequence_reached(current, target):
        """Return True when current is at/after target in uint32 sequence space."""
        return ((current - target) & 0xFFFFFFFF) < 0x80000000

    def _audible_timeline_seq(self, now=None):
        if self._timeline_anchor_seq is None or self._timeline_anchor_time is None:
            return None
        elapsed = max(0.0, (time.perf_counter() if now is None else now)
                      - self._timeline_anchor_time)
        return (self._timeline_anchor_seq + int(elapsed / 0.020)) & 0xFFFFFFFF

    def schedule_timeline_event(self, epoch, frame_seq, callback):
        if not callable(callback):
            return False
        if not (isinstance(epoch, int) and isinstance(frame_seq, int)):
            return False
        if not (0 <= epoch <= 0xFFFFFFFF and 0 <= frame_seq <= 0xFFFFFFFF):
            return False
        self.put(lambda: self._queue_timeline_event(epoch, frame_seq, callback))
        return True

    def _queue_timeline_event(self, epoch, frame_seq, callback):
        if len(self._timeline_pending) >= self.MAX_TIMELINE_EVENTS:
            # A bounded queue protects the audio worker from a malicious or
            # stuck instrument. Fall back the oldest action instead of losing it.
            _, _, _, oldest = self._timeline_pending.pop(0)
            oldest()
        self._timeline_pending.append((
            epoch & 0xFFFFFFFF,
            frame_seq & 0xFFFFFFFF,
            time.perf_counter() + self.TIMELINE_EVENT_TIMEOUT,
            callback,
        ))
        self._dispatch_timeline_events()

    def _dispatch_timeline_events(self, now=None):
        if not self._timeline_pending:
            return
        current_time = time.perf_counter() if now is None else now
        audible_seq = self._audible_timeline_seq(current_time)
        remaining = []
        for epoch, frame_seq, deadline, callback in self._timeline_pending:
            if self._timeline_epoch is not None and epoch != self._timeline_epoch:
                # A new broadcast epoch supersedes stale notes from the old song.
                continue
            due = (
                audible_seq is not None
                and epoch == self._timeline_epoch
                and self._sequence_reached(audible_seq, frame_seq)
            )
            if due or current_time >= deadline:
                callback()
            else:
                remaining.append((epoch, frame_seq, deadline, callback))
        self._timeline_pending = remaining

    def recieve_actual(self, data, music_source, radio_source, channelID, gameplay,
                       epoch=None, frame_seq=None):
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

        # All OpenAL (and the timeline bookkeeping that schedules plays) runs on the MAIN
        # thread via the audio inbox; only the Opus decode stays on this worker thread.
        # _dispatch_timeline_events is deferred too, keeping _timeline_pending single-threaded.
        generation = self._format_generation
        stereo = self._stereo
        self.game.audio_mngr.defer_audio(
            lambda: self._play_music_frame(
                music_source, pcm, epoch, frame_seq, gameplay,
                generation=generation, stereo=stereo,
            )
        )

    def _play_music_frame(self, music_source, pcm, epoch, frame_seq, gameplay,
                          generation=None, stereo=None):
        """MAIN THREAD ONLY: session recovery + buffer queue/play for one
        decoded music-bot frame. Runs via AudioManager.defer_audio() inside
        the main thread's frame batch; the batch below nests safely because
        it is on the same (main) thread."""
        try:
            with self.game.audio_mngr.context.batch():
                if gameplay.player.dead:
                    return

                # A frame decoded before a mono/stereo format switch carries
                # the old format; drop it so it can never queue beside
                # new-format buffers (OpenAL rejects mixed formats with
                # AL_INVALID_OPERATION).
                if generation is not None and generation != self._format_generation:
                    return
                # Decide the output first: this is what sets the flush below
                # when a cabinet's room took the stream over (or gave it back),
                # so the switch happens on this very frame.
                room_feed = self._room_feed()
                if getattr(self, "_pending_format_flush", False):
                    self._pending_format_flush = False
                    self._flush_source(music_source)
                if stereo is None:
                    stereo = self._stereo

                now = time.time()
                timeline_changed = (
                    epoch is not None
                    and epoch != self._timeline_epoch
                )
                sequence_discontinuity = False
                if (epoch is not None and not timeline_changed
                        and self._timeline_last_received_seq is not None):
                    seq_delta = (frame_seq - self._timeline_last_received_seq) & 0xFFFFFFFF
                    sequence_discontinuity = seq_delta == 0 or seq_delta > 9
                is_new_session = (
                    self._last_recv_time is None
                    or (now - self._last_recv_time) > self.SESSION_RESET_GAP
                    or timeline_changed
                    or sequence_discontinuity
                )
                if is_new_session:
                    # New broadcast (or first after a stop): discard everything queued on the source -
                    # including the silent keep-alive buffers entity.loop() pushes when the queue runs
                    # empty - and reset the pre-buffer threshold, mirroring a fresh map load.

                    try:
                        music_source.stop()
                        while getattr(music_source, 'buffers_processed', 0) > 0:
                            music_source.unqueue_buffers()
                    except Exception:
                        pass
                    self._has_started = False
                    self._timeline_first_queued_seq = None
                    self._timeline_anchor_seq = None
                    self._timeline_anchor_time = None
                    # A new session's song starts at the pre-buffer: the clock
                    # a live note waits on starts over with it.
                    self._pair_clock_reset()
                if timeline_changed:
                    self._timeline_epoch = epoch
                    self._timeline_last_received_seq = frame_seq
                    self._timeline_pending = [
                        event for event in self._timeline_pending
                        if event[0] == epoch
                    ]
                elif epoch is None and is_new_session:
                    self._timeline_epoch = None
                    self._timeline_last_received_seq = None
                    self._timeline_pending = []
                elif epoch is not None:
                    self._timeline_last_received_seq = frame_seq
                self._last_recv_time = now

                # A cabinet's room is a second output for the very same frames: everything above is
                # shared on purpose - the session reset, the format flush and the timeline
                # bookkeeping all describe the song, not the output it comes out of.
                if room_feed is not None:
                    self._play_room_frame(room_feed, pcm, epoch, frame_seq, stereo)
                    self._dispatch_timeline_events()
                    return

                try:
                    state = music_source.state
                except Exception:
                    state = cyal.SourceState.STOPPED

                # An underrun that STOPPED playback needs the old processed buffers flushed and the
                # pre-buffering phase restarted. (The `if` for this suite was once missing - a
                # comment stood where it belonged and the suite sat at the handler's own
                # indentation, so it was dead and a stopped source was rebuilt one frame at a time.)
                if self._has_started and state == cyal.SourceState.STOPPED:
                    try:
                        self._has_started = False
                        self._timeline_first_queued_seq = None
                        self._timeline_anchor_seq = None
                        self._timeline_anchor_time = None
                        while getattr(music_source, 'buffers_processed', 0) > 0:
                            music_source.unqueue_buffers()
                    except Exception:
                        pass
                    # The queue this feed's clock was measuring is gone: the
                    # song restarts at the pre-buffer.
                    self._pair_clock_reset()

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
                    buf.set_data(
                        bytes(pcm), sample_rate=48000,
                        format=(cyal.BufferFormat.STEREO16 if stereo
                                else cyal.BufferFormat.MONO16),
                    )
                    music_source.queue_buffers(buf)
                    # The frame is this feed's clock input (see the clock
                    # section above); the source is remembered too, because
                    # that is the queue a note's wait is measured on.
                    self._pair_source = music_source
                    self._note_pair_fed(pcm, stereo)
                    if epoch is not None and self._timeline_first_queued_seq is None:
                        self._timeline_first_queued_seq = frame_seq
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
                            if (epoch is not None
                                    and self._timeline_first_queued_seq is not None):
                                self._timeline_anchor_seq = self._timeline_first_queued_seq
                                self._timeline_anchor_time = time.perf_counter()
                        except Exception:
                            pass

                self._dispatch_timeline_events()

        except Exception as e:
            logger.log_exception(e, "MusicCompression.recieve")

    def _play_room_frame(self, feed, pcm, epoch, frame_seq, stereo):
        """MAIN THREAD ONLY: play one decoded frame out of a cabinet's room.

                                The room owns one source per speaker and its own renderer, so there is no buffer and no
                                source queue to manage: the frame is handed over and the bank does the rest. What is
                                kept is the music timeline - remote instrument notes are scheduled against the sequence
                                this feed has reached.

                                The anchor is taken exactly as the plain source's is: adding the room's own queue depth
                                here as well put the clock a pre-buffer ahead of the speakers' real position, and every
                                live note over a room-fed song then waited that much too long.
                                """
        try:
            accepted = feed.push(pcm, stereo=stereo, epoch=epoch)
        except Exception as e:
            logger.log_exception(e, "MusicCompression._play_room_frame")
            return
        if accepted and frame_seq is not None and self._timeline_first_queued_seq is None:
            self._timeline_first_queued_seq = frame_seq
        if (feed.playing and self._timeline_anchor_time is None
                and self._timeline_first_queued_seq is not None):
            self._timeline_anchor_seq = self._timeline_first_queued_seq
            self._timeline_anchor_time = time.perf_counter()
        self._has_started = True


def _queue_packet_to_source(gameplay, idx, src, play_packet,
                            force_concert_mode=None, real_prebuffer_frames=None):
    # DE-CLICK: if this source is (re)starting, ramp only the FIRST queued
    # packet up from zero. With a real-frame prebuffer the source intentionally
    # remains INITIAL for several calls; fading every one of those frames would
    # cause 50 Hz gain modulation when playback begins.
    is_stopped = False
    try:
        is_stopped = src.state in (cyal.SourceState.STOPPED, cyal.SourceState.INITIAL)
    except Exception:
        pass

    _reclaim_source_buffers(src)
    if real_prebuffer_frames is not None:
        try:
            if src.state == cyal.SourceState.STOPPED:
                # OpenAL treats NEW buffers on a STOPPED source as processed, so reclaiming them on
                # each call would prevent the prebuffer ever reaching its threshold: drain then
                # rewind to INITIAL, which preserves new frames until play().
                _reclaim_source_buffers(src)
                if src.buffers_queued:
                    return  # Do not replay stale audio if draining failed.
                src.rewind()
                is_stopped = True
        except Exception:
            return
    queued_before = 0
    try:
        queued_before = int(src.buffers_queued)
        if is_stopped and queued_before == 0:
            play_packet = _fade_in_packet(play_packet)
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
        if real_prebuffer_frames is not None:
            # Local music monitor: wait for consecutive REAL frames. The old
            # actual-frame + trailing-silence start order produced a 20 ms hole
            # after every underrun and was heard as a repeating stutter.
            try:
                threshold = max(1, int(real_prebuffer_frames))
                if src.buffers_queued < threshold:
                    return
            except Exception:
                return
        else:
            # Legacy listener/voice path: preserve its 40 ms silence cushion.
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

                    Always pad up to target_active (frames_delay + margin) so the exact inter-speaker propagation
                    delay stagger survives underrun recovery and the stereo soundstage cannot collapse from wide
                    stereo to merged mono.
                    """
    if not needs_initial_delay and not any_starved:
        return 0
    return max(0, target_active - current_active)


def queue_and_delay_frame(gameplay, sender_id, sources, packet, margin_frames=None, real_prebuffer_frames=None,
                          ignore_speaker_delay=False):
    """Queue one frame to every speaker source with the PA's spatial stagger.

                    ignore_speaker_delay: TRUE for the OWNER's own monitor. An installer's per-speaker `delay` is
                    an ALIGNMENT offset for the people standing out there, not something the owner should hear
                    themselves through (up to 0.5 s late); the propagation part (distance / 343 m/s) still applies.
                    Baselines are cached per sender key ('<player>:<producer>' vs the peer id).
                    """

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
        _reclaim_source_buffers(src)
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
    # The propagation-delay baseline is FROZEN at the stream's start position: re-basing it on every
    # walk step made the inter-cabinet stagger flip between merged and separated as the listener moved,
    # so the PA image kept 'แยกบ้าง รวมบ้าง'. Spatial GAINS still track the listener.
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
                static_delay = 0.0 if ignore_speaker_delay else spk_data.get('delay', 0.0)
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
            
            # The PA's reserve for remote listeners, whose reliable leg can pause
            # briefly for retransmission: six frames (120 ms), queued ahead of the
            # audio. This - not the jitter buffer's pre-buffer - is the cushion a
            # stall consumes (tools/megaphone_latency_sim.py). Local producers
            # pass margin_frames=0 — their monitor has no network leg, so a fixed
            # cushion would only push the owner's own voice/music late.
            if margin_frames is None:
                margin_frames = _megaphone_margin_frames(sender_id)
            
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
                    _queue_packet_to_source(
                        gameplay, idx, src, silence_packet,
                        real_prebuffer_frames=real_prebuffer_frames,
                    )
                    
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
            _queue_packet_to_source(
                gameplay, idx, src, packet_to_queue,
                real_prebuffer_frames=real_prebuffer_frames,
            )
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
