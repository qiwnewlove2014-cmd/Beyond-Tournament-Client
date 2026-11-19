"""The picture of the song that is playing, still or animated.

The Music Bot's menus can carry a picture beside their lines.  The source is
the track's own artwork: a YouTube thumbnail, the cover embedded in a local
file, or an image sitting beside that file with the same name.  An animated
GIF or APNG is decoded into frames and played on the menu's own clock, so it
moves while the song does; a still picture is one frame and costs one load.
(WebP is accepted as a still through ffmpeg; the bundled ffmpeg build's WebP
demuxer skips the animation chunks, so an animated WebP has no frames here.)

Three rules shape this module:

* **Nothing here runs on the frame loop.**  Downloading, reading the embedded
  cover through ffmpeg and decoding an animation all happen on a worker.  The
  main thread receives ready-to-load frame bytes and builds pygame surfaces;
  an image that fails is a missing picture, never an exception in a menu.
* **The picture belongs to the track, not to the menu.**  A menu can be opened
  and closed, or rebuilt on every key press, without fetching again; the owner
  replaces the picture only when the track (or its seek) starts.
* **The source is bounded.**  One download is at most :data:`MAX_DOWNLOAD_BYTES`
  and one animation is at most :data:`MAX_FRAMES` frames, so a hostile or huge
  image cannot grow without bound.  An animation is decoded at
  :data:`FRAME_RATE` and at most :data:`MAX_FRAME_EDGE` pixels wide.

Pins: client/tests/test_music_bot_artwork.py.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from urllib.parse import parse_qs, urlsplit

from .. import options
from .media import FFMPEG_PATH


#: The player's switch (Music Bot Settings -> Song Picture).
PICTURE_OPTION = "music_bot_song_picture"
DEFAULT_ENABLED = True

#: A picture URL or file larger than this is refused rather than decoded.
MAX_DOWNLOAD_BYTES = 8 * 1024 * 1024

#: An animated picture is played at this rate and stops at this many frames.
FRAME_RATE = 12
MAX_FRAMES = 48
MAX_FRAME_EDGE = 240

#: How long a download or an ffmpeg decode may take before the picture is
#: declared missing.  A song must never wait on its own cover.
REQUEST_TIMEOUT = 10
DECODE_TIMEOUT = 20

#: The files a local audio file may keep its picture beside, in preference
#: order.  GIF and APNG come first because a sidecar is often chosen *because*
#: it moves; WebP is last because only a still WebP is decoded by the bundled
#: ffmpeg, so it must not shadow a picture that can actually load.
SIDECAR_SUFFIXES = (".gif", ".apng", ".png", ".jpg", ".jpeg", ".bmp", ".webp")

_YOUTUBE_ID = re.compile(r"[A-Za-z0-9_-]{6,20}\Z")
_HTTP_PREFIXES = ("http://", "https://")


def youtube_thumbnail(page_url):
    """The still picture YouTube publishes for a watch page, or ``""``.

    Every YouTube video has a ``mqdefault.jpg`` thumbnail -- 320x180, no
    letterbox bars -- so this needs no second metadata request.  A URL that is
    not a YouTube watch/short page is not guessed at.
    """
    try:
        parts = urlsplit(str(page_url or "").strip())
    except (TypeError, ValueError, UnicodeError):
        return ""
    host = (parts.hostname or "").lower()
    video_id = ""
    if host == "youtu.be":
        video_id = parts.path.strip("/").split("/", 1)[0]
    elif host == "youtube.com" or host.endswith(".youtube.com"):
        segments = [part for part in parts.path.split("/") if part]
        if len(segments) >= 2 and segments[0] in ("shorts", "embed", "live"):
            video_id = segments[1]
        else:
            video_id = (parse_qs(parts.query).get("v") or [""])[0]
    if not _YOUTUBE_ID.fullmatch(video_id or ""):
        return ""
    return f"https://i.ytimg.com/vi/{video_id}/mqdefault.jpg"


def sidecar_picture(path):
    """An image beside a local song with the same stem, or ``""``."""
    if not path:
        return ""
    stem = os.path.splitext(str(path))[0]
    for suffix in SIDECAR_SUFFIXES:
        candidate = stem + suffix
        if os.path.isfile(candidate):
            return candidate
    return ""


def _is_webp(data):
    """Whether these bytes are a WebP container (still or animated).

    pygame's SDL_image build here cannot load WebP at all, so both a still and
    an animation have to pass through ffmpeg.  A still gives one PNG frame; an
    animated one gives none because ffmpeg's WebP demuxer skips ANIM/ANMF.
    """
    head = bytes(data[:12])
    return head[:4] == b"RIFF" and head[8:12] == b"WEBP"


def _is_animated(data):
    """Whether these bytes are an animated picture.

    The containers have reliable signatures, so checking them is cheaper and
    safer than asking a decoder that would run a process for every still
    thumbnail.  A WebP is animated only when its VP8X flags say so; an APNG is
    a PNG with an ``acTL`` chunk.  A GIF is decoded as a sequence even when it
    holds one frame -- ffmpeg then returns one frame, which is still correct.
    """
    head = bytes(data[:64])
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return True
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        # VP8X: 4-byte chunk header, then one flags byte.  Bit 1 is animation.
        return len(head) >= 21 and head[12:16] == b"VP8X" and bool(head[20] & 0x02)
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        # acTL must precede IDAT, but it is not always in the first 64 bytes.
        return b"acTL" in bytes(data[:4096])
    return False


def _cache_dir():
    path = os.path.join(options.config_dirs.user_cache_dir, "song_pictures")
    try:
        os.makedirs(path, exist_ok=True)
    except OSError:
        return ""
    return path


def _cache_path(key, suffix=".img"):
    folder = _cache_dir()
    if not folder:
        return ""
    digest = hashlib.sha256(str(key).encode("utf-8", "replace")).hexdigest()[:32]
    return os.path.join(folder, digest + suffix)


def _bounded_read(path):
    """Read at most one picture's worth of bytes, or answer ``b""``."""
    try:
        size = os.path.getsize(path)
    except OSError:
        return b""
    if size <= 0 or size > MAX_DOWNLOAD_BYTES:
        return b""
    try:
        with open(path, "rb") as handle:
            return handle.read(MAX_DOWNLOAD_BYTES + 1)
    except OSError:
        return b""


def _download(url):
    """Fetch a picture URL into the cache and return its path, or ``""``.

    The caller is a worker.  A cached file is reused, because a song's cover
    does not change between two plays and the second open should be instant.
    The size ceiling is enforced while reading, not after the whole body has
    already been held in memory.
    """
    cached = _cache_path("url:" + str(url))
    if cached and os.path.isfile(cached) and _bounded_read(cached):
        return cached
    if not str(url).lower().startswith(_HTTP_PREFIXES):
        return ""
    try:
        import requests
    except ImportError:  # pragma: no cover - requests ships with the client
        return ""
    body = bytearray()
    try:
        with requests.get(
            str(url), timeout=REQUEST_TIMEOUT, stream=True,
            headers={"User-Agent": "BeyondTournament/1.0"},
        ) as response:
            response.raise_for_status()
            for chunk in response.iter_content(65536):
                if not chunk:
                    continue
                body.extend(chunk)
                if len(body) > MAX_DOWNLOAD_BYTES:
                    return ""
    except Exception:
        return ""
    if not body or not cached:
        return ""
    temporary = cached + ".part"
    try:
        with open(temporary, "wb") as handle:
            handle.write(body)
        os.replace(temporary, cached)
    except OSError:
        with contextlib.suppress(OSError):
            os.remove(temporary)
        return ""
    return cached


def _embedded_picture(path):
    """Extract the cover embedded in a local media file, or ``""``.

    ffmpeg maps an attached picture as the file's first video stream, so this
    works for the ID3/APIC and MP4 cover shapes without a second library.  A
    song with no picture answers empty and is not an error.
    """
    if not FFMPEG_PATH or not path or not os.path.isfile(path):
        return ""
    try:
        stat = os.stat(path)
    except OSError:
        return ""
    key = f"embedded:{os.path.abspath(path)}:{stat.st_mtime_ns}:{stat.st_size}"
    cached = _cache_path(key, ".png")
    if cached and os.path.isfile(cached) and _bounded_read(cached):
        return cached
    command = [
        str(FFMPEG_PATH), "-v", "error", "-y", "-i", str(path),
        "-map", "0:v:0", "-frames:v", "1", "-f", "image2pipe",
        "-vcodec", "png", "pipe:1",
    ]
    try:
        result = subprocess.run(
            command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            timeout=DECODE_TIMEOUT, check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if result.returncode != 0 or not result.stdout or not cached:
        return ""
    try:
        with open(cached, "wb") as handle:
            handle.write(result.stdout)
    except OSError:
        return ""
    return cached


def _materialize(source):
    """The local path holding a source's bytes, or ``""`` (worker only)."""
    kind, value = source
    if kind == "file":
        return value if os.path.isfile(value) else ""
    if kind == "embedded":
        return _embedded_picture(value)
    if kind == "url":
        return _download(value)
    return ""


def _frame_paths(path, animated=True):
    """Decode a picture into PNG frames, or answer ``[]``.

    The frames are scaled once, here, so the menu only ever scales a 320 px
    image down to its panel.  A still has no timestamps for the ``fps`` filter,
    which would then produce no output at all, so only an animation gets it.  A
    temporary folder is used because ffmpeg's image writer wants a name
    pattern; it is removed in ``finally`` even when decoding fails.
    """
    if not FFMPEG_PATH or not path:
        return []
    folder = tempfile.mkdtemp(prefix="bt-song-picture-")
    try:
        pattern = os.path.join(folder, "frame-%03d.png")
        filters = []
        if animated:
            filters.append(f"fps={FRAME_RATE}")
        filters.append(
            f"scale={MAX_FRAME_EDGE}:{MAX_FRAME_EDGE}:"
            "force_original_aspect_ratio=decrease"
        )
        command = [
            str(FFMPEG_PATH), "-v", "error", "-y", "-i", str(path),
            "-vf", ",".join(filters),
            "-frames:v", str(MAX_FRAMES), "-f", "image2", pattern,
        ]
        try:
            subprocess.run(
                command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=DECODE_TIMEOUT, check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, subprocess.SubprocessError):
            return []
        frames = []
        for name in sorted(os.listdir(folder)):
            if not name.lower().endswith(".png"):
                continue
            data = _bounded_read(os.path.join(folder, name))
            if data:
                frames.append(data)
            if len(frames) >= MAX_FRAMES:
                break
        return frames
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def _frames_from_file(path):
    """Bytes for one still picture or every frame of an animated one."""
    data = _bounded_read(path)
    if not data:
        return []
    if not _is_animated(data):
        # A still PNG/JPEG/BMP is already a surface pygame can load.  A still
        # WebP is not, so ffmpeg turns it into one PNG; no fps filter here.
        if _is_webp(data):
            return _frame_paths(path, animated=False)
        return [data]
    return _frame_paths(path)


class CoverArt:
    """The current track's picture, loaded away from the frame loop.

    ``state`` is one of ``"idle"`` (no track), ``"loading"``, ``"ready"`` or
    ``"missing"``.  A menu asks :meth:`frame` for the frame due now; the answer
    is a pygame surface already fitted to a square of the requested size, or
    ``None`` while loading.  The object owns no game state and never speaks.
    """

    def __init__(self, game, enabled=DEFAULT_ENABLED):
        self._game = game
        self._enabled = bool(enabled)
        self._state = "idle"
        self._title = ""
        self._key = ""
        self._source = None
        self._generation = 0
        self._frames = []
        self._scaled = {}
        self._started = 0.0

    # -- what a menu asks -------------------------------------------------

    @property
    def state(self):
        return self._state

    @property
    def title(self):
        return self._title

    def frame(self, size):
        """The picture at ``size`` pixels square, or ``None``.

        A still picture returns its one surface every time.  An animated one
        picks the frame the menu's own clock has reached and loops, so the
        picture is live without the caller keeping any animation state.
        """
        if self._state != "ready" or not self._frames:
            return None
        try:
            size = max(32, int(size))
        except (TypeError, ValueError):
            return None
        surfaces = self._scaled.get(size)
        if surfaces is None:
            # A window may be resized while a song plays; keep the cache to the
            # last few sizes rather than every width it was dragged through.
            if len(self._scaled) >= 4:
                self._scaled.clear()
            surfaces = [self._fit(image, size) for image in self._frames]
            self._scaled[size] = surfaces
        if not surfaces:
            return None
        if len(surfaces) == 1:
            return surfaces[0]
        delay = 1.0 / FRAME_RATE
        index = int((time.monotonic() - self._started) / delay) % len(surfaces)
        return surfaces[index]

    @staticmethod
    def _fit(image, size):
        """One decoded frame on a transparent square, aspect preserved."""
        import pygame

        width, height = image.get_size()
        if width <= 0 or height <= 0:
            return None
        scale = min(size / width, size / height)
        target = (max(1, int(width * scale)), max(1, int(height * scale)))
        surface = pygame.Surface((size, size), pygame.SRCALPHA)
        try:
            picture = pygame.transform.smoothscale(image, target)
        except Exception:
            picture = pygame.transform.scale(image, target)
        surface.blit(picture, ((size - target[0]) // 2,
                               (size - target[1]) // 2))
        return surface

    # -- what the bot tells it --------------------------------------------

    def set_enabled(self, enabled):
        """Turn the picture on or off without forgetting the current track."""
        enabled = bool(enabled)
        if enabled == self._enabled:
            return
        self._enabled = enabled
        if not enabled:
            self._generation += 1
            self._state = "idle"
            self._frames = []
            self._scaled = {}
            return
        if self._key and self._source is not None:
            self._start()

    def clear(self):
        """Forget the track and release every decoded frame."""
        self._generation += 1
        self._state = "idle"
        self._title = ""
        self._key = ""
        self._source = None
        self._frames = []
        self._scaled = {}

    def set_track(self, title, page_url="", local_path="", picture_url=""):
        """Choose and load the picture belonging to one track.

        ``picture_url`` is the source's own thumbnail when a resolver supplied
        one.  Otherwise a YouTube page names its stable thumbnail, a local
        song prefers an image beside it and falls back to its embedded cover.
        The same track is a no-op, so a seek or a rebuilt menu costs nothing.
        """
        title = str(title or "")
        source = self._source_for(page_url, local_path, picture_url)
        key = self._key_for(source)
        if key and key == self._key and self._state in ("loading", "ready"):
            self._title = title
            return
        self._generation += 1
        self._title = title
        self._key = key
        self._source = source
        self._frames = []
        self._scaled = {}
        if not self._enabled:
            self._state = "idle"
            return
        if source is None:
            self._state = "missing"
            return
        self._start()

    @staticmethod
    def _source_for(page_url, local_path, picture_url):
        picture_url = str(picture_url or "").strip()
        if picture_url.lower().startswith(_HTTP_PREFIXES):
            return ("url", picture_url)
        local_path = str(local_path or "")
        sidecar = sidecar_picture(local_path)
        if sidecar:
            return ("file", sidecar)
        if local_path and os.path.isfile(local_path):
            return ("embedded", local_path)
        thumbnail = youtube_thumbnail(page_url)
        if thumbnail:
            return ("url", thumbnail)
        return None

    @staticmethod
    def _key_for(source):
        if source is None:
            return ""
        kind, value = source
        if kind == "file":
            try:
                stat = os.stat(value)
                return f"file:{os.path.abspath(value)}:{stat.st_mtime_ns}:{stat.st_size}"
            except OSError:
                return ""
        if kind == "embedded":
            try:
                stat = os.stat(value)
                return f"embedded:{os.path.abspath(value)}:{stat.st_mtime_ns}:{stat.st_size}"
            except OSError:
                return ""
        return f"url:{value}"

    def _start(self):
        generation = self._generation
        source = self._source
        self._state = "loading"

        def work():
            frames = []
            try:
                path = _materialize(source)
                if path:
                    frames = _frames_from_file(path)
            except Exception:  # a bad picture is a missing one, never a crash
                frames = []
            try:
                self._deliver(lambda: self._on_loaded(generation, frames))
            except Exception:  # the game may already be shutting down
                pass

        threading.Thread(target=work, daemon=True,
                         name="music-bot-picture").start()

    def _deliver(self, callback):
        put = getattr(self._game, "put", None)
        if callable(put):
            put(callback)
        else:  # pragma: no cover - a test's headless game
            callback()

    def _on_loaded(self, generation, frames):
        if generation != self._generation:
            return
        if not self._enabled:
            self._state = "idle"
            return
        surfaces = []
        if frames:
            import pygame

            for data in frames:
                try:
                    image = pygame.image.load(io.BytesIO(data))
                    if pygame.display.get_init():
                        with contextlib.suppress(Exception):
                            image = image.convert_alpha()
                    surfaces.append(image)
                except Exception:
                    continue
        if not surfaces:
            self._state = "missing"
            self._frames = []
            self._scaled = {}
            return
        self._frames = surfaces
        self._scaled = {}
        self._started = time.monotonic()
        self._state = "ready"
