"""Auto-Tune: the broadcast voice, snapped to the notes it was meant to sing.

Speech Key moves a whole voice by one fixed interval. Auto-Tune moves it by a
different interval every 20 ms: the distance between the note being sung and
the note it belongs to. A singer who lands 30 cents flat of a B is pushed up by
30 cents; the next syllable, 20 cents sharp of a D, is pushed down by 20. The
result is the effect an Auto-Tune pedal is bought for -- a voice that sits on
the notes -- and the glide between those decisions is what makes it either an
effect (instant) or a correction nobody notices (slow).

"The note it belongs to" is a choice, and `SCALES` is that choice: every
semitone (chromatic), the seven of a major or a minor key -- natural, harmonic
or melodic -- or the five of a pentatonic, in the key the player picked. The
chromatic grid was the first version and stays the default, because it is the
only one that can never move the voice more than half a semitone.

**A scale is four things, and the menu reads all four.** Its id, its name, its
pitch classes, and the sentence a player who has never met the word needs: what
it sounds like and what it is for. The harmonic and melodic minors are the
two that proved why the fourth field matters -- "Harmonic Minor" is a name, and
"a minor key with its seventh raised a semitone, which is what pulls a phrase
back to its own note" is the reason somebody picks it.

Three pieces, two of them already in the project
-----------------------------------------------
* **the pitch** comes from `libs/pitch.py`'s YIN (`yin_pitch_and_clarity`), the
  detector the line-in guitar already uses. It runs on the microphone's own
  20 ms frames, three of them at once: 60 ms is the shortest window that can
  see three periods of a low male voice, and a shorter one locks onto the
  second harmonic of exactly the voices this is for.
* **the shift** is `libs/music_bot/transpose.py`'s `ChromaticTransposer`, the
  song's shifter, through its continuous entry point `set_pitch_offset`: the
  same line, the same look-ahead, the same one-frame-in-one-frame-out
  contract, so the PA's frame sequence and a room's playout are untouched.
* **only the decision** between them is new, and it is this module.

Latency, and why the correction lands on the note it measured
-------------------------------------------------------------
The transposer's output lags its input by its look-ahead -- 107 ms at a ratio
near 1, and a correction is always a ratio near 1 (half a semitone at most on
the chromatic grid, and a scale's widest gap -- a whole tone or a semitone and
a half -- is still only 1.5). A detector needs 60 ms of audio before it can name
a note. Put together, the delay is longer than the window, so nothing extra is
paid: `AutoTuneLine` hands `AutoTuner.detect` the 60 ms of the press that ended
one look-ahead ago -- the audio reaching the read head -- and the correction
that comes back is applied to exactly that audio.

Detecting on the **newest** frame instead is the tempting mistake, and a held
note cannot show it: the ratio it chooses is applied to audio that is 107 ms
older, so on a slide it tunes a note by the pitch of a note nobody has sung
yet. Measured on an octave-per-three-seconds slide (400 cents a second) the
output sat 38-48 cents flat -- a third of a semitone, which is the effect gone.
`test_the_detector_hears_the_audio_the_read_head_is_reading` pins the
mechanism (a 600-cent-a-second slide, the analysis centre against the newest
frame), not only its result.

The window is handed over as **one contiguous slice**, never as three frames to
stack. The delay moves by a few samples when the ratio moves, so frames taken
that way overlap by ~74 samples and put a phase jump at every join: YIN read a
steady 217.47 Hz tone as 222, 224, 215 Hz from frame to frame, and a tuner
chasing that wobbled the output 40 cents. `track()` still stacks, for a caller
holding true consecutive frames; the line calls `detect()`.

What it costs is the look-ahead itself, on a broadcast that would otherwise be
untouched: a voice with Auto-Tune on arrives about a tenth of a second later
than an untuned one, exactly as Speech Key's does. That is stated in the player
notes rather than hidden.

Every note set repeats by the octave, so an octave error is not an error
------------------------------------------------------------------------
The correction is measured from the NEAREST note, and a semitone grid -- like
every scale in `SCALES` -- is transposition-invariant: 220 Hz and 440 Hz are
both exactly on it, so a detector that reports the wrong octave still reports
the right note, and the same correction. There is no octave-confirmation stage
here because there would be nothing for it to guard. (A detector's
THIRD-harmonic slip is a different thing -- a fifth -- and is what
`MIN_CLARITY` and the glide are for: one frame that disagrees with its
neighbours is a 20 ms wobble, not a jump.)

How much of the note is taken, and how much of the singer is left
-----------------------------------------------------------------
`TUNE_AMOUNTS` scales the correction rather than widening it: 100% puts the
voice on the nearest note of the scale, 50% takes half of the distance and
leaves the other half in the voice, 0% is the voice as it was sung. It is the
one dial here that is taste rather than physics -- nothing about a note set
says how tuned a singer wants to sound -- and it cannot go past 100%: the
nearest note IS the whole correction, so more of it would carry the voice past
the note it was aiming for.

What is deliberately not here
-----------------------------
* **No formant preservation.** The chromatic correction is at most half a
  semitone, which does not move a voice's formants enough to hear -- that is
  why this stays inside the shifter the project already has instead of a
  second, newer one. A five-note scale can ask for a semitone and a half, and
  a move that wide is heard as a formant shift: the player notes say so, and
  it is why chromatic is the default.
* **No voice-only gate on the detector's range.** `MIN_FREQ`/`MAX_FREQ` cover a
  sung voice (70 Hz to 1 kHz); a sibilant outside it is unpitched anyway and
  falls to the clarity gate, which leaves the ratio where it was.

Pinned by `client/tests/test_music_bot_autotune.py`.
"""

import collections

import numpy as np

from ..pitch import NOTE_NAMES, yin_pitch_and_clarity
from .transpose import ChromaticTransposer

#: One microphone frame: 20 ms of 48 kHz mono, the unit the whole voice path is
#: built out of (`libs/voice_chat.VOICE_FRAME_BYTES`).
FRAME_SAMPLES = 960
SAMPLE_RATE = 48000


class AutoTuner:
    """One press's tuning decisions: a window in, an offset in semitones out.

    ``detect(window)`` is the entry the line uses. It is handed ONE contiguous
    stretch of the press -- the 60 ms that reached the read head -- and answers
    the distance to the nearest note, positive when the singer is flat, so
    ``set_pitch_offset`` can push the voice up by it. It answers ``None`` while
    it has nothing to say (a silent window, a breath, a consonant), which means
    "keep the ratio you have", not "go back to the original voice".

    ``track(frame)`` is the same decision for a caller holding true consecutive
    20 ms frames: it stacks ``WINDOW_FRAMES`` of them and calls ``detect``.
    """

    WINDOW_FRAMES = 3       # 60 ms: three periods of a 50 Hz tone, two of 100
    MIN_FREQ = 70.0         # below a male speaking/singing voice
    MAX_FREQ = 1000.0       # above a soprano; keeps the lag search honest
    RMS_GATE = 0.008        # below this the frame is silence, not a note
    MIN_CLARITY = 0.55      # 1 - YIN's aperiodicity; noise sits far below this
    # The decision layer, which is what keeps a held note from wobbling: a
    # pitch is read through three frames, a new note has to be the nearer one
    # for 160 ms before the target follows it, and a note that has clearly been
    # left (further than the scale's own pull plus a quarter tone) is followed
    # at once.
    PITCH_MEDIAN = 3        # frames of measured pitch; one frame's reading is not a note
    TARGET_CONFIRM = 8      # frames a new note must stay nearer before it wins
    SWITCH_MARGIN = 0.06    # semitones: merely nearer is not enough, it must be clearer
    RAMP_SEMITONES = 0.35   # the most one frame may move the voice (see `_applied`)

    def __init__(self, sample_rate=SAMPLE_RATE, mode=None, root=None):
        self.sample_rate = int(sample_rate)
        self.mode = clamp_mode(DEFAULT_MODE if mode is None else mode)
        self.root = clamp_root(DEFAULT_ROOT if root is None else root)
        self._window = collections.deque(maxlen=self.WINDOW_FRAMES)
        #: The last frequency YIN named, or None when the last frame was not
        #: pitched. Diagnostic (and what the tests read).
        self.detected_hz = None
        #: The last correction applied, in semitones.
        self.offset = 0.0
        #: The last clarity YIN reported, 0..1.
        self.clarity = 0.0
        #: How much of the correction is used, in percent (see `TUNE_AMOUNTS`).
        self.amount = DEFAULT_AMOUNT
        #: The note this press is being tuned to, as a MIDI number, or None.
        self.held_note = None
        #: The note that would take over, and how many frames it has asked for.
        self._pending_note = None
        self._pending_frames = 0
        #: Measured pitch over a few frames, so one reading cannot name a note.
        self._pitch = collections.deque(maxlen=self.PITCH_MEDIAN)
        #: The correction the line is actually being sent, before Tune Amount.
        self._applied = 0.0
        #: The two harmony copies' intervals (see `HARMONY_INTERVALS`).
        self.harmony = (DEFAULT_HARMONY, DEFAULT_HARMONY)
        #: The pitch this press is singing right now, as a MIDI number, or None.
        #: The harmony copies are placed from it, so one detection serves three
        #: voices and a copy never gets a note of its own to disagree with.
        self.sung_midi = None

    def reset(self):
        """Forget the press: the next frame is a fresh microphone."""
        self._window.clear()
        self._pitch.clear()
        self.held_note = None
        self._pending_note = None
        self._pending_frames = 0
        self._applied = 0.0
        self.sung_midi = None
        self.detected_hz = None
        self.offset = 0.0
        self.clarity = 0.0

    def set_scale(self, mode, root):
        """Which notes this press may be moved to (a scale, in a key).

        The held note is dropped: it was chosen from the note set that has just
        changed, and a C-major D is not a note of a pentatonic.
        """
        self.mode = clamp_mode(mode)
        self.root = clamp_root(root)
        self.held_note = None
        self._pending_note = None
        self._pending_frames = 0
        return self.mode, self.root

    def set_amount(self, percent):
        """How much of the correction to use: 100 puts the voice on the note."""
        self.amount = clamp_amount(percent)
        return self.amount

    def set_harmony(self, first=None, second=None):
        """Which intervals the two harmony copies sing, if any.

        Setting either to ``'off'`` silences that copy. Nothing is rebuilt here
        -- the line does that, and it needs the old copy's delay to fade it out
        rather than cut it. The intervals arrive as None when the caller has no
        opinion, which means Off; the defaults are read here rather than in the
        signature because the catalogue is defined below this class.
        """
        first = DEFAULT_HARMONY if first is None else first
        second = DEFAULT_HARMONY if second is None else second
        self.harmony = (clamp_harmony(first), clamp_harmony(second))
        return self.harmony

    def harmony_offset(self, interval_id):
        """What one harmony copy's shifter is asked for, in semitones, or None.

        The copy is placed from the note the LEAD has landed on (or is gliding
        to), plus the interval the scale gives there -- never from its own
        reading of the pitch, so the two cannot disagree about which note is
        being sung. The Tune Amount is already inside `offset`, and the interval
        rides on top of it unchanged: at 50% the lead keeps half the singer's
        error and the copy keeps the same half, so the interval between them is
        exactly the interval that was chosen.

        None means "the copy has nothing to sing yet" (no note has been named,
        or the interval is Off), and the line holds the last ratio rather than
        dragging the copy back to the voice at every breath.
        """
        interval_id = clamp_harmony(interval_id)
        if interval_id == "off" or self.held_note is None or self.sung_midi is None:
            return None
        target = self.nearest_note(
            self.held_note + harmony_semitones(interval_id), self.mode, self.root)
        if target is None:
            return None
        return float(self.offset) + (target - self.held_note)

    def harmony_offsets(self):
        """One answer per harmony slot, in the order `self.harmony` holds them."""
        return tuple(self.harmony_offset(interval_id)
                     for interval_id in self.harmony)

    @staticmethod
    def grid_correction(hz):
        """Semitones from ``hz`` to the nearest note of every semitone.

        The chromatic answer: A4 = 440 Hz is the reference, and the result is
        always inside +/-0.5. `scale_correction` is the general one.
        """
        return AutoTuner.scale_correction(hz, "chromatic", 0)

    @staticmethod
    def nearest_note(midi, mode=None, root=None):
        """The MIDI number of the nearest note of the scale to ``midi``.

        The single place the "which note does this belong to" question is
        answered; `scale_correction` is this note minus the pitch, and the
        decision layer holds this note across frames.
        """
        if midi is None:
            return None
        mode = DEFAULT_MODE if mode is None else mode
        root = DEFAULT_ROOT if root is None else root
        classes = scale_classes(mode)
        if not classes:
            best = float(np.round(midi))
        else:
            best = None
            for offset in classes:
                base = float(clamp_root(root) + offset)
                candidate = 12.0 * round((midi - base) / 12.0) + base
                if best is None or abs(candidate - midi) < abs(best - midi) - 1e-9:
                    best = candidate
        if best is None or best < 0.0 or best > 127.0:
            return None
        return best

    @staticmethod
    def scale_correction(hz, mode=None, root=None):
        """Semitones from ``hz`` to the nearest note OF THE CHOSEN SCALE.

        Positive means the singer is flat and the voice must go up. The answer
        is bounded by the widest gap in the scale: half a semitone on the
        chromatic grid, a whole one in a seven-note scale (every note outside
        it sits exactly one semitone from both neighbours), and a semitone and
        a half in a five-note one, where a gap is three semitones wide.

        A tie -- a note exactly between two scale notes, which is every
        out-of-scale note in a major scale -- goes to the lower one, so the
        same input always answers the same way.
        """
        if hz is None or hz <= 0.0:
            return None
        mode = DEFAULT_MODE if mode is None else mode
        root = DEFAULT_ROOT if root is None else root
        midi = 69.0 + 12.0 * float(np.log2(hz / 440.0))
        best = AutoTuner.nearest_note(midi, mode, root)
        if best is None:
            return None
        return best - midi

    def track(self, frame):
        """Feed one 20 ms mono frame; answer the offset, or None to hold.

        The tracker stacks `WINDOW_FRAMES` frames and analyses the window they
        make. **That only works while the frames are contiguous.** A caller
        whose audio moves under it -- `AutoTuneLine`, which hands over the
        audio of one look-ahead ago and therefore a window that slides by a few
        samples when the ratio moves -- must call `detect` with the window
        itself: stacking three frames that overlap by 74 samples puts a phase
        jump at every join, and YIN read a steady 217 Hz tone as 222, 224, 215
        from frame to frame (measured).
        """
        samples = np.asarray(frame, dtype=np.float32)
        if samples.size == 0:
            return None
        if float(np.sqrt(np.mean(samples.astype(np.float64) ** 2))) < self.RMS_GATE:
            # Silence is not a note: the window is dropped (a note after a pause
            # must not be averaged with the one before it) but the last
            # correction stands -- `None` means "keep the ratio you have", and
            # the line would otherwise jump toward the original voice at every
            # breath a singer takes.
            self._window.clear()
            self.detected_hz = None
            return None
        self._window.append(samples)
        if len(self._window) < self.WINDOW_FRAMES:
            return None
        return self.detect(np.concatenate(self._window))

    def detect(self, window):
        """Name the note in ONE contiguous window; answer the offset, or None.

        The window must be contiguous audio of at least two or three frames:
        YIN needs two periods of the lowest frequency it is asked about.

        The answer is the distance from the pitch to the note the press is
        being held on (`_hold_note`), not to whatever note this one frame likes
        best: a vibrato that crosses a note boundary must not drag the target
        back and forth every 90 ms, and a note change must not jump the ratio
        in a single frame. `RAMP_SEMITONES` is the most one frame may move, so
        a note change is a short bend (about three frames for a semitone) and
        a vibrato of a few cents is followed smoothly.
        """
        samples = np.asarray(window, dtype=np.float32)
        if samples.size == 0:
            return None
        if float(np.sqrt(np.mean(samples.astype(np.float64) ** 2))) < self.RMS_GATE:
            self._window.clear()
            self.detected_hz = None
            return None
        hz, clarity = yin_pitch_and_clarity(
            samples, sr=self.sample_rate,
            min_freq=self.MIN_FREQ, max_freq=self.MAX_FREQ)
        self.clarity = clarity
        if hz is None or clarity < self.MIN_CLARITY:
            # A breath, a consonant, a noisy frame: say nothing, hold the ratio.
            # The pitch history is dropped too -- a note after a pause must not
            # be averaged with the one before it -- but the held note stays, so
            # the ratio does not fall back to the singer at every breath.
            self.detected_hz = None
            self._pitch.clear()
            return None
        self.detected_hz = hz
        self._pitch.append(hz)
        pitch = float(np.median(self._pitch))
        midi = 69.0 + 12.0 * float(np.log2(pitch / 440.0))
        self.sung_midi = midi
        note = self.nearest_note(midi, self.mode, self.root)
        if note is None:
            return None
        self._hold_note(note, midi)
        correction = self.held_note - midi
        # A held note can be left behind by a slide; the line's anti-aliasing
        # filter was pinned at the widest pull the catalogue allows, so the
        # correction never asks for more than that (`MAX_PULL`).
        pull = widest_pull(self.mode)
        correction = max(-pull, min(pull, correction))
        if self._pending_note is not None:
            # A vote is running, which means the pitch is nearer another note:
            # pulling it onto the held one would drag the voice the WRONG way
            # for the length of the vote -- a note change a semitone up, sung
            # 30 cents flat, was dragged half a semitone down and then taken a
            # semitone and a half back (measured: the output's largest step
            # 0.37 against 0.20 with the old decision). Freeze the correction
            # where it is: the vote either completes, and the ramp takes it to
            # the new note, or it fails, and nothing moved at all.
            correction = self._applied
        # The ramp: a note change moves the voice over a few frames, so the
        # shifter is never asked for a new interval in one 20 ms step. A
        # correction this small (a vibrato, a drift) arrives on the frame it is
        # measured, which is what keeps a fast passage in tune.
        delta = correction - self._applied
        if abs(delta) <= self.RAMP_SEMITONES:
            self._applied = correction
        else:
            self._applied += (self.RAMP_SEMITONES if delta > 0
                              else -self.RAMP_SEMITONES)
        # The amount is a share of the correction, never a wider one: at 100%
        # the voice lands on the note, at 50% half the error is left in it, and
        # at 0% the answer is zero with the note still named. 0.0 is not None:
        # None means "keep the ratio you have", which would be the last note's
        # correction, not the singer's own voice.
        self.offset = self._applied * (self.amount / 100.0)
        return self.offset

    def _hold_note(self, note, midi):
        """Choose the note this press is tuned to, once, and keep it.

        Three rules, and each of them is an artifact that is gone:

        * **A held note is held.** A new note must be the nearer one by more
          than `SWITCH_MARGIN` for `TARGET_CONFIRM` frames before the target
          follows it. A singer sitting near the boundary between two notes --
          or a vibrato that swings across it -- used to flip the interval by a
          semitone every frame, which is heard as a wobble rather than as a
          tuning, and no glide speed can hide a target that moves 100 cents at
          50 Hz.
        * **A note that has clearly been left is followed at once.** Further
          than the scale's own pull plus a quarter tone, the pitch is not
          wobbling around this note, it is on another one: a slide, a trill or
          a fast passage should not wait out a confirmation window.
        * **The comparison is against the pitch, not the note.** Two notes can
          both be near a badly placed pitch; the one that is actually nearer
          is the one the voice belongs to.
        """
        if self.held_note is None:
            self.held_note = note
            return self.held_note
        pull = widest_pull(self.mode)
        if abs(midi - self.held_note) > pull + 0.25:
            self.held_note = note
            self._pending_note = None
            self._pending_frames = 0
            return self.held_note
        if abs(note - self.held_note) < 1e-9:
            # The held note is still the nearest one: whatever the other side
            # was asking for is forgotten, so a vibrato can cross the boundary
            # and come back without ever accumulating a vote.
            self._pending_note = None
            self._pending_frames = 0
            return self.held_note
        if abs(midi - note) + self.SWITCH_MARGIN >= abs(midi - self.held_note):
            self._pending_note = None
            self._pending_frames = 0
            return self.held_note
        if (self._pending_note is not None
                and abs(note - self._pending_note) < 1e-9):
            self._pending_frames += 1
        else:
            self._pending_note = note
            self._pending_frames = 1
        if self._pending_frames >= self.TARGET_CONFIRM:
            self.held_note = note
            self._pending_note = None
            self._pending_frames = 0
        return self.held_note


# ----------------------------------------------------------------- the scales

#: The note sets Auto-Tune may move a voice to, as pitch classes above the root.
#: Chromatic is every note -- what the first version did, and the only mode whose
#: correction is bounded by half a semitone. `() ` means every class, so it is
#: spelled out rather than listed (and it makes the root irrelevant, as a grid
#: with a note every semitone can have no key).
#: ``(id, menu name, pitch classes above the root, what the menu says it is)``.
#: Chromatic is every note -- what the first version did, and the only mode
#: whose correction is bounded by half a semitone. ``()`` means every class, so
#: it is spelled out rather than listed (and it makes the root irrelevant, as a
#: grid with a note every semitone can have no key).
#:
#: The fourth field is not decoration: the Settings menu describes every line it
#: shows (`client/tests/test_music_bot_menu_help.py`), and a scale is the one
#: choice in the Music Bot nobody can be expected to know by name.
SCALES = (
    ("chromatic", "Chromatic", (),
     "Every semitone, so the voice is only ever moved to the nearest note "
     "there is: half a semitone at most, either way. It cannot be in a key."),
    ("major", "Major", (0, 2, 4, 5, 7, 9, 11),
     "The major scale of the key you pick: the bright one most songs are "
     "written in. A note outside it is moved a semitone into it."),
    ("minor", "Minor", (0, 2, 3, 5, 7, 8, 10),
     "The natural minor scale: the darker relative of major. Same seven "
     "notes as its major, started from the sixth."),
    ("harmonic_minor", "Harmonic Minor", (0, 2, 3, 5, 7, 8, 11),
     "A minor key with its seventh raised a semitone. That half-step gap at "
     "the top is the harmonic-minor sound: a phrase is pulled back to the "
     "key note, so it suits anything that wants to sound classical, "
     "Eastern or metal."),
    ("melodic_minor", "Melodic Minor", (0, 2, 3, 5, 7, 9, 11),
     "A minor key with its sixth and seventh raised, which is how a minor "
     "melody climbs. It is minor at the bottom and major on top, so it "
     "takes a tune somewhere major cannot."),
    ("major_pentatonic", "Major Pentatonic", (0, 2, 4, 7, 9),
     "Five notes of the major scale, with every half step left out, so no "
     "two of them can clash. The safest scale there is."),
    ("minor_pentatonic", "Minor Pentatonic", (0, 3, 5, 7, 10),
     "Five notes of the minor scale: the rock and blues scale. The widest "
     "move is a semitone and a half, so it is heard as an effect."),
)
DEFAULT_MODE = "chromatic"
DEFAULT_ROOT = 0        # C
#: The twelve keys, named the way the rest of the project names a note.
ROOT_NAMES = tuple(NOTE_NAMES)


def widest_gap(mode):
    """The widest step between two neighbouring notes of the scale, in semitones.

    A voice is only ever moved to the nearest note of its scale, so this -- and
    not any property of the scale's size -- is what bounds the correction.
    """
    classes = tuple(sorted(scale_classes(mode)))
    if not classes:
        return 1                # every semitone, so every step is a semitone
    gaps = [b - a for a, b in zip(classes, classes[1:])]
    gaps.append(12 - classes[-1] + classes[0])
    return max(gaps)


def widest_pull(mode):
    """The furthest this scale can move a voice, in semitones: half its widest gap.

    Half a semitone on the chromatic grid, one in a major or natural minor (a
    two-semitone gap), a semitone and a half in a pentatonic or the harmonic
    minor (a three-semitone gap).
    """
    return widest_gap(mode) / 2.0


def clamp_mode(value):
    """A scale id that exists, whatever was handed in or saved."""
    for mode_id, _label, _classes, _note in SCALES:
        if value == mode_id:
            return mode_id
    return DEFAULT_MODE


def is_chromatic(mode):
    """True when every semitone belongs to the scale (so the key means nothing)."""
    return clamp_mode(mode) == "chromatic"


def clamp_root(value):
    """A key index 0..11 (C..B), whatever was handed in or saved."""
    try:
        index = int(round(float(value)))
    except (TypeError, ValueError):
        return DEFAULT_ROOT
    return index % 12


def scale_classes(mode):
    """The pitch classes above the root, or () for every note."""
    for mode_id, _label, classes, _note in SCALES:
        if mode_id == clamp_mode(mode):
            return classes
    return ()


#: The widest correction any scale can ask for, in semitones. Measured from the
#: catalogue rather than written down, so a scale added later can never ask for
#: more than the line's anti-aliasing filter was pinned at: the filter is pinned
#: once (`ChromaticTransposer.pin_lowpass`) and must never switch under a voice.
MAX_PULL = max(widest_pull(mode_id) for mode_id, _label, _classes, _note in SCALES)


def scale_note(mode):
    """What the menu says a scale is, for a player who has never met the name."""
    for mode_id, _label, _classes, note in SCALES:
        if mode_id == clamp_mode(mode):
            return note
    return SCALES[0][3]


def scale_says(mode):
    """The sentence spoken when a scale is chosen: how far it can move a voice.

    Measured from the scale's own gap rather than from how many notes it has.
    The two used to be the same question -- a seven-note scale could only pull
    a semitone -- and the harmonic minor broke that: it has seven notes and a
    three-semitone gap, so a voice outside it can be moved a semitone and a
    half, exactly as in a pentatonic.
    """
    pull = widest_pull(mode)
    if pull <= 0.5:
        return ("every semitone, so the voice is never moved more than "
                "half a semitone")
    if pull >= 1.5:
        return ("the widest move is a semitone and a half, which is heard "
                "as an effect rather than a correction")
    return ("the seven notes of the key, so a note outside it is brought "
            "into it by a semitone at most")


def mode_name(mode):
    """The menu's words for a scale: 'Major', 'Minor Pentatonic'..."""
    for mode_id, label, _classes, _note in SCALES:
        if mode_id == clamp_mode(mode):
            return label
    return SCALES[0][1]


def root_name(root):
    """The menu's words for a key: 'C', 'F#', ..."""
    return ROOT_NAMES[clamp_root(root)]


def scale_label(mode=DEFAULT_MODE, root=DEFAULT_ROOT):
    """'Chromatic', or 'C Major' -- the whole choice in one menu line.

    Chromatic is the only mode that has no key, so it is the only one that does
    not name one: a grid with a note every semitone cannot be in D.
    """
    if clamp_mode(mode) == "chromatic":
        return mode_name(mode)
    return f"{root_name(root)} {mode_name(mode)}"


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
HARMONY_INTERVALS = (
    ("off", "Off", 0,
     "No second voice: the tuned voice on its own."),
    ("third", "Third", 4,
     "A second copy of the voice a third above: the nearest note your scale "
     "has there, so it is a major third over some notes and a minor third "
     "over others -- the interval a second singer would take."),
    ("fifth", "Fifth", 7,
     "A second copy a fifth above: the strongest harmony there is, because "
     "it sounds like the same chord one voice higher."),
    ("sixth", "Sixth", 9,
     "A second copy a sixth above: wider and softer than a third, and the "
     "one that sounds like a duet rather than a chord."),
    ("seventh", "Seventh", 11,
     "A second copy a seventh above, a whole tone under the octave. It is the "
     "jazzier, more open harmony, and it wants a scale that has one -- a "
     "pentatonic answers with its own nearest note."),
    ("octave", "Octave", 12,
     "A second copy an octave above: the same note, higher, which thickens "
     "the voice instead of adding a note to it."),
)
DEFAULT_HARMONY = "off"


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


def harmony_pitch_limit(semitones):
    """The widest interval a harmony copy may ask its shifter for.

    The copy is the interval PLUS the tuning correction the lead is holding, and
    the transposer clamps what it is asked for. An octave above a voice that is
    a semitone and a half flat is 13.5 semitones, so the line this copy runs on
    is given its own limit and a look-ahead to match (`transpose.py`).
    """
    return abs(float(semitones)) + MAX_PULL + 0.01


# ------------------------------------------------------------------ the dials

#: Retune Speed: how fast the line walks to a correction, once per 20 ms frame.
#: The values are `ChromaticTransposer`'s own glide constant at Normal, so a
#: tuner at Normal moves the voice at the speed the project's key changes
#: already move it, and Instant is one frame (the hard effect).
#:
#: ``(menu name, glide, what the speed does)`` -- the third field is what Tab
#: reads on that line, so a speed is described where it is chosen.
SPEEDS = (
    ("Instant (hard tune)", 1.0,
     "The effect: the voice snaps onto the note in one frame, so a slide "
     "between two notes is heard as a step."),
    ("Fast", 0.85,
     "The effect, a little less sharp: the note arrives in about two frames."),
    ("Normal", ChromaticTransposer.GLIDE,
     "The speed this project's own key changes move a voice at: heard as "
     "singing in tune rather than as a machine."),
    ("Slow", 0.28,
     "A pull: on a held note you hear the voice travel to the note."),
    ("Very slow", 0.12,
     "A correction you only notice when the note lands, which is what a "
     "voice off the note needs."),
)
DEFAULT_SPEED = 2


def clamp_speed(value):
    """A Retune Speed index that exists, whatever was handed in or saved."""
    try:
        index = int(round(float(value)))
    except (TypeError, ValueError):
        return DEFAULT_SPEED
    return max(0, min(len(SPEEDS) - 1, index))


def speed_label(index=None):
    """The menu's words for a speed index."""
    index = DEFAULT_SPEED if index is None else clamp_speed(index)
    return SPEEDS[index][0]


def speed_glide(index=None):
    """The per-frame glide constant a speed index means."""
    index = DEFAULT_SPEED if index is None else clamp_speed(index)
    return SPEEDS[index][1]


def speed_note(index=None):
    """What the menu says this Retune Speed does (what Tab reads)."""
    index = DEFAULT_SPEED if index is None else clamp_speed(index)
    return SPEEDS[index][2]


#: Tune Amount: how much of a correction is used, in percent. The percent
#: scales the answer and never the range -- 100% puts the voice on the nearest
#: note of the scale, 50% leaves half the error in it, 0% is the voice as it was
#: sung -- so 100 is a ceiling of accuracy, not of effect quantity: there is no
#: correction left to take past the note it was aiming for.
#:
#: ``(percent, what the amount does)`` -- the second field is what Tab reads on
#: that line, because "50%" alone does not say what half of anything sounds like.
#: The five rows are the menu's quick answers, not the only values: the Custom
#: Percentage row takes any whole number between 0 and 100 (`parse_amount`).
TUNE_AMOUNTS = (
    (0,
     "None of the correction: the effect runs and the voice goes out as it was "
     "sung. This is your own voice with Auto-Tune still switched on."),
    (25,
     "A hint of the note: enough to hear the voice lean toward it, not enough "
     "to sound tuned."),
    (50,
     "Halfway: close to the note with the human wobble still in it -- the "
     "half-tuned sound, half effect and half singer."),
    (75,
     "Nearly on the note: the note wins, with a little of the singer left in "
     "it."),
    (100,
     "All of the correction: the voice sits on the notes of the scale, exactly "
     "as Auto-Tune did before this dial existed."),
)
DEFAULT_AMOUNT = 100


def clamp_amount(value):
    """A tune amount in percent: any whole number 0..100, whatever was saved.

    It clamps and rounds rather than snapping to a step: the five rows in
    `TUNE_AMOUNTS` are the menu's quick answers, and a player who typed 63%
    means 63%.
    """
    try:
        number = int(round(float(value)))
    except (TypeError, ValueError):
        return DEFAULT_AMOUNT
    return max(0, min(100, number))


def parse_amount(text):
    """A percentage a player typed: 0..100, or None when it is not a number.

    Accepts what somebody actually types into a percentage field -- "63",
    "63%", " 63 " -- and nothing else. "half" and "63.5" are refused rather
    than guessed at: a dial is easier to trust when it does exactly what was
    asked of it, and a refusal says the value did not change.
    """
    if text is None:
        return None
    cleaned = str(text).strip().rstrip("%").strip()
    if not cleaned.isdigit():
        return None
    return clamp_amount(int(cleaned))


def amount_label(percent=None):
    """The menu's words for an amount: '100%'."""
    return f"{DEFAULT_AMOUNT if percent is None else clamp_amount(percent)}%"


def amount_note(percent=None):
    """What the menu says this amount does (what Tab reads).

    A value typed into the Custom Percentage field is described by what it is
    rather than by the nearest step: somebody who asked for 63% did not ask for
    50, so saying "halfway" would be the dial ignoring them.
    """
    wanted = DEFAULT_AMOUNT if percent is None else clamp_amount(percent)
    for step, note in TUNE_AMOUNTS:
        if step == wanted:
            return note
    return (f"{wanted}% of the correction: the voice is moved {wanted}% of the "
            f"distance to the nearest note of the scale.")
