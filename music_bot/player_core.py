import os
import subprocess
import threading
import time
from collections import deque
import cyal

# We find ffmpeg in the parent directory
app_dir = os.path.dirname(os.path.abspath(__file__))
FFMPEG_PATH = os.path.abspath(os.path.join(app_dir, "..", "ffmpeg.exe"))
if not os.path.exists(FFMPEG_PATH):
    FFMPEG_PATH = "ffmpeg"  # fallback to PATH

import yt_dlp

class YouTubeSearcher:
    """Handles searching and extracting direct stream URLs using yt-dlp."""
    
    @staticmethod
    def search_youtube(query, limit=5):
        # If it's a direct URL, just get 1
        is_url = query.startswith("http")
        
        ydl_opts = {
            'format': 'bestaudio/best',
            'noplaylist': True,
            'quiet': True,
            'no_warnings': True,
            'extract_flat': not is_url, # Faster if we just want search results
            'default_search': f'ytsearch{limit}'
        }
        
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(query, download=False)
                
                results = []
                if 'entries' in info:
                    for entry in info['entries']:
                        results.append({
                            'title': entry.get('title', 'Unknown Title'),
                            'url': entry.get('url'),
                            'webpage_url': entry.get('url') if is_url else f"https://www.youtube.com/watch?v={entry.get('id')}"
                        })
                else:
                    results.append({
                        'title': info.get('title', 'Unknown Title'),
                        'url': info.get('url'),
                        'webpage_url': info.get('webpage_url')
                    })
                return results
        except Exception as e:
            print(f"[Core] Error extracting stream URL: {e}")
            return []

    @staticmethod
    def get_direct_url(webpage_url):
        ydl_opts = {
            'format': 'bestaudio/best',
            'noplaylist': True,
            'quiet': True,
            'no_warnings': True,
        }
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(webpage_url, download=False)
                return info.get('url')
        except Exception:
            return None


class AudioStreamer(threading.Thread):
    """Streams audio from ffmpeg to OpenAL buffers."""
    
    SAMPLES_PER_BUFFER = 4096
    BUFFER_SIZE = SAMPLES_PER_BUFFER * 2 * 2  # stereo 16-bit
    NUM_BUFFERS = 16
    PRE_BUFFER_COUNT = 4

    def __init__(self, context, audio_url, source, volume=50):
        super().__init__(daemon=True)
        self.context = context
        self.audio_url = audio_url
        self.source = source
        self.volume = volume
        self.running = True
        self.paused = False
        self._lock = threading.Lock()
        self.process = None
        self._buffer_pool = []
        self._pause_buffer = deque()
        self.on_finish_callback = None

    def _init_buffer_pool(self):
        for _ in range(self.NUM_BUFFERS):
            try:
                buf = self.context.gen_buffer()
                self._buffer_pool.append(buf)
            except Exception:
                break

    def _get_buffer(self):
        if self._buffer_pool:
            return self._buffer_pool.pop(0)
        try:
            if self.source.buffers_processed > 0:
                reclaimed = self.source.unqueue_buffers()
                if reclaimed:
                    self._buffer_pool.extend(reclaimed[1:])
                    return reclaimed[0]
        except Exception:
            pass
        return None

    def _queue_data(self, data):
        buf = self._get_buffer()
        if not buf:
            return False
        try:
            buf.set_data(data, sample_rate=48000, format=cyal.BufferFormat.STEREO16)
            self.source.queue_buffers(buf)
            return True
        except Exception:
            self._buffer_pool.append(buf)
            return False

    def run(self):
        cmd = [
            FFMPEG_PATH,
            '-reconnect', '1',
            '-reconnect_streamed', '1',
            '-reconnect_delay_max', '5',
            '-i', self.audio_url,
            '-f', 's16le',
            '-ar', '48000',
            '-ac', '2',
            '-loglevel', 'error',
            'pipe:1'
        ]

        try:
            self.process = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                creationflags=0x08000000  # CREATE_NO_WINDOW
            )
        except Exception as ex:
            print(f"[Streamer] ffmpeg launch error: {ex}")
            if self.on_finish_callback: self.on_finish_callback()
            return

        self._init_buffer_pool()

        # Pre-buffer
        pre_buffered = 0
        for _ in range(self.PRE_BUFFER_COUNT):
            if not self.running: break
            data = self.process.stdout.read(self.BUFFER_SIZE)
            if not data or len(data) < self.BUFFER_SIZE: break
            with self._lock:
                if self._queue_data(data):
                    pre_buffered += 1

        if pre_buffered > 0:
            try: self.source.play()
            except Exception: pass

        # Streaming loop
        eof = False
        while self.running:
            data = None
            if not eof:
                data = self.process.stdout.read(self.BUFFER_SIZE)
                if not data or len(data) < self.BUFFER_SIZE:
                    eof = True
            
            if data:
                self._pause_buffer.append(data)

            if not self.running: break

            if self.paused:
                time.sleep(0.05)
                continue

            with self._lock:
                if not self.running: break
                try:
                    while self._pause_buffer:
                        chunk = self._pause_buffer[0]
                        if self._queue_data(chunk):
                            self._pause_buffer.popleft()
                        else:
                            break

                    if self.source.state != cyal.SourceState.PLAYING and self.source.buffers_queued > 0:
                        self.source.play()
                except Exception:
                    pass

            if eof and not self._pause_buffer and self.source.buffers_queued == 0:
                break
                
            if not data or not self._pause_buffer:
                time.sleep(0.02)

        # Wait for remaining playback
        if self.running:
            try:
                while self.source.buffers_queued > 0 and self.running:
                    if not self.paused and self.source.state != cyal.SourceState.PLAYING:
                        self.source.play()
                    time.sleep(0.1)
            except Exception:
                pass

        if self.process:
            self.process.kill()
            self.process = None

        if self.running and self.on_finish_callback:
            self.on_finish_callback()

    def set_pause(self, state):
        self.paused = state
        if not self.paused:
            with self._lock:
                if self.source.state != cyal.SourceState.PLAYING and self.source.buffers_queued > 0:
                    self.source.play()
        else:
            with self._lock:
                self.source.pause()

    def stop(self):
        self.running = False
        self.paused = False
        if self.process:
            try: self.process.kill()
            except: pass
        
        with self._lock:
            try:
                self.source.stop()
                while self.source.buffers_processed > 0:
                    self.source.unqueue_buffers()
            except: pass


class MusicPlayer:
    """Main logic coordinator for the music player."""
    
    def __init__(self):
        # We need to change cwd to the parent folder so CYAL finds openal.dll!
        parent_dir = os.path.abspath(os.path.join(app_dir, ".."))
        original_cwd = os.getcwd()
        os.chdir(parent_dir)
        
        # Initialize OpenAL
        self.device = cyal.Device()
        self.context = cyal.Context(self.device)
        self.context.make_current()
        
        # Restore cwd
        os.chdir(original_cwd)
        
        self.source = self.context.gen_source()
        self.streamer = None
        self.volume = 50
        
        # Callbacks for GUI
        self.on_track_loaded = None
        self.on_track_finished = None
        
        self.on_search_results = None
        
    def search(self, query):
        def _do_search():
            results = YouTubeSearcher.search_youtube(query)
            if self.on_search_results:
                self.on_search_results(results)
        threading.Thread(target=_do_search, daemon=True).start()
        
    def play(self, webpage_url, title):
        if self.streamer:
            self.streamer.stop()
            self.streamer.join(timeout=1.0)
            
        def _load():
            url = YouTubeSearcher.get_direct_url(webpage_url)
            if not url:
                if self.on_track_loaded: self.on_track_loaded(False, "Failed to extract track.")
                return
                
            self.source.gain = self.volume / 100.0
            self.streamer = AudioStreamer(self.context, url, self.source, self.volume)
            self.streamer.on_finish_callback = self._track_finished
            self.streamer.start()
            
            if self.on_track_loaded:
                self.on_track_loaded(True, title)

        threading.Thread(target=_load, daemon=True).start()

    def toggle_pause(self):
        if self.streamer:
            self.streamer.set_pause(not self.streamer.paused)
            return self.streamer.paused
        return False
        
    def stop(self):
        if self.streamer:
            self.streamer.stop()
            self.streamer = None
            
    def set_volume(self, volume):
        self.volume = max(0, min(100, volume))
        if self.source:
            self.source.gain = self.volume / 100.0
            
    def cleanup(self):
        self.stop()
        if self.source:
            try: self.source.destroy()
            except: pass
        if self.context:
            try: self.context.destroy()
            except: pass
        if self.device:
            try: self.device.close()
            except: pass

    def _track_finished(self):
        if self.on_track_finished:
            self.on_track_finished()
