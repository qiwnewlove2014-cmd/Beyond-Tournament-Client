"""The Music Bot's Harmony: the second and third voice, and the lines that sing them.

Harmony used to be split between two files: the catalogue (the interval table,
the shares, the bass tone) sat at the bottom of `music_bot/autotune.py`, and the
two lines that actually mix a copy (`FastHarmonyLine` for a press that wants only
the copies, `HarmonyVoice` for one copy inside a tuned line) sat inside
`libs/voice_chat.py`. A feature with its own menu page, its own option keys, its
own DSP and two callers (the microphone and the line-in guitar) had no file of
its own, so every change to it was a change to a 968-line tuner or a 3700-line
voice module. It has one now.

**Three layers, in one place.**

*The catalogue* is the menu and its arithmetic -- `HARMONY_INTERVALS` (Off, third,
fifth, sixth, seventh, octave, and the three bass copies), `clamp_harmony` and
the labels/descriptions Tab reads, `harmony_semitones` (a NEGATIVE size is a copy
BELOW the voice) and `harmony_share` (an upper copy sings as an equal; a bass one
takes `BASS_SHARE` and is low-passed at `BASS_TONE_HZ`).

*The options* are `intervals()` / `enabled()` / `ids()`: the two saved slots, and
the rule that two slots holding the same interval are ONE copy (two identical
copies are the same voice at twice the level).

*The lines* are `FastHarmonyLine` (the lead is dry, the copies are shifted on the
guitar's 21 ms window, and the lead is held back by the slowest of them) and
`HarmonyVoice` (one copy, a shifter of its own, its own delay and its own fade).
Both mix into the SAME 20 ms frame, so a listener, a PA slot and a cinema room
still count the frames they always did.

**Nothing here tunes anything.** The interval comes from the menu and, while the
tuner is on, from the lead's own corrected note (`AutoTuner.harmony_offset`,
`music_bot/autotune.py`) -- never from the copy's own reading of the pitch. The
single dependency back on the tuner is `MAX_PULL`, read lazily inside
`harmony_pitch_limit`, so this module can be imported on its own.

Pinned by `client/tests/test_music_bot_autotune.py` (the catalogue and the voice)
and `client/tests/test_guitar_harmony.py` (the fast line under a string).
"""

import threading

import numpy as np

from .transpose import ChromaticTransposer, GuitarTransposer

#: One microphone frame: 20 ms of 48 kHz mono16. The same unit
#: `libs/voice_chat.py` names `VOICE_FRAME_BYTES`; a line must not import the
#: voice module just to know how big a frame is.
FRAME_BYTES = 1920


# ------------------------------------------------------------- the harmony voices

#: A harmony voice is a second copy of the tuned voice, an interval above it.
#: The interval is counted in the note set, not in semitones: a third above C
#: in C major is E (four semitones) and above D it is F (three), which is why a
#: harmony that follows the scale is always in key while a fixed shift is not.
#: A chromatic grid has no scale to count in, so it counts in the major scale --
#: what everyone means by "a third" when no key has been chosen.
#:
#: ``(id, menu name, semitones above the tuned note, what Tab reads)``. The
#: third field is the size the note is looked for from, and `AutoTuner.
#: nearest_note` snaps it to the scale, so a pentatonic answers with the nearest
#: note it actually has rather than inventing one.
#:
#: A NEGATIVE size is a copy BELOW the voice -- the bass half of the catalogue.
#: It is the same arithmetic (a third below C in C major is A, below D it is B)
#: and the same one-frame mix, but it is mixed as a bass voice rather than as a
#: second singer (`harmony_share`, `BASS_TONE_HZ`): quieter than the lead and
#: low-passed, because a copy an octave down at the lead's own level is heard as
#: the voice doubling and smearing rather than as a low voice under it.
HARMONY_INTERVALS = (
    ("off", "Off", 0,
     "No second voice: the tuned voice on its own."),
    ("third", "Third", 4,
     "A second copy of the voice a third above: the nearest note your scale "
     "has there while Auto-Tune is tuning you, so it is a major third over "
     "some notes and a minor third over others -- the interval a second "
     "singer would take. With Auto-Tune off it is a plain major third."),
    ("fifth", "Fifth", 7,
     "A second copy a fifth above: the strongest harmony there is, because "
     "it sounds like the same chord one voice higher."),
    ("sixth", "Sixth", 9,
     "A second copy a sixth above: wider and softer than a third, and the "
     "one that sounds like a duet rather than a chord."),
    ("seventh", "Seventh", 11,
     "A second copy a seventh above, a whole tone under the octave. It is the "
     "jazzier, more open harmony, and it wants a scale that has one -- while "
     "Auto-Tune is on a pentatonic answers with its own nearest note. With "
     "Auto-Tune off it is a plain major seventh."),
    ("octave", "Octave", 12,
     "A second copy an octave above: the same note, higher, which thickens "
     "the voice instead of adding a note to it."),
    ("bass_third", "Bass Third", -3,
     "A copy a third BELOW the voice: a low voice singing the note under "
     "yours, taken from your scale so it stays in key while Auto-Tune is on "
     "(a plain minor third with it off). It is mixed quieter than you and "
     "low-passed, so it hums under the words instead of competing with them."),
    ("bass_fifth", "Bass Fifth", -5,
     "A copy a fifth below: the low root under your note, which is what a "
     "choir puts under a melody. Quieter than the lead and low-passed, so it "
     "hums under you rather than doubling you."),
    ("bass_octave", "Bass Octave", -12,
     "A copy an octave below: the same note one octave down, which thickens "
     "the voice from underneath without adding a note to it. Quieter than "
     "the lead and low-passed, so it hums."),
)
DEFAULT_HARMONY = "off"

#: How loud a copy below the voice is against the lead's own 1.0. An upper copy
#: is a second singer and takes an equal share; a bass copy is an undertone and
#: takes a little over half of one, which is what keeps the words clear while
#: the low voice is heard under them.
BASS_SHARE = 0.55

#: Where a bass copy is low-passed, in Hz. A copy an octave down is otherwise the
#: voice again, an octave lower -- heard as the same words doubled and smeared.
#: Below this the fundamental and first harmonics are kept and the consonants are
#: gone, which is the hum a bass voice is: a note, not a second talker.
BASS_TONE_HZ = 380.0


def clamp_harmony(value):
    """A harmony interval id that exists, whatever was handed in or saved."""
    for interval_id, _label, _semitones, _note in HARMONY_INTERVALS:
        if value == interval_id:
            return interval_id
    return DEFAULT_HARMONY


def harmony_semitones(interval_id):
    """The size a harmony voice is looked for at, in semitones (0 for Off)."""
    for interval_id_, _label, semitones, _note in HARMONY_INTERVALS:
        if interval_id_ == clamp_harmony(interval_id):
            return semitones
    return 0


def harmony_label(interval_id):
    """The menu's words for a harmony interval: 'Third', 'Off'..."""
    for interval_id_, label, _semitones, _note in HARMONY_INTERVALS:
        if interval_id_ == clamp_harmony(interval_id):
            return label
    return HARMONY_INTERVALS[0][1]


def harmony_note(interval_id):
    """What the menu says this harmony is, for a player meeting it first."""
    for interval_id_, _label, _semitones, note in HARMONY_INTERVALS:
        if interval_id_ == clamp_harmony(interval_id):
            return note
    return HARMONY_INTERVALS[0][3]


def harmony_is_bass(interval_id):
    """True when this copy sings BELOW the voice rather than above it."""
    return harmony_semitones(interval_id) < 0


def harmony_share(interval_id):
    """This copy's weight in the one frame, against the lead's own 1.0."""
    return BASS_SHARE if harmony_is_bass(interval_id) else 1.0


def harmony_plain_offset(interval_id):
    """What a copy shifts by while the lead is NOT being tuned, or None.

    Harmony is a mode of its own: it sings with Auto-Tune off as well as on.
    With the tuner off there is no note set to count an interval in and no
    corrected note to place it from, so the copy is the plain musical interval
    the catalogue names (`harmony_semitones`: a major third, a perfect fifth,
    an octave) measured from the voice as it is actually sung -- the lead is not
    moved, so the copy is exactly that far from it, ear or not. None is Off.
    """
    if clamp_harmony(interval_id) == DEFAULT_HARMONY:
        return None
    return float(harmony_semitones(interval_id))


def harmony_pitch_limit(semitones):
    """The widest interval a harmony copy may ask its shifter for.

    The copy is the interval PLUS the tuning correction the lead is holding, and
    the transposer clamps what it is asked for. An octave above a voice that is
    a semitone and a half flat is 13.5 semitones -- and so is an octave BELOW
    it, because the limit is a distance; the sign only says which way the copy
    is taken (`HarmonyVoice` reads it) -- so the line this copy runs on is given
    its own limit and a look-ahead to match (`transpose.py`).
    """
    from .autotune import MAX_PULL
    return abs(float(semitones)) + MAX_PULL + 0.01


# ------------------------------------------------------------ the two slots
#
# The Music Bot saves Harmony in two options, one per slot. They are read here
# rather than in `libs/voice_chat.py` because the line that sings the copies is
# here too: the menus write the options, this module is the only reader, and no
# caller can sing a copy the menu is not showing.
OPTION = "music_bot_autotune_harmony"
OPTION_2 = "music_bot_autotune_harmony2"


def intervals():
    """The two harmony slots, as ``(first, second)`` interval ids."""
    from .. import options
    return (clamp_harmony(options.get(OPTION, DEFAULT_HARMONY)),
            clamp_harmony(options.get(OPTION_2, DEFAULT_HARMONY)))


def enabled():
    """Whether either harmony copy is switched on.

    Harmony is a mode of its own, not a dial of Auto-Tune: it sings with the
    tuner off (each copy at its plain interval) exactly as it does with the
    tuner on, so a press that only wants the copies still needs a line.
    """
    return any(interval != DEFAULT_HARMONY for interval in intervals())


def ids():
    """The two slots, in order, without Off and without a twin.

    Two slots holding the same interval are ONE copy -- two identical copies are
    the same voice at twice the level -- so this is the list every harmony line
    sings, the microphone's and the line-in guitar's alike.
    """
    wanted = []
    for interval in intervals():
        if interval != DEFAULT_HARMONY and interval not in wanted:
            wanted.append(interval)
    return tuple(wanted)


def copy_delay(interval_id, lookahead, transposer_cls=None):
    """How far behind its own source one copy lands, in samples.

    A copy above the voice is read faster than it is written, so an opening
    silence of ``lookahead * ratio`` comes out as exactly ``lookahead``
    samples -- the same delay for a third, a fifth and an octave, which is
    what lets one lead delay line them all up. A copy BELOW it reads slower
    and cannot open with less than the vocoder's own window (`WINDOW + HOP`),
    so it takes longer in real time: that is where the bass copies' extra
    milliseconds come from -- and why a line whose lead was shortened for the
    tuner has to grow back when a bass copy is on (see `AutoTuneLine`).

    The window belongs to the class that reads the copy: the fast line uses
    `GuitarTransposer`, the microphone's tuner line keeps the song's
    `ChromaticTransposer` (twice the window, and therefore twice the floor).
    """
    transposer_cls = transposer_cls or GuitarTransposer
    step = harmony_semitones(interval_id)
    if step == 0:
        return 0
    nominal = 2.0 ** (step / 12.0)
    opening = max(float(transposer_cls.WINDOW + transposer_cls.HOP),
                  float(lookahead) * nominal)
    return int(round(opening / nominal))


def _to_pcm(samples):
    return (np.clip(samples, -1.0, 1.0) * 32767.0).round().astype("<i2").tobytes()


class FastHarmonyLine:
    """Harmony copies on the fast profile -- for a voice or a string.

    Written for the line-in guitar (`instrument_harmony.GuitarHarmony` is this
    class under the instrument's switch) and reused by the microphone, because
    both ask the same two things: the copies, and no tuning. A press that only
    wants the copies was paying for the tuner's whole look-ahead (107 ms at
    the voice's own key) because `ChromaticTransposer` sizes its line for
    Speech Key's +12 -- a ratio of 2.0 this line never asks for.

    **No vocoder on the lead.** A guitar has nothing tuning it, and neither
    does a voice that only asked for harmony, so the lead is the dry capture
    read out of a ring (`_lead`), delayed by whatever the copies need, never
    resynthesised. Only the copies are shifted (`GuitarTransposer`, a 21 ms
    window), and the lead is held back to the slowest of them so the result is
    a chord rather than a slap: 26.7 ms for an interval above the voice, 53.3
    ms when a bass copy is singing (it reads at half speed, so its own window
    is twice the playing time).

    **The switch is read per frame**, so a copy chosen mid-sentence joins the
    words already leaving the mouth, one switched off is faded rather than
    cut, and a line that stops singing hands the lead back by crossfading the
    held audio into the live capture sample by sample.
    """

    #: The copies' opening silence, in stretched samples: 1280 is
    #: `GuitarTransposer`'s window + hop, 26.7 ms at 48 kHz.
    LOOKAHEAD = 1280
    #: The frame the capture thread hands over: 20 ms at 48 kHz.
    FRAME_SAMPLES = 960
    #: How long a copy takes to join or leave the mix. A cut is a click.
    FADE_FRAMES = 3
    #: The longest lead delay this line can need: a bass octave's copy reads at
    #: half speed, so the window that is 1280 samples of stretched audio is
    #: 2560 samples of playing time.
    MAX_LEAD = 2560

    def __init__(self, active=None):
        """``active`` is read per frame; None means "whenever a copy is wanted".

        The microphone passes its own Harmony switch; the guitar's subclass
        answers from the instrument option, so neither caller can leave this
        line singing when its feature is off.
        """
        self._active = active
        self._lock = threading.Lock()
        #: The last seconds of dry audio, so a lead delay that grows does not
        #: have to start from silence and a rebuild repeats no more than the
        #: delay it just took.
        self._dry = np.zeros((0,), dtype=np.float32)
        self._voices = []
        self._target = 0
        self._previous = 0
        #: 1.0 while the lead is passed through untouched, 0.0 while it is
        #: held back for the copies; ramped, never stepped.
        self._bypass = 1.0

    def _reading(self):
        """Whether this line is allowed to sing, asked once per frame."""
        return True if self._active is None else bool(self._active())

    # ------------------------------------------------------------- the frame
    def shape(self, pcm):
        """One 20 ms mono frame in, one out -- unchanged unless it is singing."""
        reading = self._reading()
        if not reading and not self._voices:
            with self._lock:
                self._dry = np.zeros((0,), dtype=np.float32)
                self._bypass = 1.0
                self._target = self._previous = 0
            return pcm
        samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
        frames = samples.size
        if frames <= 0:
            return pcm
        with self._lock:
            self._remember(samples)
            self._aim(ids() if reading else ())
            voices = [voice for voice in self._voices
                      if voice.wanted or voice.gain > 0.0]
            if not voices and self._bypass >= 1.0:
                self._voices = []
                return pcm
            lead = self._lead(frames, self._target)
            if self._previous != self._target:
                # The slowest copy changed, so the lead is read from a new
                # place in its own history. Both places are blended ACROSS the
                # frame, sample by sample: a per-frame step in the blend is a
                # step in the audio (measured 0.10 -- twelve times the wave's
                # own slope -- which is the click it sounds like).
                old = self._lead(frames, self._previous)
                blend = np.linspace(0.0, 1.0, frames, endpoint=False,
                                    dtype=np.float32)
                lead = old * (1.0 - blend) + lead * blend
                self._previous = self._target
            goal = 0.0 if voices else 1.0
            was = self._bypass
            self._bypass = self._ramp(was, goal)
            if was != self._bypass:
                # The same rule on the way in and out: the delayed lead and
                # the live voice are 26 ms apart, so the two are crossfaded
                # per sample rather than per frame.
                blend = np.linspace(was, self._bypass, frames, endpoint=False,
                                    dtype=np.float32)
                lead = lead * (1.0 - blend) + samples * blend
            elif self._bypass > 0.0:
                lead = lead * (1.0 - self._bypass) + samples * self._bypass
            if not voices:
                # Everything has faded: the lead is through again (or on its
                # way), so this frame is the plain lead and nothing else.
                self._voices = []
                return _to_pcm(lead)
            return _to_pcm(self._mix(lead, pcm, frames, voices))

    def _mix(self, lead, pcm, frames, voices):
        """Sum the lead and its copies into ONE frame, in their own shares.

        The fades are ramped SAMPLE BY SAMPLE, not once per frame. A voice that
        leaves in three 1/3 steps of a 20 ms frame is three steps in the audio --
        measured 0.097 of full scale on a 0.3 tone, nine times its own slope,
        which is the click it sounds like. Carrying `voice.gain` from frame to
        frame and spreading each step over the frame leaves no step at the
        boundary, and the lead's own duck follows the same ramp (`total`), so
        the mix stays inside full scale through the whole fade.
        """
        weights = []
        total = np.ones(frames, dtype=np.float32)
        for voice in voices:
            start = voice.gain
            if (start <= 0.0 and voice.wanted
                    and voice.fed < voice.lead_in):
                # The copy's vocoder opens with `lead_in` frames of silence. A
                # fade that has already started when its first real sample
                # arrives puts a waveform in at whatever phase it happens to
                # begin on -- a step (measured 0.063, six times the wave's
                # slope). So the fade waits for the audio it is fading.
                end = 0.0
            else:
                end = voice.advance_gain()
            voice.fed += 1
            ramp = np.linspace(start, end, frames,
                               endpoint=False, dtype=np.float32)
            weight = ramp * harmony_share(voice.interval)
            weights.append((voice, weight))
            total = total + weight
        mix = lead / total
        for voice, weight in weights:
            mix = mix + voice.process(pcm) * (weight / total)
            if not voice.wanted and voice.gain <= 0.0 and voice in self._voices:
                self._voices.remove(voice)
        return mix

    # ------------------------------------------------------------- the copies
    def _aim(self, wanted):
        """Keep one copy per wanted interval, on the timeline they need.

        A copy below the voice needs a longer lead delay than one above it,
        and every copy is aligned to that same delay -- so a change of that
        delay is a rebuild for all of them (the old ones fade out on their own
        timeline rather than being cut).
        """
        delays = [copy_delay(interval, self.LOOKAHEAD) for interval in wanted]
        target = max([self.LOOKAHEAD] + delays)
        self._previous, self._target = self._target, target
        for interval in wanted:
            if not any(voice.interval == interval and voice.lead == target
                       for voice in self._voices):
                self._voices.append(self._build(interval, target))
        for voice in self._voices:
            voice.wanted = (voice.interval in wanted and voice.lead == target)

    def _build(self, interval, target):
        """One copy, aimed at its plain interval, on the line's timeline.

        `HarmonyVoice` builds the shifter and the limits but never chooses a
        ratio -- the tuner's line steers it every frame. This line has nothing
        to steer with, so the interval is set once, here, at the catalogue's
        own size (`harmony_plain_offset`: a major third is four
        semitones down to the last one), and it stays there for the copy's
        life. Skipping this leaves the copy at ratio 1.0 -- a unison doubling
        rather than a harmony, and one that arrives at a different time than
        the lead.
        """
        voice = HarmonyVoice(interval, transposer_cls=GuitarTransposer,
                             lookahead=self.LOOKAHEAD, lead_delay=target)
        offset = harmony_plain_offset(interval)
        if offset is not None:
            voice.transposer.set_pitch_offset(offset)
        #: Which timeline this copy was built for (`_aim` rebuilds on a change).
        voice.lead = target
        #: Frames this copy has been fed, and the frames of silence its own
        #: line opens with -- the fade waits for both (`_mix`). The silence is
        #: the LINE's delay, not the copy's own: a treble copy is held back to
        #: a bass copy's timeline (`_align`), so its first real sample arrives
        #: `target` samples after it is built, whatever its interval is.
        voice.fed = 0
        voice.lead_in = int(np.ceil(float(target)
                                    / float(self.FRAME_SAMPLES))) + 1
        return voice

    # ------------------------------------------------------------- the lead
    def _remember(self, samples):
        """Keep the dry audio the lead can be read from, and no more."""
        keep = self.MAX_LEAD + 4 * samples.size
        self._dry = np.concatenate((self._dry, samples))
        if self._dry.size > keep:
            self._dry = self._dry[-keep:]

    def _lead(self, frames, delay):
        """The frame the copies are aligned to: this audio, `delay` samples ago."""
        needed = frames + delay
        dry = self._dry
        if dry.size < needed:
            dry = np.concatenate((np.zeros(needed - dry.size, np.float32), dry))
        if delay <= 0:
            return dry[-frames:]
        return dry[-needed:-delay]

    def _ramp(self, value, goal):
        step = 1.0 / float(self.FADE_FRAMES)
        if value < goal:
            return min(goal, value + step)
        if value > goal:
            return max(goal, value - step)
        return value

    # ------------------------------------------------------------- lifecycle
    def latency_samples(self):
        """How far the lead is held back right now (0 when nothing sings)."""
        with self._lock:
            return 0 if not self._voices else self._target

    def tail_frames(self, frame_bytes=FRAME_BYTES):
        """The held audio, as whole frames, when the press ends.

        A press releases the line and still owes its listener the audio the
        line was holding -- otherwise the last syllable stops mid-word. The
        lead's tail is the dry audio not yet read out of the ring; each copy
        has one of its own, and they are mixed in the same shares as a frame,
        so the release sounds like the rest of the sentence rather than a cut.
        """
        step = max(2, int(frame_bytes))
        frame_samples = step // 2
        with self._lock:
            if not self._voices or self._target <= 0:
                return []
            lead_tail = self._dry[-self._target:]
            for voice in self._voices:
                voice.flush()
            if lead_tail.size < frame_samples:
                lead_tail = np.concatenate(
                    (np.zeros(frame_samples - lead_tail.size, np.float32),
                     lead_tail))
            out = []
            start = 0
            while start + frame_samples <= lead_tail.size:
                piece = lead_tail[start:start + frame_samples]
                start += frame_samples
                weights = [(voice, harmony_share(voice.interval))
                           for voice in self._voices]
                total = 1.0 + sum(weight for _voice, weight in weights)
                mix = piece / total
                for voice, weight in weights:
                    mix = mix + voice.tail(frame_samples) * (weight / total)
                out.append(_to_pcm(mix))
            self._voices = []
            self._dry = np.zeros((0,), dtype=np.float32)
            self._target = self._previous = 0
            self._bypass = 1.0
            return out

    def reset(self):
        """Forget the session: the next frame is a fresh voice."""
        with self._lock:
            self._dry = np.zeros((0,), dtype=np.float32)
            self._voices = []
            self._target = self._previous = 0
            self._bypass = 1.0


class HarmonyVoice:
    """One extra copy of the tuned voice, an interval above -- or below -- it.

    The copy is the SAME microphone frame through a shifter of its own, set to
    the lead's correction plus the interval (`AutoTuner.harmony_offset`), mixed
    into the lead's frame rather than sent as a second stream: a listener, a PA
    slot, a cinema room's playout and a cabinet's timeline count frames, and one
    frame in is still one frame out.

    **The two shifters do not delay the voice by the same amount, and that is
    what this class is for.** A line delays the voice by its look-ahead divided
    by its ratio, so a copy a third above (ratio 1.26) arrives about 22 ms
    BEFORE the lead and one an octave above (ratio 2.0) about 53 ms before it.
    Summed raw that is not a chord: it is the same voice twice, tens of
    milliseconds apart -- heard as a slap or a phasey double, with every
    consonant smeared. So the copy's output is held back by exactly that
    difference, read out of a ring by how far the lead is ahead, and the
    distance is slewed a few samples per frame instead of jumped when a glide
    moves the ratio.

    **A copy BELOW the voice is the other case, and it needs the opposite
    arithmetic.** Its ratio is under 1, so reading slower than the source turns
    the same opening silence into a LONGER delay -- an octave down would arrive
    107 ms after the lead, and no ring can read audio the vocoder has not
    produced yet. Its line is therefore built with exactly `LOOKAHEAD * ratio`
    of opening silence (`ChromaticTransposer(exact_lookahead=True)`), which puts
    its delay at `LOOKAHEAD / ratio_lead` -- the lead's own delay, whatever the
    correction does -- so `_align` comes out zero and the copy needs no ring.

    **A bass copy is mixed as an undertone, not as a second singer.** It is
    quieter than the lead (`harmony_share`) and low-passed
    (`BASS_TONE_HZ`), because an octave below at the lead's own level is
    the same words doubled and smeared; what a bass voice is, is a note without
    the consonants.
    """

    #: How long the copy takes to join or leave the mix, in frames. A copy
    #: switched off mid-sentence is faded; cutting it is a click.
    FADE_FRAMES = 3
    #: Taps in the bass copy's own low-pass. A corner this low needs a long
    #: kernel: the transition band of a windowed sinc is about `rate / taps`, and
    #: 63 taps would smear a 380 Hz corner over 763 Hz -- the note it is meant to
    #: keep lands in the roll-off (measured 0.0004 of its level). 511 makes that
    #: 94 Hz, so the fundamental and its first harmonics are kept whole.
    TONE_TAPS = 511

    def __init__(self, interval_id, transposer_cls=None, lookahead=None,
                 lead_delay=None):
        """One copy of a voice or a string, an interval above it -- or below.

        The three optional dials are for a line that is NOT the broadcast
        voice: the line-in guitar's harmony (`libs/instrument_harmony.py`) is
        played back under a ringing string, so it needs a shorter window
        (`GuitarTransposer`), a fixed opening silence, and a lead whose own
        delay is 26.7 ms rather than the tuner's 107. They default to the
        voice's own numbers, so a caller that passes none gets the same line it
        always got, byte for byte.
        """
        import numpy as np
        if transposer_cls is None:
            transposer_cls = ChromaticTransposer
        self.interval = clamp_harmony(interval_id)
        step = harmony_semitones(self.interval)
        # This copy may be asked for the interval PLUS the lead's correction,
        # and an octave above a voice a semitone and a half flat is 13.5
        # semitones: the line gets its own limit, its own anti-aliasing filter
        # ceiling and a look-ahead that reaches (`transpose.py`).
        limit = harmony_pitch_limit(step)
        nominal = 2.0 ** (step / 12.0)
        if step < 0:
            # Below the voice: the opening silence is SHORTER by the ratio, so
            # the delay (silence / ratio) is the lead's own -- see the class
            # docstring. Its output never runs faster than its input, so the
            # anti-aliasing filter it pins for an upward copy is not engaged.
            base = (transposer_cls.LOOKAHEAD if lookahead is None
                    else float(lookahead))
            self.transposer = transposer_cls(
                channels=1, lookahead=int(round(base * nominal)),
                exact_lookahead=True)
        elif lookahead is None:
            self.transposer = transposer_cls(
                channels=1, lookahead=transposer_cls.lookahead_for(limit))
        else:
            # An explicit opening silence, sized in the copy's own time: read
            # at `nominal`, it lands `lookahead` samples behind the source
            # whatever the interval is -- which is how every copy of a guitar
            # chord arrives on the same beat.
            self.transposer = transposer_cls(
                channels=1, lookahead=int(round(float(lookahead) * nominal)),
                exact_lookahead=True)
        self.transposer.pitch_limit = limit
        if step >= 0:
            self.transposer.pin_lowpass(2.0 ** (limit / 12.0))
        self._buffer = np.zeros((0,), dtype=np.float32)
        #: How far the copy's output is held back so it lines up with the lead.
        #:
        #: **A FIXED distance, never a follow-the-lead loop.** The two delays
        #: move when the tuner glides its correction, and chasing that moved the
        #: read point 8 samples a frame -- which is a 0.83 % time-scale change,
        #: measured as the copy sitting 14 cents FLAT (a slow delay sweep is a
        #: pitch bend, not an alignment). Fixed, the copy is a few samples out of
        #: place when the correction moves instead, which is a fraction of a
        #: millisecond: under 2 ms against two frames of detune.
        target = (transposer_cls.LOOKAHEAD if lead_delay is None
                  else float(lead_delay))
        self._align = max(0.0, target - self.transposer.lookahead / nominal)
        #: The bass copy's low-pass kernel and the samples it is still holding
        #: from the frame before, or None for a copy above the voice, which is
        #: passed through untouched.
        self._tone = None
        self._tone_tail = None
        if harmony_is_bass(self.interval):
            self._tone = self._tone_kernel(BASS_TONE_HZ)
        #: 1.0 while it sings, 0.0 once it has faded out and can be dropped.
        self.gain = 0.0
        #: Whether the menu still asks for it (a fade-out keeps it alive).
        self.wanted = True

    @classmethod
    def _tone_kernel(cls, hz):
        """A windowed-sinc low-pass at ``hz``: the bass copy's own voice.

        Linear phase, like the transposer's anti-aliasing filter, so it is a
        delay and never a smear: 255 samples at 511 taps, 5 ms. That lands the
        bass copy a quarter of a frame behind the lead, which on a hum with no
        consonants in it is not a slap -- and buying it back would take the
        opening silence below the vocoder's own arithmetic.
        """
        import numpy as np
        taps = np.arange(cls.TONE_TAPS) - (cls.TONE_TAPS - 1) / 2.0
        cutoff = float(hz) / 48000.0          # cycles/sample; Nyquist is 0.5
        kernel = (2.0 * cutoff * np.sinc(2.0 * cutoff * taps)
                  * np.hamming(cls.TONE_TAPS))
        total = kernel.sum()
        if abs(total) < 1e-9:
            return None
        return (kernel / total).astype(np.float32)

    def _voice_tone(self, piece):
        """One block of the copy through its low-pass, or untouched if it has none."""
        if self._tone is None:
            return piece
        import numpy as np
        needed = len(self._tone) - 1
        tail = (self._tone_tail if self._tone_tail is not None
                else np.zeros(needed, dtype=np.float32))
        data = np.concatenate((tail, piece.astype(np.float32)))
        out = np.convolve(data, self._tone, mode="valid")
        self._tone_tail = np.array(data[-needed:], copy=True)
        return out

    def advance_gain(self):
        """One frame's step of the join/leave fade; answers the gain to use."""
        target = 1.0 if self.wanted else 0.0
        step = 1.0 / self.FADE_FRAMES
        if self.gain < target:
            self.gain = min(target, self.gain + step)
        elif self.gain > target:
            self.gain = max(target, self.gain - step)
        return self.gain

    def process(self, pcm):
        """Shape one frame and answer it, delayed to the lead's own timeline."""
        import numpy as np
        out = np.frombuffer(self.transposer.process(bytes(pcm)),
                            dtype="<i2").astype(np.float32) / 32768.0
        self._buffer = np.concatenate((self._buffer, out))
        frames = out.size
        align = int(round(self._align))
        start = max(0, self._buffer.size - frames - align)
        piece = self._buffer[start:start + frames]
        if piece.size < frames:
            piece = np.concatenate((np.zeros(frames - piece.size, np.float32), piece))
        keep = frames + align + 2 * frames
        if self._buffer.size > keep:
            self._buffer = self._buffer[-keep:]
        return self._voice_tone(piece)

    def flush(self):
        """Hand the copy's own look-ahead into the ring, for the tail."""
        import numpy as np
        out = np.frombuffer(self.transposer.flush(),
                            dtype="<i2").astype(np.float32) / 32768.0
        self._buffer = np.concatenate((self._buffer, out))

    def tail(self, frames):
        """The next ``frames`` samples of the copy's aligned tail."""
        import numpy as np
        start = int(round(self._align))
        piece = self._buffer[start:start + frames]
        if piece.size < frames:
            piece = np.concatenate((np.zeros(frames - piece.size, np.float32), piece))
        if self._buffer.size >= frames:
            self._buffer = self._buffer[frames:]
        else:
            self._buffer = np.zeros((0,), dtype=np.float32)
        return self._voice_tone(piece)
