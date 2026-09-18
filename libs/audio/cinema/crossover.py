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

The mark is **one edge or two**. One edge carries a two-way split, and the
**sign** is what says which half the speaker keeps: ``120`` is a bass cabinet
(below 120 Hz), ``-3000`` is a tweeter (above 3 kHz), and ``0`` is the
full-range speaker every map had before this attribute. Picking the tweeter
line *replaces* the bass line rather than joining it: one mark, one stream,
nothing mixed and nothing filtered twice.

The third shape is the **band**: a speaker that keeps the middle, which is
what a mid cabinet is and what makes a room a three-way system (a sub below,
the mid between, a tweeter above). A band is two edges, so it is written as
two edges -- ``crossover`` and ``crossover_high`` -- and a mark that carries
both is a ``(low, high)`` tuple rather than a number. That is the one place a
mark is not a float, and every reader has to expect it: a band is a *shape a
builder asked for*, not two crossovers that happened to land on one speaker
(those two, keeping neither band, are a speaker nobody can hear). Its edges
are magnitudes in the same vocabulary as the two halves (``MIN_CROSSOVER_HZ``
up to ``MAX_TREBLE_HZ``), so a band can be a narrow one -- 40-120 Hz is a
band-pass sub, 3-6 kHz a band-pass tweeter, and both are exactly how a single
driver is isolated for testing -- and the pair is ordered here rather than
trusted: written the other way round it is the same band, and two edges that
settle onto each other are the map saying nothing (full range).

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

``crossover_hz`` is the single reader of the map's number (or numbers), and it
is the only thing that decides whether a speaker has a crossover at all:
absent, ``None`` and ``0`` all mean full range, which is the room exactly as
it shipped. It is *idempotent on a mark* -- handing it back what it returned
is how a caller that only holds marks (a bank, a speech leg, a cache key)
reads a band without knowing that bands exist.
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


def _edge(value):
    """One number as a signed mark, the way this module has always read it.

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


def _in_vocabulary(value):
    """One edge of a band, as a magnitude in the whole usable range.

    A band takes its edges from either half's vocabulary -- 40 Hz is the bass
    floor as well as the low end of a band-pass sub, and 6 kHz is the treble
    ceiling as well as the top of a band-pass tweeter -- so the one range is
    from ``MIN_CROSSOVER_HZ`` to ``MAX_TREBLE_HZ`` and nothing new is spelled
    here. Unusable input is ``None``, which is what refuses the band.
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

    A pair is *ordered* here rather than trusted: ``3000`` and ``200`` name the
    same band as ``200`` and ``3000``, so a map that wrote the two attributes
    the other way round gets the band it meant instead of a refusal. Two edges
    that settle onto the same frequency are a band with nothing in it -- the
    one pair that cannot be read as any band -- and that is the map saying
    nothing rather than a reason to invent an edge.
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

    ``crossover_hz(120)`` is a bass cabinet, ``crossover_hz(-3000)`` a tweeter
    and ``crossover_hz(200, 3000)`` a band -- the speaker keeps what lies
    between those two edges. A second edge only means anything next to a first
    one it can be the low edge *of* (a band's low edge is a magnitude whatever
    the map signed it), so a lone ``crossover_high`` on a full-range speaker is
    the map saying nothing about a band and the single edge stands. Idempotent:
    a mark handed back in is a mark handed back out, which is how every caller
    that stores marks (rather than map values) reads one.

    A band edge that cannot make a band does not destroy the mark: the single
    edge the map also wrote is still the shape it asked for, so the speaker
    keeps that rather than falling back to full range -- the answer that would
    make a "sub only" speaker play the whole song.
    """
    if isinstance(value, (tuple, list)):
        if len(value) != 2:
            return FULL_RANGE
        return _band(value[0], value[1]) or FULL_RANGE
    mark = _edge(value)
    if band is None or mark == FULL_RANGE:
        return mark
    # The edges are read from what the *map* wrote, not from the settled single
    # edge: a band's lower edge may sit anywhere in the vocabulary (a band-pass
    # sub starts at 40 and a mid cabinet's low edge is often past the bass
    # half's own ceiling of 300), and clamping it as a bass mark first would
    # move the band before it was ever built.
    return _band(value, band) or mark


def mark_of(spec):
    """A placed speaker's mark, from the two edges the map wrote on it.

    The one place a *spec* becomes a mark: ``spec.crossover`` is the first edge
    (its sign is the side when it stands alone) and ``spec.crossover_high`` is
    the second one, which makes the speaker a band. Callers that only hold a
    mark (a bank, a speech leg, a cache) hand it back to :func:`crossover_hz`,
    which is idempotent, so none of them has to know that bands exist.
    """
    if spec is None:
        return FULL_RANGE
    return crossover_hz(getattr(spec, "crossover", None),
                        getattr(spec, "crossover_high", None))


def band_edges(mark):
    """A mark's two edges when it is a band, ``None`` when it is one edge.

    The one test for "is this a band?" -- a caller must never ask with a
    comparison, because a band is a tuple and a one-edge mark is a number, and
    ``== FULL_RANGE`` (which is what "has this speaker got one?" is asked
    with) has to keep meaning what it always meant for both.
    """
    if isinstance(mark, tuple) and len(mark) == 2:
        return (float(mark[0]), float(mark[1]))
    return None


def mark_key(mark):
    """A mark as the shape a signature, a dict key and a log line can hold.

    Floats stay floats and are rounded the way the room's own signatures have
    always rounded them, so a room whose speakers have no band is byte for byte
    the signature it was; a band is the same two rounded edges. What it exists
    to stop is ``round(mark, 4)`` being asked of a tuple, which is a TypeError
    inside a re-resolve rather than a wrong number.
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

    One wording home for the four answers a crossover has: nothing at all for
    a full-range speaker (so a caller can join this into a sentence without
    leaving an orphan), the bottom of the range for a bass cabinet, the top of
    it for a tweeter and the middle for a band. This is the sentence a menu
    needs to show *why* a room sounds thin -- a pair of one sub and one tweeter
    keeps no middle at all, and nothing else in the game says so out loud.
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

    Public because a read-out that names a *span* rather than a mark (the room
    that keeps no middle, and the hole a three-way room's edges leave) has to
    say the same numbers the same way: "200 Hz", "1.5 kHz", never "200.0".
    """
    if hz >= 1000.0:
        return f"{hz / 1000.0:.1f} kHz".replace(".0 kHz", " kHz")
    return f"{hz:.0f} Hz"


def smoothing(hz, sample_rate=SAMPLERATE):
    """The one-pole coefficient that puts the section's corner at ``hz``.

    ``hz`` may be either half's mark -- or either edge of a band: the corner is
    the magnitude, so the mirror of a filter is the same filter read the other
    way. It is settled in the whole vocabulary (``_in_vocabulary``) and *not*
    through ``crossover_hz``: that reader clamps a positive number into the
    *bass* half, and a band's upper edge is a magnitude of 800 Hz and up, so
    placing a 3 kHz corner through it would put the corner at 300 Hz.
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

    One edge is a two-way split: one pair of sections, read as a low-pass for a
    positive mark and as a high-pass for a negative one. A band is both at
    once -- a high-pass pair at its low edge and a low-pass pair at its high
    one -- so it is four. It is a function rather than a constant because the
    *state* a caller keeps is sized by it: a band's state offered to a one-edge
    filter (or the other way round) is padded or trimmed, never indexed off the
    end of the tuple.
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

    ``array("h")`` raises on a value outside its range instead of wrapping it,
    so an unclamped overshoot would take a speaker out at the loudest moment
    of a song. Only the half-cascades need it: a low-pass is a convex blend of
    its own input and can never leave s16 range.
    """
    if value <= -32768.0:
        return -32768
    if value >= 32767.0:
        return 32767
    return int(value)


def _memories(state, channels, stride):
    """A filter's own memories for this frame, from whatever state was left.

    Short is padded with silence and long is trimmed, so the state a caller
    keeps is never indexed past its end: a speaker whose mark changed shape
    between two frames is the ordinary case here (a builder dialling a band
    onto a bass cabinet mid-song), and it must not be the case that raises.
    """
    needed = max(1, int(channels)) * int(stride)
    values = list(state) if state else []
    if len(values) < needed:
        values.extend([0.0] * (needed - len(values)))
    return values[:needed]


def apply(buffers, state, hz, sample_rate=SAMPLERATE):
    """Filter each channel buffer, carrying ``state`` across calls.

    ``buffers`` is one s16le byte string per channel (the room's stereo pair,
    or the single mono frame of a voice), ``state`` is what the previous call
    returned for the same speaker (``None`` for a filter that has not run yet)
    and ``hz`` is the mark: a positive one low-passes (a bass cabinet keeps
    what is below it), a negative one high-passes (a tweeter keeps what is
    above it), a ``(low, high)`` band keeps what lies between the two edges and
    ``FULL_RANGE`` is handed straight back. Returns ``(buffers, state)``.

    Both halves are pure: the input bytes and the input state are never
    touched, so a caller whose frame is refused (no free buffer, a speaker
    that stopped) can offer the very same frame again and get the very same
    answer instead of advancing a filter the room never played.

    A buffer that cannot be parsed, or a crossover of 0, is handed straight
    back: a room without a crossover takes exactly the path it always did, and
    a speaker is never silenced by a frame this module did not understand.

    A state of another shape is *settled*, not refused: the memories this mark
    needs are taken from the front of what was handed in and the rest start at
    silence, so a speaker that was a bass cabinet a moment ago and is a band
    now carries the low-pass pair it had and starts its high-pass pair from a
    standstill -- rather than raising inside a song, which is what indexing a
    two-memory state with a four-section filter would do.
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
