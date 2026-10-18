"""What a room is called, what it is missing, and how a builder picks it.

The sentences here are what a blind builder hears about a cabinet's room: which speakers
are silent, which bands are missing, how far the room reaches. The three menus under them
(mode, reach, the lock) apply the answer through the server, so the room is the map's
decision and never a local one."""

import functools

from ..audio.cinema import (CINEMA_AUTO, CINEMA_CUSTOM, CINEMA_OFF,
                            ROOM_MAX_DISTANCE, cabinet_anchor, cabinet_area,
                            cabinet_reach, neighbour_note, profile_names,
                            room_diagnosis, room_extent, room_plan)
from ..audio.cinema.crossover import FULL_RANGE as CROSSOVER_FULL_RANGE
from ..audio.cinema.crossover import band_edges, describe as crossover_band
from ..audio.cinema.crossover import hz_label, mark_of
from ..speech import speak

from .constants import CINEMA_PROFILES
from .view import _current_state, _own_name


def _cabinet_cinema_lock(gp, jukebox_id):
    # The lock on a cabinet's cinema mode, from the cached server state: (owner, coded) or
    # None. The Server never sends the code itself -- only who holds the lock and whether a
    # code exists -- so nothing here can leak one. The cabinet menu uses it to say who to
    # ask and to warn that a pick will need the code before it is applied.
    box = (_current_state(gp).get("jukeboxes", {}) or {}).get(jukebox_id) or {}
    lock = box.get("cinema_lock")
    if not isinstance(lock, dict):
        return None
    owner = str(lock.get("owner") or "").strip()
    if not owner:
        return None
    return owner, bool(lock.get("coded", True))


def _may_lock_cinema_mode(gp):
    # Whether this account may lock a cabinet's mode: read from the login/permission snapshot
    # (can_lock_cinema_mode), never guessed from a staff flag - a menu line that exists must work.
    return bool(getattr(gp, "can_lock_cinema_mode", False))


def _cabinet_cinema_mode(gp, jukebox_id):
    # The mode the map has for a cabinet, from the cached server state. An unknown value
    # reads as ``auto``, the same rule the playing path uses: a cabinet whose mode nobody
    # recognises must behave like every other cabinet rather than go silent.
    state = _current_state(gp)
    box = (state.get("jukeboxes", {}) or {}).get(jukebox_id) or {}
    raw = str(box.get("cinema_mode") or box.get("cinema_profile") or "").strip().lower()
    if raw == "":
        return CINEMA_AUTO
    if raw in (CINEMA_AUTO, CINEMA_OFF) or raw in CINEMA_PROFILES:
        return raw
    return CINEMA_AUTO


CINEMA_REACHES = (
    (20, "Booth or small room"),
    (60, "Room - the default every room has had"),
    (90, "Hall"),
    (140, "Wide stage or outdoor"),
)


def _cinema_reach_label(reach):
    """A reach as a menu line: the number first, then what it is for."""
    value = float(reach)
    for choice, description in CINEMA_REACHES:
        if abs(value - choice) < 0.5:
            return f"{choice} m - {description}"
    return f"{value:.0f} m - set by the map"


def _cabinet_cinema_reach(gp, jukebox_id):
    # The reach the map has for a cabinet, asked of the one reader (plugin.cabinet_reach): a menu
    # that read the state itself could say 60 m while the room was really built at 90 m.
    return cabinet_reach(gp, jukebox_id, default=float(ROOM_MAX_DISTANCE))


def _cinema_mode_label(mode):
    """Short form of a mode, for a menu line."""
    if mode == CINEMA_OFF:
        return "Off - the cabinet's own stereo"
    if mode == CINEMA_AUTO:
        return "Auto - the speakers around it, if any"
    if mode == CINEMA_CUSTOM:
        return "Custom - exactly the speakers placed for it"
    return f"{mode} - that room shape, speakers or not"


def _cabinet_title(game, gp, jukebox_id, mode=None):
    # The cabinet's own name: a plain jukebox, or the mode it is playing. "Music Jukebox" is the
    # wrong sentence for a cabinet the map has given a room, and this line is the only thing that
    # tells them apart in a menu. The room is *previewed* with the same resolver the read-out and
    # the playback use (room_plan), so the name can never promise a room that would not play. The
    # tail is the mode token itself (front_only, theatre, auto), never a description of the
    # speakers - a title answering "Front speakers only" would name one thing two ways beside its
    # own "Set cinema mode" line. The listener's own ``Cinema rooms:`` switch is not part of it.
    mode = mode or _cabinet_cinema_mode(gp, jukebox_id)
    if mode == CINEMA_OFF:
        return "Music Jukebox"
    anchor = cabinet_anchor(game, jukebox_id) if game is not None else None
    if anchor is None:
        return "Music Jukebox"
    try:
        plan, _silent = room_plan(game, anchor, requested=mode, room_id=jukebox_id)
    except Exception:
        # A name is read out the moment a menu opens: nothing a map could hold
        # is worth breaking that for (same rule as ``_room_extent``).
        return "Music Jukebox"
    if plan is None:
        return "Music Jukebox"
    return f"Cinema Jukebox: {mode}"


def _cinema_detail(game, gp, jukebox_id, mode=None):
    # The full answer to "what is this cabinet playing through", and why. The room is *previewed*,
    # never acquired: saying what the map could do must not create a speaker or change playback.
    # It ends with the neighbours - two cabinets close enough to share speakers is exactly why a
    # speaker here can be somebody else's.
    return (_cinema_room_sentence(game, gp, jukebox_id, mode)
            + neighbour_note(game, jukebox_id))


def _cinema_room_sentence(game, gp, jukebox_id, mode=None):
    """What this cabinet's own room is, said without its neighbours."""
    mode = mode or _cabinet_cinema_mode(gp, jukebox_id)
    reach = _cinema_reach_note(game, gp, jukebox_id)
    if mode == CINEMA_OFF:
        return ("This cabinet plays its own stereo: the map turned its cinema "
                "room off." + reach)
    if mode == CINEMA_CUSTOM:
        anchor = cabinet_anchor(game, jukebox_id)
        if anchor is None:
            return ("This cabinet has no cinema room: it is not on the map (or "
                    "its position is not known yet).")
        plan, _silent = room_plan(game, anchor, requested=mode, room_id=jukebox_id)
        if plan is None or getattr(plan, "silent", False):
            return ("This cabinet plays exactly the speakers placed for it, and "
                    "none are placed yet, so it plays nothing: "
                    + room_diagnosis(game, anchor, room_id=jukebox_id) + reach)
        return (f"This cabinet plays exactly the speakers placed for it "
                f"({plan.summary()})." + _cinema_band_note(plan) + reach)
    anchor = cabinet_anchor(game, jukebox_id)
    if anchor is None:
        return ("This cabinet has no cinema room: it is not on the map (or its "
                "position is not known yet).")
    plan, silent = room_plan(game, anchor, requested=mode, room_id=jukebox_id)
    if plan is not None and plan.placement is not None:
        if mode == CINEMA_AUTO:
            return (f"Playing through the room speakers around this cabinet "
                    f"({plan.summary()})." + _cinema_band_note(plan) + reach)
        return (f"This cabinet plays as {mode}: {plan.summary()}. A shape "
                f"nobody placed around it is filled from the ring behind it."
                + _silent_speaker_note(silent, plan)
                + _cinema_band_note(plan) + reach)
    if mode != CINEMA_AUTO:
        # The shape outlives the map: it is played from the ring behind the
        # cabinet. Every speaker standing here is outside it (named when there
        # are any), and the diagnosis says why none of them made a room.
        return (f"This cabinet plays as {mode} from a ring of speakers behind "
                f"it, because the map has no room here: "
                f"{room_diagnosis(game, anchor, room_id=jukebox_id)}."
                + _silent_speaker_note(silent, plan) + reach)
    return (f"No cinema room here, so this cabinet plays its own stereo: "
            f"{room_diagnosis(game, anchor, room_id=jukebox_id)}." + reach)


def _cinema_reach_note(game, gp, jukebox_id):
    # The cabinet's reach and area, said where the room is described. Two answers to "which
    # speakers are mine": the reach is one number under two names, and the area is the map zone
    # the cabinet stands in - on a wide map with several halls it is the area, not the distance,
    # that stops two venues claiming each other's speakers. No zone = the distance alone.
    reach = _cabinet_cinema_reach(gp, jukebox_id)
    sentence = (f" Its reach is {reach:.0f} m: a speaker further than that belongs"
                f" to another room, and nothing here is heard past it either.")
    name, _bounds = cabinet_area(game, jukebox_id)
    if name:
        sentence += (f" It stands in the map's '{name}' area, so a speaker outside"
                     f" that area is another room's however close it is.")
    # ...and what that number is measured against, so "nothing is heard past it"
    # is answerable without a tape measure: the speakers somebody placed and
    # the room's own drawn edge, both from the readers the room is built with.
    extent = _room_extent(game, jukebox_id)
    if extent is not None:
        summary = extent.summary()
        if summary:
            sentence += f" Measured from the cabinet: {summary}"
    return sentence


def _room_extent(game, jukebox_id):
    # This cabinet's geometry, or None when its position is not known. room_extent is a
    # read-out that takes an anchor, and both callers here hold the cabinet's own anchor
    # already; None keeps a cabinet the map has not placed yet from answering with a room
    # measured from nowhere.
    anchor = cabinet_anchor(game, jukebox_id) if game is not None else None
    if anchor is None:
        return None
    try:
        return room_extent(game, jukebox_id, anchor)
    except Exception:
        return None


def _silent_speaker_note(silent, plan=None):
    # Name the speakers a room does not use, or say nothing when it uses all: a requested shape
    # names a fixed set of slots, so a speaker on a slot that shape does not have is silent with
    # nothing on any screen saying so. A shape with no room on the map at all is a different
    # sentence - the ring behind the cabinet is playing, so every speaker placed here goes unread.
    if not silent:
        return ""
    names = ", ".join(str(name or slot) for name, slot in silent)
    if plan is not None and getattr(plan, "placement", None) is None:
        return (f" None of the map's speakers here is fed: the room is the "
                f"ring behind the cabinet, so no crossover, tone, level or "
                f"delay placed on them is heard ({names})")
    return (f" Speakers this shape does not use are silent: {names}"
            f" ({len(silent)} of the speakers placed here); Auto plays"
            f" whatever stands around the cabinet.")


AUDIBLE_HZ = (20.0, 20000.0)


def _kept_span(mark):
    """The span of the range one speaker keeps, or None when it keeps all of it."""
    edges = band_edges(mark)
    if edges:
        return (float(edges[0]), float(edges[1]))
    if mark == CROSSOVER_FULL_RANGE:
        return None
    if mark < 0:
        return (abs(float(mark)), AUDIBLE_HZ[1])
    return (AUDIBLE_HZ[0], float(mark))


def _missing_spans(marks):
    # The spans of the range *no* speaker of this room keeps. Nothing else in the game looks at a
    # room's crossovers together, which is why a room can sound broken while every value on screen
    # looks deliberate: a sub below 120 and a tweeter above 1.5 kHz between them keep no middle.
    # One full-range speaker keeps everything, so the answer is empty (every map with no
    # crossover), and only the gaps BETWEEN the bands are reported. Returns [(low, high), ...] in Hz.
    spans = [_kept_span(mark) for mark in marks]
    if any(span is None for span in spans):
        return []
    spans = sorted(spans)
    missing = []
    reach = spans[0][1]
    for low, high in spans[1:]:
        if low > reach:
            missing.append((reach, low))
        reach = max(reach, high)
    return missing


def _missing_bands_note(marks):
    """The hole a room's own speakers leave, said as the span it falls in."""
    spans = _missing_spans(marks)
    if not spans:
        return ""
    said = [f"{hz_label(low)} to {hz_label(high)}" for low, high in spans]
    plural = "s" if len(said) > 1 else ""
    verb = "are" if len(said) > 1 else "is"
    return (f". The range{plural} {', '.join(said)} {verb} not heard here at all")


def _cinema_band_note(plan):
    # What each speaker this room feeds keeps: the bottom, the middle, the top, or all. A room
    # whose fed speakers are one sub and one tweeter keeps no middle at all - heard as a broken
    # room with every value on screen looking deliberate. A room of full-range speakers says
    # nothing; the bands are read together (_missing_spans) because the hole is what is heard.
    placement = getattr(plan, "placement", None)
    if placement is None:
        return ""
    parts = []
    marks = []
    for slot in getattr(placement, "slots", ()):
        spec = placement.speakers[slot].spec
        mark = mark_of(spec)
        band = crossover_band(mark)
        if band:
            parts.append(f"{getattr(spec, 'name', None) or slot} {band}")
            marks.append(mark)
    if not parts:
        return ""
    return (" Speakers here keep: " + ", ".join(parts)
            + ". A band no speaker here keeps is not heard at all"
            + _missing_bands_note(marks) + ".")


def _cinema_mode_choices(game, gp, jukebox_id):
    # The mode menu's lines, each already saying what it would leave out. The warning belongs
    # where the mistake is made: a shape is chosen once and heard for every song after it, so
    # a mode that would leave one of the speakers placed here silent says so next to its own
    # name rather than in a log nobody reads.
    current = _cabinet_cinema_mode(gp, jukebox_id)
    anchor = cabinet_anchor(game, jukebox_id) if game is not None else None

    def _note(name):
        if anchor is None:
            return ""
        plan, silent = room_plan(game, anchor, requested=name, room_id=jukebox_id)
        return _silent_speaker_note(silent, plan)

    # A locked cabinet still lists every mode -- the pick is what asks for the
    # code -- so each line says that it will, instead of the box appearing
    # after a choice nobody was warned about.
    lock = _cabinet_cinema_lock(gp, jukebox_id)
    needs_code = bool(lock) and not _may_lock_cinema_mode(gp) \
        and lock[0] != _own_name(gp)
    code_note = f" - needs {lock[0]}'s code" if needs_code else ""

    choices = [
        (CINEMA_AUTO, "Auto - use the speakers placed around this cabinet"
         + _note(CINEMA_AUTO) + code_note),
        (CINEMA_OFF, "Off - always this cabinet's own stereo" + code_note),
        (CINEMA_CUSTOM, "Custom - play exactly the speakers placed for this "
         "cabinet, however they are set up" + _note(CINEMA_CUSTOM) + code_note),
    ]
    choices.extend((name, f"Force the {name} room shape{_note(name)}{code_note}")
                   for name in profile_names() if name != CINEMA_CUSTOM)
    return current, choices


def _open_cinema_mode_menu(game, gp, jukebox_id):
    # Staff menu: what this cabinet should play through. The server writes the answer into
    # the map and every client hears it from the next song on, so nothing here has to be
    # remembered locally -- the local echo below only makes the menu answer immediately.
    from .. import menu as menu_mod, menus
    from .menu import open_jukebox_menu

    current, choices = _cinema_mode_choices(game, gp, jukebox_id)

    items = []
    for value, description in choices:
        label = f"{description}{' (now)' if value == current else ''}"
        items.append((label, functools.partial(
            _apply_cinema_mode, game, gp, jukebox_id, value)))
    items.append(("Back", lambda: (gp.pop_last_substate(), open_jukebox_menu(game, gp))))

    m = menu_mod.Menu(game, "Jukebox Cinema Mode", parrent=gp)
    m.add_items(items)
    menus.set_default_sounds(m)
    gp.add_substate(m)


def _open_cinema_reach_menu(game, gp, jukebox_id):
    # Staff menu: how big this cabinet's room is. The reach is the same number under two names -
    # how far a speaker may stand to belong to the room, and how far from a listener it is still
    # heard - so widening it both takes in a hall's back wall and makes that wall audible. It is a
    # property of *this room*, so it is chosen at the cabinet.
    from .. import menu as menu_mod, menus
    from .menu import open_jukebox_menu

    current = _cabinet_cinema_reach(gp, jukebox_id)
    lock = _cabinet_cinema_lock(gp, jukebox_id)
    needs_code = bool(lock) and not _may_lock_cinema_mode(gp) \
        and lock[0] != _own_name(gp)
    code_note = f" - needs {lock[0]}'s code" if needs_code else ""

    # What each choice would do to *this* room, one line each: a reach is a number, and the two
    # things it is measured against (the speakers placed here and the edge of the zone the map
    # drew) are the reason a small one goes unheard at the back. A choice that covers the room
    # stays short.
    extent = _room_extent(game, jukebox_id)
    items = []
    for value, description in CINEMA_REACHES:
        geometry = extent.note(value) if extent is not None else ""
        label = (f"{value} m - {description}"
                 f"{' (now)' if abs(current - value) < 0.5 else ''}"
                 f"{geometry}{code_note}")
        items.append((label, functools.partial(
            _apply_cinema_reach, game, gp, jukebox_id, value)))
    items.append(("Back", lambda: (gp.pop_last_substate(), open_jukebox_menu(game, gp))))

    m = menu_mod.Menu(game, "Jukebox Cinema Reach", parrent=gp)
    m.add_items(items)
    menus.set_default_sounds(m)
    gp.add_substate(m)


def _apply_cinema_reach(game, gp, jukebox_id, reach):
    """Ask the server to change a cabinet's reach (it checks who may)."""
    from .. import consts

    gp.pop_last_substate()
    game.network.send(consts.CHANNEL_MISC, "jukebox_cinema_reach",
                      {"id": jukebox_id, "radius": int(reach)})
    player = getattr(game, "gameplay", None)
    jukebox_player = getattr(player, "jukebox_player", None)
    if jukebox_player is not None:
        jukebox_player.set_local_cinema_reach(jukebox_id, reach)
    # Like the mode, this does not wait for the next track: the server writes
    # the number into the map and re-offers the song, which re-shapes the
    # playing room -- a speaker the wider reach now takes in joins on the beat.
    extent = _room_extent(game, jukebox_id)
    geometry = extent.note(reach, prefix=" ") if extent is not None else ""
    speak(f"Cinema reach {int(reach)} m ({_cinema_reach_label(reach)}). "
          "Re-shaping this cabinet's room." + geometry)


def _open_cinema_lock_menu(game, gp, jukebox_id):
    # Ask the Server for one cabinet's lock controls: the Server owns the lock (it writes the
    # code's hash into the map), so the menu is the Server's too, and a refusal arrives in its words.
    from .. import consts

    game.network.send(consts.CHANNEL_MENUS, "builder_cinema_mode_lock",
                      {"elementId": jukebox_id})


def _apply_cinema_mode(game, gp, jukebox_id, mode):
    """Ask the server to change a cabinet's mode (it checks who may)."""
    from .. import consts

    gp.pop_last_substate()
    game.network.send(consts.CHANNEL_MISC, "jukebox_cinema_mode",
                      {"id": jukebox_id, "mode": mode})
    player = getattr(game, "gameplay", None)
    jukebox_player = getattr(player, "jukebox_player", None)
    if jukebox_player is not None:
        jukebox_player.set_local_cinema_mode(jukebox_id, mode)
    # It does not wait for the next track: the server writes the mode into the
    # map and re-offers the song it is playing, which the client re-tunes.
    speak(f"Cinema mode {mode} ({_cinema_mode_label(mode)}). "
          "Re-tuning this cabinet.")
