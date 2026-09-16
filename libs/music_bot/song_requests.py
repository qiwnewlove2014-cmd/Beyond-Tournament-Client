# Song requests — a party listener asking the host's music bot for a song.
#
# A Party Sync guest hears the host's music but the queue lives on the HOST's
# machine (YouTubeSearcher + next_up_queue are client-side; the server has no
# music-bot queue at all). A request therefore travels three legs:
#
#   guest client ──/m <song>──▶ server (membership + rate limit) ──▶ host
#   client ──search, enqueue──▶ queue ──result──▶ server ──▶ the room
#
# This module is the host's half of the decision, kept free of pygame/OpenAL so
# the rules are unit-testable: may this request be served, and what do we say
# when it may not. The client that owns the queue is the only thing that can
# answer truthfully ("the bot is off", "no results", "the queue is full"), so
# every reason is decided here and nowhere else.
#
# The rules (each one is a way a party's queue gets ruined by one listener):
#   * the host's switch is the gate ("Song Requests: ON/OFF"), and it is off
#     until the host turns it on — a session must never turn a queue public
#   * one request per requester per COOLDOWN_S (spam protection that works even
#     before the search, which is the expensive part: yt-dlp takes ~1 s)
#   * at most MAX_IN_QUEUE of the host's upcoming slots may be requests
#
# The command is `/m <song>` -- also `/p` (the spelling this shipped with) and
# `/play`; see party_sync.SONG_REQUEST_COMMANDS.
#   * the same song asked for twice inside DEDUPE_S is the same request
#   * what is playing now is never touched — a request only ever appends
#   * the queue itself is shared read-only with the session (what is playing,
#     what is waiting, whose song) — bounded, and only ever the host's copy
#
# The server rate-limits on its own side as well (and refuses a session's
# requests when the host's flag is off), so a client with a stale flag cannot
# turn the host's queue into a target.

import time

from ..party_sync import clean_song_query

#: One requester cannot ask again for this long.
COOLDOWN_S = 30.0
#: How many search results a request offers its asker to choose from.
CANDIDATE_LIMIT = 5
#: How many searches may wait for a choice before the oldest one is dropped.
MAX_PENDING = 8
#: The request id a host's own /p uses (the server mints `r<N>` for guests).
LOCAL_REQUEST_ID = "local"
#: `PendingPicks.pick`'s note for "the asker withdrew" - not a failure, and
#: nothing for the room to hear.
WITHDRAWN = "withdrawn"
#: Said when a picker has nothing left to show (the request is gone).
NO_CHOICES = "That request is no longer open."
#: The same song from the same requester inside this window is a repeat.
DEDUPE_S = 600.0
#: How many of the host's upcoming slots may hold guest requests.
MAX_IN_QUEUE = 5
#: How many upcoming tracks the host shares with the session.
SHARE_LIMIT = 12
#: Longest title that travels (the queue keeps the full one, the room does not).
SHARE_TITLE_MAX = 120


class SongRequestBoard:
    """The host's record of who asked for what, and when.

    One per music bot. The counts that matter are the ones in the *queue*, not
    here, so nothing in this object is a promise about what is playing: it
    tracks the two time-based rules and answers refusals.
    """

    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._last_at = {}      # requester key -> last accepted request time
        self._asked = {}        # requester key -> [(query_key, time)]

    # ── the decision ────────────────────────────────────────────────────

    def refusal(self, requester, query, queue=(), open_=True, bot_running=True):
        """Why this request cannot be served, or None to serve it.

        `queue` is the host's upcoming tracks (the music bot's next_up_queue):
        only its requested entries are counted, so a host's own queue does not
        use up the listeners' slots.
        """
        requester = str(requester or "").strip()
        query = clean_song_query(query)
        if not requester:
            return "Song requests need a name on them."
        if not bot_running:
            return "The music bot is not running here."
        if not open_:
            return "Song requests are closed in this party."
        if not query:
            return "Which song? Type /m followed by a song name."
        now = self._clock()
        last = self._last_at.get(requester.lower())
        if last is not None and now - last < COOLDOWN_S:
            wait = int(COOLDOWN_S - (now - last)) + 1
            return f"One request at a time - try again in {wait} second(s)."
        key = query.lower()
        for asked_key, asked_at in self._asked.get(requester.lower(), ()):
            if asked_key == key and now - asked_at < DEDUPE_S:
                return f"You already asked for {query}."
        waiting = self.count_in_queue(queue)
        if waiting >= MAX_IN_QUEUE:
            return ("The request queue is full here "
                    f"({waiting} waiting) - try again later.")
        return None

    def note_served(self, requester, query):
        """Record an accepted request (call only after it really was queued)."""
        requester = str(requester or "").strip().lower()
        if not requester:
            return
        now = self._clock()
        self._last_at[requester] = now
        asked = self._asked.setdefault(requester, [])
        asked.append((clean_song_query(query).lower(), now))
        if len(asked) > 8:
            del asked[:-8]

    def forget(self, requester=None):
        """Drop one requester's history, or all of it (a session ended)."""
        if requester is None:
            self._last_at.clear()
            self._asked.clear()
            return
        key = str(requester).strip().lower()
        self._last_at.pop(key, None)
        self._asked.pop(key, None)

    # ── reading the host's queue ────────────────────────────────────────

    def count_in_queue(self, queue):
        """How many upcoming tracks are guest requests."""
        return count_in_queue(queue)


def requested_by(track):
    """The requester's name on a queued track ("" for the host's own).

    One reader for the queue entry's marker: the host's queue menu, the quota
    above and the room's announcement all ask this, so a track can never look
    like a request to one of them and the host's own song to another.
    """
    if not isinstance(track, dict):
        return ""
    return str(track.get("requested_by") or "")


def count_in_queue(queue):
    """How many upcoming tracks hold a listener's request."""
    return sum(1 for track in queue or () if requested_by(track))


def request_line(title, requester, waiting=0, started=False):
    """The line the room hears for an accepted request.

    The host composes it because only the host's client knows the resolved
    title; the server decides who hears it (the requester always, the whole
    room when it was queued).
    """
    title = str(title or "").strip() or "the song"
    requester = str(requester or "").strip() or "somebody"
    if started:
        return f"Playing now: {title} (requested by {requester})"
    if waiting > 0:
        return f"Queued: {title} (requested by {requester}; {waiting} waiting)"
    return f"Queued: {title} (requested by {requester})"


# ── choosing one of the results ─────────────────────────────────────────
# A search returns five candidates (versions, uploads, live takes), and only
# the person who asked knows which one they meant. So the host searches and
# offers them: `PendingPicks` keeps the host's copy (the full result, with the
# URLs a queue entry needs) and what travels is the choice itself, as an index
# (`choice_line` numbers them). The asker's pick comes back as that index and
# is resolved against the host's own list -- a title or a URL from a client is
# never trusted, exactly like every other value that reaches a host's queue.


def _duration_s(value, default=0):
    """Seconds as an int, bounded, or `default` when it cannot be read."""
    try:
        secs = int(float(value))
    except (TypeError, ValueError):
        return default
    if secs <= 0 or secs > 24 * 3600:
        return default
    return secs


def choice_line(index, title, seconds=None):
    """One candidate in a picker: `3. A Song (4:29)`.

    The host's own search-results menu and a requester's picker show the same
    results of the same search, so both render them here (`4:29`, or no
    duration at all when the search did not say).
    """
    title = " ".join(str(title or "").split()) or "Unknown"
    secs = _duration_s(seconds)
    if secs:
        return f"{index}. {title} ({secs // 60}:{secs % 60:02d})"
    return f"{index}. {title}"


def candidates(results, limit=CANDIDATE_LIMIT):
    """The playable results of one search, in the order they were found.

    One reader for "is this result playable" (a canonical webpage URL or a
    direct URL) and one place that decides what a queue entry will carry, so a
    picked song is queued exactly the way the host's own search queued it.
    """
    out = []
    for result in results or ():
        if not isinstance(result, dict):
            continue
        webpage = str(result.get("webpage_url") or "").strip()
        direct = str(result.get("url") or "").strip()
        if not (webpage or direct):
            continue
        out.append({
            "title": " ".join(str(result.get("title") or "").split())[:SHARE_TITLE_MAX],
            "duration": _duration_s(result.get("duration")),
            "webpage_url": webpage,
            "direct_url": direct,
            "http_headers": dict(result.get("http_headers") or {}),
        })
        if len(out) >= limit:
            break
    return out


def offered(items):
    """What travels to an asker: `[{title, duration}]`, and nothing else.

    The URLs stay on the host: a picker is a menu with names in it, and the
    choice that comes back is an index into the host's own list.
    """
    out = []
    for item in items or ():
        out.append({
            "title": str(item.get("title") or ""),
            "duration": _duration_s(item.get("duration")),
        })
    return out


class PendingPicks:
    """Searches this host ran that are waiting for their asker to choose.

    One per music bot. Keyed by the request id the server minted, which is
    what makes a pick answerable at all: an entry exists only for a request
    that really happened here, and it remembers **who** asked, so one listener
    can never answer for somebody else's request. An entry stays until it is
    picked or withdrawn (a listener may take their time -- the asker decides
    when), so the table is bounded and the oldest entry is dropped when a new
    search arrives.
    """

    def __init__(self, limit=MAX_PENDING):
        self._limit = max(1, int(limit))
        self._entries = {}      # request_id -> {requester, query, items}

    def add(self, request_id, requester, query, results):
        """Offer one search. Returns the candidates it may be picked from."""
        found = candidates(results)
        if not found:
            return []
        key = str(request_id)
        self._entries.pop(key, None)
        self._entries[key] = {
            "requester": str(requester or "").strip(),
            "query": str(query or ""),
            "items": found,
        }
        while len(self._entries) > self._limit:
            self._entries.pop(next(iter(self._entries)))
        return found

    def offered(self, request_id):
        """What travels to the asker (`[]` when the request is not open)."""
        entry = self._entries.get(str(request_id))
        if not entry:
            return []
        return offered(entry["items"])

    def query(self, request_id):
        """What was asked for ("" when the request is not open)."""
        entry = self._entries.get(str(request_id))
        return entry["query"] if entry else ""

    def pick(self, request_id, requester, index):
        """What one pick means: `(candidate, None)`, `(None, reason)`, or
        `(None, WITHDRAWN)` when the asker said "nothing, thanks".

        The requester must be the one the search was for: a pick is the only
        thing a listener can send at a host's queue, so it may only ever
        answer that listener's own request.
        """
        key = str(request_id)
        entry = self._entries.get(key)
        if entry is None:
            return None, NO_CHOICES
        if entry["requester"].lower() != str(requester or "").strip().lower():
            return None, "That request was somebody else's."
        try:
            i = int(index)
        except (TypeError, ValueError):
            i = -2
        if i == -1:
            self._entries.pop(key, None)
            return None, WITHDRAWN
        if i < 0 or i >= len(entry["items"]):
            return None, "That is not one of the songs offered."
        self._entries.pop(key, None)
        return entry["items"][i], None

    def forget(self, request_id=None):
        """Drop one request, or all of them (a session ended)."""
        if request_id is None:
            self._entries.clear()
            return
        self._entries.pop(str(request_id), None)

    def __len__(self):
        return len(self._entries)


# ── the queue the session can read ──────────────────────────────────────
# A listener hears the host's music but the queue is on the HOST's machine, so
# the host relays a bounded snapshot of it and every listener renders what
# arrives. One builder, one wording: the snapshot is built by `queue_share`
# (the host's side) and read by `queue_lines` (every listener's side), so a
# menu can never describe a queue that is not the one being played.
#
# The host's own Play Queue menu describes that SAME queue, so every line it
# shows is composed here too (`queue_label`, `now_playing_line`, `track_line`,
# `empty_line`) and `queue_lines` is only those builders in order. Two
# spellings of one queue ("3. A Song (requested by Bob)" for the host, "3. A
# Song - requested by Bob" for the listener) is how a host and a listener end
# up reading out two different stories about the same list of songs.


def queue_label(waiting):
    """How much is waiting, in the one wording either menu uses.

    Composed rather than interpolated at each call site: this string is the
    menu title for a listener, the first line for the host and a line in the
    Music Bot menu, and three copies of it is three chances to drift.
    """
    try:
        n = max(0, int(waiting))
    except (TypeError, ValueError):
        n = 0
    return f"Play Queue ({n} waiting)" if n else "Play Queue (empty)"


def now_playing_line(title):
    """The line for the song playing now ("" when the bot has nothing on)."""
    title = " ".join(str(title or "").split())
    return f"Playing now: {title}" if title else ""


def track_line(index, title, who=""):
    """One waiting track, numbered as it is shown in either menu.

    `index` is what the reader sees (the "1." in "1. A Song"), not a queue
    offset: a line the list skipped must not leave a hole in the numbering
    somebody reads out loud.
    """
    title = " ".join(str(title or "").split()) or "Unknown"
    who = " ".join(str(who or "").split())
    return f"{index}. {title}" + (f" (requested by {who})" if who else "")


def empty_line():
    """The sentence for a queue with nothing in it."""
    return "The queue is empty."


def queue_share(queue, limit=SHARE_LIMIT, title_max=SHARE_TITLE_MAX):
    """The host's play queue as the session may see it: `[{title, by}]`.

    `by` comes from the same reader as everything else (`requested_by`), so a
    listener's list and the host's own Play Queue menu can never disagree
    about whose song a track is. A track with no title is not shared (there is
    nothing to read), and the list is capped: this is somebody else's menu.
    """
    items = []
    for track in queue or ():
        if not isinstance(track, dict):
            continue
        title = " ".join(str(track.get("title") or "").split())
        if not title:
            continue
        items.append({
            "title": title[:title_max],
            "by": requested_by(track),
        })
        if len(items) >= limit:
            break
    return items


def queue_lines(items, now_playing=""):
    """The lines of a queue menu, in the order either menu shows them.

    The playing song first (when there is one), then every waiting track in
    the order it will play, saying whose request it is; an empty queue is a
    sentence rather than a blank menu. This is the host's own Play Queue menu
    as much as a listener's read-only view -- the two differ in their ends
    (Clear Queue / Close) and in nothing a line says.
    """
    lines = []
    head = now_playing_line(now_playing)
    if head:
        lines.append(head)
    shown = 0
    for item in items or ():
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        if not title:
            continue
        shown += 1
        lines.append(track_line(shown, title, item.get("by")))
    if not lines:
        lines.append(empty_line())
    return lines
