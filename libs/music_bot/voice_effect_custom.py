"""The Custom Amp: a knob board, one table, and the chain it configures.

Why this exists
---------------
The Voice Effect page is a shelf of finished pedals. A player who wants *their*
amp -- more drive than `Crunch`, less top than `Bright`, a gate between chords
-- has no shelf to pick from, and adding a preset for every combination is how
a menu becomes a catalogue nobody reads. So the Amp & Drive page ends with one
line that opens a board: the same chain the presets use, with the values the
player chose (`voice_effects.spec_for` hands the board's spec to the processor
that every other preset is built by -- there is no second DSP).

**One table drives the whole feature.** `PARAMS` is the board: a row is a knob
-- its name, what Tab says about it, its choices (each with its own Tab text)
and its default. `_build` is the only place a choice becomes a filter setting.
A new knob is one row plus one line in `_build`; a second board (a pedal
builder, an amp's tone stack) is a second table with its own option key, and no
new menu code at all, because `menus._open_custom_effect_menu` walks whatever
table it is handed.

**A change lands on the next frame, not on a rebuild.** The processor is built
once per preset, so a board that changed under a sounding voice has to be
rebuilt or the knobs would do nothing until the player picked another pedal.
`revision()` is that signal: `VoiceEffectLine` reads it beside the preset id and
crossfades into a fresh processor when it moves -- the same fade, and the same
reason, as a preset change.
"""

from .. import options
from . import voice_effect_boards as boards

#: The id the whole chain calls this board by. It sits in the catalogue's Amp &
#: Drive family like a preset, because to every other part of the feature it IS
#: one: `effect_id()` returns it, the line builds a processor for it, and the
#: pedal list marks it.
EFFECT_ID = "custom"

#: The saved board. One dict of knob id to chosen value, beside the preset that
#: selects it (`voice_effects.OPTION`) and for the same reason: a player's amp
#: has to survive a restart.
_OPTION = "music_bot_voice_effect_custom"

#: The board itself. A row is ``(id, menu name, what Tab reads on the knob's
#: own line, choices, default)`` and a choice is ``(menu name, stored value,
#: what Tab reads on that choice)``. Stored values are short ids rather than
#: hertz or gains: a saved setting has to survive the day a filter is retuned,
#: and a player reads "Open", not "6000".
PARAMS = (
    ("drive", "Drive",
     "How hard the amp is pushed. Off leaves the cabinet's colour and the three "
     "tone knobs; Saturated is everything at once. Each step is a gain into "
     "the same shaper, so the sound gets thicker rather than merely louder.",
     (("Off", "off",
       "No shaper at all: what you hear is the cabinet's corner and the tone "
       "knobs. The amp is a colour, not a distortion."),
      ("Edge", "edge",
       "Just breaking up: the loudest syllable takes a little dirt and the "
       "quiet ones stay clean, which is what an amp at the edge of its headroom "
       "does."),
      ("Crunch", "crunch",
       "A cranked small amp: chords grind but still show their notes. The "
       "rhythm-guitar sound, and the default here."),
      ("Drive", "drive",
       "A pedal in front of a crunchy amp: more gain and more compression, and "
       "the notes still come through one at a time."),
      ("High Gain", "high",
       "The lead channel: sustain under the words, a harmonic on every vowel, "
       "and the pick still audible."),
      ("Saturated", "sat",
       "Everything at once: a wall of saturation. The pitch survives and the "
       "consonants turn into splinters -- the heaviest the board goes.")),
     "crunch"),
    ("character", "Character",
     "Which non-linearity the amp has. Clean is no valve at all, Valve is the "
     "asymmetric soft clip an amp makes, Hard is a transistor's square edge and "
     "Fuzz is a badly biased clip -- the same drive sounds like four different "
     "amps through these.",
     (("Clean", "clean",
       "No valve colour: the cabinet and the knobs are the whole amp, so a "
       "quiet voice stays a clean voice, only placed in a room."),
      ("Warm", "warm",
       "A soft knee and a little even-harmonic weight: louder and thicker "
       "without being a distortion. What a valve preamp does before it breaks "
       "up."),
      ("Valve", "valve",
       "A driven valve's asymmetry: the top of the wave rounds off before the "
       "bottom does, which is why an amp sounds like an amp and a clipper does "
       "not."),
      ("Hard", "hard",
       "A transistor's clip: square edges and a bright buzz on the consonants. "
       "The distortion-pedal sound, and the one that cuts through a band."),
      ("Fuzz", "fuzz",
       "A fuzz's hard, badly biased clip: the two halves no longer match, which "
       "is where the splinter and the octave-ish edge come from. The dirtiest "
       "character.")),
     "valve"),
    ("bass", "Bass",
     "Where the low end is cut before the shaper. A driven amp does not want "
     "the desk rumble -- it turns into a flub on every chord -- so this is a "
     "corner, not a shelf: below it the amp simply does not hear.",
     (("Tight", "tight",
       "Sixty hertz: the low strings keep their weight and the room's rumble is "
       "gone. The sound of a tight modern amp."),
      ("Full", "full",
       "Ninety-five hertz: a guitar's low E and a voice's chest both get "
       "through, and the shaper takes both."),
      ("Warm", "warm",
       "A hundred and thirty: the bottom is thinned a little, which keeps a "
       "driven amp from flubbing on a chord."),
      ("Thin", "thin",
       "Two hundred: no bottom at all -- the small-speaker sound a practice amp "
       "makes in a bedroom."),
      ("Telephone", "telephone",
       "Four hundred hertz: nothing below the phone band. For a lo-fi part, "
       "never for a solo.")),
     "full"),
    ("mid", "Mid",
     "One band lifted before the cabinet. Flat is no lift; every other choice "
     "is a resonance, so this knob is what makes an amp bark, honk or cut -- "
     "the difference between a scooped heavy sound and one that sits in a mix.",
     (("Flat", "flat",
       "No band lifted: the shaper and the cabinet are the whole colour. The "
       "right answer for a scooped modern sound."),
      ("Low", "low",
       "Five hundred hertz, the body of a chord: weight without brightness, "
       "for a part under a voice."),
      ("Bark", "bark",
       "Nine hundred hertz, where a guitar's bite lives: the amp pushes back on "
       "every chord."),
      ("Honk", "honk",
       "Thirteen hundred, a small combo's nasal peak: an amp that is too loud "
       "for the room it is in."),
      ("Presence", "presence",
       "Twenty-two hundred: the part of the amp that cuts through a band, on "
       "its own. Bright and forward.")),
     "flat"),
    ("treble", "Treble",
     "The cabinet's top. Every amp here is a cabinet before it is anything "
     "else, and this is how far up it reaches -- from a dark closed box to a "
     "keyboard amp with almost no box at all.",
     (("Dark", "dark",
       "Everything above twenty-four hundred is gone: a cabinet under a "
       "blanket, for a pad or a phone-call part."),
      ("Warm", "warm",
       "A closed-back cabinet, close-miked: no fizz, all midrange. The sound a "
       "four-by-twelve makes three centimetres from the grille."),
      ("Classic", "classic",
       "Four thousand two hundred: the cabinet the fixed presets use. Bright "
       "enough to hear the pick, dark enough not to hiss."),
      ("Open", "open",
       "Six thousand: an open-back cabinet in a room, so the air around the "
       "note comes with the note."),
      ("Bright", "bright",
       "Nine thousand: almost no cabinet -- an amp into a tweeter. A keyboard "
       "amp rather than a guitar one.")),
     "classic"),
    ("gate", "Gate",
     "A noise gate between the notes. High gain on an open microphone is a "
     "feedback loop waiting for the room, and on a guitar it is the hum of the "
     "cable -- the gate closes when the frame is quiet and opens in half a "
     "frame, always with a ramp so it cannot click.",
     (("Off", "off",
       "No gate: the room behind you is amplified too. Only with headphones, or "
       "on a line-in with a quiet pickup."),
      ("Soft", "soft",
       "Closes between phrases and opens on the first syllable: nearly "
       "invisible, and the safe default."),
      ("Firm", "firm",
       "Closes a little sooner, so a driven amp does not breathe between "
       "words."),
      ("Hard", "hard",
       "Shuts the moment you stop: silent between chords, at the price of a "
       "clipped tail on a held syllable.")),
     "soft"),
    ("output", "Output",
     "The level the board hands on, after the cabinet. The line's ceiling is "
     "still above this, so Loud cannot clip a frame -- it is trim, not a "
     "boost's excuse to overload the PA.",
     (("Quiet", "quiet",
       "Six decibels down: for a very hot input, or for a room that does not "
       "want it."),
      ("Half", "half",
       "Three decibels down: a little headroom left for the PA's own sends."),
      ("Unity", "unity",
       "The level the fixed presets use: as loud as the voice, no louder. The "
       "default."),
      ("Loud", "loud",
       "A couple of decibels up: a lead part. The ceiling still catches it, so "
       "the frame keeps its headroom.")),
     "unity"),
)

#: What each character choice does to the shaper: the shape and its bias (the
#: asymmetry -- a valve's clip is not symmetric, a transistor's is).
_CHARACTERS = {
    "clean": (None, 0.0),
    "warm": ("tanh", 0.06),
    "valve": ("tanh", 0.14),
    "hard": ("hard", 0.10),
    "fuzz": ("hard", 0.22),
}

#: The gain into the shaper, in the order the Drive row lists it. Saturated is
#: past the fuzz preset's 22 -- this board's own top.
_DRIVES = {
    "off": 1.0,
    "edge": 2.2,
    "crunch": 4.6,
    "drive": 7.0,
    "high": 12.0,
    "sat": 20.0,
}

#: The input high-pass corner, in hertz.
_BASS = {"tight": 60.0, "full": 95.0, "warm": 130.0, "thin": 200.0,
         "telephone": 400.0}

#: The lifted band: None, or (centre, Q, mix, gain) for the formant block.
_MID = {
    "flat": None,
    "low": (500.0, 1.15, 0.40, 1.6),
    "bark": (900.0, 1.20, 0.42, 1.7),
    "honk": (1300.0, 1.40, 0.45, 1.7),
    "presence": (2200.0, 1.20, 0.36, 1.7),
}

#: The cabinet's corner, in hertz.
_TREBLE = {"dark": 2400.0, "warm": 3200.0, "classic": 4200.0,
           "open": 6000.0, "bright": 9000.0}

#: The gate's RMS floor, as a fraction of full scale.
_GATE = {"off": 0.0, "soft": 0.004, "firm": 0.010, "hard": 0.020}

#: The output trim.
_OUTPUT = {"quiet": 0.60, "half": 0.78, "unity": 0.95, "loud": 1.15}

#: Bumped by every change, so a running line knows its processor is stale.
_revision = 0


def param(param_id):
    """One knob's row, for a menu that is building the page from the table."""
    return boards.param(PARAMS, param_id)


def choices(param_id):
    """The knob's choices, as the menu builds them."""
    return boards.choices(PARAMS, param_id)


def values():
    """The board as it is set, every knob filled in and every value known.

    Read fresh on each call: the menu labels and the frame itself both read
    this, and a saved dict that lost a knob (a board that grew a row since the
    setting was written) is filled from the row's default rather than trusted.
    `boards.read` owns that rule; the options call stays here so a test can
    patch this module's own seam.
    """
    return boards.read(PARAMS, options.get(_OPTION, {}))


def set_value(param_id, value):
    """Set one knob, save the board, and tell the running line to rebuild."""
    global _revision
    row = param(param_id)
    if row is None:
        return None
    chosen = boards.choice_value(row[3], value, row[4])
    board = values()
    board[param_id] = chosen
    options.set(_OPTION, board)
    _revision += 1
    return chosen


def default_label(param_id):
    """What this knob's own default is called in the menu's words."""
    return boards.default_label(PARAMS, param_id)


def clear_value(param_id):
    """One knob back to the value the board opens with, and one revision.

    The `Clear: back to ...` line on that knob's page. It removes the key
    rather than writing the default into it: `values()` fills the hole from
    `PARAMS`, so a cleared knob follows a default that moves tomorrow, the
    same reason the whole board's Clear saves an empty dict.

    Unlike that one, this does not ask: the page already says what it holds
    and any choice above puts a value straight back.
    """
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
    """A knob's chosen choice in the menu's words: 'Crunch', 'Classic'."""
    board = values()
    if value is not None:
        board[param_id] = value
    return boards.label(PARAMS, board, param_id)


def choice_note(param_id, value):
    """What Tab reads on one choice of one knob."""
    return boards.note(PARAMS, param_id, value)


def summary():
    """The board in three words, for the page's title."""
    board = values()
    return ", ".join((choice_label("drive", board["drive"]),
                      choice_label("character", board["character"]),
                      choice_label("treble", board["treble"])))


def note():
    """What Tab reads on the Custom Amp line in the pedal list."""
    return ("Your own amp: the board under this line sets the drive, the "
            f"character, the bass, mid and treble and the gate. As it is now: "
            f"{summary()}, "
            f"{choice_label('bass').lower()} bass, "
            f"{choice_label('mid').lower()} mid, "
            f"{choice_label('gate').lower()} gate, "
            f"{choice_label('output').lower()} output. It is the same chain the "
            "fixed pedals use -- a guitar plugged into the line-in is shaped by "
            "it too -- and every knob lands on the next 20 millisecond frame.")


def spec():
    """The board as the processor's own spec: the table, turned into numbers."""
    board = values()
    shape, bias = _CHARACTERS[board["character"]]
    if board["drive"] == "off":
        # A shaper with unity gain is still a shaper: Off has to mean the
        # cabinet and the tone knobs only, so the shape goes with it.
        shape, bias = None, 0.0
    built = dict(hp=_BASS[board["bass"]],
                 lp=_TREBLE[board["treble"]],
                 drive=_DRIVES[board["drive"]],
                 shape=shape,
                 bias=bias,
                 gate=_GATE[board["gate"]],
                 out=_OUTPUT[board["output"]])
    mid = _MID[board["mid"]]
    if mid is not None:
        built["formant"] = mid
    return built

