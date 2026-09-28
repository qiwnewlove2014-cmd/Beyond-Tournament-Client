"""A cinema speaker's crossover: the room's own filter, played by the room.

A speaker a builder marks is fed the room's programme *on one side* of that
frequency and none of the other -- the bottom for a bass cabinet, the top for a
tweeter. Not quieter on the other side: gone. That is the one thing OpenAL
cannot be asked for:

* every EFX filter gain is capped at 1.0 (``AL_LOWPASS_MAX_GAIN``), the low-pass
  shelf is anchored at 5 kHz (``LowPassFreqRef``) and its shape is "below the
  shelf at ``GAIN``, above at ``GAIN*GAINHF``", so pulling the mids down with
  ``GAIN`` takes the bass down with them and the *ratio* never changes;
* ``AL_FILTER_BANDPASS`` is the only stock filter with a low band at all
  (``HighPassFreqRef`` 250 Hz), but its band sits at unity and ``GAINLF`` only
  ever cuts -- a bass can never be raised above the mids;
* ``AL_EFFECT_EQUALIZER`` does have the bands (gains up to 7.943) but it rides an
  auxiliary **send**, and a send *adds* a shaped copy.

So the room filters the frames it is about to hand a speaker -- the same place it
already treats a delay trim. The song (``bank.py``) and a voice (``speech.py``)
both ask this module, so a bass cabinet is the same bass cabinet whatever comes
out of it; a live note keeps the map's voicing filter instead (``live.py``).

The mark is **one edge or two**, and the *sign* says which half is kept: ``120``
a bass cabinet (below 120 Hz), ``-3000`` a tweeter (above 3 kHz), ``0`` the
full-range speaker every map had before this attribute. The third shape is the
**band**: two edges (``crossover`` + ``crossover_high``), which is what makes a
room a three-way system, and the one place a mark is a ``(low, high)`` tuple
rather than a number -- the pair is ordered here rather than trusted, and two
edges that settle onto each other are the map saying nothing. ``crossover_hz`` is
the single reader of all of that, it hands the number back *signed*, and it is
idempotent on a mark -- so a caller asks "is this the full-range speaker?" with
``== FULL_RANGE``, never with ``<= 0``, which a tweeter would answer yes to.

Two things are deliberately fixed, because a map attribute should ask *where* the
halves meet and nothing else: the slope is two cascaded one-pole sections
(12 dB/oct, so at 120 Hz, 500 Hz sits near -25 dB and 1 kHz near -37 dB, and a
tweeter gets the mirror), and the filter is *stateful* across frames -- a
per-frame filter starting from zero clicks at every boundary, so the caller keeps
the state this module returns and ``apply`` never mutates what it is given.

Rules, traps and the measured numbers: .agents/skills/cinema_speaker_system/.
"""

from array import array
import math
import sys

# The frames the cinema room exchanges are s16le PCM at this rate (see
# ``libs/audio/cinema/__init__.py``).
SAMPLERATE = 48000
# A map's crossover, as a magnitude: the bass half of the split below
# MAX_CROSSOVER_HZ (below the floor a speaker would be a rumble nobody asked
# for, above the ceiling it is not a bass cabinet at all) and the treble half
# above MIN_TREBLE_HZ. Both pairs are the same numbers the builder menu offers
# and the element clamps to, so one spelling of each end exists.
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


def _edge(value):
    """One number as a signed mark, the way this module has always read it.

    0 is the full-range speaker, a positive number is a bass cabinet (the speaker
    keeps what is *below* it), a negative one is a tweeter (what is *above* the
    magnitude) -- the sign is the side, so a caller compares against ``FULL_RANGE``
    rather than against 0 as a limit.

    Unusable input is *full range*, never a guess: a string that will not parse and a
    ``None`` are both "the map said nothing". A number inside its half's own ends is
    clamped rather than rejected, because the only thing on the other side of a wrong
    number here is a speaker somebody placed and cannot hear.
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


def _in_vocabulary(value):
    """One edge of a band, as a magnitude in the whole usable range.

    40 Hz is the bass floor as well as the low end of a band-pass sub, 6 kHz the
    treble ceiling as well as the top of a band-pass tweeter, so the one range is
    ``MIN_CROSSOVER_HZ``..``MAX_TREBLE_HZ``. Unusable input is ``None``.
    """
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number == 0.0:
        return None
    return max(MIN_CROSSOVER_HZ, min(MAX_TREBLE_HZ, abs(number)))


def _band(low, high):
    """Two edges as a band, or ``None`` when they cannot be one.

    A pair is *ordered* here rather than trusted: 3000/200 names the same band as
    200/3000. Two edges that settle onto the same frequency are the one pair that
    cannot be read as a band -- the map saying nothing rather than a reason to invent
    an edge.
    """
    edges = (_in_vocabulary(low), _in_vocabulary(high))
    if edges[0] is None or edges[1] is None:
        return None
    if edges[0] > edges[1]:
        edges = (edges[1], edges[0])
    if edges[0] == edges[1]:
        return None
    return edges


def crossover_hz(value, band=None):
    """A map's crossover as a usable *mark*: one edge, or a band of two.

    ``crossover_hz(120)`` a bass cabinet, ``(-3000)`` a tweeter, ``(200, 3000)`` the
    middle. A second edge only means anything next to a first one it can be the low
    edge *of*, so a lone ``crossover_high`` on a full-range speaker is the map saying
    nothing and the single edge stands. Idempotent: a mark handed back in is a mark
    handed back out, which is how a caller that stores marks reads one. A band edge
    that cannot make a band does not destroy the mark -- the single edge the map also
    wrote is still the shape it asked for.
    """
    if isinstance(value, (tuple, list)):
        if len(value) != 2:
            return FULL_RANGE
        return _band(value[0], value[1]) or FULL_RANGE
    mark = _edge(value)
    if band is None or mark == FULL_RANGE:
        return mark
    # The edges are read from what the *map* wrote, not from the settled single
    # edge: a band's lower edge may sit anywhere in the vocabulary (a band-pass sub
    # starts at 40, a mid's low edge is often past the bass half's own ceiling of
    # 300), and clamping it as a bass mark first would move the band.
    return _band(value, band) or mark


def mark_of(spec):
    """A placed speaker's mark, from the two edges the map wrote on it.

    The one place a *spec* becomes a mark: ``spec.crossover`` is the first edge (its
    sign is the side when it stands alone), ``spec.crossover_high`` the second, which
    makes the speaker a band.
    """
    if spec is None:
        return FULL_RANGE
    return crossover_hz(getattr(spec, "crossover", None),
                        getattr(spec, "crossover_high", None))


def band_edges(mark):
    """A mark's two edges when it is a band, ``None`` when it is one edge.

    The one test for "is this a band?": never a comparison, because a band is a tuple
    and a one-edge mark is a number, and ``== FULL_RANGE`` has to keep meaning what it
    always meant for both.
    """
    if isinstance(mark, tuple) and len(mark) == 2:
        return (float(mark[0]), float(mark[1]))
    return None


def mark_key(mark):
    """A mark as the shape a signature, a dict key and a log line can hold.

    Floats stay floats and are rounded the way the room's own signatures always
    rounded them, so a room with no band is byte for byte the signature it was; a band
    is the same two rounded edges. It stops ``round(mark, 4)`` being asked of a tuple,
    which is a TypeError inside a re-resolve rather than a wrong number.
    """
    edges = band_edges(mark)
    if edges:
        return tuple(round(edge, 4) for edge in edges)
    try:
        return round(float(mark), 4)
    except (TypeError, ValueError):
        return FULL_RANGE


def describe(value, band=None):
    """What a speaker keeps, said the way a menu line says it.

    One wording home for the four answers: nothing at all for a full-range speaker
    (so a caller can join it into a sentence without an orphan), the bottom of the
    range, the top of it, and the middle. It is the sentence a menu needs to show
    *why* a room sounds thin -- a sub and a tweeter keep no middle at all.
    """
    mark = crossover_hz(value, band)
    edges = band_edges(mark)
    if edges:
        return (f"between {hz_label(edges[0])} and {hz_label(edges[1])} "
                f"(the middle of the range)")
    if mark == FULL_RANGE:
        return ""
    corner = hz_label(abs(mark))
    return (f"above {corner} (the top of the range)" if mark < 0
            else f"below {corner} (the bottom of the range)")


def hz_label(hz):
    """A corner frequency in the unit a person would say it in.

    Public because a read-out that names a *span* rather than a mark has to say the
    same numbers the same way: "200 Hz", "1.5 kHz", never "200.0".
    """
    if hz >= 1000.0:
        return f"{hz / 1000.0:.1f} kHz".replace(".0 kHz", " kHz")
    return f"{hz:.0f} Hz"


def smoothing(hz, sample_rate=SAMPLERATE):
    """The one-pole coefficient that puts the section's corner at ``hz``.

    ``hz`` may be either half's mark -- or either edge of a band: the corner is the
    magnitude, so the mirror of a filter is the same filter read the other way. It is
    settled in the whole vocabulary and *not* through ``crossover_hz``: that reader
    clamps a positive number into the *bass* half, and a band's upper edge is a
    magnitude of 800 Hz and up, so placing a 3 kHz corner through it would put the
    corner at 300 Hz.
    """
    corner = _in_vocabulary(hz)
    if corner is None or sample_rate <= 0.0:
        return 0.0
    hz = corner
    # The usual 1 - exp(-2*pi*f/fs): positive, small, and never above 1 even
    # for a nonsensical corner, so the filter is always a convex blend and its
    # output can never leave the range of its input (no clipping to clamp).
    coefficient = 1.0 - math.exp(-2.0 * math.pi * float(hz) / float(sample_rate))
    return max(0.0, min(1.0, coefficient))


def sections(mark):
    """How many one-pole sections one mark's filter runs per channel.

    One edge is a two-way split (one pair of sections, read as a low-pass for a
    positive mark and a high-pass for a negative one); a band is both at once, so
    four. A function rather than a constant because the *state* a caller keeps is
    sized by it: a band's state offered to a one-edge filter is padded or trimmed,
    never indexed off the end of the tuple.
    """
    return SECTIONS * (2 if band_edges(mark) else 1)


def initial_state(channels=1, mark=FULL_RANGE):
    """A fresh filter's state: silent, this mark's own memories per channel."""
    return tuple(0.0 for _ in range(sections(mark) * max(1, int(channels))))


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


def _s16(value):
    """A filtered float as an s16 int, clamped rather than wrapped.

    ``array("h")`` raises on a value outside its range instead of wrapping it, so an
    unclamped overshoot would take a speaker out at the loudest moment of a song.
    Only the half-cascades need it: a low-pass is a convex blend of its own input and
    can never leave s16 range.
    """
    if value <= -32768.0:
        return -32768
    if value >= 32767.0:
        return 32767
    return int(value)


def _memories(state, channels, stride):
    """A filter's own memories for this frame, from whatever state was left.

    Short is padded with silence and long is trimmed, so the state a caller keeps is
    never indexed past its end: a mark changed shape between two frames (a builder
    dialling a band onto a bass cabinet mid-song) is the ordinary case here, and it
    must not be the case that raises.
    """
    needed = max(1, int(channels)) * int(stride)
    values = list(state) if state else []
    if len(values) < needed:
        values.extend([0.0] * (needed - len(values)))
    return values[:needed]


def apply(buffers, state, hz, sample_rate=SAMPLERATE):
    """Filter each channel buffer, carrying ``state`` across calls.

    ``buffers`` is one s16le byte string per channel, ``state`` is what the previous
    call returned for the same speaker (``None`` for a filter that has not run) and
    ``hz`` is the mark: a positive one low-passes, a negative one high-passes, a
    ``(low, high)`` band keeps what lies between the two edges, and ``FULL_RANGE`` is
    handed straight back. Returns ``(buffers, state)``.

    Both halves are pure: the input bytes and the input state are never touched, so a
    caller whose frame is refused can offer the very same frame again and get the very
    same answer instead of advancing a filter the room never played. A buffer that
    cannot be parsed, or a crossover of 0, is handed straight back: a speaker is never
    silenced by a frame this module did not understand. A state of another shape is
    *settled*, not refused -- the memories this mark needs come from the front of what
    was handed in and the rest start at silence, rather than raising inside a song.
    """
    frequency = crossover_hz(hz)
    if frequency == FULL_RANGE:
        return tuple(buffers), state or initial_state(len(buffers))
    edges = band_edges(frequency)
    above = edges is None and frequency < 0.0
    stride = sections(frequency)
    if edges:
        # The two corners of a band, in the order the signal meets them: the
        # high-pass pair at the low edge first, then the low-pass pair at the
        # high one. Both are the same one-pole section, so a band's skirt is
        # exactly as steep as either half of a two-way split (12 dB/oct).
        low_coefficient = smoothing(edges[1], sample_rate)
        high_coefficient = smoothing(edges[0], sample_rate)
        coefficient = 0.0
    else:
        low_coefficient = high_coefficient = 0.0
        coefficient = smoothing(frequency, sample_rate)
    if edges and (low_coefficient <= 0.0 or high_coefficient <= 0.0):
        return tuple(buffers), initial_state(len(buffers))
    if not edges and coefficient <= 0.0:
        return tuple(buffers), initial_state(len(buffers))
    memories = _memories(state, len(buffers), stride)
    output = []
    fresh = []
    for index, pcm in enumerate(buffers):
        if pcm is None or len(pcm) % 2:
            # Nothing to filter that we can read: pass it through untouched
            # and keep this channel's memories where they were.
            output.append(pcm)
            base = index * stride
            fresh.extend(memories[base:base + stride])
            continue
        samples = _unpack(pcm)
        filtered = array("h", bytes(len(samples) * 2))
        position = index * stride
        memories_here = memories[position:position + stride]
        if edges:
            high_first, high_second = memories_here[0], memories_here[1]
            low_first, low_second = memories_here[2], memories_here[3]
            for cursor in range(len(samples)):
                value = float(samples[cursor])
                high_first += high_coefficient * (value - high_first)
                value -= high_first
                high_second += high_coefficient * (value - high_second)
                value -= high_second
                low_first += low_coefficient * (value - low_first)
                low_second += low_coefficient * (low_first - low_second)
                filtered[cursor] = _s16(low_second)
            fresh.extend((high_first, high_second, low_first, low_second))
            output.append(_pack(filtered))
            continue
        first = memories_here[0]
        second = memories_here[1]
        if above:
            # A tweeter: the same two sections read the other way round, each one "this
            # sample minus its own low-pass". A cascade of those is deliberately *not* a
            # convex blend of its input -- a step can overshoot the input by up to twice its
            # amplitude -- so the result is clamped into s16 rather than trusted, which is
            # also why array("h") is never handed a value it would raise on.
            for cursor in range(len(samples)):
                value = samples[cursor]
                first += coefficient * (value - first)
                value -= first
                second += coefficient * (value - second)
                value -= second
                filtered[cursor] = _s16(value)
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
