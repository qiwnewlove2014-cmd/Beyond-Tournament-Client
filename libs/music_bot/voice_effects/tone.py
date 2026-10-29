"""The channel strip one voice effect carries: four bands, two cuts, a de-esser
and a mixer's controls.

Why this exists
---------------
The shelf is a hundred and forty-four finished sounds, and a finished sound is
somebody else's taste. A voice through `Studio Condenser` is close and
levelled, and it is still not *this* desk's condenser: one player wants less
mud under the words, another wants the top opened, and neither of them wants
to open a whole board and rebuild the effect to get there. So every preset
carries four bands of its own -- Low, Body, Presence and Air -- and the bands
are read on top of that preset and nothing else.

**The strip belongs to the preset, not to the voice.** A player who brightens
`Choir` has not asked for a brighter `Doom`, and a single shared control would
make every choice of pedal move the sound it was tuned to. The setting is one
saved dict, preset id -> the row that preset moved, so a preset nobody has
touched costs one missing key and nothing else.

**Flat is not a filter.** A preset whose four bands are all 0 dB, whose cuts
are off, whose mix is fully wet and whose level is 0 dB is handed to the
processor
as no strip at all: `dsp._ToneStack` returns the frame untouched, so every
preset that has never been equalised is byte-for-byte the pedal it was (the
whole shelf was tuned by ear, and this feature must not re-tune it by
accident). The first decibel a player moves is the first sample of DSP this
costs.

**Four bands, not three or thirty.** A voice's useful tone is at the ends and
at two places in the middle: the chest (a low shelf), the mud (a bell around
400 Hz), the words (a bell around 2.8 kHz) and the breath on top (a high
shelf). Three would put both middles behind one knob -- a telephone with a
tone control -- and thirty is a mixing console, which is the Music Bot's song
EQ, not a voice in a room.

**Two cuts and a de-esser, because the ends are where a preset goes wrong.**
A low cut was the first thing the strip had; a high cut is its other end, and
without it the only way to make a voice smaller is to turn `Air` down, which
leaves the fizz above the shelf in place. The de-esser is the one block that is
not a shape but a response: `Presence` and `Air` lift the band a `s` lives in,
and a fixed filter can only choose between a dull voice and a spitting one. It
is one row -- how much of the `s` to take -- because the detector's threshold
is a property of speech rather than a setting, and a second knob would only be
a second thing to mis-set on a voice.

**The controls around the bands are a mixer's, not an equaliser's.** A
low cut takes the rumble, the desk thump and the plosive off the bottom before
the bands see it -- it is the first thing a mixer reaches for on a voice, and
it is the one thing a shelf at 120 Hz cannot do. A mix decides how much of the
pedal a listener hears at all, so `Fuzz` at 40 percent is a colour on the
voice instead of a costume over it. A level is the fader that matches this
preset to the one beside it, because `Fuzz` and `Studio Condenser` do not
leave at the same volume. A compressor is the last thing a voice needs and the
first thing a mixer misses: it is what makes a quiet sentence and a loud one
arrive at the same distance, which neither a filter nor a fader can do. They
are saved with the bands in the same row, because they are all answers to one
question: how does THIS effect sound on MY voice.

**The compressor is after the bands and before the fader.** The bands decide
what the compressor hears, so a chest pushed up is held as firmly as the
words -- that is the order a console strip has, and it is also why the level
row comes after it: the fader is the make-up gain, and the meter says how far
to bring it up. It is a feed-forward compressor with a soft knee and a
high-passed detector (`dsp._Compressor`): the knee keeps a band from switching
on and off around the threshold, and the detector's corner at 120 Hz stops a
plosive or a desk thump from ducking the whole sentence, which is the one
mistake a voice compressor usually makes.

Where it sits in the chain
--------------------------
`dsp._EffectProcessor` applies the cuts, the bands, the de-esser, the
compressor and the level after the preset's own blocks and before the output
trim and the peak ceiling, so a band pushed up or a fader pushed high is
caught by `PEAK_CEILING` like any other loud pedal, and a preset that was not
touched takes no new code path at all.
The mix is the one control that is not in that stage: it blends the frame that
entered the effect with the one the chain made (`_EffectProcessor._blend`), so
a pedal at 40 percent is heard *beside* the voice rather than on it, and the
bands then shape the blend rather than the wet alone.

The stage glides a moved band over a few frames (`dsp._ToneStack.GLIDE_DB`)
rather than jumping, because a filter whose coefficients step is a click; a
slider press is the only thing that moves a band, and a press that lands over
five frames still sounds immediate. The cut crossfades the same way, and the
level and the mix ramp across the frame they change on.

How a player sets it
--------------------
The `Tone:` line on the Voice Effect page (and on each board's own page) opens
`menu_pages.voice_effect._VoiceToneSlider`, which is one page of thirteen rows
in the order a mixer would put them: Low Cut, Low, Body, Presence, Air,
De-ess, High Cut, Mix, Comp, Ratio, Attack, Release, Level. Tab switches rows,
Up and Down move the row, Page Up and Page Down move it further, Home puts the
row back to what the preset opens with, `R` puts the whole strip back, `M`
speaks the level of the last frame that went through and how far the
compressor pulled it, and Enter keeps everything. It is the same keyboard
shape as the Music Bot's own Bass/Mid/Treble slider, and every press is
spoken, so a player who cannot see the page can still hear exactly what the
row is doing.

Rules: .agents/skills/music_bot_integration/references/voice-tone.md.
"""

import time

from ... import options

#: The saved setting. One dict: preset id -> ``{row id: value}``. A value that
#: is what the preset opens with is absent rather than saved, so the file
#: carries only what a player actually moved and "flat" is the state of a key
#: that is not there.
_OPTION = "music_bot_voice_tone"

#: How far either way a tone band may be moved, in decibels.
MAX_DB = 12

#: How far either way the preset's own level may be moved, in decibels. The
#: same range as a band: enough to match a quiet pedal to a loud one, not
#: enough to make the ceiling do the mixing.
MAX_LEVEL_DB = 12

#: The low cut's stops, in hertz; 0 is off. Six stops rather than a typed
#: number, because a cut is chosen by what it takes out of the room, not by a
#: figure a player reads off a meter -- 80 and 100 are the two that matter and
#: the rest bracket them for a deep voice and a thin one.
CUT_FREQS = (0, 60, 80, 100, 120, 160)

#: The high cut's stops, in hertz; 0 is off. The other end of the same idea:
#: every corner here is above the words' clarity, so a stop turns the top of a
#: shaper down without taking the words with it. 4 kHz is a telephone and
#: 16 kHz is a top no ear in a room hears but a sibilant still does.
HIGH_CUT_FREQS = (0, 16000, 12000, 9000, 7000, 5500, 4000)

#: What the de-esser may take off at most, in decibels; 0 is off. One row and
#: not a threshold plus a depth: the detector's threshold is fixed at the level
#: a sibilant crosses, and the choice a player is actually making is how much
#: of the `s` they hear, not where the machine decides it starts.
DEESS_AMOUNTS = (0, 3, 6, 9, 12)

#: The mix's step, in percent, and the step Page Up takes.
MIX_STEP = 5
MIX_PAGE = 25

#: What a preset opens with, for the row ids that are not a band.
DEFAULT_CUT = 0
DEFAULT_HIGH_CUT = 0
DEFAULT_DEESS = 0
DEFAULT_MIX = 100
DEFAULT_LEVEL = 0
DEFAULT_COMP = 0
DEFAULT_RATIO = 4
DEFAULT_ATTACK = 10
DEFAULT_RELEASE = 250

#: The compressor's threshold stops, in decibels; 0 is off. A voice is
#: compressed from the bottom up, so the ladder walks from where a quiet
#: sentence already sits to where only the peaks cross: a player turns the row
#: up until the meter starts moving rather than reading a figure off a dial.
COMP_THRESHOLDS = (0, -36, -30, -24, -18, -12, -6)

#: The ratios the row offers, as the number before ": one". 2 is a level ride
#: and 20 is nearly a limiter; 4 is where a spoken voice is usually sat.
COMP_RATIOS = (2, 3, 4, 6, 8, 12, 20)

#: The attack ladder, in milliseconds: how quickly the compressor leans in.
#: 1 ms catches a consonant and 60 ms lets the syllable through first.
COMP_ATTACKS = (1, 3, 10, 30, 60)

#: The release ladder, in milliseconds: how quickly it lets go. 60 ms pumps
#: on every word and 1000 ms rides a whole sentence.
COMP_RELEASES = (60, 120, 250, 500, 1000)

#: The compressor's whole setting when nothing is compressed.
COMP_OFF = (DEFAULT_COMP, DEFAULT_RATIO, DEFAULT_ATTACK, DEFAULT_RELEASE)

#: The four bands, in the order Tab walks them: (id, menu name, centre in Hz,
#: which filter, Q, what the band is for). The two ends are shelves -- they move
#: everything past their corner -- and the two middles are bells, which move
#: only the band around their centre. That is the difference between "less top"
#: and "less hiss on the s", and it is why four bands are not two.
BANDS = (
    ("low", "Low", 120.0, "lowshelf", 0.0,
     "the chest and the room under the voice; a low shelf at 120 hertz"),
    ("body", "Body", 400.0, "bell", 0.9,
     "where a voice turns muddy and boxy; a bell at 400 hertz"),
    ("presence", "Presence", 2800.0, "bell", 1.0,
     "the clarity of the words, and the edge on a consonant; a bell at "
     "2800 hertz"),
    ("air", "Air", 9000.0, "highshelf", 0.0,
     "the breath on top of the voice; a high shelf at 9000 hertz"),
)

#: The band ids, in the same order.
BAND_IDS = tuple(row[0] for row in BANDS)

#: Every band at 0 dB, as the processor reads them.
FLAT = tuple(0 for _ in BANDS)

#: The whole strip in the order one page walks it: (row id, menu name, kind).
#: The low cut comes first because it is what the bands see first; the bands
#: are the shape; the de-esser is the response to what the presence and the air
#: bring out; the high cut is where the top stops; the mix and the compressor
#: are what the sound does once it has a shape; and the level comes last
#: because it is the fader after all of it.
ROWS = (
    ("cut", "Low Cut", "cut"),
    ("low", "Low", "db"),
    ("body", "Body", "db"),
    ("presence", "Presence", "db"),
    ("air", "Air", "db"),
    ("deess", "De-ess", "deess"),
    ("highcut", "High Cut", "highcut"),
    ("mix", "Mix", "mix"),
    ("comp", "Comp", "comp"),
    ("ratio", "Ratio", "ratio"),
    ("attack", "Attack", "attack"),
    ("release", "Release", "release"),
    ("level", "Level", "level"),
)

#: The row ids, in the same order.
ROW_IDS = tuple(row[0] for row in ROWS)

#: The rows that are a ladder of named stops rather than a figure that moves
#: one at a time. Up and Down walk the ladder, so a value the DSP is not built
#: for cannot be typed into it -- and so "80 hertz" and "four to one" can be
#: spoken as choices instead of as numbers.
LADDERS = {
    "cut": CUT_FREQS,
    "highcut": HIGH_CUT_FREQS,
    "deess": DEESS_AMOUNTS,
    "comp": COMP_THRESHOLDS,
    "ratio": COMP_RATIOS,
    "attack": COMP_ATTACKS,
    "release": COMP_RELEASES,
}


def band(band_id):
    """One band's row, or None for an id the table does not offer."""
    for row in BANDS:
        if row[0] == band_id:
            return row
    return None


def row(row_id):
    """One strip row: (id, name, kind), or None for an id not in the table."""
    for entry in ROWS:
        if entry[0] == row_id:
            return entry
    return None


def _clamp(value):
    """A whole number of decibels inside the range a band can be moved."""
    try:
        number = int(round(float(value)))
    except (TypeError, ValueError):
        return 0
    return max(-MAX_DB, min(MAX_DB, number))


def _clamp_level(value):
    """A whole number of decibels inside the range the level can be moved."""
    try:
        number = int(round(float(value)))
    except (TypeError, ValueError):
        return 0
    return max(-MAX_LEVEL_DB, min(MAX_LEVEL_DB, number))


def _clamp_cut(hz):
    """A stop on the cut's ladder -- an offered corner, not an arbitrary one."""
    try:
        number = int(round(float(hz)))
    except (TypeError, ValueError):
        return 0
    if number <= 0:
        return 0
    return min(CUT_FREQS[1:], key=lambda stop: abs(stop - number))


def _clamp_high_cut(hz):
    """A stop on the high cut's ladder -- an offered corner, not an arbitrary one."""
    try:
        number = int(round(float(hz)))
    except (TypeError, ValueError):
        return 0
    if number <= 0:
        return 0
    return min(HIGH_CUT_FREQS[1:], key=lambda stop: abs(stop - number))


def _clamp_deess(decibels):
    """A stop on the de-esser's ladder, 0 being off."""
    try:
        number = float(decibels)
    except (TypeError, ValueError):
        return 0
    if number <= 0:
        return 0
    return min(DEESS_AMOUNTS[1:], key=lambda stop: abs(stop - number))


def _clamp_mix(percent):
    """A whole percent, on the mix's five-percent steps."""
    try:
        number = int(round(float(percent)))
    except (TypeError, ValueError):
        return DEFAULT_MIX
    number = max(0, min(100, number))
    return int(round(number / MIX_STEP)) * MIX_STEP


def _clamp_comp(decibels):
    """A stop on the compressor's threshold ladder, 0 being off.

    Anything at or above 0 dB is off rather than a very high threshold: a
    threshold above the loudest a frame can be is not a setting, it is the
    absence of one, and it must read back as the state the file stores.
    """
    try:
        number = float(decibels)
    except (TypeError, ValueError):
        return DEFAULT_COMP
    if number >= 0:
        return 0
    return min(COMP_THRESHOLDS[1:], key=lambda stop: abs(stop - number))


def _clamp_choice(value, ladder):
    """The nearest stop on a ladder of positive choices."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return ladder[0]
    return min(ladder, key=lambda stop: abs(stop - number))


def _saved():
    """The whole saved setting, whatever shape an older file left it in."""
    stored = options.get(_OPTION, {})
    return stored if isinstance(stored, dict) else {}


def _row_of(preset_id):
    """One preset's saved row, or an empty dict when it has never been moved."""
    row_value = _saved().get(str(preset_id))
    return row_value if isinstance(row_value, dict) else {}


def values(preset_id):
    """One preset's four bands as numbers: an untouched preset is all zeros."""
    stored = _row_of(preset_id)
    return {band_id: _clamp(stored.get(band_id, 0)) for band_id in BAND_IDS}


def gains(preset_id):
    """The same four bands as a tuple, in band order -- what the DSP reads.

    Read on the capture thread once per frame, so it is the cheap shape: no
    dictionaries built when the preset has never been touched, and one tuple
    when it has.
    """
    stored = _row_of(preset_id)
    if not stored:
        return FLAT
    return tuple(_clamp(stored.get(band_id, 0)) for band_id in BAND_IDS)


def cut(preset_id):
    """The preset's low cut, in hertz; 0 is off."""
    return _clamp_cut(_row_of(preset_id).get("cut", DEFAULT_CUT))


def high_cut(preset_id):
    """The preset's high cut, in hertz; 0 is off."""
    return _clamp_high_cut(_row_of(preset_id).get("highcut",
                                                DEFAULT_HIGH_CUT))


def deess(preset_id):
    """How much the de-esser may take off the `s`, in decibels; 0 is off."""
    return _clamp_deess(_row_of(preset_id).get("deess", DEFAULT_DEESS))


def mix(preset_id):
    """How much of the effect is heard, in percent: 100 is all of it."""
    return _clamp_mix(_row_of(preset_id).get("mix", DEFAULT_MIX))


def level(preset_id):
    """The preset's own level, in decibels either way."""
    return _clamp_level(_row_of(preset_id).get("level", DEFAULT_LEVEL))


def _comp_of(stored):
    """One preset's saved row as the compressor tuple the DSP reads."""
    return (_clamp_comp(stored.get("comp", DEFAULT_COMP)),
            _clamp_choice(stored.get("ratio", DEFAULT_RATIO), COMP_RATIOS),
            _clamp_choice(stored.get("attack", DEFAULT_ATTACK), COMP_ATTACKS),
            _clamp_choice(stored.get("release", DEFAULT_RELEASE),
                          COMP_RELEASES))


def compressor(preset_id):
    """The whole compressor as `(threshold, ratio, attack, release)`."""
    return _comp_of(_row_of(preset_id))


def strip(preset_id):
    """The whole strip in one read, for the capture thread.

    `(bands, cut hertz, high cut hertz, de-ess decibels, mix percent,
    compressor, level decibels)`, the compressor being its own `(threshold,
    ratio, attack, release)` -- one dictionary lookup and one tuple per frame,
    rather than thirteen lookups and twelve tuples.
    """
    stored = _row_of(preset_id)
    if not stored:
        return (FLAT, DEFAULT_CUT, DEFAULT_HIGH_CUT, DEFAULT_DEESS,
                DEFAULT_MIX, COMP_OFF, DEFAULT_LEVEL)
    return (tuple(_clamp(stored.get(band_id, 0)) for band_id in BAND_IDS),
            _clamp_cut(stored.get("cut", DEFAULT_CUT)),
            _clamp_high_cut(stored.get("highcut", DEFAULT_HIGH_CUT)),
            _clamp_deess(stored.get("deess", DEFAULT_DEESS)),
            _clamp_mix(stored.get("mix", DEFAULT_MIX)),
            _comp_of(stored),
            _clamp_level(stored.get("level", DEFAULT_LEVEL)))


def state(preset_id):
    """Every row of one preset's strip, with what it opens with filled in."""
    (bands, cut_hz, high_cut_hz, deess_db, mix_percent, comp,
     level_db) = strip(preset_id)
    out = dict(zip(BAND_IDS, bands))
    out["cut"] = cut_hz
    out["highcut"] = high_cut_hz
    out["deess"] = deess_db
    out["mix"] = mix_percent
    out["comp"], out["ratio"], out["attack"], out["release"] = comp
    out["level"] = level_db
    return out


#: What each kind of row is when the preset has never been touched.
_DEFAULTS = {
    "cut": DEFAULT_CUT,
    "highcut": DEFAULT_HIGH_CUT,
    "deess": DEFAULT_DEESS,
    "mix": DEFAULT_MIX,
    "comp": DEFAULT_COMP,
    "ratio": DEFAULT_RATIO,
    "attack": DEFAULT_ATTACK,
    "release": DEFAULT_RELEASE,
    "level": DEFAULT_LEVEL,
}


def default_of(row_id):
    """What one row is when the preset has never been touched."""
    entry = row(row_id)
    if entry is None:
        return None
    return _DEFAULTS.get(entry[2], 0)


def has_tone(preset_id):
    """Whether this preset's strip has anything moved at all.

    Named for the tone because that is what a player sees it as -- the mark on
    the list says `(tuned)` -- but it is the whole strip: a preset whose level
    was matched to its neighbour is as much this player's as one whose Body
    was cut. The compressor counts from its threshold, not from its ratio: a
    ratio left at 8 while the compressor is off changes nothing a listener
    could hear, and the mark is a claim about the sound.
    """
    (bands, cut_hz, high_cut_hz, deess_db, mix_percent, comp,
     level_db) = strip(preset_id)
    return (any(bands) or bool(cut_hz) or bool(high_cut_hz) or bool(deess_db)
            or mix_percent != DEFAULT_MIX
            or bool(comp[0]) or bool(level_db))


def _store(preset_id, key, value, empty, save):
    """Save one row's value, dropping the key when it is the preset's own."""
    preset = str(preset_id)
    stored = dict(_saved())
    row_value = dict(stored.get(preset) or {})
    if value != empty:
        row_value[key] = value
    else:
        row_value.pop(key, None)
    if row_value:
        stored[preset] = row_value
    else:
        stored.pop(preset, None)
    options.set(_OPTION, stored, autosave=save)
    return value


def set_band(preset_id, band_id, decibels, save=True):
    """Move one band of one preset, and hand back what was stored.

    A band moved back to 0 dB is removed rather than written as a zero: flat is
    the absence of a key, which is what makes `gains` answer `FLAT` without
    building anything for the presets nobody has equalised.

    `save=False` keeps the setting live in memory (the capture thread reads the
    same dict) without rewriting and re-encrypting the settings file -- what a
    slider does while it is being moved. The page saves once, when it closes.
    """
    if band(band_id) is None:
        return None
    return _store(preset_id, band_id, _clamp(decibels), 0, save)


def set_cut(preset_id, hertz, save=True):
    """Move the preset's low cut to a stop on the ladder, 0 being off."""
    return _store(preset_id, "cut", _clamp_cut(hertz), DEFAULT_CUT, save)


def set_high_cut(preset_id, hertz, save=True):
    """Move the preset's high cut to a stop on the ladder, 0 being off."""
    return _store(preset_id, "highcut", _clamp_high_cut(hertz),
                  DEFAULT_HIGH_CUT, save)


def set_deess(preset_id, decibels, save=True):
    """Set how much of the `s` the de-esser may take, 0 being off."""
    return _store(preset_id, "deess", _clamp_deess(decibels), DEFAULT_DEESS,
                  save)


def set_mix(preset_id, percent, save=True):
    """Set how much of the effect is heard, in percent."""
    return _store(preset_id, "mix", _clamp_mix(percent), DEFAULT_MIX, save)


def set_level(preset_id, decibels, save=True):
    """Set the preset's own level, in decibels either way."""
    return _store(preset_id, "level", _clamp_level(decibels), DEFAULT_LEVEL,
                  save)


def set_comp(preset_id, threshold_db, save=True):
    """Move the compressor's threshold to a stop on its ladder, 0 being off."""
    return _store(preset_id, "comp", _clamp_comp(threshold_db), DEFAULT_COMP,
                  save)


def set_ratio(preset_id, ratio, save=True):
    """Set how hard the compressor leans above its threshold."""
    return _store(preset_id, "ratio", _clamp_choice(ratio, COMP_RATIOS),
                  DEFAULT_RATIO, save)


def set_attack(preset_id, milliseconds, save=True):
    """Set how quickly the compressor leans in, in milliseconds."""
    return _store(preset_id, "attack", _clamp_choice(milliseconds,
                                                     COMP_ATTACKS),
                  DEFAULT_ATTACK, save)


def set_release(preset_id, milliseconds, save=True):
    """Set how quickly the compressor lets go, in milliseconds."""
    return _store(preset_id, "release", _clamp_choice(milliseconds,
                                                     COMP_RELEASES),
                  DEFAULT_RELEASE, save)


def set_row(preset_id, row_id, value, save=True):
    """Move one row of the strip, whatever kind of row it is."""
    entry = row(row_id)
    if entry is None:
        return None
    kind = entry[2]
    if kind == "cut":
        return set_cut(preset_id, value, save)
    if kind == "highcut":
        return set_high_cut(preset_id, value, save)
    if kind == "deess":
        return set_deess(preset_id, value, save)
    if kind == "mix":
        return set_mix(preset_id, value, save)
    if kind == "comp":
        return set_comp(preset_id, value, save)
    if kind == "ratio":
        return set_ratio(preset_id, value, save)
    if kind == "attack":
        return set_attack(preset_id, value, save)
    if kind == "release":
        return set_release(preset_id, value, save)
    if kind == "level":
        return set_level(preset_id, value, save)
    return set_band(preset_id, row_id, value, save)


def reset(preset_id, save=True):
    """The whole strip of one preset back, and the preset's key forgotten."""
    stored = dict(_saved())
    if stored.pop(str(preset_id), None) is not None:
        options.set(_OPTION, stored, autosave=save)
    return state(preset_id)


def spoken(decibels):
    """A decibel value the way a screen reader should say it."""
    value = _clamp(decibels)
    if not value:
        return "flat"
    return f"{'plus' if value > 0 else 'minus'} {abs(value)} decibels"


def cut_spoken(hertz):
    """The cut's value in words: `off`, or the corner it sits on."""
    value = _clamp_cut(hertz)
    if not value:
        return "off, the voice's own bottom"
    return f"{value} hertz"


def _hz_spoken(hertz):
    """A corner in the words a screen reader can say: `7 kilohertz`."""
    value = int(round(float(hertz)))
    if value >= 1000:
        kilo = value / 1000.0
        if abs(kilo - round(kilo)) < 1e-9:
            return f"{int(round(kilo))} kilohertz"
        return f"{kilo:g} kilohertz"
    return f"{value} hertz"


def high_cut_spoken(hertz):
    """The high cut in words: `off`, or the corner the top stops at."""
    value = _clamp_high_cut(hertz)
    if not value:
        return "off, the voice's own top"
    return f"cutting above {_hz_spoken(value)}"


def deess_spoken(decibels):
    """The de-esser in words: `off`, or how much of the `s` it takes."""
    value = _clamp_deess(decibels)
    if not value:
        return "off, no de-essing"
    return f"up to {value} decibels off the s"


def mix_spoken(percent):
    """The mix in words, naming the two ends rather than a number alone."""
    value = _clamp_mix(percent)
    if value >= 100:
        return "fully wet, the effect alone"
    if value <= 0:
        return "fully dry, the effect silent"
    return f"{value} percent wet"


def level_spoken(decibels):
    """The level in words -- the same `plus 3 decibels` a band uses."""
    return spoken(decibels)


#: How each ratio is said out loud, so "four to one" is not read as "4:1".
_RATIO_WORDS = {2: "two", 3: "three", 4: "four", 6: "six", 8: "eight",
                12: "twelve", 20: "twenty"}


def comp_spoken(decibels):
    """The threshold in words: `off`, or where the compressor starts."""
    value = _clamp_comp(decibels)
    if not value:
        return "off, no compression"
    return f"compressing above minus {abs(value)} decibels"


def ratio_spoken(ratio):
    """The ratio in words -- `four to one`, never `4:1`."""
    value = _clamp_choice(ratio, COMP_RATIOS)
    return f"{_RATIO_WORDS.get(value, value)} to one"


def attack_spoken(milliseconds):
    """The attack in words."""
    return f"{_clamp_choice(milliseconds, COMP_ATTACKS)} milliseconds"


def release_spoken(milliseconds):
    """The release in words."""
    return f"{_clamp_choice(milliseconds, COMP_RELEASES)} milliseconds"


def _signed_db(decibels):
    """A negative figure in words, for rows below zero that are not a band."""
    return f"minus {abs(int(round(float(decibels))))} decibels"


def spoken_row(row_id, value):
    """One row's value in the words its kind asks for."""
    entry = row(row_id)
    kind = entry[2] if entry is not None else "db"
    if kind == "cut":
        return cut_spoken(value)
    if kind == "highcut":
        return high_cut_spoken(value)
    if kind == "deess":
        return deess_spoken(value)
    if kind == "mix":
        return mix_spoken(value)
    if kind == "comp":
        return comp_spoken(value)
    if kind == "ratio":
        return ratio_spoken(value)
    if kind == "attack":
        return attack_spoken(value)
    if kind == "release":
        return release_spoken(value)
    return spoken(value)


def summary(preset_id):
    """The strip in the menu's words: `Flat`, or `Cut 80, Comp 4:1, +3 Low`."""
    (bands, cut_hz, high_cut_hz, deess_db, mix_percent, comp,
     level_db) = strip(preset_id)
    parts = []
    if cut_hz:
        parts.append(f"Cut {cut_hz}")
    parts.extend(f"{value:+d} {entry[1]}"
                 for entry, value in zip(BANDS, bands) if value)
    if deess_db:
        parts.append(f"De-ess {deess_db} dB")
    if high_cut_hz:
        parts.append(f"High Cut {high_cut_hz}")
    if mix_percent != DEFAULT_MIX:
        parts.append(f"Mix {mix_percent}%")
    threshold, ratio, _attack, _release = comp
    if threshold:
        parts.append(f"Comp {threshold} dB {ratio}:1")
    if level_db:
        parts.append(f"Level {level_db:+d} dB")
    return ", ".join(parts) if parts else "Flat"


def description(preset_id):
    """What the strip is doing, in one sentence, for speech and for Tab.

    Flat is said as the pedal it is -- the preset exactly as it was made --
    because "0 decibels on four bands, no cut, fully wet" is not something a
    player asked for.
    """
    (bands, cut_hz, high_cut_hz, deess_db, mix_percent, comp,
     level_db) = strip(preset_id)
    parts = []
    if cut_hz:
        parts.append(f"cut below {cut_hz} hertz")
    parts.extend(f"{spoken(value)} on {entry[1]}"
                 for entry, value in zip(BANDS, bands) if value)
    if deess_db:
        parts.append(f"de-essing up to {deess_db} decibels off the s")
    if high_cut_hz:
        parts.append(f"a high cut above {_hz_spoken(high_cut_hz)}")
    if mix_percent != DEFAULT_MIX:
        parts.append(f"a {mix_percent} percent mix")
    threshold, ratio, attack, release = comp
    if threshold:
        line = f"compression above {_signed_db(threshold)} at {ratio} to one"
        if attack != DEFAULT_ATTACK:
            line += f", {attack} milliseconds of attack"
        if release != DEFAULT_RELEASE:
            line += f", {release} milliseconds of release"
        parts.append(line)
    if level_db:
        parts.append(f"level {spoken(level_db)}")
    if not parts:
        return "Flat: this effect is exactly as it was made."
    return f"This effect, then {', '.join(parts)}."


# -------------------------------------------------------------------- the meter
#
# What a player setting a level cannot see is how loud the frame is that leaves.
# The capture thread measures the shaped frame (after the ceiling and the trim,
# `dsp._EffectProcessor.process`) and leaves the reading here; the page speaks
# it when `M` is pressed. A reading older than `METER_FRESH` is not reported:
# a press that ended leaves a number behind, and a meter that still answers it
# is a meter that lies about the silence between sentences.

#: How long a reading counts as current, in seconds.
METER_FRESH = 2.5

_METER = {}


def _db(value):
    """A linear level as decibels, floored at -60 rather than -infinity."""
    import math
    if value <= 1e-3:
        return -60.0
    return 20.0 * math.log10(value)


def report(preset_id, rms, peak, now=None, reduction=0.0):
    """Record the level of one shaped frame, and how far it was pulled down.

    The reduction is the compressor's, in decibels, measured across the same
    frame the level was measured on -- so a player who turns the threshold
    down reads both numbers off the same moment rather than two different
    ones.
    """
    _METER[str(preset_id)] = (
        round(_db(float(rms)), 1), round(_db(float(peak)), 1),
        time.monotonic() if now is None else float(now),
        round(float(reduction), 1))


def reading(preset_id, now=None):
    """The last frame's `(rms, peak)` in decibels, or None when it is stale."""
    entry = _METER.get(str(preset_id))
    if entry is None:
        return None
    rms_db, peak_db, when = entry[:3]
    moment = time.monotonic() if now is None else float(now)
    if moment - when > METER_FRESH:
        return None
    return rms_db, peak_db


def reduction(preset_id, now=None):
    """How far the compressor pulled the last fresh frame down, in decibels."""
    entry = _METER.get(str(preset_id))
    if entry is None or len(entry) < 4:
        return 0.0
    _rms, _peak, when, pulled = entry
    moment = time.monotonic() if now is None else float(now)
    if moment - when > METER_FRESH:
        return 0.0
    return pulled


def meter_spoken(preset_id, now=None):
    """The meter in words, or the honest answer when nothing is going through."""
    reading_now = reading(preset_id, now)
    if reading_now is None:
        return ("Nothing has gone through this effect just now: press the key "
                "and speak.")
    rms_db, peak_db = reading_now
    said = f"Level {_spoken_db(rms_db)}, peak {_spoken_db(peak_db)}."
    pulled = reduction(preset_id, now)
    if pulled >= 0.5:
        # A compressor that is barely working has nothing worth saying: half a
        # decibel is inside what the ear calls "the same loudness", and a
        # meter that reported it would make every gentle setting sound busy.
        amount = ("less than a decibel" if pulled < 1.0
                  else f"{int(round(pulled))} decibels")
        said += f" The compressor pulled {amount} off it."
    return said


def _spoken_db(decibels):
    """A measured level in words: `minus 18`, `minus 3`, `silence`."""
    value = float(decibels)
    if value <= -60.0:
        return "silence"
    rounded = int(round(value))
    if rounded >= 0:
        return f"{rounded}"
    return f"minus {abs(rounded)}"
