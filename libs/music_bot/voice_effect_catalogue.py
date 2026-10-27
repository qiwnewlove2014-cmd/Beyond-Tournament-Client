"""The pedals on the Voice Effect page: their names, and what Tab reads.

Kept apart from `voice_effects.py` on purpose. The DSP is one kind of change
and the words are another: a name moves a menu line, a description is what a
player hears when they press Tab, and neither should have to be found inside
eight hundred lines of filters. `voice_effects` imports this module and
re-exports it, so every existing caller (`controller.py`, the voice-effect menu
page, the tests) keeps saying `voice_effects.effect_label`.

A row is ``(id, menu name, what Tab reads on that line)``. The name has to stay
short enough to arrow past; the description is where the pedal explains itself.
The DSP for a preset lives in `voice_effects._SPECS` under the same id, and the
two must agree -- `voice_effects._EffectProcessor` raises on an id that has no
spec, and a spec with no row is a pedal nobody can choose.

Three rows are not fixed presets: ``custom`` is the Custom Amp board
(`voice_effect_custom.py`), ``voice_build`` is the Custom Voice board
(`voice_effect_builder.py`) and ``vox_lab`` is the Vocoder Lab board
(`voice_effect_vocoder.py`), and their DSP is built from those boards instead
of being written into ``_SPECS``. All three are catalogued like the others so
the pedal list, the family check and the clamp all treat them the same way; the
builder is in ``STANDALONE_EFFECTS`` because it sits on the Voice Effect page's
top level rather than inside a family, the Pan Delay is there for the same
reason (its delay is typed, so it opens a page rather than being a row of
steps) and the vocoder board is the last line of the Vocoder Lab family, where
its twenty presets live.
"""

from .voice_effect_builder import EFFECT_ID as BUILDER_EFFECT
from .voice_effect_custom import EFFECT_ID as CUSTOM_EFFECT
from .voice_effect_pan import EFFECT_ID as PAN_EFFECT
from .voice_effect_vocoder import EFFECT_ID as VOX_LAB_EFFECT

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
    (PAN_EFFECT, "Pan Delay",
     "A delay that moves: the repeats cross the listener's ears (or pass in "
     "front and behind) while the words repeat beside them. The delay is typed "
     "in milliseconds -- it is both the gap between the repeats and the time "
     "one crossing takes -- and the movement is applied by each listener's own "
     "machine, because one mono frame cannot carry a direction."),
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
    ("hiss", "Hiss",
     "The breath with no voice under it: a bright band of noise riding the "
     "shape of the syllable, so every word arrives as its own hiss. Not a "
     "whisper -- there are no formants in it, which is what makes it sound like "
     "a brush across a snare drum."),
    ("hiss_room", "Hiss Room",
     "The same hiss left in a stone room: four taps feed one another, so a "
     "syllable hisses and then hisses from the walls. Wide, dry, and a little "
     "wrong on purpose."),
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
    # ---- the studio ten: the chain a record's vocal is actually built from
    ("tube", "Tube Warmth",
     "A valve stage at low gain: even harmonics and a soft knee, no cabinet "
     "and no fizz. What a singer is given when a take is too clean -- it adds "
     "weight, not distortion."),
    ("presence", "Presence",
     "The three-kilohertz band lifted and its top opened, which is what puts a "
     "voice in front of a mix. No distortion: this is an EQ with a singer's "
     "name on it."),
    ("plate", "Plate",
     "A plate: six short bright taps between eighteen and a hundred "
     "milliseconds, feeding one another. Denser and brighter than Cave, and "
     "gone by the next word."),
    ("gated", "Gated Reverb",
     "A big room chopped by a timer: the tail blooms when you speak and is cut "
     "about a fifth of a second later. The sound of every drum of 1983, on a "
     "voice."),
    ("throw", "Delay Throw",
     "A quarter-second repeat at three-eighths of a second, the last word of a "
     "line thrown into a darker echo. Each repeat is filtered, so it fades "
     "away rather than piling up."),
    ("dub", "Dub Delay",
     "A dark feedback delay: the repeats come back lower and darker each time, "
     "until they are a rumble. The vocal dub a sound system uses."),
    ("wash", "Wash",
     "Almost all room and almost no voice: the voice is sent into a wide, "
     "bright space and only a quarter of the dry stays. For a layer under a "
     "chorus, not for words."),
    ("choir", "Choir",
     "An octave above the voice, doubled into three drifting copies: a small "
     "choir singing the same words. Clean of distortion -- the copies carry "
     "the pitch, not a fuzz."),
    ("riser", "Riser",
     "The voice's pitch climbs and the head catches back up to the live "
     "sample, over and over. The build before a drop, made from one word."),
    ("scream", "Scream",
     "A hard clip with the midrange pushed: the modern scream, where the words "
     "survive the distortion. Louder and more forward than Fuzz, and shorter "
     "in the tail."),
    # ---- the twenty amps: the Amp & Drive family's second shelf
    ("crunch", "Crunch",
     "A cranked small amp at the edge of its headroom: the chords grind and "
     "still show their notes, and a quiet syllable stays clean. The rhythm "
     "sound, with a mid that pushes back."),
    ("overdrive", "Overdrive",
     "A pedal in front of a crunchy amp: a mid hump and a soft clip, so the "
     "voice thickens and the top stays open. Less dirt than Guitar Amp, more "
     "push than Clean."),
    ("boost", "Clean Boost",
     "A clean boost: the voice lifted a couple of decibels through a valve's "
     "soft knee, with no cabinet and almost no distortion. For a quiet voice "
     "that has to be heard."),
    ("lead", "Lead",
     "A lead channel: high gain with a mid pushed forward and the cabinet kept "
     "dark, so a syllable sings and still lands. The most sustain of any amp "
     "here."),
    ("modern", "Modern Gain",
     "A modern high-gain amp: the bass cut tight before the shaper and a hard "
     "clip after it, so a chug stays a chug. Bright, tight and unforgiving."),
    ("tweed", "Tweed",
     "A small tweed combo breaking up: a dark cabinet, strong mids and an "
     "asymmetric clip, so the voice sounds like it comes out of a box with one "
     "speaker in it."),
    ("brown", "Brown Sound",
     "A cranked British head with the mids forward and the top rolled back: "
     "the brown sound of a hot-rodded amp -- all bark and no fizz."),
    ("hotrod", "Hot Rod",
     "A hot-rodded amp: more gain than the brown one and a bright cap on the "
     "top, so the pick and the consonants cut through the dirt."),
    ("chime", "Chime",
     "A class-A amp's chime: barely any clipping, a bright cabinet and the top "
     "opened by an exciter. Clean, glassy, and it rings."),
    ("sag", "Sag",
     "A valve rectifier's sag: the supply dips under a loud passage and "
     "recovers slowly, so the voice breathes under its own weight instead of "
     "staying pinned flat."),
    ("direct", "Direct",
     "A DI box: no cabinet and no shaper, the whole range of the voice with a "
     "gentle compressor's glue. The clean reference the other amps are heard "
     "against."),
    ("acoustic", "Acoustic DI",
     "A piezo pickup's amp: the quack around three kilohertz lifted, the top "
     "opened and the bottom kept out of the way. For a voice that should sound "
     "like a guitar's box."),
    ("jazz", "Jazz Clean",
     "A jazz clean: the top rolled off at three kilohertz and a warm low-mid "
     "lift, so the voice is round and dark and nothing hisses."),
    ("room_cab", "Cab Room",
     "A cabinet in a small room: the amp's own cabinet plus four short taps "
     "under thirty milliseconds, so a chord is heard with the room it was "
     "played in."),
    ("doom", "Doom",
     "A doom amp: the drive at the top of the range, an octave down under it "
     "and the cabinet down at two kilohertz. Slow, enormous, and muddy on "
     "purpose."),
    ("stoner", "Stoner",
     "A stoner fuzz with a slow filter riding it: a hard clip and a narrow "
     "band sweeping once every five seconds, so a held syllable opens and "
     "closes."),
    ("velcro", "Velcro Fuzz",
     "A velcro fuzz: a hard clip chopped thirteen times a second, so a note "
     "tears instead of ringing. Sputtering, and deliberately broken."),
    ("octavia", "Octave Fuzz",
     "An octave fuzz: a hard clip with the rectified copy mixed back in, adding "
     "a note an octave up. The oldest fuzz trick, and the dirtiest way to sing "
     "higher."),
    ("fuzzsub", "Fuzz Sub",
     "A fuzz with an octave down: the bass cut low before a hard clip and a "
     "half-speed copy under it, so one voice becomes a riff and its floor."),
    ("twelve", "Twelve String",
     "A twelve-string's shimmer: a clean amp with the rectified octave mixed "
     "lightly, so every word gains a second, brighter string above it."),
    # ---- the third shelf (2026-10-27): twenty more voicings, one per corner
    ("plexi", "Bright Stack",
     "A hundred-watt stack with the bright channel up: the top stays open "
     "over the dirt and the upper mid pushes at a syllable. The sound of a "
     "big room with a loud band in it."),
    ("scoop", "Scoop",
     "A high-gain amp with the mids taken out: a hard clip, a low bottom and "
     "an exciter far above the voice, so the consonants stay sharp while the "
     "middle falls away. Modern, wide and empty in the centre."),
    ("treble_boost", "Treble Boost",
     "A treble booster in front of an amp: everything under three hundred "
     "hertz is cut away before the shaper and the top is opened after it, so "
     "a dull voice arrives bright and biting, with no bottom at all."),
    ("bass_rig", "Bass Rig",
     "A big bass rig: the low end reaches forty hertz and the cabinet stops "
     "early, with only a whisper of drive. The one amp here that puts a chest "
     "under the words instead of a bite over them."),
    ("cocked", "Cocked Wah",
     "A wah pedal parked half open: one very narrow band at twelve hundred "
     "hertz, and the drive behind it. It does not sweep -- the sound is the "
     "fixed vowel the pedal is stuck on, which is what makes the words honk."),
    ("wool", "Wool Fuzz",
     "A germanium fuzz at low gain: a soft, badly matched clip with the "
     "cabinet dark and the top almost gone. Rounder and woollier than Fuzz, "
     "and it cleans up on a quiet syllable."),
    ("starved", "Starved Fuzz",
     "A fuzz run on a dying battery: the supply cannot keep up, so the clip "
     "sputters and the gate splutters on the tail of every word. Broken on "
     "purpose, where Velcro tears on a clock and this one chokes on its own "
     "current."),
    ("boxy", "Small Box",
     "A five-watt combo in a small box: the cabinet starts at two and a half "
     "kilohertz, one boxy resonance at seven hundred and a clip with some "
     "weight. Small, nasal and entirely convincing."),
    ("studio_pre", "Studio Pre",
     "A rack preamp rather than an amp: a wide, quiet channel with one small "
     "lift at sixteen hundred and barely any clipping. The clean front end a "
     "record is cut through before anything else is added."),
    ("tape", "Tape Saturation",
     "A voice printed to tape: a soft, almost symmetric clip, the top rolled "
     "at four and a half kilohertz and no cabinet resonance at all. It rounds "
     "the sound rather than dirtying it -- the tamest drive here."),
    ("organ", "Organ Drive",
     "A combo organ through its own amp: the bass cut high, a hard band at "
     "twenty-four hundred and a gentle clip, so the voice takes on the reedy "
     "edge of a drawbar instead of a guitar's bark."),
    ("air_amp", "Air Amp",
     "An amp with almost no cabinet at all: the top runs to nine kilohertz "
     "and an exciter puts back what the microphone lost. Bright, open and "
     "thin, where Direct is flat and this one is lit."),
    ("horn", "Horn",
     "A voice through a horn: one broad band at fifteen hundred hertz with "
     "the drive behind it, so every syllable is pushed out of a bell. Loud, "
     "nasal and unmistakably a speaker aimed at a street."),
    ("grind", "Grind",
     "A modern metal channel: the hardest clip of the shelf, the cabinet cut "
     "at four kilohertz and no lifted band to hide behind. Tight, dry and "
     "relentless."),
    ("substack", "Sub Stack",
     "An amp tuned for the bottom: the cabinet at twenty-two hundred hertz "
     "and a half-speed copy under it, so a word arrives an octave deep and "
     "heavy. Doom's floor without Doom's mud."),
    ("sizzle", "Sizzle",
     "A driven amp with a bright cap and a lift at three and a fifth: the "
     "consonants sizzle over a tight clip while the body of the voice stays "
     "dry. The most forward top of the shelf."),
    ("lowgrowl", "Low Growl",
     "A big amp with its weight in the low mids: a band at four hundred and "
     "twenty hertz, a drive that thickens and a cabinet that still reaches "
     "the top. Chest, not rumble."),
    ("wall", "Wall",
     "Everything the shaper can do at once: the drive at the top of the "
     "range into a hard clip, the cabinet at four kilohertz. There is no "
     "clean syllable left -- a wall of voice with the words inside it."),
    ("spring", "Spring Amp",
     "A small amp with a spring tank in it: four short bright taps between "
     "eleven and thirty-one milliseconds, so a word twangs back at you from "
     "the springs. The amp's reverb, not a room's."),
    ("trem", "Amp Tremolo",
     "An amp with its own tremolo switched on: the valve bias swings about "
     "five times a second, so the voice pulses from full to a third. Not the "
     "pedal's square chop -- this one breathes."),
    # ---- the vocoder family (2026-10-26): the words spoken by a machine
    ("vox_classic", "Classic Robot",
     "The vocoder everyone hears in their head: six bands of your voice drive a "
     "110 Hz saw, so the mouth is yours and the throat is a synth's. Words "
     "survive whole; the voice does not."),
    ("vox_deep", "Deep Machine",
     "The same machine an octave lower: a 55 Hz saw with its band bank pulled "
     "down, so the voice arrives huge and slow. The one to use for a villain's "
     "announcement."),
    ("vox_chip", "Chip Voice",
     "A toy's speak-and-spell: a 220 Hz saw with the bands pushed up into the "
     "top of the voice. Small, bright and a little comic."),
    ("vox_giant", "Giant",
     "A 41 Hz carrier with the bands pulled right down: deeper than Deep "
     "Machine, and the bottom of the cabinet is opened for it. A voice from "
     "under a hill."),
    ("vox_gnome", "Gnome",
     "The other end of the same trick: a 330 Hz saw and the bands lifted, so "
     "everything sits high and quick. Fussy rather than cute."),
    ("vox_square", "Square Bot",
     "A square-wave carrier instead of a saw: only the odd harmonics, so the "
     "fizz under the words is thicker and the voice is plainly a buzzer."),
    ("vox_pulse", "Pulse Talker",
     "A narrow pulse carrier: thinner than the square, with a click on every "
     "cycle that reads as a mechanical tick under each syllable."),
    ("vox_stack", "Twin Oscillator",
     "Two oscillators an octave apart instead of one: the classic machine with "
     "a brighter copy on top of it, so the words sit in a fatter synth than "
     "Classic Robot's."),
    ("vox_noise", "Noise Whisper",
     "A noise carrier instead of a note: no pitch at all, so the words arrive "
     "as a loud whisper that keeps the mouth's shape. The quietest-sounding "
     "loud preset here."),
    ("vox_breath", "Breath Machine",
     "The classic saw with a breath of noise on the carrier, so air hisses "
     "through the machine on every word -- a vocoder with a throat rather than "
     "a speaker."),
    ("vox_choir", "Synth Choir",
     "The classic vocoder sung by three copies drifting apart: the machine "
     "becomes a small choir of itself."),
    ("vox_hall", "Vocoder Hall",
     "The classic vocoder in a stone room: four short taps feed each other, so "
     "the machine rings on after the word."),
    ("vox_radio", "Broadcast Bot",
     "A vocoder heard down a telephone line: a square carrier band-passed to "
     "the midrange and driven, so it sounds like a machine reading the news "
     "from a speaker in another room."),
    ("vox_bit", "Bit Machine",
     "A vocoder through a broken sampler: the output is sampled coarsely, so "
     "the machine's voice is grit held together by the words."),
    ("vox_alien", "Alien Synth",
     "A vocoder through a sliding ring modulator: the metallic sidebands move "
     "up and down under the words, so the machine sings rather than speaks."),
    ("vox_shimmer", "Sky Machine",
     "A vocoder through a single-sideband shifter: a copy an octave up slides "
     "under the voice and lifts it. Bright, wide and not quite there."),
    ("vox_iron", "Iron Throat",
     "A low carrier with a chest formant and some drive: the machine has a "
     "body, and it is not a small one."),
    ("vox_air", "Air Machine",
     "A vocoder with an exciter after it: the top is grown rather than "
     "equalised, so the machine's consonants sparkle without getting louder."),
    ("vox_formant", "Formant Synth",
     "A vocoder with one band lifted high: the machine has a tube in its "
     "throat, so the words come out shaped like a talkbox rather than a "
     "speaker."),
    ("vox_octave", "Octave Machine",
     "A vocoder with a copy an octave up mixed in: one machine becomes two, "
     "the second one brighter and slightly out of step."),
    # ---- the board: not a preset, the family's own tuner
    (VOX_LAB_EFFECT, "Vocoder Lab",
     "Not a preset: your own carrier, on a board of nine rows. Current keeps "
     "the classic 110 Hz machine; Tune Note locks it to Root + Octave, so "
     "everything you say comes out on that note; Interval adds a scale degree "
     "above or below it, for a fixed harmony or a drone in a key. Wave, "
     "Formant Shift, Breath and Mix shape it, and every row lands on the next "
     "20 millisecond frame."),
    # ---- the radio family (2026-10-26): twenty channels, worst to best
    ("rad_8bit", "Eight Bit",
     "A four-bit toy radio: six kilohertz of samples, sixteen steps and a hiss "
     "behind the words. The worst thing here on purpose -- it is the sound of a "
     "sentence squeezed through a chip."),
    ("rad_broken", "Broken Speaker",
     "An eight-bit radio with a torn cone: the top above 2.6 kilohertz is gone, "
     "what is left is driven hard, and the hiss is loud. Every word lands like "
     "a knock on cardboard."),
    ("rad_toy", "Toy Walkie",
     "A five-bit walkie-talkie: a narrow channel, coarse samples and a bright "
     "hiss, like two children talking through a garden fence."),
    ("rad_cardboard", "Cardboard Radio",
     "A six-bit radio in a box: a hard clip, a middly channel and almost no top. "
     "Cheap, loud and entirely unlovely -- the sound of a radio in a garage."),
    ("rad_intercom", "Intercom Buzzer",
     "A six-bit intercom with the gain up: the voice is driven into the "
     "clipper's corner, so it buzzes rather than speaks. The one for a door "
     "speaker nobody wants to answer."),
    ("rad_cb", "CB Radio",
     "A citizen's-band radio: narrow, levelled hard and faintly hissing. Clear "
     "enough to understand, plain enough to sound like a lorry's dashboard."),
    ("rad_walkie", "Walkie Talkie",
     "A seven-bit handheld: a five-hundred-hertz channel with a steady hiss, "
     "the middle of the cheap handheld range."),
    ("rad_drivethru", "Drive-Thru",
     "A speaker in a box at a window: band-limited, with one band near two "
     "kilohertz pushed forward so an order of fries arrives perfectly clear."),
    ("rad_am", "AM Radio",
     "An amplitude-modulated station: a narrow channel, a hiss, and a faint "
     "fifty-five-hertz ring where the carrier sits. Warm, old and a little "
     "whistly."),
    ("rad_shortwave", "Shortwave",
     "A shortwave station fading in and out: the loudest hiss here, a slow ring "
     "that drifts with the carrier, and a channel that keeps changing its mind."),
    ("rad_numbers", "Numbers Station",
     "A number station: a narrow channel, a steady hiss and a sixty-hertz ring "
     "under every digit. Flat, mechanical and oddly unsettling."),
    ("rad_fieldphone", "Field Phone",
     "A field telephone: a very narrow channel, seven-bit samples and almost no "
     "low end at all. Built to be understood in a wind, not enjoyed."),
    ("rad_military", "Military Radio",
     "A military set: driven, band-limited and levelled flat, so a shouted "
     "order and a quiet one arrive equally loud."),
    ("rad_police", "Police Radio",
     "A patrol radio: a narrow channel with the top clipped off and a hard "
     "leveller, so it cuts through an engine and a siren."),
    ("rad_dispatch", "Dispatch",
     "A dispatcher's desk: clearer than the patrol set, a light leveller and a "
     "thin hiss. Businesslike."),
    ("rad_marine", "Marine VHF",
     "A marine band radio: a slightly wider channel than the police set and a "
     "steady level, because a call from a boat has to survive wind."),
    ("rad_ham", "Ham Radio",
     "An amateur set: a wide-ish channel, a little drive and a hiss you can "
     "hear between the words. Friendly rather than official."),
    ("rad_airtraffic", "Air Traffic",
     "An air-traffic channel: a clean four-and-a-half-kilohertz channel with a "
     "strong leveller and very little hiss. Every word clipped to the same "
     "loudness, which is exactly why pilots are easy to understand."),
    ("rad_tower", "Control Tower",
     "A tower frequency: wider again and quieter, levelled so hard that a "
     "whisper and a shout come out the same size. Calm and procedural."),
    ("rad_cockpit", "Cockpit Headset",
     "A pilot's own headset: a clean channel, a lift near three kilohertz for "
     "consonants, and a level that never moves. The sound of someone reading a "
     "checklist twenty thousand feet up."),
    ("rad_terminal", "Terminal PA",
     "An airport's announcement: a wide channel, a light leveller and a small "
     "hall behind it, so a gate number arrives with the room around it."),
    ("rad_station", "Station Hall",
     "A train station: the same clarity as the terminal, in a taller room with "
     "a longer tail. The last syllable of a platform number hangs."),
    ("rad_stadium", "Stadium Announcer",
     "A stadium: levelled to the top, opened at the top, with a big room under "
     "it. A name read out over a crowd."),
    ("rad_church", "Church PA",
     "A church's speech system: a warm channel in a stone room, with a tail "
     "long enough to be a building and short enough to stay a sentence."),
    ("rad_cinema", "Cinema Voice",
     "The voice at the front of a cinema: wide, quiet between the words, and "
     "levelled so the soft line and the loud one both reach the back row."),
    ("rad_trailer", "Movie Trailer",
     "The trailer voice: chest pushed forward, driven a little, and levelled as "
     "hard as the format allows. Every sentence sounds like the last one before "
     "a title card."),
    ("rad_fm", "Broadcast FM",
     "A broadcast FM chain: wide, bright, compressed to the loudest the ceiling "
     "allows. The station that never has a quiet moment."),
    ("rad_studio", "Studio Condenser",
     "A studio condenser on a treated room: the widest channel here, a gentle "
     "lift at the top and a leveller that only glues. Nothing added, nothing "
     "taken away."),
    ("rad_news", "News Anchor",
     "A news desk: crisp, forward around three kilohertz, and levelled so the "
     "reading never dips. The voice that tells you what happened."),
    ("rad_mission", "Mission Control",
     "A flight controller's loop: clean, quiet, level, with a small room behind "
     "it. Calm on purpose -- the sound of a place where nothing is allowed to "
     "sound alarmed."),
    # ---- the board: not a preset, the player's own
    ("custom", "Custom Amp",
     "Your own amp, built on a page of ten knobs: drive, character, bass, "
     "mid, treble, gate, air, sag, wobble and output -- forty-four choices. It "
     "is the same chain every fixed pedal uses, so it can be anything from a "
     "clean DI to a saturated fuzz, an exciter-bright stack or an amp that "
     "dips and moves, and a guitar plugged into the line-in is shaped by it "
     "too."),
    (BUILDER_EFFECT, "Custom Voice",
     "Your own effect, assembled from the blocks the fixed pedals are made of: "
     "colour, tone, body, movement, pitch, space, texture, rhythm, a gate and "
     "the output. Eleven slots and seventy-eight choices between them, so a fuzz in "
     "a cave with a slow chorus is a sound you can build rather than one the "
     "list happens not to have."),
)

#: Rows that are reachable from the Voice Effect page's *top* level rather than
#: from a family. `catalogue_problems` treats them as reachable -- the check
#: exists to catch a preset nobody can choose, not to force every row into a
#: category -- and `menus._open_voice_effect_menu` puts each one on that page by
#: hand, which is the thing a new standalone row has to remember. Both of these
#: open a page rather than being a pedal to pick: the Pan Delay's setting is
#: typed, and Custom Voice is a board. A row in a family AND here is an error
#: (`listed twice`), so a line that moves up here moves out of its family.
STANDALONE_EFFECTS = (PAN_EFFECT, BUILDER_EFFECT)

#: The page is too long to read as one list, so it is a page of families and a
#: page of pedals under each: ``(id, menu name, what Tab reads, (members,))``.
#: A preset that is in no family cannot be reached, and one in two is reachable
#: twice -- ``catalogue_problems`` is what the tests hold this to.
VOICE_EFFECT_GROUPS = (
    ("drive", "Amp & Drive",
     "The amp, the fuzz, the radio and the pedals that add dirt or a "
     "cabinet's colour -- forty-six of them, and the Custom Amp board at the "
     "end of the list.",
     ("amp", "clean", "fuzz", "radio", "crush", "growl",
      "crunch", "overdrive", "boost", "lead", "modern", "tweed", "brown",
      "hotrod", "chime", "sag", "direct", "acoustic", "jazz", "room_cab",
      "doom", "stoner", "velcro", "octavia", "fuzzsub", "twelve",
      "plexi", "scoop", "treble_boost", "bass_rig", "cocked", "wool",
      "starved", "boxy", "studio_pre", "tape", "organ", "air_amp", "horn",
      "grind", "substack", "sizzle", "lowgrowl", "wall", "spring", "trem",
      "custom")),
    ("motion", "Motion",
     "Tremolo, wah, phaser, flanger, vibrato and chorus: the pedals that move "
     "the voice rather than colour it.",
     ("tremolo", "wah", "envelope_wah", "phaser", "flanger", "vibrato",
      "chorus")),
    ("machine", "Machine",
     "Octave, sub octave, ring modulation and the vocoder: the pedals that "
     "turn a voice into a machine.",
     ("octave", "sub_octave", "robot", "alien", "vocoder", "shimmer")),
    ("space", "Space",
     "Echoes, a cave and a cloud: the pedals that put the voice in a room "
     "bigger than the one you are in.",
     ("echo", "cave", "cloud", "swell", "reverse")),
    ("character", "Character",
     "Vinyl, wood, breath, air, a hiss, a double and a talkbox: the pedals that "
     "change what the voice is made of.",
     ("vinyl", "body", "whisper", "hiss", "hiss_room", "air", "doubler",
      "talkbox")),
    ("rhythm", "Rhythm & Glitch",
     "Stutter, gate, step, pump, tape stop, hyperpop and freeze: the pedals "
     "that play the voice in time.",
     ("stutter", "gate", "step", "pump", "tapestop", "hyperpop", "freeze")),
    ("studio", "Studio Vocals",
     "The ten a record's vocal chain is made of: warmth, presence, a plate, a "
     "gated tail, throws, dub, a wash, a choir, a riser and a scream.",
     ("tube", "presence", "plate", "gated", "throw", "dub", "wash", "choir",
      "riser", "scream")),
    ("synth", "Vocoder Lab",
     "Twenty machines that speak your words for you: the carrier's note and "
     "waveform, its bands moved up or down, and one block behind each -- a "
     "chorus, a hall, a ring, a crush, a chest. The same six-band bank the "
     "Machine family's Vocoder uses, set twenty ways -- and the board at the "
     "end, which puts that carrier in your hands.",
     ("vox_classic", "vox_deep", "vox_chip", "vox_giant", "vox_gnome",
      "vox_square", "vox_pulse", "vox_stack", "vox_noise", "vox_breath",
      "vox_choir", "vox_hall", "vox_radio", "vox_bit", "vox_alien",
      "vox_shimmer", "vox_iron", "vox_air", "vox_formant", "vox_octave",
      VOX_LAB_EFFECT)),
    ("broadcast", "Radio",
     "Twenty channels in a ladder, from a four-bit toy with a torn cone to a "
     "cockpit headset: what changes is the channel's corners, the codec's bit "
     "depth, how loud the channel hisses and how hard it is levelled. The "
     "start of the list is deliberately bad.",
     ("rad_8bit", "rad_broken", "rad_toy", "rad_cardboard", "rad_intercom",
      "rad_cb", "rad_walkie", "rad_drivethru", "rad_am", "rad_shortwave",
      "rad_numbers", "rad_fieldphone", "rad_military", "rad_police",
      "rad_dispatch", "rad_marine", "rad_ham", "rad_airtraffic",
      "rad_tower", "rad_cockpit")),
    ("venue", "PA & Venue",
     "The six places a voice is announced rather than transmitted: an "
     "airport terminal, a station hall, a stadium, a church, a cinema and a "
     "trailer. The room is the effect; the channel itself is clean.",
     ("rad_terminal", "rad_station", "rad_stadium", "rad_church",
      "rad_cinema", "rad_trailer")),
    ("console", "Studio Console",
     "The four clean chains at the top of the old ladder: an FM station, a "
     "studio condenser, a news anchor and mission control. What leaves the "
     "console -- close, quiet and levelled, with no codec and no hiss.",
     ("rad_fm", "rad_studio", "rad_news", "rad_mission")),
)


def group_members(group_id):
    """The presets in one family, in the order the page lists them."""
    for wanted, _label, _note, members in VOICE_EFFECT_GROUPS:
        if wanted == group_id:
            return members
    return ()


def group_label(group_id):
    """The family's name, or the first family's for an unknown id."""
    for wanted, label, _note, _members in VOICE_EFFECT_GROUPS:
        if wanted == group_id:
            return label
    return VOICE_EFFECT_GROUPS[0][1]


def group_note(group_id):
    """What Tab reads on the family's line."""
    for wanted, _label, note, _members in VOICE_EFFECT_GROUPS:
        if wanted == group_id:
            return note
    return VOICE_EFFECT_GROUPS[0][2]


def group_of(effect_id):
    """Which family holds a preset, or None for an id that is not listed."""
    for group_id, _label, _note, members in VOICE_EFFECT_GROUPS:
        if effect_id in members:
            return group_id
    return None


def catalogue_problems():
    """What is wrong with the page: a pedal lost, listed twice, or unknown.

    A flat list cannot lose a row and still look complete; two levels can. This
    is the check the tests run, so adding a preset and forgetting its family is
    a red test rather than a pedal nobody can choose.
    """
    problems = []
    listed = []
    for group_id, _label, _note, members in VOICE_EFFECT_GROUPS:
        for effect_id in members:
            if effect_id not in {row[0] for row in VOICE_EFFECTS}:
                problems.append(f"{group_id} names a preset that is not "
                                f"catalogued: {effect_id}")
            listed.append(effect_id)
    seen = set()
    for effect_id in listed:
        if effect_id in seen:
            problems.append(f"listed twice: {effect_id}")
        seen.add(effect_id)
    catalogued = {row[0] for row in VOICE_EFFECTS}
    for effect_id, _label, _note in VOICE_EFFECTS:
        if effect_id == DEFAULT_EFFECT or effect_id in STANDALONE_EFFECTS:
            continue
        if effect_id not in seen:
            problems.append(f"no family holds it: {effect_id}")
    for effect_id in STANDALONE_EFFECTS:
        if effect_id not in catalogued:
            problems.append(f"standalone line is not catalogued: {effect_id}")
        if effect_id in seen:
            problems.append(f"standalone line is also in a family: {effect_id}")
    return problems

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
    """What Tab reads on that preset's line.

    The Custom Amp is the one row whose answer changes: its line has to say
    what the board is set to now, not what the board is, or Tab on the pedal
    list would describe a page the player already left.
    """
    wanted = clamp_effect(value)
    if wanted == CUSTOM_EFFECT:
        from . import voice_effect_custom
        return voice_effect_custom.note()
    if wanted == BUILDER_EFFECT:
        from . import voice_effect_builder
        return voice_effect_builder.note()
    if wanted == VOX_LAB_EFFECT:
        from . import voice_effect_vocoder
        return voice_effect_vocoder.note()
    for effect_id, _label, note in VOICE_EFFECTS:
        if effect_id == wanted:
            return note
    return VOICE_EFFECTS[0][2]
