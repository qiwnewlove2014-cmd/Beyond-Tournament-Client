"""Small, session-owned cache of already-playable jukebox media credentials.

One JukeboxPlayer owns this cache across map changes. Worker threads may share
it, but it owns no audio, processes, playback position, or persistent files.
Only successful playback should publish entries; failed playback invalidates
the exact entry it used so an older worker cannot remove a newer resolution.
"""

from collections import OrderedDict
from dataclasses import dataclass
import math
import re
import threading
import time
from urllib.parse import parse_qsl, unquote, urlsplit

from .youtube_resolver import _valid_url, _validated_info


_MAX_ENTRIES = 8
_TTL = 300.0
_EXPIRY_MARGIN = 30.0
_EXPIRY = re.compile(r"[0-9]{1,20}\Z", re.ASCII)


@dataclass(frozen=True, eq=False, repr=False, slots=True)
class _MediaEntry:
    _url: str
    _headers: tuple
    _deadline: float
    _expires_at: float

    def info(self):
        """Give each consumer its own plain dict, preserving paired headers."""
        return {"url": self._url, "http_headers": dict(self._headers)}


def _https_parts(url):
    if not _valid_url(url):
        return None
    try:
        parsed = urlsplit(url)
        if (parsed.scheme != "https" or parsed.username is not None
                or parsed.password is not None or parsed.port not in (None, 443)):
            return None
        return parsed
    except (ValueError, UnicodeError):
        return None


def _canonical_key(url):
    parsed = _https_parts(url)
    if parsed is None:
        return False
    host = parsed.hostname.lower()
    if not (host in ("youtube.com", "youtu.be")
            or host.endswith(".youtube.com")):
        return False
    # A /watch URL does not itself identify live media, so callers should also
    # restrict reuse to known fixed-length songs. Never cache explicit /live.
    path = unquote(parsed.path).lower()
    return path != "/live" and not path.startswith("/live/")


def _media_expiry(url):
    parsed = _https_parts(url)
    if parsed is None:
        return None
    host = parsed.hostname.lower()
    if not (host == "googlevideo.com" or host.endswith(".googlevideo.com")):
        return None
    # Restrict this optimization to direct media, never HLS/DASH manifests.
    if unquote(parsed.path).lower() != "/videoplayback":
        return None
    try:
        fields = parse_qsl(parsed.query, keep_blank_values=True, max_num_fields=256)
    except (ValueError, UnicodeError):
        return None
    expirations = []
    for raw_name, value in fields:
        name, value_lower = raw_name.lower(), value.lower()
        if name == "expire":
            if raw_name != "expire":
                return None
            expirations.append(value)
        if name in ("live", "is_live", "livestream", "noclen"):
            if value_lower not in ("0", "false"):
                return None
        if name in ("source", "playlist_type") and "live" in value_lower:
            return None
    if len(expirations) != 1 or _EXPIRY.fullmatch(expirations[0]) is None:
        return None
    return int(expirations[0]) - _EXPIRY_MARGIN


class JukeboxMediaCache:
    """Bounded URL/header reuse; all external work stays outside the lock."""

    def __init__(self, *, monotonic=None, wall_clock=None):
        self._monotonic = time.monotonic if monotonic is None else monotonic
        self._wall_clock = time.time if wall_clock is None else wall_clock
        self._entries = OrderedDict()
        self._lock = threading.Lock()

    def _now(self):
        try:
            monotonic = float(self._monotonic())
            wall = float(self._wall_clock())
            if math.isfinite(monotonic) and math.isfinite(wall):
                return monotonic, wall
        except (TypeError, ValueError, OverflowError):
            pass
        return None

    @staticmethod
    def _expired(entry, monotonic, wall):
        return monotonic >= entry._deadline or wall >= entry._expires_at

    def get(self, canonical_url):
        if not _canonical_key(canonical_url):
            return None
        now = self._now()
        if now is None:
            return None
        with self._lock:
            entry = self._entries.get(canonical_url)
            if entry is None:
                return None
            if self._expired(entry, *now):
                del self._entries[canonical_url]
                return None
            self._entries.move_to_end(canonical_url)
            return entry

    def put(self, canonical_url, info):
        if not _canonical_key(canonical_url):
            return None
        try:
            validated = _validated_info(info)
        except (TypeError, ValueError, UnicodeError, RuntimeError):
            return None
        if validated is None:
            return None
        expires_at = _media_expiry(validated["url"])
        now = self._now()
        if expires_at is None or now is None or expires_at <= now[1]:
            return None
        entry = _MediaEntry(
            validated["url"], tuple(validated["http_headers"].items()),
            now[0] + min(_TTL, expires_at - now[1]), expires_at,
        )
        with self._lock:
            # At most eight comparisons, and no validation, callbacks or I/O
            # inside the lock. Expired entries should not displace live ones.
            for key in tuple(self._entries):
                if self._expired(self._entries[key], *now):
                    del self._entries[key]
            self._entries[canonical_url] = entry
            self._entries.move_to_end(canonical_url)
            while len(self._entries) > _MAX_ENTRIES:
                self._entries.popitem(last=False)
        return entry

    def invalidate(self, canonical_url, exact_entry):
        if not _canonical_key(canonical_url):
            return False
        with self._lock:
            entry = self._entries.get(canonical_url)
            if entry is None or entry is not exact_entry:
                return False
            del self._entries[canonical_url]
            return True
