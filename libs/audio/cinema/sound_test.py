"""The staff sound test: one short note at a cabinet's speakers, everyone hears it.

A pan is resolved on the *listener's* own machine, and so is the room a band
plays through, so "did that work" cannot be answered from one client: the person
who fired it hears one answer and the person standing next to them may hear
nothing at all because of a switch on their own machine. Two clients open on one
desk make it worse rather than better -- the pan lands on both and can be heard
on neither, and a switch that is off, a listener out of the room's reach and an
old build all look exactly alike.

The sound test closes that gap the only way it can be closed. One short note is
fired into a named cabinet's room through the very route a panned band travels
(``libs/piano.py::route_test_note_to_room`` -> ``live.route_to_room``), every
client plays it or does not according to its own switches and its own ears, and
each one reports back what it did. The Server collects the answers and hands the
person who fired it one line (see ``server/libs/cinema_sound_test.ts``); this
module is the client's half: play it here, say why not, and send the answer.

Three rules it keeps. It never touches the frame queue of a room that is playing
a song (a test is a note *at* the speakers, so a song in progress is not
disturbed -- what it cannot check is the queue's own depth), it never asks the
Server to decide anything the listening machine decides (the switches are read
here, where they are), and it holds exactly **one** exception to the listening
switches: the client that fired a shot hears that one note whatever its own
``Instruments:`` line says. That line is a listening choice about *bands*, and a
tester checking a room they are standing in front of is not asking to hear a
band over it -- they are asking whether this cabinet can be heard at all, and
without the exception the answer is always no on the one machine that is
standing in the room. Every other machine answers its own switch, and nothing
else about the note is exempt (see ``play``).
"""

from ... import consts
from ...deferred_log import log_deferred as log_line
from . import live as cinema_live
from . import plugin as cinema_plugin
from .pan import DEFAULT_DIRECTION

# What a test fires: one short note, the same one every time, so two shots are
# the room's differences rather than the note's. Both live in the piano's own
# route (they are that route's numbers), named here for the callers that only
# need to say what was fired.
NOTE = "C4"
DURATION_MS = 380
VOLUME = 300

# A reason is one line in a menu on somebody else's screen: bounded here as well
# as in the packet schema, because a diagnosis string can be long and the
# interesting half of it is always at the front.
MAX_REASON = 48

# Where this client keeps the last report it was given. On the gameplay object,
# like the pan table, so it dies with the map it describes.
ATTRIBUTE = "cinema_sound_test"

# How a direction is said out loud. Only ``auto`` needs words of its own: the
# rest are the words the picker offers.
_SPOKEN_DIRECTION = {
    "auto": "as the room is mixed",
    "front": "front",
    "back": "back",
    "left": "left",
    "right": "right",
    "centre": "centre",
}


def fire(game, gameplay, cabinet, direction):
    """Ask the Server to fire one test at a cabinet; it relays it to the map.

    The shot itself is the Server's (it is the only side that knows who is
    listening, and the only side that can hold a rate limit a client cannot
    lift), so this only names the destination. Returns whether it was sent.
    """
    network = getattr(game, "network", None)
    if network is None:
        return False
    payload = {
        "cabinet": str(cabinet or "").strip(),
        "direction": str(direction or DEFAULT_DIRECTION).strip().lower(),
    }
    try:
        network.send(consts.CHANNEL_MISC, "cinema_test", payload)
    except Exception:
        return False
    return True


def play(game, gameplay, cabinet, direction, *, test_id=0, mine=False):
    """Play the test note here and return the report this client will send.

    The report is ``{"heard", "speakers", "reason"}``: the number of speakers the
    note reached on *this* client, or the one phrase that says why it reached
    none. Every reason is a decision made on this machine -- the listener's own
    switch, a cabinet this map does not have, a room that does not resolve, ears
    out of the room's reach -- and each one is a different thing to go and fix,
    which is why they are told apart instead of collapsed into "nothing".

    ``mine`` is true on the one client that **fired** this shot (the Server
    relays the firing player's name with it, and the handler compares it with
    this connection's own). That client gets the one exception in the whole
    cinema layer: its own ``Instruments:`` switch is not consulted for the note
    it fired itself. A tester whose normal listening choice is "played where
    they stand" could otherwise never hear the room they are checking -- the
    summary would say "heard 0/n" while the one pair of ears standing in front
    of that cabinet was the reason. Nothing else is exempt, deliberately: a
    muted machine, a cabinet this map does not have, a room that does not
    resolve and ears out of the room's reach are all answers about the room (and
    about this machine's audio), and inventing audibility for them would be the
    false green this feature exists to remove.
    """
    report = {"heard": False, "speakers": 0, "reason": ""}
    if game is None or gameplay is None:
        report["reason"] = _bounded("no map is loaded")
        return report
    # The listener's own switch, asked first and asked live: with the band off
    # the rooms (Music Bot menu -> Instruments), a test is not heard from a
    # cabinet here -- and that is an answer about this listener, not about the
    # cabinet, so it is named as such. **Except for the shot this client fired
    # itself** (``mine``): that is one client, one note, and the switch line only
    # -- a room's reach and this machine's own audio are asked exactly as they
    # were below, so the tester cannot be told a room works when it does not.
    switch_off = not cinema_live.live_instruments_enabled()
    if switch_off and not mine:
        report["reason"] = _bounded("Instruments switch is off here")
        return report
    # ...and this machine's own audio, which is the one silence no switch in the
    # Music Bot menu explains: a muted game (or the Miscellaneous slider at
    # zero, the category a room note plays through) plays the note nowhere here,
    # and reporting "heard it" for it would be the exact false green this whole
    # test exists to remove.
    silent = _silent_here(game)
    if silent is not None:
        report["reason"] = _bounded(silent)
        return report
    anchor = cabinet_anchor(game, cabinet)
    if anchor is None:
        report["reason"] = _bounded(f"no jukebox {cabinet} on this map")
        return report
    terms = cinema_live.room_terms_for(game, anchor, pan=(cabinet, direction))
    if not terms:
        # Two very different silences: the destination does not resolve at all
        # (and ``room_diagnosis`` says which speaker set is missing), or it
        # resolves and this listener simply stands out of its reach.
        room = cinema_live.room_for(game, anchor, pan=(cabinet, direction))
        if room is None:
            report["reason"] = _bounded(
                cinema_plugin.room_diagnosis(game, anchor, room_id=cabinet))
        else:
            report["reason"] = _bounded(_out_of_reach(game, anchor, cabinet))
        return report
    piano = getattr(getattr(game, "audio_mngr", None), "piano", None)
    spoken = 0
    if piano is not None:
        try:
            spoken = piano.route_test_note_to_room(
                cabinet, direction, anchor,
                note_name=NOTE, base_volume=VOLUME, duration_ms=DURATION_MS,
            )
        except Exception:
            spoken = 0
    report["heard"] = spoken > 0
    report["speakers"] = int(spoken)
    # One line per shot on this client, on the routine sink: the summary says
    # how many heard it, and this says what *this* machine did about it -- the
    # half a tester standing in front of the room cannot otherwise read. When
    # the switch above was out of the way, the line says so: a later reader
    # (this tester, another one, a bug report) has to be able to tell a note
    # heard through a room from one heard because its own player fired it.
    exempt = (" (own Instruments switch is off; the note this client fired is "
              "heard anyway)") if (spoken and switch_off and mine) else ""
    log_line(f"[Cinema] sound test -> jukebox {cabinet} ({direction}): "
             + (f"played at {spoken} speaker(s){exempt}" if spoken
                else f"not heard here ({report['reason']})"))
    return report


def send_report(game, test_id, report):
    """Answer the test this client was relayed (the Server files it by name)."""
    network = getattr(game, "network", None)
    if network is None:
        return False
    payload = {
        "id": int(test_id),
        "heard": bool(report.get("heard")),
        "speakers": int(report.get("speakers") or 0),
        "reason": _bounded(str(report.get("reason") or "")),
    }
    try:
        network.send(consts.CHANNEL_MISC, "cinema_test_report", payload)
    except Exception:
        return False
    return True


def cabinet_anchor(game, cabinet):
    """Where the named cabinet stands on this client's map, or None."""
    key = str(cabinet or "").strip()
    if not key:
        return None
    for cabinet_id, anchor in cinema_plugin.cabinet_anchors(game):
        if str(cabinet_id) == key:
            return anchor
    return None


def _out_of_reach(game, anchor, cabinet=None):
    """Why this listener heard none of a room that resolves: the distance.

    The room's own reach is the *cabinet's* -- a hall that takes its speakers
    in at 90 m is heard to 90 m -- and a listener outside it hears nothing of a
    note the room carries (see ``live.note_goes_to_a_room``), so naming the gap
    and the reach says exactly how far back to walk. The number is read from
    the cabinet that was fired at, not from a module constant: a test that
    reported the wrong reach would send somebody walking the wrong distance.
    """
    position = getattr(getattr(game, "audio_mngr", None), "position", None)
    if position is None:
        return "out of reach here"
    try:
        gap = sum((float(anchor[i]) - float(position[i])) ** 2
                  for i in range(3)) ** 0.5
    except Exception:
        return "out of reach here"
    reach = cinema_plugin.cabinet_reach(game, cabinet) if cabinet else None
    if reach is None:
        return f"out of reach here ({gap:.0f} m)"
    # Short enough to survive the packet schema's 48 characters even with a
    # four-digit distance: the number is the fix (how far back to walk).
    return (f"out of reach here ({gap:.0f} m; "
            f"reach is {reach:.0f} m)")


def _silent_here(game):
    """Why this machine can make no sound at all, or None when it can.

    Asked about the audio manager rather than about any cinema setting: the note
    is played through the Miscellaneous category like every other instrument
    sound, so a muted game or that slider at zero means the shot was heard by
    nobody *here* however the listening switches are set.
    """
    audio = getattr(game, "audio_mngr", None)
    if audio is None:
        return None
    if getattr(audio, "muted", False):
        return "this machine's audio is muted"
    categories = getattr(audio, "volume_categories", None)
    if isinstance(categories, dict):
        entry = categories.get("miscelaneous")
        try:
            if entry is not None and float(entry[0]) <= 0.0:
                return "this machine's Miscellaneous volume is at zero"
        except Exception:
            return None
    return None


def _bounded(text):
    """One line, no longer than a menu line and the packet schema allow."""
    return " ".join(str(text or "").split())[:MAX_REASON]


# ------------------------------------------------------------------- the answer
def note_result(gameplay, data):
    """Keep the Server's summary and say it: the line the tester waits for.

    Short by design -- "heard 3/4" -- with the per-client reasons one line away
    in the menu (``details``), because a tester firing shots at five cabinets
    wants the count now and the names only for the ones that missed.
    """
    if gameplay is None or not isinstance(data, dict):
        return None
    result = dict(data)
    if result.get("error"):
        # A refusal (fired again inside the cooldown) is not an answer about the
        # room: writing it down would replace the last real report with
        # something ``describe`` can only ignore, so the tester would lose the
        # answer they just fired for by pressing a direction too soon. It is
        # still returned, because it is spoken.
        return result
    try:
        gameplay.cinema_sound_test = result
    except Exception:
        return None
    return result


def last(gameplay):
    """The last summary this client was given, or None."""
    result = getattr(gameplay, ATTRIBUTE, None)
    return result if isinstance(result, dict) else None


def describe(gameplay):
    """The summary in the words a menu line uses, or ``''`` with nothing to say."""
    result = last(gameplay)
    if not result or result.get("error"):
        return ""
    heard = int(result.get("heard") or 0)
    total = int(result.get("total") or 0)
    missing = max(0, total - heard)
    where = (f"jukebox {result.get('cabinet')}"
             if result.get("cabinet") else "the last test")
    if not missing:
        return f"Test report: {where} - heard {heard}/{total}, all of them"
    return f"Test report: {where} - heard {heard}/{total}, {missing} did not"


def details(gameplay):
    """One line per client for the last test: who heard it, and why not.

    The reasons are the client's own (each listener reports its own answer), so
    a line here is that machine's decision, not a guess about it. A player who
    never answered is named as such: an old build drops the relay without a
    trace, which must not read as "did not hear it".
    """
    result = last(gameplay)
    if not result:
        return []
    lines = []
    for entry in result.get("details") or ():
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or "?")
        if not entry.get("answered"):
            lines.append(f"{name} - no answer (an older build?)")
        elif entry.get("heard"):
            count = int(entry.get("speakers") or 0)
            lines.append(f"{name} - heard it ({count} speaker"
                         + ("" if count == 1 else "s") + ")")
        else:
            lines.append(f"{name} - not heard: "
                         + (str(entry.get("reason")) or "no reason given"))
    return lines


def spoken_summary(result):
    """The one sentence a tester hears the moment the answers are in."""
    if not isinstance(result, dict) or not result:
        return ""
    if result.get("error") == "cooldown":
        wait = max(0.0, float(result.get("wait_ms") or 0) / 1000.0)
        return f"Wait {wait:.1f} seconds before testing again."
    heard = int(result.get("heard") or 0)
    total = int(result.get("total") or 0)
    direction = _SPOKEN_DIRECTION.get(str(result.get("direction") or ""), "")
    where = f"jukebox {result.get('cabinet')}" if result.get("cabinet") else "the room"
    if direction and direction != _SPOKEN_DIRECTION["auto"]:
        where = f"{where} ({direction})"
    if total <= 0:
        return f"Nobody was on the map to hear the test at {where}."
    return f"Test at {where}: heard {heard}/{total}."
