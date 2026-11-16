"""The lines of the Music Bot a player has hidden, and the key that hides one.

Why this exists
---------------
The Music Bot grows a line every release, and a player who only ever uses three
of them still arrows past all of them. This is the player's own answer: hide the
lines you do not use, keep the ones you do. It is a listening preference of one
machine, not a permission, so it lives beside `own_voice_from_cabinets` in
`options` and is never sent anywhere -- the Server does not know it exists.

**A new feature costs nothing here.** Every Music Bot line already carries a
stable id, because the Developer's gate (`music_bot_gate.ts`, `gate.py`) needs
one; hiding is keyed on that same id. So a page that builds its lines with
`build(menu, items)` gets "hide this line" for every line it will ever have, and
the only thing a new feature must do is the thing it already had to do: give its
line an id.

Two filters, one order
----------------------
`build` applies the gate first and this store second, so a Developer's lock
always wins: hiding can only take more away, never put back something staff
locked. A line with no id (`Back`, `Help`, `Music Bot Settings`, `Lines I hide`)
is never hideable, which is what keeps the way back open.

Hiding a line hides the LINE, never the key. A key that went silent because a
menu line was tidied away would read as a broken key, and disabling a feature is
the gate's job, not a player's.

Pins: client/tests/test_music_bot_menu_prefs.py.
Rules: .agents/skills/music_bot_integration/.
"""

import json

from .. import options
from . import gate

# Two dots, not three: this module is `libs.music_bot.menu_prefs`, so `..` is
# `libs` and `...` would be the client root (the `menu_pages/` pages are one
# level deeper and do use three). The wrong count does not raise -- it lands in
# the fallback below, which is silent -- so a hide that said nothing at all was
# this line, and the test below pins the real function.
try:  # pragma: no cover - speech is present in every real client
    from ..speech import speak
except Exception:  # pragma: no cover
    def speak(*_args, **_kwargs):
        return None


#: Where this machine keeps the lines its player hid: `{id: label}` as JSON.
OPTION = "music_bot_hidden_lines"


def _read():
    """The stored `{id: label}` map, tolerating anything unreadable.

    A preference that cannot be read must never hide a line: an empty map is
    the whole menu, which is the safe direction to fail in.
    """
    try:
        raw = options.get(OPTION, "")
    except Exception:
        return {}
    if isinstance(raw, dict):
        stored = raw
    else:
        try:
            stored = json.loads(raw or "{}")
        except Exception:
            return {}
    if not isinstance(stored, dict):
        return {}
    return {
        str(key): (label if isinstance(label, str) and label else str(key))
        for key, label in stored.items()
        if isinstance(key, str) and key
    }


def _write(stored):
    try:
        options.set(OPTION, json.dumps(stored, ensure_ascii=False, sort_keys=True))
    except Exception:
        pass


def hidden_ids():
    """Every id this player hid, as a frozenset."""
    return frozenset(_read())


def hidden():
    """The stored `{id: label}` map itself, for the page that offers them back."""
    return _read()


def count():
    """How many lines are hidden, for the label of the page that manages them."""
    return len(_read())


def is_hidden(feature_id):
    """Whether this player hid `feature_id`."""
    return bool(feature_id) and feature_id in _read()


def may_hide(feature_id):
    """Whether a line with this id can be hidden at all.

    A line with no id is not a feature: `Back`, `Help`, the Settings hub, and
    `Lines I hide` itself. They are what makes a hidden menu recoverable, so
    they are never hideable -- which is the whole of the rule, and why it needs
    no list to keep in step with the menus.
    """
    return bool(feature_id)


def hide(feature_id, label=""):
    """Hide one line, remembering the words it had for the manage page."""
    if not may_hide(feature_id):
        return False
    stored = _read()
    stored[feature_id] = label or str(feature_id)
    _write(stored)
    return True


def show(feature_id):
    """Put one line back. False when it was not hidden."""
    stored = _read()
    if feature_id not in stored:
        return False
    del stored[feature_id]
    _write(stored)
    return True


def toggle(feature_id, label=""):
    """Hide it or put it back; True when it is hidden afterwards."""
    if is_hidden(feature_id):
        show(feature_id)
        return False
    hide(feature_id, label)
    return True


def show_everything():
    """Put every line back. Returns how many had been hidden."""
    stored = _read()
    _write({})
    return len(stored)


def label_of(feature_id):
    """The words a hidden line had when it was hidden."""
    return _read().get(feature_id) or str(feature_id)


def filter_items(items):
    """This player's filter over `(id, label, action[, description])` lines.

    The exact shape `gate.filter_items` takes, and the same one trap: a Menu
    line is `(label, action, preview_sound, description)`, so a description has
    to be padded past the preview slot rather than slid into it. A locked or
    hidden line is simply not built, so it cannot be chosen.
    """
    kept = []
    for item in items:
        if is_hidden(item[0]):
            continue
        entry = tuple(item[1:])
        if len(entry) > 2:
            entry = (entry[0], entry[1], None, *entry[2:])
        kept.append(entry)
    return kept


def build(menu, items):
    """Fill `menu` from a page's `(id, label, action, description)` lines.

    The one call a Music Bot page makes instead of `add_items(gate.filter_items(
    [...])`: it applies the Developer's gate, then this player's own hidden set,
    adds what is left, and teaches the menu to hide the line the player is on
    with Ctrl+H (`libs/menu.py` carries the key to `hide_current_line`).

    It also keeps the id of every line it added, in the same order, because the
    Menu itself only ever holds labels and actions -- that parallel list is what
    makes Ctrl+H possible without a second table of which line is which feature.
    """
    kept, ids = [], []
    locked = gate.locked_ids()
    for item in items:
        feature_id = item[0]
        if feature_id and feature_id in locked:
            continue
        if is_hidden(feature_id):
            continue
        entry = tuple(item[1:])
        if len(entry) > 2:
            entry = (entry[0], entry[1], None, *entry[2:])
        kept.append(entry)
        ids.append(feature_id)
    menu.add_items(kept)
    menu.line_ids = list(getattr(menu, "line_ids", []) or []) + ids
    menu.hide_current_line = lambda: hide_current(menu)
    return menu


def _label_text(menu, pos):
    """The words the focused line is reading out, without speaking them."""
    try:
        label = menu.items[pos][0]
    except Exception:
        return ""
    if callable(label):
        try:
            label = label()
        except Exception:
            return ""
    return str(label or "").strip()


def position_text(menu):
    """Where the focus is, said the way a player hears a list: "line 3 of 8".

    A blind player has no highlight to look at, and a line that leaves a page
    slides the next one up into the same index -- so "the cursor did not move"
    and "the cursor moved" sound exactly alike. The count is what tells them the
    list shrank, and the index is what tells them where they are standing now.
    """
    count = len(getattr(menu, "items", ()) or ())
    pos = getattr(menu, "pos", -1)
    if count <= 0 or not (0 <= pos < count):
        return ""
    return f"line {pos + 1} of {count}"


def hide_current(menu):
    """Hide the line the player is standing on (Ctrl+H), and say what happened.

    The line leaves this menu in place -- no page is rebuilt -- so the next line
    slides up into the index the player was on. One utterance says all of it:
    which line went away, how many are left, and which line the focus is on now
    (`position_text`). Nothing is spoken twice and nothing interrupts: a second
    `speak` call here would cut the first one off mid-word, and the player would
    hear the new line without ever hearing what happened to the old one.

    Reopening the page rebuilds it from the builders anyway, which is what keeps
    a hidden line hidden across visits.
    """
    ids = getattr(menu, "line_ids", None) or []
    pos = getattr(menu, "pos", -1)
    if not ids or not (0 <= pos < len(ids)) or pos >= len(getattr(menu, "items", ())):
        return False
    feature_id = ids[pos]
    if not may_hide(feature_id):
        speak("This line cannot be hidden. Back, Help and Lines I hide always stay.")
        return False
    label = _label_text(menu, pos) or str(feature_id)
    hide(feature_id, label)
    del ids[pos]
    try:
        del menu.items[pos]
    except Exception:
        pass
    if getattr(menu, "pos", 0) >= len(getattr(menu, "items", ())):
        menu.pos = max(0, len(menu.items) - 1)
    remaining = len(getattr(menu, "items", ()) or ())
    if not remaining:
        speak(f"Hidden: {label}. Every line on this page is hidden now. "
              "Back and Help are still here.")
        return True
    where = position_text(menu) or f"{remaining} left"
    now_on = _label_text(menu, menu.pos)
    speak(f"Hidden: {label}. {remaining} line"
          f"{'' if remaining == 1 else 's'} left. Now on {where}"
          f"{': ' + now_on if now_on else ''}. Music Bot Settings, Lines I "
          "hide, brings it back.")
    return True
