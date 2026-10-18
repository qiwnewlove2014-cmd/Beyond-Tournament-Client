"""What the panel knows, read in one place.

These readers all answer a question about gp state (the cabinet's cached volume, repeat
mode, EQ values, the needle's position, which cabinet is nearest) and none of them plays
anything. Keeping them together means a menu never reaches into the panel itself."""

import time

from .player import JukeboxPlayer


def _current_state(gp):
    """The last jukebox state payload received from the server."""
    state = getattr(gp, "jukebox_state", None)
    return state if isinstance(state, dict) else {"jukeboxes": {}}


def _own_name(gp):
    # This client's own player name, or "" when login has not set it yet. The lock's owner
    # is a *name*, so the one client that may manage it without asking its own permission is
    # the one whose name matches -- an empty name matches nobody, which is the honest answer
    # before login.
    player = getattr(gp, "player", None)
    return str(getattr(player, "name", "") or "")


def _closest_jukebox(gp):
    """Pick the jukebox nearest to the player from the cached server state."""
    state = _current_state(gp)
    boxes = state.get("jukeboxes", {})
    if not boxes:
        return None
    px = getattr(getattr(gp, "player", None), "x", 0) or 0
    py = getattr(getattr(gp, "player", None), "y", 0) or 0
    pz = getattr(getattr(gp, "player", None), "z", 0) or 0
    best = None
    best_dist = None
    for jid, box in boxes.items():
        bx = box.get("x", 0)
        by = box.get("y", 0)
        bz = box.get("z", 0)
        d = (px - bx) ** 2 + (py - by) ** 2 + (pz - bz) ** 2
        if best_dist is None or d < best_dist:
            best_dist = d
            best = box
    return best


def _is_jukebox_paused(gp, jukebox_id):
    """Return the authoritative paused flag cached for one jukebox."""
    box = _current_state(gp).get("jukeboxes", {}).get(jukebox_id) or {}
    return bool(box.get("paused", False))


def _pause_menu_label(gp, jukebox_id):
    """Accessible dynamic label for the shared Pause / Resume action."""
    return "Resume playback" if _is_jukebox_paused(gp, jukebox_id) else "Pause playback"


def _position_label(seconds):
    # A needle as m:ss for speech. Imported inside the call rather than at module load:
    # reaching into a music_bot submodule executes its package __init__, which imports the
    # controller -- and the controller imports this module.
    from ..music_bot.media import format_track_position
    return format_track_position(seconds)


def _playing_needle(gp, jukebox_id):
    # Where the song is in *this* machine's ears, or None when this machine is not the one playing
    # it: the direct-fallback anchor (position plus elapsed) is the number a scrubbing listener
    # points at - the song as they hear it, not as another machine's clock counts it.
    player = getattr(gp, "jukebox_player", None)
    entries = getattr(player, "players", None)
    entry = entries.get(jukebox_id) if isinstance(entries, dict) else None
    params = (entry or {}).get("play_params") or {}
    received_at = params.get("received_at")
    if received_at is None:
        return None
    try:
        return float(params.get("start_offset") or 0.0) + max(
            0.0, time.monotonic() - float(received_at)
        )
    except (TypeError, ValueError):
        return None


def _scrub_spot(gp, jukebox_id):
    # What there is to scrub here: (needle, duration), or None. The length comes from the cached
    # song (no length = a livestream, nothing to scrub); the needle is this machine's own audible
    # position while it plays here, else the Server's own number from the pause packet/map state.
    box = (_current_state(gp).get("jukeboxes", {}) or {}).get(jukebox_id) or {}
    current = box.get("current") or {}
    try:
        duration = int(current.get("duration") or 0)
    except (TypeError, ValueError):
        duration = 0
    if duration <= 0:
        return None
    if not _is_jukebox_paused(gp, jukebox_id):
        needle = _playing_needle(gp, jukebox_id)
        if needle is not None:
            return needle, duration
    try:
        return float(box.get("position") or 0.0), duration
    except (TypeError, ValueError):
        return 0.0, duration


def _scrub_menu_label(gp, jukebox_id, spot=None):
    # The Scrub line's label: the read-out of a needle at rest. ``spot`` overrides the read for
    # a control that is holding a scrub of its own: while the room is frozen nothing else can
    # know where the needle is -- the Server is told about a move only now and then -- so the
    # line keeps reading the number the arrows just changed.
    if spot is None:
        spot = _scrub_spot(gp, jukebox_id)
    if spot is None:
        return "Scrub playback"
    needle, duration = spot
    return (f"Scrub playback (now: {_position_label(needle)} of "
            f"{_position_label(duration)})")


def _get_repeat_mode(gp, jukebox_id):
    """The cached server repeat mode for one jukebox (off / one / all)."""
    box = _current_state(gp).get("jukeboxes", {}).get(jukebox_id) or {}
    mode = box.get("repeat") or "off"
    return mode if mode in ("off", "one", "all") else "off"


def _get_eq_profile(gp, jukebox_id):
    """The cached server EQ sound profile for one jukebox."""
    box = _current_state(gp).get("jukeboxes", {}).get(jukebox_id) or {}
    profile = str(box.get("eq_profile") or "normal").lower()
    return profile if profile in ("normal", "bass_boost", "custom") else "normal"


def _get_eq_values(gp, jukebox_id):
    """Return the three accessible 0-100 EQ values for one cabinet."""
    box = _current_state(gp).get("jukeboxes", {}).get(jukebox_id) or {}
    profile = _get_eq_profile(gp, jukebox_id)
    if profile == "bass_boost":
        return {"bass": 100, "mid": 50, "treble": 50}
    if profile != "custom":
        return {"bass": 50, "mid": 50, "treble": 50}
    return JukeboxPlayer._normalize_eq_values(box.get("eq_values"))


def _get_jukebox_volume(gp, jukebox_id):
    """The cached server volume for one jukebox (0-100)."""
    box = _current_state(gp).get("jukeboxes", {}).get(jukebox_id) or {}
    try:
        vol = int(box.get("volume", 100))
        return max(0, min(100, vol))
    except Exception:
        return 100


def _get_or_create_jukebox_player(game, gp):
    player = getattr(gp, "jukebox_player", None)
    if player is None:
        player = gp.jukebox_player = JukeboxPlayer(game)
    return player
