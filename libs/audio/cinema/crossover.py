"""A cinema speaker's crossover: the room's own filter, played by the room.

A cinema speaker a builder marks with a crossover is fed the room's programme
*on one side* of that frequency and none of the other: the bottom of it for a
bass cabinet, the top of it for a tweeter. Not quieter on the other side --
gone. That is what a subwoofer and a tweeter are, and it is the one thing
OpenAL cannot be asked for:

* every filter gain in EFX is capped at 1.0 (``AL_LOWPASS_MAX_GAIN`` and
  friends in ``AL/efx.h``), so a filter can only cut ``GAIN``, the low-pass's
  own shelf is anchored at 5 kHz (``LowPassFreqRef``), and its low-pass shape
  is "everything below the shelf at ``GAIN``, everything above at
  ``GAIN*GAINHF``" -- so pulling the mids down with ``GAIN`` takes the bass
  down with them and the *ratio* never changes;
* ``AL_FILTER_BANDPASS`` is the only stock filter with a low band at all
  (``HighPassFreqRef`` = 250 Hz), but its band sits at unity and ``GAINLF``
  only ever cuts -- a speaker's bass can never be raised above its mids;
* ``AL_EFFECT_EQUALIZER`` does have the bands (``low_cutoff`` 50-800,
  ``mid1``/``mid2``, ``high``, and gains up to 7.943) but it rides an
  auxiliary **send**, and a send *adds* a shaped copy: the dry signal's mids
  are still there, one dB under a boost that reads as a bass tilt rather than
  a bass cabinet.

So the room does it itself, on the frames it is about to hand a speaker --
the same place it already treats a delay trim, and for the same reason: what
a speaker is fed is the room's to decide, and no engine feature can say
"this speaker plays the bass". The song (``bank.py``) and a voice
(``speech.py``) both ask this module, so a bass cabinet is the same bass
cabinet whatever is coming out of it; a live note is spawned per speaker at
its own trims (``live.py``) and keeps the map's voicing filter instead, which
is the one shape of sound this crossover does not reach.

One attribute carries both halves of a two-way split, and the **sign** is what
says which half the speaker keeps: ``120`` is a bass cabinet (below 120 Hz),
``-3000`` is a tweeter (above 3 kHz), and ``0`` is the full-range speaker every
map had before this attribute. The mark is one number rather than a pair of
``below``/``above`` fields for the reason a builder gives for wanting it: a
speaker can then never hold two crossovers at once. Two of them on one speaker
is a band-pass -- or, on a speaker that keeps neither band, a speaker nobody
can hear -- and neither is what "make this one the sub" means. Picking the
tweeter line therefore *replaces* the bass line rather than joining it: one
mark, one stream, nothing mixed and nothing filtered twice.

Each half keeps its own usable ends (``MIN_CROSSOVER_HZ`` ..
``MAX_CROSSOVER_HZ`` for the bass, ``MIN_TREBLE_HZ`` .. ``MAX_TREBLE_HZ`` for
the treble), and the sign picks which pair a number is settled into: a speaker
is a sub or a tweeter, never one clamped to a corner the other half could not
play. ``crossover_hz`` is the single reader of all of that, and the number it
hands back is signed -- so a caller asks "is this the full-range speaker?" with
``== FULL_RANGE``, never with ``<= 0``, which a tweeter would answer yes to.

Two things are deliberately fixed, because a map attribute should ask *where*
the two halves meet and nothing else:

* the slope is two cascaded one-pole sections (12 dB/oct), which at 120 Hz
  puts 500 Hz around -25 dB and 1 kHz around -37 dB -- a real crossover, not
  a tilt, and a tweeter gets the mirror of it (at 3 kHz, 750 Hz is down there
  too, and no bass is left at all);
* the filter is *stateful* across frames. A per-frame filter starting from
  zero would click at every frame boundary, so the caller keeps the state
  this module returns and hands it back next frame (``apply`` never mutates
  what it is given, so a frame that is refused can simply be offered again).

``crossover_hz`` is the single reader of the map's number, and it is the only
thing that decides whether a speaker has a crossover at all: absent, ``None``
and ``0`` all mean full range, which is the room exactly as it shipped.
"""

from array import array
import math
import sys

# The frames the cinema room exchanges are s16le PCM at this rate (see
# ``libs/audio/cinema/__init__.py``).
SAMPLERATE = 48000
# A map's crossover, as a magnitude: the bass half of the split below
# ``MAX_CROSSOVER_HZ`` (below the floor a speaker would be a rumble nobody
# asked for, above the ceiling it is not a bass cabinet at all) and the treble
# half above ``MIN_TREBLE_HZ``. Both pairs are the same numbers the builder
# menu offers and the element clamps to, so one spelling of each end exists.
MIN_CROSSOVER_HZ = 40.0
MAX_CROSSOVER_HZ = 300.0
MIN_TREBLE_HZ = 800.0
MAX_TREBLE_HZ = 6000.0
# The full-range speaker: what a map with no crossover, and every map written
# before this attribute, means. It is a name for 0.0 because a mark is signed
# -- a tweeter is a real crossover and a negative number, so "no crossover"
# can no longer be spelled as "not above zero".
FULL_RANGE = 0.0
# Cascaded one-pole sections: the sub's own slope, 12 dB/oct, and a tweeter's.
SECTIONS = 2


def crossover_hz(value):
    """A map's ``crossover`` attribute as a usable, *signed* mark.

    0 is the full-range speaker. A positive number is a bass cabinet: the
    speaker keeps what is *below* it. A negative number is a tweeter: the
    speaker keeps what is *above* the magnitude. The sign is the side, so a
    caller compares against ``FULL_RANGE`` rather than against 0 as a limit.

    Unusable input is *full range*, never a guess: a string that will not parse
    and a ``None`` are both "the map said nothing", and the room must keep the
    sound it shipped with rather than pick a corner frequency out of a typo.
    A number inside its half's own ends is clamped rather than rejected,
    because the only thing on the other side of a wrong number here is a
    speaker somebody placed and cannot hear.
    """
    if value is None or value == "":
        return FULL_RANGE
    try:
        number = float(value)
    except (TypeError, ValueError):
        return FULL_RANGE
    if not math.isfinite(number) or number == 0.0:
        return FULL_RANGE
    if number > 0.0:
        return max(MIN_CROSSOVER_HZ, min(MAX_CROSSOVER_HZ, number))
    return -max(MIN_TREBLE_HZ, min(MAX_TREBLE_HZ, -number))


def smoothing(hz, sample_rate=SAMPLERATE):
    """The one-pole coefficient that puts the section's corner at ``hz``.

    ``hz`` may be either half's mark: the corner is the magnitude, so the
    mirror of a filter is the same filter read the other way.
    """
    hz = abs(crossover_hz(hz))
    if hz <= 0.0 or sample_rate <= 0.0:
        return 0.0
    # The usual 1 - exp(-2*pi*f/fs): positive, small, and never above 1 even
    # for a nonsensical corner, so the filter is always a convex blend and its
    # output can never leave the range of its input (no clipping to clamp).
    coefficient = 1.0 - math.exp(-2.0 * math.pi * float(hz) / float(sample_rate))
    return max(0.0, min(1.0, coefficient))


def initial_state(channels=1):
    """A fresh filter's state: silent, one pair of section memories per channel."""
    return tuple(0.0 for _ in range(SECTIONS * max(1, int(channels))))


def _unpack(pcm):
    """s16le bytes as an array of samples, whatever this machine's byte order."""
    samples = array("h")
    samples.frombytes(pcm)
    if sys.byteorder != "little":
        samples.byteswap()
    return samples


def _pack(samples):
    """An array of samples back to s16le bytes."""
    if sys.byteorder != "little":
        samples.byteswap()
    return samples.tobytes()


def apply(buffers, state, hz, sample_rate=SAMPLERATE):
    """Filter each channel buffer, carrying ``state`` across calls.

    ``buffers`` is one s16le byte string per channel (the room's stereo pair,
    or the single mono frame of a voice), ``state`` is what the previous call
    returned for the same speaker (``None`` for a filter that has not run yet)
    and ``hz`` is the signed crossover: a positive one low-passes (a bass
    cabinet keeps what is below it), a negative one high-passes (a tweeter
    keeps what is above it) and ``FULL_RANGE`` is handed straight back.
    Returns ``(buffers, state)``.

    Both halves are pure: the input bytes and the input state are never
    touched, so a caller whose frame is refused (no free buffer, a speaker
    that stopped) can offer the very same frame again and get the very same
    answer instead of advancing a filter the room never played.

    A buffer that cannot be parsed, or a crossover of 0, is handed straight
    back: a room without a crossover takes exactly the path it always did, and
    a speaker is never silenced by a frame this module did not understand.
    """
    frequency = crossover_hz(hz)
    if frequency == FULL_RANGE:
        return tuple(buffers), state or initial_state(len(buffers))
    above = frequency < 0.0
    coefficient = smoothing(frequency, sample_rate)
    if coefficient <= 0.0:
        return tuple(buffers), initial_state(len(buffers))
    memories = tuple(state) if state else initial_state(len(buffers))
    output = []
    fresh = []
    for index, pcm in enumerate(buffers):
        if pcm is None or len(pcm) % 2:
            # Nothing to filter that we can read: pass it through untouched
            # and keep this channel's memories where they were.
            output.append(pcm)
            base = index * SECTIONS
            fresh.extend(memories[base:base + SECTIONS])
            continue
        samples = _unpack(pcm)
        filtered = array("h", bytes(len(samples) * 2))
        position = index * SECTIONS
        first = memories[position] if position < len(memories) else 0.0
        second = (memories[position + 1] if position + 1 < len(memories) else 0.0)
        if above:
            # A tweeter: the same two sections read the other way round, each
            # one "this sample minus its own low-pass". A cascade of those is
            # deliberately *not* a convex blend of its input -- a step can
            # overshoot the input by up to twice its amplitude -- so the
            # result is clamped into s16 rather than trusted, which is also
            # why ``array("h")`` is never handed a value it would raise on.
            for cursor in range(len(samples)):
                value = samples[cursor]
                first += coefficient * (value - first)
                value -= first
                second += coefficient * (value - second)
                value -= second
                filtered[cursor] = (-32768 if value <= -32768.0
                                    else 32767 if value >= 32767.0
                                    else int(value))
        else:
            for cursor in range(len(samples)):
                value = samples[cursor]
                first += coefficient * (value - first)
                second += coefficient * (first - second)
                # The cascade is a convex blend of its input at every step, so
                # the result is already inside s16 range: no clamp, no branch.
                filtered[cursor] = int(second)
        fresh.extend((first, second))
        output.append(_pack(filtered))
    return tuple(output), tuple(fresh)
