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
    being investigated -- its room, level, trim, voicing and crossover. The
    element id is omitted for the same reason as the label: it is internal
    plumbing, and a speech engine reads it as a string of letters.
    """
    parts = [f"Speaker {index} of {total}.", f"Channel {_channel(speaker)}."]
    room = str(speaker.get("room") or "").strip()
    parts.append(f"Room {room or 'none'}.")
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
    items = []
    if found:
        for index, speaker in enumerate(found, 1):
            items.append((label(speaker, index, total),
                          _edit(game, gp, speaker), None,
                          detail(speaker, index, total)))
    else:
        items.append(("No cinema speakers on this map",
                      _reader("This map has no cinema speakers. Place one "
                              "from the Builder menu, then press Alt plus C again.")))
    items.append(("Back", lambda: gp.pop_last_substate()))
    m.add_items(items)
    menus.set_default_sounds(m)
    gp.add_substate(m)


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
