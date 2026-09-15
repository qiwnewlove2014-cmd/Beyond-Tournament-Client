"""The staff menu that moves somebody's voice -- and their band -- somewhere else.

Three questions, in the order a person asks them:

    1. *whose* sound? (a player on this map, by name)
    2. *where*? (a cabinet's room, or clear -- which means the room the player
       is standing in, the same answer every client resolves by itself)
    3. *how* inside that room? (as the room is, or leaning to a side of it)

The choice is sent to the Server and relayed to the whole map, because the
routing it changes is decided on every listener's own machine -- see
``libs/audio/cinema/pan.py`` for why that is, and for the rules (a pan chooses
*which* room -- a listener's own listening switches are asked first and still
decide what that listener hears; a destination the map does not have simply
does nothing).

Deliberately *not* a line in the Music Bot menu: that menu holds how a listener
hears the world, and this is a control over other people's sound. It is opened
from the **Builder/Technician menu**, beside the Cinema Speaker entry the same
job places (:meth:`Gameplay.open_cinema_pan`, reached through the Server's
``cinema_pan_menu`` event after its own permission check), so an account without
the rank never gets the line. It used to ride a key of its own (F6), which is
the beacon toggle's key -- a menu line has no such neighbour to collide with.
"""

from . import consts
from .audio.cinema import pan as cinema_pan
from .audio.cinema import plugin as cinema_plugin
from .audio.cinema import sound_test as cinema_sound_test
from .speech import speak

# Two jobs, one flow: a destination is chosen the same way whether it is about
# to be *used* (a pan) or only *tried* (a sound test), and the cabinet list --
# with its diagnosis of every cabinet that cannot carry sound -- is the same
# list. The mode only picks the last step.
MODE_PAN = "pan"
MODE_TEST = "test"

# The direction vocabulary in the order a reader wants it, with the words a
# player hears instead of the wire values in ``pan.DIRECTIONS``.
DIRECTION_LABELS = (
    ("auto", "As the room is mixed"),
    ("front", "Towards the screen (front)"),
    ("back", "Towards the back of the room"),
    ("left", "Towards the left of the room"),
    ("right", "Towards the right of the room"),
    ("centre", "Towards the centre of the room"),
)


def allowed(gameplay):
    """Whether this account may pan anybody (the Server owns the answer)."""
    return bool(getattr(gameplay, "can_use_cinema_pan", False))


def open_menu(game, gameplay=None, mode=MODE_PAN):
    """Open the menu: move somebody's sound, or fire a test at a cabinet.

    ``MODE_TEST`` asks the same three questions about a *destination* instead of
    about a person -- firing one short note into that cabinet's room so the
    room can be heard (and, on the map, checked) before a performance rather
    than during one. It is reached from the same Builder/Technician menu line,
    so the rank rule is the same one (see ``allowed``).
    """
    gp = gameplay if gameplay is not None else getattr(game, "gameplay", None)
    if gp is None or game is None:
        return
    if not allowed(gp):
        speak("Cinema panning is for technicians and contributors.")
        return
    if mode == MODE_TEST:
        _cabinets_menu(game, gp, None, None, MODE_TEST)
        return
    _players_menu(game, gp)


def open_test_menu(game, gameplay=None):
    """Fire a test at a cabinet without moving anybody's sound."""
    open_menu(game, gameplay, MODE_TEST)


# --------------------------------------------------------------------- levels
def _players_menu(game, gp):
    from . import menu as menu_mod, menus

    m = menu_mod.Menu(game, "Move whose sound?", parrent=gp)
    items = []
    for name, channel in _players(gp):
        items.append((_label(game, gp, name, channel),
                      _player_picker(game, gp, name, channel)))
    if items:
        if cinema_pan.describe(gp):
            items.append(("Clear every pan", _clear_everything(game, gp)))
    else:
        items.append(("No players on this map to move",
                      lambda: speak("There is nobody else here to move.")))
    items.append(("Close", lambda: gp.pop_last_substate()))
    m.add_items(items)
    menus.set_default_sounds(m)
    gp.add_substate(m)


def _player_picker(game, gp, name, channel):
    def _open():
        _cabinets_menu(game, gp, name, channel)
    return _open


def _cabinets_menu(game, gp, name, channel, mode=MODE_PAN):
    """Pick a cabinet: the destination of a pan, or the target of a test.

    The same list either way, because it is the same question -- which cabinet,
    and can it carry a sound at all -- and the diagnosis a refused cabinet shows
    is the thing a builder needs to read before either.
    """
    from . import menu as menu_mod, menus

    if mode == MODE_TEST:
        title = "Test which cabinet's sound?"
    else:
        title = f"Where should {_display(gp, name)} be heard?"
    m = menu_mod.Menu(game, title, parrent=gp)
    items = list(_report_items(gp))
    current = None if mode == MODE_TEST else cinema_pan.target_for_channel(gp, channel)
    # The room a player's live sound belongs to with nobody having moved them:
    # the one at their feet. It is what clearing a pan sends them back to, and
    # what the list marks when it is where they already are (see
    # ``_auto_cabinet``), so a pan is a choice about a known destination rather
    # than a guess. A test has nobody to ask this about.
    auto = None if mode == MODE_TEST else _auto_cabinet(game, gp, name, channel)
    if current is not None:
        who = _display(gp, name)
        items.append((f"Clear - {who} goes back to {_back_to(gp, name, auto)}",
                      _apply(game, gp, name, channel, "", "auto")))
    cabinets = _map_cabinets(game)
    if not cabinets:
        items.append(("This map has no jukebox to play through",
                      lambda: speak("This map has no jukebox cabinet.")))
    for cabinet_id, anchor, room in cabinets:
        position = f"({anchor[0]:.0f}, {anchor[1]:.0f}, {anchor[2]:.0f})"
        if room is None:
            # A cabinet with no room cannot carry a voice at all. It is listed
            # with its own diagnosis rather than hidden, because "my cabinet is
            # missing from the menu" and "my cabinet cannot carry sound" are
            # very different things to the person who just placed speakers.
            why = cinema_plugin.room_diagnosis(game, anchor, room_id=cabinet_id)
            items.append((f"Jukebox {cabinet_id} {position} - {why}",
                          _refused(cabinet_id, why)))
            continue
        label = f"Jukebox {cabinet_id} {position} - {room.summary()}"
        if current is not None and current[0] == str(cabinet_id):
            label = f"* {label} (playing there)"
        elif current is None and auto is not None and str(cabinet_id) == auto:
            here = "you are here" if _is_me(gp, name) else "they are here"
            label = f"* {label} ({here})"
        items.append((label, _direction_picker(game, gp, name, channel,
                                              cabinet_id, mode)))
    items.append(("Back", lambda: gp.pop_last_substate()))
    m.add_items(items)
    menus.set_default_sounds(m)
    gp.add_substate(m)


def _direction_picker(game, gp, name, channel, cabinet_id, mode=MODE_PAN):
    """Pick how the destination is aimed: apply a pan, or aim a test at it."""
    def _open():
        from . import menu as menu_mod, menus

        if mode == MODE_TEST:
            title = f"How should jukebox {cabinet_id} sound for the test?"
        else:
            title = f"How should jukebox {cabinet_id} mix {_display(gp, name)}?"
        m = menu_mod.Menu(game, title, parrent=gp)
        if mode == MODE_TEST:
            items = [(label, _fire(game, gp, cabinet_id, value))
                     for value, label in DIRECTION_LABELS]
            items.extend(_report_items(gp))
        else:
            items = [(label, _apply(game, gp, name, channel, cabinet_id, value))
                     for value, label in DIRECTION_LABELS]
            # The same destination, tried before it is used: a pan is heard
            # differently on every machine, so "did jukebox j2 take it, and does
            # the direction sound right" is a question to answer *before* the
            # performance. One short note, and every client reports back.
            items.append((f"Test jukebox {cabinet_id} (short note, pick a "
                          f"direction next)",
                          _direction_picker(game, gp, name, channel,
                                            cabinet_id, MODE_TEST)))
        items.append(("Back", lambda: gp.pop_last_substate()))
        m.add_items(items)
        menus.set_default_sounds(m)
        gp.add_substate(m)
    return _open


def _fire(game, gp, cabinet, direction):
    """Ask the Server to fire one test at this cabinet (it relays to the map).

    The menu deliberately **stays on the direction list**: a tester walks the
    sides of one room by pressing a direction, hearing what came back, then
    pressing the next one, and stepping back after every shot would mean
    re-navigating to the same cabinet each time. ``Back`` is the way out, and
    the answers are also waiting in the cabinet list one step behind it (the
    last test's report line); a press that is too soon is answered with how
    long to wait rather than being swallowed.
    """
    def _send():
        if not allowed(gp):
            speak("Cinema sound testing is for technicians and contributors.")
        elif cinema_sound_test.fire(game, gp, cabinet, direction):
            # What was *asked*, not what happened: the answers come back a
            # moment later and are spoken then (see ``note_result``), because
            # the whole point of the shot is what the other machines did.
            speak(f"Firing a test at jukebox {cabinet}.")
        else:
            speak("Could not send that test.")
    return _send


def _report_items(gp):
    """The last test's answer, when there is one: a count, then the reasons."""
    summary = cinema_sound_test.describe(gp)
    if not summary:
        return []
    lines = cinema_sound_test.details(gp)
    if not lines:
        return [(summary, lambda: speak(summary))]
    return [(f"{summary} - press for details", _details_menu(gp, lines))]


def _details_menu(gp, lines):
    """One line per client of the last test, read one at a time."""
    def _open():
        from . import menu as menu_mod, menus

        m = menu_mod.Menu(gp.game, "Last sound test", parrent=gp)
        m.add_items([(line, _say(line)) for line in lines]
                    + [("Back", lambda: gp.pop_last_substate())])
        menus.set_default_sounds(m)
        gp.add_substate(m)
    return _open


def _say(line):
    def _speak():
        speak(line)
    return _speak


# -------------------------------------------------------------------- actions
def _map_cabinets(game):
    """``[(cabinet_id, anchor, room)]`` for every cabinet on this map."""
    found = []
    for cabinet_id, anchor in cinema_plugin.cabinet_anchors(game):
        room = cinema_plugin.preview_room(game, anchor, room_id=cabinet_id)
        found.append((cabinet_id, anchor, room))
    return found


def _auto_cabinet(game, gp, name, channel):
    """The cabinet a player's live sound belongs to right now, with no pan.

    The very rule the routing uses (``live.room_for``), asked about the
    *person* rather than about the reader's ears: it is one answer on every
    machine, which is what lets this menu say where somebody's voice and band
    go now -- the question a staff member opens the menu with, and the answer
    an explicit pan overrides. A player the map has no room for, or one whose
    entity this client has not seen yet, answers None instead of guessing.
    """
    from .audio.cinema import live as cinema_live
    from .audio.cinema import speech as cinema_speech

    if _is_me(gp, name):
        point = cinema_speech.local_position(gp)
    else:
        point = cinema_speech.talker_position(gp, channel)
    if point is None:
        return None
    room = cinema_live.room_for(game, point)
    return room[0] if room is not None else None


def _back_to(gp, name, auto):
    """Where clearing a pan sends somebody, in the menu's own words.

    Deliberately not "the map's PA": clearing an explicit pan means going back
    to the *rule*, which is the room the player is standing in whenever the map
    has one, and only the map's PA (or the instrument at their feet) when there
    is no room at all.
    """
    if not auto:
        return "the map's PA"
    where = "the room you are in" if _is_me(gp, name) else "the room they are in"
    return f"jukebox {auto} ({where})"


def _refused(cabinet_id, why):
    def _say():
        speak(f"Jukebox {cabinet_id} cannot carry a sound: {why}")
    return _say


def _apply(game, gp, name, channel, cabinet, direction):
    def _send():
        who = _display(gp, name)
        if send_pan(game, name, channel, cabinet, direction):
            # A confirmation of what was *asked*, not of what happened: the
            # Server relays the real state back (and speaks to the player whose
            # sound moved), so a refusal -- a rank that changed, a player who
            # left -- never sounds like success.
            if cabinet:
                speak(f"Sending {who} to jukebox {cabinet}.")
                # ...and what it means *here*: a pan is resolved on every
                # listener's own machine, so the person testing on one client
                # has a different answer from the person next to them. Saying
                # it back turns "the pan looks broken" into "my Instruments
                # line is off" before anybody blames the room.
                speak(f"Here: {cinema_plugin.listening_summary()}.")
            else:
                speak(f"{who} goes back to the normal room.")
        else:
            speak("Could not send that pan.")
        gp.pop_last_substate()
    return _send


def _clear_everything(game, gp):
    def _clear():
        for _name, channel in _players(gp):
            if cinema_pan.target_for_channel(gp, channel) is not None:
                send_pan(game, "", channel, "", "auto")
        speak("Cleared every pan on this map.")
        gp.pop_last_substate()
    return _clear


def send_pan(game, name, channel, cabinet, direction):
    """Ask the Server to move (or clear) one player's sound."""
    network = getattr(game, "network", None)
    if network is None:
        return False
    payload = {
        "channel": int(channel),
        "name": str(name or ""),
        "cabinet": str(cabinet or ""),
        "direction": cinema_pan.normalize_direction(direction),
    }
    try:
        network.send(consts.CHANNEL_MISC, "staff_pan", payload)
    except Exception:
        return False
    return True


def _own_channel(gp):
    """This client's *own* voice channel, or None when it is not known yet.

    A player's own name never reaches their own client through a spawn packet:
    ``Map.add_player`` tells a joiner about everyone on the map *except* them
    (they are not in the player tree yet, and the broadcast that announces them
    excludes the sender). So the local channel arrives in the login snapshot
    instead, which is the one thing that lets a staff member pan themselves.
    A Server that predates that field leaves this None and self is simply not
    listed -- the old behaviour, never a crash.
    """
    value = getattr(gp, "own_voice_channel", None)
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _me(gp):
    """The local player's name, or ``''`` when there is nobody to move."""
    return str(getattr(getattr(gp, "player", None), "name", "") or "").strip()


def _players(gp):
    """``[(name, voice channel)]`` for everyone staff may move, self first.

    The channel is the key both ends already agree on: the Server resolves the
    player by it, a listener's voice play-out is keyed by it, and the
    instrument path reaches the same entry by name (see ``pan.PanTable``).

    **Yourself is in this list** (``_own_channel``): an MC standing in a booth
    but meant to be heard from the hall pans their own voice, and until this
    existed the menu could only ever move somebody else. Self is listed first
    because it is the one entry the reader knows without looking.
    """
    found = {}
    channels = getattr(gp, "voice_channels", None)
    if isinstance(channels, dict):
        for channel, entity in channels.items():
            name = str(getattr(entity, "name", "") or "").strip()
            if not name:
                continue
            try:
                found[name] = int(channel)
            except (TypeError, ValueError):
                continue
    me = _me(gp)
    channel = _own_channel(gp)
    if me and channel is not None:
        found.setdefault(me, channel)
    return sorted(found.items(),
                  key=lambda item: (0 if me and item[0].lower() == me.lower()
                                    else 1, item[0].lower()))


def _is_me(gp, name):
    """Whether this name is the person reading the menu."""
    me = _me(gp)
    return bool(me) and str(name or "").lower() == me.lower()


def _display(gp, name):
    """How to say a player's name to whoever is reading the menu."""
    return "you" if _is_me(gp, name) else str(name or "")


def _label(game, gp, name, channel):
    """A line in the picker: the name, then what it is to this listener."""
    tags = []
    if _is_me(gp, name):
        tags.append("you")
    if cinema_pan.target_for_channel(gp, channel) is not None:
        tags.append("moved")
    else:
        # Where their live sound belongs with nobody having moved them, so a
        # staff member can see what an explicit pan would be replacing.
        cabinet = _auto_cabinet(game, gp, name, channel)
        if cabinet:
            tags.append(f"at jukebox {cabinet}")
    return f"{name} ({', '.join(tags)})" if tags else str(name)


def describe(game, gameplay=None):
    """One line per active pan, for a menu footer or a log."""
    gp = gameplay if gameplay is not None else getattr(game, "gameplay", None)
    return cinema_pan.describe(gp)
