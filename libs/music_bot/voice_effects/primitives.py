"""Filter and dynamics primitives for the broadcast voice effect.

`_Biquad` / `_Fir` / `_Compressor` / `_DeEsser` / `_ToneStack` and the
coefficient builders: the pieces a preset's chain is made from, with no
knowledge of which preset is selected. `dsp.py` imports and re-exports them so
existing call sites and tests keep their names.
"""

import numpy as np

from . import tone
from .presets import SAMPLE_RATE

# ------------------------------------------------------------------ the filters


def _lowpass(hz, taps):
    """A windowed-sinc low-pass, normalised to unity at DC."""
    offset = np.arange(taps) - (taps - 1) / 2.0
    cutoff = float(hz) / SAMPLE_RATE
    kernel = (2.0 * cutoff * np.sinc(2.0 * cutoff * offset)
              * np.hamming(taps))
    total = kernel.sum()
    if abs(total) < 1e-12:
        return np.zeros(taps, dtype=np.float32)
    return (kernel / total).astype(np.float32)


def _tick(taps):
    """A short one-sided click: what a record's scratch sounds like in a frame."""
    window = np.hanning(taps)
    total = float(window.sum())
    return (window / total if total else window).astype(np.float32)


def _hilbert(taps):
    """A windowed Hilbert kernel: the quadrature branch of a 90-degree shift.

    Odd length, antisymmetric, zero at the centre; a Hamming window keeps the
    stop band from ringing into the voice's top octave. The branch it produces
    lags by 90 degrees at every frequency the kernel covers, which is what makes
    a frequency shifter shift every partial by the same number of hertz instead
    of multiplying the spectrum the way a ring modulator does.
    """
    centre = (taps - 1) // 2
    index = np.arange(taps) - centre
    kernel = np.zeros(taps, dtype=np.float32)
    odd = (np.arange(taps) - centre) % 2 != 0
    kernel[odd] = 2.0 / (np.pi * index[odd])
    kernel *= np.hamming(taps)
    return kernel.astype(np.float32)


class _Biquad:
    """A second-order high-pass that carries its state across frames.

    Why not an FIR like the cabinet: at 95 Hz a 129-tap windowed-sinc filter has
    a transition band more than a kilohertz wide, so it passes 0.76 of a 220 Hz
    tone -- a 12 dB cut to the voice's own fundamental, heard as a thin
    telephone. A biquad is where the corner is named, and its delay (two or
    three samples) is small enough to ignore.
    """

    __slots__ = ("b", "a", "_z1", "_z2")

    def __init__(self, b, a):
        self.b = np.asarray(b, dtype=np.float64)
        self.a = np.asarray(a, dtype=np.float64)
        self._z1 = 0.0
        self._z2 = 0.0

    @staticmethod
    def _bandpass_coefficients(hz, q):
        """RBJ's band-pass, normalised so the peak gain is exactly 1."""
        w0 = 2.0 * np.pi * float(hz) / SAMPLE_RATE
        cos_w0 = float(np.cos(w0))
        alpha = float(np.sin(w0)) / (2.0 * float(q))
        a0 = 1.0 + alpha
        return ((alpha / a0, 0.0, -alpha / a0),
                (1.0, -2.0 * cos_w0 / a0, (1.0 - alpha) / a0))

    @classmethod
    def bandpass(cls, hz, q=1.0):
        b, a = cls._bandpass_coefficients(hz, q)
        return cls(b, a)

    def retune_bandpass(self, hz, q=1.0):
        """Move a running band-pass, keeping its state (a sweep, not a reset)."""
        self.b, self.a = self._bandpass_coefficients(hz, q)

    def retune(self, b, a):
        """A new response on the same filter, keeping the samples it holds.

        The tone stage moves a band a few frames at a time (`_ToneStack`), and
        a filter that reset its state on every one of those frames would drop
        the tail it is still ringing with -- a tick at each 20 ms boundary.
        """
        self.b = np.asarray(b, dtype=np.float64)
        self.a = np.asarray(a, dtype=np.float64)

    def reset(self):
        """Forget the samples this filter is holding (a band let go of)."""
        self._z1 = 0.0
        self._z2 = 0.0

    def state(self):
        """The samples this filter is holding, so they can be rewound.

        A response that changes between frames has to be auditioned twice on
        the same input (`_ToneStack`): once with the response that was playing
        and once with the new one, crossfaded across the frame. The second run
        starts from the first one's state, which is what makes the seam
        continuous -- the alternative is a step the size of whatever the old
        response was holding.
        """
        return self._z1, self._z2

    def restore(self, state):
        """Put back a state `state()` saved, before a second run."""
        self._z1, self._z2 = state

    @staticmethod
    def _lowpass_coefficients(hz, q=0.7071067811865476):
        """RBJ's cookbook low-pass, at a Butterworth's Q (the delay's darkening)."""
        w0 = 2.0 * np.pi * float(hz) / SAMPLE_RATE
        cos_w0 = float(np.cos(w0))
        alpha = float(np.sin(w0)) / (2.0 * q)
        b0 = (1.0 - cos_w0) / 2.0
        b1 = 1.0 - cos_w0
        b2 = (1.0 - cos_w0) / 2.0
        a0 = 1.0 + alpha
        a1 = -2.0 * cos_w0
        a2 = 1.0 - alpha
        return ((b0 / a0, b1 / a0, b2 / a0),
                (1.0, a1 / a0, a2 / a0))

    @classmethod
    def lowpass(cls, hz, q=0.7071067811865476):
        b, a = cls._lowpass_coefficients(hz, q)
        return cls(b, a)

    def retune_lowpass(self, hz, q=0.7071067811865476):
        """Move a running low-pass, keeping the samples it is holding.

        The same reason as the high-pass's (`retune_highpass`): a stop on the
        high cut's ladder is a new response, and a filter that reset its state
        on the press would drop the tail it is still ringing with.
        """
        self.b, self.a = self._lowpass_coefficients(hz, q)

    @staticmethod
    def _highpass_coefficients(hz, q=0.7071067811865476):
        """RBJ's cookbook high-pass, at a Butterworth's Q."""
        w0 = 2.0 * np.pi * float(hz) / SAMPLE_RATE
        cos_w0 = float(np.cos(w0))
        alpha = float(np.sin(w0)) / (2.0 * q)
        b0 = (1.0 + cos_w0) / 2.0
        b1 = -(1.0 + cos_w0)
        b2 = (1.0 + cos_w0) / 2.0
        a0 = 1.0 + alpha
        a1 = -2.0 * cos_w0
        a2 = 1.0 - alpha
        return ((b0 / a0, b1 / a0, b2 / a0),
                (1.0, a1 / a0, a2 / a0))

    @classmethod
    def highpass(cls, hz, q=0.7071067811865476):
        b, a = cls._highpass_coefficients(hz, q)
        return cls(b, a)

    def retune_highpass(self, hz, q=0.7071067811865476):
        """Move a running high-pass, keeping the samples it is holding.

        The cut ladder changes on a key press, and a filter that reset its
        state on that press would drop the tail it is still ringing with -- a
        tick at the frame boundary. `_ToneStack` crossfades the frame as well;
        this keeps the state the new response starts from.
        """
        self.b, self.a = self._highpass_coefficients(hz, q)

    def process(self, block):
        b0, b1, b2 = (float(value) for value in self.b)
        _unity, a1, a2 = (float(value) for value in self.a)
        z1, z2 = self._z1, self._z2
        # A list of Python floats, not the numpy array: a per-sample loop over
        # numpy scalars is several times slower than one over floats, and this
        # loop runs 50 times a second on the talker's capture thread.
        samples = block.tolist()
        out = [0.0] * len(samples)
        for index, x in enumerate(samples):
            y = b0 * x + z1
            z1 = b1 * x - a1 * y + z2
            z2 = b2 * x - a2 * y
            out[index] = y
        self._z1, self._z2 = z1, z2
        return np.asarray(out, dtype=np.float32)


#: A filter that changes nothing, for a tone band that is back at flat.
_IDENTITY = (1.0, 0.0, 0.0)


def _shelf_coefficients(hz, decibels, high=False):
    """RBJ's shelf, at S = 1: it reaches its whole gain by the corner's octave.

    A low shelf is what "less bottom" means and a high shelf is what "less
    top" means: neither leaves a hole in the middle of the voice the way a
    bell at 120 Hz would.
    """
    amplitude = 10.0 ** (float(decibels) / 40.0)
    w0 = 2.0 * np.pi * float(hz) / SAMPLE_RATE
    cos_w0 = float(np.cos(w0))
    alpha = float(np.sin(w0)) / 2.0 * np.sqrt(2.0)
    root_alpha = 2.0 * np.sqrt(amplitude) * alpha
    if high:
        b0 = amplitude * ((amplitude + 1.0) + (amplitude - 1.0) * cos_w0
                          + root_alpha)
        b1 = -2.0 * amplitude * ((amplitude - 1.0)
                                 + (amplitude + 1.0) * cos_w0)
        b2 = amplitude * ((amplitude + 1.0) + (amplitude - 1.0) * cos_w0
                          - root_alpha)
        a0 = ((amplitude + 1.0) - (amplitude - 1.0) * cos_w0 + root_alpha)
        a1 = 2.0 * ((amplitude - 1.0) - (amplitude + 1.0) * cos_w0)
        a2 = ((amplitude + 1.0) - (amplitude - 1.0) * cos_w0 - root_alpha)
    else:
        b0 = amplitude * ((amplitude + 1.0) - (amplitude - 1.0) * cos_w0
                          + root_alpha)
        b1 = 2.0 * amplitude * ((amplitude - 1.0)
                                - (amplitude + 1.0) * cos_w0)
        b2 = amplitude * ((amplitude + 1.0) - (amplitude - 1.0) * cos_w0
                          - root_alpha)
        a0 = ((amplitude + 1.0) + (amplitude - 1.0) * cos_w0 + root_alpha)
        a1 = -2.0 * ((amplitude - 1.0) + (amplitude + 1.0) * cos_w0)
        a2 = ((amplitude + 1.0) + (amplitude - 1.0) * cos_w0 - root_alpha)
    return ((b0 / a0, b1 / a0, b2 / a0), (1.0, a1 / a0, a2 / a0))


def _bell_coefficients(hz, q, decibels):
    """RBJ's peaking band: only the band around its centre moves.

    This is the one that takes the mud out from under the words without
    thinning the chest -- a shelf cannot do it from either end.
    """
    amplitude = 10.0 ** (float(decibels) / 40.0)
    w0 = 2.0 * np.pi * float(hz) / SAMPLE_RATE
    cos_w0 = float(np.cos(w0))
    alpha = float(np.sin(w0)) / (2.0 * float(q))
    a0 = 1.0 + alpha / amplitude
    return ((1.0 + alpha * amplitude) / a0,
            -2.0 * cos_w0 / a0,
            (1.0 - alpha * amplitude) / a0), (
        1.0, -2.0 * cos_w0 / a0, (1.0 - alpha / amplitude) / a0)


def _tone_coefficients(index, decibels):
    """The coefficients for one tone band, from the table in `tone.BANDS`."""
    _band_id, _label, hz, kind, q, _note = tone.BANDS[index]
    if kind == "bell":
        return _bell_coefficients(hz, q, decibels)
    return _shelf_coefficients(hz, decibels, high=(kind == "highshelf"))


class _Fir:
    """One FIR filter that carries the samples it needs across frames."""

    __slots__ = ("kernel", "tail")

    def __init__(self, kernel):
        self.kernel = kernel
        self.tail = np.zeros(len(kernel) - 1, dtype=np.float32)

    def process(self, block):
        data = np.concatenate((self.tail, block))
        out = np.convolve(data, self.kernel, mode="valid")
        self.tail = data[-(len(self.kernel) - 1):].copy()
        return out.astype(np.float32, copy=False)


class _Compressor:
    """The voice strip's level rider: the quiet and the loud arrive together.

    **Why a compressor, when the chain already ends in a ceiling and a trim.**
    The peak ceiling catches a shout after it has happened, and the loudness
    trim pulls a dense effect back to the average a voice arrives at -- both
    are safety, and neither can make a whisper and a shout *the same size*.
    That is the one job of a compressor, and it is the job a listener hears as
    a professional voice: the sentence stays at the same distance from the
    room whether it is spoken across the desk or into the microphone.

    **Feed-forward, with the detector high-passed at 120 Hz.** The gain is
    computed from the level of the frame arriving, so it cannot overshoot into
    feedback the way a feedback design can, and it needs no look-ahead. The
    level is read through its own high-pass, though: a `p` or a knock on the
    desk is a large low-frequency spike that carries no words, and a full-band
    detector would duck the whole sentence under every one of them. That is
    the voice compressor's most common fault -- the words pumping whenever the
    speaker moves.

    **A soft knee of 6 dB.** A hard knee switches on at one decibel and off at
    the one below it, which on a voice is heard as a lisp around the
    threshold; the curve is bent across six decibels instead, so a level that
    crosses it is leaned on rather than caught.

    **Attack and release are the detector's.** The gain is the static curve of
    a level that has already been through them, which is what makes a release
    time sound like a breath rather than like a delay. A threshold or a ratio
    moved on the page is ramped across the frame -- the same rule as the level
    fader, because a gain that steps is a click.

    **There is no make-up gain of its own.** A compressor that lifts itself
    back up is guessing at how much of the signal was above its threshold; the
    Level row sits immediately after this block, and the meter reports the
    reduction so a player can add exactly that. It is also what makes the
    block usable as a bus compressor later: it does what it is told and leaves
    the level where it was put.
    """

    #: The width of the knee, in decibels: three either side of the threshold.
    KNEE_DB = 6.0

    #: Where the detector's high-pass sits: under the fundamental of a deep
    #: voice, over the thump of a desk and the plosive of a `p`.
    DETECTOR_HZ = 120.0

    #: The threshold that is "no threshold": no frame can cross it, so the
    #: static curve is flat while the release finishes what it was holding.
    OFF = 200.0

    def __init__(self):
        self._sidechain = _Biquad.highpass(self.DETECTOR_HZ)
        self._env = 0.0
        self._attack_ms = None
        self._release_ms = None
        self._attack = 0.0
        self._release = 0.0
        self._threshold = self.OFF
        self._ratio = 1.0
        self._reduction = 0.0

    def reduction(self):
        """How far the last frame was pulled down, in decibels."""
        return self._reduction

    def rearm(self):
        """Start a fresh engagement with nothing left from the last one.

        The stage is skipped while the threshold is off, so the detector's
        filter state and its envelope would otherwise sit where the previous
        engagement left them. Switching the compressor back on ten seconds
        later would then begin with a second of gain reduction that belongs to
        audio nobody is playing -- measured as the quiet sentence after a
        re-engage being pulled down by the press that had already ended.
        """
        self._sidechain.reset()
        self._env = 0.0

    def active(self):
        """Whether the compressor is holding the voice down at all.

        The stage is skipped entirely while this is false and the saved
        threshold is off, which is what keeps a preset nobody has compressed
        byte-for-byte the pedal it was.
        """
        return self._reduction > 0.05

    def _retune(self, attack_ms, release_ms):
        """The detector's two coefficients, when the page has moved them."""
        if (attack_ms, release_ms) == (self._attack_ms, self._release_ms):
            return
        self._attack_ms = attack_ms
        self._release_ms = release_ms
        self._attack = float(np.exp(-1.0 / (max(1.0, attack_ms) * 0.001
                                           * SAMPLE_RATE)))
        self._release = float(np.exp(-1.0 / (max(1.0, release_ms) * 0.001
                                             * SAMPLE_RATE)))

    @staticmethod
    def _ramp(before, after, count):
        """A parameter across the frame, so a press is not a step."""
        if abs(after - before) < 1e-6:
            return np.full(count, after, dtype=np.float64)
        return np.linspace(before, after, count, dtype=np.float64)

    def process(self, block, threshold_db, ratio, attack_ms, release_ms):
        """Pull one frame down by the static curve of its own level."""
        count = block.size
        if count <= 0:
            return block
        self._retune(attack_ms, release_ms)
        # `threshold_db` is 0 for off; the internal "off" is a threshold no
        # frame can cross, so the release still finishes what it was holding
        # instead of the gain snapping back to one in a single sample.
        target = float(threshold_db) if threshold_db else self.OFF
        thresholds = self._ramp(self._threshold, target, count)
        self._threshold = target
        slopes = 1.0 - 1.0 / self._ramp(self._ratio, float(ratio), count)
        self._ratio = float(ratio)
        detector = self._sidechain.process(block).tolist()
        env = self._env
        attack = self._attack
        release = self._release
        levels = [0.0] * count
        for index, sample in enumerate(detector):
            square = sample * sample
            coefficient = attack if square > env else release
            env = square + coefficient * (env - square)
            levels[index] = env
        self._env = env
        # The mean square is a power, so 10*log10 of it is the same decibel a
        # 20*log10 of an amplitude would be -- without a square root per sample.
        over = (10.0 * np.log10(np.maximum(np.asarray(levels, dtype=np.float64),
                                           1e-12)) - thresholds)
        half = self.KNEE_DB / 2.0
        reduction = np.zeros(count, dtype=np.float64)
        above = over >= half
        knee = (over > -half) & (over < half)
        reduction[above] = over[above] * slopes[above]
        reduction[knee] = (slopes[knee] * (over[knee] + half) ** 2
                           / (2.0 * self.KNEE_DB))
        self._reduction = float(reduction.max())
        if self._reduction <= 0.0:
            return block
        gain = np.power(10.0, -reduction / 20.0).astype(np.float32)
        return block * gain


class _DeEsser:
    """The strip's sibilance rider: it takes the `s` while it is happening.

    **Why a rider and not a filter.** A high shelf turned down takes the breath
    off every syllable, and a high cut takes the top off the voice for good. A
    `s` is neither: it is an event, loud for eighty milliseconds, and the tool
    that leaves the rest of the sentence alone is a gain that moves with it.
    The gain is read from the frame's own top -- a high-pass at 4.8 kHz, which
    a vowel barely crosses and a sibilant fills -- and applied to the frame.

    **Why the whole frame, and not a split band.** A band de-esser is the
    textbook shape and it was built first: split at 4.8 kHz, pull the top half
    down, add the bottom half back. Measured, it could take only 2.4 decibels
    off an 8 kHz tone set to `12` -- the 12 dB/octave split leaves a third of
    the `s` in the low half, out of the gain's reach -- and cascading the split
    made it worse, because the two halves are not in phase: an 8 kHz tone came
    out *louder* with the row at 12 than with it off. A full-band duck does
    exactly what the row says (-12.0 dB measured at `12`) and there is no
    vowel under a `s` for it to darken. The row is a duck, not an EQ.

    **The threshold is on the band, not on the frame.** A loud vowel carries
    more energy than a quiet `s`, so a detector on the whole frame would duck
    the `s` in a whisper and miss it in a shout. The level read is the top
    band's own RMS: a vowel carries almost nothing up there, a sibilant carries
    most of its energy there, and the one setting works at every volume the
    strip is handed.

    **One knob, because a second is a second mistake.** The threshold, attack
    and release are properties of speech and are fixed; the row is the most the
    frame may lose. That is the decision a player makes when they hear a spit
    on the `s`.

    **Off is no filter at all.** The first stop is 0 dB and the stage is
    skipped entirely there, so a preset nobody has de-essed costs nothing and
    is byte-for-byte the pedal it was.
    """

    #: Where the band it watches begins: over the words' own clarity and under
    #: where the `s`, the `sh` and a cymbal sit.
    SPLIT_HZ = 4800.0

    #: The band's own RMS the reduction starts from, as an amplitude. A vowel
    #: carries a thousandth of its energy here; a `s` carries a tenth of a
    #: frame, and this is the level between them.
    THRESHOLD = 0.02

    #: The knee, in decibels: the same reason the compressor has one at all.
    KNEE_DB = 6.0

    #: The envelope's times. A `s` is over in tens of milliseconds, so the
    #: attack has to catch it, and the release must not chop the next word.
    ATTACK_MS = 0.5
    RELEASE_MS = 70.0

    def __init__(self):
        self._split = _Biquad.highpass(self.SPLIT_HZ)
        self._env = 0.0
        self._attack = float(np.exp(-1.0 / (self.ATTACK_MS * 0.001
                                            * SAMPLE_RATE)))
        self._release = float(np.exp(-1.0 / (self.RELEASE_MS * 0.001
                                             * SAMPLE_RATE)))
        self._reduction = 0.0
        self._amount = 0.0

    def reduction(self):
        """How far the last frame was pulled down, in decibels."""
        return self._reduction

    def active(self):
        """Whether the stage is doing anything -- the row on, or letting go."""
        return self._amount > 0.0 or self._reduction > 0.0

    def rearm(self):
        """Start a fresh engagement with nothing held from the last one."""
        self._split.reset()
        self._env = 0.0
        self._reduction = 0.0
        self._amount = 0.0

    def process(self, block, amount_db):
        """Duck the frame for the length of a sibilant, by at most the row.

        The row is ramped across the frame rather than read at its new value,
        exactly like the compressor's threshold and for the same reason: a
        player who turns the row down while a `s` is still sounding would
        otherwise be handed a gain that steps from a quarter to one in a
        single sample, which is a click. Turning it up ramps the other way, so
        the first `s` after the press is ducked in rather than dropped on.
        """
        count = block.size
        if count <= 0:
            return block
        before = self._amount
        self._amount = float(amount_db)
        ceiling = np.linspace(before, self._amount, count, dtype=np.float64)
        high = self._split.process(block)
        attack = self._attack
        release = self._release
        env = self._env
        levels = [0.0] * count
        for index, sample in enumerate(high.tolist()):
            square = sample * sample
            coefficient = attack if square > env else release
            env = square + coefficient * (env - square)
            levels[index] = env
        self._env = env
        # The mean square is a power, so 10*log10 of it is the amplitude in
        # decibels without a square root per sample -- the same arithmetic the
        # compressor's own detector uses.
        over = (10.0 * np.log10(np.maximum(
            np.asarray(levels, dtype=np.float64), 1e-12))
            - 10.0 * np.log10(self.THRESHOLD * self.THRESHOLD))
        half = self.KNEE_DB / 2.0
        reduction = np.zeros(count, dtype=np.float64)
        above = over >= half
        knee = (over > -half) & (over < half)
        # An infinite ratio -- the band is brought back to the threshold -- and
        # the row is the ceiling on how far that may go. A `s` that is 20 dB
        # over the threshold at `De-ess 6` loses 6, not 20.
        reduction[above] = np.minimum(over[above], ceiling[above])
        reduction[knee] = np.minimum(
            (over[knee] + half) ** 2 / (2.0 * self.KNEE_DB), ceiling[knee])
        self._reduction = float(reduction.max())
        if self._reduction <= 0.0:
            return block
        gain = np.power(10.0, -reduction / 20.0).astype(np.float32)
        return block * gain


class _ToneStack:
    """The cuts, bands, de-esser, compressor and fader for one preset.

    `tone.py` owns the table, the saved setting and the words; this is only the
    filters and the gain. **Flat is not a filter**: a preset whose bands are
    all zero, whose cuts are off, whose de-esser is off, whose compressor is
    off and whose level is 0 dB is handed straight back, so the hundred and
    forty-four presets that nobody has touched cost exactly what they cost
    before this class existed.

    A band is GLIDED to its saved value rather than jumped to, because a filter
    whose coefficients change between one sample and the next is a step the
    size of whatever the old response was holding -- heard as a click. 3 dB a
    frame puts a slider press on the next ~80 ms, and the band's state (the
    samples the biquad is still ringing with) is kept across every one of
    those frames, which is what makes the glide continuous.
    """

    #: How far a band may move in one frame, in decibels.
    GLIDE_DB = 3.0

    def __init__(self, preset):
        self.preset = preset
        self._filters = [_Biquad(_IDENTITY, _IDENTITY)
                         for _band in tone.BANDS]
        self._applied = [0.0] * len(tone.BANDS)
        # The low cut and the preset's own fader, moved by the same page as
        # the bands. Both start at what a preset opens with: no cut, 0 dB.
        self._cut = _Biquad(_IDENTITY, _IDENTITY)
        self._cut_hz = 0.0
        # The other end of the strip, and the band rider between them. Both
        # start off, which is no filter and no per-sample loop at all.
        self._highcut = _Biquad(_IDENTITY, _IDENTITY)
        self._highcut_hz = 0.0
        self._deess = _DeEsser()
        self._deess_on = False
        # The compressor, between the bands and the fader. Off is a threshold
        # of 0 and no frame of DSP: `_comp_on` holds it for the frames a
        # release still needs after the player turns it off.
        self._comp = _Compressor()
        self._comp_on = False
        self._comp_live = False
        self._level_gain = 1.0
        self._level_db = 0.0
        self._engaged = False

    def active(self):
        """Whether the strip is doing anything -- the loudness trim asks this.

        A fixed preset is normally left alone by `_loudness_trim` because it
        was tuned against a PA by ear. A preset whose tone has been moved is
        no longer that measurement, so from the first decibel it is levelled
        like a board; flat returns it to exactly as it was made.

        **The latch does not clear at 0 dB on the way past.** A band gliding
        from +12 to -12 crosses 0, and a trim that read the applied gain alone
        would release for that one frame -- which measured as a 2x burst in the
        middle of a 20 ms glide, heard as a thump under the words. It clears
        only when the saved value is flat too.
        """
        return self._engaged

    def compression(self):
        """How far the last frame was pulled down, in decibels (0 when off)."""
        return self._comp.reduction()

    def process(self, block):
        (targets, cut_hz, high_cut_hz, deess_db, _mix, compressor,
         level_db) = tone.strip(self.preset)
        threshold, ratio, attack, release = compressor
        idle = (not any(targets) and not cut_hz and not high_cut_hz
                and not deess_db and not level_db and not threshold)
        if idle and not self._engaged:
            return block
        if cut_hz != self._cut_hz:
            block = self._retune_cut(cut_hz, block)
            self._engaged = True
        if self._cut_hz:
            block = self._cut.process(block)
        for index, target in enumerate(targets):
            applied = self._applied[index]
            if applied != target:
                step = max(-self.GLIDE_DB,
                           min(self.GLIDE_DB, float(target) - applied))
                applied += step
                self._applied[index] = applied
                block = self._change(index, applied, block)
                self._engaged = True
            elif applied:
                block = self._filters[index].process(block)
        # The two ends and the sibilance, in the order they hear the voice: the
        # de-esser has to see the band a `s` lives in before the high cut takes
        # the top off, and both keep their state across the frame they change
        # on (a filter whose state was built for another response ticks).
        if deess_db or self._deess.active():
            if not self._deess_on:
                self._deess.rearm()
            block = self._deess.process(block, deess_db)
            self._deess_on = self._deess.active()
            self._engaged = True
        else:
            self._deess_on = False
        if high_cut_hz != self._highcut_hz:
            block = self._retune_highcut(high_cut_hz, block)
            self._engaged = True
        if self._highcut_hz:
            block = self._highcut.process(block)
        # The compressor sits between the bands and the fader, and it is the
        # only block here whose own state (a release still letting go) keeps
        # it running after its threshold is put back to off. Turning it back
        # on starts from a clean detector rather than from the press before.
        if threshold and not self._comp_live:
            self._comp.rearm()
        self._comp_live = bool(threshold)
        if threshold or self._comp_on:
            block = self._comp.process(block, threshold, ratio, attack,
                                       release)
            self._comp_on = self._comp.active()
            if self._comp_on:
                self._engaged = True
        block = self._level_block(level_db, block)
        if (idle and not any(self._applied) and not self._cut_hz
                and not self._highcut_hz and not self._level_db
                and not self._comp_on):
            self._engaged = False
        return block

    def _retune_cut(self, hz, block):
        """Move the low cut, crossfading the frame it moves on."""
        self._cut_hz = hz
        return self._retune_edge(self._cut, hz, block,
                                 self._cut.retune_highpass)

    def _retune_highcut(self, hz, block):
        """Move the high cut, the same way round as the low one."""
        self._highcut_hz = hz
        return self._retune_edge(self._highcut, hz, block,
                                 self._highcut.retune_lowpass)

    def _retune_edge(self, edge, hz, block, retune):
        """Run the frame through the old corner and the new one, and blend.

        The same reason as a band's change: a biquad's state belongs to the
        response that filled it, so the first samples after a new corner are a
        step the old one was about to make. The frame is run twice from the
        same state and the two are blended across the 20 ms -- a ladder press
        is a change of corner, not a glide along one.

        Off is a response like any other, the identity: both halves of it.
        `retune` takes the b triple and the a triple; the one value that used to
        be splatted on the low cut raised instead of easing the bottom back in
        (a live press, 2026-10-29).
        """
        previous = edge.state()
        before = edge.process(block)
        edge.restore(previous)
        if hz:
            retune(hz)
        else:
            edge.retune(_IDENTITY, _IDENTITY)
        after = edge.process(block)
        blend = np.linspace(0.0, 1.0, block.size, endpoint=False,
                            dtype=np.float32)
        return before * (1.0 - blend) + after * blend

    def _level_block(self, decibels, block):
        """The preset's own fader, ramped across the frame it changes on.

        A fader is a gain, not a filter, so there is no response to crossfade;
        a linear ramp from the gain the last frame ended on is enough to keep
        a press from being a step. At 0 dB the frame is handed back without a
        multiply at all, which is what keeps an untouched preset byte-exact.
        """
        target = 10.0 ** (float(decibels) / 20.0)
        if abs(target - self._level_gain) < 1e-6:
            if abs(self._level_gain - 1.0) < 1e-9:
                return block
            return block * self._level_gain
        ramp = np.linspace(self._level_gain, target, block.size,
                           endpoint=False, dtype=np.float32)
        self._level_gain = target
        self._level_db = float(decibels)
        return block * ramp

    def _change(self, index, applied, block):
        """Run the same frame through the old response and the new one, blended.

        Changing a biquad's coefficients under a running stream is only half a
        fix: the filter's state was computed for the response that was playing, so
        the first samples after a change are a step the old response was about
        to make. Measured on the 0 dB crossing of a +12 to -12 glide, that step
        was 0.067 of full scale -- a click under the words, from the one frame
        the stage spent at "no change".

        So the frame is shaped twice: once with the filter as it was (its state
        carried on), then again from the same starting state with the new
        response, and the two are crossfaded across the 20 ms. The second run's
        end state is the one kept, so the next frame starts where this one
        ended. It costs one extra filter run only on frames where a band moves.
        """
        band_filter = self._filters[index]
        state = band_filter.state()
        before = band_filter.process(block)
        band_filter.restore(state)
        band_filter.retune(*_tone_coefficients(index, applied))
        after = band_filter.process(block)
        blend = np.linspace(0.0, 1.0, block.size, endpoint=False,
                            dtype=np.float32)
        return before * (1.0 - blend) + after * blend
