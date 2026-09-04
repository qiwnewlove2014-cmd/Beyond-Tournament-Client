"""Music Bot media layer - URL handling, ffmpeg discovery, local fallback
tracks and YouTube search/resolution helpers. Game-independent: this layer can
be reused headlessly (e.g. by a standalone TeamTalk bot)."""

import os
import sys

from .. import logger
from ..speech import speak


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


def format_track_position(seconds):
    """Render a track position in seconds as m:ss for speech output."""
    seconds = max(0, int(seconds))
    return f"{seconds // 60}:{seconds % 60:02d}"


def clamp_seek_position(position, delta):
    """Clamp a seek target to the valid range [0, inf).

    Returns None when the requested jump is a no-op (the stream is already
    sitting at the very start and the jump would only move backward).  A jump
    from a positive position back to 0 is legal: it restarts the intro.
    """
    position = max(0.0, float(position or 0.0))
    target = max(0.0, position + float(delta))
    if target <= 0.0 and position <= 0.0:
        return None
    return target


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
                        'is_live': bool(e.get('is_live', False)),
                        'live_status': e.get('live_status', ''),
                    })
                return results
        except Exception as ex:
            logger.log_exception(ex, "YouTubeSearcher.search")
            return []

    @staticmethod
    def get_stream_info(webpage_url, *, cancelled=None):
        """Worker-only resolution, isolated from the game's Python runtime."""
        from ..youtube_resolver import resolve_stream_info
        return resolve_stream_info(webpage_url, cancelled=cancelled)

    @staticmethod
    def get_stream_url(webpage_url):
        """Compatibility helper for callers that only need the direct URL."""
        info = YouTubeSearcher.get_stream_info(webpage_url)
        return info.get('url') if info else None


