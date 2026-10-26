"""The machinery every knob board shares: a table, read and written.

There are three boards on the Voice Effect page and they are the same shape:

- `voice_effect_custom.py` -- **Custom Amp**, the last line of Amp & Drive:
  seven knobs that configure the amp chain (drive, character, bass, mid,
  treble, gate, output);
- `voice_effect_builder.py` -- **Custom Voice**, the last choice on the Voice
  Effect page itself: ten slots that assemble a whole voice effect out of the
  blocks the sixty-seven presets are made of;
- `voice_effect_vocoder.py` -- **Vocoder Lab**, the last line of that family:
  nine rows that expose the six-band vocoder's carrier, with the rows a mode
  cannot use hidden by the board's own `rows()`.

Keeping the *machinery* here and the *tables* there is what makes a new board
cheap: a board module is a `PARAMS` tuple, a `spec()` builder and its own option
key, and it gets the reading, clamping, labelling and Tab text for free. The
menu walks the board's `PARAMS` (or its `rows()` filter), so a page is generated
and never written out by hand.

What deliberately stays in each board module: the `options.get`/`options.set`
calls. A test patches `voice_effect_custom.options` (or the builder's, or the
vocoder's) to keep a board in memory, and a shared reader would slip past that
seam and write the player's real settings file from a test.
"""


def param(params, param_id):
    """One knob or slot's row, or None for an id that is not on the board."""
    for row in params:
        if row[0] == param_id:
            return row
    return None


def choices(params, param_id):
    """The rows a knob opens into."""
    row = param(params, param_id)
    return row[3] if row else ()


def choice_value(choices, value, default):
    """A stored value that is a choice of this knob, or the knob's default.

    A saved board outlives the table it was written from: a value that is not
    offered any more (a choice renamed, a board that lost a row) must fall back
    to the default rather than build a spec from nothing.
    """
    for _label, stored, _note in choices:
        if stored == value:
            return stored
    return default


def read(params, saved):
    """The board, every knob filled in from `saved` or from its default."""
    board = {}
    for param_id, _label, _note, row_choices, default in params:
        stored = saved.get(param_id) if isinstance(saved, dict) else None
        board[param_id] = choice_value(row_choices, stored, default)
    return board


def default(params, param_id):
    """The value a knob sits at when nothing has been chosen for it."""
    row = param(params, param_id)
    return row[4] if row else None


def default_label(params, param_id):
    """What that default is called in the menu's words: 'Crunch', 'Unity'.

    The choice pages have never said what the board opens with -- a player
    reads six values and no answer to "which one was it" -- so the line that
    puts one back names it (`Clear: back to Crunch`). Read from the table, so
    the wording follows a default that moves and a renamed choice.
    """
    value = default(params, param_id)
    if value is None:
        return ""
    for choice_label, stored, _note in choices(params, param_id):
        if stored == value:
            return choice_label
    return ""


def label(params, board, param_id):
    """A knob's chosen choice in the menu's words: 'Crunch', 'Cave'."""
    row = param(params, param_id)
    if row is None:
        return ""
    chosen = board[param_id]
    for choice_label, stored, _note in row[3]:
        if stored == chosen:
            return choice_label
    return row[3][0][0]


def note(params, param_id, value):
    """What Tab reads on one choice of one knob."""
    row = param(params, param_id)
    if row is None:
        return ""
    for _label, stored, choice_note in row[3]:
        if stored == value:
            return choice_note
    return ""
