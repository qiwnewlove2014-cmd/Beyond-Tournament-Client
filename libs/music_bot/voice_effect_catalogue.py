"""The pedals on the Voice Effect page: their names, and what Tab reads.

Kept apart from `voice_effects.py` on purpose. The DSP is one kind of change
and the words are another: a name moves a menu line, a description is what a
player hears when they press Tab, and neither should have to be found inside
eight hundred lines of filters. `voice_effects` imports this module and
re-exports it, so every existing caller (`controller.py`, `menus.py`, the
tests) keeps saying `voice_effects.effect_label`.

A row is ``(id, menu name, what Tab reads on that line)``. The name has to stay
short enough to arrow past; the description is where the pedal explains itself.
The DSP for a preset lives in `voice_effects._SPECS` under the same id, and the
two must agree -- `voice_effects._EffectProcessor` raises on an id that has no
spec, and a spec with no row is a pedal nobody can choose.
"""

#: Every preset, in the order the page lists them.
VOICE_EFFECTS = (
    ("off", "Off",
     "Your own voice, dry: no amp, no pedal. This is the same voice you had "
     "before the feature existed."),
    ("amp", "Guitar Amp",
     "A distorted guitar amp: pre-gain, a valve-ish soft clip and a 4x12 "
     "cabinet's dark top, with a noise gate between the words. Your words are "
     "still words -- it is the voice through the amp, not a guitar. Adds about "
     "3 milliseconds."),
    ("clean", "Clean Guitar",
     "A clean amp with a chorus pedal: no distortion, a bright cabinet and a "
     "slow wide doubling, heard as a shimmering second copy of the voice. This "
     "is the one preset with the pedal's own 12 milliseconds."),
    ("fuzz", "Fuzz",
     "A fuzz pedal: hard, asymmetric clipping with a thicker low end and a "
     "darker cabinet. The most aggressive of the set -- consonants turn into "
     "splinters and the voice bites."),
    ("radio", "Radio",
     "A guitar amp heard through a small speaker: the voice is band-passed to "
     "a narrow midrange and then driven, so it sounds like a broadcast coming "
     "from another room."),
    ("crush", "Crush",
     "A bitcrusher pedal: the voice is sampled at a lower rate and quantised, "
     "adding a gritty digital buzz under the words. The cleanest way to make a "
     "sentence sound broken."),
    ("octave", "Octave Up",
     "An octave pedal's oldest trick: the voice is rectified and mixed back "
     "with itself, which adds a note an octave above. It is dirty rather than "
     "clean -- a fuzz with a second voice on top, not a pitch shifter."),
    ("tremolo", "Tremolo",
     "A tremolo pedal: the voice pulses about five times a second, from full "
     "to nearly silent. The oldest way to make a spoken sentence sound like it "
     "is coming and going."),
    ("wah", "Wah",
     "An auto-wah: a narrow band sweeps up and down the voice about once a "
     "second, so consonants turn into a talking wah-wah and the words ride "
     "under it."),
    ("phaser", "Phaser",
     "A phaser: swept all-pass stages are mixed with the dry voice, so the two "
     "cancel and reinforce as they move. The effect is a slow whoosh under the "
     "words rather than a copy beside them."),
    ("echo", "Echo",
     "A slapback delay: what you said comes back about a seventh of a second "
     "later, quieter, and again after that. Long enough to be a repeat, short "
     "enough to stay under the next word."),
    ("robot", "Robot",
     "A ring modulator: the voice is multiplied by a 55 Hz tone, which "
     "replaces its pitch with metallic sidebands. It sounds like a machine -- "
     "the words are what gets lost first."),
    ("flanger", "Flanger",
     "A flanger: a very short delay sweeps across the voice and feeds back, "
     "so a jet-like whoosh moves through the words. Shorter and sharper than "
     "the clean amp's chorus."),
    ("vibrato", "Vibrato",
     "A vibrato pedal: the tape speed wobbles about five times a second, so "
     "the pitch rises and falls. Every note of the voice moves; the words do "
     "not change."),
    ("stutter", "Stutter",
     "A stutter: the last tenth of a second repeats twice, then the voice "
     "carries on, five times a second. The glitch a DJ makes with a delay "
     "pedal, on speech."),
    ("cave", "Cave",
     "A cave: four short echoes at different distances feed each other, so "
     "the voice rings in a big stone room. Longer and darker than the Echo "
     "pedal."),
    ("alien", "Alien",
     "An alien: a ring modulator whose carrier slides up and down, so the "
     "metallic sidebands move with it. Sings like a machine from a 1950s "
     "film."),
    ("sub_octave", "Sub Octave",
     "An octave down, read at half speed in short windows: a deep copy under "
     "your own voice. It is not a clean pitch shifter -- a little warble is "
     "the pedal."),
    ("envelope_wah", "Auto-Wah",
     "A touch wah: the sweep follows how loud you are, not a clock. Talk "
     "softly and it stays low; push a word and it opens. The funk pedal's "
     "trick."),
    ("gate", "Gate",
     "A hard gate: the voice is chopped on and off eight times a second, with "
     "none of the tremolo's soft edge. Rhythmic and mechanical."),
    ("reverse", "Reverse",
     "A reverse delay: the last tenth of a second is played backwards, then "
     "the voice carries on. Consonants arrive after the vowel that follows "
     "them."),
    ("chorus", "Chorus",
     "A chorus pedal: three copies of the voice, each a little later and "
     "slowly drifting, mixed with the dry one. Lush and wide, where Clean "
     "Guitar's is a single shimmer."),
    ("vinyl", "Vinyl",
     "A record: a delay whose length breathes a third of a millisecond, about "
     "three cents of wow, with a handful of clicks and the top rolled off. Old "
     "and warm rather than dirty."),
    ("body", "Wood Body",
     "A voice inside a guitar's box: one resonance at a low A and its "
     "harmonics, so the words gain a hollow wooden knock instead of a tail."),
    ("shimmer", "Shimmer",
     "A frequency shifter: every partial of the voice moves up by the same "
     "fourteen hertz, so the voice glides and shimmers without the mirroring "
     "a robot effect gives it."),
    ("whisper", "Whisper",
     "A hush: the voice loses its low end and filtered breath rides its own "
     "envelope. Quiet and close, as if someone is speaking into your ear."),
    ("growl", "Growl",
     "Two formants moved down, so the same words come from a much bigger "
     "chest. Dark and heavy, where Sub Octave is buzzy and Alien is metallic."),
    ("doubler", "Doubler",
     "One copy twenty-two milliseconds behind and barely moving: the voice "
     "sounds double-tracked, two takes an inch apart, rather than chorused."),
    ("vocoder", "Vocoder",
     "A vocoder: six bands of the voice drive a synth tone instead of being "
     "heard themselves, so the words come out of a machine that is singing "
     "them. The clearest robot here -- you can still understand every word."),
    ("talkbox", "Talkbox",
     "A talkbox: the voice is pushed into the top of its range and driven, so "
     "it sounds like it comes out of a tube in someone's mouth. Nasal and "
     "bright, where Growl is the same trick downwards."),
    ("hyperpop", "Hyperpop",
     "The loudness of a modern pop vocal: the quiet part of every word is "
     "lifted almost to the loud part and the loud part is held down, so the "
     "voice is all front and no room. Consonants press together."),
    ("tapestop", "Tape Stop",
     "A tape stop: the voice slows, drops in pitch and stops, then snaps back "
     "at full speed. The gesture a DJ makes with a reel, every second and a "
     "half."),
    ("cloud", "Cloud",
     "A cloud: four copies of the last third of a second drift under the "
     "voice at different speeds, so a word leaves a wide haze behind it "
     "instead of a repeat."),
    ("air", "Air",
     "Air: everything above four kilohertz is filtered out, driven and put "
     "back, which grows new top harmonics. For a dull, close voice -- it opens "
     "it up rather than changing it."),
    ("pump", "Pump",
     "A pumping sidechain: the voice ducks into every beat at 128 and comes "
     "back between them, the way a synth does under a kick drum. Rhythmic, not "
     "a wobble."),
    ("freeze", "Freeze",
     "A freeze: the last sixth of a second is held and repeated, so the voice "
     "becomes a drone under itself. It refreshes now and then, so the drone "
     "follows the words without ever being one of them."),
    ("step", "Step Filter",
     "A step filter: the voice is heard through one of three bands at a time "
     "and the band jumps through a pattern on sixteenths. A filter being "
     "played rather than swept."),
    ("swell", "Swell",
     "A swell: a backwards ghost of what was said a moment ago grows under "
     "the voice and is cut off, over and over. The reverse reverb of a "
     "trailer, made from words already spoken."),
)

DEFAULT_EFFECT = "off"


def clamp_effect(value):
    """A preset id that exists, whatever was saved or handed in."""
    for effect_id, _label, _note in VOICE_EFFECTS:
        if value == effect_id:
            return effect_id
    return DEFAULT_EFFECT


def effect_label(value):
    """The menu's words for a preset: 'Guitar Amp'."""
    wanted = clamp_effect(value)
    for effect_id, label, _note in VOICE_EFFECTS:
        if effect_id == wanted:
            return label
    return VOICE_EFFECTS[0][1]


def effect_note(value):
    """What Tab reads on that preset's line."""
    wanted = clamp_effect(value)
    for effect_id, _label, note in VOICE_EFFECTS:
        if effect_id == wanted:
            return note
    return VOICE_EFFECTS[0][2]
