# Party Sync — client helpers for private "listen together" sessions.
#
# The server (libs/party_sync.ts) is authoritative: it runs the session
# state machine, re-validates every invite/accept/decline step and gates the
# host's music relay so only session guests receive it. This module only
# parses the server's S2C payloads into validated client state, decides
# whether the local music bot must upload its stream (host in a session) and
# provides role helpers for the menus/prompts. It is deliberately free of
# pygame/game imports so the parsing and broadcast rules are unit-testable.
#
# C2S events the client may send:
#   party_sync_start / party_sync_end          (host, no data)
#   party_sync_invite {name} / party_sync_kick {name}
#   party_sync_accept / party_sync_decline     (invitee)
#   party_sync_leave / party_sync_list         (member / host)
#   party_sync_song_request {query}            (guest: /m <song> asks the host)
#   party_sync_song_requests {open}            (host: the request switch)
#   party_sync_song_result {to, request_id, ok, title|reason, waiting}
#                                              (host: what happened to one ask)
#   party_sync_song_choices {to, request_id, query, items:[{title, duration}]}
#                                        (host: the results, for the asker to
#                                         choose one of -- names only)
#   party_sync_song_pick {request_id, index}
#                               (asker: which result to queue, or -1 to
#                                withdraw; resolved against the host's own list)
#   party_sync_queue {now_playing, items:[{title, by}]}
#                               (host: relays its play queue to the session)
# S2C events this module parses (arrive on CHANNEL_MISC):
#   party_sync_invite_request, party_sync_state, party_sync_joined,
#   party_sync_kicked, party_sync_ended, party_sync_roster_change,
#   party_sync_player_list, party_sync_song_request (to the host),
#   party_sync_song_choices (to the asker), party_sync_song_pick (to the host),
#   party_sync_queue (to every listener; also carried on the state)

import time

MAX_NAME_LEN = 32
MAX_SESSION_ID_LEN = 96
MAX_LIST = 64
#: Longest accepted song-request query (the server's own cap is the same).
MAX_SONG_QUERY_LEN = 100
#: Longest request id the host echoes back with a result.
MAX_REQUEST_ID_LEN = 32
#: How many upcoming tracks a shared queue carries (the host caps it too).
MAX_QUEUE_ITEMS = 12
#: Longest shared queue title (the play-head title and every waiting one).
MAX_QUEUE_TITLE_LEN = 120
#: The slash forms a Party Sync room reads as "ask the host for this song".
#: `/m` is the one the menus and the notes tell players to type; `/p` (the
#: first spelling) and `/play` stay: a command this client once advertised must
#: keep working -- an alias nobody honours is worse than a long one, because an
#: unrecognized slash line is not refused, it is SAID IN THE ROOM as chat.
SONG_REQUEST_COMMANDS = ("/m", "/p", "/play")
#: How many candidates a picker may show (the host's search offers 5).
MAX_CHOICES = 8
#: Longest candidate title that travels.
MAX_CHOICE_TITLE_LEN = 120


def same_player(a, b):
    """Case-insensitive username comparison (matches the server's key)."""
    return str(a or "").strip().lower() == str(b or "").strip().lower()


def _text(value, limit=MAX_NAME_LEN):
    if not isinstance(value, str):
        return ""
    return value.strip()[:limit]


def _one_line(value, limit):
    """One line of host text, collapsed and capped.

    A shared queue is drawn in somebody else's menu: a title pasted with
    newlines in it must not be able to make that menu look like several
    entries, so this is the only shape a shared title takes.
    """
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())[:limit]


def _int(value, default, lo=None, hi=None):
    if isinstance(value, bool) or not isinstance(value, int):
        return default
    if lo is not None and value < lo:
        return default
    if hi is not None and value > hi:
        return default
    return value


def parse_invite_request(data):
    """Validate a party_sync_invite_request payload -> dict or None."""
    if not isinstance(data, dict):
        return None
    session_id = _text(data.get("session_id"), MAX_SESSION_ID_LEN)
    host_name = _text(data.get("host_name"))
    if not session_id or not host_name:
        return None
    voice = _int(data.get("host_voice_channel"), None, 0, 255)
    expires_ms = data.get("expires_ms")
    if isinstance(expires_ms, bool) or not isinstance(expires_ms, (int, float)):
        expires_ms = 30000
    expires_ms = min(max(int(expires_ms), 1000), 120000)
    return {
        "session_id": session_id,
        "host_name": host_name,
        "host_voice_channel": voice,
        "expires_ms": expires_ms,
    }


def parse_session_event(data):
    """party_sync_joined / kicked / ended payloads -> dict or None."""
    if not isinstance(data, dict):
        return None
    session_id = _text(data.get("session_id"), MAX_SESSION_ID_LEN)
    host_name = _text(data.get("host_name"))
    if not session_id or not host_name:
        return None
    return {"session_id": session_id, "host_name": host_name}


def parse_state(data):
    """party_sync_state payload -> normalized dict or None.

    Host/guest names are usernames (server-side identity). Only members
    receive state, so the client derives its role by comparing the host name
    with its own account name.
    """
    if not isinstance(data, dict):
        return None
    if data.get("status") not in (None, "active"):
        return None
    session_id = _text(data.get("session_id"), MAX_SESSION_ID_LEN)
    host = data.get("host")
    if not session_id or not isinstance(host, dict):
        return None
    host_name = _text(host.get("name"))
    if not host_name:
        return None
    host_voice = _int(host.get("voice_channel"), None, 0, 255)
    guests = []
    raw_guests = data.get("guests")
    if isinstance(raw_guests, list):
        for g in raw_guests[:MAX_LIST]:
            if not isinstance(g, dict):
                continue
            name = _text(g.get("name"))
            if not name:
                continue
            guests.append({
                "name": name,
                "voice_channel": _int(g.get("voice_channel"), None, 0, 255),
            })
    max_guests = _int(data.get("max_guests"), 8, 1, 32)
    return {
        "session_id": session_id,
        "host_name": host_name,
        "host_voice_channel": host_voice,
        "guests": guests,
        "max_guests": max_guests,
        # Whether this session's host is taking song requests (/p). Off until
        # the host turns it on, and carried on every state push so a guest can
        # see it (`Song requests: open/closed`) instead of guessing.
        "song_requests": data.get("song_requests") is True,
        # The host's play queue as the host last shared it. It rides the state
        # push as well as its own event, which is what lets somebody who joined
        # after the last change read the queue without waiting for one.
        "now_playing": _one_line(data.get("now_playing"), MAX_QUEUE_TITLE_LEN),
        "queue": _parse_queue_items(data.get("queue")),
    }


def _parse_queue_items(raw):
    """The `queue` array of a state push or party_sync_queue payload."""
    items = []
    if not isinstance(raw, list):
        return items
    for entry in raw[:MAX_QUEUE_ITEMS]:
        if not isinstance(entry, dict):
            continue
        title = _one_line(entry.get("title"), MAX_QUEUE_TITLE_LEN)
        if not title:
            continue
        items.append({
            "title": title,
            "by": _one_line(entry.get("by"), MAX_NAME_LEN),
        })
    return items


def parse_queue(data):
    """party_sync_queue payload -> `{now_playing, items:[{title, by}]}`.

    Read-only data on its way to a listener's menu, so everything is bounded
    and one line. A malformed payload reads as an empty queue rather than as
    somebody else's queue, which is the only safe direction: the next push
    (the host repeats while the queue has anything in it) corrects it.
    """
    if not isinstance(data, dict):
        return {"now_playing": "", "items": []}
    return {
        "now_playing": _one_line(data.get("now_playing"), MAX_QUEUE_TITLE_LEN),
        "items": _parse_queue_items(data.get("items")),
    }


def clean_song_query(text):
    """Normalize a song request: one line, trimmed, capped.

    Collapses whitespace (a pasted YouTube title arrives with newlines and runs
    of spaces, which only make the search worse) and returns "" when nothing is
    left to search for. The server cleans the query the same way, so a client
    and the server can never disagree about what was asked for.
    """
    if not isinstance(text, str):
        return ""
    return " ".join(text.split())[:MAX_SONG_QUERY_LEN].strip()


def parse_chat_request(message):
    """The song asked for on a party chat line, or None.

    `/p` and `/play` (case-insensitive) are read as a request *inside a Party
    Sync room only*: party chat is plain text and never fires a server command,
    so this is the one place a slash means something (see
    Gameplay.party_sync_chat2). Returns:

      * a string -> the query to request
      * ""       -> the command with nothing after it (usage hint)
      * None     -> an ordinary chat message, leave it alone
    """
    if not isinstance(message, str):
        return None
    text = message.strip()
    if not text.startswith("/"):
        return None
    parts = text.split(None, 1)
    if parts[0].lower() not in SONG_REQUEST_COMMANDS:
        return None
    return clean_song_query(parts[1]) if len(parts) > 1 else ""


#: The line a mistyped request gets instead of being said in the room.
NEAR_COMMAND_HINT = ("Nothing was sent: {word} is not a command in a party "
                     "room. To ask for a song, type /m followed by the song "
                     "name.")
#: Longest mistyped word echoed back (it is somebody's typing, not a title).
MAX_NEAR_WORD_LEN = 24


def _within_one_edit(a, b):
    """True when `a` is at most one insert/delete/replace away from `b`."""
    if abs(len(a) - len(b)) > 1:
        return False
    if a == b:
        return True
    if len(a) > len(b):
        a, b = b, a
    i = j = 0
    edited = False
    while i < len(a) and j < len(b):
        if a[i] == b[j]:
            i += 1
            j += 1
            continue
        if edited:
            return False
        edited = True
        # Same length: this is a replacement. One longer: `b` has an extra
        # character here, so only `b` moves on.
        if len(a) == len(b):
            i += 1
        j += 1
    return True


def _collapse_runs(word):
    """`/mmm` and `/playy` as a finger held on one key, not a new command."""
    out = []
    for ch in word:
        if not out or out[-1] != ch:
            out.append(ch)
    return "".join(out)


def near_song_command(message):
    """A hint when a party chat line looks like a mistyped request, or None.

    A slash line this room does not understand is **said in the room as chat**
    (party chat is plain text and never fires a server command), so a typo is
    not a missing feature -- it is the request announced to everybody as
    somebody's message. Only a line one edit away from a request command earns
    a hint (plus a held key: `/mm`), and it is spoken instead of the line, so
    nothing reaches the room. Everything else -- `/help`, `/who`, any real
    slash line somebody means to say -- stays ordinary chat: guessing at a
    message is worse than sending it, and only the request is a command here.
    """
    if not isinstance(message, str):
        return None
    text = message.strip()
    if not text.startswith("/"):
        return None
    word = text.split(None, 1)[0]
    low = word.lower()
    if low in SONG_REQUEST_COMMANDS or len(word) < 2:
        return None
    collapsed = _collapse_runs(low)
    for command in SONG_REQUEST_COMMANDS:
        if _within_one_edit(low, command) or _within_one_edit(collapsed, command):
            return NEAR_COMMAND_HINT.format(word=word[:MAX_NEAR_WORD_LEN])
    return None


def parse_song_request(data):
    """party_sync_song_request (relayed TO THE HOST) -> dict or None.

    `from` is the requester's name as the server knows it (never taken from a
    client) and `request_id` is what the host echoes back with the result, so
    {from, query, request_id} is the whole contract.
    """
    if not isinstance(data, dict):
        return None
    sender = _text(data.get("from"))
    request_id = _text(data.get("request_id"), MAX_REQUEST_ID_LEN)
    query = clean_song_query(data.get("query"))
    if not sender or not query or not request_id:
        return None
    return {"from": sender, "query": query, "request_id": request_id}


def parse_song_choices(data):
    """party_sync_song_choices (relayed TO THE ASKER) -> dict or None.

    The host's search results, as names to choose from: `{request_id, query,
    items:[{title, duration}]}`. Only the asker ever sees this, and only names
    and durations travel -- the URLs stay on the host that will play them.
    """
    if not isinstance(data, dict):
        return None
    request_id = _text(data.get("request_id"), MAX_REQUEST_ID_LEN)
    items = []
    raw = data.get("items")
    for item in (raw if isinstance(raw, list) else [])[:MAX_CHOICES]:
        if not isinstance(item, dict):
            continue
        title = _one_line(item.get("title"), MAX_CHOICE_TITLE_LEN)
        if not title:
            continue
        try:
            seconds = int(float(item.get("duration") or 0))
        except (TypeError, ValueError):
            seconds = 0
        if seconds < 0 or seconds > 24 * 3600:
            seconds = 0
        items.append({"title": title, "duration": seconds})
    if not request_id or not items:
        return None
    return {"request_id": request_id,
            "query": clean_song_query(data.get("query")),
            "items": items}


def parse_song_pick(data):
    """party_sync_song_pick (relayed TO THE HOST) -> dict or None.

    `from` is the picker's name as the server knows it, and `index` is the
    place in the list the host itself offered -- `-1` means "nothing, thanks".
    A pick never carries a title or a URL: the host resolves the index against
    its own results, so the only thing a client can choose is one of the songs
    that were actually offered.
    """
    if not isinstance(data, dict):
        return None
    sender = _text(data.get("from"))
    request_id = _text(data.get("request_id"), MAX_REQUEST_ID_LEN)
    try:
        index = int(data.get("index"))
    except (TypeError, ValueError):
        return None
    if not sender or not request_id or index < -1 or index > MAX_CHOICES:
        return None
    return {"from": sender, "request_id": request_id, "index": index}


def parse_player_list_entries(data):
    """party_sync_player_list payload -> [{"name", "near"}].

    A Party Sync session is not map-scoped, so the invite menu lists every
    player online, the host's own map first. `near` is the server's own answer
    to "are they standing on this map", carried here so the menu can say which
    is which — two names from different maps are otherwise identical lines.
    Deduplicated and capped exactly like the old name-only list, and a payload
    that predates the field reads as `near` (the same map), which is what it
    was.
    """
    if not isinstance(data, dict):
        return []
    raw = data.get("players")
    if not isinstance(raw, list):
        return []
    out = []
    seen = set()
    for p in raw[:MAX_LIST]:
        if not isinstance(p, dict):
            continue
        name = _text(p.get("name"))
        if not name or name in seen:
            continue
        seen.add(name)
        near = p.get("near")
        out.append({"name": name, "near": True if near is None else bool(near)})
    return out


def parse_player_list(data):
    """party_sync_player_list payload -> list of inviteable usernames."""
    return [entry["name"] for entry in parse_player_list_entries(data)]


def upload_should_send(bot):
    """Whether the music bot stream must be uploaded right now.

    True when the user enabled public broadcast, routes to the megaphone, OR
    hosts an active Party Sync session (the server then narrows recipients to
    the session guests, so this upload is private by construction).
    """
    if bot is None:
        return False
    return bool(
        getattr(bot, "broadcast_enabled", False)
        or getattr(bot, "broadcast_to_megaphone", False)
        or getattr(bot, "party_sync_force_upload", False)
    )


def stereo_upload_eligible(bot, channels=2, live_input_pending=False):
    """Whether the music-bot upload may carry true stereo this frame.

    Only the private Party Sync leg is allowed to send stereo: the host
    uploads while its session is active, never through the (mono) PA
    megaphone path, the decode must actually be two channels, and no
    live-input (mic/guitar) mix is waiting (that mix is built in mono).
    """
    if bot is None:
        return False
    return bool(
        getattr(bot, "party_sync_force_upload", False)
        and not getattr(bot, "broadcast_to_megaphone", False)
        and channels == 2
        and not live_input_pending
    )


def set_direct_mode(entity, gain=None):
    """Turn an entity's music source into a direct (non-positional) feed.

    Used on the Party Sync GUEST side: the host's stream must reach the
    guest as clear "headphones" audio at any distance instead of a 3D
    boombox placed at the host's position. Only touches the music source;
    the voice source stays fully positional. Returns False when the entity
    has no music source yet (retry on the next state refresh).
    """
    src = getattr(entity, "music_source", None)
    if src is None:
        return False
    if getattr(entity, "_party_sync_direct", False):
        return True
    entity._party_sync_direct = True
    entity._party_sync_direct_gain = max(
        0.0, float(gain) if gain is not None else 1.0
    )
    try:
        entity._party_sync_direct_restore = (
            src.spatialize, src.relative, src.direct_channels,
        )
    except Exception:
        entity._party_sync_direct_restore = None
    # Mirror the host's own Music Bot source exactly (see
    # MapMusicBot._create_stream_source: direct_channels + spatialize off):
    # the guest must hear the same clean two-channel feed, not a source that
    # still passes through HRTF/panning and therefore sounds placed "in front"
    # or off to one side. Each property applies on its own so one unsupported
    # extension cannot leave the source half-configured.
    for apply_ in (
        lambda: setattr(src, "spatialize", False),
        lambda: setattr(src, "relative", True),
        lambda: setattr(src, "position", (0.0, 0.0, 0.0)),
        lambda: setattr(src, "direct_channels", True),
    ):
        try:
            apply_()
        except Exception:
            pass
    try:
        # Initial level; entity.loop/move re-apply the guest's live Music
        # slider value every frame from here on.
        src.gain = entity._party_sync_direct_gain
    except Exception:
        pass
    return True


def clear_direct_mode(entity):
    """Restore positional behavior for an entity put into direct mode."""
    src = getattr(entity, "music_source", None)
    entity._party_sync_direct = False
    restore = getattr(entity, "_party_sync_direct_restore", None)
    entity._party_sync_direct_restore = None
    if src is not None and restore is not None:
        try:
            src.spatialize, src.relative, src.direct_channels = restore
        except Exception:
            try:
                src.spatialize, src.relative = restore[:2]
            except Exception:
                pass


def clear_all_party_direct(gameplay):
    """Restore every direct-to-ear source (music + voice) on all entities.

    Used when a session ends locally through a menu/key path that does not
    wait for a server state refresh (host ending from the Music Bot menu,
    a guest pressing the leave key). Safe to call any time.
    """
    vc = getattr(gameplay, "voice_channels", None) or {}
    for e in vc.values():
        if getattr(e, "_party_sync_direct", False):
            clear_direct_mode(e)
        if getattr(e, "_party_sync_voice_direct", False):
            clear_voice_direct_mode(e)


def party_member_channels(state):
    """Set of voice-channel ids of every session member (host + guests).

    Channel ids are the exact keys the client's `voice_channels` map uses,
    so enabling direct mode by channel is exact — no display-name matching.
    Empty when no session / no members yet.
    """
    chans = set()
    if state is None:
        return chans
    hc = getattr(state, "host_voice_channel", None)
    if hc is not None:
        try:
            chans.add(int(hc))
        except Exception:
            pass
    for g in (getattr(state, "guests", None) or []):
        if not isinstance(g, dict):
            continue
        vc = g.get("voice_channel")
        if vc is None:
            continue
        try:
            chans.add(int(vc))
        except Exception:
            pass
    return chans


def set_voice_direct_mode(entity, gain=None):
    """Turn an entity's VOICE source into a direct (non-positional) feed.

    Party Sync "team talk": while a session is active each member's voice
    chat is delivered straight into the other members' ears (like the music
    direct feed) instead of as a 3D world voice that fades with distance.
    Uses the same source flags as the music direct mode; the music source is
    untouched. Returns False when the entity has no vc_source yet (caller
    retries on the next sync / incoming frame).
    """
    src = getattr(entity, "vc_source", None)
    if src is None:
        return False
    if getattr(entity, "_party_sync_voice_direct", False):
        return True
    entity._party_sync_voice_direct = True
    try:
        entity._party_sync_voice_direct_restore = (
            src.spatialize, src.relative, src.direct_channels,
        )
    except Exception:
        entity._party_sync_voice_direct_restore = None
    for apply_ in (
        lambda: setattr(src, "spatialize", False),
        lambda: setattr(src, "relative", True),
        lambda: setattr(src, "position", (0.0, 0.0, 0.0)),
        lambda: setattr(src, "direct_channels", True),
    ):
        try:
            apply_()
        except Exception:
            pass
    try:
        if gain is not None:
            src.gain = max(0.0, float(gain))
    except Exception:
        pass
    return True


def clear_voice_direct_mode(entity):
    """Restore positional voice behavior for a party member's entity."""
    src = getattr(entity, "vc_source", None)
    entity._party_sync_voice_direct = False
    restore = getattr(entity, "_party_sync_voice_direct_restore", None)
    entity._party_sync_voice_direct_restore = None
    if src is not None and restore is not None:
        try:
            src.spatialize, src.relative, src.direct_channels = restore
        except Exception:
            try:
                src.spatialize, src.relative = restore[:2]
            except Exception:
                pass


class PartySyncState:
    """Client mirror of the server session (best effort; server is truth)."""

    def __init__(self):
        self.reset()

    def reset(self):
        self.role = None  # "host" | "guest" | None
        self.session_id = ""
        self.host_name = ""
        self.host_voice_channel = None
        self.guests = []  # list of {"name", "voice_channel"}
        self.max_guests = 8
        self.pending = None  # validated invite_request + expires_at
        self.invite_players = []  # latest host invite-candidate list
        # Whether the host is taking song requests in this session (the
        # server's own answer, relayed on every state push).
        self.song_requests = False
        # The host's play queue, as the host relays it (what is playing and
        # what is waiting). Read-only, and empty until the host says
        # otherwise.
        self.now_playing = ""
        self.queue = []

    # ── pending invite ──────────────────────────────────────────────
    def set_pending(self, invite):
        self.pending = {
            "session_id": invite["session_id"],
            "host_name": invite["host_name"],
            "host_voice_channel": invite["host_voice_channel"],
            "expires_at": time.monotonic() + invite["expires_ms"] / 1000.0,
        }

    def pending_valid(self):
        p = self.pending
        if not p:
            return False
        if time.monotonic() >= p.get("expires_at", 0):
            self.pending = None
            return False
        return True

    def clear_pending(self):
        self.pending = None

    # ── session membership ──────────────────────────────────────────
    def is_host(self, self_name):
        return self.role == "host" or (
            bool(self.host_name) and same_player(self.host_name, self_name)
        )

    def apply_state(self, payload, self_name):
        """Apply party_sync_state; returns the role string or None."""
        parsed = parse_state(payload)
        if parsed is None:
            return None
        self.session_id = parsed["session_id"]
        self.host_name = parsed["host_name"]
        self.host_voice_channel = parsed["host_voice_channel"]
        self.guests = parsed["guests"]
        self.max_guests = parsed["max_guests"]
        self.song_requests = parsed["song_requests"]
        self.now_playing = parsed["now_playing"]
        self.queue = parsed["queue"]
        self.role = "host" if same_player(parsed["host_name"], self_name) else "guest"
        return self.role

    def start_session(self, payload, self_name):
        """Host starts: seed from the authoritative state push."""
        return self.apply_state(payload, self_name)

    def end_session(self):
        """Session over for this client (ended/kicked/left)."""
        self.role = None
        self.session_id = ""
        self.host_name = ""
        self.host_voice_channel = None
        self.guests = []
        self.song_requests = False
        self.now_playing = ""
        self.queue = []
        self.clear_pending()


def session_roster(state):
    """[(name, role)] of everyone in the session: host first, then guests.

    Used by the quick leave menu (Ctrl+F8) so the player sees who is
    listening before deciding to leave/end. Invalid entries are skipped;
    an empty list means the session has no usable roster.
    """
    roster = []
    host = _text(getattr(state, "host_name", "") or "")
    if host:
        roster.append((host, "host"))
    guests = getattr(state, "guests", None) or []
    for g in guests:
        if not isinstance(g, dict):
            continue
        name = _text(g.get("name", "") or "")
        if not name or (host and same_player(name, host)):
            continue
        roster.append((name, "guest"))
    return roster
