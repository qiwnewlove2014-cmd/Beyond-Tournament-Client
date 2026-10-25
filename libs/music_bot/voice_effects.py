"""The broadcast voice through a guitar amp -- for a player with no guitar.

What this is
------------
A player who cannot play a guitar can still want the *sound*: a voice pushed
through a distorted amp, a clean amp with a chorus, a fuzz pedal, a small radio
or a bitcrusher. Nothing here becomes a string or a note -- a voice through a
guitar amp is a voice through a guitar amp, and that is the joke. The presets are
the pedals on the floor; the voice is still the player's own.

**It is a line, like every other voice effect.** The broadcast voice is shaped on
the talker's machine, in one 20 ms frame at a time, before Opus: a frame in is
the same frame out, so the PA's sequence numbers, a cinema room's playout, the
jam clock and a listener's decoder count exactly what they counted before. The
line WRAPS whatever line the press already had (`AutoTuneLine`, the fast harmony
line or `SpeechKeyLine`), so Auto-Tune, Harmony and the speech key keep working
and the effect is the last thing measured before the voice leaves. With nothing
else on, the wrapper holds no other line and the effect is the only work.

Only the broadcast leg is wrapped -- `VoiceChatRecord._speech_line_for` is what
decides that, exactly as it does for Auto-Tune. Proximity voice stays raw.

The chain, in order
-------------------
1. A second-order high-pass at the preset's own corner (95 Hz for the amp, 430
   for the radio), so a desk rumble is not driven into the clipper. It is a
   biquad and not an FIR deliberately: a 129-tap windowed-sinc at 95 Hz still
   passes only 0.76 of a 220 Hz tone, so the "high-pass" was taking 12 dB out
   of the voice's own fundamental. Two poles put the corner where it is named,
   and cost a couple of samples of delay instead of a look-ahead.
2. A noise gate. High gain on an open microphone is a feedback loop waiting for
   a room; the gate closes over about eight frames when the frame's RMS falls
   below the preset's threshold and opens in half a frame, always with a ramp
   across the frame rather than a step (a step in gain is a click).
3. The shaper: `tanh` with a little bias for the amp and radio (a valve's
   asymmetric soft clip), a hard, more biased clip for the fuzz, and nothing for
   the clean amp or the crusher.
4. The crusher, when the preset has one: sample-and-hold at 8 kHz and 6 bits.
5. The cabinet: a linear-phase low-pass at 4.2 kHz (3.4 for the fuzz, 6.5 for
   the clean amp), then a 20 Hz high-pass that removes the DC the asymmetric
   shaper leaves behind.
6. The chorus, on the clean preset: a 12 ms delay swept 3 ms either way at
   0.45 Hz, mixed a third in. It is the only preset with a delay of its own.
7. Output trim and a soft ceiling. Opus's `voip` mode is speech-tuned and will
   thin the top of any distortion it is handed; the cabinet is deliberately
   below the range where that hurts, and the ceiling keeps the encoder from
   being handed a clipped frame.

The cabinet is FIR (windowed sinc, 257 taps), so its delay is a known constant:
128 samples after the shaper, 2.7 ms, well inside the 20 ms frame. `_Fir` carries
its tail from frame to frame, so the stream is continuous across frame
boundaries. The high-passes are biquads (see above), whose delay is a few
samples and not worth a look-ahead.

Switching a preset mid-sentence
-------------------------------
The preset is read per frame, like Auto-Tune's switch. A change is crossfaded
from the previous frame's output into the new one, sample by sample, because a
hard switch is a step the size of the waveform -- and that is heard as a click.
The first frame of a press has nothing to crossfade from (the microphone was
silent), so it starts clean.

The Developer's lock
--------------------
`voice_fx` is a row in `server/libs/music_bot_gate.ts`. While it is locked the
preset reads as Off, so a saved effect stops applying and the voice is dry; the
Settings line is hidden by the same gate.

Pinned by `client/tests/test_voice_effects.py`.
"""

import threading

import numpy as np

from .. import options
from . import gate

#: One microphone frame: 20 ms of 48 kHz mono16, the unit every voice line is
#: fed (`libs/voice_chat.VOICE_FRAME_BYTES`).
FRAME_BYTES = 1920
SAMPLE_RATE = 48000

#: ``(id, menu name, what Tab reads on that line)``. The menu name is short
#: enough to arrow past; the description is where the pedal explains itself.
VOICE_EFFECTS = (
    ("off", "Off",
     "Your own voice, dry: no amp, no pedal. This is the same voice you had "
     "before the feature existed."),
    ("amp", "Guitar Amp",        "A distorted guitar amp: pre-gain, a valve-ish soft clip and a 4x12 "
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
)
DEFAULT_EFFECT = "off"

#: The two saved option keys live here, beside the catalogue and the only reader.
OPTION = "music_bot_voice_effect"

#: What each preset does, in pieces the processor can build. ``hp``/``lp`` are
#: the cabinet's corners in Hz, ``drive`` the gain into the shaper, ``bias`` the
#: asymmetry, ``gate`` the RMS floor and ``out`` the trim after the cabinet.
_SPECS = {
    "amp": dict(hp=95.0, lp=4200.0, drive=9.0, shape="tanh", bias=0.10,
                gate=0.008, out=0.72, chorus=0.0, crush=None),
    "clean": dict(hp=100.0, lp=6500.0, drive=1.0, shape=None, bias=0.0,
                  gate=0.004, out=0.92, chorus=0.35, crush=None),
    "fuzz": dict(hp=110.0, lp=3400.0, drive=22.0, shape="hard", bias=0.18,
                 gate=0.010, out=0.55, chorus=0.0, crush=None),
    "radio": dict(hp=430.0, lp=2800.0, drive=6.0, shape="tanh", bias=0.05,
                  gate=0.008, out=0.85, chorus=0.0, crush=None),
    "crush": dict(hp=80.0, lp=5000.0, drive=1.0, shape=None, bias=0.0,
                  gate=0.006, out=0.80, chorus=0.0, crush=(6, 8000.0)),
}

#: The gate's two speeds, per frame. Opening is quick (a word must not lose its
#: first consonant); closing is slow enough that a breath holds the gate open
#: rather than chopping the tail of a sentence into pieces.
GATE_OPEN = 0.5
GATE_CLOSE = 0.12


def clamp_effect(value):
    """A preset id that exists, whatever was saved or handed in."""
    for effect_id_, _label, _note in VOICE_EFFECTS:
        if value == effect_id_:
            return effect_id_
    return DEFAULT_EFFECT


def effect_label(value):
    """The menu's words for a preset: 'Guitar Amp'."""
    for effect_id_, label, _note in VOICE_EFFECTS:
        if effect_id_ == clamp_effect(value):
            return label
    return VOICE_EFFECTS[0][1]


def effect_note(value):
    """What Tab reads on that preset's line."""
    for effect_id_, _label, note in VOICE_EFFECTS:
        if effect_id_ == clamp_effect(value):
            return note
    return VOICE_EFFECTS[0][2]


def selected():
    """The saved preset, without asking the Developer's gate."""
    return clamp_effect(options.get(OPTION, DEFAULT_EFFECT))


def effect_id():
    """The preset to apply right now; Off when staff have locked the family.

    Reading the option is not enough: a lock must silence the effect for a
    player who already saved one, not merely hide the line that chose it.
    """
    if gate.is_locked("voice_fx"):
        return DEFAULT_EFFECT
    return selected()


def enabled():
    """Whether a preset is chosen and allowed."""
    return effect_id() != DEFAULT_EFFECT


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

    @classmethod
    def highpass(cls, hz, q=0.7071067811865476):
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
        return cls((b0 / a0, b1 / a0, b2 / a0),
                   (1.0, a1 / a0, a2 / a0))

    def process(self, block):
        b0, b1, b2 = self.b
        _unity, a1, a2 = self.a
        z1, z2 = self._z1, self._z2
        out = np.empty_like(block)
        for index in range(block.size):
            x = float(block[index])
            y = b0 * x + z1
            z1 = b1 * x - a1 * y + z2
            z2 = b2 * x - a2 * y
            out[index] = y
        self._z1, self._z2 = z1, z2
        return out


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


class _EffectProcessor:
    """One preset's chain, with the state a stream needs between frames."""

    #: The cabinet is designed at a fixed length so every preset has the same
    #: delay: 128 samples after the shaper, 2.7 ms. Every preset also carries the
    #: same two biquads (input high-pass and DC block), so the presets stay in
    #: step with one another as well as with the frames.
    TAPS = 257

    def __init__(self, preset):
        spec = _SPECS[preset]
        self.preset = preset
        self.spec = spec
        self.pre = _Biquad.highpass(spec["hp"])
        self.dc = _Biquad.highpass(20.0)
        self.post = _Fir(_lowpass(spec["lp"], self.TAPS))
        self._gate_gain = 0.0
        self._gate_threshold = float(spec["gate"])
        self._drive = float(spec["drive"])
        self._bias = float(spec["bias"])
        self._shape = spec["shape"]
        self._out = float(spec["out"])
        # A chorus is a modulated delay; the ring is long enough for the base
        # delay plus its whole sweep.
        self._chorus = float(spec["chorus"])
        self._ring = None
        self._ring_pos = 0
        self._phase = 0.0
        if self._chorus:
            self._ring = np.zeros(4096, dtype=np.float32)
        # A bitcrusher holds one sample for several and rounds what it holds.
        self._crush = spec["crush"]
        self._crush_value = 0.0
        self._crush_phase = 0.0

    # ------------------------------------------------------------------ the gate
    def _gate(self, block):
        """Open or close this frame, then ramp the gain ACROSS it.

        The ramp is what keeps a gate from clicking: the gain arrives at the
        frame boundary at exactly the value the next frame starts from.
        """
        level = float(np.sqrt(np.mean(np.square(block)))) if block.size else 0.0
        before = self._gate_gain
        if level >= self._gate_threshold:
            self._gate_gain = min(1.0, before + GATE_OPEN)
        else:
            self._gate_gain = max(0.0, before - GATE_CLOSE)
        if before == self._gate_gain:
            return block * self._gate_gain
        ramp = np.linspace(before, self._gate_gain, block.size,
                           endpoint=False, dtype=np.float32)
        return block * ramp

    # ---------------------------------------------------------------- the shaper
    def _shape_block(self, block):
        if self._shape is None:
            return block
        driven = block * self._drive
        if self._shape == "tanh":
            offset = np.tanh(self._bias)
            scaled = (np.tanh(driven + self._bias) - offset) / (1.0 - offset)
            return scaled
        # A fuzz's hard clip, biased so the two halves do not match: that
        # asymmetry is the octave-ish edge a fuzz has and a distortion does not.
        floor = -1.0 + 0.0
        clipped = np.clip(driven + self._bias, floor, 1.0)
        scaled = (clipped - np.clip(self._bias, floor, 1.0)) / (1.0 - abs(self._bias))
        return scaled

    # --------------------------------------------------------------- the crusher
    def _crush_block(self, block):
        if self._crush is None:
            return block
        bits, rate = self._crush
        step = SAMPLE_RATE / float(rate)
        held = np.empty_like(block)
        value = self._crush_value
        phase = self._crush_phase
        for index in range(block.size):
            if phase <= 0.0:
                value = block[index]
                phase += step
            held[index] = value
            phase -= 1.0
        self._crush_value = float(value)
        self._crush_phase = phase
        levels = float(2 ** (int(bits) - 1))
        return np.round(held * levels) / levels

    # ---------------------------------------------------------------- the chorus
    def _chorus_block(self, block):
        if not self._chorus:
            return block
        limits = self._ring.size
        start = self._ring_pos
        positions = np.arange(block.size)
        write_at = (start + positions) % limits
        self._ring[write_at] = block
        # 12 ms base, 3 ms sweep, 0.45 Hz: the width is what is heard, and the
        # base is why this preset carries the only real delay in the set.
        sweep = (0.012 + 0.003 * np.sin(
            2.0 * np.pi * 0.45 * positions / SAMPLE_RATE + self._phase))
        delay = sweep * SAMPLE_RATE
        read_at = (start + positions - delay) % limits
        lower = np.floor(read_at).astype(np.int64)
        frac = (read_at - lower).astype(np.float32)
        delayed = (self._ring[lower % limits] * (1.0 - frac)
                   + self._ring[(lower + 1) % limits] * frac)
        self._ring_pos = (start + block.size) % limits
        self._phase = (self._phase
                       + 2.0 * np.pi * 0.45 * block.size / SAMPLE_RATE) % (
                           2.0 * np.pi)
        return block * (1.0 - self._chorus) + delayed * self._chorus

    # ------------------------------------------------------------------ the frame
    def process(self, pcm):
        """Shape one frame; the same number of bytes comes back."""
        block = np.frombuffer(bytes(pcm), dtype="<i2").astype(np.float32) / 32768.0
        if block.size <= 0:
            return bytes(pcm)
        block = self.pre.process(block)
        block = self._gate(block)
        block = self._shape_block(block)
        block = self._crush_block(block)
        block = self.dc.process(block)
        block = self.post.process(block)
        block = self._chorus_block(block)
        # A soft ceiling rather than a hard clip: the encoder is handed a frame
        # that is already inside full scale, and the last harmonics fold gently.
        shaped = np.tanh(block * self._out * 1.1) / np.tanh(1.1)
        return (np.clip(shaped, -1.0, 1.0) * 32767.0).round().astype(
            "<i2").tobytes()


def _crossfade(previous, fresh):
    """Blend the previous frame's output into this one, sample by sample.

    A preset change lands between two frames, and the two sides of it are not
    the same waveform: switching hard is a step, and a step is a click. The
    blend is one frame long (20 ms), which is short enough to be heard as a
    change of sound rather than as a slide.
    """
    if not previous or len(previous) != len(fresh):
        return fresh
    old = np.frombuffer(previous, dtype="<i2").astype(np.float32)
    new = np.frombuffer(fresh, dtype="<i2").astype(np.float32)
    blend = np.linspace(0.0, 1.0, old.size, endpoint=False, dtype=np.float32)
    mixed = old * (1.0 - blend) + new * blend
    return np.clip(mixed, -32768.0, 32767.0).round().astype("<i2").tobytes()


class VoiceEffectLine:
    """The effect on top of whatever line the press already had.

    Wrapping the line -- instead of another line beside it -- is what keeps the
    promises the voice path already makes: one frame in is one frame out, the
    release still hands the inner line's look-ahead back, and Auto-Tune, Harmony
    and the speech key do not have to know this exists. `inner` is None for a
    press that has no other line; the effect is then the whole line.
    """

    def __init__(self, inner=None):
        self.inner = inner
        self._lock = threading.Lock()
        self._processor = None
        self._preset = DEFAULT_EFFECT
        self._last = b""

    @property
    def semitones(self):
        return self.inner.semitones if self.inner is not None else 0

    def set_semitones(self, semitones):
        if self.inner is not None:
            return self.inner.set_semitones(semitones)
        return semitones

    def shape(self, pcm):
        """Shape one frame through the inner line, then through the preset."""
        with self._lock:
            data = self.inner.shape(pcm) if self.inner is not None else bytes(pcm)
            wanted = effect_id()
            if wanted == DEFAULT_EFFECT:
                if self._processor is None:
                    self._last = data
                    return data
                # Fade the processed voice back into the dry one rather than
                # cutting the pedal: the last processed frame is the other half
                # of the crossfade.
                out = _crossfade(self._last, data)
                self._processor = None
                self._preset = DEFAULT_EFFECT
                self._last = out
                return out
            if self._processor is None or wanted != self._preset:
                switching = self._processor is not None
                self._processor = _EffectProcessor(wanted)
                out = self._processor.process(data)
                if switching:
                    out = _crossfade(self._last, out)
                self._preset = wanted
                self._last = out
                return out
            out = self._processor.process(data)
            self._last = out
            return out

    def tail_frames(self, frame_bytes=FRAME_BYTES):
        """The inner line's look-ahead, with the effect already on it."""
        frames = (list(self.inner.tail_frames(frame_bytes))
                  if self.inner is not None else [])
        if not frames:
            return []
        with self._lock:
            wanted = effect_id()
            if wanted == DEFAULT_EFFECT:
                return frames
            processor = self._processor
            if processor is None or wanted != self._preset:
                processor = _EffectProcessor(wanted)
            return [processor.process(frame) for frame in frames]
