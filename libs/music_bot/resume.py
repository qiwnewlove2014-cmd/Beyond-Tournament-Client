"""Where the Music Bot left each track, so coming back continues it.

One small JSON file beside the player's settings, replaced atomically: a map
of track -> playhead, plus the track that played last.  Nothing here touches a
buffer, a stream, a source or a playout instant -- a missing, unreadable or
hand-edited file simply means there is nothing to continue.

`MapMusicBot.loop` writes the playing track every few seconds, so an abrupt
exit loses at most that window; a track the player leaves (a skip, a pause, a
stop, a seek) is written at once.  The map is bounded and pruned oldest-first,
so a long session cannot grow the file without limit.  A point inside the last
few seconds of its track counts as finished: that song starts from the top
instead of resuming at its own outro.
"""

from __future__ import annotations

import json
import math
import os
import time

from .. import options

FILE_NAME = "music_resume.json"
VERSION = 2
# The loop's autosave window: an abrupt exit loses at most this much position.
SAVE_INTERVAL = 5.0
NEAR_END_SECONDS = 5.0
MAX_POSITION_SECONDS = 24 * 60 * 60
MAX_TRACKS = 30
MAX_TARGET_CHARS = 4096
MAX_TITLE_CHARS = 300
SOURCES = ("youtube", "local")


def path():
    """The one file this module owns, beside the client's settings."""
    return os.path.join(options.config_dirs.user_config_dir, FILE_NAME)


def _finite(value, fallback=None):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return fallback
    if not math.isfinite(number):
        return fallback
    return number


def empty_book():
    return {"version": VERSION, "last": "", "tracks": {}}


def normalize_point(raw):
    """One track's playhead from anything, or None when there is no track.

    Every field is clamped or defaulted rather than refused: a resume file that
    a person edited, or that an older build wrote, must not stop the bot from
    opening.
    """
    if not isinstance(raw, dict):
        return None
    target = raw.get("target")
    if not isinstance(target, str):
        return None
    target = target.strip()[:MAX_TARGET_CHARS]
    if not target:
        return None
    source = raw.get("source")
    if source not in SOURCES:
        source = "youtube" if target.startswith(("http://", "https://")) else "local"
    title = raw.get("title")
    title = title.strip()[:MAX_TITLE_CHARS] if isinstance(title, str) else ""
    position = _finite(raw.get("position"), 0.0)
    if position is None or position < 0.0:
        position = 0.0
    duration = _finite(raw.get("duration"))
    if duration is not None and duration <= 0.0:
        duration = None
    saved_at = _finite(raw.get("saved_at"), 0.0) or 0.0
    return {
        "target": target,
        "title": title or target,
        "source": source,
        "position": min(position, MAX_POSITION_SECONDS),
        "duration": duration,
        "paused": bool(raw.get("paused")),
        "saved_at": saved_at,
    }


def _pruned(tracks, keep=MAX_TRACKS):
    """The newest `keep` tracks, so the file cannot grow without limit.

    The insertion position breaks a tie: two saves inside one clock tick must
    keep the one written second, not the one a sort happened to see first.
    """
    ordered = sorted(enumerate(tracks.items()),
                     key=lambda item: (item[1][1].get("saved_at", 0.0), item[0]),
                     reverse=True)
    return dict(item[1] for item in ordered[:keep])


def normalize_book(raw):
    """The book from anything on disk: a version 2 map, or a version 1 point."""
    if not isinstance(raw, dict):
        return empty_book()
    if "tracks" in raw:
        tracks = {}
        for value in (raw.get("tracks") or {}).values() if isinstance(raw.get("tracks"), dict) else ():
            point = normalize_point(value)
            if point is not None:
                tracks[point["target"]] = point
        last = raw.get("last") if isinstance(raw.get("last"), str) else ""
    else:
        # Version 1 held exactly one point, written by the build before the
        # book existed; reading it as one entry keeps a player's place.
        point = normalize_point(raw)
        tracks = {point["target"]: point} if point else {}
        last = point["target"] if point else ""
    tracks = _pruned(tracks)
    if last not in tracks:
        last = max(tracks.items(), key=lambda item: item[1].get("saved_at", 0.0))[0] if tracks else ""
    return {"version": VERSION, "last": last, "tracks": tracks}


def load():
    """The whole book; an empty one when there is nothing readable."""
    try:
        with open(path(), "r", encoding="utf-8") as handle:
            return normalize_book(json.load(handle))
    except (OSError, ValueError, TypeError):
        return empty_book()


def write(book):
    destination = path()
    temporary = destination + ".tmp"
    try:
        directory = os.path.dirname(destination)
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(temporary, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(book, handle, ensure_ascii=False, sort_keys=True)
            handle.write("\n")
        os.replace(temporary, destination)
        return True
    except OSError:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        return False


def add(book, point, now=None):
    """`book` with this point upserted and made the last one (pure)."""
    point = normalize_point(point)
    if point is None:
        return book
    point["saved_at"] = time.time() if now is None else float(now)
    tracks = dict(book.get("tracks") or {})
    tracks[point["target"]] = point
    return {"version": VERSION, "last": point["target"],
            "tracks": _pruned(tracks)}


def without(book, target):
    """`book` with one track's point removed, and its pointer moved on (pure)."""
    target = str(target or "")
    tracks = dict(book.get("tracks") or {})
    tracks.pop(target, None)
    last = book.get("last") or ""
    if last == target:
        last = (max(tracks.values(),
                    key=lambda point: point.get("saved_at", 0.0))["target"]
                if tracks else "")
    return {"version": VERSION, "last": last, "tracks": tracks}


def save(point):
    """Remember one track's playhead.  The saved book, or None if the disk refused."""
    point = normalize_point(point)
    if point is None:
        return None
    book = add(load(), point)
    return book if write(book) else None


def get(target, book=None):
    """One track's saved point, or None."""
    book = load() if book is None else book
    if not target:
        return None
    return (book.get("tracks") or {}).get(str(target))


def forget(target=None):
    """Drop one track's point (or every one).  True when something was written."""
    if target is None:
        return write(empty_book())
    book = load()
    if str(target) not in book["tracks"]:
        return False
    return write(without(book, target))


def offset_for(point, duration=None):
    """Seconds to restart the saved track at; 0 when it is not worth it.

    A position at the very start needs no seek, and a position inside the last
    few seconds means the track had already finished -- continuing there would
    play the outro and stop, which is not what "where I left off" means.
    """
    if not point:
        return 0.0
    position = _finite(point.get("position"), 0.0) or 0.0
    if position <= 0.0:
        return 0.0
    end = _finite(duration if duration is not None else point.get("duration"))
    if end is not None and end > 0.0 and position >= end - NEAR_END_SECONDS:
        return 0.0
    return position
