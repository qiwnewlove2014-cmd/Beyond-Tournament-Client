"""Channel analysis and Mid/Side mixing for the Cinema Speaker System.

Mono detection has to look at the samples, not the container. The server
relay always encodes Opus stereo and the direct ffmpeg path always runs
``-ac 2``, so a mono master arrives as two bit-identical channels and any
container-level check would report "stereo" forever. A running
``|L - R| / (|L| + |R|)`` ratio answers the real question: does this stream
actually carry a stereo image worth preserving?

Nothing here imports OpenAL, audioop or the Jukebox: this is pure sample
maths so it can be unit tested offline and reused by any future source.
"""

import array
from collections import deque

AUTO = "auto"
MONO = "mono"
STEREO = "stereo"

_SOURCE_LAYOUTS = (AUTO, MONO, STEREO)

# Fixed point at 15 bits: gain 1.0 becomes 32768, and ``sample * 32768 >> 15``
# returns the original sample exactly, so the unity/zero weights used by the
# front-only profile reproduce the source bytes bit for bit.
_GAIN_SCALE = 1 << 15

_INT16_MIN = -32768
_INT16_MAX = 32767


def source_layout(value):
    """Normalise a layout token; anything unknown means 'decide from audio'.

    The server can declare the layout of a stream it already knows (a
    mono-only radio rip, for example). An explicit declaration always wins
    over the detector, which only ever sees a stereo-downmixed copy.
    """
    text = str(value or AUTO).strip().lower()
    return text if text in _SOURCE_LAYOUTS else AUTO


def to_samples(data):
    """int16 samples from raw s16le bytes, dropping a torn odd tail byte."""
    samples = array.array("h")
    usable = len(data) - (len(data) & 1)
    if usable:
        samples.frombytes(memoryview(data)[:usable])
    return samples


# Internal alias kept short for the hot per-frame path.
_samples = to_samples


def mid_side(left, right):
    """Split stereo into (mid, side) so the image can be re-spread intact.

    ``M = (L + R) / 2`` and ``S = (L - R) / 2``, and since ``L = M + S`` and
    ``R = M - S`` the original channels are recoverable, which is why using
    M for the centre and S for the sides preserves the stereo image instead
    of destroying it. The shift is safe: summing or differencing two int16
    samples then halving always lands back inside int16.

    Integer mid/side cannot be perfectly reversible: when L + R is odd the
    half is not representable, so a reconstruction can sit one LSB off on
    one channel (-90 dBFS, below the 16-bit noise floor). The front pair
    never goes through mid/side -- its weights are exactly (1, 0) and
    (0, 1) -- so the audible image is not affected by this at all.
    """
    left_samples = to_samples(left)
    right_samples = to_samples(right)
    count = min(len(left_samples), len(right_samples))
    mid = array.array("h", bytes(2 * count))
    side = array.array("h", bytes(2 * count))
    for index in range(count):
        l_value = left_samples[index]
        r_value = right_samples[index]
        mid[index] = (l_value + r_value) >> 1
        side[index] = (l_value - r_value) >> 1
    return mid.tobytes(), side.tobytes()


def mix_samples(left_samples, right_samples, gain_l=1.0, gain_r=0.0):
    """Blend two already-decoded channels into one MONO feed (bytes out).

    Split out from :func:`mix_channels` so the renderer can decode a frame
    once and then mix it for every speaker, instead of re-decoding the same
    frame on each slot. ``(1, 0)`` is exactly the left channel, ``(0, 1)``
    exactly the right, ``(0.5, 0.5)`` the mid and ``(0.5, -0.5)`` the side,
    which is the whole vocabulary the profiles need. Samples that would
    overflow are clamped, because a clipped speaker sounds like a broken
    speaker while a clamped one only ever sounds loud.
    """
    count = min(len(left_samples), len(right_samples))
    scaled_l = int(gain_l * _GAIN_SCALE)
    scaled_r = int(gain_r * _GAIN_SCALE)
    mixed = array.array("h", bytes(2 * count))
    for index in range(count):
        value = (left_samples[index] * scaled_l
                 + right_samples[index] * scaled_r) >> 15
        if value > _INT16_MAX:
            value = _INT16_MAX
        elif value < _INT16_MIN:
            value = _INT16_MIN
        mixed[index] = value
    return mixed.tobytes()


def mix_channels(left, right, gain_l=1.0, gain_r=0.0):
    """Bytes-in convenience wrapper around :func:`mix_samples`."""
    return mix_samples(to_samples(left), to_samples(right), gain_l, gain_r)


class ChannelAnalyzer:
    """Decide whether a playing stream is really mono or really stereo.

    Evidence accumulates per frame and the verdict only flips once a window
    is full, so a momentary centred hit (a snare landing dead centre in an
    otherwise wide mix) or a momentary wide reverb tail can never thrash the
    layout mid-song. Digital silence carries no evidence at all: it would
    otherwise vote "mono" for every quiet passage. The default verdict is
    stereo, because wrongly collapsing a stereo image is audible and
    irreversible while wrongly spreading a mono programme only widens it.
    """

    MONO_RATIO = 0.02
    WINDOW = 10
    MONO_VOTES = 0.8
    SILENCE_FLOOR = 64.0

    def __init__(self, window=None, mono_ratio=None, silence_floor=None):
        self.window = max(2, int(window or self.WINDOW))
        self.mono_ratio = float(self.MONO_RATIO if mono_ratio is None else mono_ratio)
        self.silence_floor = float(self.SILENCE_FLOOR if silence_floor is None else silence_floor)
        self._votes = deque(maxlen=self.window)
        self.layout = STEREO

    def reset(self):
        """Forget the running evidence; used when a jukebox changes song."""
        self._votes.clear()
        self.layout = STEREO

    def ratio(self, left, right):
        """``|L - R|`` relative to the total energy of a frame (0.0 = mono).

        Returns None for a frame too quiet to mean anything.
        """
        left_samples = _samples(left)
        right_samples = _samples(right)
        count = min(len(left_samples), len(right_samples))
        if not count:
            return None
        difference = 0
        energy = 0
        for index in range(count):
            l_value = left_samples[index]
            r_value = right_samples[index]
            difference += abs(l_value - r_value)
            energy += abs(l_value) + abs(r_value)
        if energy / count < self.silence_floor:
            return None
        return difference / energy

    def observe(self, left, right):
        """Feed one PCM frame and return the current layout verdict."""
        ratio = self.ratio(left, right)
        if ratio is not None:
            self._votes.append(ratio < self.mono_ratio)
            if len(self._votes) == self.window:
                mono_votes = sum(1 for mono in self._votes if mono)
                self.layout = MONO if mono_votes >= self.MONO_VOTES * self.window else STEREO
        return self.layout

    @property
    def confident(self):
        """True once a full window of evidence backs the current verdict."""
        return len(self._votes) == self.window
