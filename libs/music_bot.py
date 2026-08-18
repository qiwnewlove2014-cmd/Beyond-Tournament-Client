"""
MapMusicBot — YouTube Music Bot for Beyond Tournament
Searches YouTube via yt-dlp and streams audio in real-time via ffmpeg → OpenAL.
Also supports local file playback as fallback.
"""

import os
import sys
import random
import threading
import subprocess
import time
import contextlib
import queue
import struct
from collections import deque

import cyal
import cyal.exceptions
import pygame

from .string_utils import friendly_key_name

from . import options
from .speech import speak
from . import logger

# Try to find ffmpeg path
def _is_youtube_watch_url(value):
    """True when the URL is a stable https youtube.com / youtu.be page URL."""
    try:
        from urllib.parse import urlparse
        host = (urlparse(value).hostname or "").lower()
        return bool(value and value.startswith("https://")) and (
            host == "youtube.com" or host.endswith(".youtube.com") or host == "youtu.be"
        )
    except Exception:
        return False


def _find_ffmpeg():
    """Find ffmpeg binary - check common locations"""
    # 1. Check ffmpeg-downloader path
    try:
        from ffmpeg_downloader import ffmpeg_path
        if ffmpeg_path and os.path.exists(ffmpeg_path):
            return ffmpeg_path
    except ImportError:
        pass
    # 2. Check next to executable (handle Nuitka/PyInstaller standalone state)
    is_compiled = getattr(sys, 'frozen', False) or '__compiled__' in globals() or not os.path.basename(sys.executable).lower().startswith("python")
    if is_compiled:
        exe_dir = os.path.dirname(sys.executable)
    else:
        exe_dir = os.path.dirname(os.path.abspath(sys.argv[0]))
        
    for name in ["ffmpeg$.exe", "ffmpeg.exe", "ffmpeg"]:
        p = os.path.join(exe_dir, name)
        if os.path.exists(p):
            return p
    # 3. Check PATH
    import shutil
    p = shutil.which("ffmpeg")
    if p:
        return p
    return None

FFMPEG_PATH = _find_ffmpeg()

# Default map-to-music mapping for local fallback
DEFAULT_MAP_MUSIC = {
    "map1": ["Map1.ogg"], "map2": ["Map2.ogg"], "map3": ["Map3.ogg"],
    "map4": ["Map4.ogg"], "map5": ["Map5.ogg"], "map6": ["Map6.ogg"],
    "warehouse": ["Warehouse1.ogg", "Warehouse2.ogg", "Warehouse3.ogg", "Warehouse4.ogg"],
    "sub": ["Sub1.ogg", "Sub2.ogg", "Sub3.ogg"],
    "fort": ["Fort.ogg"], "crash": ["Crash.ogg"], "ctf": ["CTF.ogg"],
    "defender": ["Defender.ogg"], "future": ["Future.ogg"],
    "lastman": ["LastMan.ogg"], "quest": ["Quest.ogg"], "sniper": ["Sniper.ogg"],
}
FALLBACK_PLAYLIST = ["1.ogg", "2.ogg", "3.ogg", "4.ogg", "5.ogg", "6.ogg", "7.ogg", "8.ogg", "9.ogg"]


class YouTubeSearcher:
    """Search YouTube using yt-dlp and extract audio stream URLs."""

    @staticmethod
    def search(query, count=5):
        """Search YouTube, returns list of {title, url, duration, webpage_url}"""
        try:
            import yt_dlp
        except ImportError:
            speak("yt-dlp is not installed. Cannot search YouTube.")
            return []

        ydl_opts = {
            # FLAT search: yt-dlp reads the search page's own metadata (title,
            # id, duration) in ONE request instead of fully extracting every
            # result (formats, signed stream URLs, per-video player fetches) —
            # measured ~5x faster (5.3s -> ~1s for 5 results). Both consumers
            # (jukebox queue, personal music bot) only ever keep the canonical
            # webpage URL: the jukebox queues it server-side and the music bot
            # re-resolves it to a FRESH stream at play time, so no signed
            # googlevideo URL (which expires -> 403) is needed from search.
            'quiet': True,
            'no_warnings': True,
            'noplaylist': True,
            # One poisoned result (e.g. an age-restricted video raising
            # "Sign in to confirm your age") must not kill the WHOLE search —
            # broken entries come back as None and are filtered below.
            'ignoreerrors': True,
            'extract_flat': 'in_playlist',
            'skip_download': True,
        }
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(f"ytsearch{count}:{query}", download=False)
                entries = info.get('entries', [])
                results = []
                for e in entries:
                    if not e:
                        continue
                    video_id = e.get('id', '')
                    # Flat entries carry the canonical watch URL in `url` and
                    # no `webpage_url`; prefer the stable page URL either way.
                    webpage_url = e.get('webpage_url') or ""
                    if not webpage_url and video_id:
                        webpage_url = f"https://www.youtube.com/watch?v={video_id}"
                    if not webpage_url and _is_youtube_watch_url(e.get('url', '')):
                        webpage_url = e.get('url')
                    results.append({
                        'title': e.get('title', 'Unknown'),
                        # Flat search returns durations as FLOAT (233.0); the
                        # results menus format them with '% 60:02d', which only
                        # accepts ints — normalize here so a float can never
                        # crash the menu open.
                        'duration': int(e.get('duration') or 0),
                        'webpage_url': webpage_url,
                        'url': e.get('url', ''),  # canonical watch URL in flat mode
                        # A googlevideo URL can be authorized for the exact
                        # request headers returned by yt-dlp. Keep them paired
                        # so ffmpeg is not rejected with HTTP 403. Flat search
                        # results carry none; consumers re-resolve at play time.
                        'http_headers': dict(e.get('http_headers') or {}),
                    })
                return results
        except Exception as ex:
            logger.log_exception(ex, "YouTubeSearcher.search")
            return []

    @staticmethod
    def get_stream_info(webpage_url):
        """Resolve a YouTube page to its paired stream URL and HTTP headers."""
        try:
            import yt_dlp
        except ImportError:
            return None
        ydl_opts = {
            # Progressive 360p first (see search()): audio-only DASH URLs from
            # googlevideo intermittently 403 on fresh resolution, which made
            # "Could not load track." appear even though pressing Ctrl+M again
            # eventually worked (each retry resolved a new URL).
            'format': 'best[acodec!=none][vcodec!=none][height<=360]/bestaudio/best',
            'quiet': True,
            'no_warnings': True,
            'extractor_args': {'youtube': {'player_client': ['android', 'web']}},
        }
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(webpage_url, download=False)
                stream_url = info.get('url')
                if not stream_url:
                    return None
                return {
                    'url': stream_url,
                    'http_headers': dict(info.get('http_headers') or {}),
                }
        except Exception as ex:
            logger.log_exception(ex, "YouTubeSearcher.get_stream_info")
            return None

    @staticmethod
    def get_stream_url(webpage_url):
        """Compatibility helper for callers that only need the direct URL."""
        info = YouTubeSearcher.get_stream_info(webpage_url)
        return info.get('url') if info else None


class AudioStreamer(threading.Thread):
    """Background thread: ffmpeg decodes audio URL → raw PCM mono → queued to OpenAL source.
    
    Audio pipeline:
      YouTube URL → ffmpeg (decode to s16le mono 48kHz) → PCM chunks → OpenAL buffer queue
                                                        → Opus encode → Network broadcast (rate-limited)
    
    Network streaming uses real-time rate limiting (one 20ms frame per ~20ms) to prevent
    packet bursting which causes stuttering on receivers.
    """

    # 960 samples per channel (20ms at 48kHz for Opus)
    SAMPLES_PER_BUFFER = 960
    BUFFER_SIZE = SAMPLES_PER_BUFFER * 2 * 2  # stereo 16-bit (3840 bytes)
    NUM_BUFFERS = 32      # Total buffers in pool
    PRE_BUFFER_COUNT = 10 # Buffers to fill before starting local playback

    def __init__(self, game, audio_url, source, volume=50, bot=None, channels=2,
                 spatial_pair=None, start_offset=0.0, http_headers=None,
                 start_offset_received_at=None, canonical_url=None):
        super().__init__(daemon=True)
        self.game = game
        self.bot = bot
        self.audio_url = audio_url
        # The stable YouTube page URL (if known). When a freshly resolved
        # googlevideo URL 403s at startup, re-resolving THIS url yields a new
        # signed URL+headers — retrying the same stale URL never helps.
        self.canonical_url = canonical_url
        self.source = source  # cyal OpenAL source
        self.volume = volume
        self.start_offset = float(start_offset or 0.0)
        self.start_offset_received_at = start_offset_received_at
        self.http_headers = dict(http_headers or {})
        # Spatial stereo pair (jukeboxes): two MONO sources placed at the same
        # spot minus/plus a small offset, fed with the LEFT and RIGHT channels
        # of a STEREO decode. Two positioned mono sources produce a real stereo
        # image when you stand close, which naturally collapses toward mono at
        # distance — exactly how piano/drum sounds are anchored in the world.
        # `spatial_pair` is (src_l, src_r, reference_distance, max_distance).
        self.spatial_pair = spatial_pair
        if self.spatial_pair:
            self.spatial_src_l, self.spatial_src_r = spatial_pair[0], spatial_pair[1]
            self.spatial_ref = float(spatial_pair[2])
            self.spatial_max = float(spatial_pair[3])
            self.spatial_base_gain = max(0.0, min(1.0, volume / 100.0))
            self.channels = 2  # decode interleaved stereo, split per channel
        else:
            self.channels = int(channels)
        self.BUFFER_SIZE = self.SAMPLES_PER_BUFFER * self.channels * 2
        self.running = True
        self.paused = False
        self._lock = threading.Lock()
        self._cleanup_lock = threading.Lock()
        self._cleaned_up = False
        self.ready_event = threading.Event()
        self.failure_reason = None
        self.completed_normally = False
        self.process = None
        self._buffer_pool = []       # Reusable buffer objects
        self._pause_buffer = deque() # Store data read while paused
        from pyogg import OpusEncoder
        self.encoder = OpusEncoder()
        self.encoder.set_application('audio')
        self.encoder.set_channels(1)  # Opus network stream is ALWAYS MONO
        self.encoder.set_sampling_frequency(48000)
        self.last_send_time = None
        # Keep broadcast latency bounded.  A network hiccup must discard stale
        # music frames instead of building an ever-growing backlog that later
        # reaches each PA cabinet at a different time.
        self.network_queue = queue.Queue(maxsize=50)
        self.sender_thread = None
        # Versioned performance timeline. The delayed PCM deque mirrors the
        # ten OpenAL pre-buffer frames, so normal Music Broadcast packets leave
        # at the same media position the performer is hearing locally. PA uses
        # its proven legacy path and is deliberately not changed here.
        self._timeline_lock = threading.Lock()
        self._timeline_epoch = random.getrandbits(32) or 1
        self._timeline_next_seq = 0
        self._timeline_last_sent_seq = None
        self._timeline_delay = deque()

    def _all_sources(self):
        """All OpenAL sources this stream feeds (1 normal, 2 for spatial pairs)."""
        if self.spatial_pair:
            return (self.spatial_src_l, self.spatial_src_r)
        return (self.source,)

    def _play_all(self):
        for src in self._all_sources():
            try:
                src.play()
            except Exception:
                pass

    def _all_playing(self):
        for src in self._all_sources():
            try:
                if src.state != cyal.SourceState.PLAYING:
                    return False
            except Exception:
                return False
        return True

    def _buffers_queued(self):
        total = 0
        for src in self._all_sources():
            try:
                total += src.buffers_queued
            except Exception:
                pass
        return total

    @staticmethod
    def _split_stereo_16(data):
        """Split interleaved s16le stereo bytes into (left, right) mono bytes."""
        import array
        samples = array.array('h')
        samples.frombytes(data)
        return samples[0::2].tobytes(), samples[1::2].tobytes()

    def _update_spatial_gain(self):
        """Linear distance fade for spatial pairs (same behavior as drums' 3D
        stereo): full volume inside the reference distance, linearly down to
        true silence at max_distance, computed from the listener's position."""
        try:
            audio = getattr(self.game, "audio_mngr", None)
            pos = getattr(audio, "position", None)
            if pos is None:
                return
            span = max(0.0001, self.spatial_max - self.spatial_ref)
            for src in (self.spatial_src_l, self.spatial_src_r):
                p = src.position
                dx = pos[0] - p[0]
                dy = pos[1] - p[1]
                dz = pos[2] - p[2]
                dist = (dx * dx + dy * dy + dz * dz) ** 0.5
                if dist <= self.spatial_ref:
                    g = 1.0
                elif dist >= self.spatial_max:
                    g = 0.0
                else:
                    g = 1.0 - (dist - self.spatial_ref) / span
                # Jukebox songs use their own mixer category ("jukebox"), so
                # lowering the music-bot/map-music slider does not silence them.
                music_gain = audio.volume_categories.get("jukebox", [100])[0] / 100.0
                src.gain = self.spatial_base_gain * music_gain * g
        except Exception:
            pass

    def set_volume(self, volume):
        """Update playback volume in real time."""
        self.volume = volume
        self.spatial_base_gain = max(0.0, min(1.0, volume / 100.0))
        if self.spatial_pair:
            self._update_spatial_gain()
        elif self.source:
            try:
                self.source.gain = max(0.0, min(1.0, volume / 100.0))
            except Exception:
                pass

    def _init_buffer_pool(self):
        """Pre-allocate OpenAL buffers for reuse"""
        for _ in range(self.NUM_BUFFERS):
            try:
                buf = self.game.audio_mngr.context.gen_buffer()
                self._buffer_pool.append(buf)
            except cyal.exceptions.InvalidOperationError:
                # The project audio backend can surface one stale OpenAL error
                # before succeeding; other buffer loaders use the same retry.
                try:
                    buf = self.game.audio_mngr.context.gen_buffer()
                    self._buffer_pool.append(buf)
                except Exception as ex:
                    logger.log_exception(ex, "AudioStreamer._init_buffer_pool retry")
                    break
            except Exception:
                break

    @staticmethod
    def _ffmpeg_header_block(headers):
        """Build an injection-safe CRLF header block for ffmpeg's input."""
        lines = []
        token_chars = "!#$%&'*+-.^_`|~"
        for raw_name, raw_value in (headers or {}).items():
            name = str(raw_name).strip()
            value = str(raw_value).strip()
            if not name or not value:
                continue
            if any(ch in name or ch in value for ch in ('\r', '\n')):
                continue
            if not all(ch.isalnum() or ch in token_chars for ch in name):
                continue
            lines.append(f"{name}: {value}\r\n")
        return "".join(lines)

    def _get_buffer(self):
        """Get a reclaimed buffer without growing the OpenAL pool.

        Streaming must apply backpressure when every pre-allocated buffer is in
        flight.  Allocating another hardware buffer here lets a fast decoder
        queue an entire song and can exhaust the shared audio device.
        """
        self._reclaim_processed()

        if self._buffer_pool:
            return self._buffer_pool.pop(0)
        return None

    def _reclaim_processed(self):
        """Return processed buffers to pool for reuse.
        
        CRITICAL: cyal's unqueue_buffers() returns a SINGLE Buffer object by default,
        not a list. Handle both cases robustly. Spatial pairs drain both sources.
        """
        try:
            for src in self._all_sources():
                while src.buffers_processed > 0:
                    result = src.unqueue_buffers()
                    if result is not None:
                        try:
                            for buf in result:
                                self._buffer_pool.append(buf)
                        except TypeError:
                            # Not iterable — single buffer object (cyal default)
                            self._buffer_pool.append(result)
        except Exception:
            pass

    def _claim_timeline_marker(self):
        with self._timeline_lock:
            seq = self._timeline_next_seq
            self._timeline_next_seq = (seq + 1) & 0xFFFFFFFF
            return self._timeline_epoch, seq

    def performance_timeline_marker(self):
        """Return the last normal-broadcast frame aligned to local playback."""
        if not self.ready_event.is_set() or not self.running:
            return None
        with self._timeline_lock:
            seq = self._timeline_last_sent_seq
            if seq is None:
                return None
            return {
                "version": 1,
                "epoch": self._timeline_epoch,
                "frame_seq": seq,
            }

    def _send_to_network(self, data, timeline_epoch=None, timeline_seq=None):
        """Queue raw PCM chunk to be sent to the network by the sender thread."""
        item = (data, timeline_epoch, timeline_seq)
        try:
            self.network_queue.put_nowait(item)
        except queue.Full:
            try:
                self.network_queue.get_nowait()
                self.network_queue.put_nowait(item)
            except queue.Empty:
                pass

    def _route_aligned_network_frame(self, decoded_frame=None):
        """Advance the normal-broadcast delay line by one media frame.

        Normal Music Broadcast sends the oldest pre-buffered frame, matching
        the performer's OpenAL playhead. Megaphone keeps sending the current
        decoded frame so the just-verified PA transport remains untouched.
        """
        if not self.bot:
            return
        if decoded_frame is not None:
            self._timeline_delay.append(bytes(decoded_frame))
        aligned = self._timeline_delay.popleft() if self._timeline_delay else None
        if getattr(self.bot, 'broadcast_to_megaphone', False):
            if decoded_frame is not None:
                self._send_to_network(decoded_frame)
            return
        if aligned is not None:
            epoch, seq = self._claim_timeline_marker()
            self._send_to_network(aligned, epoch, seq)

    def _network_sender_loop(self):
        """Paced network sending loop running in a separate thread.
        Decouples network scheduling sleeps from local OpenAL playback.
        """
        while self.running:
            try:
                # Wait for a packet, with timeout so we check self.running regularly
                item = self.network_queue.get(timeout=0.1)
                if isinstance(item, tuple) and len(item) == 3:
                    data, timeline_epoch, timeline_seq = item
                else:
                    data, timeline_epoch, timeline_seq = item, None, None
            except queue.Empty:
                data = None
                timeline_epoch = timeline_seq = None

            # Do not leak pre-pause PCM into the next broadcast segment unless live input is present.
            if self.paused:
                data = None

            # Single music slot: when "Broadcast to Megaphone" is ON but another
            # performer holds the music-bot PA slot, keep the MP3 private - only
            # this performer's live instruments (guitar/mic) continue into the
            # PA mix, so two people's music never overlaps on the speakers.
            if (data is not None and self.bot
                    and getattr(self.bot, 'broadcast_to_megaphone', False)
                    and not self.bot._is_music_owner()):
                data = None

            if data is None:
                # Keep the live mix flowing while the music bot broadcast is on
                # OR the performer enabled "Broadcast to Megaphone" (an
                # independent toggle, like piano/drums).
                if self.bot and (
                    getattr(self.bot, 'broadcast_enabled', False)
                    or getattr(self.bot, 'broadcast_to_megaphone', False)
                ):
                    has_guitar = bool(getattr(self.bot, 'guitar_pcm_queue', None) and len(self.bot.guitar_pcm_queue) > 0)
                    has_mic = bool(getattr(self.bot, 'mic_pcm_queue', None) and len(self.bot.mic_pcm_queue) > 0)
                    if has_guitar or has_mic:
                        data = b'\x00' * 3840
                        if not getattr(self.bot, 'broadcast_to_megaphone', False):
                            timeline_epoch, timeline_seq = self._claim_timeline_marker()

            if data is None:
                continue

            # High-resolution time pacing
            now = time.perf_counter()
            if self.last_send_time is not None:
                elapsed = now - self.last_send_time
                target_interval = 0.020  # 20ms per buffer
                if elapsed < target_interval:
                    # Sleep most of the way (subtracting 1ms margin for Windows scheduler inaccuracy)
                    sleep_time = target_interval - elapsed
                    if sleep_time > 0.001:
                        time.sleep(sleep_time - 0.001)
                    # Spin lock for the remaining fraction of a millisecond
                    while time.perf_counter() - self.last_send_time < target_interval:
                        pass
            # Set last_send_time before doing encoding/networking to prevent work time drift
            self.last_send_time = time.perf_counter()

            self._send_to_network_actual(data, timeline_epoch, timeline_seq)

    def _send_to_network_actual(self, data, timeline_epoch=None, timeline_seq=None):
        """Downmix Stereo to Mono, scale volume, encode as Opus, and send to network."""
        try:
            if not self.game or not self.game.network:
                return
                
            # Check if the stream is being broadcast: the music bot broadcast
            # is on, OR the performer enabled "Broadcast to Megaphone" (the
            # PA/megaphone routing is an independent toggle - exactly like
            # piano/drums, so guitar and music reach the PA on their own).
            # A stream WITHOUT a bot (jukebox playback, bot=None) NEVER sends:
            # otherwise the jukebox audio would be re-broadcast to the whole map
            # as the player's own music bot stream (double audio everywhere).
            if not self.bot or not (
                self.bot.broadcast_enabled or self.bot.broadcast_to_megaphone
            ):
                return

            # Downmix 16-bit stereo → 16-bit mono
            import audioop
            mono_data = audioop.tomono(data, 2, 0.5, 0.5)

            # Scale the PCM stream volume according to self.volume and the dynamic ducking multiplier before network broadcast
            current_volume_scale = (self.volume / 100.0)
            if self.bot:
                current_volume_scale *= getattr(self.bot, 'duck_multiplier', 1.0)
            
            if current_volume_scale != 1.0:
                try:
                    mono_data = audioop.mul(mono_data, 2, current_volume_scale)
                except Exception:
                    pass

            from . import consts
            target_channel = consts.CHANNEL_MUSICBOT
            if self.bot and self.bot.broadcast_to_megaphone:
                target_channel = consts.CHANNEL_MEGAPHONE

            # Mix queued live input (voice mic and/or line-in guitar) into the
            # outgoing stream. This happens on BOTH broadcast paths (the 3D
            # music bot channel and the megaphone), so a guitar strum is heard
            # either way - always gated by broadcast_enabled above.
            mic_data = None
            guitar_data = None
            if self.bot:
                try:
                    if getattr(self.bot, 'mic_pcm_queue', None) and self.bot.mic_pcm_queue:
                        mic_data = self.bot.mic_pcm_queue.popleft()
                except Exception:
                    pass
                try:
                    if getattr(self.bot, 'guitar_pcm_queue', None) and self.bot.guitar_pcm_queue:
                        guitar_data = self.bot.guitar_pcm_queue.popleft()
                except Exception:
                    pass
            if mic_data is not None or guitar_data is not None:
                try:
                    def _align(b):
                        if len(b) > len(mono_data):
                            return b[:len(mono_data)]
                        if len(b) < len(mono_data):
                            return b + b'\x00' * (len(mono_data) - len(b))
                        return b
                    # Scale down before mixing to prevent 16-bit overflow clipping
                    mono_data = audioop.mul(mono_data, 2, 0.75)
                    if mic_data is not None:
                        mono_data = audioop.add(
                            mono_data, audioop.mul(_align(mic_data), 2, 0.85), 2)
                    if guitar_data is not None:
                        mono_data = audioop.add(
                            mono_data, audioop.mul(_align(guitar_data), 2, 0.85), 2)
                except Exception:
                    pass

            # When routing through the megaphone, feed the mixed stream into the
            # local PA sidechain (main thread) so the broadcaster hears their own
            # music/instruments through the speakers with zero latency - the server
            # no longer echoes the broadcast back to the sender.
            if self.bot and self.bot.broadcast_to_megaphone:
                try:
                    gp = self.bot._find_gameplay()
                    if gp is not None:
                        from . import voice_chat
                        if hasattr(voice_chat, '_feed_local_megaphone_direct'):
                            local_pcm = bytes(mono_data)
                            voice_chat._feed_local_megaphone_direct(gp, local_pcm, producer='music')
                except Exception:
                    pass

            encoded = self.encoder.encode(bytearray(mono_data))
            if (target_channel == consts.CHANNEL_MUSICBOT
                    and getattr(self.game, 'music_timeline_supported', False)
                    and timeline_epoch is not None and timeline_seq is not None):
                target_channel = consts.CHANNEL_MUSICBOT_TIMELINE
                encoded = struct.pack(
                    ">BII", 1, int(timeline_epoch) & 0xFFFFFFFF,
                    int(timeline_seq) & 0xFFFFFFFF,
                ) + bytes(encoded)
                with self._timeline_lock:
                    self._timeline_last_sent_seq = int(timeline_seq) & 0xFFFFFFFF
            self.game.network.send(target_channel, "n/a", encoded, reliable=False)
        except Exception:
            pass



    def _queue_local(self, data):
        """Queue a chunk of PCM data to the LOCAL OpenAL source(s)."""
        if self.spatial_pair:
            return self._queue_local_spatial(data)
        self._reclaim_processed()
        buf = self._get_buffer()
        if buf is None:
            return False
        try:
            # Local playback is STEREO for the personal music bot (highest
            # quality); MONO for plain jukebox streams.
            if self.channels == 1:
                buf.set_data(data, sample_rate=48000, format=cyal.BufferFormat.MONO16)
            else:
                buf.set_data(data, sample_rate=48000, format=cyal.BufferFormat.STEREO16)
            self.source.queue_buffers(buf)
            return True
        except Exception:
            return False

    def _queue_local_spatial(self, data):
        """Queue a STEREO chunk split into L/R MONO buffers on the two positioned
        sources (jukebox stereo-spatial playback, same as drums)."""
        self._reclaim_processed()
        buf_l = self._get_buffer()
        if buf_l is None:
            return False
        buf_r = self._get_buffer()
        if buf_r is None:
            # The left buffer has not been queued yet, so return it to the
            # shared pool and retry this PCM frame on the next iteration.
            self._buffer_pool.append(buf_l)
            return False
        try:
            left, right = self._split_stereo_16(data)
            buf_l.set_data(left, sample_rate=48000, format=cyal.BufferFormat.MONO16)
            buf_r.set_data(right, sample_rate=48000, format=cyal.BufferFormat.MONO16)
            self.spatial_src_l.queue_buffers(buf_l)
            self.spatial_src_r.queue_buffers(buf_r)
            return True
        except Exception:
            return False

    def _read_prebuffer(self):
        """Read and queue the startup buffer for the current ffmpeg process."""
        pre_buffered = 0
        leftover = b''
        deadline = time.time() + 12.0
        while (self.running and pre_buffered < self.PRE_BUFFER_COUNT
               and time.time() < deadline):
            if self.process.poll() is not None and not leftover:
                break
            try:
                needed = self.BUFFER_SIZE - len(leftover)
                chunk = self.process.stdout.read(needed)
                if chunk:
                    leftover += chunk
                elif self.process.poll() is not None:
                    break
                else:
                    time.sleep(0.02)
            except Exception:
                time.sleep(0.02)

            while (len(leftover) >= self.BUFFER_SIZE
                   and pre_buffered < self.PRE_BUFFER_COUNT):
                data = leftover[:self.BUFFER_SIZE]
                leftover = leftover[self.BUFFER_SIZE:]
                with self._lock:
                    if self._queue_local(data):
                        pre_buffered += 1
                        if self.bot:
                            self._timeline_delay.append(bytes(data))
        return pre_buffered, leftover

    def run(self):
        if not FFMPEG_PATH:
            print("[MusicBot] ffmpeg not found!")
            speak("ffmpeg not found.")
            self.failure_reason = "ffmpeg not found"
            self.running = False
            self._cleanup()
            return

        # Resolve the stream URL if target_url is not already a direct stream.
        canonical_url = self.canonical_url
        target_url = self.audio_url
        input_headers = dict(self.http_headers)
        if "youtube.com" in target_url or "youtu.be" in target_url:
            canonical_url = target_url
            target_url = ""
        if canonical_url and not target_url.startswith(("http://", "https://")):
            fresh = YouTubeSearcher.get_stream_info(canonical_url)
            if fresh:
                target_url = fresh['url']
                input_headers = fresh.get('http_headers') or {}

        def _build_cmd():
            cmd = [FFMPEG_PATH]
            if (target_url.startswith(("http://", "https://"))
                    and "googlevideo.com" not in target_url.lower()):
                cmd.extend([
                    '-reconnect', '1',
                    '-reconnect_streamed', '1',
                    '-reconnect_on_http_error', '403,429,5xx',
                    '-reconnect_delay_max', '5',
                    '-reconnect_delay_total_max', '12',
                ])
            user_agent = next(
                (str(value).strip() for name, value in input_headers.items()
                 if str(name).lower() == 'user-agent'),
                '',
            )
            if user_agent and '\r' not in user_agent and '\n' not in user_agent:
                # ffmpeg has a dedicated option for User-Agent. Using it avoids
                # a built-in Lavf agent overriding the yt-dlp authorization UA.
                cmd.extend(['-user_agent', user_agent])
            header_block = self._ffmpeg_header_block({
                name: value for name, value in input_headers.items()
                if str(name).lower() != 'user-agent'
            })
            if header_block:
                cmd.extend(['-headers', header_block])
            effective_offset = getattr(self, "start_offset", 0.0)
            # Only compensate the client's resolve delay when RESUMING mid-song
            # (start_offset > 0). A fresh song must start at 0: adding the
            # yt-dlp resolve time here made the jukebox skip past the intro
            # every time the direct fallback was used.
            if self.start_offset_received_at is not None and effective_offset > 0.0:
                effective_offset += max(0.0, time.monotonic() - self.start_offset_received_at)
            # Input authorization must precede seek. YouTube's signed range
            # request can return 403 when -ss is placed before these headers.
            if effective_offset > 0.5:
                cmd.extend(['-ss', f"{effective_offset:.2f}"])
            cmd.extend([
                '-re',  # Read/decode at media rate; network and OpenAL consume 20 ms frames.
                '-i', target_url,
                '-f', 's16le',
                '-ar', '48000',
                '-ac', str(self.channels),
                '-loglevel', 'error',
                'pipe:1'
            ])
            return cmd

        try:
            cmd = _build_cmd()
        except Exception as ex:
            logger.log_exception(ex, "AudioStreamer.run command")
            self.failure_reason = "playback command error"
            self.running = False
            self._cleanup()
            return

        # Initialize buffer pool
        self._init_buffer_pool()

        # Start background network sender thread ONLY when this stream actually
        # broadcasts (bot attached). Jukebox players (bot=None) play locally only
        # and must never re-broadcast the song as their own music bot stream.
        if self.bot:
            self.sender_thread = threading.Thread(target=self._network_sender_loop, daemon=True)
            self.sender_thread.start()

        # === Pre-buffer phase: fill LOCAL buffers before starting playback ===
        # Some fresh googlevideo URLs briefly return 403 while their CDN edge
        # authorization propagates. Retry — first with the same URL+headers, then
        # with a freshly RE-RESOLVED URL (a stale signed URL 403s forever no
        # matter how many times the identical command is retried). All work stays
        # on this worker thread.
        pre_buffered = 0
        _pre_leftover = b''
        error_detail = ""
        for attempt in range(4):
            try:
                self.process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
                )
            except Exception as ex:
                logger.log_exception(ex, "AudioStreamer.run Popen")
                self.failure_reason = "playback launch error"
                break

            pre_buffered, _pre_leftover = self._read_prebuffer()
            if pre_buffered > 0 or not self.running:
                break

            try:
                if self.process.poll() is not None:
                    error_detail = self.process.stderr.read(4096).decode(
                        'utf-8', 'replace'
                    ).strip()
            except Exception:
                error_detail = ""

            try:
                self.process.kill()
                self.process.wait(timeout=2)
            except Exception:
                pass
            self.process = None

            retryable = any(tok in error_detail for tok in ("403", "429", "503", "connection", "timeout", "reset"))
            if not retryable or attempt >= 3:
                break
            if attempt >= 1 and canonical_url and ("youtube.com" in canonical_url or "youtu.be" in canonical_url):
                # The exact URL+headers already failed once — grab a fresh
                # signed stream URL instead of re-running the same command.
                fresh = YouTubeSearcher.get_stream_info(canonical_url)
                if fresh and fresh.get('url'):
                    target_url = fresh['url']
                    input_headers = fresh.get('http_headers') or {}
                    try:
                        cmd = _build_cmd()
                    except Exception:
                        pass
            time.sleep(1.0 + attempt)

        if pre_buffered == 0 and not self.running:
            # Intentional cancellation (map change, newer playback generation,
            # or stop) is not a load failure and must stay silent.
            self._cleanup()
            return

        if pre_buffered == 0:
            if self.failure_reason is None:
                self.failure_reason = "stream produced no audio"
            error_summary = ""
            if error_detail:
                error_summary = next(
                    (line.strip() for line in reversed(error_detail.splitlines())
                     if line.strip()),
                    "",
                )
            logger.log(
                "[AudioStreamer] stream produced no audio (channels="
                f"{self.channels}, url={self.audio_url[:80]!r}, "
                f"ffmpeg={error_summary[-240:]!r})"
            )
            if self.bot is None:
                speak("The jukebox song could not be loaded. Try another song.")
            self.running = False
            self._cleanup()
            return

        # Start local playback after pre-buffering (spatial pairs also need
        # their distance fade computed before the first audible sample).
        if pre_buffered > 0:
            if self.spatial_pair:
                self._update_spatial_gain()
            self._play_all()
            self.ready_event.set()
            # Frame zero leaves at the same instant local OpenAL begins frame
            # zero. Subsequent decoded frames advance this fixed delay line.
            self._route_aligned_network_frame()

        # === Streaming loop ===
        eof = False
        _leftover = _pre_leftover  # Carry over any leftover bytes from pre-buffer
        while self.running:
            if self.paused:
                time.sleep(0.05)
                continue

            data = None
            if not eof:
                # Accumulate partial reads until we have a full frame.
                # Only set eof when read() returns empty (ffmpeg closed pipe).
                while len(_leftover) < self.BUFFER_SIZE:
                    chunk = self.process.stdout.read(self.BUFFER_SIZE - len(_leftover))
                    if not chunk:  # Real EOF: ffmpeg closed the pipe
                        eof = True
                        break
                    _leftover += chunk
                if len(_leftover) >= self.BUFFER_SIZE:
                    data = _leftover[:self.BUFFER_SIZE]
                    _leftover = _leftover[self.BUFFER_SIZE:]
                elif _leftover and eof:
                    # Flush remaining partial data at the very end
                    data = _leftover
                    _leftover = b''

            if not self.running:
                break

            # === NETWORK: Send at the performer's audible media position ===
            if data:
                self._route_aligned_network_frame(data)
            elif eof and self._timeline_delay:
                # ffmpeg has ended, but OpenAL still owns the pre-buffer tail.
                # Flush one aligned frame per loop so listeners hear the same
                # ending instead of losing the final ~180ms.
                self._route_aligned_network_frame()

            # === LOCAL: Buffer for OpenAL playback ===
            if data:
                self._pause_buffer.append(data)

            with self._lock:
                if not self.running:
                    break
                try:
                    # [FIX]: Always reclaim processed buffers so buffers_queued drops to 0 at EOF
                    self._reclaim_processed()

                    # Keep the spatial pair's distance fade up to date as the
                    # listener walks (cheap: two distance checks per frame).
                    if self.spatial_pair:
                        self._update_spatial_gain()

                    # Drain pause buffer into OpenAL
                    while self._pause_buffer:
                        chunk = self._pause_buffer[0]
                        if self._queue_local(chunk):
                            self._pause_buffer.popleft()
                        else:
                            break  # No available OpenAL buffers, wait

                    # Restart if source stopped and we have buffers queued (only if new buffers are queued and not EOF)
                    if not eof and not self._all_playing() and self._buffers_queued() > 0:
                        self._play_all()
                except Exception:
                    pass

            if (eof and not self._pause_buffer
                    and not self._timeline_delay
                    and self._buffers_queued() == 0):
                break
                
            # Sleep a bit to prevent busy-waiting ONLY if we didn't read any data
            # (e.g. EOF reached, but OpenAL is still playing the last few buffers).
            # Do NOT sleep during normal streaming — ffmpeg's '-re' flag already
            # paces stdout.read() perfectly. Sleeping here adds Windows scheduler jitter.
            if not data:
                time.sleep(0.02)

        # Wait for remaining buffers to finish playing
        if self.running:
            try:
                # Keep checking and unqueuing until all queued buffers are processed
                while self._buffers_queued() > 0 and self.running:
                    with self._lock:
                        self._reclaim_processed()
                        if self.spatial_pair:
                            self._update_spatial_gain()
                    # If we are paused at the very end, wait here until resumed
                    if not self.paused and not self._all_playing() and self._buffers_queued() > 0:
                        self._play_all()
                    time.sleep(0.05)
            except Exception:
                pass

        if self.running:
            # Distinguish a natural song end from a mid-song ffmpeg death (403
            # on a CDN reconnect, connection reset, ...). ffmpeg exits 0 on a
            # clean EOF; any other exit code after audio already started means
            # the stream died EARLY. Mark it a failure so the jukebox recovery
            # watchdog rebuilds with a fresh resolve instead of treating the
            # silence as a finished song ("music disappears before the end").
            exit_code = None
            try:
                if self.process is not None:
                    exit_code = self.process.poll()
            except Exception:
                exit_code = None
            if exit_code not in (None, 0):
                self.failure_reason = f"ffmpeg exited early (code {exit_code})"
            else:
                self.completed_normally = True

        # Cleanup
        self._cleanup()

    def _cleanup(self):
        """Clean up ffmpeg process and buffers"""
        with self._cleanup_lock:
            if self._cleaned_up:
                return
            self._cleaned_up = True
        self.running = False
        if getattr(self, "ytdlp_process", None):
            try:
                self.ytdlp_process.kill()
                self.ytdlp_process.wait(timeout=2)
            except Exception:
                pass
            self.ytdlp_process = None
        if self.process:
            try:
                self.process.kill()
                self.process.wait(timeout=2)
            except Exception:
                pass
            self.process = None
        # Drain remaining buffers (both sources for spatial pairs)
        for src in self._all_sources():
            try:
                src.stop()
                drain_limit = 64
                while src.buffers_processed > 0 and drain_limit > 0:
                    src.unqueue_buffers()
                    drain_limit -= 1
            except Exception:
                pass
        self._buffer_pool.clear()
        self._pause_buffer.clear()
        # Drain the network queue to free references
        while not self.network_queue.empty():
            try:
                self.network_queue.get_nowait()
            except Exception:
                pass

    def stop(self):
        self.running = False
        self._cleanup()

    def resume_output_if_buffered(self):
        """Restart a stopped OpenAL output without replacing this stream.

        This is intentionally limited to already queued frames.  Starting a
        fresh stream here would lose timing and can duplicate a broadcast;
        the decoder thread remains the owner of buffering new audio.
        """
        with self._lock:
            if not self.running or self.paused or self._buffers_queued() <= 0:
                return False
            if not self._all_playing():
                self._play_all()
                return True
        return False

    def set_pause(self, paused):
        self.paused = paused
        if paused:
            # The local source can pause in-place, but queued network PCM has
            # no timestamp.  It must be discarded or listeners will hear it
            # after resume as a second, delayed copy of the song.
            while True:
                try:
                    self.network_queue.get_nowait()
                except queue.Empty:
                    break
        with self._lock:
            try:
                if paused:
                    self.source.pause()
                else:
                    self.source.play()
            except Exception:
                pass


class LiveRelayStreamer(threading.Thread):
    """Stand-alone streaming thread for live instrument (guitar/mic) PCM when no MP3 audio is playing."""
    def __init__(self, game, bot):
        super().__init__(daemon=True)
        self.game = game
        self.bot = bot
        self.running = True
        from pyogg import OpusEncoder
        self.encoder = OpusEncoder()
        self.encoder.set_application('audio')
        self.encoder.set_channels(1)  # Opus network stream is MONO
        self.encoder.set_sampling_frequency(48000)
        self.last_send_time = None

    def stop(self):
        self.running = False

    def run(self):
        import audioop
        from . import consts
        while self.running and self.bot and (
            getattr(self.bot, 'broadcast_enabled', False)
            or getattr(self.bot, 'broadcast_to_megaphone', False)
        ):
            # If main AudioStreamer MP3 thread is active, let it handle the network stream
            if self.bot.streamer and self.bot.streamer.is_alive():
                break

            guitar_data = None
            mic_data = None
            try:
                if getattr(self.bot, 'guitar_pcm_queue', None) and self.bot.guitar_pcm_queue:
                    guitar_data = self.bot.guitar_pcm_queue.popleft()
            except Exception:
                pass
            try:
                if getattr(self.bot, 'mic_pcm_queue', None) and self.bot.mic_pcm_queue:
                    mic_data = self.bot.mic_pcm_queue.popleft()
            except Exception:
                pass

            if guitar_data is None and mic_data is None:
                time.sleep(0.010)
                continue

            # Base silent mono PCM buffer (20ms at 48kHz = 960 samples * 2 bytes = 1920 bytes)
            mono_data = b'\x00' * 1920
            try:
                def _align(b, length=1920):
                    if len(b) > length:
                        return b[:length]
                    if len(b) < length:
                        return b + b'\x00' * (length - len(b))
                    return b

                if guitar_data is not None:
                    mono_data = audioop.add(mono_data, audioop.mul(_align(guitar_data), 2, 0.85), 2)
                if mic_data is not None:
                    mono_data = audioop.add(mono_data, audioop.mul(_align(mic_data), 2, 0.85), 2)

                current_volume_scale = (self.bot.volume / 100.0) * getattr(self.bot, 'duck_multiplier', 1.0)
                if current_volume_scale != 1.0:
                    mono_data = audioop.mul(mono_data, 2, current_volume_scale)

                target_channel = consts.CHANNEL_MEGAPHONE if self.bot.broadcast_to_megaphone else consts.CHANNEL_MUSICBOT

                # Local zero-latency PA monitoring (the server no longer echoes
                # the megaphone broadcast back to the sender).
                if self.bot.broadcast_to_megaphone:
                    try:
                        gp = self.bot._find_gameplay()
                        if gp is not None:
                            from . import voice_chat
                            if hasattr(voice_chat, '_feed_local_megaphone_direct'):
                                local_pcm = bytes(mono_data)
                                voice_chat._feed_local_megaphone_direct(gp, local_pcm, producer='music')
                    except Exception:
                        pass

                # Rate-limit to 20ms pacing
                now = time.perf_counter()
                if self.last_send_time is not None:
                    elapsed = now - self.last_send_time
                    if elapsed < 0.020:
                        sleep_time = 0.020 - elapsed
                        if sleep_time > 0.001:
                            time.sleep(sleep_time - 0.001)
                        while time.perf_counter() - self.last_send_time < 0.020:
                            pass
                self.last_send_time = time.perf_counter()

                encoded = self.encoder.encode(bytearray(mono_data))
                if self.game and self.game.network:
                    self.game.network.send(target_channel, "n/a", encoded, reliable=False)
            except Exception:
                time.sleep(0.010)


class MapMusicBot:
    """Music Bot — searches YouTube and streams audio in real-time.
    Falls back to local files when YouTube is unavailable.
    
    Controls are resolved from the player's key bindings in gameplay.py.
    Modifier combinations continue to use the configured Music Bot key.
    """

    def __init__(self, game):
        self.game = game
        # OpenAL source for streaming (not using soundgroup — direct source for buffer queuing)
        self.stream_source = None
        # Local file playback
        self.soundgroup = game.audio_mngr.create_soundgroup(direct=True)
        self.current_local_sound = None

        # State
        self.current_title = ""
        self.playing = False
        self.paused = False
        self.mode = "idle"  # "idle", "youtube", "local"

        # YouTube streamer thread
        self.streamer = None
        self.live_relay_streamer = None
        self._stream_announced = False

        # Main-thread playback generation. Background URL resolution captures a
        # generation but may never create audio after a newer play or Stop.
        self._playback_generation = 0
        self._playback_generation_lock = threading.Lock()

        # Last played YouTube info (for replay)
        self.last_youtube_url = ""
        self.last_youtube_title = ""

        # Local playlist (fallback)
        self.playlist = []
        self.playlist_index = 0

        # Personal Favorites / custom-playlist queue.  This is separate from
        # map music, which has its own local playlist state above.
        self.play_queue = []
        self.play_queue_index = -1
        self.play_queue_label = ""

        # A shuffled Favorites feed. It preserves the user's current broadcast
        # routing and never changes saved playlists.
        self.feed_tracks = []
        self.feed_index = -1

        # Settings
        self.volume = options.get("music_bot_volume", 50)
        self.enabled = options.get("music_bot_enabled", True)
        self.broadcast_enabled = False  # Disabled by default (Private listening mode)
        self.broadcast_to_megaphone = False
        # Line-in guitar raw PCM queue: the instrument input appends 20 ms
        # mono16 frames while guitar mode is on and this broadcast is enabled;
        # AudioStreamer mixes them into the outgoing stream.
        self.guitar_pcm_queue = deque(maxlen=10)

        # Personal Playlist & Favorites Manager (Stored locally on Client)
        from .playlist_manager import PlaylistManager
        self.playlist_mgr = PlaylistManager()
        self.current_target = ""
        self.current_source = "youtube"

        # Unified Last Played Track State (for Ctrl+M Replay and Shift+M Pause/Resume)
        self.last_track_title = ""
        self.last_track_target = ""
        self.last_track_source = "youtube"

        # Search state
        self.searching = False
        self.is_loading_stream = False
        self.search_results = []

        # Environmental reverb tracking
        self._current_reverb_slot = None

    def toggle_broadcast(self):
        """Toggle network broadcasting on/off."""
        if getattr(self.game, 'pong_mode', False) and not getattr(self.game, 'pong_arcade', False):
            from .speech import speak
            if getattr(self.game, 'pong_training', False):
                speak("Broadcasting is disabled in training mode.")
            else:
                speak("Broadcasting is disabled in competition matches.")
            return

        self.broadcast_enabled = not self.broadcast_enabled
        from .speech import speak
        if self.broadcast_enabled:
            speak("Music broadcast enabled. Others can hear the music.")
        else:
            speak("Music broadcast disabled. Private listening mode.")
            if self.broadcast_to_megaphone:
                self.broadcast_to_megaphone = False
                from . import consts
                self.game.network.send(
                    consts.CHANNEL_MISC,
                    "megaphone_broadcast_lock",
                    {"locked": False}
                )

    def _create_stream_source(self):
        """Create a fresh OpenAL source for streaming.
        Uses direct_channels=True for clear stereo, plus EFX reverb send
        for environmental atmosphere.
        """
        self._destroy_stream_source()
        try:
            src = self.game.audio_mngr.context.gen_source()
            src.direct_channels = True
            src.spatialize = False
            music_vol = self.game.audio_mngr.volume_categories.get("music", [100])[0] / 100
            src.gain = (self.volume / 100) * music_vol
            self.stream_source = src
            # Apply current map reverb immediately
            self._sync_map_reverb()
        except Exception as ex:
            print(f"[MusicBot] Error creating source: {ex}")

    def _destroy_stream_source(self):
        if self.stream_source:
            try:
                self.stream_source.stop()
                drain_limit = 64
                while self.stream_source.buffers_processed > 0 and drain_limit > 0:
                    self.stream_source.unqueue_buffers()
                    drain_limit -= 1
                drain_limit = 64
                while self.stream_source.buffers_queued > 0 and drain_limit > 0:
                    self.stream_source.unqueue_buffers()
                    drain_limit -= 1
                self.stream_source.delete()
            except Exception:
                pass
            self.stream_source = None

    def _begin_playback_generation(self):
        """Invalidate pending starts and reserve a generation for new playback."""
        with self._playback_generation_lock:
            self._playback_generation += 1
            return self._playback_generation

    def _is_current_playback_generation(self, generation):
        with self._playback_generation_lock:
            return generation == self._playback_generation

    # === YouTube Playback ===

    def open_search(self):
        """Open search dialog — music keeps playing until a new song is selected."""
        if not self.enabled:
            speak("Music Bot is off. Press Ctrl Shift M to enable.")
            return
        if self.searching:
            speak("Still searching, please wait. Press Ctrl M to cancel.")
            return

        # Don't stop current music — let it play while user searches
        self.game.put(lambda: self._show_mode_menu())

    def _show_mode_menu(self):
        """Show menu to choose between YouTube search and Local playlist"""
        from . import menu as menu_mod, menus

        gp = self._find_gameplay()
        if not gp:
            return

        def go_search():
            gp.pop_last_substate()
            self._open_search_input()

        def go_local():
            gp.pop_last_substate()
            self._open_file_dialog()

        def go_playlists():
            gp.pop_last_substate()
            self._open_playlists_menu()

        def go_personal_feed():
            gp.pop_last_substate()
            self._show_personal_feed_menu()

        def go_help():
            gp.pop_last_substate()
            self._show_help_menu()

        m = menu_mod.Menu(self.game, "Music Bot Mode", parrent=gp)
        items = [
            ("Search YouTube", go_search),
            ("Choose Local File", go_local),
            ("My Playlists & Favorites", go_playlists),
            ("Personal Music Feed", go_personal_feed),
        ]
        
        # Show the megaphone routing option only when the server explicitly granted
        # broadcast permission (canBroadcastMegaphone()). The server is the single
        # source of truth; gating on it keeps the client menu and the server lock
        # perfectly in sync (no client-side role guessing).
        can_broadcast_megaphone = getattr(gp, 'can_broadcast_megaphone', False) if gp else False

        if can_broadcast_megaphone:
            def get_megaphone_label():
                status = "ON" if self.broadcast_to_megaphone else "OFF"
                return f"Broadcast to Megaphone: {status}"
                
            def toggle_megaphone_routing():
                # No broadcast_enabled gate: piano broadcast is independent of music
                # playback, so performers can broadcast the piano through PA speakers
                # without starting a music track first.
                self.broadcast_to_megaphone = not self.broadcast_to_megaphone
                status_text = "enabled" if self.broadcast_to_megaphone else "disabled"
                speak(f"Broadcast to megaphone {status_text}.")
                m.speak_current_item()

                # Send lock request to the server
                from . import consts
                self.game.network.send(
                    consts.CHANNEL_MISC,
                    "megaphone_broadcast_lock",
                    {"locked": self.broadcast_to_megaphone}
                )
                
            items.append((get_megaphone_label, toggle_megaphone_routing))

        items.extend([
            ("Help", go_help),
            ("Cancel", lambda: gp.pop_last_substate())
        ])
        m.add_items(items)
        menus.set_default_sounds(m)
        gp.add_substate(m)

    def _save_current_to_favorites(self):
        """Save currently playing track to Favorites"""
        from .speech import speak
        if not self.current_title or not self.current_target:
            speak("No track is currently playing.")
            return

        added = self.playlist_mgr.add_favorite(self.current_title, self.current_target, self.current_source)
        if added:
            speak(f"Saved {self.current_title} to favorites.")
        else:
            speak(f"{self.current_title} is already in favorites.")

    def _show_personal_feed_menu(self):
        """Open the shuffled Favorites feed controls."""
        from . import menu as menu_mod, menus
        gp = self._find_gameplay()
        if not gp:
            return

        def start_feed():
            gp.pop_last_substate()
            self._start_personal_feed()

        def next_feed():
            gp.pop_last_substate()
            self._next_personal_feed()

        m = menu_mod.Menu(self.game, "Personal Music Feed", parrent=gp)
        m.add_items([
            ("Start shuffled Favorites feed", start_feed),
            ("Next feed song", next_feed),
            ("Back", lambda: (gp.pop_last_substate(), self._show_mode_menu())),
        ])
        menus.set_default_sounds(m)
        gp.add_substate(m)

    def _clear_personal_feed(self):
        self.feed_tracks = []
        self.feed_index = -1

    def _start_personal_feed(self):
        favorites = [dict(track) for track in self.playlist_mgr.get_favorites() if track.get("target")]
        if not favorites:
            speak("Your Favorites are empty. Save songs first, then start the feed.")
            return

        random.shuffle(favorites)
        self.feed_tracks = favorites
        self.feed_index = 0
        speak(f"Personal Music Feed started. {len(favorites)} songs from Favorites.")
        self._play_personal_feed_track()

    def _next_personal_feed(self):
        if not self.feed_tracks:
            self._start_personal_feed()
            return
        self.feed_index = (self.feed_index + 1) % len(self.feed_tracks)
        self._play_personal_feed_track()

    def previous_feed_track(self):
        """Return to the prior song in an active Personal Music Feed."""
        if not self.feed_tracks:
            speak("Personal Music Feed is not active.")
            return
        self.feed_index = (self.feed_index - 1) % len(self.feed_tracks)
        self._play_personal_feed_track()

    def next_feed_track(self):
        """Advance an active Personal Music Feed without changing normal playlists."""
        if not self.feed_tracks:
            speak("Personal Music Feed is not active.")
            return
        self._next_personal_feed()

    def _play_personal_feed_track(self):
        if not (0 <= self.feed_index < len(self.feed_tracks)):
            return
        track = self.feed_tracks[self.feed_index]
        self.play_single_track(
            track.get("title", "Unknown"),
            track.get("target", ""),
            track.get("source", "youtube"),
            preserve_feed=True,
        )

    def _open_playlists_menu(self):
        """Show main My Playlists & Favorites menu"""
        from . import menu as menu_mod, menus
        gp = self._find_gameplay()
        if not gp:
            return

        m = menu_mod.Menu(self.game, "My Playlists & Favorites", parrent=gp)
        items = []

        if self.current_title and self.current_target:
            def fav_current():
                gp.pop_last_substate()
                self._save_current_to_favorites()
            items.append(("Save Current Song to Favorites", fav_current))

        def go_favorites():
            gp.pop_last_substate()
            self._show_favorites_menu()

        def go_create_playlist():
            gp.pop_last_substate()
            self._prompt_create_playlist()

        items.append(("All Favorites", go_favorites))
        items.append(("Create New Playlist", go_create_playlist))

        # List custom playlists
        playlist_names = self.playlist_mgr.get_playlist_names()
        for p_name in playlist_names:
            def make_p_cb(name):
                return lambda: (gp.pop_last_substate(), self._show_custom_playlist_menu(name))
            items.append((f"Playlist: {p_name}", make_p_cb(p_name)))

        items.append(("Back", lambda: (gp.pop_last_substate(), self._show_mode_menu())))
        m.add_items(items)
        menus.set_default_sounds(m)
        gp.add_substate(m)

    def _show_favorites_menu(self):
        """Show menu of favorite tracks"""
        from . import menu as menu_mod, menus
        from .speech import speak
        gp = self._find_gameplay()
        if not gp:
            return

        favs = self.playlist_mgr.get_favorites()
        if not favs:
            speak("No favorite tracks saved yet.")
            return

        m = menu_mod.Menu(self.game, "All Favorites", parrent=gp)
        items = []

        def play_all():
            gp.pop_last_substate()
            self._start_track_queue(favs, "Favorites")

        items.append(("Play All Favorites", play_all))
        for track in favs:
            title = track.get("title", "Unknown")
            target = track.get("target", "")
            source = track.get("source", "youtube")

            def make_fav_item_cb(t_title, t_target, t_source):
                return lambda: (gp.pop_last_substate(), self._show_track_action_menu(t_title, t_target, t_source, is_favorite=True))

            items.append((title, make_fav_item_cb(title, target, source)))

        items.append(("Back", lambda: (gp.pop_last_substate(), self._open_playlists_menu())))
        m.add_items(items)
        menus.set_default_sounds(m)
        gp.add_substate(m)

    def _show_custom_playlist_menu(self, playlist_name):
        """Show tracks inside a custom playlist"""
        from . import menu as menu_mod, menus
        from .speech import speak
        gp = self._find_gameplay()
        if not gp:
            return

        tracks = self.playlist_mgr.get_playlist_tracks(playlist_name)
        m = menu_mod.Menu(self.game, f"Playlist: {playlist_name}", parrent=gp)
        items = []

        if tracks:
            def play_all():
                gp.pop_last_substate()
                self._play_playlist_all(playlist_name)

            items.append(("Play All Tracks", play_all))

        def delete_playlist():
            gp.pop_last_substate()
            self.playlist_mgr.delete_playlist(playlist_name)
            speak(f"Deleted playlist {playlist_name}.")

        items.append(("Delete Playlist", delete_playlist))

        for track in tracks:
            title = track.get("title", "Unknown")
            target = track.get("target", "")
            source = track.get("source", "youtube")

            def make_tr_cb(t_title, t_target, t_source, p_name):
                return lambda: (gp.pop_last_substate(), self._show_track_action_menu(t_title, t_target, t_source, playlist_name=p_name))

            items.append((title, make_tr_cb(title, target, source, playlist_name)))

        items.append(("Back", lambda: (gp.pop_last_substate(), self._open_playlists_menu())))
        m.add_items(items)
        menus.set_default_sounds(m)
        gp.add_substate(m)

    def _show_track_action_menu(self, title, target, source, is_favorite=False, playlist_name=None):
        """Show actions for a specific track (Play Now, Remove)"""
        from . import menu as menu_mod, menus
        from .speech import speak
        gp = self._find_gameplay()
        if not gp:
            return

        m = menu_mod.Menu(self.game, f"Track: {title}", parrent=gp)
        items = []

        def play_now():
            gp.pop_last_substate()
            self.play_single_track(title, target, source)

        items.append(("Play Now", play_now))

        if is_favorite:
            def remove_fav():
                gp.pop_last_substate()
                self.playlist_mgr.remove_favorite(target)
                speak(f"Removed {title} from favorites.")
            items.append(("Remove from Favorites", remove_fav))

        if playlist_name:
            def remove_from_p():
                gp.pop_last_substate()
                self.playlist_mgr.remove_from_playlist(playlist_name, target)
                speak(f"Removed {title} from playlist.")
            items.append((f"Remove from {playlist_name}", remove_from_p))

        def back_action():
            gp.pop_last_substate()
            if playlist_name:
                self._show_custom_playlist_menu(playlist_name)
            else:
                self._show_favorites_menu()

        items.append(("Back", back_action))
        m.add_items(items)
        menus.set_default_sounds(m)
        gp.add_substate(m)

    def play_single_track(self, title, target, source, preserve_queue=False, preserve_feed=False):
        """Play a single track from playlist/favorites"""
        from .speech import speak
        import threading
        if not preserve_queue:
            self._clear_track_queue()
        if not preserve_feed:
            self._clear_personal_feed()
        playback_generation = self._begin_playback_generation()
        self.current_title = title
        self.current_target = target
        self.current_source = source

        # Save for replay
        self.last_track_title = title
        self.last_track_target = target
        self.last_track_source = source
        self.last_youtube_url = target
        self.last_youtube_title = title

        if source == "local":
            self._start_local_file_stream(
                target,
                title,
                preserve_queue=preserve_queue,
                preserve_feed=preserve_feed,
                playback_generation=playback_generation,
            )
        else:
            if target.startswith("http://") or target.startswith("https://"):
                speak(f"Loading: {title}")
                self.stop(
                    clear_queue=False,
                    clear_feed=not preserve_feed,
                    invalidate_pending=False,
                )
                self.is_loading_stream = True

                def do_play():
                    stream_info = YouTubeSearcher.get_stream_info(target)
                    if not stream_info:
                        if self._is_current_playback_generation(playback_generation):
                            speak("Failed to get audio stream.")
                            self.is_loading_stream = False
                        return
                    self.game.put(lambda: self._start_youtube_stream(
                        stream_info['url'], title, playback_generation,
                        http_headers=stream_info.get('http_headers'),
                        canonical_url=target,
                    ))

                threading.Thread(target=do_play, daemon=True).start()
            else:
                self._on_search_submit(target)

    def _clear_track_queue(self):
        self.play_queue = []
        self.play_queue_index = -1
        self.play_queue_label = ""

    def _start_track_queue(self, tracks, label):
        """Start a personal playlist/favorites queue without mixing map music."""
        from .speech import speak
        self.play_queue = [dict(track) for track in tracks if track.get("target")]
        self.play_queue_index = 0
        self.play_queue_label = label
        if not self.play_queue:
            speak(f"{label} is empty.")
            self._clear_track_queue()
            return
        speak(f"Playing {label}. {len(self.play_queue)} tracks.")
        self._play_queued_track()

    def _play_queued_track(self):
        if not (0 <= self.play_queue_index < len(self.play_queue)):
            return
        track = self.play_queue[self.play_queue_index]
        self.play_single_track(
            track.get("title", "Unknown"),
            track.get("target", ""),
            track.get("source", "youtube"),
            preserve_queue=True,
        )

    def _advance_track_queue(self):
        from .speech import speak
        if not self.play_queue:
            return False
        self.play_queue_index += 1
        if self.play_queue_index >= len(self.play_queue):
            speak(f"{self.play_queue_label} finished.")
            self._clear_track_queue()
            return False
        self._play_queued_track()
        return True

    def _play_playlist_all(self, playlist_name):
        """Play all tracks in a custom playlist sequentially"""
        tracks = self.playlist_mgr.get_playlist_tracks(playlist_name)
        self._start_track_queue(tracks, f"playlist {playlist_name}")

    def _prompt_create_playlist(self):
        """Prompt user for a new playlist name"""
        gp = self._find_gameplay()
        if gp:
            gp.add_substate(self.game.input.run(
                "Enter new playlist name:",
                handeler=self._on_create_playlist_submit
            ))

    def _on_create_playlist_submit(self, name):
        from .speech import speak
        gp = self._find_gameplay()
        if gp:
            gp.pop_last_substate()

        if not name.strip():
            speak("Cancelled.")
            return

        success = self.playlist_mgr.create_playlist(name)
        if success:
            speak(f"Created playlist {name}.")
        else:
            speak(f"Playlist {name} already exists.")
        self._open_playlists_menu()

    def _show_help_menu(self):
        """Show scrollable menu containing the Music Bot key controls"""
        from . import menu as menu_mod, menus

        gp = self._find_gameplay()
        if not gp:
            return

        def go_back():
            gp.pop_last_substate()
            self._show_mode_menu()

        toggle_key = friendly_key_name(
            self.game.keyconfig.get("music_bot_toggle", pygame.K_m)
        )
        volume_down_key = friendly_key_name(
            self.game.keyconfig.get("music_bot_vol_down", pygame.K_F9)
        )
        volume_up_key = friendly_key_name(
            self.game.keyconfig.get("music_bot_vol_up", pygame.K_F10)
        )

        m = menu_mod.Menu(self.game, "Music Bot Controls Help", parrent=gp)
        items = [
            (f"{toggle_key}: Open mode menu", lambda: None),
            (f"Shift + {toggle_key}: Pause / Resume", lambda: None),
            (f"Ctrl + {toggle_key}: Stop / Replay last song", lambda: None),
            (f"Ctrl + Shift + {toggle_key}: Speak status", lambda: None),
            (f"Alt + {toggle_key}: Toggle broadcast (Private/Public)", lambda: None),
            ("Personal Music Feed: Ctrl left bracket for previous; Ctrl right bracket for next", lambda: None),
            (f"{volume_down_key}: Decrease volume", lambda: None),
            (f"{volume_up_key}: Increase volume", lambda: None),
            ("Back", go_back)
        ]
        m.add_items(items)
        menus.set_default_sounds(m)
        gp.add_substate(m)

    def _open_file_dialog(self):
        """Open Windows file chooser dialog in a background thread to prevent game freezing"""
        import threading
        from .speech import speak

        def select_file():
            try:
                import tkinter as tk
                from tkinter import filedialog
                
                root = tk.Tk()
                root.withdraw()  # Hide the main tk window
                root.attributes("-topmost", True)  # Bring file dialog to front
                
                filepath = filedialog.askopenfilename(
                    title="Select Audio File",
                    filetypes=[
                        ("Audio Files", "*.ogg *.mp3 *.wav *.flac"),
                        ("All Files", "*.*")
                    ]
                )
                root.destroy()
                
                if filepath:
                    # Resolve base name as title
                    import os
                    title = os.path.splitext(os.path.basename(filepath))[0]
                    # Put stream start callback on the main game thread queue
                    self.game.put(lambda: self._start_local_file_stream(filepath, title))
                else:
                    self.game.put(lambda: speak("No file selected."))
            except Exception as ex:
                print(f"[MusicBot] Error opening file dialog: {ex}")
                self.game.put(lambda: speak("Error opening file dialog."))

        t = threading.Thread(target=select_file, daemon=True)
        t.start()
        speak("Opening file explorer...")

    def _start_local_file_stream(self, filepath, title, preserve_queue=False, preserve_feed=False,
                                 playback_generation=None):
        """Start streaming local file"""
        import os
        if not os.path.exists(filepath):
            speak("File not found.")
            return

        if playback_generation is None:
            playback_generation = self._begin_playback_generation()
        if not self._is_current_playback_generation(playback_generation):
            return

        speak(f"Loading local file: {title}")
        self.current_title = title
        self.current_target = filepath
        self.current_source = "local"

        # Save for replay
        self.last_track_title = title
        self.last_track_target = filepath
        self.last_track_source = "local"
        self.is_loading_stream = True

        # Stop any current playback
        self.stop(
            clear_queue=not preserve_queue,
            clear_feed=not preserve_feed,
            invalidate_pending=False,
        )

        # Start streaming local file via ffmpeg -> AudioStreamer
        self._start_youtube_stream(filepath, title, playback_generation)

    def _open_search_input(self):
        """Open the text input for search query"""
        self._gp = self._find_gameplay()
        if self._gp:
            self._gp.add_substate(self.game.input.run(
                "Enter song name:",
                handeler=self._on_search_submit
            ))

    def _find_gameplay(self):
        """Find the Gameplay state instance"""
        from . import gameplay
        for st in reversed(self.game.stack):
            if isinstance(st, gameplay.Gameplay):
                return st
        return None

    def _is_music_owner(self):
        """True if this performer holds the single music-bot PA slot.

        The server keeps the music slot single-owner (only one MP3 stream on
        the PA at a time, so two people's music never overlaps); everyone else
        with "Broadcast to Megaphone" still broadcasts their live instruments.
        """
        gp = self._find_gameplay()
        if not gp or not getattr(gp, 'megaphone', None):
            return False
        name = getattr(getattr(gp, 'player', None), 'name', '')
        return bool(name and getattr(gp.megaphone, 'lock_owner', None) == name)

    def _on_search_submit(self, query):
        """Called when user submits search query"""
        # ALWAYS pop the input substate first — otherwise it blocks all events!
        gp = self._gp or self._find_gameplay()
        if gp:
            gp.pop_last_substate()

        if not query.strip():
            speak("Search cancelled.")
            return

        speak(f"Searching: {query}")
        self.searching = True

        # Search in background thread to not block game
        def do_search():
            results = YouTubeSearcher.search(query, count=5)
            self.search_results = results
            self.searching = False
            # Show results menu on main thread
            self.game.put(lambda: self._show_results_menu(results))

        t = threading.Thread(target=do_search, daemon=True)
        t.start()

    def _show_results_menu(self, results):
        """Show search results as a menu"""
        from . import menu as menu_mod, menus

        gp = self._find_gameplay()
        if not gp:
            return

        if not results:
            speak("No results found.")
            return

        m = menu_mod.Menu(self.game, "Search Results", parrent=gp)
        items = []
        for i, r in enumerate(results):
            dur = int(r.get('duration', 0))
            dur_str = f"{dur // 60}:{dur % 60:02d}" if dur else "?"
            title = r.get('title', 'Unknown')
            # Use default_factory to capture loop variable
            def make_callback(idx):
                return lambda: self._on_result_selected(idx, gp)
            items.append((f"{title} ({dur_str})", make_callback(i)))

        items.append(("Cancel", lambda: gp.pop_last_substate()))
        m.add_items(items)
        menus.set_default_sounds(m)
        gp.add_substate(m)

    def _prompt_add_track_to_playlist(self, title, target, source="youtube"):
        """Prompt user to choose which custom playlist to add a track to"""
        from . import menu as menu_mod, menus
        from .speech import speak
        gp = self._find_gameplay()
        if not gp:
            return

        names = self.playlist_mgr.get_playlist_names()
        if not names:
            speak("No custom playlists created yet. Please create one first.")
            return

        m = menu_mod.Menu(self.game, f"Add '{title}' to Playlist", parrent=gp)
        items = []
        for name in names:
            def make_add_cb(p_name):
                def do_add():
                    gp.pop_last_substate()
                    added = self.playlist_mgr.add_to_playlist(p_name, title, target, source)
                    if added:
                        speak(f"Added {title} to {p_name}.")
                    else:
                        speak(f"{title} is already in {p_name}.")
                return do_add
            items.append((name, make_add_cb(name)))

        items.append(("Cancel", lambda: gp.pop_last_substate()))
        m.add_items(items)
        menus.set_default_sounds(m)
        gp.add_substate(m)

    def _on_result_selected(self, index, gp):
        """User selected a search result -> Show options (Play Now / Save to Favorites / Save to Playlist)"""
        gp.pop_last_substate()

        if index >= len(self.search_results):
            return

        result = self.search_results[index]
        title = result.get('title', 'Unknown')
        webpage_url = result.get('webpage_url', '')
        direct_url = result.get('url', '')
        http_headers = result.get('http_headers') or {}
        target = webpage_url or direct_url

        from . import menu as menu_mod, menus
        m = menu_mod.Menu(self.game, title, parrent=gp)
        items = []

        def play_now():
            gp.pop_last_substate()
            self._start_youtube_stream_from_search(
                title, webpage_url, direct_url, http_headers
            )

        def save_fav():
            gp.pop_last_substate()
            added = self.playlist_mgr.add_favorite(title, target, "youtube")
            if added:
                speak(f"Saved {title} to favorites.")
            else:
                speak(f"{title} is already in favorites.")

        def save_playlist():
            gp.pop_last_substate()
            self._prompt_add_track_to_playlist(title, target, "youtube")

        items.append(("Play Now", play_now))
        items.append(("Save to Favorites", save_fav))
        items.append(("Add to Playlist...", save_playlist))
        items.append(("Cancel", lambda: gp.pop_last_substate()))

        m.add_items(items)
        menus.set_default_sounds(m)
        gp.add_substate(m)

    def _start_youtube_stream_from_search(self, title, webpage_url, direct_url,
                                          http_headers=None):
        from .speech import speak
        if self.is_loading_stream:
            speak("Please wait, already loading a track.")
            return

        # A manually selected search result leaves the private feed.
        self._clear_personal_feed()
        playback_generation = self._begin_playback_generation()

        speak(f"Loading: {title}")
        self.current_title = title
        self.current_target = webpage_url or direct_url
        self.current_source = "youtube"

        # Save for replay
        self.last_track_title = title
        self.last_track_target = webpage_url or direct_url
        self.last_track_source = "youtube"
        self.last_youtube_url = webpage_url
        self.last_youtube_title = title
        self.is_loading_stream = True

        # Stop any current playback
        self.stop(invalidate_pending=False)
        self.is_loading_stream = True

        # Get stream URL in background
        def do_play():
            import threading
            # Resolve again at selection time so URL and authorization headers
            # are fresh. Search result direct URLs are only a fallback for
            # providers that do not expose a canonical webpage URL.
            stream_info = (
                YouTubeSearcher.get_stream_info(webpage_url)
                if webpage_url else None
            )
            if not stream_info and direct_url:
                stream_info = {
                    'url': direct_url,
                    'http_headers': dict(http_headers or {}),
                }
            if not stream_info:
                if self._is_current_playback_generation(playback_generation):
                    speak("Failed to get audio stream.")
                    self.is_loading_stream = False
                return
            # Start streaming on main thread
            self.game.put(lambda: self._start_youtube_stream(
                stream_info['url'], title, playback_generation,
                http_headers=stream_info.get('http_headers'),
                canonical_url=webpage_url or direct_url,
            ))

        t = threading.Thread(target=do_play, daemon=True)
        t.start()

    def _start_youtube_stream(self, audio_url, title, playback_generation=None,
                              http_headers=None, canonical_url=None):
        """Start streaming from YouTube audio URL"""
        if (playback_generation is not None
                and not self._is_current_playback_generation(playback_generation)):
            return
        self.is_loading_stream = False
        self._create_stream_source()
        if not self.stream_source:
            speak("Audio error.")
            return

        self.streamer = AudioStreamer(
            self.game, audio_url, self.stream_source, self.volume, bot=self,
            http_headers=http_headers,
            canonical_url=canonical_url,
        )
        self.streamer.start()

        self.mode = "youtube"
        self.playing = True
        self.paused = False
        self.current_title = title
        self._stream_announced = False

    def has_last_track(self):
        """Check if any track has been played and is available for replay"""
        return bool(self.last_track_target or self.last_youtube_url)

    def _replay_last(self):
        """Replay the last played track (YouTube, Local, or Playlist)"""
        if self.is_loading_stream:
            return

        if self.last_track_target:
            self.play_single_track(self.last_track_title, self.last_track_target, self.last_track_source)
        elif self.last_youtube_url:
            self.play_single_track(self.last_youtube_title, self.last_youtube_url, "youtube")

    # === Local File Playback (fallback/map music) ===

    def load_map_music(self, map_data):
        """Store playlist based on map data but do NOT auto-play.
        The bot only plays music when the user explicitly searches YouTube.
        Local playlist is kept as a fallback reference only.
        """
        playlist = self._resolve_playlist(map_data)
        if playlist:
            self.playlist = playlist
            self.playlist_index = 0

    def _resolve_playlist(self, map_data):
        if isinstance(map_data, dict):
            # Try music_bot data from server
            mbd = map_data.get("music_bot")
            if mbd and mbd.get("tracks"):
                return mbd["tracks"]
            # Try matching map name
            map_name = ""
            for el in map_data.get("elements", []):
                if el.get("type") == "zone":
                    map_name = el.get("data", {}).get("innerText", "")
                    if map_name:
                        break
            if not map_name:
                map_name = map_data.get("name", "")
            for key, tracks in DEFAULT_MAP_MUSIC.items():
                if key in map_name.lower():
                    return tracks
        return FALLBACK_PLAYLIST.copy()

    def _play_local_current(self):
        if not self.playlist:
            return
        idx = self.playlist_index % len(self.playlist)
        track = self.playlist[idx]
        path = f"music/{track}"

        self._stop_local()
        try:
            snd = self.soundgroup.play(
                path, looping=False, id="music_bot_track", cat="music", volume=self.volume
            )
            if snd is None:
                # File doesn't exist or failed to load — skip to next
                print(f"[MusicBot] Failed to load: {path}, skipping...")
                self.playing = False
                return
            self.current_local_sound = snd
            self.mode = "local"
            self.playing = True
            self.paused = False
            self.current_title = track
        except Exception as ex:
            print(f"[MusicBot] Error playing local: {ex}")
            self.playing = False

    def _stop_local(self):
        if self.current_local_sound:
            try:
                self.current_local_sound.destroy()
            except Exception:
                pass
            self.current_local_sound = None

    # === Common Controls ===

    def stop(self, clear_queue=True, clear_feed=True, invalidate_pending=True):
        """Stop all playback and cancel any pending search"""
        if invalidate_pending:
            self._begin_playback_generation()
        # Cancel any ongoing search
        self.searching = False
        self.is_loading_stream = False
        # Stop YouTube streamer
        if self.streamer:
            self.streamer.stop()
            self.streamer = None
        self._destroy_stream_source()
        # Stop local playback
        self._stop_local()
        self.playing = False
        self.paused = False
        self.mode = "idle"
        self._stream_announced = False
        self._current_reverb_slot = None
        if clear_queue:
            self._clear_track_queue()
        if clear_feed:
            self._clear_personal_feed()

    def toggle_pause(self):
        from .speech import speak
        if not self.playing:
            # If we have a last played song, replay it
            if self.has_last_track():
                speak(f"Replaying: {self.last_track_title or self.last_youtube_title}")
                self._replay_last()
            else:
                speak("Nothing is playing. Press M to search.")
            return

        if self.streamer:
            self.paused = not self.paused
            self.streamer.set_pause(self.paused)
            speak("Paused" if self.paused else "Resumed")
        elif self.mode == "local":
            if self.paused:
                self.paused = False
                self.soundgroup.resume()
                speak("Resumed")
            else:
                self.paused = True
                self.soundgroup.pause()
                speak("Paused")
        else:
            speak("Nothing is playing.")

    def next_track(self):
        if self.feed_tracks:
            self._next_personal_feed()
            return
        if self.mode == "local" and self.playlist:
            self.playlist_index = (self.playlist_index + 1) % len(self.playlist)
            self._play_local_current()
            speak(f"Next: {self.current_title}")

    def toggle_enabled(self):
        self.enabled = not self.enabled
        options.set("music_bot_enabled", self.enabled)
        if self.enabled:
            speak("Music Bot: On")
        else:
            speak("Music Bot: Off")
            self.stop()

    def speak_status(self):
        if not self.enabled:
            speak("Music Bot is off")
            return
        status = "paused" if self.paused else ("playing" if self.playing else "stopped")
        mode = "stream" if self.streamer else self.mode
        speak(f"Music Bot: {status}. Mode: {mode}. Track: {self.current_title or 'none'}. Volume: {self.volume}%")

    def set_volume(self, volume):
        self.volume = max(0, min(100, volume))
        if self.streamer:
            self.streamer.volume = self.volume
        options.set("music_bot_volume", self.volume)
        music_vol = self.game.audio_mngr.volume_categories.get("music", [100])[0] / 100
        gain = (self.volume / 100) * music_vol
        if self.stream_source:
            try:
                self.stream_source.gain = gain
            except Exception:
                pass
        if self.current_local_sound and self.current_local_sound.source:
            try:
                self.current_local_sound.source.gain = gain
                self.current_local_sound.volume = self.volume
            except Exception:
                pass

    def loop(self):
        """Called every frame — check if track ended + sync reverb"""
        if not self.enabled:
            return

        # Smooth volume ducking interpolation
        gp = self._find_gameplay()
        is_speaking_on_mega = False
        if gp and gp.voice_chat and gp.voice_chat.recording and getattr(gp, 'voice_chat_using_megaphone', False):
            is_speaking_on_mega = True

        target_duck = 0.2 if (is_speaking_on_mega and self.broadcast_to_megaphone) else 1.0
        
        if not hasattr(self, 'duck_multiplier'):
            self.duck_multiplier = 1.0
        
        # LERP towards target (10% step per frame ~300ms transition)
        self.duck_multiplier += (target_duck - self.duck_multiplier) * 0.1
        
        # Ensure live instrument / mic relay streamer is active if needed
        self._ensure_live_relay_streamer()

        # Apply updated gain to local stream source
        if self.stream_source and (self.playing or self.paused):
            try:
                music_vol = self.game.audio_mngr.volume_categories.get("music", [100])[0] / 100
                self.stream_source.gain = (self.volume / 100) * music_vol * self.duck_multiplier
            except Exception:
                pass

        # Sync reverb even when paused so it matches when resumed
        if self.stream_source and (self.playing or self.paused):
            self._sync_map_reverb()

        if not self.playing or self.paused:
            return

        # Announce playback only after ffmpeg produced PCM and OpenAL accepted
        # the pre-buffer. This prevents the misleading sequence
        # "Now playing" -> "Track finished" when stream startup actually failed.
        if (self.streamer and self.streamer.ready_event.is_set()
                and not self._stream_announced):
            self._stream_announced = True
            speak(f"Now playing: {self.current_title}")

        if self.mode == "local" and self.current_local_sound:
            try:
                if self.current_local_sound.source.state == cyal.SourceState.STOPPED:
                    self.playlist_index = (self.playlist_index + 1) % len(self.playlist)
                    self._play_local_current()
            except Exception:
                pass
        elif self.streamer and not self.streamer.is_alive():
            # Keep startup failures distinct from a real end-of-track.
            finished_streamer = self.streamer
            self.streamer = None
            self.playing = False
            self.mode = "idle"
            if not self._advance_track_queue():
                if finished_streamer.failure_reason:
                    speak("Could not load track.")
                else:
                    speak("Track finished.")

    def recover_output(self):
        """Resume buffered local music after a transient UI/output interruption."""
        streamer = self.streamer
        if not (self.enabled and self.playing and not self.paused and streamer):
            return False
        try:
            return bool(streamer.resume_output_if_buffered())
        except Exception:
            return False

    def performance_timeline_marker(self):
        """Marker attached to this performer's event-driven instruments.

        Only ordinary Music Broadcast has a versioned timeline. Megaphone and
        private playback keep their existing paths and therefore return None.
        """
        if (not self.broadcast_enabled or self.broadcast_to_megaphone
                or self.paused or not self.playing):
            return None
        streamer = self.streamer
        if streamer is None:
            return None
        return streamer.performance_timeline_marker()

    def _ensure_live_relay_streamer(self):
        """Ensure background live relay thread runs when broadcast is enabled and live input exists without an active MP3 stream."""
        if not (self.broadcast_enabled or self.broadcast_to_megaphone):
            if getattr(self, 'live_relay_streamer', None):
                self.live_relay_streamer.stop()
                self.live_relay_streamer = None
            return

        if self.streamer and self.streamer.is_alive():
            if getattr(self, 'live_relay_streamer', None):
                self.live_relay_streamer.stop()
                self.live_relay_streamer = None
            return

        has_guitar = bool(getattr(self, 'guitar_pcm_queue', None) and len(self.guitar_pcm_queue) > 0)
        has_mic = bool(getattr(self, 'mic_pcm_queue', None) and len(self.mic_pcm_queue) > 0)

        if has_guitar or has_mic:
            if getattr(self, 'live_relay_streamer', None) is None or not self.live_relay_streamer.is_alive():
                self.live_relay_streamer = LiveRelayStreamer(self.game, bot=self)
                self.live_relay_streamer.start()

    def _sync_map_reverb(self):
        """Apply the map's reverb at the player's position to the music source.
        This gives the music an environmental feel — cave echo, outdoor ambience, etc.
        The dry signal stays stereo-direct (headphone quality),
        while the wet signal from the reverb adds the room's atmosphere.
        """
        if not self.stream_source:
            return
        try:
            gp = self._find_gameplay()
            map_obj = getattr(gp, 'map', None) or getattr(gp, 'world_map', None)
            if not map_obj:
                return

            player = gp.player
            reverb = map_obj.get_reverb_at(player.x, player.y, player.z)

            if reverb and reverb.reverb:
                # Apply map's reverb to the music via aux send 0
                if self._current_reverb_slot != reverb.reverb:
                    self.game.audio_mngr.efx.send(
                        self.stream_source, 0, reverb.reverb
                    )
                    self._current_reverb_slot = reverb.reverb
            else:
                # No reverb zone — remove effect
                if self._current_reverb_slot is not None:
                    self.game.audio_mngr.efx.send(
                        self.stream_source, 0, None
                    )
                    self._current_reverb_slot = None
        except Exception:
            pass

    def _detach_map_reverb(self):
        """Detach the old map slot without interrupting the active stream."""
        if not self.stream_source or self._current_reverb_slot is None:
            self._current_reverb_slot = None
            return
        try:
            self.game.audio_mngr.efx.send(self.stream_source, 0, None)
        except Exception:
            pass
        self._current_reverb_slot = None

    def destroy(self):
        self.stop()
        if getattr(self, 'live_relay_streamer', None):
            self.live_relay_streamer.stop()
            self.live_relay_streamer = None
        try:
            self.soundgroup.destroy()
        except Exception:
            pass
