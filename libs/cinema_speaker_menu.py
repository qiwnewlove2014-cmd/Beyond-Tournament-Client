"""The map's cinema speakers, in one list.

``Alt + C`` (the configured Speak Location key with Alt) opens this from the
world. The Builder's **Edit Map Elements** is where a speaker is *changed*;
this is where a room is *checked* -- every ``cinemaSpeaker`` the map placed,
counted, named by its role and read back at its coordinates. Standing in a
hall and asking "how many speakers did I place, and where is the one that is
quiet" used to mean walking the whole Edit Element flow.

Tab reads one speaker's settings without leaving the list; Enter hands that
speaker to the Server's own element menu, so a trim heard to be wrong is fixed
where it was heard. The list itself writes nothing and changes no listening
switch: the edit menu keeps the lock, the rank check and the confirm step the
Builder's Edit Map Elements always had. It is a staff tool on the same rule as
the pan and the cabinet sound test (``can_use_cinema_pan``: every staff rank),
and it deliberately leaves the Builder menu's element list exactly as it was:
that list is the builder's, this is the technician's.
"""

from . import consts
from .speech import speak
from .audio.cinema import crossover as cinema_crossover
from .audio.cinema import plugin as cinema_plugin

REFUSAL = "The cinema speaker list is for staff."


def allowed(gameplay):
    """Whether this account may read the room's speaker list.

    The same Server-owned answer as the pan and the sound test: the people
    who set a cinema room up are exactly the people who check it.
    """
    return bool(getattr(gameplay, "can_use_cinema_pan", False))


def speakers(game):
    """Every speaker the map placed, in map order, or an empty list.

    Read through the cinema plugin rather than off the map object, so this
    answers the same thing the room does and a game without a map (a headless
    harness, a server object) is simply a map with no speakers.
    """
    try:
        return list(cinema_plugin.map_speakers(game))
    except Exception:
        return []


def count(game):
    """How many cinema speakers stand on this map."""
    return len(speakers(game))


def title(total):
    """The line a technician hears on opening the list.

    The word *Menu* is part of the name: when Back closes the Server's element
    options, this title is what the player hears again, and "Cinema Speakers"
    alone sounded like the first line of a report rather than the menu they had
    returned to.
    """
    return f"Cinema Speakers Menu ({total} on this map)"


def label(speaker, index, total):
    """One line of the list: the speaker's number, role and coordinates.

    The number is deliberately spoken as "of total" -- with twelve speakers
    the useful answer is not only *which* one this is but how many the room
    has, and the title is already behind the cursor by the time somebody
    arrows down the list. The element id is deliberately *not* spoken: a
    machine reads a map id letter by letter, and it is the one thing on the
    line a person never asked for. It still travels with the item internally,
    because Enter needs it to open the Server's edit menu.
    """
    return (f"Speaker {index} of {total}: {_channel(speaker)} "
            f"at {_coordinates(speaker)}")


def detail(speaker, index, total):
    """Everything a technician can check about one speaker, in one sentence.

    The label carries what is needed while arrowing (which speaker, which
    role, where it stands); this is what Tab reads when the line *is* the one
    being investigated -- its room assignment, level, trim, voicing and
    crossover. The element id is omitted for the same reason as the label: it
    is internal plumbing, and a speech engine reads it as a string of letters.
    """
    parts = [f"Speaker {index} of {total}.", f"Channel {_channel(speaker)}."]
    # ``room`` is the map's own assignment label, not the cabinet's mode: an
    # empty value leaves the cabinet choice to the resolver, and a value is
    # spoken exactly as stored because ids differ between maps and servers.
    room = str(speaker.get("room") or "").strip()
    if room:
        parts.append(f"Room assigned to {room}.")
    else:
        parts.append("Room unassigned (resolver chooses).")
    parts.append(f"Level {_number(speaker.get('level', 100))} percent.")
    parts.append(f"Delay {_number(speaker.get('delay_ms', 0))} milliseconds.")
    parts.append(f"Tone {_number(speaker.get('tone', 100))} percent.")
    band = _crossover(speaker)
    parts.append(f"Crossover: {band}." if band else "Crossover: full range.")
    aim = speaker.get("aim_yaw")
    if aim is None:
        parts.append("Aim: omnidirectional.")
    else:
        parts.append(f"Aim: {_number(aim)} degrees.")
    parts.append(f"At X {_number(speaker.get('x'))}, "
                 f"Y {_number(speaker.get('y'))}, "
                 f"Z {_number(speaker.get('z'))}.")
    return " ".join(parts)


def open_menu(game, gameplay=None):
    """Open the speaker list, or say who it is for.

    No key of its own belongs in ``default_keyconfig.json``: the chord rides
    the configured ``speak_location`` key, exactly as ``Alt + V`` rides
    ``speak_zone``. A config file stores one key per action and no modifiers,
    so a second row for this would silently steal the plain location key.
    """
    gp = gameplay if gameplay is not None else getattr(game, "gameplay", None)
    if game is None or gp is None:
        return
    if not allowed(gp):
        speak(REFUSAL)
        return

    found = speakers(game)
    total = len(found)
    from . import menu as menu_mod, menus

    m = menu_mod.Menu(game, title(total), parrent=gp)
    items, ids = _items(game, gp, found)
    m.add_items(items)
    # A tag rather than a field on the shared Menu class: `refresh_open` below
    # has to find this list again after the Server has rewritten the map, and
    # only this menu is a speaker list.
    m.cinema_speaker_list = True
    m.cinema_speaker_ids = ids
    menus.set_default_sounds(m)
    gp.add_substate(m)


def _items(game, gp, found):
    """The list's lines for the map as it stands now, and the ids behind them.

    Both are returned because a refresh needs both: the lines to show and, by
    id, which speaker the reader was standing on before the map changed under
    them -- a line's number changes when a speaker before it is deleted, so an
    index is not a place to hold on to.
    """
    total = len(found)
    items = []
    ids = []
    if found:
        for index, speaker in enumerate(found, 1):
            items.append((label(speaker, index, total),
                          _edit(game, gp, speaker), None,
                          detail(speaker, index, total)))
            ids.append(str(speaker.get("id") or ""))
    else:
        items.append(("No cinema speakers on this map",
                      _reader("This map has no cinema speakers. Place one "
                              "from the Builder menu, then press Alt plus C again.")))
    items.append(("Back", lambda: gp.pop_last_substate()))
    return items, ids


def refresh_open(game, gameplay=None):
    """Rebuild every open speaker list from the map as it stands now.

    The Server's element menu writes the map and then sends the whole map back
    (``update_map``), which this Client applies on the game thread a tick
    later; the invitation to reopen this list arrives first, so a list built at
    that moment still holds the speaker that was just deleted -- and pressing
    Enter on it only produced "this id has already been deleted". Rather than
    asking the reader to leave the list and open it again, the list is rebuilt
    where it stands, on the speaker they were on.

    Talk is for a change in the speakers themselves: a deleted or added one is
    announced with the new count, because nothing else would tell them. An edit
    to a speaker that is still there (a level, a trim, a position) leaves the
    list as it was and says nothing -- Tab re-reads it, and a sentence about a
    list nobody asked to change is noise over the room they are standing in.
    """
    gp = gameplay if gameplay is not None else getattr(game, "gameplay", None)
    if game is None or gp is None:
        return False
    refreshed = False
    for state in list(getattr(gp, "substates", ()) or ()):
        if not getattr(state, "cinema_speaker_list", False):
            continue
        found = speakers(game)
        total = len(found)
        pos = getattr(state, "pos", -1)
        old_ids = list(getattr(state, "cinema_speaker_ids", ()))
        current = old_ids[pos] if 0 <= pos < len(old_ids) else ""
        items, ids = _items(game, gp, found)
        state.items = []
        state.add_items(items)
        # Follow the speaker, not the index: the cursor must not land on Back
        # because the last speaker was the one deleted.
        if pos < 0:
            new_pos = -1
        elif current and current in ids:
            new_pos = ids.index(current)
        elif ids:
            new_pos = max(0, min(pos, len(ids) - 1))
        else:
            new_pos = 0
        state.pos = max(-1, min(new_pos, len(items) - 1))
        state.cinema_speaker_ids = ids
        state.title = title(total)
        if ids != old_ids:
            _announce(state, total)
        refreshed = True
    return refreshed


def _announce(state, total):
    """Say the new count and the line the cursor is on, in one utterance.

    Not interrupting on purpose: this runs a tick after the Server said
    "Element deleted.", and cutting that off would swallow the answer to the
    delete the technician just made.
    """
    pos = getattr(state, "pos", -1)
    line = ""
    if 0 <= pos < len(getattr(state, "items", ())):
        first = state.items[pos][0]
        line = first() if callable(first) else str(first)
    speak(f"{title(total)}. {line}".strip(), False, id="menu_title")


def _edit(game, gp, speaker):
    """Enter opens this speaker's own options on the Server.

    The same menu the Builder's Edit Map Elements opens when that speaker is
    picked: the Server re-checks the rank and the game-in-progress rule, opens
    the element's own lock and offers the in-place lines (channel, level,
    delay, tone, crossover, aim). Sending the event is all this client does --
    it writes no map element itself, and an unconnected client says so rather
    than looking like a menu that did nothing.

    The list closes before the Server menu opens and carries `cinemaList`, so
    the Server's Back callback invites this client to build a fresh list (with
    its title spoken again) instead of walking back out through Edit Map
    Elements and the Builder Menu. The list is local state, not a map write;
    closing it is safe even if the Server refuses the edit, and the refusal is
    spoken over the game the player is back in.
    """
    ident = str(speaker.get("id") or "").strip()

    def action():
        if not ident:
            speak("This speaker has no element id, so it cannot be edited.")
            return
        network = getattr(game, "network", None)
        if network is None:
            speak("Editing a speaker needs a connection to the Server.")
            return
        network.send(consts.CHANNEL_MENUS, "edit_element_select", {
            "value": {"elementId": ident, "elementType": "CinemaSpeaker"},
            "cinemaList": True,
        })
        gp.pop_last_substate()

    return action


def _reader(line):
    """A line with no edit (the empty list) answers in words."""
    return lambda: speak(line)


def _channel(speaker):
    channel = str(speaker.get("channel") or "").strip().lower()
    return channel or "auto"


def _coordinates(speaker):
    return (f"({_number(speaker.get('x'))}, "
            f"{_number(speaker.get('y'))}, "
            f"{_number(speaker.get('z'))})")


def _crossover(speaker):
    """What this speaker keeps, in the crossover module's own words."""
    try:
        return cinema_crossover.describe(speaker.get("crossover", 0),
                                         speaker.get("crossover_high", 0))
    except Exception:
        return ""


def _number(value):
    """A coordinate or setting as a person says it, never as a float repr.

    ``21.0`` is read aloud as "twenty one point zero" and a menu has no room
    for that; ``21.5`` keeps the half metre it needs. An unreadable value is a
    question mark rather than a crash inside a read-out.
    """
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "?"
    if number != number or number in (float("inf"), float("-inf")):
        return "?"
    text = f"{number:.1f}"
    return text.rstrip("0").rstrip(".") if "." in text else text
