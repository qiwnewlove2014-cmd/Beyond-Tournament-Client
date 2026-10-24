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

What is deliberately not here
-----------------------------
* **No tune amount dial.** How far is given by the note set: half a semitone
  at most on the chromatic grid, a whole one in a seven-note scale, a semitone
  and a half in a five-note one. "How fast" is the dial people hear, and that
  is Retune Speed.
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

    def reset(self):
        """Forget the press: the next frame is a fresh microphone."""
        self._window.clear()
        self.detected_hz = None
        self.offset = 0.0
        self.clarity = 0.0

    def set_scale(self, mode, root):
        """Which notes this press may be moved to (a scale, in a key)."""
        self.mode = clamp_mode(mode)
        self.root = clamp_root(root)
        return self.mode, self.root

    @staticmethod
    def grid_correction(hz):
        """Semitones from ``hz`` to the nearest note of every semitone.

        The chromatic answer: A4 = 440 Hz is the reference, and the result is
        always inside +/-0.5. `scale_correction` is the general one.
        """
        return AutoTuner.scale_correction(hz, "chromatic", 0)

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
            self.detected_hz = None
            return None
        correction = self.scale_correction(hz, self.mode, self.root)
        if correction is None:
            self.detected_hz = None
            return None
        self.detected_hz = hz
        self.offset = correction
        return correction


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
