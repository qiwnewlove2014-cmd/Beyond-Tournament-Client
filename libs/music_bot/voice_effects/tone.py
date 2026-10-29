"""Four tone bands a player can put on any one of the voice effects.

Why this exists
---------------
The shelf is a hundred and forty-four finished sounds, and a finished sound is
somebody else's taste. A voice through `Studio Condenser` is close and
levelled, and it is still not *this* desk's condenser: one player wants less
mud under the words, another wants the top opened, and neither of them wants
to open a whole board and rebuild the effect to get there. So every preset
carries four bands of its own -- Low, Body, Presence and Air -- and the bands
are read on top of that preset and nothing else.

**The tone belongs to the preset, not to the voice.** A player who brightens
`Choir` has not asked for a brighter `Doom`, and a single shared control would
make every choice of pedal move the sound it was tuned to. The setting is one
saved dict, preset id -> the bands that preset moved, so a preset nobody has
touched costs one missing key and nothing else.

**Flat is not a filter.** A preset whose four bands are all 0 dB is handed to
the processor as no tone stage at all: `dsp._ToneStack` returns the frame
untouched, so every preset that has never been equalised is byte-for-byte the
pedal it was (the whole shelf was tuned by ear, and this feature must not
re-tune it by accident). The first decibel a player moves is the first sample
of DSP this costs.

**Four bands, not three or thirty.** A voice's useful tone is at the ends and
at two places in the middle: the chest (a low shelf), the mud (a bell around
400 Hz), the words (a bell around 2.8 kHz) and the breath on top (a high
shelf). Three would put both middles behind one knob -- a telephone with a
tone control -- and thirty is a mixing console, which is the Music Bot's song
EQ, not a voice in a room.

Where it sits in the chain
--------------------------
`dsp._EffectProcessor` applies this after the preset's own blocks and before
the output trim and the peak ceiling, so a band pushed up is caught by
`PEAK_CEILING` like any other loud pedal, and a preset that was not touched
takes no new code path at all. The stage glides a moved band over a few
frames (`dsp._ToneStack.GLIDE_DB`) rather than jumping, because a filter
whose coefficients step is a click; a slider press is the only thing that
moves a band, and a press that lands over five frames still sounds immediate.

How a player sets it
--------------------
The `Tone:` line on the Voice Effect page (and on each board's own page) opens
`menu_pages.voice_effect._VoiceToneSlider`: Tab switches bands, Up and Down
move a decibel, Page Up and Page Down move six, Home puts the band back flat,
`R` puts all four back, and Enter keeps them. It is the same keyboard shape as
the Music Bot's own Bass/Mid/Treble slider, and every press is spoken, so a
player who cannot see the page can still hear exactly what the band is doing.

Rules: .agents/skills/music_bot_integration/references/voice-effects.md.
"""

from ... import options

#: The saved setting. One dict: preset id -> ``{band id: decibels}``. A band at
#: 0 is absent rather than saved as a zero, so the file carries only what a
#: player actually moved and "flat" is the state of a key that is not there.
_OPTION = "music_bot_voice_tone"

#: How far either way a band may be moved, in decibels.
MAX_DB = 12

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


def band(band_id):
    """One band's row, or None for an id the table does not offer."""
    for row in BANDS:
        if row[0] == band_id:
            return row
    return None


def _clamp(value):
    """A whole number of decibels inside the range a band can be moved."""
    try:
        number = int(round(float(value)))
    except (TypeError, ValueError):
        return 0
    return max(-MAX_DB, min(MAX_DB, number))


def _saved():
    """The whole saved setting, whatever shape an older file left it in."""
    stored = options.get(_OPTION, {})
    return stored if isinstance(stored, dict) else {}


def values(preset_id):
    """One preset's four bands as numbers: an untouched preset is all zeros."""
    row = _saved().get(str(preset_id))
    if not isinstance(row, dict):
        return {band_id: 0 for band_id in BAND_IDS}
    return {band_id: _clamp(row.get(band_id, 0)) for band_id in BAND_IDS}


def gains(preset_id):
    """The same four bands as a tuple, in band order -- what the DSP reads.

    Read on the capture thread once per frame, so it is the cheap shape: no
    dictionaries built when the preset has never been touched, and one tuple
    when it has.
    """
    row = _saved().get(str(preset_id))
    if not isinstance(row, dict) or not row:
        return FLAT
    return tuple(_clamp(row.get(band_id, 0)) for band_id in BAND_IDS)


def has_tone(preset_id):
    """Whether this preset has any band off flat."""
    return any(gains(preset_id))


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
    preset = str(preset_id)
    value = _clamp(decibels)
    stored = dict(_saved())
    row = dict(stored.get(preset) or {})
    if value:
        row[band_id] = value
    else:
        row.pop(band_id, None)
    if row:
        stored[preset] = row
    else:
        stored.pop(preset, None)
    options.set(_OPTION, stored, autosave=save)
    return value


def reset(preset_id, save=True):
    """Every band of one preset back to flat, and the preset's key forgotten."""
    stored = dict(_saved())
    if stored.pop(str(preset_id), None) is not None:
        options.set(_OPTION, stored, autosave=save)
    return dict(values(preset_id))


def spoken(decibels):
    """A band's value the way a screen reader should say it."""
    value = _clamp(decibels)
    if not value:
        return "flat"
    return f"{'plus' if value > 0 else 'minus'} {abs(value)} decibels"


def summary(preset_id):
    """The bands in the menu's words: `Flat`, or `+3 Low, -2 Air`."""
    moved = [(row, values(preset_id)[row[0]]) for row in BANDS]
    moved = [(row, value) for row, value in moved if value]
    if not moved:
        return "Flat"
    return ", ".join(f"{value:+d} {row[1]}" for row, value in moved)


def description(preset_id):
    """What the tone is doing, in one sentence, for speech and for Tab.

    Flat is said as the pedal it is -- the preset exactly as it was made --
    because "0 decibels on four bands" is not something a player asked for.
    """
    moved = [row for row in BANDS if values(preset_id)[row[0]]]
    if not moved:
        return "Flat: this effect is exactly as it was made."
    parts = ", ".join(f"{spoken(values(preset_id)[row[0]])} on {row[1]}"
                      for row in moved)
    return f"This effect, then {parts}."
