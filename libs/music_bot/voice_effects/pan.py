"""The Pan Delay: a delay whose repeats cross the space around the listener.

Why this is not only a DSP
--------------------------
Every other pedal here shapes the mono frame the talker sends, and that is all
a listener can hear -- one channel of 20 ms frames has no left, right, front or
back in it. A pan has to be applied *where the sound is rendered*, which is the
listener's own machine: their copy of the talker's voice already plays from a 3D
source at the talker's body (`entity.vc_source`), through EFX/HRTF. So this
effect is two halves:

- **the delay** is the talker's, on the same chain as every other preset
  (`dsp._SPECS`'s `pan_delay` key is built from `spec()` below);
- **the position** is three bytes per frame on `CHANNEL_VOICE_MOTION`, which the
  Server relays beside the voice, and which every listener turns into an offset
  of that 3D source around the talker's body -- along the LISTENER's right or
  forward axis, so "left to right" means their left and right.

An older client ignores the channel (there is no receiver for it, so
`party_sync_audio.receiver_for` answers None); an older Server never relays it,
so the sender only emits it when the login snapshot carried `voice_motion_v1`
(the same negotiation `pa_timeline_v1` uses).

The typed value
---------------
`Delay` is typed, not a row of fixed steps: 20-2000 ms, whole milliseconds. It
is the time between the repeats AND the time one crossing takes -- at 300 ms a
word is on the right when it is spoken, on the left one repeat later. Saved under
`music_bot_voice_pan_delay_ms`; `Motion` (`Stereo`, `Front-Back`, `Off`) is
`music_bot_voice_pan_motion`. Every change bumps `revision()`, which
`VoiceEffectLine` reads beside the preset id, so a typed delay lands on the next
20 ms frame rather than at the next pedal change.

Where the line lives
--------------------
On the Voice Effect page itself, beside the eleven families and `Custom Voice`
(`catalogue.STANDALONE_EFFECTS`), because a typed delay is not one
of a family's rows of fixed steps: the line opens a page rather than choosing a
pedal in place. It is deliberately not a member of `Motion` -- a preset is in
one family or a standalone row, never both (`catalogue_problems` reports
`listed twice`), and a player looking for the delay should not have to guess
which family it was filed under.

The named settings, and the one table they live in
--------------------------------------------------
`PRESETS` is a delay and a movement under a name (`Ping-Pong`, `Wide Sweep`,
`Front Walk`, `Deep Behind`, `Slow Drift`), plus `Custom (this page)`, which is
the player's own two numbers read live. Choosing a named row WRITES the delay
and the movement, so the two options stay the only truth and the mark only
records where the pair came from -- a typed delay or a chosen movement puts the
mark back to `Custom` rather than naming a preset those numbers no longer
match.

**The same table is the Custom Voice board's Pan Delay slot**
(`builder`, through `preset_choices()` and `spec_keys()`), which is
what makes a delay set on this page usable by another function: a built voice
can carry any of these names, and `Custom (this page)` carries whatever is set
here -- kept live on purpose, with `dsp._board_revision` reading this
module's `revision()` while the board's slot is on `custom`, so a number typed
here is heard through the board on the next 20 ms frame.
"""

from ... import options

#: The catalogue id. It is a standalone row on the Voice Effect page, not a
#: board and not a member of a family: its two settings are options, not a table
#: of knobs.
EFFECT_ID = "pan_delay"

#: The two saved settings, beside the preset that selects it.
DELAY_OPTION = "music_bot_voice_pan_delay_ms"
MOTION_OPTION = "music_bot_voice_pan_motion"

DEFAULT_DELAY_MS = 300
MIN_DELAY_MS = 20
MAX_DELAY_MS = 2000

#: Three repeats is what makes a ping-pong read as movement; a fourth is a
#: muddier tail for one more filter-free copy.
TAPS = 3

#: What one unit of motion means in metres, at the listener's ear. One metre is
#: enough for HRTF to place a voice clearly beside the head and small enough that
#: the distance attenuation (computed from the source's position) does not
#: audibly pump.
METRES = 1.0

#: The movement choices: left-right across the listener, front-back through
#: them, or still. `Off` keeps the delay and drops the motion.
MOTIONS = (
    ("Stereo", "stereo",
     "The repeats cross from one ear to the other: the voice is on your right "
     "as it is spoken and on your left one delay later. The ping-pong."),
    ("Front-Back", "front_back",
     "The repeats pass in front of you and behind you instead of beside you. "
     "Needs headphones or HRTF to place behind -- on plain speakers it still "
     "reads as a movement, just a smaller one."),
    ("Off", "off",
     "The delay stays where the voice is: three repeats, no movement. The "
     "effect without the trick."),
)

_MOTION_IDS = {stored for _label, stored, _note in MOTIONS}

#: The player's own numbers, and the mark a named setting leaves behind it.
CUSTOM_PRESET = "custom"

#: The saved mark: which named setting the two values came from, or `custom`
#: when the player typed them (`music_bot_voice_pan_preset`).
PRESET_OPTION = "music_bot_voice_pan_preset"

#: The named settings, and **the one table two features share**: the page lists
#: them under `Preset:`, and the Custom Voice board's own Pan slot offers the
#: same names (`builder`), so a delay set here is a delay another
#: function can play. A named row WRITES the delay and the movement above when
#: it is chosen -- the two options stay the only truth, and the mark only says
#: where the pair came from, so the page can never contradict what it plays.
#: ``(stored id, menu name, milliseconds, movement, what Tab reads)``.
PRESETS = (
    (CUSTOM_PRESET, "Custom (this page)", None, None,
     "The two settings above, as you set them: the delay you typed and the "
     "movement you chose. Choosing this from another page keeps it following "
     "this page, so a delay typed here is the delay played there."),
    ("ping_pong", "Ping-Pong", 120, "stereo",
     "A fast crossing: 120 milliseconds, so the repeat is on the other ear "
     "almost as soon as the word is spoken, and the voice bounces between "
     "them. The tight side-to-side ping-pong."),
    ("wide_sweep", "Wide Sweep", 620, "stereo",
     "A slow crossing at 620 milliseconds: the voice is on one side for "
     "about a third of a second before the repeat arrives on the other. "
     "Wide, calm, and the default's slower cousin."),
    ("front_walk", "Front Walk", 400, "front_back",
     "The movement runs front-to-back instead of ear-to-ear at 400 "
     "milliseconds: the voice steps in front of the listener and then behind "
     "them. Needs headphones or HRTF to place the half behind."),
    ("deep_behind", "Deep Behind", 1000, "front_back",
     "One whole second between the repeats, front to back: a slow walk "
     "around the listener with the words repeating far apart. The longest "
     "setting here that still reads as movement rather than as an echo."),
    ("slow_drift", "Slow Drift", 1700, "stereo",
     "Almost a second and a half from ear to ear: a drift rather than a "
     "crossing, with three long repeats. For a voice that should hang in the "
     "room rather than walk through it."),
)

_PRESET_ROWS = {row[0]: row for row in PRESETS}


def _preset_row(name):
    """The row a saved mark names, or None for a mark that is not in the table."""
    return _PRESET_ROWS.get(name)


def preset():
    """Which named setting the two values came from, `custom` when typed."""
    raw = options.get(PRESET_OPTION, CUSTOM_PRESET)
    if not isinstance(raw, str) or raw not in _PRESET_ROWS:
        return CUSTOM_PRESET
    return raw


def set_preset(name):
    """Choose a named setting; it writes the delay and the movement it names.

    A named row is a delay and a movement, so choosing one is the same two
    writes a player would make by hand -- followed by the mark that says which
    name they came from. `custom` writes only the mark, which is how a player
    goes back to their own numbers. None for a name that is not in the table.
    """
    global _revision
    row = _preset_row(name)
    if row is None:
        return None
    stored, label, milliseconds, mode, _note = row
    if stored != CUSTOM_PRESET:
        options.set(DELAY_OPTION, int(milliseconds))
        options.set(MOTION_OPTION, mode)
    options.set(PRESET_OPTION, stored)
    _revision += 1
    return label


def preset_label(value=None):
    """The mark in the menu's words: 'Custom (this page)', 'Ping-Pong'."""
    row = _preset_row(preset() if value is None else value)
    return row[1] if row else PRESETS[0][1]


def preset_note(value):
    """What Tab reads on one named setting."""
    row = _preset_row(value)
    return row[4] if row else ""


def preset_values(name):
    """`(milliseconds, movement)` for a name; the page's own for `custom`.

    `custom` is read live rather than stored: that is what makes the mark a
    link to this page instead of a copy of it.
    """
    if name == CUSTOM_PRESET or name not in _PRESET_ROWS:
        return delay_ms(), motion()
    _stored, _label, milliseconds, mode, _note = _PRESET_ROWS[name]
    return int(milliseconds), mode


def preset_choices():
    """The table in the builder's own shape: ``(menu name, stored, note)``.

    The Custom Voice board's Pan slot is generated from this, so the two pages
    cannot drift: a name added above is a line on both.
    """
    return tuple((label, stored, note) for stored, label, _ms, _mode, note
                 in PRESETS)


def spec_keys(name):
    """The processor's keys for a named setting: what the board adds to a spec.

    The same two keys `spec()` writes, so a board that chose a Pan Delay and the
    Pan Delay page itself build the identical block.
    """
    milliseconds, mode = preset_values(name)
    return {"pan_delay": (milliseconds / 1000.0, TAPS, METRES),
            "pan_motion": mode}

#: The chain the delay runs on. A wide, clean channel: the movement is the
#: effect, so the cabinet does not colour it and the shaper is not engaged.
_BASE = dict(hp=120.0, lp=6500.0, drive=1.0, shape=None, bias=0.0,
             gate=0.004, out=0.98)

_revision = 0


def delay_ms():
    """The typed delay in whole milliseconds, clamped to the range."""
    raw = options.get(DELAY_OPTION, DEFAULT_DELAY_MS)
    try:
        value = int(round(float(raw)))
    except (TypeError, ValueError):
        return DEFAULT_DELAY_MS
    return max(MIN_DELAY_MS, min(MAX_DELAY_MS, value))


def set_delay_ms(value):
    """Store a typed delay; None when the text is not a number of milliseconds.

    A typed "0.3s" is a person saying 300 ms, so a trailing unit is read rather
    than refused; "abc" leaves the setting alone and the page says so.
    """
    global _revision
    text = str(value).strip().lower()
    if not text:
        return None
    factor = 1.0
    if text.endswith("ms"):
        text = text[:-2].strip()
    elif text.endswith("s"):
        text = text[:-1].strip()
        factor = 1000.0
    try:
        number = float(text) * factor
    except ValueError:
        return None
    stored = max(MIN_DELAY_MS, min(MAX_DELAY_MS, int(round(number))))
    options.set(DELAY_OPTION, stored)
    # Typing a number is the player making the pair their own: the mark goes
    # back to `custom` rather than naming a preset the numbers no longer match.
    options.set(PRESET_OPTION, CUSTOM_PRESET)
    _revision += 1
    return stored


def motion():
    """The chosen movement, or `stereo` for a saved value that is gone.

    A saved value that is not a string at all (a corrupted options file, a test
    that answers every key with the same object) is the default too: the menu
    reads this on every visit to the page, so it must not be able to raise.
    """
    raw = options.get(MOTION_OPTION, "stereo")
    if not isinstance(raw, str):
        return "stereo"
    return raw if raw in _MOTION_IDS else "stereo"


def set_motion(value):
    """Choose the movement; an unknown value leaves the setting alone."""
    global _revision
    if value not in _MOTION_IDS:
        return None
    options.set(MOTION_OPTION, value)
    options.set(PRESET_OPTION, CUSTOM_PRESET)
    _revision += 1
    return value


def motion_label(value=None):
    """A movement id in the menu's words: 'Stereo', 'Front-Back', 'Off'."""
    wanted = motion() if value is None else value
    for label, stored, _note in MOTIONS:
        if stored == wanted:
            return label
    return MOTIONS[0][0]


def motion_note(value):
    """What Tab reads on one movement choice."""
    for _label, stored, note in MOTIONS:
        if stored == value:
            return note
    return ""


def note():
    """What Tab reads on the Pan Delay line in the pedal list."""
    return (f"A delay the listener hears move: {delay_ms()} milliseconds between "
            f"the repeats, and the repeats cross {motion_label().lower()} "
            f"around the listener's head. It is the one preset that is not a "
            f"shape on the mono frame -- the movement is applied by the "
            f"listener's own machine, from three bytes the Server relays beside "
            f"the voice. The same named settings sit in the Custom Voice "
            f"board's Pan Delay slot, so a delay set here can be played by a "
            f"voice built there.")


def summary():
    """The page's title: the delay and the movement in four words."""
    return f"{delay_ms()} ms, {motion_label()}"


def spec():
    """The board-like spec: the fixed chain plus this page's two settings."""
    return dict(_BASE,
                pan_delay=(delay_ms() / 1000.0, TAPS, METRES),
                pan_motion=motion())


def revision():
    """How many changes the page has had; the line rebuilds when it moves."""
    return _revision


def clear(value=None):
    """Back to 300 ms and Stereo -- the page's own Clear line."""
    global _revision
    options.set(DELAY_OPTION, DEFAULT_DELAY_MS)
    options.set(MOTION_OPTION, "stereo")
    options.set(PRESET_OPTION, CUSTOM_PRESET)
    _revision += 1
    return delay_ms()


def motion_vector(pan, mode):
    """`(right, forward)` shares of one metre for a normalised pan and a mode.

    The sender writes a single signed number per frame; which axis it moves
    along is a property of the preset, and this is the one place that maps it.
    """
    if mode == "front_back":
        return 0.0, pan
    if mode == "off":
        return 0.0, 0.0
    return pan, 0.0
