"""Fifty ready-made voices for the Custom Voice board to start from.

Why this exists
---------------
`voice_effect_builder.py` is a board of ten slots holding seventy-one choices,
which is more combinations than anyone will ever try and, on the first visit,
ten lines all reading their default. A player who wants "my voice, but through
a broken radio" has to know which of ten slots that is before they can hear it.
So the board's **first line** is `Start From:` and it opens this module: fifty
finished voices, each one the same ten slots with values in them. Picking one is
picking a sound, and every slot underneath stays the player's to change.

A recipe is not a pedal. It has no row in `voice_effect_catalogue`, it never
appears in the Voice Effect list, and it has no spec of its own: `apply()` writes
the board and `voice_effect_builder.spec()` builds the sound, exactly as it does
for a board the player set by hand. That is why fifty of them cost the DSP
nothing -- there is no second processor, no second table of filters, and no way
for a recipe and the board to disagree about what is being heard.

One row is the whole statement
------------------------------
A row is ``(id, menu name, family, what Tab reads, board)``. The **family is a
field of the row**, never a second list of ids: a recipe cannot be orphaned from
the page that offers it or counted in two families, and `FAMILIES` is only the
five names and the order they are listed in. `family_members` filters this table,
so the page and the data are the same object.

The board names only the slots it moves. The defaults live in
`voice_effect_builder.PARAMS` and `boards.read` fills them in, so a recipe says
"Dark tone, Cave" rather than ten values of which eight are what you would get
anyway -- and the day a default changes, the recipe follows it.

A starting point is not a claim about where the player ended
------------------------------------------------------------
`current_label()` answers "My Own" the moment a slot no longer matches the
recipe the board came from, because the board's first line is a statement about
what the player is hearing, not a memory of a button they pressed. `current_id()`
is the same answer for the page's mark.
"""

#: The five pages the fifty are split across: ``(id, menu name, what Tab
#: reads)``. Ten each, because a flat list of fifty is a wall and a family of
#: ten is a choice -- the same reason the pedals themselves are in families.
FAMILIES = (
    ("amp", "Amp & Rock",
     "Ten voices built from the guitar-amp blocks: clean, dirty and the "
     "loud ones. The place to start if what you want is a voice with "
     "speakers around it."),
    ("machine", "Machine",
     "Ten voices that are made of something other than a throat: vocoders, "
     "ring modulators, a crushed sampler and a voice from an orbit. These "
     "change what the voice is made of, not only how it is coloured."),
    ("room", "Rooms & Ghosts",
     "Ten voices that are mostly the place they are heard in: a cave, a "
     "plate, a hall that closes on every word, and the ones that answer you "
     "before you have finished."),
    ("rhythm", "Rhythm",
     "Ten voices played in time: stuttered, chopped, ducked, stopped and "
     "held. These are gestures -- the word arrives when the board says so."),
    ("singer", "Singer & Pop",
     "Ten voices for a singer rather than a talker: the current pop front, a "
     "whisper, a doubled take, a backing stack and the cassette in the car."),
)

#: The fifty. A board dict names only the slots it moves; every other slot is
#: the board's own default (`voice_effect_builder.PARAMS`).
RECIPES = (
    # ------------------------------------------------------------ Amp & Rock
    ("stack", "Full Stack", "amp",
     "A cranked half-stack: drive into a closed cabinet with the bite band "
     "pushed, a short slap behind it and a firm gate. Four hands of air "
     "moving one voice.",
     {"colour": "drive", "body": "bark", "space": "slap", "gate": "firm"}),
    ("scoop", "Scooped Metal", "amp",
     "The modern heavy sound: the middle scooped out, the presence band "
     "pushed and every word squeezed flat first, so the voice cuts through a "
     "wall of guitars.",
     {"colour": "drive", "tone": "dark", "body": "presence",
      "texture": "hyperpop", "space": "plate", "gate": "firm",
      "output": "half"}),
    ("plexi", "Plexi Roar", "amp",
     "An old British head turned up past its headroom: spitting top, a growl "
     "in the middle and a room behind it. Loud on purpose.",
     {"colour": "drive", "tone": "open", "body": "bark", "space": "plate",
      "gate": "firm", "output": "loud"}),
    ("boutique", "Boutique Clean", "amp",
     "A small valve amp, barely breathing: a wide chorus and a plate behind a "
     "clean voice. For singing, not for shouting.",
     {"colour": "glass", "tone": "open", "movement": "chorus",
      "space": "plate", "output": "half"}),
    ("doom", "Doom Wall", "amp",
     "A fuzz pedal into a bass cabinet in a stone room: everything below the "
     "word is weight and everything above it is dust.",
     {"colour": "fuzz", "tone": "dark", "body": "chest", "space": "cave",
      "gate": "firm", "output": "half"}),
    ("surf", "Surf Twang", "amp",
     "Bright, clean and wobbling, with an echo behind it: a 1960s "
     "instrumental amp. The words come out sunburnt.",
     {"colour": "glass", "tone": "bright", "body": "presence",
      "movement": "tremolo", "space": "echo"}),
    ("garage", "Garage Punk", "amp",
     "A fuzz box into a practice amp on the floor of a garage: blunt, loud "
     "and chopped at every pause.",
     {"colour": "fuzz", "tone": "warm", "body": "low", "space": "slap",
      "gate": "hard"}),
    ("stadium", "Stadium Lead", "amp",
     "The lead-channel sound from the front row: drive, an open top, a delay "
     "you can count, and enough level to sit on top of everything.",
     {"colour": "drive", "tone": "bright", "body": "presence",
      "space": "echo", "output": "loud"}),
    ("british", "British Crunch", "amp",
     "A small combo pushed until it honks: the nasal middle of a 1970s rock "
     "record, with a room that closes as soon as you stop.",
     {"colour": "crunch", "tone": "warm", "body": "honk", "space": "gated",
      "gate": "firm"}),
    ("acoustic", "Acoustic Amp", "amp",
     "An acoustic amp on a stand: no dirt at all, air on the top and a soft "
     "haze behind. The voice of somebody sitting down to sing.",
     {"colour": "glass", "tone": "bright", "body": "low", "texture": "air",
      "space": "cloud", "output": "half"}),

    # --------------------------------------------------------------- Machine
    ("vocoder", "Vocoder Stack", "machine",
     "Six bands of your voice play a synth instead of you: the words survive "
     "and the throat does not. The robot choir of every pop chorus.",
     {"colour": "off", "tone": "off", "texture": "vocoder",
      "pitch": "octave", "space": "plate", "gate": "firm"}),
    ("robot_choir", "Robot Choir", "machine",
     "A ring modulator with a chorus folded around it: many metallic voices "
     "saying one thing. Cold, wide and impossible to ignore.",
     {"colour": "warm", "pitch": "robot", "movement": "chorus",
      "space": "cloud"}),
    ("alien", "Alien Broadcast", "machine",
     "A sliding carrier over a telephone band: the words arrive from "
     "somewhere with different physics, and they arrive twice.",
     {"colour": "radio", "tone": "dark", "pitch": "alien", "space": "echo",
      "gate": "firm"}),
    ("crush_king", "Bitcrush King", "machine",
     "Six bits and a broken sampler: the voice is crushed and then stutters "
     "on itself. Every consonant is a small accident.",
     {"colour": "fuzz", "tone": "warm", "texture": "crush",
      "rhythm": "stutter", "gate": "firm", "output": "half"}),
    ("talkbox", "Talkbox", "machine",
     "The sound of a tube in somebody's mouth: the vowel is pushed up until "
     "the voice seems to come out of an instrument rather than a person.",
     {"colour": "drive", "texture": "talkbox", "space": "slap",
      "gate": "firm"}),
    ("shimmer_pad", "Shimmer Pad", "machine",
     "Every partial slides up together and a wide room holds it: a voice "
     "becoming a pad rather than a voice with an effect on it.",
     {"colour": "off", "tone": "bright", "texture": "air",
      "pitch": "shimmer", "space": "wash", "output": "half"}),
    ("sub_marine", "Sub Marine", "machine",
     "A copy read at half speed hums under you in a low room: the words on "
     "top, a submarine underneath.",
     {"colour": "warm", "tone": "dark", "pitch": "sub", "space": "cave",
      "gate": "firm", "output": "half"}),
    ("console", "Console Voice", "machine",
     "A voice from a game that shipped in 1994: eight kilohertz, a handful "
     "of bits, and a gate that shuts the room behind it.",
     {"colour": "warm", "tone": "dark", "texture": "crush", "gate": "hard"}),
    ("modem", "Modem", "machine",
     "A fax machine arguing with a ring modulator. Barely words, entirely a "
     "transmission, and the funniest thing on the board.",
     {"colour": "radio", "tone": "warm", "texture": "crush",
      "pitch": "robot", "rhythm": "stutter", "gate": "firm",
      "output": "half"}),
    ("satellite", "Satellite", "machine",
     "A voice bouncing off something far away and coming back thin and "
     "wobbling: a broadcast from an orbit nobody visits.",
     {"colour": "radio", "tone": "off", "movement": "vibrato",
      "texture": "air", "space": "wash", "output": "half"}),

    # --------------------------------------------------------- Rooms & Ghosts
    ("sermon", "Cave Sermon", "room",
     "Warm and heavy in a stone room: the vowels ring on after the word. "
     "Preaching to a congregation you cannot see.",
     {"colour": "warm", "tone": "dark", "body": "chest", "space": "cave",
      "gate": "firm"}),
    ("cathedral", "Cathedral", "room",
     "Almost all room: a bright, enormous space that holds the voice long "
     "after the sentence. For singing slowly, not for talking.",
     {"colour": "glass", "tone": "open", "texture": "air", "space": "wash",
      "output": "half"}),
    ("oracle", "Dub Oracle", "room",
     "Repeats that come back darker each time, with a backwards ghost "
     "growing under the next word. A very large machine that knows things.",
     {"colour": "warm", "tone": "warm", "space": "dub", "rhythm": "swell",
      "gate": "firm"}),
    ("plate_hall", "Plate Hall", "room",
     "A bright plate behind a clean voice: dense, short and "
     "expensive-sounding. The studio trick every ballad leans on.",
     {"colour": "glass", "tone": "bright", "space": "plate",
      "body": "flat"}),
    ("gated_room", "Gated Room", "room",
     "A big room cut off a fifth of a second after every word: the 1980s "
     "drum sound, applied to a sentence.",
     {"colour": "drive", "space": "gated", "gate": "hard"}),
    ("reverse_ghost", "Reverse Ghost", "room",
     "The last tenth of a second played backwards before each word, with "
     "breath riding it. Something is about to say what you just said.",
     {"colour": "off", "tone": "dark", "texture": "whisper",
      "space": "reverse"}),
    ("cloud_choir", "Cloud Choir", "room",
     "An octave above plus a drifting copy, all inside a haze: a choir that "
     "only exists in this room.",
     {"colour": "off", "tone": "bright", "texture": "air", "pitch": "choir",
      "space": "cloud", "output": "half"}),
    ("tunnel", "Tunnel", "room",
     "A pipe you have to shout down: a jet-like sweep on a narrow voice, "
     "with the shout coming back at you.",
     {"colour": "radio", "tone": "dark", "movement": "flanger",
      "space": "echo"}),
    ("distant_pa", "Distant PA", "room",
     "An announcement at the other end of a hall: small, far away, and "
     "answered by its own echo. The voice nobody turns to look at.",
     {"colour": "radio", "tone": "dark", "space": "echo",
      "output": "quiet"}),
    ("midnight", "Midnight Room", "room",
     "A slow whoosh over breath in a dark room: the sound of being told "
     "something at two in the morning.",
     {"colour": "warm", "tone": "dark", "movement": "phaser",
      "texture": "whisper", "space": "cave", "output": "half"}),

    # ---------------------------------------------------------------- Rhythm
    ("stutter_rap", "Stutter Rap", "rhythm",
     "The last tenth of a second repeats twice, five times a second: a verse "
     "chopped into itself. Every word starts three times.",
     {"colour": "drive", "rhythm": "stutter", "gate": "firm"}),
    ("trem_ghost", "Tremolo Ghost", "rhythm",
     "Breath pulsing in a cave: loud, quiet, loud, quiet, and never quite "
     "there. Hiding inside the pulse.",
     {"colour": "warm", "tone": "dark", "movement": "tremolo",
      "texture": "whisper", "space": "cave"}),
    ("pump_vocal", "Pump Vocal", "rhythm",
     "The voice ducks into every beat of a 128 house record and comes back "
     "between them, with its quiet half pushed up first.",
     {"colour": "warm", "texture": "hyperpop", "rhythm": "pump"}),
    ("tape_stop", "Tape Stop Talk", "rhythm",
     "The tape slows, sags in pitch and stops, then snaps back: a sentence "
     "played on a machine that dies every second and a half.",
     {"colour": "warm", "tone": "warm", "texture": "vinyl",
      "rhythm": "tapestop"}),
    ("chop_choir", "Chopped Choir", "rhythm",
     "A choir gated eight times a second: the words arrive in slices, each "
     "one with a room behind it.",
     {"colour": "glass", "tone": "open", "pitch": "choir",
      "rhythm": "chop", "space": "plate"}),
    ("wah_preach", "Wah Preacher", "rhythm",
     "A wah pedal under every syllable: the voice talks back to itself and "
     "the consonants turn into quacks.",
     {"colour": "drive", "body": "bark", "movement": "wah",
      "space": "slap", "gate": "firm"}),
    ("riser", "Riser Drop", "rhythm",
     "The pitch climbs, the head catches up, and it climbs again: the build "
     "before a drop, made from one held word.",
     {"colour": "warm", "tone": "bright", "space": "wash",
      "rhythm": "riser", "output": "half"}),
    ("swell_pad", "Swell Pad", "rhythm",
     "A backwards copy of what you said a moment ago grows under the voice "
     "and is cut off: every phrase arrives after its own echo.",
     {"colour": "off", "tone": "open", "texture": "air", "rhythm": "swell",
      "space": "wash", "output": "half"}),
    ("freeze", "Freeze Drone", "rhythm",
     "The last sixth of a second held and repeated under you as a drone: you "
     "talk over a note you have already sung.",
     {"colour": "off", "tone": "warm", "space": "cloud",
      "rhythm": "freeze", "output": "half"}),
    ("auto_funk", "Auto-Wah Funk", "rhythm",
     "A touch wah that follows how hard you speak: murmur for the low pass, "
     "push a word and it opens. The voice as a rhythm part.",
     {"colour": "crunch", "body": "bark", "movement": "autowah",
      "gate": "firm"}),

    # ----------------------------------------------------------- Singer & Pop
    ("hyperpop_lead", "Hyperpop Lead", "singer",
     "All front and no room: the quiet half of every word lifted until it is "
     "as loud as the loud half, with a plate far behind. The current pop "
     "vocal.",
     {"colour": "warm", "tone": "bright", "texture": "hyperpop",
      "space": "plate", "output": "half"}),
    ("close_whisper", "Close Whisper", "singer",
     "Nothing but breath next to the microphone: close, quiet and "
     "confiding. The one to use when the room is already listening.",
     {"colour": "off", "tone": "off", "texture": "whisper",
      "output": "half"}),
    ("air_ballad", "Air Ballad", "singer",
     "New harmonics above four kilohertz on a clean voice: a dull recording "
     "opened up, with a plate to give it a stage.",
     {"colour": "glass", "tone": "open", "texture": "air",
      "space": "plate"}),
    ("doubled", "Double Tracked", "singer",
     "A copy twenty-two milliseconds behind: two takes an inch apart rather "
     "than a chorus. What a singer sounds like after a second pass.",
     {"colour": "glass", "movement": "double", "space": "slap"}),
    ("vocal_stack", "Vocal Stack", "singer",
     "An octave above and a drifting copy, chorused and placed in a plate: a "
     "whole backing stack out of one throat.",
     {"colour": "glass", "movement": "chorus", "pitch": "choir",
      "space": "plate", "output": "half"}),
    ("telephone_pop", "Telephone Pop", "singer",
     "A phone call from a pop record: the band cut below four hundred hertz, "
     "nothing above the middle, and no room at all.",
     {"colour": "radio", "tone": "dark", "texture": "hyperpop",
      "gate": "hard"}),
    ("lofi", "Lo-Fi Tape", "singer",
     "Dust, drift and a rolled-off top: the voice of a cassette that has "
     "lived in a car since 1987.",
     {"colour": "warm", "tone": "warm", "texture": "vinyl", "space": "slap",
      "output": "half"}),
    ("growl_soul", "Growl Soul", "singer",
     "Everything under four hundred and fifty hertz lifted and driven: the "
     "same words from a much bigger chest, with a slap behind them.",
     {"colour": "warm", "body": "chest", "texture": "growl",
      "space": "slap"}),
    ("glass_tenor", "Glass Tenor", "singer",
     "A barely-dirty chime, the presence band forward and a six-hertz "
     "wobble: a tenor who has never had a bad night.",
     {"colour": "glass", "tone": "open", "body": "presence",
      "movement": "vibrato", "space": "plate"}),
    ("wide", "Wide Chorus", "singer",
     "Nothing shaping the voice and three copies drifting behind it: as wide "
     "as the board goes without changing what you are.",
     {"colour": "off", "tone": "open", "texture": "air",
      "movement": "chorus", "space": "cloud", "output": "half"}),
)

#: What the board's first line reads when the player has moved a slot since:
#: the label for "not one of the fifty any more".
MY_OWN = "My Own"


def _builder():
    """The board this module writes to, imported where it is used.

    Inside the call because `voice_effect_builder` imports the recipes' own
    module only from the menu; a cycle at import time would make the sounds
    depend on which module was read first.
    """
    from . import voice_effect_builder
    return voice_effect_builder


def recipe(recipe_id):
    """One row, or None for an id this module does not offer."""
    for row in RECIPES:
        if row[0] == recipe_id:
            return row
    return None


def family(family_id):
    """One family's row, or None."""
    for row in FAMILIES:
        if row[0] == family_id:
            return row
    return None


def family_members(family_id):
    """The recipes of one family, in the order this table lists them."""
    return [row[0] for row in RECIPES if row[2] == family_id]


def recipe_note(recipe_id):
    """What Tab reads on a recipe's line in its family."""
    row = recipe(recipe_id)
    return row[3] if row else ""


def recipe_label(recipe_id):
    """A recipe's own name, or MY_OWN for an id that is not offered."""
    row = recipe(recipe_id)
    return row[1] if row else MY_OWN


def family_label(family_id):
    """A family's own name."""
    row = family(family_id)
    return row[1] if row else ""


def board(recipe_id):
    """The ten slots a recipe sets, every default filled in, or None."""
    row = recipe(recipe_id)
    return _builder().filled(row[4]) if row else None


def apply(recipe_id):
    """Fill the board from a recipe, in one save and one revision.

    The board is still the player's: everything this writes is an ordinary
    slot value, and the next press on a slot page changes one of them.
    """
    row = recipe(recipe_id)
    if row is None:
        return None
    return _builder().set_values(row[4], recipe=recipe_id)


def current_id():
    """The recipe the board still matches exactly, or empty.

    Empty covers both "the board was built by hand" and "it came from a recipe
    and one slot has moved since" -- the first line is about the sound, so a
    memory of a button press that no longer describes it would be a lie.
    """
    builder = _builder()
    stored = builder.recipe()
    row = recipe(stored)
    if row is None:
        return ""
    if builder.values() != builder.filled(row[4]):
        return ""
    return stored


def current_label():
    """`Start From:`'s value: the recipe's name, or MY_OWN."""
    return recipe_label(current_id())


def start_note():
    """What Tab reads on the board's first line."""
    return ("Fifty ready-made voices: pick one and the ten slots above fill "
            "themselves from it, then change whatever you like. As it is now: "
            f"{current_label()}. A recipe is not a pedal -- it writes the "
            "board, so what you hear is always the board.")


def problems():
    """Every way this table could lie to a player, as a list of sentences.

    A menu test and a DSP test both start from "the fifty are real"; this is
    the one place that says what real means, so a row added later is checked
    without another test having to know the rules.
    """
    found = []
    seen_ids = set()
    seen_labels = set()
    boards_seen = {}
    builder = _builder()
    slots = {row[0]: row for row in builder.PARAMS}
    for recipe_id, label, family_id, note, chosen in RECIPES:
        if recipe_id in seen_ids:
            found.append(f"{recipe_id} is used twice")
        seen_ids.add(recipe_id)
        if label in seen_labels:
            found.append(f"{label} is used twice")
        seen_labels.add(label)
        if family(family_id) is None:
            found.append(f"{recipe_id} is in no family ({family_id})")
        if not note.strip():
            found.append(f"{recipe_id} says nothing on Tab")
        for slot_id, value in chosen.items():
            row = slots.get(slot_id)
            if row is None:
                found.append(f"{recipe_id} sets a slot that is not on the "
                             f"board ({slot_id})")
            elif value not in [stored for _l, stored, _n in row[3]]:
                found.append(f"{recipe_id} asks {slot_id} for a choice it "
                             f"does not offer ({value})")
        filled = builder.filled(chosen)
        key = tuple(sorted(filled.items()))
        if key in boards_seen:
            found.append(f"{recipe_id} is the same voice as "
                         f"{boards_seen[key]}")
        boards_seen[key] = recipe_id
    for family_id, _name, _note in FAMILIES:
        if not family_members(family_id):
            found.append(f"{family_id} has no recipes")
    return found


def count():
    """How many starting points there are, for the tests and the note."""
    return len(RECIPES)
