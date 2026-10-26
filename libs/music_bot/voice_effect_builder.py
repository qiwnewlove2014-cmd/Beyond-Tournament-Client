"""Custom Voice: the player's own effect, assembled from the blocks we have.

Why a second board
------------------
`Custom Amp` (in `voice_effect_custom.py`) is a board for one family: seven
knobs that configure the amp chain. A player who wants their *voice* to be
something the list does not have -- a fuzz, a cave and a slow chorus at once, a
vocoder with a sub octave, a whisper behind a tremolo -- needs more than an amp,
and adding a preset for every combination is how sixty-seven pedals became a
list nobody reads. So the Voice Effect page ends with one line that opens this
board, and the board is built from the same blocks the fixed presets are.

**Ten slots, seventy-one choices.** A slot is a table row: its name, what Tab
says about it, and its choices, each with its own sentence. The slots are the parts
an effect is made of, and each one holds exactly the choices that cannot run
with each other:

- **Colour** (7) -- the shaper: Off, Warm, Crunch, Drive, Fuzz, Glass, Radio.
- **Tone** (6) -- the cabinet: Off (no cabinet), Dark, Warm, Classic, Open,
  Bright.
- **Body** (6) -- one lifted band: Flat, Chest, Low, Bark, Honk, Presence.
- **Movement** (9) -- the block that owns the shared LFO: Off, Tremolo, Wah,
  Auto-Wah, Phaser, Flanger, Vibrato, Chorus, Double.
- **Pitch** (7) -- Off, Octave Up, Sub Octave, Choir, Robot, Alien, Shimmer.
- **Space** (10) -- Off, Slapback, Echo, Dub, Cave, Plate, Gated, Wash, Cloud,
  Reverse.
- **Texture** (9) -- Off, Air, Whisper, Growl, Talkbox, Vocoder, Hyperpop,
  Crush, Vinyl.
- **Rhythm** (9) -- Off, Stutter, Chop, Step, Pump, Tape Stop, Freeze, Swell,
  Riser.
- **Gate** (4) and **Output** (4) -- the two that every effect needs.

**Why the slots are grouped that way.** The processor runs one instance of each
block per effect, and several of them share state: Tremolo, Wah, Phaser and the
chop all advance `self._lfo`, and Plate/Cave/Wash are the same tap bank under
three sets of numbers. Two choices from one slot would write the same key and
the later would silently win -- a menu that lies about what it is playing. One
choice per slot is the rule that keeps "what you picked" and "what you hear"
the same thing, and it is why every choice is a slot and not a dial.

**The spec is the presets' own.** `spec()` returns a dict in exactly the shape
`voice_effects._SPECS` uses, so the same `_EffectProcessor` builds it; three
collisions are resolved on purpose and said so in the choices' own Tab text: a
Talkbox takes the Body slot's band, Whisper adds breath *over* whatever body the
board has, and Space can only be one room at a time.

Switching a slot lands on the next frame: `revision()` is read beside the
preset id by `VoiceEffectLine`, which rebuilds the processor and crossfades
into it (`voice_effect_boards.py` holds the reading half of that rule).

**The board's first line is `Start From:`**, and it is not a slot: fifty
finished voices (`voice_effect_recipes.py`) in five families, because ten lines
reading their defaults is not a way in. A recipe is a saved board and nothing
else -- `set_values` is what it calls, so all ten slots move in one save and one
revision (one crossfade), and `spec()` still builds the sound.

**Its last line above Back is `Clear:`**, which is also not a slot: `clear()`
puts the ten back at their defaults and forgets the recipe, in one save. The
menu asks first, because it is the one line on this page a single extra press
cannot undo.
"""

from .. import options
from . import voice_effect_boards as boards

#: The id the rest of the feature knows this board by. It is a catalogue row and
#: a line on the Voice Effect page's top level -- not a family member, because it
#: is one choice on that page rather than a category under it
#: (`voice_effect_catalogue.STANDALONE_EFFECTS`).
EFFECT_ID = "voice_build"

#: The saved board, beside the preset that selects it.
_OPTION = "music_bot_voice_effect_builder"

#: The starting point the board was last filled from (`voice_effect_recipes`),
#: or empty for a board the player built from the defaults. Saved here rather
#: than in the recipes module because it is part of this board's state -- and
#: because one options seam is one thing a test has to stand in for.
_RECIPE_OPTION = "music_bot_voice_effect_recipe"

#: A slot: ``(id, menu name, what Tab reads on the slot's line, choices,
#: default)``. A choice is ``(menu name, stored value, what Tab reads)``. The
#: stored values are short ids, never hertz or gains: a saved voice has to
#: outlive the day a filter is retuned, and a player reads "Cave", not "31/47".
PARAMS = (
    ("colour", "Colour",
     "The shaper: how hard the voice is driven and which kind of clip does it. "
     "Off is a clean voice with only the tone and cabinet around it; Radio is "
     "the same trick heard over a telephone line. This is the first thing the "
     "voice meets and the last thing that decides how dirty it is.",
     (("Off", "off",
       "No shaper at all: the voice goes through the cabinet and the rest of "
       "the board clean. The place to start when you want an effect, not a "
       "distortion."),
      ("Warm", "warm",
       "A soft valve knee at 2.2x: thicker, no fizz. The voice grows weight "
       "and stays a voice."),
      ("Crunch", "crunch",
       "A cranked amp at 5x: chords and words begin to grind. The default, and "
       "the middle of the shelf."),
      ("Drive", "drive",
       "A pedal in front of a crunchy amp at 9x: more compression, more "
       "sustain, and the words still come through."),
      ("Fuzz", "fuzz",
       "A hard, badly biased clip at 18x: the two halves of the wave no longer "
       "match, so consonants splinter. The dirtiest choice here."),
      ("Glass", "glass",
       "A barely-dirty 3x with a bright cabinet: a class-A amp's chime. Clean "
       "enough to sing through, coloured enough to hear."),
      ("Radio", "radio",
       "The voice cut below four hundred hertz before the shaper: a small "
       "speaker in another room. Pair it with Tone Off for the full telephone "
       "effect.")),
     "crunch"),
    ("tone", "Tone",
     "The cabinet: how far up the voice's top reaches. Every amp is a cabinet "
     "before it is anything else, and this is the slot that decides whether the "
     "board sounds like a 4x12, a practice amp or nothing at all.",
     (("Off", "off",
       "No cabinet: the whole range stays, wide open. For a clean voice or a "
       "vocoder, where a cabinet would only take the shine off."),
      ("Dark", "dark",
       "Everything above twenty-four hundred hertz gone: a cabinet under a "
       "blanket. For a pad, a ghost or a phone call."),
      ("Warm", "warm",
       "A closed-back cabinet close-miked: no fizz, all midrange."),
      ("Classic", "classic",
       "Four thousand two hundred: the cabinet most of the fixed presets use. "
       "Bright enough to hear the consonant, dark enough not to hiss."),
      ("Open", "open",
       "Six thousand: an open-back cabinet, so the air around the word comes "
       "with the word."),
      ("Bright", "bright",
       "Nine thousand: almost no box. A keyboard amp rather than a guitar one.")),
     "classic"),
    ("body", "Body",
     "One band lifted before the cabinet, which is what gives a voice a chest, "
     "a nose or a bark. Flat leaves the band alone; the four lifts are the "
     "ones the fixed pedals use, and a Talkbox in the Texture slot takes this "
     "slot's band for its own.",
     (("Flat", "flat",
       "No band lifted: the shaper and the cabinet are the whole colour."),
      ("Chest", "chest",
       "Four hundred and fifty hertz lifted: the same words from a bigger "
       "chest. Dark, heavy, and it hums under the consonants."),
      ("Low", "low",
       "Five hundred, the body of a chord: weight without brightness."),
      ("Bark", "bark",
       "Nine hundred, where a guitar's bite lives: the board pushes back on "
       "every vowel."),
      ("Honk", "honk",
       "Thirteen hundred, a small combo's nasal peak: an amp too loud for the "
       "room it is in."),
      ("Presence", "presence",
       "Twenty-two hundred: the part of an amp that cuts through a band, on "
       "its own. Forward and bright.")),
     "flat"),
    ("movement", "Movement",
     "The block that moves, and the only one that can: Tremolo, Wah, Phaser, "
     "Flanger, Vibrato, Chorus and Double all advance the same oscillator, so "
     "the board gives them one slot rather than letting two of them fight over "
     "it. Off is a still voice.",
     (("Off", "off",
       "Nothing moves: the voice is shaped but steady. The default, and the "
       "right answer under a word you want understood."),
      ("Tremolo", "tremolo",
       "A 5.2 Hz pulse from full to nearly silent: the oldest way to make a "
       "spoken sentence sound like it is coming and going."),
      ("Wah", "wah",
       "A narrow band swept 360-1800 Hz once a second: consonants turn into a "
       "talking wah-wah and the words ride under it."),
      ("Auto-Wah", "autowah",
       "A touch wah: the sweep follows how loud you are instead of a clock. "
       "Speak softly and it stays low; push a word and it opens."),
      ("Phaser", "phaser",
       "Two swept all-pass stages mixed with the dry voice: a slow whoosh "
       "under the words rather than a copy beside them."),
      ("Flanger", "flanger",
       "A very short delay swept and fed back: a jet-like whoosh through the "
       "words. Shorter and sharper than the Chorus choice."),
      ("Vibrato", "vibrato",
       "The tape speed wobbles six times a second: every partial of the voice "
       "moves and the words do not change."),
      ("Chorus", "chorus",
       "Three copies at 7, 9 and 12 ms, drifting: lush and wide. What a clean "
       "amp with a chorus pedal sounds like."),
      ("Double", "double",
       "One copy 22 ms behind, barely moving: two takes an inch apart rather "
       "than a chorus. Turns one voice into a double-tracked one.")),
     "off"),
    ("pitch", "Pitch",
     "Octaves and machines: the choices that change what the voice is made of "
     "rather than what it is coloured with. One at a time -- an octave above "
     "and a robot under it is two voices in one slot, and the board is honest "
     "about what it can play.",
     (("Off", "off",
       "The voice's own pitch, untouched. The default."),
      ("Octave Up", "octave",
       "The rectified voice mixed back in: a dirty copy an octave above. The "
       "oldest octave trick, and it splinters rather than shifting cleanly."),
      ("Sub Octave", "sub",
       "A copy read at half speed under you: a deep voice humming beneath the "
       "words. A little warble is the pedal."),
      ("Choir", "choir",
       "The octave above plus one drifting copy: a small choir singing the same "
       "words, clean of distortion."),
      ("Robot", "robot",
       "A 55 Hz ring modulator: the voice multiplied by a tone, so its pitch "
       "becomes metallic sidebands and the words are what gets lost first."),
      ("Alien", "alien",
       "A ring modulator whose carrier slides up and down: the sidebands move "
       "with it. Sings like a machine from a 1950s film."),
      ("Shimmer", "shimmer",
       "A frequency shifter: every partial moves up by the same fourteen "
       "hertz, so the voice glides instead of turning metallic.")),
     "off"),
    ("space", "Space",
     "Where the voice is heard from: one room, one echo, one ghost at a time. "
     "Cave, Plate and Wash are the same bank of taps under three sets of "
     "numbers, which is why they share a slot instead of stacking.",
     (("Off", "off",
       "The room you are actually in. Nothing is added behind the voice."),
      ("Slapback", "slap",
       "A 145 ms repeat at 35% feedback: long enough to be a repeat, short "
       "enough to stay under the next word."),
      ("Echo", "echo",
       "A third of a second, fed back and filtered: what you said comes back "
       "quieter, then again."),
      ("Dub", "dub",
       "A dark feedback echo: the repeats come back lower and darker each time "
       "until they are a rumble."),
      ("Cave", "cave",
       "Four short taps feeding one another: the voice rings in a big stone "
       "room."),
      ("Plate", "plate",
       "Six short bright taps between 18 and 97 ms: denser and brighter than "
       "Cave, and gone by the next word."),
      ("Gated", "gated",
       "A big room chopped by a timer: the tail blooms when you speak and is "
       "cut a fifth of a second later."),
      ("Wash", "wash",
       "Almost all room and a quarter voice: for a layer under a chorus, not "
       "for words."),
      ("Cloud", "cloud",
       "Four copies of the last third of a second drifting at different "
       "speeds: a wide haze instead of a repeat."),
      ("Reverse", "reverse",
       "The last tenth of a second played backwards, then the voice carries "
       "on. Consonants arrive after the vowel that follows them.")),
     "off"),
    ("texture", "Texture",
     "The last colour the voice is made of: breath, a tube, a machine that "
     "sings your words, or something broken. Off leaves the voice alone; these "
     "are the choices that change what it is made of rather than where it is.",
     (("Off", "off",
       "The voice's own texture. The default."),
      ("Air", "air",
       "The top filtered out, driven and put back: new harmonics above four "
       "kilohertz, which opens a dull voice instead of merely brightening it."),
      ("Whisper", "whisper",
       "Filtered breath rides your own envelope: quiet and close, as if "
       "someone is speaking into your ear. It adds to the body the board "
       "already has."),
      ("Growl", "growl",
       "Everything under 450 hertz lifted and driven: the same words from a "
       "much bigger chest."),
      ("Talkbox", "talkbox",
       "A formant pushed up to 2.3 kHz: it sounds like the voice comes out of "
       "a tube in someone's mouth. It takes the Body slot's band for its own."),
      ("Vocoder", "vocoder",
       "Six bands of the voice drive a synth tone instead of being heard: the "
       "words come out of a machine that is singing them."),
      ("Hyperpop", "hyperpop",
       "The quiet half of every word lifted until it is almost level with the "
       "loud half: all front and no room."),
      ("Crush", "crush",
       "A bitcrusher: the voice sampled at 8 kHz and six bits, so a digital "
       "buzz rides under the words."),
      ("Vinyl", "vinyl",
       "A delay that drifts a third of a millisecond, a few clicks and the top "
       "rolled off: old and warm rather than dirty.")),
     "off"),
    ("rhythm", "Rhythm",
     "How the voice is played in time: repeated, chopped, ducked, stopped or "
     "held. One at a time -- these are gestures, not a chain of them.",
     (("Off", "off",
       "The word arrives when you say it. The default."),
      ("Stutter", "stutter",
       "The last tenth of a second repeats twice, then the voice carries on, "
       "five times a second."),
      ("Chop", "chop",
       "A hard gate eight times a second: the voice is chopped on and off "
       "with none of Tremolo's soft edge."),
      ("Step", "step",
       "One of three bands at a time, jumping on sixteenths: a filter being "
       "played rather than swept."),
      ("Pump", "pump",
       "The voice ducks into every beat at 128 and comes back between them, "
       "the way a synth does under a kick drum."),
      ("Tape Stop", "tapestop",
       "The voice slows, drops in pitch and stops, then snaps back at full "
       "speed, every second and a half."),
      ("Freeze", "freeze",
       "The last sixth of a second held and repeated as a drone under itself, "
       "refreshing so it follows you."),
      ("Swell", "swell",
       "A backwards ghost of what was said a moment ago grows under the voice "
       "and is cut off, over and over."),
      ("Riser", "riser",
       "The pitch climbs and the head catches back up to live, over and over: "
       "the build before a drop, made from one word.")),
     "off"),
    ("gate", "Gate",
     "A noise gate between the notes. High gain on an open microphone is a "
     "feedback loop waiting for the room, and the gate closes when the frame is "
     "quiet and opens in half a frame, always with a ramp so it cannot click.",
     (("Off", "off",
       "No gate: the room behind you is amplified too. Only with headphones."),
      ("Soft", "soft",
       "Closes between phrases and opens on the first syllable: nearly "
       "invisible. The default."),
      ("Firm", "firm",
       "Closes a little sooner, so a driven board does not breathe between "
       "words."),
      ("Hard", "hard",
       "Shuts the moment you stop: silent between words, at the price of a "
       "clipped tail on a held vowel.")),
     "soft"),
    ("output", "Output",
     "The level the board hands on, after everything else. The line's ceiling "
     "is still above this, so Loud is trim rather than an excuse to overload "
     "the PA.",
     (("Quiet", "quiet",
       "Six decibels down: for a very hot input, or a room that does not want "
       "it."),
      ("Half", "half",
       "Three decibels down: a little headroom left for the PA's own sends."),
      ("Unity", "unity",
       "As loud as the voice, no louder. The default."),
      ("Loud", "loud",
       "A couple of decibels up: a lead part. The ceiling still catches it.")),
     "unity"),
)

#: Colour -> (shape, drive, bias, input high-pass). Radio's 430 Hz corner is the
#: only one that is not a voicing of the same low end.
_COLOUR = {
    "off": (None, 1.0, 0.0, 90.0),
    "warm": ("tanh", 2.2, 0.06, 100.0),
    "crunch": ("tanh", 5.0, 0.10, 110.0),
    "drive": ("tanh", 9.0, 0.12, 120.0),
    "fuzz": ("hard", 18.0, 0.22, 110.0),
    "glass": ("tanh", 3.0, 0.04, 130.0),
    "radio": ("tanh", 6.0, 0.05, 430.0),
}

#: Tone -> cabinet corner (Off is no cabinet at all).
_TONE = {"off": 12000.0, "dark": 2400.0, "warm": 3200.0, "classic": 4200.0,
         "open": 6000.0, "bright": 9000.0}

#: Body -> the lifted band, in the formant block's own four numbers.
_BODY = {
    "flat": None,
    "chest": (450.0, 1.10, 0.45, 1.6),
    "low": (500.0, 1.15, 0.40, 1.6),
    "bark": (900.0, 1.20, 0.42, 1.7),
    "honk": (1300.0, 1.40, 0.45, 1.7),
    "presence": (2200.0, 1.20, 0.36, 1.7),
}

#: Movement -> the spec key it writes. These are the presets' own numbers, so a
#: board with one slot set sounds exactly like the pedal that slot names.
_MOVEMENT = {
    "tremolo": {"tremolo": (5.2, 0.85)},
    "wah": {"wah": (360.0, 1800.0, 1.25, 3.2, 4.6)},
    "autowah": {"envelope_wah": (300.0, 1900.0, 3.0, 10.0)},
    "phaser": {"phaser": (0.4, 320.0, 1500.0)},
    "flanger": {"flanger": (1.5, 4.0, 0.25, 0.45, 0.55)},
    "vibrato": {"vibrato": (6.0, 2.5, 5.5)},
    "chorus": {"chorus3": ((7.0, 9.0, 12.0), 0.35, 1.6, 0.5)},
    "double": {"doubler": (22.0, 1.5, 0.33, 0.55)},
}

#: Pitch -> the spec keys. Choir is the octave plus one drifting copy, which is
#: the one place a second slot's block is written on purpose -- and Chorus lives
#: in the Movement slot, so the two can never fight for it.
_PITCH = {
    "octave": {"octave": 0.55},
    "sub": {"sub_octave": 0.50},
    "choir": {"octave": 0.30, "chorus": 0.35},
    "robot": {"robot": (55.0, 0.78)},
    "alien": {"alien": (300.0, 0.7, 0.72)},
    "shimmer": {"shimmer": (14.0, 0.62)},
}

#: Space -> one room, one echo, one ghost.
_SPACE = {
    "slap": {"echo": (0.145, 0.35, 0.45)},
    "echo": {"echo": (0.32, 0.45, 0.50)},
    "dub": {"dark_echo": (0.24, 0.68, 0.60, 1100.0)},
    "cave": {"cave": ((31.0, 47.0, 61.0, 83.0), 0.45, 0.55)},
    "plate": {"cluster": ((18.0, 27.0, 41.0, 58.0, 79.0, 97.0),
                          (0.8, 0.7, 0.6, 0.5, 0.42, 0.35), 0.22, 0.42)},
    "gated": {"gated": ((14.0, 23.0, 34.0, 47.0, 63.0, 81.0),
                        (0.8, 0.72, 0.62, 0.54, 0.46, 0.4), 0.22, 0.55,
                        0.22, 0.02)},
    "wash": {"cluster": ((90.0, 150.0, 220.0, 300.0),
                         (0.7, 0.6, 0.5, 0.4), 0.34, 0.8)},
    "cloud": {"cloud": ((95.0, 160.0, 240.0, 330.0), 0.11, 16.0, 0.62)},
    "reverse": {"reverse": (5, 10)},
}

#: Texture -> the spec keys. Talkbox writes `formant`, which is the Body slot's
#: key, so it deliberately overrides it: one band, one meaning.
_TEXTURE = {
    "air": {"air": (4200.0, 2.6, 0.45)},
    "whisper": {"whisper": (0.45, 0.50)},
    "growl": {"growl": (450.0, 0.80)},
    "talkbox": {"formant": (2300.0, 1.1, 0.55, 1.9)},
    "vocoder": {"vocoder": (2.6, 0.9)},
    "hyperpop": {"ott": (0.9, 0.35)},
    "crush": {"crush": (6, 8000.0)},
    "vinyl": {"vinyl": (0.6, 0.45, 0.30)},
}

#: Rhythm -> the spec keys. `chop` and `step` are already names in the presets.
_RHYTHM = {
    "stutter": {"stutter": (5, 10)},
    "chop": {"chop": (8.0, 0.06)},
    "step": {"step": (7.0, (250.0, 900.0, 3000.0), 1.4)},
    "pump": {"pump": (128.0, 0.8, 0.22)},
    "tapestop": {"tapestop": (0.55, 0.85)},
    "freeze": {"freeze": (8, 60)},
    "swell": {"swell": (0.06, 1.10, 0.55, 0.9, 0.5)},
    "riser": {"riser": (0.42, 0.55, 1.3, 0.55)},
}

#: Gate -> the RMS floor; Output -> the trim.
_GATE = {"off": 0.0, "soft": 0.004, "firm": 0.010, "hard": 0.020}
_OUTPUT = {"quiet": 0.60, "half": 0.78, "unity": 0.95, "loud": 1.15}

#: The slots whose choice the summary names, in the order a player thinks of
#: them: what it is made of, what it is doing, where it is. The rest are shown
#: on the board itself.
_SUMMARY_SLOTS = ("colour", "pitch", "movement", "space", "texture", "rhythm")

#: Bumped by every change, so a running line knows its processor is stale.
_revision = 0


def param(param_id):
    """One slot's row, for a menu building the page from the table."""
    return boards.param(PARAMS, param_id)


def choices(param_id):
    """The choices one slot opens into."""
    return boards.choices(PARAMS, param_id)


def values():
    """The board as it is set, every slot filled in from the table."""
    return boards.read(PARAMS, options.get(_OPTION, {}))


def set_value(param_id, value):
    """Set one slot, save the board, and tell the running line to rebuild."""
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
    """What this slot's own default is called in the menu's words."""
    return boards.default_label(PARAMS, param_id)


def clear_value(param_id):
    """One slot back to the value the board opens with, and one revision.

    The `Clear: back to ...` line on that slot's page. It removes the key
    rather than writing the default into it: `values()` fills the hole from
    `PARAMS`, so a cleared slot follows a default that moves tomorrow, the
    same reason `clear()` saves an empty board for all ten.

    The starting point's mark is left alone, and needs no help: if the slot
    now differs from the recipe the board was filled from, `current_label()`
    already reads `My Own`, and if it does not differ the board still IS that
    recipe -- there is nothing to forget.

    Unlike the whole board's Clear, this does not ask: the page already says
    what it holds and any choice above puts a value straight back.
    """
    global _revision
    if param(param_id) is None:
        return None
    board = values()
    board.pop(param_id, None)
    options.set(_OPTION, board)
    _revision += 1
    return values()[param_id]


def filled(board):
    """A partial board with every slot filled in, as `set_values` stores it.

    A starting point (`voice_effect_recipes`) names only the slots it moves,
    because that is what makes a recipe readable; the board itself is always
    ten values. This is the one place that difference is resolved, so a saved
    board and a recipe are comparable -- which is how the board's first line
    can say whether the player is still on the voice they picked.
    """
    return boards.read(PARAMS, board)


def set_values(board, recipe=None):
    """Write every slot at once -- a starting point, one revision.

    Ten `set_value` calls would be ten rebuilds of a processor that is heard
    between them, and a click at every slot. One write is one crossfade, which
    is the same thing a single slot change gives the player.

    `recipe` is the starting point's id, kept so the board can answer which one
    it came from; None leaves the mark alone, and a player who turns a slot
    afterwards reads "My Own" because the board no longer matches it.
    """
    global _revision
    clean = filled(board)
    options.set(_OPTION, clean)
    if recipe is not None:
        options.set(_RECIPE_OPTION, recipe)
    _revision += 1
    return clean


def recipe():
    """The starting point the board was last filled from, or empty."""
    return str(options.get(_RECIPE_OPTION, "") or "")


def clear():
    """Every slot back to its table default, and the starting point forgotten.

    The board's `Clear` line, and the only change on that page that cannot be
    read from the page and undone by pressing the same line again -- ten slots
    move at once -- so the menu asks before it calls this.

    It writes an EMPTY board rather than the defaults written out: `values()`
    fills the holes from `PARAMS`, so the two are the same board today, and an
    empty one follows the table if a default moves tomorrow. One save and one
    revision, like `set_values`, because ten slot writes would be ten heard
    rebuilds.
    """
    global _revision
    options.set(_OPTION, {})
    options.set(_RECIPE_OPTION, "")
    _revision += 1
    return values()


def revision():
    """How many changes the board has had; the line rebuilds when it moves."""
    return _revision


def choice_label(param_id, value=None):
    """A slot's chosen choice in the menu's words: 'Fuzz', 'Cave'."""
    board = values()
    if value is not None:
        board[param_id] = value
    return boards.label(PARAMS, board, param_id)


def choice_note(param_id, value):
    """What Tab reads on one choice of one slot."""
    return boards.note(PARAMS, param_id, value)


def summary():
    """The board in a few words, for the page's title and the pedal list.

    Only the slots that are not Off, so a player hears what the board *is*
    rather than a list of things it is not. A board with nothing switched on in
    those slots reads "Dry" -- it is still shaped by the cabinet and the tone
    and body slots, and the page's own lines say which.
    """
    board = values()
    parts = [choice_label(slot, board[slot]) for slot in _SUMMARY_SLOTS
             if board[slot] != "off"]
    return ", ".join(parts[:3]) if parts else "Dry"


def note():
    """What Tab reads on the Custom Voice line in the Voice Effect list."""
    return ("Your own effect, built on a page of ten slots: colour, tone, "
            "body, movement, pitch, space, texture, rhythm, a gate and the "
            f"output. As it is now: {summary()}. Fifty ready-made voices sit "
            "on the board's first line to start from, and every slot holds "
            "the blocks the fixed pedals are made of, so any combination of "
            "them is a sound that already works -- and every change lands on "
            "the next 20 millisecond frame.")


def spec():
    """The board as the processor's own spec: the slots, turned into numbers."""
    board = values()
    shape, drive, bias, hp = _COLOUR[board["colour"]]
    built = dict(hp=hp, lp=_TONE[board["tone"]], drive=drive, shape=shape,
                 bias=bias, gate=_GATE[board["gate"]],
                 out=_OUTPUT[board["output"]])
    body = _BODY[board["body"]]
    if body is not None:
        built["formant"] = body
    for slot, table in (("movement", _MOVEMENT), ("pitch", _PITCH),
                        ("space", _SPACE), ("texture", _TEXTURE),
                        ("rhythm", _RHYTHM)):
        chosen = board[slot]
        if chosen != "off":
            built.update(table[chosen])
    return built
