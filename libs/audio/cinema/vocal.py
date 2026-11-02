"""Vocal focus: a cheap centre-layer pan for a stereo programme.

This is deliberately **not** source separation.  It removes the mid channel in
a vocal band from one side of the stereo image and leaves the other side
untouched, so a voice mixed down the centre is heard on the chosen side.  A
centre-panned snare, guitar or bass above the crossover travels with it; that
is the price of a DSP-only layer, and the menu calls the setting *Vocal focus*
rather than *Isolate vocal* for the same reason.

The room core (``channel``/``router`` and the other portable modules) imports
the standard library only.  This module is engine-side like ``fastmix``: it
takes numpy when the host has it and returns the original bytes otherwise.  A
mode of ``in_song`` is a true bypass, so a cabinet that never chose the
feature is byte-identical to the plain jukebox.

The filter is a pair of RBJ biquads (a high pass and a low pass) run on the
mid signal.  ``left`` keeps the original left channel and subtracts the
centre from the right; ``right`` is the mirror image.  The target side is
never altered, so the vocal keeps its original level and only the other side
loses the centre.
"""

import math

try:                                      # numpy is optional, never fatal
    import numpy as _np
except Exception:                         # pragma: no cover - no-numpy machine
    _np = None


IN_SONG = "in_song"
LEFT = "left"
RIGHT = "right"

MODES = (IN_SONG, LEFT, RIGHT)

LABELS = {
    IN_SONG: "In the song",
    LEFT: "Left of the room",
    RIGHT: "Right of the room",
}

# Keep the bass and kick in both channels: a vocal high pass above this leaves
# the low end as the room heard it.  The top edge removes cymbals and sibilance
# from the extracted centre without touching the target channel.
LOW_CUT_HZ = 140.0
HIGH_CUT_HZ = 8000.0
MONO_RATIO = 0.01
_SILENCE_FLOOR = 1.0


def normalize(value):
    """A stored vocal mode, or the bypass for anything unrecognised."""
    text = str(value or "").strip().lower()
    return text if text in MODES else IN_SONG


def available():
    """Whether this machine has the maths module the effect needs."""
    return _np is not None


def label(value):
    return LABELS[normalize(value)]


class VocalFocus:
    """Stateful centre extractor for one room.

    The filter state belongs to the room, not to a frame: a biquad fed one
    20 ms frame at a time must carry its two delay taps into the next frame or
    every frame boundary becomes a click.  One instance lives on the bank.
    """

    def __init__(self, mode=IN_SONG, sample_rate=48000):
        self.sample_rate = int(sample_rate)
        self.mode = normalize(mode)
        self._high = _biquad(self.sample_rate, LOW_CUT_HZ, high=True)
        self._low = _biquad(self.sample_rate, HIGH_CUT_HZ, high=False)
        self._high_state = (0.0, 0.0)
        self._low_state = (0.0, 0.0)

    def set_mode(self, mode):
        """Change the setting; a new mode starts with clean filter state."""
        value = normalize(mode)
        if value != self.mode:
            self.mode = value
            self._high_state = (0.0, 0.0)
            self._low_state = (0.0, 0.0)
        return self.mode

    def reset(self):
        self._high_state = (0.0, 0.0)
        self._low_state = (0.0, 0.0)

    def apply(self, left, right):
        """Return ``(left, right)`` bytes, processed only when a mode is on."""
        if self.mode == IN_SONG or _np is None or not left or not right:
            return left, right
        if len(left) != len(right) or len(left) & 1:
            return left, right
        count = len(left) // 2
        if count <= 0:
            return left, right
        left_samples = _np.frombuffer(left, dtype=_np.int16).astype(_np.float64)
        right_samples = _np.frombuffer(right, dtype=_np.int16).astype(_np.float64)
        # A mono master has no centre to move.  Detect it from the samples, not
        # from the container: the relay always encodes stereo.
        difference = _np.mean(_np.abs(left_samples - right_samples))
        level = _np.mean(_np.abs(left_samples) + _np.abs(right_samples))
        if level <= _SILENCE_FLOOR or difference <= max(1.0, level * MONO_RATIO):
            return left, right
        mid = (left_samples + right_samples) * 0.5
        high, self._high_state = _filter(mid, self._high, self._high_state)
        centre, self._low_state = _filter(high, self._low, self._low_state)
        if self.mode == LEFT:
            out_left = left_samples
            out_right = right_samples - centre
        else:
            out_left = left_samples - centre
            out_right = right_samples
        return _to_pcm(out_left), _to_pcm(out_right)


def _biquad(sample_rate, frequency, *, high):
    """RBJ biquad coefficients, normalised by ``a0``."""
    frequency = max(1.0, min(float(frequency), sample_rate * 0.45))
    omega = 2.0 * math.pi * frequency / sample_rate
    cosine = math.cos(omega)
    alpha = math.sin(omega) / (2.0 * 0.7071067811865476)
    if high:
        b0 = (1.0 + cosine) * 0.5
        b1 = -(1.0 + cosine)
        b2 = b0
    else:
        b0 = (1.0 - cosine) * 0.5
        b1 = 1.0 - cosine
        b2 = b0
    a0 = 1.0 + alpha
    a1 = -2.0 * cosine
    a2 = 1.0 - alpha
    return (b0 / a0, b1 / a0, b2 / a0, a1 / a0, a2 / a0)


def _filter(samples, coefficients, state):
    """One biquad, direct form II transposed, carrying state across frames."""
    b0, b1, b2, a1, a2 = coefficients
    z1, z2 = state
    out = _np.empty(len(samples), dtype=_np.float64)
    for index, value in enumerate(samples.tolist()):
        y = b0 * value + z1
        z1 = b1 * value - a1 * y + z2
        z2 = b2 * value - a2 * y
        out[index] = y
    return out, (z1, z2)


def _to_pcm(samples):
    return _np.clip(samples, -32768.0, 32767.0).astype(_np.int16).tobytes()
