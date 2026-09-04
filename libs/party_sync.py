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
# S2C events this module parses (arrive on CHANNEL_MISC):
#   party_sync_invite_request, party_sync_state, party_sync_joined,
#   party_sync_kicked, party_sync_ended, party_sync_roster_change,
#   party_sync_player_list

import time

MAX_NAME_LEN = 32
MAX_SESSION_ID_LEN = 96
MAX_LIST = 64


def same_player(a, b):
    """Case-insensitive username comparison (matches the server's key)."""
    return str(a or "").strip().lower() == str(b or "").strip().lower()


def _text(value, limit=MAX_NAME_LEN):
    if not isinstance(value, str):
        return ""
    return value.strip()[:limit]


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
    }


def parse_player_list(data):
    """party_sync_player_list payload -> list of inviteable usernames."""
    if not isinstance(data, dict):
        return []
    raw = data.get("players")
    if not isinstance(raw, list):
        return []
    out = []
    for p in raw[:MAX_LIST]:
        if not isinstance(p, dict):
            continue
        name = _text(p.get("name"))
        if name and name not in out:
            out.append(name)
    return out


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
