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

def soft_limit_audio(audio_bytes, threshold=0.35, ratio=12.0):
    """
    Apply soft limiting to prevent clipping when multiple audio streams combine.
    
    Args:
        audio_bytes: Raw 16-bit PCM audio data
        threshold: Level (0.0-1.0) above which limiting starts (0.35 = 35% of max)
        ratio: Compression ratio above threshold (12.0 = 12:1 heavy compression)
    
    Returns:
        Limited audio bytes
    
    ANTI-CLIPPING - AGGRESSIVE SETTINGS:
    - Very low threshold (0.35): Start limiting very early 
    - Very high ratio (12.0): Extreme compression to prevent any distortion
    - Pre-scaling: Automatically reduce peaks before processing
    """
    try:
        # Convert bytes to samples
        samples = list(struct.unpack(f'<{len(audio_bytes)//2}h', audio_bytes))
        
        max_val = 32767
        threshold_val = int(max_val * threshold)
        
        # Apply soft limiting to each sample with additional peak normalization
        limited = []
        peak = max(abs(s) for s in samples) if samples else 1
        
        # If peak is very high, apply additional pre-scaling
        pre_scale = 1.0
        if peak > max_val * 0.9:
            pre_scale = (max_val * 0.85) / peak
        
        for sample in samples:
            # Pre-scale to prevent extreme peaks
            sample = int(sample * pre_scale)
            
            abs_sample = abs(sample)
            if abs_sample > threshold_val:
                # Calculate how much over threshold
                over = abs_sample - threshold_val
                # Compress the amount over threshold (stronger ratio)
                compressed_over = over / ratio
                # New sample value
                new_abs = threshold_val + compressed_over
                # Clamp to max
                new_abs = min(new_abs, max_val)
                # Restore sign
                sample = int(new_abs) if sample > 0 else -int(new_abs)
            limited.append(sample)
        
        # Convert back to bytes
        return struct.pack(f'<{len(limited)}h', *limited)
    except Exception:
        # If anything fails, return original
        return audio_bytes

# ============================================================================
# PROFESSIONAL JITTER BUFFER FOR MEGAPHONE (Discord/TeamSpeak Style)
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
    Based on techniques used by Discord, TeamSpeak, and Mumble.
    """
    
    # === CONFIGURATION ===
    FRAME_SIZE = 1920           # 20ms at 48kHz mono (960 samples * 2 bytes)
    FRAME_DURATION_MS = 20      # Each Opus frame is 20ms
    PRE_BUFFER_FRAMES = 3       # Wait for 3 frames (60ms) before playing
    MAX_BUFFER_FRAMES = 8       # Maximum frames in buffer (160ms)
    TARGET_BUFFER_FRAMES = 4    # Target buffer level (80ms latency)
    
    def __init__(self, game):
        self.game = game
        self.lock = threading.Lock()
        
        # Packet queue (deque for O(1) append/popleft)
        self.packet_queue = collections.deque(maxlen=self.MAX_BUFFER_FRAMES)
        
        # Playback state
        self.is_playing = False
        self.frames_received = 0
        
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
            # Pre-buffering: Wait until we have enough packets
            if not self.is_playing:
                if len(self.packet_queue) >= self.PRE_BUFFER_FRAMES:
                    self.is_playing = True
                    logger.log(f"[JitterBuffer] Started playback after {self.frames_received} frames")
                else:
                    return None  # Still pre-buffering
            
            # Get next packet
            if len(self.packet_queue) > 0:
                self.packets_played += 1
                return self.packet_queue.popleft()
            else:
                # Buffer underrun - stop playback, will restart with pre-buffering
                self.is_playing = False
                self.frames_received = 0
                return None
    
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
            self.frames_received = 0

# Per-source jitter buffers (one per megaphone speaker)
_jitter_buffers = {}

def get_jitter_buffer(game, source_id):
    """Get or create jitter buffer for a specific audio source"""
    global _jitter_buffers
    if source_id not in _jitter_buffers:
        _jitter_buffers[source_id] = MegaphoneJitterBuffer(game)
    return _jitter_buffers[source_id]

def reset_jitter_buffers():
    """Reset all jitter buffers"""
    global _jitter_buffers
    _jitter_buffers = {}

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



    def recieve(self, data, vc_source, radio_source, channelID, gameplay):
        self.put(lambda: self.recieve2(data, vc_source, radio_source, channelID, gameplay))

    def recieve2(self, data, vc_source, radio_source, channelID, gameplay):
        buffer = None
        data = bytearray(self.decoder.decode(bytearray(data)))
        
        with self.game.audio_mngr.context.batch():
            if not gameplay.player.dead:
                # Handle single source or list of sources (for Megaphone Quadraphonic)
                sources = vc_source if isinstance(vc_source, list) else [vc_source]
                
                # === MEGAPHONE: Use Jitter Buffer for smooth playback ===
                if channelID == consts.CHANNEL_MEGAPHONE:
                    # Count active sources for dynamic ducking
                    active_count = sum(1 for src in sources if hasattr(src, 'state') and src.state != cyal.SourceState.STOPPED)
                    update_active_speakers(active_count)
                    
                    # Calculate dynamic volume reduction based on speaker count
                    # LESS AGGRESSIVE: Preserve volume while still preventing clipping
                    # 1 speaker = 100%, 2 speakers = 85% each, 3+ = 70% each
                    speaker_count = get_active_speaker_count()
                    if speaker_count >= 3:
                        volume_factor = 0.7  # Was 0.5 - too quiet with reverb
                    elif speaker_count >= 2:
                        volume_factor = 0.85  # Was 0.7 - too quiet with reverb
                    else:
                        volume_factor = 1.0
                    
                    # Apply soft limiter with dynamic parameters based on speaker count
                    # LESS AGGRESSIVE limiting to preserve audio clarity
                    dynamic_threshold = 0.5 - (speaker_count - 1) * 0.05  # Gentler adjustment
                    dynamic_threshold = max(0.35, dynamic_threshold)  # Don't go below 35%
                    dynamic_ratio = 6.0 + (speaker_count - 1) * 1.0  # Gentler compression
                    
                    limited_data = soft_limit_audio(bytes(data), threshold=dynamic_threshold, ratio=dynamic_ratio)
                    
                    # Additional volume reduction for multi-speaker scenarios
                    if volume_factor < 1.0:
                        try:
                            limited_data = audioop.mul(limited_data, 2, volume_factor)
                        except Exception:
                            pass
                    
                    for idx, src in enumerate(sources):
                        # Get jitter buffer for this source
                        jb = get_jitter_buffer(self.game, id(src))
                        
                        # Add LIMITED packet to jitter buffer
                        jb.add_packet(limited_data)
                        
                        # Only output if buffer is ready (pre-buffering complete)
                        packet = jb.get_packet()
                        if packet is None:
                            continue  # Still pre-buffering
                        
                        # Clear processed buffers
                        try:
                            while src.buffers_processed > 0:
                                src.unqueue_buffers()
                        except Exception:
                            pass
                        
                        # Queue the packet from jitter buffer
                        if src.buffers_processed > 0: 
                            buf = src.unqueue_buffers()[0]
                        else: 
                            buf = self.game.audio_mngr.context.gen_buffer()
                        
                        try:
                            buf.set_data(packet, sample_rate=48000, format=cyal.BufferFormat.MONO16)
                            src.queue_buffers(buf)
                        except (cyal.exceptions.InvalidOperationError, cyal.exceptions.ALError): 
                            continue
                        
                        # Start playing if stopped
                        if src.state == cyal.SourceState.STOPPED or src.state == cyal.SourceState.INITIAL:
                            # Re-apply EFX effects before playing
                            if hasattr(gameplay, 'megaphone_speaker_data') and idx < len(gameplay.megaphone_speaker_data):
                                speaker_data = gameplay.megaphone_speaker_data[idx]
                                if hasattr(self.game.audio_mngr, 'efx'):
                                    if hasattr(gameplay, 'megaphone_eq_slot') and gameplay.megaphone_eq_slot:
                                        self.game.audio_mngr.efx.send(src, 0, gameplay.megaphone_eq_slot)
                                    if speaker_data.get('reverb_slot'):
                                        self.game.audio_mngr.efx.send(src, 1, speaker_data['reverb_slot'])
                                    if hasattr(gameplay, 'megaphone_compressor_slot') and gameplay.megaphone_compressor_slot:
                                        self.game.audio_mngr.efx.send(src, 2, gameplay.megaphone_compressor_slot)
                                if hasattr(gameplay, 'megaphone_lowpass_filter') and gameplay.megaphone_lowpass_filter:
                                    try:
                                        src.direct_filter = gameplay.megaphone_lowpass_filter
                                    except:
                                        pass
                            try:
                                src.play()
                            except:
                                pass
                    return  # Megaphone handled, skip normal processing
                
                # === NORMAL VOICE CHAT: Direct playback (no jitter buffer needed) ===
                sources_to_play = []
                for idx, src in enumerate(sources):
                    try:
                        while src.buffers_processed > 0:
                            src.unqueue_buffers()
                    except Exception:
                        pass
                    
                    if src.buffers_processed > 0: 
                        buf = src.unqueue_buffers()[0]
                    else: 
                        buf = self.game.audio_mngr.context.gen_buffer()
                    
                    buf.set_data(data, sample_rate=48000, format=cyal.BufferFormat.MONO16)
                    try: 
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
            if radio_source.buffers_processed > 0: buffer = radio_source.unqueue_buffers()[0]
            else: buffer = self.game.audio_mngr.context.gen_buffer()
            buffer.set_data(data, sample_rate=48000, format=cyal.BufferFormat.MONO16)
            radio_source.queue_buffers(buffer)
            if radio_source.state == cyal.SourceState.STOPPED or radio_source.state == cyal.SourceState.INITIAL: radio_source.play()


class VoiceChatRecord(threading.Thread):
    def __init__(self, game, player):
        super().__init__(daemon=True)
        self.game = game
        self.player = player
        self.capture_ext = cyal.CaptureExtension()
        device = options.get("audio_input_device", 'system default')
        if device == 'system default': device = self.capture_ext.default_device.decode('utf-8')
        try: self.audio_input = self.capture_ext.open_device(name=device.encode(), sample_rate=48000)
        except cyal.exceptions.DeviceNotFoundError: 
            self.audio_input = None
            speak(f"Failed to load audio device: {device}")
        self.vc_compression = voice_chat_compression(self.game)
        self.recording = False
        self.running = True
        self.start()
    

    def run(self):
        while self.running:
            time.sleep(0.0005)
            if not self.recording: continue
            if self.audio_input is None or not options.get("microphone", True) or not options.get("voice_chat", True): continue
            samples = self.audio_input.available_samples
            if samples >= 960:
                buf = bytearray(960 * 2)
                self.audio_input.capture_samples(buf)
                
                self.vc_compression.put(buf)

    def voice_chat_finish(self):
        self.voice_chat_finish2()
    
    def voice_chat_finish2(self):
        if self.audio_input.available_samples < 960: return self.audio_input.capture_samples(bytearray(self.audio_input.available_samples*2))
        buf = bytearray(1920)
        self.audio_input.capture_samples(buf)
        self.vc_compression.put(buf)
    
    def close(self):
        self.vc_compression.put(None)
        self.running = False
        
