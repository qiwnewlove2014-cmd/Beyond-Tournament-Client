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
from collections import deque

import cyal
import cyal.exceptions

from . import options
from .speech import speak

# Try to find ffmpeg path
def _find_ffmpeg():
    """Find ffmpeg binary - check common locations"""
    # 1. Check ffmpeg-downloader path
    try:
        from ffmpeg_downloader import ffmpeg_path
        if ffmpeg_path and os.path.exists(ffmpeg_path):
            return ffmpeg_path
    except ImportError:
        pass
    # 2. Check next to executable (handle PyInstaller frozen state)
    if getattr(sys, 'frozen', False):
        exe_dir = os.path.dirname(sys.executable)
    else:
        exe_dir = os.path.dirname(os.path.abspath(sys.argv[0]))
        
    for name in ["ffmpeg.exe", "ffmpeg$.exe", "ffmpeg"]:
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
            'format': 'bestaudio/best',
            'quiet': True,
            'no_warnings': True,
            'noplaylist': True,
        }
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(f"ytsearch{count}:{query}", download=False)
                entries = info.get('entries', [])
                results = []
                for e in entries:
                    if not e:
                        continue
                    results.append({
                        'title': e.get('title', 'Unknown'),
                        'duration': e.get('duration', 0),
                        'webpage_url': e.get('webpage_url', ''),
                        'url': e.get('url', ''),  # direct audio stream URL
                    })
                return results
        except Exception as ex:
            print(f"[MusicBot] YouTube search error: {ex}")
            return []

    @staticmethod
    def get_stream_url(webpage_url):
        """Get direct audio stream URL from a YouTube video URL"""
        try:
            import yt_dlp
        except ImportError:
            return None
        ydl_opts = {
            'format': 'bestaudio/best',
            'quiet': True,
            'no_warnings': True,
        }
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(webpage_url, download=False)
                return info.get('url')
        except Exception as ex:
            print(f"[MusicBot] Error getting stream URL: {ex}")
            return None


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

    def __init__(self, game, audio_url, source, volume=50, bot=None):
        super().__init__(daemon=True)
        self.game = game
        self.bot = bot
        self.audio_url = audio_url
        self.source = source  # cyal OpenAL source
        self.volume = volume
        self.running = True
        self.paused = False
        self._lock = threading.Lock()
        self.process = None
        self._buffer_pool = []       # Reusable buffer objects
        self._pause_buffer = deque() # Store data read while paused
        from pyogg import OpusEncoder
        self.encoder = OpusEncoder()
        self.encoder.set_application('audio')
        self.encoder.set_channels(1)  # Opus network stream is ALWAYS MONO
        self.encoder.set_sampling_frequency(48000)
        self.last_send_time = None
        self.network_queue = queue.Queue()
        self.sender_thread = None

    def _init_buffer_pool(self):
        """Pre-allocate OpenAL buffers for reuse"""
        for _ in range(self.NUM_BUFFERS):
            try:
                buf = self.game.audio_mngr.context.gen_buffer()
                self._buffer_pool.append(buf)
            except Exception:
                break

    def _get_buffer(self):
        """Get a buffer from the pool, or reclaim a processed one"""
        self._reclaim_processed()
        
        if self._buffer_pool:
            return self._buffer_pool.pop(0)
            
        try:
            return self.game.audio_mngr.context.gen_buffer()
        except Exception:
            return None

    def _reclaim_processed(self):
        """Return processed buffers to pool for reuse.
        
        CRITICAL: cyal's unqueue_buffers() returns a SINGLE Buffer object by default,
        not a list. Handle both cases robustly.
        """
        try:
            while self.source.buffers_processed > 0:
                result = self.source.unqueue_buffers()
                if result is not None:
                    try:
                        for buf in result:
                            self._buffer_pool.append(buf)
                    except TypeError:
                        # Not iterable — single buffer object (cyal default)
                        self._buffer_pool.append(result)
        except Exception:
            pass

    def _send_to_network(self, data):
        """Queue raw PCM chunk to be sent to the network by the sender thread."""
        self.network_queue.put(data)

    def _network_sender_loop(self):
        """Paced network sending loop running in a separate thread.
        Decouples network scheduling sleeps from local OpenAL playback.
        """
        while self.running:
            try:
                # Wait for a packet, with timeout so we check self.running regularly
                data = self.network_queue.get(timeout=0.1)
            except queue.Empty:
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

            self._send_to_network_actual(data)

    def _send_to_network_actual(self, data):
        """Downmix Stereo to Mono, scale volume, encode as Opus, and send to network."""
        try:
            if not self.game or not self.game.network:
                return
                
            # Check if broadcast is enabled
            if self.bot and not self.bot.broadcast_enabled:
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
                
                # Check if there is mic PCM data queued to mix
                if hasattr(self.bot, 'mic_pcm_queue') and self.bot.mic_pcm_queue:
                    try:
                        mic_data = self.bot.mic_pcm_queue.popleft()
                        # Mix the two mono 16-bit PCM streams together
                        mono_data = audioop.add(mono_data, mic_data, 2)
                    except Exception:
                        pass
            
            encoded = self.encoder.encode(bytearray(mono_data))
            self.game.network.send(target_channel, "n/a", encoded, reliable=False)
        except Exception:
            pass



    def _queue_local(self, data):
        """Queue a chunk of STEREO PCM data to the LOCAL OpenAL source."""
        self._reclaim_processed()
        buf = self._get_buffer()
        if buf is None:
            return False
        try:
            # Local playback is always STEREO for highest quality
            buf.set_data(data, sample_rate=48000, format=cyal.BufferFormat.STEREO16)
            self.source.queue_buffers(buf)
            return True
        except Exception:
            return False

    def run(self):
        if not FFMPEG_PATH:
            print("[MusicBot] ffmpeg not found!")
            return

        cmd = [FFMPEG_PATH]
        if self.audio_url.startswith(("http://", "https://")):
            cmd.extend([
                '-reconnect', '1',
                '-reconnect_streamed', '1',
                '-reconnect_delay_max', '5'
            ])
        cmd.extend([
            '-re',                # Decode at exactly 1x real-time (perfect clock sync)
            '-i', self.audio_url,
            '-f', 's16le',
            '-ar', '48000',
            '-ac', '2',           # STEREO output for local high-fidelity playback
            '-loglevel', 'error',
            'pipe:1'
        ])

        try:
            self.process = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0)
            )
        except Exception as ex:
            print(f"[MusicBot] ffmpeg launch error: {ex}")
            return

        # Initialize buffer pool
        self._init_buffer_pool()

        # Start background network sender thread
        self.sender_thread = threading.Thread(target=self._network_sender_loop, daemon=True)
        self.sender_thread.start()

        # === Pre-buffer phase: fill LOCAL buffers before starting playback ===
        pre_buffered = 0
        for _ in range(self.PRE_BUFFER_COUNT):
            if not self.running:
                break
            data = self.process.stdout.read(self.BUFFER_SIZE)
            if not data or len(data) < self.BUFFER_SIZE:
                break
            with self._lock:
                if self._queue_local(data):
                    pre_buffered += 1

        # Start local playback after pre-buffering
        if pre_buffered > 0:
            try:
                self.source.play()
            except Exception:
                pass

        # === Streaming loop ===
        eof = False
        while self.running:
            if self.paused:
                time.sleep(0.05)
                continue

            data = None
            if not eof:
                data = self.process.stdout.read(self.BUFFER_SIZE)
                if not data or len(data) < self.BUFFER_SIZE:
                    eof = True

            if not self.running:
                break

            # === NETWORK: Send at hardware-synchronized real-time rate ===
            if data:
                self._send_to_network(data)

            # === LOCAL: Buffer for OpenAL playback ===
            if data:
                self._pause_buffer.append(data)

            with self._lock:
                if not self.running:
                    break
                try:
                    # Drain pause buffer into OpenAL
                    while self._pause_buffer:
                        chunk = self._pause_buffer[0]
                        if self._queue_local(chunk):
                            self._pause_buffer.popleft()
                        else:
                            break  # No available OpenAL buffers, wait

                    # Restart if source stopped and we have buffers queued
                    if self.source.state != cyal.SourceState.PLAYING and self.source.buffers_queued > 0:
                        self.source.play()
                except Exception:
                    pass

            if eof and not self._pause_buffer and self.source.buffers_queued == 0:
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
                # Keep checking until all queued buffers are processed
                while self.source.buffers_queued > 0 and self.running:
                    # If we are paused at the very end, wait here until resumed
                    if not self.paused and self.source.state != cyal.SourceState.PLAYING:
                        self.source.play()
                    time.sleep(0.1)
            except Exception:
                pass

        # Cleanup
        self._cleanup()

    def _cleanup(self):
        """Clean up ffmpeg process and buffers"""
        if self.process:
            try:
                self.process.kill()
                self.process.wait(timeout=2)
            except Exception:
                pass
            self.process = None
        # Drain remaining buffers
        try:
            self.source.stop()
            while self.source.buffers_processed > 0:
                self.source.unqueue_buffers()
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

    def set_pause(self, paused):
        self.paused = paused
        with self._lock:
            try:
                if paused:
                    self.source.pause()
                else:
                    self.source.play()
            except Exception:
                pass


class MapMusicBot:
    """Music Bot — searches YouTube and streams audio in real-time.
    Falls back to local files when YouTube is unavailable.
    
    Controls (via gameplay.py key bindings):
        M           = Open YouTube search / Pause-Resume
        Shift+M     = Next track (local playlist)
        Ctrl+M      = Stop
        Alt+M       = Speak status
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

        # Last played YouTube info (for replay)
        self.last_youtube_url = ""
        self.last_youtube_title = ""

        # Local playlist (fallback)
        self.playlist = []
        self.playlist_index = 0

        # Settings
        self.volume = options.get("music_bot_volume", 50)
        self.enabled = options.get("music_bot_enabled", True)
        self.broadcast_enabled = False  # Disabled by default (Private listening mode)
        self.broadcast_to_megaphone = False

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
                while self.stream_source.buffers_processed > 0:
                    self.stream_source.unqueue_buffers()
                while self.stream_source.buffers_queued > 0:
                    self.stream_source.unqueue_buffers()
                self.stream_source.delete()
            except Exception:
                pass
            self.stream_source = None

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

        def go_help():
            gp.pop_last_substate()
            self._show_help_menu()

        m = menu_mod.Menu(self.game, "Music Bot Mode", parrent=gp)
        items = [
            ("Search YouTube", go_search),
            ("Choose Local File", go_local),
        ]
        
        # Check if staff to show megaphone routing option
        is_staff = getattr(gp, 'is_staff', False) if gp else False
        if is_staff:
            def get_megaphone_label():
                status = "ON" if self.broadcast_to_megaphone else "OFF"
                return f"Broadcast to Megaphone: {status}"
                
            def toggle_megaphone_routing():
                if not self.broadcast_enabled:
                    self.game.direct_soundgroup.play("ui/error.ogg", cat="ui")
                    speak("Cannot change option. Please turn on podcast first by pressing Alternate M.")
                    return
                
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

    def _show_help_menu(self):
        """Show scrollable menu containing the Music Bot key controls"""
        from . import menu as menu_mod, menus

        gp = self._find_gameplay()
        if not gp:
            return

        def go_back():
            gp.pop_last_substate()
            self._show_mode_menu()

        m = menu_mod.Menu(self.game, "Music Bot Controls Help", parrent=gp)
        items = [
            ("M: Open mode menu", lambda: None),
            ("Shift + M: Pause / Resume", lambda: None),
            ("Ctrl + M: Stop / Replay last song", lambda: None),
            ("Ctrl + Shift + M: Speak status", lambda: None),
            ("Alt + M: Toggle broadcast (Private/Public)", lambda: None),
            ("F9: Decrease volume", lambda: None),
            ("F10: Increase volume", lambda: None),
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

    def _start_local_file_stream(self, filepath, title):
        """Start streaming local file"""
        import os
        if not os.path.exists(filepath):
            speak("File not found.")
            return

        speak(f"Loading local file: {title}")
        self.current_title = title
        self.is_loading_stream = True

        # Stop any current playback
        self.stop()

        # Start streaming local file via ffmpeg -> AudioStreamer
        self._start_youtube_stream(filepath, title)

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
            dur = r.get('duration', 0)
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

    def _on_result_selected(self, index, gp):
        """User selected a search result"""
        gp.pop_last_substate()

        if index >= len(self.search_results):
            return

        if self.is_loading_stream:
            speak("Please wait, already loading a track.")
            return

        result = self.search_results[index]
        title = result.get('title', 'Unknown')
        webpage_url = result.get('webpage_url', '')
        direct_url = result.get('url', '')

        speak(f"Loading: {title}")
        self.current_title = title
        self.is_loading_stream = True

        # Save for replay
        self.last_youtube_url = webpage_url
        self.last_youtube_title = title

        # Stop any current playback
        self.stop()
        self.is_loading_stream = True

        # Get stream URL in background
        def do_play():
            url = direct_url
            if not url:
                url = YouTubeSearcher.get_stream_url(webpage_url)
            if not url:
                speak("Failed to get audio stream.")
                self.is_loading_stream = False
                return
            # Start streaming on main thread
            self.game.put(lambda: self._start_youtube_stream(url, title))

        t = threading.Thread(target=do_play, daemon=True)
        t.start()

    def _start_youtube_stream(self, audio_url, title):
        """Start streaming from YouTube audio URL"""
        self.is_loading_stream = False
        self._create_stream_source()
        if not self.stream_source:
            speak("Audio error.")
            return

        self.streamer = AudioStreamer(self.game, audio_url, self.stream_source, self.volume, bot=self)
        self.streamer.start()

        self.mode = "youtube"
        self.playing = True
        self.paused = False
        self.current_title = title
        speak(f"Now playing: {title}")

    def _replay_last(self):
        """Replay the last YouTube song by re-fetching stream URL"""
        if self.is_loading_stream:
            return
            
        self.is_loading_stream = True
        url = self.last_youtube_url
        title = self.last_youtube_title
        
        self.stop()
        self.is_loading_stream = True

        def do_replay():
            stream_url = YouTubeSearcher.get_stream_url(url)
            if not stream_url:
                speak("Failed to get audio stream for replay.")
                self.is_loading_stream = False
                return
            self.game.put(lambda: self._start_youtube_stream(stream_url, title))

        t = threading.Thread(target=do_replay, daemon=True)
        t.start()

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

    def stop(self):
        """Stop all playback and cancel any pending search"""
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
        self._current_reverb_slot = None

    def toggle_pause(self):
        if not self.playing:
            # If we have a last played song, replay it
            if self.last_youtube_url:
                speak(f"Replaying: {self.last_youtube_title}")
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

        if self.mode == "local" and self.current_local_sound:
            try:
                if self.current_local_sound.source.state == cyal.SourceState.STOPPED:
                    self.playlist_index = (self.playlist_index + 1) % len(self.playlist)
                    self._play_local_current()
            except Exception:
                pass
        elif self.streamer and not self.streamer.is_alive():
            # Stream finished
            self.playing = False
            self.mode = "idle"
            speak("Track finished.")

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
            if not gp or not hasattr(gp, 'player') or not gp.player:
                return
            if not hasattr(gp, 'world_map') or not gp.world_map:
                return

            player = gp.player
            reverb = gp.world_map.get_reverb_at(player.x, player.y, player.z)

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

    def destroy(self):
        self.stop()
        try:
            self.soundgroup.destroy()
        except Exception:
            pass
