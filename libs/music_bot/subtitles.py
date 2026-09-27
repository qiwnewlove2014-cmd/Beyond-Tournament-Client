"""Spoken subtitles: the Music Bot reads a video's captions out loud.

The player hears the lines of the song or video they are playing, in their own
screen reader, while the audio keeps going underneath. This module is the whole
feature's engine-free half: fetching a YouTube caption track, turning it into
timed lines, and deciding which line is due at the song position the player's
own ears have reached.

Three measured facts shape it, and each has exactly one home here:

* **A caption track is metadata on the page yt-dlp already reads.** Every song
  the bot plays keeps its canonical YouTube page URL (that is what
  ``canonical_url`` is for), and asking that page for its captions is a second
  look at a response yt-dlp already parses. The ``json3`` track is what this
  file reads: measured over real tracks it is tiny (8,325 bytes for 3:32 of a
  lyric track, about 39 bytes a second) and it is the one format offered for an
  uploader's track and for the automatic one alike.
* **YouTube rate limits the caption endpoint, fast.** A second request for a
  track milliseconds after the first answered ``429 Too Many Requests`` while
  this was being written, so a track is fetched once per page and kept
  (:class:`CaptionFetcher`), and a 429 is reported to the player as "busy"
  rather than as "this video has no subtitles".
* **A screen reader is not a sample-accurate output.** ``speak()`` hands the
  line over and returns: there is no callback for "that line finished", the
  reader's own queue is invisible from here, and its rate is the player's
  setting. So cues are aimed at the *song's* position (never at a stopwatch),
  a line whose moment has already gone past is dropped rather than read late,
  and a reader that has fallen too far behind the music stops being fed
  (:class:`SubtitleReader`) -- the same "skip once, never drift" rule the
  guitar monitor's queue lives by.

Nothing here touches the game, OpenAL, or the network at import time: the
fetch is the only I/O and it runs on the caller's worker thread.
"""

from bisect import bisect_right
from collections import OrderedDict
from dataclasses import dataclass
import contextlib
import importlib
import json
import threading
import time
from urllib.parse import urlsplit

from .. import logger
from ..youtube_resolver import _valid_url


# ── What a player picked, in the words the menus speak ──────────────────────

#: The language orders a player can ask for. A preference is an ORDER, never a
#: single tag: a Thai player watching a Thai video wants the uploader's Thai
#: track, and the same player on an English documentary wants English rather
#: than silence. The ``*-only`` entries exist for the player who wants nothing
#: else read over the music.
LANGUAGE_PREFERENCES = (
    ("th", "Thai first, then English", ("th", "en")),
    ("en", "English first, then Thai", ("en", "th")),
    ("th-only", "Thai only", ("th",)),
    ("en-only", "English only", ("en",)),
)
DEFAULT_LANGUAGE_PREFERENCE = "th"

#: The names of the languages a player hears in the spoken confirmations.
LANGUAGE_LABELS = {"th": "Thai", "en": "English"}

#: How far a player may shift the captions against the audio, and by how much
#: one press moves them. Positive speaks EARLIER (the cue clock is read that
#: much ahead of the song).
OFFSET_LIMIT_MS = 5000
OFFSET_STEP_MS = 500


def languages_for(preference):
    """The language order to ask YouTube for (Thai first when unknown)."""
    for key, _label, languages in LANGUAGE_PREFERENCES:
        if key == preference:
            return languages
    return LANGUAGE_PREFERENCES[0][2]


def preference_label(preference):
    """The picker's own words for a preference."""
    for key, label, _languages in LANGUAGE_PREFERENCES:
        if key == preference:
            return label
    return LANGUAGE_PREFERENCES[0][1]


def normalize_preference(value):
    """A stored preference, or the default when it is not one of ours."""
    for key, _label, _languages in LANGUAGE_PREFERENCES:
        if key == value:
            return key
    return DEFAULT_LANGUAGE_PREFERENCE


def language_label(language):
    """What to call a language tag in speech (the tag itself when unknown)."""
    return LANGUAGE_LABELS.get(str(language or "").lower(), str(language or ""))


def normalize_offset(value):
    """A stored sync offset, clamped to what a player may ask for."""
    try:
        offset = int(value)
    except (TypeError, ValueError):
        return 0
    return max(-OFFSET_LIMIT_MS, min(OFFSET_LIMIT_MS, offset))


def offset_label(offset_ms):
    """The sync line's own words for a stored offset."""
    offset_ms = normalize_offset(offset_ms)
    if not offset_ms:
        return "Timing: as the video timed it"
    direction = "earlier" if offset_ms > 0 else "later"
    return f"Timing: spoken {abs(offset_ms) / 1000:.1f} seconds {direction}"


def is_caption_page(url):
    """True when this URL is a page YouTube hands caption tracks out for."""
    if not _valid_url(url):
        return False
    try:
        host = (urlsplit(url).hostname or "").lower()
    except (ValueError, UnicodeError):
        return False
    return host in ("youtube.com", "youtu.be") or host.endswith(".youtube.com")


# ── The cues ────────────────────────────────────────────────────────────────

#: Longest line handed to the reader. A line longer than this is cut at a word
#: boundary: the reader has no way to catch up inside a song, and the cut is
#: where a queue of speech starts growing.
CUE_MAX_CHARS = 200

#: Symbols YouTube's automatic captions use for music. A screen reader reads
#: them out as words ("music note music note") over the song itself, so they
#: are dropped rather than spoken. ``[...]`` tags are kept: "[Laughter]" is
#: information, exactly like the words beside it.
_MUSIC_MARKS = "\u266a\u266b\u266c\u2669"


@dataclass(frozen=True, slots=True)
class Cue:
    """One line of a caption track, on the song's own clock (milliseconds)."""

    start_ms: int
    end_ms: int
    text: str


def parse_json3(payload):
    """The cues in one YouTube ``json3`` caption track.

    ``payload`` is the response body (bytes or str) or an already-decoded
    document, so a caller and a test can hand in either. Anything malformed
    answers ``[]`` -- a caption track is never worth failing playback over.
    """
    if isinstance(payload, dict):
        document = payload
    else:
        try:
            if isinstance(payload, (bytes, bytearray)):
                payload = bytes(payload).decode("utf-8", "replace")
            document = json.loads(payload)
        except (ValueError, TypeError, UnicodeError):
            return []
    events = document.get("events") if isinstance(document, dict) else None
    if not isinstance(events, list):
        return []
    fragments = []
    for event in events:
        if not isinstance(event, dict):
            continue
        segments = event.get("segs")
        text = "".join(str(segment.get("utf8") or "")
                       for segment in segments or ()
                       if isinstance(segment, dict))
        if not text:
            continue
        fragments.append((_int_or_zero(event.get("tStartMs")),
                          _int_or_zero(event.get("dDurationMs")),
                          text))
    if not fragments:
        return []
    return _tidy(_lines_from(fragments))


def _int_or_zero(value):
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _lines_from(fragments):
    """One cue per caption LINE, out of the per-fragment events.

    Both shapes arrive in this one format and are told apart by the one thing
    that separates them: an uploader's track puts a whole line in one event
    and ends it with a newline, while the automatic one re-emits a line as it
    lengthens (word group, then more of the same line), ending the line it
    finally spells out. So an event that BEGINS with what is already buffered
    is that line again rather than more of it -- concatenating those would
    read the same sentence out several times in a row. What is left over is a
    line split across events, joined with the space YouTube left out (a Thai
    caption, which needs no spaces, only gains a harmless one). A track that
    never spells a line break at all gets one cue per event, which is the only
    reading left.
    """
    if not any("\n" in text for _start, _duration, text in fragments):
        return [Cue(start, start + max(duration, 1), text)
                for start, duration, text in fragments]
    cues = []
    start_ms = 0
    end_ms = 0
    buffer = ""
    for start, duration, text in fragments:
        if not buffer:
            # A line begins where the fragment carrying it begins; a buffer
            # that is still growing keeps the start of its FIRST fragment, so
            # a rolling re-emission does not walk the line forward.
            start_ms = start
        buffer = text if buffer and text.startswith(buffer) else _join(buffer, text)
        end_ms = start + duration
        while "\n" in buffer:
            head, buffer = buffer.split("\n", 1)
            if head.strip():
                cues.append(Cue(start_ms, end_ms, head))
            # What follows the break belongs to the same event's clock.
            start_ms = start
    if buffer.strip():
        cues.append(Cue(start_ms, max(end_ms, start_ms + 1), buffer))
    return cues


def _join(buffer, text):
    """Two fragments of one line, with the space YouTube left out."""
    if not buffer:
        return text
    if buffer[-1].isspace() or text[:1].isspace():
        return buffer + text
    return f"{buffer} {text}"


def _clean_text(text):
    for mark in _MUSIC_MARKS:
        text = text.replace(mark, " ")
    text = " ".join(text.split())
    if len(text) > CUE_MAX_CHARS:
        cut = text[:CUE_MAX_CHARS]
        space = cut.rfind(" ")
        text = cut[:space] if space > CUE_MAX_CHARS // 2 else cut
        text = text.rstrip()
    return text


def _tidy(cues):
    cleaned = []
    for cue in cues:
        text = _clean_text(cue.text)
        if not text:
            continue
        cleaned.append(Cue(cue.start_ms,
                           max(cue.start_ms + 1, cue.end_ms), text))
    return _fold_rolling(cleaned)


def _fold_rolling(cues):
    """Fold a line that grew inside its own cue back into one cue.

    Automatic captions re-emit a line as it lengthens ("hello", then "hello
    world"), so the same sentence would be read out three times. What makes a
    pair of cues one line is that they overlap in time: a genuinely repeated
    line (a chorus) is a separate cue with its own window and is left alone.
    """
    folded = []
    for cue in cues:
        if folded:
            previous = folded[-1]
            same_line = (cue.text == previous.text
                         and cue.start_ms < previous.end_ms)
            grew = (len(previous.text) < len(cue.text)
                    and cue.text.startswith(previous.text)
                    and cue.start_ms <= previous.end_ms)
            if same_line or grew:
                longer = cue if len(cue.text) > len(previous.text) else previous
                folded[-1] = Cue(previous.start_ms,
                                 max(previous.end_ms, cue.end_ms), longer.text)
                continue
        folded.append(cue)
    return folded


# ── Picking the track the player asked for ──────────────────────────────────

@dataclass(frozen=True, slots=True)
class CaptionTrack:
    """One caption track, and where to read it from."""

    language: str
    url: str
    automatic: bool


def select_track(info, languages):
    """The caption track to read for a language order, or ``None``.

    An uploader's own track wins over an automatic one in *every* language: it
    is a human's transcription of their own video, and automatic captions on a
    song are where the nonsense lives. Within a language the ``json3`` entry is
    the one this module reads (``srv1``/``srv3``/``ttml``/``srt`` sit beside
    it, and an entry whose URL is not a usable https URL is not one at all).
    """
    if not isinstance(info, dict):
        return None
    for automatic in (False, True):
        table = info.get("automatic_captions" if automatic else "subtitles")
        if not isinstance(table, dict):
            continue
        for language in languages:
            for entry in table.get(language) or ():
                if not isinstance(entry, dict):
                    continue
                if str(entry.get("ext") or "").lower() != "json3":
                    continue
                if _valid_url(entry.get("url")):
                    return CaptionTrack(language, entry["url"], automatic)
    return None


# ── Fetching one track per page, once ───────────────────────────────────────

#: How long a track (or a "there is none" answer) is kept. A caption track is
#: the same bytes for every replay of that song, and YouTube answers a second
#: request for one with a 429 -- so the fetch is per page, not per play.
CACHE_TTL_S = 900.0
NEGATIVE_TTL_S = 120.0
CACHE_MAX_ENTRIES = 8

#: A track is small (measured: about 39 bytes a second of video), but a long
#: video is not: two hours is a few hundred KB, so the read is capped rather
#: than trusted. A truncated track still plays -- the lines after the cut are
#: simply not there.
MAX_TRACK_BYTES = 2_000_000

REASON_NONE = ""            # cues are there
REASON_NO_TRACK = "no-track"
REASON_BUSY = "busy"
REASON_FAILED = "failed"
REASON_CANCELLED = "cancelled"

#: What the player hears when a track could not be read. Silence is
#: indistinguishable from a broken feature, so every reason has a sentence --
#: except "cancelled", where the player has already moved on.
_REASON_SENTENCES = {
    REASON_NO_TRACK: "No subtitles for this video.",
    REASON_BUSY: "Subtitles are busy right now. YouTube is limiting requests.",
    REASON_FAILED: "Could not read the subtitles for this video.",
}


def reason_sentence(reason):
    """The one sentence a missing track is reported with."""
    return _REASON_SENTENCES.get(reason, "")


@dataclass(slots=True)
class CaptionLoad:
    """A fetched track, or the reason there is none.

    ``reason`` is ``""`` exactly when ``cues`` is non-empty.
    """

    cues: tuple = ()
    language: str = ""
    automatic: bool = False
    reason: str = REASON_NO_TRACK


class CaptionFetcher:
    """Reads caption tracks, one per page, with a small session-owned cache.

    The yt-dlp module is imported lazily and can be handed in, so this class is
    testable with no network and no yt-dlp installed, and the ~700 ms import
    only ever happens on a worker thread of a player who turned subtitles on.
    """

    def __init__(self, ydl_module=None, clock=time.monotonic,
                 max_entries=CACHE_MAX_ENTRIES):
        self._ydl_module = ydl_module
        self._clock = clock
        self._max_entries = max(1, int(max_entries))
        self._cache = OrderedDict()
        self._lock = threading.Lock()

    def fetch(self, page_url, languages, *, cancelled=None):
        """The cues for ``page_url``, or a :class:`CaptionLoad` saying why not.

        ``languages`` is the order to look in (see :func:`languages_for`).
        ``cancelled`` is polled between the two network steps, so a player who
        skipped the song is not kept waiting for a track nobody will hear.
        """
        if not is_caption_page(page_url):
            return CaptionLoad(reason=REASON_FAILED)
        key = (page_url, tuple(languages))
        cached = self._cached(key)
        if cached is not None:
            return cached
        load = self._read(page_url, tuple(languages), cancelled)
        # Only a real answer is kept for the long TTL: a network wobble must
        # not hide a track for the session, while a "no subtitles here" answer
        # is held briefly so a skipped song is not asked for in a loop (and a
        # 429 is not earned twice).
        self._remember(key, load,
                       CACHE_TTL_S if load.cues else NEGATIVE_TTL_S)
        return load

    def _cached(self, key):
        now = self._clock()
        with self._lock:
            entry = self._cache.get(key)
            if entry is None:
                return None
            expires_at, load = entry
            if now < expires_at:
                self._cache.move_to_end(key)
                return load
            del self._cache[key]
        return None

    def _remember(self, key, load, ttl):
        with self._lock:
            self._cache[key] = (self._clock() + ttl, load)
            self._cache.move_to_end(key)
            while len(self._cache) > self._max_entries:
                self._cache.popitem(last=False)

    def _module(self):
        module = self._ydl_module
        if module is None:
            # Lazy on purpose: the import is ~700 ms and ~24 MB, and only a
            # player who asked for subtitles ever pays it.
            module = importlib.import_module("yt_dlp")
            self._ydl_module = module
        return module

    def _read(self, page_url, languages, cancelled):
        try:
            yt_dlp = self._module()
        except Exception as error:
            logger.log_exception(error, "music_bot.captions.import")
            return CaptionLoad(reason=REASON_FAILED)
        options = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "noplaylist": True,
        }
        try:
            with yt_dlp.YoutubeDL(options) as ydl:
                info = ydl.extract_info(page_url, download=False)
                if _cancelled(cancelled):
                    return CaptionLoad(reason=REASON_CANCELLED)
                track = select_track(info, languages)
                if track is None:
                    return CaptionLoad(reason=REASON_NO_TRACK)
                payload = _read_url(ydl, track.url)
        except Exception as error:
            logger.log_exception(error, "music_bot.captions")
            return CaptionLoad(reason=_failure_reason(error))
        cues = parse_json3(payload)
        if not cues:
            return CaptionLoad(reason=REASON_NO_TRACK)
        return CaptionLoad(cues=tuple(cues), language=track.language,
                           automatic=track.automatic, reason=REASON_NONE)


def _cancelled(cancelled):
    if cancelled is None:
        return False
    with contextlib.suppress(Exception):
        return bool(cancelled())
    return False


def _read_url(ydl, url):
    """Fetch a caption track body through the reader that got the URL.

    The opener is yt-dlp's own, so the request carries whatever cookies and
    headers the page extraction used (a caption URL is bound to the extraction
    that produced it, the same way a signed media URL is).
    """
    response = ydl.urlopen(url)
    try:
        return response.read(MAX_TRACK_BYTES)
    finally:
        close = getattr(response, "close", None)
        if callable(close):
            with contextlib.suppress(Exception):
                close()


def _failure_reason(error):
    """A rate limit is not a missing track, and the player is told which."""
    text = f"{error}".lower()
    status = getattr(error, "status", None) or getattr(error, "code", None)
    if status == 429 or "429" in text or "too many requests" in text:
        return REASON_BUSY
    return REASON_FAILED


# ── Reading the cues against the song ───────────────────────────────────────

#: How late a line may be before it is dropped instead of read. A subtitle is
#: the line being sung NOW: reading one whose moment has gone past is how a
#: stream of captions turns into a stream of captions that are always behind.
LATE_DROP_MS = 2500

#: How far behind the music the reader may fall before lines start being
#: skipped instead of queued. Speech left waiting is heard late for the rest of
#: the track and never catches up, so a bound is the only honest answer -- and
#: it is deliberately generous, because a skipped line is lost content.
MAX_LAG_MS = 8000

#: A cue with no window of its own still gets this much of the reader's time.
MIN_CUE_SPEECH_MS = 1200

#: A position jump this large is a seek, not jitter: the cues around it are
#: re-aimed (backward re-arms the lines the song is about to sing again).
SEEK_BACK_MS = 1500
SEEK_FORWARD_MS = 3000


class SubtitleReader:
    """Speaks the cue the song has reached -- never a backlog of them.

    Every decision here is made against a *position in the song* handed in by
    the caller (the Music Bot's ``content_position()``), so a seek, a pause, a
    stalled buffer or a restarted decode re-base the captions with no extra
    bookkeeping. That is the same rule the jam-note scheduler follows, and for
    the same reason: a stopwatch walks away from the music.
    """

    def __init__(self):
        # No clock of its own on purpose: every decision here is made against
        # the song position the caller hands in, never against wall time.
        self.cues = ()
        self.language = ""
        self.automatic = False
        self.spoken = 0
        self.dropped = 0
        self._starts = []
        self._index = 0
        self._last_position_ms = None
        self._last_spoken = None
        self._reader_free_at_ms = 0

    # -- what is loaded ----------------------------------------------------

    def load(self, cues, language="", automatic=False):
        """Hold a track's cues, aimed at the start of the song."""
        self.cues = tuple(cues or ())
        self._starts = [cue.start_ms for cue in self.cues]
        self.language = language
        self.automatic = automatic
        self.spoken = 0
        self.dropped = 0
        self._index = 0
        self._last_position_ms = None
        self._last_spoken = None
        self._reader_free_at_ms = 0

    def clear(self):
        self.load(())

    @property
    def ready(self):
        return bool(self.cues)

    # -- what is due -------------------------------------------------------

    def pump(self, position_ms, *, offset_ms=0):
        """Speak what the song has reached; return the lines spoken.

        ``[]`` is the ordinary quiet frame: nothing is due yet, the due line
        was already spoken, it had gone past (dropped), or the reader is too
        far behind the music to be handed another one.
        """
        if not self.cues or position_ms is None:
            return []
        target = int(position_ms) + int(offset_ms)
        self._rebase(target)
        due = []
        while (self._index < len(self.cues)
               and self._starts[self._index] <= target):
            due.append(self.cues[self._index])
            self._index += 1
        if not due:
            return []
        cue = due[-1]
        # Everything the song ran past on the way here is a superseded line:
        # the caption being sung is the last one, not the first one owed.
        self.dropped += len(due) - 1
        if cue.text == self._last_spoken:
            return []
        if target - cue.start_ms > LATE_DROP_MS:
            self.dropped += 1
            return []
        budget = max(MIN_CUE_SPEECH_MS, cue.end_ms - cue.start_ms)
        free_at = max(target, self._reader_free_at_ms) + budget
        if free_at - target > MAX_LAG_MS:
            self.dropped += 1
            return []
        self._last_spoken = cue.text
        self._reader_free_at_ms = free_at
        self.spoken += 1
        return [cue.text]

    def _rebase(self, target):
        """Follow the song, including a seek.

        The index counts positions in the track, not progress through it: a
        rewind re-arms the lines the player is about to hear again, and a jump
        forward skips the ones it went past. A cue already spoken is not
        repeated by a still frame either, because the pump keeps the last line
        it spoke -- pausing the bot stops the pump entirely instead.
        """
        previous = self._last_position_ms
        self._last_position_ms = target
        if previous is None:
            # A track joined (or restarted) mid-song: the line being sung now
            # is the one to read, not the one after it.
            index = bisect_right(self._starts, target)
            if (index and target - self._starts[index - 1] <= LATE_DROP_MS):
                index -= 1
            # A track joined (or restarted) mid-song: the lines whose moment
            # has already gone past were never read, and the count says so.
            self.dropped += index
            self._index = index
            return
        if target < previous - SEEK_BACK_MS:
            self.dropped += max(0, self._index)
            self._index = max(0, bisect_right(self._starts, target) - 1)
            self._last_spoken = None
            self._reader_free_at_ms = 0
        elif target > previous + SEEK_FORWARD_MS:
            self._index = max(self._index,
                              bisect_right(self._starts, target - LATE_DROP_MS))
            self._reader_free_at_ms = 0

    def status(self):
        """A line for a menu that has to answer "is this working"."""
        if not self.cues:
            return "No captions loaded."
        source = "automatic captions" if self.automatic else "captions"
        return (f"{language_label(self.language)} {source}: "
                f"{len(self.cues)} lines, {self.spoken} read, "
                f"{self.dropped} skipped.")
