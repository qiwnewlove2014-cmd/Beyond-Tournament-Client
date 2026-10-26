"""The Vocoder Lab board: one vocoder, with its carrier exposed.

Why this exists
---------------
The Vocoder Lab family is twenty finished machines. They are all the same
six-band bank with a different carrier -- a note, a waveform, where the bands
sit, a breath of noise -- so a player who wants a *particular* note has no way
to ask for it: the nearest preset is a guess, and the family's own tuner is not
one of its presets. This board is that ask: **Current** keeps the classic
110 Hz machine and lets the waveform, shift, breath and mix be chosen;
**Tune Note** locks the carrier to Root + Octave, so everything said comes out
on that note; **Interval** adds one degree of a scale above or below it, which
is a fixed harmony or a drone in a key.

The board is a third table beside `voice_effect_custom.py` (the amp) and
`voice_effect_builder.py` (the whole voice). It is the same contract:

- `PARAMS` is the whole page: a row is a knob -- name, what Tab says, choices,
  default -- and `_carrier_hz` is the only place a mode and a note become a
  frequency. A new row is one tuple, and the menu generates the page.
- `spec()` returns the same dict `_SPECS` holds, with `vocoder=(q, mix, hz,
  wave, shift, breath)`. There is no second DSP: `_EffectProcessor` builds it.
- `revision()` is bumped by every write, and `VoiceEffectLine` reads it beside
  the effect id, rebuilds and crossfades on the next 20 ms frame.
- `options.get`/`options.set` stay in this module, so a test patches this
  module's own seam and never the player's real settings file.

The mode rules
--------------
Current is the fixed 110 Hz carrier the family grew out of; Root and Octave do
nothing and are not shown. Tune Note is monotone **by design** -- the carrier
is the pitch, so a vocoder cannot follow the microphone and stay on one note at
the same time. Interval is the same note moved by a degree of Scale: a third is
major or minor because the scale says which, never because the microphone did.

**Noise has no note.** A noise carrier is allowed in Current (it is the family's
Noise Whisper), but Tune Note and Interval need a pitch to lock, so choosing
Noise there is refused and the board falls back to Saw. The same happens to a
saved board that had Noise in Current when its mode is switched.

Developer-only
--------------
The Server owns the permission (`player.canUseVocoderLab` ->
`can_use_vocoder_lab`), the Client's Voice Effect page shows the board's line
only to an account that has it, and `voice_effects.effect_id()` silences a
saved board when the permission is not present. The line is one row at the
bottom of the Vocoder Lab family, beside the twenty presets, for the same
reason Custom Amp sits at the bottom of Amp & Drive.

Pinned by `client/tests/test_voice_effects_vocoder_lab.py`.
"""

from .. import options
from . import autotune
from . import voice_effect_boards as boards

#: The id the rest of the feature knows this board by. It is a catalogue row and
#: a member of the Vocoder Lab family, so `catalogue_problems` can find it and
#: the top page can name it while it is chosen.
EFFECT_ID = "vox_lab"

#: The saved board, beside the preset that selects it.
_OPTION = "music_bot_voice_effect_vocoder"

#: The carrier Current mode speaks through, and the family's classic note.
CARRIER_HZ = 110.0

#: The twelve roots, in the order the note names list them.
_ROOTS = tuple(autotune.ROOT_NAMES)
_NOTE_PCS = {name: index for index, name in enumerate(_ROOTS)}

#: The plain semitone size of each interval, and where a key looks for its
#: note. Positive is above the root, negative below; the anchors are the
#: Harmony menu's own (-3 for a bass third, -5 for a bass fifth, -12 for a bass
#: octave), so a player who knows one menu finds the same note here. On a key the
#: anchor is snapped to the nearest note the scale has, never transposed into a
#: note outside it -- which is why a third is major over one root and minor over
#: another.
_INTERVAL_SEMITONES = {
    "off": 0,
    "third": 4,
    "fifth": 7,
    "sixth": 9,
    "seventh": 11,
    "octave": 12,
    "bass_third": -3,
    "bass_fifth": -5,
    "bass_octave": -12,
}

#: The menu's words for a mode, for the summary line.
_MODE_LABELS = {
    "current": "Current",
    "tune": "Tune Note",
    "interval": "Interval",
}

#: The menu's words for an interval, for the summary line.
_INTERVAL_LABELS = {
    "off": "Off",
    "third": "Third",
    "fifth": "Fifth",
    "sixth": "Sixth",
    "seventh": "Seventh",
    "octave": "Octave",
    "bass_third": "Bass Third",
    "bass_fifth": "Bass Fifth",
    "bass_octave": "Bass Octave",
}

#: The menu's words for a scale, for the summary line.
_SCALE_LABELS = {mode_id: label for mode_id, label, _classes, _note
                 in autotune.SCALES}


def _root_note(name):
    """What Tab says on one root, in octave 4 -- the row beneath moves it."""
    midi = 12 * 5 + _NOTE_PCS[name]
    hz = 440.0 * 2.0 ** ((midi - 69) / 12.0)
    return (f"{name}: {hz:.2f} Hz in octave 4. The Octave row moves it up or "
            "down, and Interval builds its scale from this note.")


_ROOT_CHOICES = tuple((name, name, _root_note(name)) for name in _ROOTS)

_OCTAVE_CHOICES = (
    ("1", "1",
     "The lowest: C is 32.70 Hz, below the first band at 180 Hz, so only the "
     "carrier's harmonics are heard. A sub-bass machine."),
    ("2", "2",
     "C is 65.41 Hz: still under the first band, but its harmonics fill the "
     "bank. A deep machine."),
    ("3", "3",
     "C is 130.81 Hz, the middle of a voice and the default. The classic "
     "vocoder sits just below it."),
    ("4", "4",
     "C is 261.63 Hz, middle C: bright and intelligible, above most speaking "
     "voices."),
    ("5", "5",
     "C is 523.25 Hz: a small, chip-like machine; the top band takes the "
     "brightness."),
    ("6", "6",
     "C is 1046.50 Hz, the highest: thin and whistle-like. Its harmonics "
     "still reach 4.2 kHz, so the words survive."),
)

_SCALE_CHOICES = (
    ("Chromatic", "chromatic",
     "Every semitone, so the plain sizes: a third is four semitones, a fifth "
     "seven, a seventh eleven. There is no key, so nothing is spelled major "
     "or minor."),
    ("Major", "major",
     "The bright seven-note scale. A third above the root is major (four "
     "semitones), and the sixth and seventh are major too."),
    ("Minor", "minor",
     "The natural minor scale, the darker relative of major. A third above "
     "the root is minor (three semitones), and the sixth and seventh are "
     "minor."),
    ("Harmonic Minor", "harmonic_minor",
     "A minor scale with its seventh raised a semitone: the harmonic-minor "
     "sound. A third above the root is still minor; the raised seventh leaves "
     "a wide gap under the octave."),
    ("Melodic Minor", "melodic_minor",
     "A minor scale with its sixth and seventh raised: minor at the bottom, "
     "major on top. A third above the root is minor, the sixth and seventh "
     "major."),
    ("Major Pentatonic", "major_pentatonic",
     "Five notes of the major scale, every half step left out. A third above "
     "the root is major; some degrees skip a note, which is the open sound."),
    ("Minor Pentatonic", "minor_pentatonic",
     "Five notes of the minor scale: the rock and blues scale. A third above "
     "the root is minor, and the wide gaps make an interval leap."),
)

_INTERVAL_CHOICES = (
    ("Off", "off",
     "The root alone: the carrier is tuned to the note with no second voice, "
     "which is exactly Tune Note."),
    ("Third", "third",
     "A third above the root, snapped to the nearest note of Scale: major "
     "over C in C major, minor over C in C minor -- the interval a second "
     "singer takes."),
    ("Fifth", "fifth",
     "A fifth above, snapped to Scale: the strongest harmony, the same note a "
     "chord degree higher. G above C."),
    ("Sixth", "sixth",
     "A sixth above, snapped to Scale: wider and softer than a third, the "
     "duet interval. A above C in C major."),
    ("Seventh", "seventh",
     "A seventh above, snapped to Scale: a whole tone under the octave in "
     "major, a semitone in minor. The jazzy, open one."),
    ("Octave", "octave",
     "The same note an octave above. It thickens the carrier without adding a "
     "note to it, and every scale has it."),
    ("Bass Third", "bass_third",
     "A third BELOW the root, from the same anchor the Harmony menu uses and "
     "snapped to Scale, so it is a minor third under some roots and a major "
     "third under others. A low second voice under the words."),
    ("Bass Fifth", "bass_fifth",
     "The fifth of the root placed underneath it, snapped to Scale -- the low "
     "root under the note, a drone under the words."),
    ("Bass Octave", "bass_octave",
     "The root an octave below. The same note, lower and heavier, and every "
     "scale has it."),
)

_WAVE_CHOICES = (
    ("Saw", "saw",
     "Every harmonic: the classic vocoder carrier, and the one with the most "
     "for the six bands to shape. The default."),
    ("Square", "square",
     "Odd harmonics only: hollow and buzzy, a machine with a nasal edge."),
    ("Pulse", "pulse",
     "A narrow pulse: thinner and more pointed than square, with a chip-like "
     "buzz."),
    ("Stack", "stack",
     "A saw with the same saw an octave up, half as loud: richer and brighter "
     "than one saw."),
    ("Noise", "noise",
     "No pitch at all: the words arrive as a loud whisper that keeps their "
     "formants. Tune Note and Interval refuse it -- there is no note to lock."),
)

_SHIFT_CHOICES = (
    ("0.5", "0.5",
     "Half: the bands sit an octave down -- a giant's throat, broad and dark."),
    ("0.7", "0.7",
     "Seven tenths: a bigger machine, lower and heavier, still clear."),
    ("0.85", "0.85",
     "A little below the presets: a chesty machine."),
    ("1.0", "1.0",
     "The bands where the twenty presets put them: no shift. The default."),
    ("1.2", "1.2",
     "A smaller machine: thinner and brighter, like a child's voice."),
    ("1.45", "1.45",
     "Nearly half again: a chipmunk machine."),
    ("2.0", "2.0",
     "Double: every band an octave up -- a tiny, whistling machine. The top "
     "band may run out of carrier harmonics and the words thin out."),
)

_BREATH_CHOICES = (
    ("Off", "off",
     "A clean carrier: the classic machine, with no air on it."),
    ("Light", "light",
     "A little noise on the carrier: the machine sounds like it is in a room, "
     "not a test tone."),
    ("Medium", "medium",
     "A clear breath behind the words: the carrier hisses and the formants "
     "stay where they are."),
    ("Heavy", "heavy",
     "Half noise: a whispered machine that still has a pitch. The Noise wave "
     "is the full whisper, with no pitch at all."),
)

_MIX_CHOICES = (
    ("60%", "60",
     "Mostly your own voice with the machine behind it: the words stay very "
     "clear."),
    ("75%", "75",
     "The machine is forward, but your own voice still leads."),
    ("85%", "85",
     "The machine leads; your consonants are still audible under it."),
    ("90%", "90",
     "Nearly all machine: the classic balance, and the default."),
    ("100%", "100",
     "The machine alone: no dry voice at all, the clearest robot and the "
     "hardest to understand."),
)

#: The mix, shift and breath values a stored id turns into. A saved setting is
#: a short id, never a float, for the same reason the other boards' are.
_MIX = {"60": 0.60, "75": 0.75, "85": 0.85, "90": 0.90, "100": 1.0}
_SHIFT = {"0.5": 0.5, "0.7": 0.7, "0.85": 0.85, "1.0": 1.0,
          "1.2": 1.2, "1.45": 1.45, "2.0": 2.0}
_BREATH = {"off": 0.0, "light": 0.12, "medium": 0.25, "heavy": 0.45}

#: The board itself. A row is ``(id, menu name, what Tab reads on the row's
#: line, choices, default)`` and a choice is ``(menu name, stored value, what
#: Tab reads on that choice)``.
PARAMS = (
    ("mode", "Mode",
     "What the carrier is. Current keeps the classic 110 Hz machine and lets "
     "Wave, Formant Shift, Breath and Mix shape it. Tune Note locks the "
     "carrier to Root + Octave, so every word comes out on that one note -- "
     "this is the tuner, and it is deliberately monotone. Interval adds one "
     "degree of Scale to that note, for a fixed harmony or a drone in a key.",
     (("Current", "current",
       "The classic vocoder: a 110 Hz carrier, with Wave, Formant Shift and "
       "Breath shaping it. Root, Octave, Scale and Interval are hidden and do "
       "nothing."),
      ("Tune Note", "tune",
       "The carrier is one note, chosen by Root and Octave: speech comes out "
       "sung on that note, whatever pitch the microphone heard. A Noise "
       "carrier is switched to Saw, because there is no note to lock."),
      ("Interval", "interval",
       "The carrier is one scale degree above or below the Root: a fixed "
       "second voice or a drone in the chosen key. Scale decides whether a "
       "third is major or minor; Interval chooses the degree.")),
     "current"),
    ("root", "Root",
     "The note the carrier is tuned to in Tune Note and Interval. Twelve "
     "semitones from C to B; the Octave row moves the whole note up or down. "
     "It does nothing in Current, which is fixed at 110 Hz, so the row is "
     "hidden there.",
     _ROOT_CHOICES,
     "C"),
    ("octave", "Octave",
     "Which octave the Root is in, 1 to 6. C3 is 130.81 Hz, the middle of a "
     "voice; C1 is 32.70 Hz, below the vocoder's first band, so only the "
     "carrier's harmonics speak. The carrier has to reach 4.2 kHz, so the top "
     "octave is bright and thin. Hidden in Current.",
     _OCTAVE_CHOICES,
     "3"),
    ("scale", "Scale",
     "Which scale the Interval is spelled in. Chromatic uses the plain size of "
     "each interval; a key makes a third, sixth and seventh major or minor to "
     "stay in it. It does nothing in Current or Tune Note -- one note has no "
     "scale -- so the row is hidden there.",
     _SCALE_CHOICES,
     "major"),
    ("interval", "Interval",
     "Which scale degree above or below the Root the carrier moves to, in "
     "Interval mode only. The degree is spelled by Scale: a Third is major in "
     "a major key and minor in a minor one. Off leaves the carrier on the "
     "Root. Hidden in Current and Tune Note.",
     _INTERVAL_CHOICES,
     "off"),
    ("wave", "Wave",
     "The carrier's waveform. Saw is every harmonic, the classic; Square is "
     "hollow (odd harmonics); Pulse is thinner; Stack is a saw plus its "
     "octave; Noise has no pitch, so it is a whisper that keeps the words. "
     "Noise is refused in Tune Note and Interval.",
     _WAVE_CHOICES,
     "saw"),
    ("shift", "Formant Shift",
     "Where the vocoder's six bands sit on the carrier, as a multiplier of "
     "their own frequencies: below 1 is a bigger machine (the words lower and "
     "broader), above 1 a smaller one (thinner and brighter). It does not "
     "transpose the voice; it moves the filters.",
     _SHIFT_CHOICES,
     "1.0"),
    ("breath", "Breath",
     "How much noise rides on the carrier, so the machine sounds like it has "
     "air rather than a perfect oscillator. It is noise on the carrier, not on "
     "your voice: the words keep their shape and the pitch keeps its centre.",
     _BREATH_CHOICES,
     "off"),
    ("mix", "Mix",
     "How much of the vocoder replaces the voice. 100% is the machine alone; "
     "below that your dry voice is mixed back in, which keeps consonants and "
     "lets the machine sit inside the words rather than in front of them.",
     _MIX_CHOICES,
     "90"),
)

#: Bumped by every change, so a running line knows its processor is stale.
_revision = 0


def param(param_id):
    """One row's table entry, for a menu that is building the page."""
    return boards.param(PARAMS, param_id)


def choices(param_id):
    """A row's choices, as the menu builds them."""
    return boards.choices(PARAMS, param_id)


def values():
    """The board as it is set, every row filled in from its default.

    A saved board that lost a row (a board that grew one since the setting was
    written) reads the row's default rather than failing. A board saved with
    Noise in a mode that cannot use it reads Saw: a locked note and a noise
    carrier cannot both be true, and the note is the part the player asked for.
    """
    board = boards.read(PARAMS, options.get(_OPTION, {}))
    if board["mode"] != "current" and board["wave"] == "noise":
        board["wave"] = "saw"
    return board


def row_visible(param_id, board):
    """Whether a row is shown for the mode the board is in.

    Current owns no Root, Octave, Scale or Interval (the carrier is fixed at
    110 Hz), and Tune Note owns no Scale or Interval (one note is not a key).
    The page is generated, so it hides the rows here rather than carrying a
    second list of which mode shows what.
    """
    mode = board["mode"]
    if param_id in ("root", "octave"):
        return mode != "current"
    if param_id in ("scale", "interval"):
        return mode == "interval"
    return True


def rows():
    """The rows the board page shows, in table order."""
    board = values()
    return tuple(row for row in PARAMS if row_visible(row[0], board))


def set_value(param_id, value):
    """Set one row, save the board, and tell the running line to rebuild."""
    global _revision
    row = param(param_id)
    if row is None:
        return None
    chosen = boards.choice_value(row[3], value, row[4])
    board = values()
    board[param_id] = chosen
    # Noise cannot carry a locked note. A mode that locks one takes the
    # waveform with it in the same save (one revision, one crossfade), and a
    # request for Noise in that mode is answered with Saw rather than saved
    # into a board that would immediately have to lie about it.
    if param_id == "mode" and chosen != "current" and board["wave"] == "noise":
        board["wave"] = "saw"
    if param_id == "wave" and chosen == "noise" and board["mode"] != "current":
        board["wave"] = "saw"
        chosen = "saw"
    options.set(_OPTION, board)
    _revision += 1
    return chosen


def refusal(param_id, wanted, chosen):
    """Why a chosen value was answered with another one, for the menu to say.

    The only refusal the board has is Noise in a mode that locks a note. The
    menu speaks this and the value it fell back to in the same line, so the
    player is never left wondering why the press did not do what it said.
    """
    if param_id == "wave" and wanted == "noise" and chosen != "noise":
        return ("Noise has no note to lock, so Tune Note and Interval refuse "
                "it. The carrier stays Saw.")
    return ""


def default_label(param_id):
    """What this row's own default is called in the menu's words."""
    return boards.default_label(PARAMS, param_id)


def clear_value(param_id):
    """One row back to the value the board opens with, and one revision."""
    global _revision
    if param(param_id) is None:
        return None
    board = values()
    board.pop(param_id, None)
    options.set(_OPTION, board)
    _revision += 1
    return values()[param_id]


def revision():
    """How many changes the board has had; the line rebuilds when it moves."""
    return _revision


def choice_label(param_id, value=None):
    """A row's chosen choice in the menu's words: 'Tune Note', 'Saw'."""
    board = values()
    if value is not None:
        board[param_id] = value
    return boards.label(PARAMS, board, param_id)


def choice_note(param_id, value):
    """What Tab reads on one choice of one row."""
    return boards.note(PARAMS, param_id, value)


def _root_pc(root):
    """The pitch class of a root name, or 0 for anything unexpected."""
    return _NOTE_PCS.get(str(root), 0)


def note_hz(root, octave):
    """The frequency of Root + Octave, in equal temperament from A4 = 440.

    C3 is 130.81 Hz and C4 is middle C. The formula is the MIDI one every other
    note reader in the project uses, so a note played on the piano and a note
    locked here agree.
    """
    try:
        octave_number = int(octave)
    except (TypeError, ValueError):
        octave_number = 3
    midi = 12 * (octave_number + 1) + _root_pc(root)
    return 440.0 * 2.0 ** ((midi - 69) / 12.0)


def interval_semitones(scale_id, root, interval_id):
    """How far an interval moves the carrier, in semitones.

    On Chromatic there is no key, so the plain size is used (a third is four
    semitones up and three down). On a key the anchor is snapped to the nearest
    note the scale has, in the direction the anchor asked for; a tie goes to the
    lower note. That is what makes a third major over one root and minor over
    another, and it is the same rule the Harmony menu's bass copies follow, so a
    player moving between the two menus hears the same interval.
    """
    anchor = _INTERVAL_SEMITONES.get(interval_id, 0)
    if anchor == 0:
        return 0
    classes = autotune.scale_classes(scale_id)
    if not classes:
        return anchor
    root_pc = _root_pc(root)
    target = root_pc + anchor
    ladder = sorted(pc + 12 * octave
                    for octave in range(-2, 3)
                    for pc in set(classes) | {root_pc})
    best = min(ladder, key=lambda note: (abs(note - target), note))
    return best - root_pc


def carrier_hz(board=None):
    """The carrier's frequency for the board as it is set, in hertz."""
    board = values() if board is None else board
    if board["mode"] == "current":
        return CARRIER_HZ
    hz = note_hz(board["root"], board["octave"])
    if board["mode"] == "interval":
        hz *= 2.0 ** (interval_semitones(board["scale"], board["root"],
                                         board["interval"]) / 12.0)
    return hz


def summary():
    """The board in a few words, for the page's title and the family line."""
    board = values()
    mode = board["mode"]
    if mode == "current":
        return f"{_MODE_LABELS[mode]} {CARRIER_HZ:g} Hz"
    note = f"{board['root']}{board['octave']}"
    if mode == "interval" and board["interval"] != "off":
        return (f"Interval {note} + {_INTERVAL_LABELS[board['interval']]} "
                f"({_SCALE_LABELS.get(board['scale'], 'Major')})")
    hz = note_hz(board["root"], board["octave"])
    return f"{_MODE_LABELS[mode]} {note} ({hz:.2f} Hz)"


def note():
    """What Tab reads on the board's line in the Vocoder Lab family."""
    return ("Your own vocoder: the board under this line sets the carrier's "
            f"mode and note, waveform, band-bank shift, breath and mix. As it "
            f"is now: {summary()}, "
            f"{choice_label('wave').lower()} wave, "
            f"{choice_label('shift')} shift, "
            f"{choice_label('breath').lower()} breath, "
            f"{choice_label('mix')} mix. It is the same six-band bank the "
            "twenty presets here use, with every part of the carrier exposed, "
            "and every row lands on the next 20 millisecond frame.")


def spec():
    """The board as the processor's own spec: the table, turned into numbers."""
    board = values()
    return dict(hp=90.0, lp=7500.0, drive=1.0, shape=None, bias=0.0,
                gate=0.004, out=1.25,
                vocoder=(2.6, _MIX[board["mix"]], carrier_hz(board),
                         board["wave"], _SHIFT[board["shift"]],
                         _BREATH[board["breath"]]))
