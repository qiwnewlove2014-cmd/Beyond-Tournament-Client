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
_speaker_delay_queues = {}
_last_play_times = {}
_last_packet_times = {}

def get_jitter_buffer(game, source_id):
    """Get or create jitter buffer for a specific audio source"""
    global _jitter_buffers
    if source_id not in _jitter_buffers:
        _jitter_buffers[source_id] = MegaphoneJitterBuffer(game)
    return _jitter_buffers[source_id]

def reset_jitter_buffers():
    """Reset all jitter buffers and delay queues"""
    global _jitter_buffers, _speaker_delay_queues, _last_play_times, _last_packet_times
    _jitter_buffers = {}
    _speaker_delay_queues = {}
    _last_play_times = {}
    _last_packet_times = {}

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
                    
                    # Single jitter buffer per sender — ensures all speakers play the same frame simultaneously
                    buffer_key = sender_id if sender_id is not None else "megaphone_shared"
                    jb = get_jitter_buffer(self.game, buffer_key)
                    jb.add_packet(limited_data)
                    
                    # Get the next frame ONCE for all speakers
                    packet = jb.get_packet()
                    if packet is None:
                        return  # Still pre-buffering, no speaker plays yet
                    
                    # Update last play time
                    global _last_play_times, _speaker_delay_queues, _last_packet_times
                    _last_play_times[sender_id] = time.time()
                    
                    # If this is a new sentence after a pause (>300ms), clear delay queues to prevent echoes from the past
                    current_time = time.time()
                    last_pkt_time = _last_packet_times.get(sender_id, 0.0)
                    _last_packet_times[sender_id] = current_time
                    if current_time - last_pkt_time > 0.3:
                        for idx in range(len(sources)):
                            queue_key = (sender_id, idx)
                            if queue_key in _speaker_delay_queues:
                                _speaker_delay_queues[queue_key].clear()
                    
                    # Queue and delay the frame for each speaker
                    queue_and_delay_frame(gameplay, sender_id, sources, packet)
                    return  # Megaphone handled, skip normal processing
                
                # === NORMAL VOICE CHAT: Direct playback (no jitter buffer needed) ===
                sources_to_play = []
                for idx, src in enumerate(sources):
                    buf = None
                    try:
                        while src.buffers_processed > 0:
                            result = src.unqueue_buffers()
                            if result is not None:
                                if isinstance(result, (list, tuple)):
                                    buf = result[0]
                                else:
                                    buf = result
                    except Exception:
                        pass
                    
                    if buf is None: 
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
            buffer = None
            try:
                if radio_source.buffers_processed > 0:
                    result = radio_source.unqueue_buffers()
                    if result is not None:
                        if isinstance(result, (list, tuple)):
                            buffer = result[0]
                        else:
                            buffer = result
            except Exception:
                pass
            if buffer is None:
                buffer = self.game.audio_mngr.context.gen_buffer()
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


class MusicCompression:
    PRE_BUFFER_FRAMES = 8   # 160ms before first play
    RESUME_FRAMES     = 3   # 60ms before resuming after underrun

    def __init__(self, game):
        self.game = game
        from pyogg import OpusDecoder
        self.decoder = OpusDecoder()
        self.decoder.set_channels(1)
        self.decoder.set_sampling_frequency(48000)
        self._has_started = False

    def recieve(self, data, music_source, radio_source, channelID, gameplay):
        try:
            with self.game.audio_mngr.context.batch():
                if gameplay.player.dead:
                    return

                state = music_source.state

                # Recycle or generate buffer
                buf = None
                if state == cyal.SourceState.PLAYING:
                    try:
                        while music_source.buffers_processed > 0:
                            result = music_source.unqueue_buffers()
                            if result is not None:
                                if isinstance(result, (list, tuple)):
                                    buf = result[0]
                                else:
                                    buf = result
                    except Exception:
                        pass

                # Decode Opus packet
                try:
                    pcm = bytearray(self.decoder.decode(bytearray(data)))
                except Exception:
                    return

                if buf is None:
                    try:
                        buf = self.game.audio_mngr.context.gen_buffer()
                    except Exception:
                        return

                # Fill and queue
                buf.set_data(bytes(pcm), sample_rate=48000, format=cyal.BufferFormat.MONO16)
                try:
                    music_source.queue_buffers(buf)
                except Exception:
                    return

                # Start or resume playback
                if state == cyal.SourceState.STOPPED or state == cyal.SourceState.INITIAL:
                    threshold = self.PRE_BUFFER_FRAMES if not self._has_started else self.RESUME_FRAMES
                    if music_source.buffers_queued >= threshold:
                        try:
                            music_source.play()
                            self._has_started = True
                        except Exception:
                            pass

        except Exception as e:
            logger.log_exception(e, "MusicCompression.recieve")


def _queue_packet_to_source(gameplay, idx, src, play_packet):
    buf = None
    try:
        while src.buffers_processed > 0:
            result = src.unqueue_buffers()
            if result is not None:
                if isinstance(result, (list, tuple)):
                    buf = result[0]
                else:
                    buf = result
    except Exception:
        pass
    
    if buf is None: 
        buf = gameplay.game.audio_mngr.context.gen_buffer()
    
    try:
        buf.set_data(play_packet, sample_rate=48000, format=cyal.BufferFormat.MONO16)
        src.queue_buffers(buf)
    except (cyal.exceptions.InvalidOperationError, cyal.exceptions.ALError): 
        return
        # Start playing if stopped
    if src.state == cyal.SourceState.STOPPED or src.state == cyal.SourceState.INITIAL:
        # Re-apply EFX effects before playing using the source's unique filter
        spk_idx = idx // 2
        is_reflection = (idx % 2 == 1)
        if hasattr(gameplay, 'megaphone_speaker_data') and spk_idx < len(gameplay.megaphone_speaker_data):
            speaker_data = gameplay.megaphone_speaker_data[spk_idx]
            
            # Lookup unique filter belonging to this source
            filter_to_apply = None
            if hasattr(gameplay, 'megaphone_player_sources'):
                for entry in gameplay.megaphone_player_sources.values():
                    if 'sources' in entry and src in entry['sources']:
                        src_idx = entry['sources'].index(src)
                        if 'filters' in entry and src_idx < len(entry['filters']):
                            filter_to_apply = entry['filters'][src_idx]
                        break
            
            # Fallback to physical templates
            if filter_to_apply is None:
                filter_to_apply = speaker_data.get('refl_filter' if is_reflection else 'filter')

            if hasattr(gameplay.game.audio_mngr, 'efx'):
                if hasattr(gameplay, 'megaphone_eq_slot') and gameplay.megaphone_eq_slot:
                    gameplay.game.audio_mngr.efx.send(src, 0, gameplay.megaphone_eq_slot, filter=filter_to_apply)
                if speaker_data.get('reverb_slot'):
                    gameplay.game.audio_mngr.efx.send(src, 1, speaker_data['reverb_slot'], filter=filter_to_apply)
                if hasattr(gameplay, 'megaphone_compressor_slot') and gameplay.megaphone_compressor_slot:
                    gameplay.game.audio_mngr.efx.send(src, 2, gameplay.megaphone_compressor_slot, filter=filter_to_apply)
            
            if filter_to_apply:
                try:
                    src.direct_filter = filter_to_apply
                except:
                    pass
        try:
            src.play()
        except:
            pass


def queue_and_delay_frame(gameplay, sender_id, sources, packet):
    global _speaker_delay_queues
    import math
    
    # Get player (listener) position from camera focus object
    try:
        player_pos = (gameplay.camera.focus_object.x, gameplay.camera.focus_object.y, gameplay.camera.focus_object.z)
    except AttributeError:
        player_pos = (0.0, 0.0, 0.0)
        
    for idx, src in enumerate(sources):
        if src is None:
            continue
            
        spk_idx = idx // 2
        is_reflection = (idx % 2 == 1)
        
        static_delay = 0.0
        speaker_pos = (0.0, 0.0, 0.0)
        
        if hasattr(gameplay, 'megaphone_speaker_data') and spk_idx < len(gameplay.megaphone_speaker_data):
            spk_data = gameplay.megaphone_speaker_data[spk_idx]
            static_delay = spk_data.get('delay', 0.0)
            speaker_pos = spk_data.get('position', (0.0, 0.0, 0.0))
            
        # Calculate dynamic propagation delay (speed of sound = 343 m/s)
        if not is_reflection:
            # Direct path: Speaker -> Player
            dx = player_pos[0] - speaker_pos[0]
            dy = player_pos[1] - speaker_pos[1]
            dz = player_pos[2] - speaker_pos[2]
            distance = math.sqrt(dx*dx + dy*dy + dz*dz)
            propagation_delay = distance / 343.0
        else:
            # Ground reflection path: Speaker -> Ground -> Player
            ground_level = gameplay.map.minz if hasattr(gameplay, 'map') and hasattr(gameplay.map, 'minz') else 0.0
            dist_spk_to_ground = abs(speaker_pos[2] - ground_level)
            
            dx = player_pos[0] - speaker_pos[0]
            dy = player_pos[1] - speaker_pos[1]
            dz = player_pos[2] - ground_level
            dist_ground_to_player = math.sqrt(dx*dx + dy*dy + dz*dz)
            
            distance = dist_spk_to_ground + dist_ground_to_player
            propagation_delay = distance / 343.0
            
        total_delay = static_delay + propagation_delay
        frames_delay = int(total_delay / 0.02)  # Convert to 20ms frames
        
        queue_key = (sender_id, idx)
        if queue_key not in _speaker_delay_queues:
            _speaker_delay_queues[queue_key] = collections.deque()
        dq = _speaker_delay_queues[queue_key]
        
        if frames_delay > 0:
            dq.append(packet)
            # Catch up if player moved closer and queue has too many old packets
            while len(dq) > frames_delay + 1:
                dq.popleft()
                
            if len(dq) <= frames_delay:
                play_packet = bytes(len(packet))
            else:
                play_packet = dq.popleft()
        else:
            if len(dq) > 0:
                dq.clear()
            play_packet = packet
            
        _queue_packet_to_source(gameplay, idx, src, play_packet)


def tick_megaphone_delay(gameplay):
    global _last_play_times, _speaker_delay_queues, _last_packet_times
    current_time = time.time()
    
    if not hasattr(gameplay, 'megaphone_player_sources') or not gameplay.megaphone_player_sources:
        return
        
    for sender_id, entry in list(gameplay.megaphone_player_sources.items()):
        sources = entry['sources']
        last_time = _last_play_times.get(sender_id, 0)
        last_pkt_time = _last_packet_times.get(sender_id, 0)
        
        # Only tick if we haven't received a network packet for at least 40ms (flushing phase)
        # This prevents the tick loop from interfering with active network speech playback
        if current_time - last_pkt_time >= 0.04:
            if current_time - last_time >= 0.02:
                # Check if any delay queue has pending frames
                has_delayed_audio = False
                for idx in range(len(sources)):
                    queue_key = (sender_id, idx)
                    if queue_key in _speaker_delay_queues and len(_speaker_delay_queues[queue_key]) > 0:
                        has_delayed_audio = True
                        break
                        
                if has_delayed_audio:
                    _last_play_times[sender_id] = current_time
                    dummy_packet = bytes(1920)
                    queue_and_delay_frame(gameplay, sender_id, sources, dummy_packet)





