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
5. The octave, when the preset wants one: the rectified voice mixed back with
   the dry one. Rectification is the oldest octave trick there is -- it adds a
   copy an octave up without a pitch shifter, and it is dirty on purpose. It
   runs BEFORE the DC block because a rectified waveform has a large offset.
6. The cabinet: a linear-phase low-pass at 4.2 kHz (3.4 for the fuzz, 6.5 for
   the clean amp), then a 20 Hz high-pass that removes the DC the asymmetric
   shaper leaves behind.
7. The chorus, on the clean preset: a 12 ms delay swept 3 ms either way at
   0.45 Hz, mixed a third in.
8. The pedals that are one block each, off unless the preset names them: a
   tremolo (5.2 Hz, depth 0.85), an auto-wah (a resonant band swept 360-1800 Hz
   at 1.25 Hz), a two-stage phaser (all-passes swept 320-1500 Hz at 0.4 Hz), a
   slapback echo (145 ms, 0.35 feedback) and a ring modulator (55 Hz).
9. The modern pedals (2026-10-25): an exciter (Air), a raised formant
   (Talkbox), a channel vocoder, an upward-and-downward compressor (Hyperpop),
   a stepped three-band filter, a beat duck (Pump), four drifting taps (Cloud),
   a backwards ghost (Swell), a held slice (Freeze) and a varispeed that slows
   to a stop (Tape Stop). Two rules keep them out of trouble: **a gain that
   moves once per frame is a 50 Hz buzz**, so Hyperpop's follower runs per
   sample and the step filter crossfades the frame its band changes in; and **a
   varispeed is a delay that grows faster than real time** (rate `1 -
   d(delay)/dt`), so every gain is zero where a head jumps.
10. Output trim and a soft ceiling. Opus's `voip` mode is speech-tuned and will
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

import collections
import threading

import numpy as np

from .. import options
from . import gate
from .voice_effect_catalogue import (DEFAULT_EFFECT, VOICE_EFFECTS,
                                     clamp_effect, effect_label, effect_note)

#: One microphone frame: 20 ms of 48 kHz mono16, the unit every voice line is
#: fed (`libs/voice_chat.VOICE_FRAME_BYTES`).
FRAME_BYTES = 1920
SAMPLE_RATE = 48000


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
    # The pedals below reuse the same chain and add one block each; every
    # setting is read with `.get` so an older preset carries no dead keys.
    "octave": dict(hp=110.0, lp=5500.0, drive=1.0, shape=None, bias=0.0,
                   gate=0.006, out=1.55, octave=0.65),
    "tremolo": dict(hp=100.0, lp=6200.0, drive=1.0, shape=None, bias=0.0,
                    gate=0.004, out=0.94, tremolo=(5.2, 0.85)),
    "wah": dict(hp=120.0, lp=5000.0, drive=1.0, shape=None, bias=0.0,
                gate=0.005, out=0.95, wah=(360.0, 1800.0, 1.25, 3.2, 4.6)),
    "phaser": dict(hp=100.0, lp=6500.0, drive=1.0, shape=None, bias=0.0,
                   gate=0.004, out=1.0, phaser=(0.4, 320.0, 1500.0)),
    "echo": dict(hp=100.0, lp=5200.0, drive=1.0, shape=None, bias=0.0,
                 gate=0.005, out=0.94, echo=(0.145, 0.35, 0.45)),
    "robot": dict(hp=120.0, lp=6000.0, drive=1.0, shape=None, bias=0.0,
                  gate=0.004, out=0.90, robot=(55.0, 0.78)),
    "flanger": dict(hp=100.0, lp=6200.0, drive=1.0, shape=None, bias=0.0,
                    gate=0.004, out=0.95,
                    flanger=(1.5, 4.0, 0.25, 0.45, 0.55)),
    "vibrato": dict(hp=100.0, lp=6500.0, drive=1.0, shape=None, bias=0.0,
                    gate=0.004, out=0.95, vibrato=(6.0, 2.5, 5.5)),
    "stutter": dict(hp=100.0, lp=6000.0, drive=1.0, shape=None, bias=0.0,
                    gate=0.004, out=0.95, stutter=(5, 10)),
    "cave": dict(hp=110.0, lp=4800.0, drive=1.0, shape=None, bias=0.0,
                 gate=0.005, out=1.5,
                 cave=((31.0, 47.0, 61.0, 83.0), 0.45, 0.55)),
    "alien": dict(hp=120.0, lp=6000.0, drive=1.0, shape=None, bias=0.0,
                  gate=0.004, out=0.92, alien=(300.0, 0.7, 0.72)),
    "sub_octave": dict(hp=90.0, lp=5000.0, drive=1.0, shape=None, bias=0.0,
                       gate=0.005, out=0.95, sub_octave=0.5),
    "envelope_wah": dict(hp=110.0, lp=5200.0, drive=1.0, shape=None, bias=0.0,
                         gate=0.004, out=0.90,
                         envelope_wah=(300.0, 1900.0, 3.0, 10.0)),
    "gate": dict(hp=100.0, lp=6000.0, drive=1.0, shape=None, bias=0.0,
                 gate=0.004, out=0.95, chop=(8.0, 0.06)),
    "reverse": dict(hp=100.0, lp=6000.0, drive=1.0, shape=None, bias=0.0,
                    gate=0.004, out=0.95, reverse=(5, 10)),
    "chorus": dict(hp=100.0, lp=6800.0, drive=1.0, shape=None, bias=0.0,
                   gate=0.004, out=0.92,
                   chorus3=((7.0, 9.0, 12.0), 0.35, 1.6, 0.5)),
    "vinyl": dict(hp=80.0, lp=5500.0, drive=1.0, shape=None, bias=0.0,
                  gate=0.005, out=0.98, vinyl=(0.6, 0.45, 0.30)),
    "body": dict(hp=90.0, lp=5200.0, drive=1.0, shape=None, bias=0.0,
                 gate=0.005, out=1.0, body=(110.0, 0.72, 0.55)),
    "shimmer": dict(hp=100.0, lp=6000.0, drive=1.0, shape=None, bias=0.0,
                    gate=0.004, out=0.95, shimmer=(14.0, 0.62)),
    "whisper": dict(hp=900.0, lp=7000.0, drive=1.0, shape=None, bias=0.0,
                    gate=0.003, out=1.30, whisper=(0.45, 0.50)),
    "growl": dict(hp=80.0, lp=6000.0, drive=2.2, shape="tanh", bias=0.04,
                  gate=0.005, out=0.95, growl=(450.0, 0.80)),
    "doubler": dict(hp=100.0, lp=6500.0, drive=1.0, shape=None, bias=0.0,
                    gate=0.004, out=0.95,
                    doubler=(22.0, 1.5, 0.33, 0.55)),
    # The modern pedals. A talkbox is a formant pushed up (Growl is the same
    # trick down), a vocoder replaces the excitation and keeps the envelopes,
    # and the rest are a production gesture each.
    "vocoder": dict(hp=90.0, lp=7500.0, drive=1.0, shape=None, bias=0.0,
                    gate=0.004, out=1.3, vocoder=(2.6, 0.9)),
    "talkbox": dict(hp=300.0, lp=7200.0, drive=3.4, shape="tanh", bias=0.06,
                    gate=0.006, out=0.72, formant=(2300.0, 1.1, 0.55, 1.9)),
    "hyperpop": dict(hp=80.0, lp=9000.0, drive=1.0, shape=None, bias=0.0,
                     gate=0.004, out=1.5, ott=(0.9, 0.35)),
    "tapestop": dict(hp=120.0, lp=6000.0, drive=1.0, shape=None, bias=0.0,
                     gate=0.004, out=0.95, tapestop=(0.55, 0.85)),
    "cloud": dict(hp=120.0, lp=5200.0, drive=1.0, shape=None, bias=0.0,
                  gate=0.005, out=1.3,
                  cloud=((95.0, 160.0, 240.0, 330.0), 0.11, 16.0, 0.62)),
    "air": dict(hp=150.0, lp=12000.0, drive=1.0, shape=None, bias=0.0,
                gate=0.004, out=0.95, air=(4200.0, 2.6, 0.45)),
    "pump": dict(hp=100.0, lp=6500.0, drive=1.0, shape=None, bias=0.0,
                 gate=0.004, out=0.95, pump=(128.0, 0.8, 0.22)),
    "freeze": dict(hp=110.0, lp=5200.0, drive=1.0, shape=None, bias=0.0,
                   gate=0.004, out=0.95, freeze=(8, 60)),
    "step": dict(hp=120.0, lp=6500.0, drive=1.0, shape=None, bias=0.0,
                 gate=0.004, out=1.35,
                 step=(7.0, (250.0, 900.0, 3000.0), 1.4)),
    "swell": dict(hp=110.0, lp=5500.0, drive=1.0, shape=None, bias=0.0,
                  gate=0.004, out=0.95, swell=(0.06, 1.10, 0.55, 0.9, 0.5)),
}

#: The loudest a pedal may hand over, in full scale. The PA and a cinema cabinet
#: both put the voice through effect sends *on top of* the dry voice -- the PA two
#: (its cabinet EQ and its compressor), a cinema room up to four (the room's
#: reverb, the cabinet's EQ, the same voice EQ and the compressor). A preamp
#: pushing the frame to 1.0 left that chain no headroom at all: the mixer clipped
#: on every syllable, heard as a buzz under the words and as a compressor that
#: worked so hard a tremolo arrived flattened. 0.7 is 3 dB of room for the chain
#: and still a loud pedal -- unity below the knee, so a quiet voice is untouched.
PEAK_CEILING = 0.70

#: The vocoder's six bands, in hertz. A channel vocoder is a bank of band-pass
#: filters on both sides: the voice's bands supply the envelopes, a fixed saw
#: supplies the excitation, and the result keeps the words and loses the voice.
#: Six is the narrowest bank that still separates vowels; every extra band is
#: another per-sample filter on the talker's own thread.
VOCODER_BANDS = (180.0, 360.0, 700.0, 1300.0, 2400.0, 4200.0)

#: The saw the vocoder speaks through, and each band's own level on it --
#: measured once per process, because the carrier is a fixed waveform and its
#: band levels are constants. Dividing the voice's envelope by them is what
#: keeps a band with a quiet harmonic as loud as one with a strong harmonic.
VOCODER_CARRIER_HZ = 110.0
_vocoder_carrier_levels = None

#: The step filter's pattern: which of the three bands is heard on each
#: sixteenth. Eight steps is a bar's worth of movement, not a melody.
STEP_PATTERN = (0, 0, 1, 2, 1, 0, 2, 1)


def _carrier_band_levels(q):
    """The carrier's RMS in each vocoder band, measured once and cached."""
    global _vocoder_carrier_levels
    if _vocoder_carrier_levels is not None and _vocoder_carrier_levels[0] == q:
        return _vocoder_carrier_levels[1]
    frames = 3
    samples = frames * 960
    cycle = np.arange(samples) / SAMPLE_RATE * VOCODER_CARRIER_HZ
    carrier = (2.0 * (cycle % 1.0) - 1.0).astype(np.float32)
    levels = []
    for hz in VOCODER_BANDS:
        band = _Biquad.bandpass(hz, q)
        shaped = np.concatenate([
            band.process(carrier[start:start + 960])
            for start in range(0, samples, 960)])
        # The last frame only: the filters have settled by then.
        levels.append(max(1e-3, float(np.sqrt(np.mean(
            np.square(shaped[-960:]))))))
    _vocoder_carrier_levels = (q, tuple(levels))
    return _vocoder_carrier_levels[1]

#: The gate's two speeds, per frame. Opening is quick (a word must not lose its
#: first consonant); closing is slow enough that a breath holds the gate open
#: rather than chopping the tail of a sentence into pieces.
GATE_OPEN = 0.5
GATE_CLOSE = 0.12


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
        self._chorus = float(spec.get("chorus", 0.0))
        self._ring = None
        self._ring_pos = 0
        self._phase = 0.0
        if self._chorus:
            self._ring = np.zeros(4096, dtype=np.float32)
        # A bitcrusher holds one sample for several and rounds what it holds.
        self._crush = spec.get("crush")
        self._crush_value = 0.0
        self._crush_phase = 0.0
        # The pedals below are one block each, off unless their spec names
        # them, so a preset pays only for what it uses. One LFO is shared by
        # whichever sweep pedal a preset has (only one preset runs at a time).
        self._octave = float(spec.get("octave", 0.0))
        self._tremolo = spec.get("tremolo")
        self._wah = spec.get("wah")
        self._wah_filter = (_Biquad.bandpass(self._wah[0], self._wah[3])
                            if self._wah else None)
        self._phaser = spec.get("phaser")
        self._phaser_state = [[0.0, 0.0], [0.0, 0.0]]
        self._echo = spec.get("echo")
        if self._echo:
            seconds = float(self._echo[0])
            self._echo_ring = np.zeros(
                int(round(seconds * SAMPLE_RATE)) + 4096, dtype=np.float32)
        else:
            self._echo_ring = None
        self._echo_pos = 0
        self._robot = spec.get("robot")
        self._robot_phase = 0.0
        self._lfo = 0.0
        self._flanger = spec.get("flanger")
        self._flanger_ring = (np.zeros(4096, dtype=np.float32)
                              if self._flanger else None)
        self._flanger_pos = 0
        self._vibrato = spec.get("vibrato")
        self._vibrato_ring = (np.zeros(4096, dtype=np.float32)
                              if self._vibrato else None)
        self._vibrato_pos = 0
        self._stutter = spec.get("stutter")
        self._stutter_history = collections.deque(
            maxlen=int(self._stutter[0]) if self._stutter else 0)
        self._stutter_loop = None
        self._stutter_pos = 0
        self._cave = spec.get("cave")
        if self._cave:
            longest = max(float(tap) for tap in self._cave[0])
            self._cave_ring = np.zeros(
                int(round(longest * SAMPLE_RATE / 1000.0)) + 4096,
                dtype=np.float32)
        else:
            self._cave_ring = None
        self._cave_pos = 0
        self._alien = spec.get("alien")
        self._alien_phase = 0.0
        self._sub_octave = spec.get("sub_octave")
        self._divider_filter = (_Fir(_lowpass(700.0, 129))
                                if self._sub_octave else None)
        self._divider_state = 0
        self._divider_negative = False
        self._divider_level = 0.0
        self._envelope_wah = spec.get("envelope_wah")
        self._env_filter = (_Biquad.bandpass(self._envelope_wah[0],
                                            self._envelope_wah[2])
                            if self._envelope_wah else None)
        self._env = 0.0
        self._gate_pedal = spec.get("chop")
        self._gate_fir = (_Fir(np.ones(48, dtype=np.float32) / 48.0)
                          if self._gate_pedal else None)
        self._reverse = spec.get("reverse")
        self._reverse_history = collections.deque(
            maxlen=int(self._reverse[0]) if self._reverse else 0)
        self._reverse_loop = None
        self._reverse_pos = 0
        self._chorus3 = spec.get("chorus3")
        self._chorus_ring = (np.zeros(4096, dtype=np.float32)
                            if self._chorus3 else None)
        self._chorus_pos = 0
        # Vinyl: a short delay whose length drifts (a tape's wow) plus the odd
        # click. Doubler: one copy at 22 ms, almost still.
        self._vinyl = spec.get("vinyl")
        self._vinyl_ring = (np.zeros(4096, dtype=np.float32)
                            if self._vinyl else None)
        self._vinyl_pos = 0
        self._vinyl_pop = (_Fir(_tick(7)) if self._vinyl else None)
        self._vinyl_level = 0.0
        self._rng = np.random.default_rng(0x5EED)
        self._doubler = spec.get("doubler")
        self._doubler_ring = (np.zeros(4096, dtype=np.float32)
                              if self._doubler else None)
        self._doubler_pos = 0
        # Wood Body: one resonant comb, the air of a guitar's box.
        self._body = spec.get("body")
        self._body_ring = (np.zeros(8192, dtype=np.float32)
                           if self._body else None)
        self._body_pos = 0
        # Shimmer: a single-sideband shifter. The Hilbert branch is a short FIR
        # and the real branch is delayed to meet it, so the sideband that is
        # cancelled stays cancelled at the top of the voice.
        self._shimmer = spec.get("shimmer")
        self._shimmer_phase = 0.0
        self._shimmer_delay = (np.zeros(4096, dtype=np.float32)
                               if self._shimmer else None)
        self._shimmer_pos = 0
        self._shimmer_fir = (_Fir(_hilbert(127)) if self._shimmer else None)
        # Whisper: filtered noise shaped by the voice's own envelope.
        self._whisper = spec.get("whisper")
        self._whisper_noise = (_Biquad.bandpass(2500.0, 0.8)
                               if self._whisper else None)
        self._whisper_level = 0.0
        # Growl: two formants moved down, so the voice sits in its chest.
        self._growl = spec.get("growl")
        self._growl_filter = (_Fir(_lowpass(self._growl[0], 129))
                              if self._growl else None)
        # The modern pedals: a vocoder bank, a raised formant, an exciter, an
        # up-and-down compressor, a varispeed delay, a cloud, a beat duck, a
        # held slice, a stepped band-bank and a backwards ghost.
        self._vocoder = spec.get("vocoder")
        self._vocoder_filters = ([_Biquad.bandpass(hz, self._vocoder[0])
                                  for hz in VOCODER_BANDS]
                                 if self._vocoder else None)
        self._vocoder_envelopes = [0.0] * len(VOCODER_BANDS)
        self._vocoder_gains = [0.0] * len(VOCODER_BANDS)
        self._vocoder_cycle = 0.0
        self._formant = spec.get("formant")
        self._formant_filter = (_Biquad.bandpass(self._formant[0],
                                                 self._formant[1])
                                if self._formant else None)
        self._ott = spec.get("ott")
        self._ott_env = 0.0
        self._tapestop = spec.get("tapestop")
        self._tapestop_ring = (np.zeros(32768, dtype=np.float32)
                               if self._tapestop else None)
        self._tapestop_pos = 0
        self._tapestop_clock = 0.0
        self._cloud = spec.get("cloud")
        self._cloud_ring = (np.zeros(32768, dtype=np.float32)
                            if self._cloud else None)
        self._cloud_pos = 0
        self._cloud_phase = 0.0
        self._air = spec.get("air")
        self._air_filter = (_Biquad.highpass(self._air[0], 1.1)
                            if self._air else None)
        self._pump = spec.get("pump")
        self._pump_phase = 0.0
        self._freeze = spec.get("freeze")
        self._freeze_history = collections.deque(
            maxlen=int(self._freeze[0]) if self._freeze else 0)
        self._freeze_loop = None
        self._freeze_pos = 0
        self._freeze_counter = 0
        self._freeze_seam = None
        self._step = spec.get("step")
        self._step_filters = ([_Biquad.bandpass(hz, self._step[2])
                               for hz in self._step[1]]
                              if self._step else None)
        self._step_index = 0
        self._step_counter = 0
        self._step_band = 0
        self._swell = spec.get("swell")
        self._swell_ring = (np.zeros(65536, dtype=np.float32)
                            if self._swell else None)
        self._swell_pos = 0
        self._swell_clock = 0.0

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

    # ------------------------------------------------- the one-block pedals
    def _octave_block(self, block):
        """The rectified copy that adds the octave, before the DC block."""
        if not self._octave:
            return block
        return block * (1.0 - self._octave) + np.abs(block) * self._octave

    def _tremolo_block(self, block):
        if not self._tremolo:
            return block
        rate, depth = self._tremolo
        positions = np.arange(block.size)
        phase = self._lfo + 2.0 * np.pi * rate * positions / SAMPLE_RATE
        # Depth 0.85 reaches 0.15 at the bottom: a pulse, not a mute.
        gain = (1.0 - depth) + depth * (0.5 + 0.5 * np.sin(phase))
        self._lfo = ((self._lfo
                      + 2.0 * np.pi * rate * block.size / SAMPLE_RATE)
                     % (2.0 * np.pi))
        return block * gain

    def _wah_block(self, block):
        if not self._wah:
            return block
        low, high, rate, q, boost = self._wah
        sweep = 0.5 - 0.5 * np.cos(self._lfo)
        self._wah_filter.retune_bandpass(low + (high - low) * sweep, q)
        self._lfo = ((self._lfo
                      + 2.0 * np.pi * rate * block.size / SAMPLE_RATE)
                     % (2.0 * np.pi))
        return self._wah_filter.process(block) * boost

    def _phaser_block(self, block):
        """Two swept all-pass stages mixed with the dry voice.

        Two, not four: each stage is a sample loop in Python, and two already
        put a pair of moving notches in the voice -- four cost twice the frame
        time for a shade more depth, which is the wrong trade on a talker's own
        machine.
        """
        if not self._phaser:
            return block
        rate, low, high = self._phaser
        sweep = 0.5 - 0.5 * np.cos(self._lfo)
        centre = low + (high - low) * sweep
        self._lfo = ((self._lfo
                      + 2.0 * np.pi * rate * block.size / SAMPLE_RATE)
                     % (2.0 * np.pi))
        coefficients = []
        for multiplier in (1.0, 1.6):
            hz = min(centre * multiplier, SAMPLE_RATE * 0.45)
            tangent = float(np.tan(np.pi * hz / SAMPLE_RATE))
            coefficients.append((1.0 - tangent) / (1.0 + tangent))
        state = self._phaser_state
        samples = block.tolist()
        out = [0.0] * len(samples)
        for index, dry in enumerate(samples):
            sample = dry
            for stage in range(2):
                a = coefficients[stage]
                stage_state = state[stage]
                y = a * sample + stage_state[0] - a * stage_state[1]
                stage_state[0] = sample
                stage_state[1] = y
                sample = y
            out[index] = 0.5 * dry + 0.5 * sample
        return np.asarray(out, dtype=np.float32)

    def _echo_block(self, block):
        """A single tap with feedback; the delay is longer than a frame."""
        if not self._echo:
            return block
        seconds, feedback, mix = self._echo
        delay = int(round(float(seconds) * SAMPLE_RATE))
        ring, size = self._echo_ring, self._echo_ring.size
        positions = np.arange(block.size)
        read = (self._echo_pos + positions - delay) % size
        write = (self._echo_pos + positions) % size
        delayed = ring[read].copy()
        ring[write] = block + float(feedback) * delayed
        self._echo_pos = int((self._echo_pos + block.size) % size)
        return block * (1.0 - mix) + delayed * mix

    def _robot_block(self, block):
        if not self._robot:
            return block
        hz, mix = self._robot
        phase = (self._robot_phase
                 + 2.0 * np.pi * hz * np.arange(block.size) / SAMPLE_RATE)
        carrier = np.sin(phase)
        self._robot_phase = ((self._robot_phase
                              + 2.0 * np.pi * hz * block.size / SAMPLE_RATE)
                             % (2.0 * np.pi))
        return block * (1.0 - mix) + block * carrier * mix

    def _flanger_block(self, block):
        """A short swept delay that feeds back -- the jet sound.

        One sample at a time, unlike the pure delays: the ring is fed the
        output, and the delay (1.5-5.5 ms) is shorter than a frame, so the
        sample being read can have been written by this very block. Reading a
        whole block first and writing it afterwards -- what this used to do --
        means every read past the delay lands on the *previous* wrap of the
        ring, 4096 samples old: a step at every 20 ms boundary, heard as a
        50 Hz buzz under the jet.
        """
        if not self._flanger:
            return block
        base, sweep, rate, feedback, mix = self._flanger
        ring, size = self._flanger_ring, self._flanger_ring.size
        start = self._flanger_pos
        samples = block.tolist()
        out = [0.0] * len(samples)
        step = 2.0 * np.pi * rate / SAMPLE_RATE
        angle = self._lfo
        gain = 1.0 - float(mix)
        for index, dry in enumerate(samples):
            delay = ((base + sweep * (0.5 + 0.5 * float(np.sin(angle))))
                     * SAMPLE_RATE / 1000.0)
            read = start + index - delay
            lower = int(np.floor(read))
            fraction = read - lower
            delayed = (ring[lower % size] * (1.0 - fraction)
                       + ring[(lower + 1) % size] * fraction)
            ring[(start + index) % size] = dry + float(feedback) * delayed
            out[index] = dry * gain + delayed * float(mix)
            angle += step
        self._flanger_pos = int((start + block.size) % size)
        self._lfo = float(angle % (2.0 * np.pi))
        return np.asarray(out, dtype=np.float32)

    def _delay_frame(self, block, ring, start, delay_samples):
        """Write the frame, then read the delay -- the correct order.

        A delay shorter than a frame (every wobbling pedal here) has to see the
        samples this frame has just written: read-before-write makes the read
        fall on the previous wrap of the ring for every sample past the delay, a
        step at the frame boundary. Writing first is exact and still
        vectorised; the ring must be longer than the frame plus the longest
        delay, which is why they are powers of two far above both.
        """
        size = ring.size
        positions = np.arange(block.size)
        ring[(start + positions) % size] = block
        read = (start + positions - delay_samples) % size
        lower = np.floor(read).astype(np.int64)
        fraction = (read - lower).astype(np.float32)
        delayed = (ring[lower % size] * (1.0 - fraction)
                   + ring[(lower + 1) % size] * fraction)
        return delayed, int((start + block.size) % size)

    def _vibrato_block(self, block):
        """A delay that only wobbles, fully wet: pitch, not an echo."""
        if not self._vibrato:
            return block
        base, sweep, rate = self._vibrato
        positions = np.arange(block.size)
        phase = self._lfo + 2.0 * np.pi * rate * positions / SAMPLE_RATE
        delay = (base + sweep * np.sin(phase)) * SAMPLE_RATE / 1000.0
        delayed, self._vibrato_pos = self._delay_frame(
            block, self._vibrato_ring, self._vibrato_pos, delay)
        self._lfo = ((self._lfo
                      + 2.0 * np.pi * rate * block.size / SAMPLE_RATE)
                     % (2.0 * np.pi))
        return delayed

    def _stutter_block(self, block):
        """Repeat the last tenth of a second, five times a second.

        Frames are the unit because the effect is a rhythm, not a sample
        operation: every period the last `slice` frames are captured and played
        back, then the voice carries on. The first slice waits until there is
        something to repeat, so a press never opens with silence.
        """
        if not self._stutter:
            return block
        slice_frames, period_frames = self._stutter
        history = self._stutter_history
        if len(history) < slice_frames:
            history.append(block.copy())
            return block
        position = self._stutter_pos
        if position == 0:
            self._stutter_loop = list(history)
        out = block
        looped = (self._stutter_loop[position]
                  if position < slice_frames and self._stutter_loop else None)
        if looped is not None and looped.size == block.size:
            out = looped
        history.append(block.copy())
        self._stutter_pos = (position + 1) % period_frames
        return out

    @staticmethod
    def _multi_tap(block, taps, gains, feedback, mix, ring, position):
        """A bank of taps feeding one another; returns the frame and the next
        write position. Delays are longer than a frame, which is what makes
        read-before-write safe and the whole thing vectorised."""
        size = ring.size
        positions = np.arange(block.size)
        wet = np.zeros(block.size, dtype=np.float32)
        for tap_ms, gain in zip(taps, gains):
            delay = int(round(float(tap_ms) * SAMPLE_RATE / 1000.0))
            read = (position + positions - delay) % size
            wet += ring[read] * gain
        ring[(position + positions) % size] = (block
                                              + float(feedback) * wet)
        return (block * (1.0 - mix) + wet * mix,
                int((position + block.size) % size))

    def _cave_block(self, block):
        """Four taps that feed one another: a room rather than one repeat."""
        if not self._cave:
            return block
        taps, feedback, mix = self._cave
        out, self._cave_pos = self._multi_tap(
            block, taps, (0.7, 0.55, 0.4, 0.3), feedback, mix,
            self._cave_ring, self._cave_pos)
        return out

    def _sub_octave_block(self, block):
        """An octave down with a divider, not a pitch shifter.

        A square wave toggles at every positive-going zero crossing of the
        (low-passed) voice, so its period is two of the voice's cycles: an
        octave below. This is the oldest sub-octave pedal there is, and it is
        cheap and has no latency -- at the cost of a buzzy, square copy. The
        700 Hz filter before the crossing detector keeps a formant from adding
        crossings of its own and turning the note into noise.
        """
        if not self._sub_octave:
            return block
        mix = float(self._sub_octave)
        smooth = self._divider_filter.process(block)
        negative = smooth < 0.0
        rise = np.empty(block.size, dtype=bool)
        rise[0] = self._divider_negative and not bool(negative[0])
        rise[1:] = negative[:-1] & ~negative[1:]
        toggles = np.cumsum(rise) % 2
        levels = ((toggles ^ self._divider_state).astype(np.float32)
                  * 2.0 - 1.0)
        self._divider_state = int(self._divider_state
                                  ^ int(toggles[-1]))
        self._divider_negative = bool(negative[-1])
        # A square's own DC must not make silence audible: the divider fades
        # with the voice, and a frame that was never voiced stays exactly zero.
        level = (float(np.sqrt(np.mean(np.square(smooth))))
                 if smooth.size else 0.0)
        self._divider_level += (min(1.0, level / 0.02)
                                - self._divider_level) * 0.5
        return block * (1.0 - mix) + levels * (mix * self._divider_level)

    def _envelope_wah_block(self, block):
        """A band that follows the voice's level, not a clock."""
        if not self._envelope_wah:
            return block
        low, high, q, boost = self._envelope_wah
        level = (float(np.sqrt(np.mean(np.square(block))))
                 if block.size else 0.0)
        target = min(1.0, level / 0.12)
        # Opening is quick (a word pushes the filter up); closing is slow, so a
        # gap between words does not slam it shut and open on the next one.
        speed = 0.45 if target > self._env else 0.08
        self._env += (target - self._env) * speed
        self._env_filter.retune_bandpass(low + (high - low) * self._env, q)
        return self._env_filter.process(block) * boost

    def _gate_pedal_block(self, block):
        """A hard 8 Hz chop, smoothed over a millisecond so it is not a click."""
        if not self._gate_pedal:
            return block
        rate, depth = self._gate_pedal
        positions = np.arange(block.size)
        phase = self._lfo + 2.0 * np.pi * rate * positions / SAMPLE_RATE
        raw = np.where(np.sin(phase) >= 0.0, 1.0, depth).astype(np.float32)
        gain = self._gate_fir.process(raw)
        self._lfo = ((self._lfo
                      + 2.0 * np.pi * rate * block.size / SAMPLE_RATE)
                     % (2.0 * np.pi))
        return block * gain

    def _reverse_block(self, block):
        """Play the last slice backwards, then let the voice carry on."""
        if not self._reverse:
            return block
        slice_frames, period_frames = self._reverse
        history = self._reverse_history
        if len(history) < slice_frames:
            history.append(block.copy())
            return block
        position = self._reverse_pos
        if position == 0:
            self._reverse_loop = [np.ascontiguousarray(frame[::-1])
                                  for frame in reversed(list(history))]
        out = block
        looped = (self._reverse_loop[position]
                  if position < slice_frames and self._reverse_loop else None)
        if looped is not None and looped.size == block.size:
            out = looped
        history.append(block.copy())
        self._reverse_pos = (position + 1) % period_frames
        return out

    def _vinyl_block(self, block):
        """A record: a delay that drifts a few cents, and the odd click.

        The drift is a delay of 2.5 ms whose length breathes by a third of a
        millisecond at 0.6 Hz -- about 3 cents of wow, the amount a turntable's
        motor gives a sustained note. The crackle is sparse by construction
        (four sigma of white noise) and smoothed to a millisecond so a click is
        a tick rather than a sample-wide snap.
        """
        if not self._vinyl:
            return block
        rate, depth_ms, crackle = self._vinyl
        positions = np.arange(block.size)
        phase = self._lfo + 2.0 * np.pi * rate * positions / SAMPLE_RATE
        delay = ((2.5 + 0.5 * depth_ms * (1.0 + np.sin(phase)))
                 * SAMPLE_RATE / 1000.0)
        delayed, self._vinyl_pos = self._delay_frame(
            block, self._vinyl_ring, self._vinyl_pos, delay)
        self._lfo = ((self._lfo
                      + 2.0 * np.pi * rate * block.size / SAMPLE_RATE)
                     % (2.0 * np.pi))
        noise = self._rng.standard_normal(block.size).astype(np.float32)
        pops = (np.abs(noise) > 3.0).astype(np.float32) * np.sign(noise)
        # The crackle rides the voice: an empty frame stays exactly empty, and
        # silence never turns into a hiss of its own.
        level = (float(np.sqrt(np.mean(np.square(block))))
                 if block.size else 0.0)
        self._vinyl_level += (min(1.0, level / 0.03)
                              - self._vinyl_level) * 0.5
        return delayed + (self._vinyl_pop.process(pops) * float(crackle)
                          * self._vinyl_level)

    def _body_block(self, block):
        """A voice inside a guitar's box: one resonant comb.

        `y[n] = x[n] + g*y[n-D]` with D the period of 110 Hz is the shortest
        honest model of a wooden body -- it puts a resonance on a low A and its
        harmonics, so the voice gains a hollow knock rather than a reverb tail.
        """
        if not self._body:
            return block
        hz, feedback, mix = self._body
        ring, size = self._body_ring, self._body_ring.size
        delay = max(1, int(round(SAMPLE_RATE / float(hz))))
        start = self._body_pos
        samples = block.tolist()
        out = [0.0] * len(samples)
        gain = 1.0 - float(mix)
        for index, dry in enumerate(samples):
            wet = dry + float(feedback) * ring[(start + index - delay) % size]
            ring[(start + index) % size] = wet
            out[index] = dry * gain + wet * float(mix)
        self._body_pos = int((start + block.size) % size)
        return np.asarray(out, dtype=np.float32)

    def _shimmer_block(self, block):
        """A single-sideband frequency shifter: every partial moves by 14 Hz.

        Unlike the ring modulator (which mirrors the spectrum around a carrier
        and so sounds robotic), shifting *up* every partial by the same number
        of hertz keeps the voice's own shape and makes it glide -- the sound a
        ring-mod with a moving carrier only imitates. The quadrature branch is
        a 31-tap Hilbert FIR; the real branch waits the FIR's own delay so the
        two still cancel the lower sideband.
        """
        if not self._shimmer:
            return block
        hz, mix = self._shimmer
        quadrature = self._shimmer_fir.process(block)
        # `_Fir` correlates with its kernel, and a Hilbert kernel is
        # antisymmetric: what comes back is minus the 90-degree copy, aligned
        # with the frame. Pairing it with the voice delayed by half the kernel
        # (its own group delay) is what makes the pair add up to the *upper*
        # sideband only; the sign here is the difference between a shimmer and
        # a plain ring modulator.
        delay = (self._shimmer_fir.kernel.size - 1) // 2
        in_phase, self._shimmer_pos = self._delay_frame(
            block, self._shimmer_delay, self._shimmer_pos, delay)
        positions = np.arange(block.size)
        phase = (self._shimmer_phase
                 + 2.0 * np.pi * hz * positions / SAMPLE_RATE)
        shifted = (in_phase * np.cos(phase)
                   - quadrature * np.sin(phase)).astype(np.float32)
        self._shimmer_phase = ((self._shimmer_phase
                                + 2.0 * np.pi * hz * block.size / SAMPLE_RATE)
                               % (2.0 * np.pi))
        return block * (1.0 - float(mix)) + shifted * float(mix)

    def _whisper_block(self, block):
        """A hush: the voice thinned, with breath riding its own envelope.

        A whisper has no pitch, only noise shaped by the mouth -- so the noise
        here is filtered noise multiplied by the frame's own level, added under
        a voice that has already lost everything below 900 Hz. The envelope is
        smoothed over two frames, because a per-frame gain would chop the noise
        into 20 ms steps.
        """
        if not self._whisper:
            return block
        noise_mix, voice_mix = self._whisper
        level = (float(np.sqrt(np.mean(np.square(block))))
                 if block.size else 0.0)
        self._whisper_level += (level - self._whisper_level) * 0.5
        noise = self._whisper_noise.process(
            self._rng.standard_normal(block.size).astype(np.float32))
        breath = noise * min(1.0, self._whisper_level / 0.12)
        return block * float(voice_mix) + breath * float(noise_mix)

    def _growl_block(self, block):
        """Everything below 450 Hz lifted into a driven chest.

        A band-pass at two fixed formants made a voice quieter and hollow; what
        makes a growl is weight, not a notch. The low half of the voice is mixed
        back 1.6 times over the dry one, and the pre-gain in the chain has
        already made it dirty, so the result is a bigger body rather than a
        filter.
        """
        if not self._growl:
            return block
        _hz, mix = self._growl
        dark = self._growl_filter.process(block)
        return block * (1.0 - float(mix)) + dark * float(mix) * 1.6

    def _doubler_block(self, block):
        """One copy, 22 ms behind and barely moving: a double-tracked voice."""
        if not self._doubler:
            return block
        base, drift, rate, mix = self._doubler
        positions = np.arange(block.size)
        phase = self._lfo + 2.0 * np.pi * rate * positions / SAMPLE_RATE
        delay = (base + drift * 0.5 * (1.0 + np.sin(phase))) \
            * SAMPLE_RATE / 1000.0
        doubled, self._doubler_pos = self._delay_frame(
            block, self._doubler_ring, self._doubler_pos, delay)
        self._lfo = ((self._lfo
                      + 2.0 * np.pi * rate * block.size / SAMPLE_RATE)
                     % (2.0 * np.pi))
        return block * (1.0 - float(mix)) + doubled * float(mix)

    def _chorus3_block(self, block):
        """Three drifting copies, one LFO at three phase offsets."""
        if not self._chorus3:
            return block
        bases, rate, sweep, mix = self._chorus3
        ring, size = self._chorus_ring, self._chorus_ring.size
        positions = np.arange(block.size)
        start = self._chorus_pos
        ring[(start + positions) % size] = block
        wet = np.zeros(block.size, dtype=np.float32)
        for index, base in enumerate(bases):
            phase = (self._lfo + index * 2.1
                     + 2.0 * np.pi * rate * positions / SAMPLE_RATE)
            delay = (base + sweep * np.sin(phase)) * SAMPLE_RATE / 1000.0
            read = (start + positions - delay) % size
            lower = np.floor(read).astype(np.int64)
            fraction = (read - lower).astype(np.float32)
            wet += (ring[lower % size] * (1.0 - fraction)
                    + ring[(lower + 1) % size] * fraction)
        self._chorus_pos = int((start + block.size) % size)
        self._lfo = ((self._lfo
                      + 2.0 * np.pi * rate * block.size / SAMPLE_RATE)
                     % (2.0 * np.pi))
        return block * (1.0 - mix) + (wet / 3.0) * mix

    def _alien_block(self, block):
        """Ring modulation with a carrier that slides, not a fixed pitch."""
        if not self._alien:
            return block
        hz, rate, mix = self._alien
        centre = hz * (1.0 + 0.5 * np.sin(self._lfo))
        phase = (self._alien_phase
                 + 2.0 * np.pi * centre * np.arange(block.size) / SAMPLE_RATE)
        carrier = np.sin(phase)
        self._alien_phase = ((self._alien_phase
                              + 2.0 * np.pi * centre * block.size
                              / SAMPLE_RATE) % (2.0 * np.pi))
        self._lfo = ((self._lfo
                      + 2.0 * np.pi * rate * block.size / SAMPLE_RATE)
                     % (2.0 * np.pi))
        return block * (1.0 - mix) + block * carrier * mix

    # -------------------------------------------------------------- the modern
    def _air_block(self, block):
        """An exciter: the top filtered out, driven, and added back.

        A voice recorded close has nothing above four kilohertz to work with,
        and an EQ can only take away from what is there. Driving the top makes
        new harmonics above it, which is why this opens a dull voice instead of
        merely brightening it.
        """
        if not self._air:
            return block
        _hz, drive, mix = self._air
        top = self._air_filter.process(block)
        return block + np.tanh(top * float(drive)) * float(mix)

    def _formant_block(self, block):
        """One band lifted and driven: a talkbox's tube, a chest's weight."""
        if not self._formant:
            return block
        _hz, _q, mix, gain = self._formant
        band = self._formant_filter.process(block)
        return block * (1.0 - float(mix)) + band * (float(mix) * float(gain))

    def _vocoder_block(self, block):
        """The voice's six band envelopes, spoken by a saw instead of by you.

        A channel vocoder does not shift pitch: it keeps *what* the mouth did
        and throws away *what* made the sound, then rebuilds it from a fixed
        saw through the same six bands. That is why the words survive and the
        voice does not. The envelopes are read as one spectrum per frame (six
        more filters on the voice would be six more per-sample loops), and each
        band's gain ramps across the frame, so a syllable's edge is not a step
        at a frame boundary.
        """
        if not self._vocoder:
            return block
        q, mix = self._vocoder
        count = block.size
        positions = np.arange(count)
        cycle = ((self._vocoder_cycle
                  + VOCODER_CARRIER_HZ * positions / SAMPLE_RATE) % 1.0)
        carrier = (2.0 * cycle - 1.0).astype(np.float32)
        self._vocoder_cycle = ((self._vocoder_cycle
                                + VOCODER_CARRIER_HZ * count / SAMPLE_RATE)
                               % 1.0)
        spectrum = np.abs(np.fft.rfft(block)) ** 2
        bin_hz = SAMPLE_RATE / float(count)
        levels = _carrier_band_levels(q)
        half = 2.0 ** 0.35
        wet = np.zeros(count, dtype=np.float32)
        for index, hz in enumerate(VOCODER_BANDS):
            first = max(1, int(hz / half / bin_hz))
            last = max(first + 1, int(hz * half / bin_hz))
            energy = float(spectrum[first:last].sum()) * 2.0 / (count * count)
            wanted = float(np.sqrt(energy))
            envelope = self._vocoder_envelopes[index]
            # Up fast, down slowly: a consonant is an envelope step and a vowel
            # is a steady band, so only the release may be slow.
            envelope = (wanted if wanted > envelope
                        else envelope + (wanted - envelope) * 0.45)
            self._vocoder_envelopes[index] = envelope
            gain = min(6.0, envelope / levels[index])
            band = self._vocoder_filters[index].process(carrier)
            wet += band * np.linspace(self._vocoder_gains[index], gain, count,
                                      endpoint=False)
            self._vocoder_gains[index] = gain
        return block * (1.0 - float(mix)) + wet * float(mix)

    def _ott_block(self, block):
        """A pop vocal's loudness: quiet lifted, loud held, no dials.

        Per sample, not per frame: a gain that moves at frame rate is a 50 Hz
        amplitude modulation under the words, the same buzz the wobbling delays
        had. The follower's attack is instant and its release is about forty
        milliseconds, which is what makes a compressor sound like a compressor.
        """
        if not self._ott:
            return block
        upward, downward = self._ott
        target = 0.05
        samples = block.tolist()
        out = [0.0] * len(samples)
        env = self._ott_env
        for index, sample in enumerate(samples):
            level = sample if sample >= 0.0 else -sample
            env = level if level > env else env + (level - env) * 0.0018
            if env > 1e-4:
                if env > target:
                    gain = (target / env) ** downward
                else:
                    gain = (target / env) ** upward
                if gain > 4.0:
                    gain = 4.0
            else:
                gain = 1.0
            out[index] = sample * gain
        self._ott_env = env
        return np.asarray(out, dtype=np.float32)

    def _pump_block(self, block):
        """A four-to-the-floor duck: the voice steps back into every beat."""
        if not self._pump:
            return block
        bpm, depth, release = self._pump
        beats = float(bpm) / 60.0
        positions = np.arange(block.size)
        phase = (self._pump_phase + beats * positions / SAMPLE_RATE) % 1.0
        gain = 1.0 - float(depth) * np.exp(-phase / float(release))
        self._pump_phase = float((self._pump_phase
                                  + beats * block.size / SAMPLE_RATE) % 1.0)
        return block * gain.astype(np.float32)

    def _step_block(self, block):
        """Three bands, one heard at a time, the choice on sixteenths."""
        if not self._step:
            return block
        rate, _bands, _q = self._step
        step_frames = max(1, int(round(1.0 / (float(rate) * 0.020))))
        bands = [filt.process(block) for filt in self._step_filters]
        self._step_counter += 1
        if self._step_counter >= step_frames:
            self._step_counter = 0
            self._step_index += 1
        wanted = STEP_PATTERN[self._step_index % len(STEP_PATTERN)]
        previous = self._step_band
        if wanted == previous:
            return bands[previous]
        # The band changes between frames, so the frame it changes in
        # crossfades from the old band into the new one: a hard switch between
        # two filters is a step, and a step is a click.
        ramp = np.linspace(0.0, 1.0, block.size, endpoint=False,
                           dtype=np.float32)
        self._step_band = wanted
        return bands[previous] * (1.0 - ramp) + bands[wanted] * ramp

    def _cloud_block(self, block):
        """Four drifting copies of the last third of a second."""
        if not self._cloud:
            return block
        taps, rate, depth, mix = self._cloud
        ring, size = self._cloud_ring, self._cloud_ring.size
        positions = np.arange(block.size)
        start = self._cloud_pos
        ring[(start + positions) % size] = block
        wet = np.zeros(block.size, dtype=np.float32)
        for index, tap in enumerate(taps):
            phase = (self._cloud_phase + index * 1.7
                     + 2.0 * np.pi * rate * positions / SAMPLE_RATE)
            delay = ((float(tap) + float(depth) * np.sin(phase))
                     * SAMPLE_RATE / 1000.0)
            read = (start + positions - delay) % size
            lower = np.floor(read).astype(np.int64)
            fraction = (read - lower).astype(np.float32)
            copy = (ring[lower % size] * (1.0 - fraction)
                    + ring[(lower + 1) % size] * fraction)
            # Each tap swells and fades at its own speed, so the haze moves
            # instead of being four fixed echoes.
            wet += copy * (0.55 + 0.45 * np.sin(phase * 0.5))
        self._cloud_pos = int((start + block.size) % size)
        self._cloud_phase = ((self._cloud_phase
                              + 2.0 * np.pi * rate * block.size / SAMPLE_RATE)
                             % (2.0 * np.pi))
        return block * (1.0 - float(mix)) + (wet / 4.0) * float(mix)

    def _tapestop_block(self, block):
        """The head slows to a stop, then the tape starts again.

        A varispeed: the read head runs at a rate that falls to zero, so the
        pitch falls with it. The gain is zero while the head resets to the live
        sample, which is what keeps the jump back inaudible; the ring is
        written all the while, so the voice is never lost.
        """
        if not self._tapestop:
            return block
        hold, fall = self._tapestop
        count = block.size
        positions = np.arange(count)
        cycle = float(hold) + float(fall)
        phase = np.mod(self._tapestop_clock + positions / SAMPLE_RATE, cycle)
        progress = np.clip((phase - float(hold)) / float(fall), 0.0, 1.0)
        # A 20 ms delay through the hold, then the delay grows as p-squared, so
        # the playback rate (1 - d(delay)/dt) reaches exactly zero at its end.
        delay = (0.020 + (float(fall) * 0.5) * progress * progress) * SAMPLE_RATE
        fade_in = np.clip(phase / 0.12, 0.0, 1.0)
        fade_out = np.clip((1.0 - progress) / 0.20, 0.0, 1.0)
        self._tapestop_clock = float((self._tapestop_clock
                                      + count / SAMPLE_RATE) % cycle)
        slowed, self._tapestop_pos = self._delay_frame(
            block, self._tapestop_ring, self._tapestop_pos, delay)
        return slowed * np.minimum(fade_in, fade_out)

    def _swell_block(self, block):
        """A backwards ghost of the last word, growing under this one.

        The read head walks backwards (the delay grows faster than real time),
        so what is heard under the voice is a slice of the recent past played
        in reverse, with an envelope that grows into the cut. It is the reverse
        reverb of a trailer, made without the look-ahead a real one would need.
        """
        if not self._swell:
            return block
        near, far, active, cycle, mix = self._swell
        count = block.size
        positions = np.arange(count)
        clock = np.mod(self._swell_clock + positions / SAMPLE_RATE, float(cycle))
        progress = np.clip(clock / float(active), 0.0, 1.0)
        delay = (float(near) + (float(far) - float(near)) * progress) \
            * SAMPLE_RATE
        gain = np.where(clock < float(active), progress * progress, 0.0)
        self._swell_clock = float((self._swell_clock
                                   + count / SAMPLE_RATE) % float(cycle))
        ghost, self._swell_pos = self._delay_frame(
            block, self._swell_ring, self._swell_pos, delay)
        return block + ghost * (gain.astype(np.float32) * float(mix))

    def _freeze_block(self, block):
        """The last sixth of a second held, with a crossfade at its seam."""
        if not self._freeze:
            return block
        length, refresh = self._freeze
        history = self._freeze_history
        history.append(block.copy())
        if len(history) < length:
            return block
        self._freeze_counter += 1
        if self._freeze_loop is None:
            self._freeze_loop = list(history)
            self._freeze_pos = 0
        elif self._freeze_counter % int(refresh) == 0:
            # The drone follows the newest words without cutting the loop: the
            # position and the seam both survive the capture.
            self._freeze_loop = list(history)
        loop = self._freeze_loop
        position = self._freeze_pos
        out = loop[position]
        seam = self._freeze_seam
        if seam is not None and seam.shape == out.shape:
            # Two frames of blend: the slice ends on one phoneme and starts on
            # another, and that join is the one place this drone can click.
            if position == 0:
                out = loop[0] * 0.7 + seam * 0.3
            elif position == 1:
                out = loop[1] * 0.9 + seam * 0.1
        if position == length - 1:
            self._freeze_seam = loop[-1]
        self._freeze_pos = (position + 1) % length
        return out

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
        block = self._octave_block(block)
        block = self.dc.process(block)
        block = self.post.process(block)
        block = self._air_block(block)
        block = self._formant_block(block)
        block = self._vocoder_block(block)
        block = self._ott_block(block)
        block = self._chorus_block(block)
        block = self._tremolo_block(block)
        block = self._wah_block(block)
        block = self._phaser_block(block)
        block = self._echo_block(block)
        block = self._robot_block(block)
        block = self._flanger_block(block)
        block = self._vibrato_block(block)
        block = self._stutter_block(block)
        block = self._cave_block(block)
        block = self._alien_block(block)
        block = self._sub_octave_block(block)
        block = self._envelope_wah_block(block)
        block = self._gate_pedal_block(block)
        block = self._reverse_block(block)
        block = self._chorus3_block(block)
        block = self._vinyl_block(block)
        block = self._body_block(block)
        block = self._shimmer_block(block)
        block = self._whisper_block(block)
        block = self._growl_block(block)
        block = self._doubler_block(block)
        block = self._step_block(block)
        block = self._pump_block(block)
        block = self._cloud_block(block)
        block = self._swell_block(block)
        block = self._freeze_block(block)
        block = self._tapestop_block(block)
        # Unity below the knee and a hard asymptotic ceiling at PEAK_CEILING:
        # the encoder is handed a frame with room left for the effect sends the
        # PA and a cinema room put on top of it (see PEAK_CEILING).
        shaped = PEAK_CEILING * np.tanh(block * self._out / PEAK_CEILING)
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
