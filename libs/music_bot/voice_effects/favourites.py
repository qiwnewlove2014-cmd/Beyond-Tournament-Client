"""The player's own shortlist of voice effects: a page of sounds, not a list.

A hundred and fifty-four presets is more than anyone uses, and the four that
matter to a player are not the four at the top of a family -- they are the ones
they found, or the one they built. This module is that shortlist: a small,
ordered, saved set of entries, each one either

- a **preset** -- any row of `catalogue.VOICE_EFFECTS` except `off`, which is
  not a sound but the absence of one; or
- a **board snapshot** -- the Custom Amp, Custom Voice, Vocoder Lab or Pan
  Delay as it was set when the player saved it, values and all. That is the
  part that makes the list flexible rather than a second favourites button:
  a sound somebody designed is saved by what it *is*, and recalling it writes
  those settings back onto the board and chooses it.

An entry is a plain dict, so it survives the options file and a future version
that grows a field:

    {"kind": "preset", "id": "amp"}
    {"kind": "board", "id": "custom", "values": {...}, "label": "Crunch, ..."}

**Storage is one option key**, like every other board's setting
(`music_bot_voice_favourites`), read through this module's own `options` seam so
a test can patch it without writing the player's real settings file.

**Nothing here raises on bad data.** An options file is not a contract: an id
that the game no longer has is dropped from the page (`entries()` filters), a
value that is not a dict is ignored, and a board snapshot that lost a knob
recalls with the knob's default because the board's own `boards.read` owns that
rule. A favourite is a convenience, and a convenience that crashes the menu is
worse than one that quietly forgets.

What deliberately does NOT live here: the bot, the menu, and speaking. This
module answers questions and writes values; the page says the words.
"""

import hashlib
import json

from ... import options
from . import builder
from . import custom
from . import pan
from . import vocoder
from .catalogue import DEFAULT_EFFECT, VOICE_EFFECTS, effect_label, effect_note

#: The one saved setting: an ordered list of entries, newest last.
_OPTION = "music_bot_voice_favourites"

#: How many sounds the list holds. One page a player can arrow through without
#: scrolling a second screen, and more shortlist than anyone has ever used --
#: a favourites list long enough to need searching is the problem it was meant
#: to solve. The page says the number when it is full rather than silently
#: refusing the press.
MAX = 20

#: The boards whose *settings* an entry can carry. A plain pedal has no
#: settings of its own beyond the tone strip, which is already saved per preset
#: by `tone.py`, so a pedal entry is just its id.
BOARDS = {
    custom.EFFECT_ID: custom,
    builder.EFFECT_ID: builder,
    vocoder.EFFECT_ID: vocoder,
    pan.EFFECT_ID: pan,
}

#: Every id a player could save: the catalogue, minus Off.
_KNOWN = tuple(row[0] for row in VOICE_EFFECTS if row[0] != DEFAULT_EFFECT)


# ------------------------------------------------------------------ reading


def is_board(effect_id):
    """Whether this id's sound is its settings rather than a fixed pedal."""
    return effect_id in BOARDS


def values_of(effect_id):
    """The board as it is set right now, or {} for a preset."""
    if effect_id == pan.EFFECT_ID:
        return {"delay_ms": pan.delay_ms(), "motion": pan.motion()}
    board = BOARDS.get(effect_id)
    if board is None:
        return {}
    return dict(board.values())


def summary_of(effect_id):
    """A few words for what the board holds now: '300 ms, Stereo'."""
    board = BOARDS.get(effect_id)
    return board.summary() if board is not None else ""


def _clean(entry):
    """A stored entry as a dict with the fields this module promises, or None.

    The options file is a text file a player (or a bad merge) can edit, so an
    entry is validated rather than trusted: no id, an id that is gone, or a
    kind that is not one of the two are all just a row that is not shown.
    """
    if not isinstance(entry, dict):
        return None
    effect_id = entry.get("id")
    if not isinstance(effect_id, str) or effect_id not in _KNOWN:
        return None
    kind = entry.get("kind")
    if kind == "preset":
        return {"kind": "preset", "id": effect_id}
    if kind == "board" and effect_id in BOARDS:
        values = entry.get("values")
        if not isinstance(values, dict):
            values = {}
        label = entry.get("label")
        return {"kind": "board", "id": effect_id,
                "values": dict(values),
                "label": label if isinstance(label, str) else ""}
    return None


def stored():
    """The saved list as it was written, cleaned but not filtered for id."""
    raw = options.get(_OPTION, [])
    if not isinstance(raw, (list, tuple)):
        return []
    rows = []
    for entry in raw:
        cleaned = _clean(entry)
        if cleaned is not None:
            rows.append(cleaned)
    return rows


def entries():
    """The rows the page shows: the saved list, in order, nothing dropped.

    `stored()` already refuses an id the game does not have, so the page and
    the count can never disagree with what `add` would let a player save.
    """
    return tuple(stored())


def count():
    return len(stored())


def key(entry):
    """A stable identity for one entry, so the same sound saves once.

    A preset is its id. A board is its id *and* its values: two amp settings
    are two sounds and both belong in the list, while the same setting saved
    twice is one row. The digest keeps the key short and the comparison cheap;
    nothing outside this module reads it.
    """
    effect_id = entry.get("id", "")
    if entry.get("kind") != "board":
        return f"preset:{effect_id}"
    payload = json.dumps(entry.get("values") or {}, sort_keys=True,
                         separators=(",", ":"), default=str)
    digest = hashlib.sha1(payload.encode("utf-8")).hexdigest()[:12]
    return f"board:{effect_id}:{digest}"


def label(entry):
    """What the page's line says: the pedal's name, or the board and its set."""
    effect_id = entry.get("id")
    name = effect_label(effect_id)
    if entry.get("kind") != "board":
        return name
    held = entry.get("label") or summary_of(effect_id)
    return f"{name}: {held}" if held else name


def describe(entry):
    """What Tab reads on that line: the pedal's description, plus the snapshot."""
    effect_id = entry.get("id")
    note = effect_note(effect_id)
    if entry.get("kind") != "board":
        return f"{note} Saved in your favourites."
    held = entry.get("label") or ""
    if held:
        return (f"A saved setting of {effect_label(effect_id)}, not the "
                f"{effect_label(effect_id)} line: it recalls {held} as it was "
                f"when you saved it. {note}")
    return (f"A saved setting of {effect_label(effect_id)}. {note}")


def matches(entry):
    """Whether the game is on this saved sound right now.

    Only the values the entry carries are compared, so a board that grew a
    knob since the save still reads as the saved sound when the knobs it knew
    are the same -- which is what the `(active)` mark on the page means.
    """
    if entry.get("kind") != "board":
        return False
    live = values_of(entry.get("id"))
    for param_id, value in (entry.get("values") or {}).items():
        if live.get(param_id) != value:
            return False
    return True


# ------------------------------------------------------------------ writing


def snapshot(effect_id):
    """An entry for the effect as it is set right now, or None for no effect.

    This is the designer's door: a player who has spent ten minutes on the
    Custom Voice board presses `Add this Custom Voice to Favorites` and the
    sound they made becomes a row they can come back to. For a fixed preset
    the snapshot is just its id -- there is nothing else to remember.
    """
    if effect_id not in _KNOWN:
        return None
    if not is_board(effect_id):
        return {"kind": "preset", "id": effect_id}
    return {"kind": "board", "id": effect_id,
            "values": values_of(effect_id),
            "label": summary_of(effect_id)}


def add(entry):
    """Save one entry. Answers 'added', 'exists', 'full' or 'unknown'.

    The answer, not an exception: every one of these is a sentence the page
    says back to the player ('that is already in your favourites', 'the list
    is full'), and a menu is the wrong place to find out by traceback.
    """
    cleaned = _clean(entry)
    if cleaned is None:
        return "unknown"
    rows = stored()
    wanted = key(cleaned)
    if any(key(row) == wanted for row in rows):
        return "exists"
    if len(rows) >= MAX:
        return "full"
    rows.append(cleaned)
    options.set(_OPTION, rows)
    return "added"


def remove(wanted_key):
    """Drop the entry with this key; True when a row went."""
    rows = stored()
    kept = [row for row in rows if key(row) != wanted_key]
    if len(kept) == len(rows):
        return False
    options.set(_OPTION, kept)
    return True


def clear():
    """Forget the whole list, and answer how many rows went."""
    rows = stored()
    options.set(_OPTION, [])
    return len(rows)


def effect_of(entry):
    """The pedal this entry means, or None for one the game does not have."""
    return entry.get("id") if _clean(entry) is not None else None


def restore(entry):
    """Put a saved entry back: write the board's values, choose the pedal.

    Returns the effect id, or None when the entry no longer resolves (a preset
    removed since it was saved). The bot is the caller's to touch: this module
    writes the board and answers which pedal to choose, so the menu and a test
    both see the same two steps.
    """
    cleaned = _clean(entry)
    if cleaned is None:
        return None
    effect_id = cleaned["id"]
    values = cleaned.get("values") or {}
    if cleaned["kind"] == "board":
        if effect_id == pan.EFFECT_ID:
            delay = values.get("delay_ms")
            if delay is not None:
                pan.set_delay_ms(delay)
            motion = values.get("motion")
            if motion is not None:
                pan.set_motion(motion)
        else:
            board = BOARDS[effect_id]
            writer = getattr(board, "set_values", None)
            if callable(writer):
                writer(values)
            else:
                for param_id, value in values.items():
                    board.set_value(param_id, value)
    return effect_id
