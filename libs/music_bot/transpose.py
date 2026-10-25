"""The Music Bot's own pitch shifter: the song's key moves, the song's tempo stays.

Why this is not a filter on the ffmpeg command
----------------------------------------------
A song that is already playing cannot have its key changed by an ffmpeg
filter: the command is built once per flight (``streaming._build_cmd``) with a
filter graph fixed for the life of the decode, so every key change would mean
killing ffmpeg and starting again -- and that throws away the pre-buffer,
jumps the song's position and, on a cabinet, unpicks the room's shared clock.
Resampling alone (``asetrate``) also changes the *duration* unless it is
paired with ``atempo``: that pairing is exactly the "the song speeds up when I
change its key" report, and it still cannot be changed mid-song.

So the transposition happens here instead, on the 20 ms frames the decode
already produces, before anything hears them (the local OpenAL buffers, the
broadcast/megaphone leg, a cinema room's speaker bank). A frame in, the same
number of frames out, because byte count is what ``content_position``, the
cabinet's end-of-song hand-over, the pair clock and the network timeline all
measure: moving the key cannot move the beat, and a listener's jam notes stay
on it.

How the key moves
-----------------
Pitch and duration are two faces of one thing in a plain resample, so they are
separated the way a sampler has always separated them: time-stretch the song by
the pitch ratio, then read the stretched song back at that ratio. The duration
is then exactly what it was, and only the pitch has moved.

The stretch is a **phase vocoder, not a granular one**. WSOLA -- overlapping
copied segments -- is cheaper, but every seam joins two places in the song that
are not phase-aligned, so a sustained note or a chord beats at the hop rate:
heard as a wobble and a sound that is not quite full. A phase vocoder keeps a
phase accumulator per bin and advances it by each partial's own frequency, so
the overlap-add is coherent and the tone stays steady. Two refinements make it
musical rather than "phasey":

* **An attack resets the accumulator.** A transient is detected as a sudden
  rise in total magnitude; that frame takes its phase straight from the
  analysis, so a drum hit or a pluck keeps its edge instead of being smeared
  across the window.
* **One advance, ONE rotation.** The advance is measured per bin on the
  LOUDEST CHANNEL there, never on a sum: the parts of a voice that cancel in
  a sum -- its width, its reverb, a doubled take -- read as silence in it, and
  their advance came out as noise (a wide voice wobbled, and drifted). One
  rotation per bin is then applied to every side, which is what makes the
  stereo image safe by construction: a common rotation cannot change the phase
  relationship between the sides. Giving each channel its OWN rotation was
  tried and measured: the sides' relationship is then re-derived per channel
  from that channel's own peaks, and a wide signal's width fell 0.83 -> 0.61
  at +1 semitone (a bin beside a peak was rotated by the peak's relation
  rather than its own), so it is not the shape of this code.
* **Identity phase locking, blended in with the interval.** Rotating every bin
  by its own advance loses the phase relationship between the bins of one
  harmonic, which is heard as a rough, hollow shimmer on a held voice. Each
  bin next to a spectral peak is therefore rotated by that peak (Laroche and
  Dolson's identity locking, with the lock's span kept to the main lobe: a
  peak's skirt is usually another partial's leakage, and dragging it along
  modulates it at the hop rate). The lock is blended in as the key leaves the
  song's own key (0 at ratio 1, so the identity stays exact) and skipped on an
  attack frame, where a transient's edge matters more than a smooth region.

What that was worth, measured on a centred tone plus a side partial that
moves -- a voice's width, a doubled take, a reverb tail -- at -12 semitones:
the side channel held 0.256 of its level before and 0.307 after, its envelope
wobbled 0.581 before and 0.209 after. A vowel's harmonic energy is unchanged
(0.930 at +7, 0.894 at +12) and the centred tone's level is untouched, so the
transposition itself did not get duller to buy any of it.

The read back is Catmull-Rom cubic at the ratio. Reading faster than the
stretch produced is a decimation, so above a whole tone the stretched stream
goes through a 63-tap windowed-sinc low-pass at ``fs / 2 / ratio``: a sharp
filter from a long kernel keeps the passband flat (a short one is what made an
octave up sound dull), and only content above the new Nyquist is lost, which
no transposition can keep.

The line delays the song by ``LOOKAHEAD`` of the stretched stream: one read
step plus the analysis window the vocoder needs after it, which is about
107 ms at the song's own key and less as the key goes up, because the read
runs faster. A streaming shifter cannot avoid that delay, and it is not
audible at the start of a song; ``flush`` hands it back at the end so the
last note is not clipped.

**The overlap-add counts a partial's level and a noise's level differently.**
The published hop is divided by the sum of the squared windows, which is right
for a sustained partial (the frames of a partial add in phase) and wrong for
everything else: a voice's breath, a consonant, a snare, a diffuse reverb tail
is rotated frame to frame, so its frames add in power, and the line published
only `sqrt(sum w^4) / sum w^2` of it -- measured **0.61-0.68** of white noise's
level at every interval while a tone kept 1.00. That is a voice that has lost
its body and its air while the instruments around it kept theirs: the report of
"the vocal goes thin, sunk and mono, the music is fine" (2026-10-21). So each
written hop is level-matched to the audio it was read from, lifting only (up to
`LEVEL_MAX`, the worst measured deficit being 1.6 an octave down): noise then
holds **0.86-1.00** at every interval (0.973 at +7, 0.997 at +12, 0.888 at -7,
0.857 at -12), a voice 1.00-1.02, a diffuse room's wet 0.95-1.00, with a tone at
1.00-1.02 and the stereo image untouched. At the song's own key the match holds
unity, so zero semitones stays the byte-exact identity.

A key change glides over a handful of frames rather than jumping: the ratio
walks toward its target once per frame, so the pitch bends into the new key
instead of clicking into it. Zero semitones means the original key; a stream
that never engaged the shifter is byte-for-byte untouched (the streamer never
builds one), and a stream that glides back to zero keeps its delay until the
song ends rather than jumping forward mid-line. Once it has arrived it takes
its synthesis phase from the analysis every frame, which drops the phase the
shifted passage had accumulated: the song is then handed back exactly as it
came in, not as a phase-rotated copy of itself. That is the promise "Original
Key" makes -- the body returns with the key, in a tenth of a second, rather
than fading back over the seconds a stale accumulator would take.

Pinned by ``client/tests/test_music_bot_transpose.py``.
"""

import numpy as np


class ChromaticTransposer:
    """One song's key shifter, fed the stream's own 20 ms frames.

    ``process(pcm)`` takes interleaved s16le frames and returns exactly as many
    frames back, transposed by ``set_semitones``; ``flush()`` returns the
    look-ahead still inside the line at the end of a song. See the module
    docstring for why it is built this way.
    """

    SAMPLE_RATE = 48000
    MAX_SEMITONES = 12
    WINDOW = 2048        # analysis window, ~43 ms (low bass needs the length)
    HOP = 512            # analysis hop (4x overlap, so every attack lands mid-window)
    MIN_HOP = 64         # the synthesis hop follows the ratio, never below this
    TAPS = 63            # long enough that the passband stays flat
    FILTER_FROM = 1.02   # below this the read is an interpolation, not a decimation
    # The anti-aliasing low-pass is a step in the audio, not only a filter: it
    # is linear phase, so everything through it arrives half its length late
    # (31 samples), and switching it on jumps the audio by that much. A tuner's
    # ratio crosses FILTER_FROM every time a note's vibrato does, which is how
    # a crackle got under the slide. Two rules follow: the filter is dropped a
    # little BELOW the ratio that engaged it, so it cannot flap on and off
    # around one line (measured: 7 clicks in 4 s on a vibrato note without it,
    # 0 with it), and a line whose ratio moves every frame pins it instead
    # (see `pin_lowpass`) rather than switching it under a voice.
    FILTER_HYSTERESIS = 0.004  # the ratio must fall this far below the line to drop
    FILTER_RETUNE = 0.01       # rebuild the cutoff only when the ratio moved this far
    GLIDE = 0.55         # per-frame approach to a new ratio (~5 frames to settle)
    SNAP = 0.002         # stop gliding this close: a ratio this small is inaudible
    # The level the overlap-add loses on everything that is not a sustained
    # partial (see the module docstring), and how fast it is put back.
    LEVEL_MATCH = 0.12   # per 43 ms of written audio
    LEVEL_MAX = 2.0      # the worst measured deficit is 1.6 (an octave down)
    LEVEL_RELEASE = 0.9  # back to unity (3 hops, ~130 ms) at the song's own key
    # The read cannot ask for a stretched sample the vocoder has not produced,
    # and the vocoder cannot produce a frame before the window after it exists:
    # one read step plus the window, at the largest ratio the dial allows (2x).
    LOOKAHEAD = 5120     # 5120 STRETCHED samples: read at the ratio, that is
                         # 53 ms with the key an octave up, 107 ms at the song's
                         # own key, and 213 ms an octave down

    def __init__(self, channels=2, sample_rate=SAMPLE_RATE, lookahead=None,
                 exact_lookahead=False):
        self.channels = max(1, int(channels))
        self.sample_rate = int(sample_rate)
        # A line that asks for a wider interval than the dial allows -- a
        # harmony copy, a third or an octave above the broadcast voice -- needs
        # two things of its own to stay inside this class's contract: a longer
        # opening silence (the read has further to go before the vocoder has
        # produced the audio) and a limit for `set_pitch_offset`. Both default
        # to the dial's own, so an ordinary line is byte-for-byte what it was.
        self.pitch_limit = float(self.MAX_SEMITONES)
        self.lookahead = (self.LOOKAHEAD if lookahead is None
                          else self._opening_silence(lookahead, exact_lookahead))
        n = np.arange(self.WINDOW)
        self._window = (0.5 - 0.5 * np.cos(2.0 * np.pi * n / self.WINDOW)).astype(np.float32)
        self._omega = (2.0 * np.pi * np.arange(self.WINDOW // 2 + 1) / self.WINDOW)
        self._semitones = 0
        self._target = 1.0
        self._ratio = 1.0
        # How fast the ratio walks to a new target, once per frame. The dial's
        # own value is the class constant; Auto-Tune moves it (a "Retune
        # Speed" of Instant sets it to 1.0, which arrives in one frame).
        self.glide = self.GLIDE
        # True once the line is back at the song's own key: the synthesis phase
        # is then taken from the analysis every frame, so the output is the
        # song itself rather than a phase-rotated copy of it (see _emit_hop).
        self._identity = True
        # Input ring: the samples this line has been handed but not yet used.
        self._in = np.zeros((0, self.channels), dtype=np.float32)
        self._in_pos = 0
        self._a = 0.0                 # analysis position of the next frame
        self._prev_phase = None
        self._prev_total = 0.0
        self._sum_phase = np.zeros(self.WINDOW // 2 + 1)
        # The identity lock's blend, which fades in with the interval.
        self._lock_blend = 0.0
        # Which channel the phase is measured on, per bin (see _reference_pick).
        self._pick = None
        # Stretched stream (the vocoder's output) and the read head into it.
        # It opens with LOOKAHEAD silent samples: that is the line's delay, and
        # it is what keeps the read from ever running past produced audio.
        self._st = np.zeros((self.lookahead, self.channels), dtype=np.float32)
        self._st_pos = 0
        self._pending = np.zeros((self.WINDOW, self.channels), dtype=np.float32)
        # The overlap weight is seeded with its steady-state value: the first
        # frames then fade in over one window instead of dividing by a weight
        # that is still zero at the edges of the very first frame.
        self._wsum = np.full(self.WINDOW, self._steady_weight(self.HOP))
        self._q = 0.0
        self._fir = None
        self._fir_ratio = 0.0
        #: The ratio a caller pinned the filter at, or None (see pin_lowpass).
        self._pinned = None
        # The level match's running gain and the power of the hops it is
        # measuring (see _level_match).
        self._gain = 1.0
        self._out_power = 0.0
        self._out_samples = 0
        # Diagnostic: input samples the line had to invent as silence because
        # the song had not produced them yet. Zero once it is running is the
        # whole contract of LOOKAHEAD (pinned by the test suite).
        self.padded_samples = 0

    # ------------------------------------------------------------- the dial
    @property
    def semitones(self):
        return self._semitones

    def set_semitones(self, semitones):
        """Choose the key, in semitones from the original (clamped to +/- one octave)."""
        value = int(round(float(semitones)))
        value = max(-self.MAX_SEMITONES, min(self.MAX_SEMITONES, value))
        self._semitones = value
        self._target = float(2.0 ** (value / 12.0))
        return value

    def set_pitch_offset(self, semitones):
        """A continuous move, in semitones -- what a tuner asks for.

        ``set_semitones`` is a dial: whole semitones, clamped to +/-12. A
        tuner's correction is the distance between the note that was sung and
        the note it belongs to, which is a fraction of a semitone and changes
        every 20 ms, so this path keeps the fraction instead of rounding it
        away. The ratio it sets is the dial's own ratio, so the line, its
        glide, its delay and its tail are unchanged -- only the resolution is.
        """
        value = float(semitones)
        value = max(-self.pitch_limit, min(self.pitch_limit, value))
        self._semitones = value
        self._target = float(2.0 ** (value / 12.0))
        return value

    def pin_lowpass(self, ratio=None):
        """Hold the anti-aliasing filter on, at its widest, for this line's life.

        A tuner's ratio moves every frame, so a filter that follows it is
        switched and rebuilt under a voice that is already sounding -- and a
        linear-phase low-pass is a delay, so every switch is a step in the
        audio. Pinned, it is engaged ONCE, into the line's opening silence, at
        the largest ratio the caller will ask for, and never touched again: the
        song's dial keeps the filter that follows its key, a tuner does not need
        one. `ratio` is that largest ratio.
        """
        value = self.FILTER_FROM if ratio is None else float(ratio)
        # The ceiling is THIS line's own widest interval: a harmony copy an
        # octave up plus a correction reaches past the dial's 2.0, and a filter
        # pinned below the ratio it feeds would alias instead of anti-alias.
        ceiling = float(2.0 ** (self.pitch_limit / 12.0))
        self._pinned = min(ceiling, max(self.FILTER_FROM, value))
        self._make_lowpass(self._pinned)
        return self._pinned

    def reset(self):
        """Forget the stream: the next frame is a fresh song."""
        self._in = np.zeros((0, self.channels), dtype=np.float32)
        self._in_pos = 0
        self._a = 0.0
        self._prev_phase = None
        self._prev_total = 0.0
        self._sum_phase = np.zeros(self.WINDOW // 2 + 1)
        self._lock_blend = 0.0
        self._identity = True
        self._pick = None
        self._st = np.zeros((self.lookahead, self.channels), dtype=np.float32)
        self._st_pos = 0
        self._pending = np.zeros((self.WINDOW, self.channels), dtype=np.float32)
        self._wsum = np.full(self.WINDOW, self._steady_weight(self.HOP))
        self._q = 0.0
        self._fir = None
        self._fir_ratio = 0.0
        if self._pinned:
            # A pinned line re-engages its filter on the silence it resets to.
            self._make_lowpass(self._pinned)
        self._gain = 1.0
        self._out_power = 0.0
        self._out_samples = 0
        self.padded_samples = 0
        self.glide = self.GLIDE

    # ------------------------------------------------------------- one frame
    def process(self, pcm):
        """Transpose one interleaved s16le frame; the same number of frames comes back."""
        size = 2 * self.channels
        usable = len(pcm) - (len(pcm) % size)
        if usable <= 0:
            return b""
        frames = usable // size
        block = np.frombuffer(pcm[:usable], dtype="<i2").reshape(frames, self.channels)
        block = block.astype(np.float32) / 32768.0

        self._glide()
        self._append(block)
        out = np.empty((frames, self.channels), dtype=np.float32)
        done = 0
        # Read a hop at a time, not the whole frame: the look-ahead the line
        # needs is then this step plus the window, not a frame plus the window.
        while done < frames:
            step = min(self.HOP, frames - done)
            self._ensure(self._q + step * self._ratio + 3.0)
            out[done:done + step] = self._read(step)
            self._q += step * self._ratio
            done += step
        self._trim()
        return self._to_pcm(out)

    def delay_samples(self):
        """How many of the song's own samples the line is holding back.

        ``LOOKAHEAD`` counts stretched samples; the read runs at the ratio, so
        in the song's time the delay is that much shorter as the key goes up.
        """
        return max(1, int(round(self.lookahead / self._ratio)))

    def flush(self):
        """The look-ahead the line is still holding, at the end of a song."""
        lag = self.delay_samples()
        self._ensure(self._q + lag * self._ratio + 3.0)
        out = self._read(lag)
        self._q += lag * self._ratio
        return self._to_pcm(out)

    @classmethod
    def lag_samples(cls):
        """How many samples this line delays the song by (its whole look-ahead)."""
        return cls.LOOKAHEAD

    @classmethod
    def _opening_silence(cls, lookahead, exact=False):
        """How much silence a line opens with, for the look-ahead it was handed.

        ``LOOKAHEAD`` is the dial's own: the opening silence a line that may run
        at a ratio of 2.0 needs. A caller that asks for more gets more (a copy
        an octave above the voice reads faster still), and one that asks for
        less is held at the dial's own -- correct, because the delay it buys is
        the delay the caller planned for.

        ``exact=True`` is the other half of that story: the delay is in the
        SONG's time, so reading slower than the source (a copy BELOW the voice)
        turns the same opening silence into a LONGER delay. Such a caller wants
        the delay it planned, not the floor -- a bass copy is lined up with the
        lead by passing exactly `LOOKAHEAD * ratio` -- so the floor drops to the
        vocoder's own arithmetic: one read step plus the analysis window, which
        is `(HOP + WINDOW) * ratio` and therefore always a step below what that
        caller asks for (`HarmonyVoice`).
        """
        wanted = int(lookahead)
        if exact:
            return max(cls.WINDOW + cls.HOP, wanted)
        return max(cls.LOOKAHEAD, wanted)

    @classmethod
    def lookahead_for(cls, limit):
        """The opening silence a line that may ask for ``limit`` semitones needs.

        ``LOOKAHEAD`` is sized for the dial's own +12 (a ratio of 2.0): one read
        step plus the analysis window at that ratio. A line whose interval can
        go wider -- a harmony copy, an octave above the voice and a correction
        on top -- needs the same arithmetic at ITS ratio, or the read walks past
        what the vocoder has produced and `_input_slice` invents the samples
        (`padded_samples` is the witness). Past +12 this grows; below it the
        class constant is already enough and `__init__` keeps the larger one.
        """
        return int(np.ceil(cls.LOOKAHEAD * (2.0 ** (float(limit) / 12.0)) / 2.0))

    # --------------------------------------------------------------- internals
    def _steady_weight(self, hop):
        """The Hann overlap-add weight a steady hop accumulates (4x hop = 1.5)."""
        return (self.WINDOW / float(hop)) * 0.375

    PEAK_FLOOR = 0.02     # a peak quieter than this (of the loudest) owns nothing
    REFERENCE_HOLD = 1.25  # a channel must be this much louder to take a bin
    LOCK_BINS = 1         # bins either side of a peak that move with it

    def _glide(self):
        # The lock's own blend: identity phase locking is what takes the
        # roughness out of a held voice, but at the song's own key the line
        # must stay the exact identity, so it fades in with the interval.
        lock_target = 1.0 if self._semitones else 0.0
        if abs(lock_target - self._lock_blend) < self.SNAP:
            self._lock_blend = lock_target
        else:
            self._lock_blend += (lock_target - self._lock_blend) * self.GLIDE
        target = self._target
        if abs(target - self._ratio) < self.SNAP:
            settled = True
            self._ratio = target
        else:
            settled = False
            self._ratio += (target - self._ratio) * self.glide
        self._identity = settled and not self._semitones
        # The low-pass is only touched once the bend has arrived: replacing its
        # carry-over in the middle of a bend is a step at a frame boundary.
        if not settled:
            return
        if self._pinned:
            # Engaged once, at construction, and never switched under a voice.
            return
        if self._fir is None:
            if self._ratio > self.FILTER_FROM:
                self._make_lowpass(self._ratio)
        elif self._ratio < self.FILTER_FROM - self.FILTER_HYSTERESIS:
            self._fir = None
            self._fir_ratio = 0.0
        elif abs(self._ratio - self._fir_ratio) > self.FILTER_RETUNE:
            self._make_lowpass(self._ratio)

    def _append(self, block):
        if len(self._in):
            self._in = np.concatenate((self._in, block), axis=0)
        else:
            self._in = np.array(block, dtype=np.float32, copy=True)

    def _ensure(self, target):
        """Generate stretched samples until absolute position ``target`` exists."""
        while self._st_pos + len(self._st) < target:
            self._emit_hop()

    def _emit_hop(self):
        start = max(0, int(round(self._a)))
        # The ANALYSIS hop is fixed, so every attack is seen by a frame whose
        # window centres on it, and the SYNTHESIS hop carries the ratio. The
        # other way round (fixed synthesis, variable analysis) gave an upward
        # shift more analysis overlap and a downward one less, and on a
        # downward shift attacks fell between frames (measured: clicks came back
        # at 0.45-0.74 of their level).
        synthesis_hop = int(round(self.HOP * self._ratio))
        synthesis_hop = max(self.MIN_HOP, min(self.WINDOW, synthesis_hop))
        analysis_hop = float(self.HOP)
        signal = self._input_slice(start, self.WINDOW)
        spectra = [np.fft.rfft(signal[:, channel] * self._window)
                   for channel in range(self.channels)]

        # The phase reference is the LOUDEST CHANNEL in each bin, never the sum
        # of the channels: the width of a voice -- its reverb, a doubled take, a
        # hard-panned layer -- is exactly what cancels in a sum, and a phase
        # taken from a near-empty sum is noise. That noise became the frame's
        # advance, so those bins were frequency-modulated at the hop rate: a
        # wide voice wobbled and drifted toward the centre.
        if self.channels == 1:
            reference = spectra[0]
        else:
            stacked = np.stack([np.abs(spectrum) for spectrum in spectra])
            pick = self._reference_pick(stacked)
            reference = np.zeros_like(spectra[0])
            for channel in range(self.channels):
                take = pick == channel
                reference[take] = spectra[channel][take]
        phase = np.angle(reference)
        magnitude = np.abs(reference)
        total = float(magnitude.sum())

        first = self._prev_phase is None
        if first:
            true_omega = self._omega
        else:
            step = phase - self._prev_phase - self._omega * analysis_hop
            step = (step + np.pi) % (2.0 * np.pi) - np.pi
            true_omega = self._omega + step / analysis_hop
        attack = (not first) and total > 2.0 * self._prev_total > 0.0

        if first or attack or self._identity:
            # An attack takes its phase straight from the analysis: that is
            # what keeps a transient's edge instead of smearing it.
            self._sum_phase = np.array(phase, copy=True)
        else:
            self._sum_phase = self._sum_phase + true_omega * synthesis_hop
        self._prev_phase = phase
        self._prev_total = total

        # One rotation for the whole frame, so the sides keep the relationship
        # they were analysed with (see the module docstring).
        rotation = np.exp(1j * (self._sum_phase - phase))
        # Identity phase locking (Laroche and Dolson): each bin beside a peak is
        # rotated by that peak, which keeps one harmonic's bins together.
        # Per-bin rotation alone is "phasy" -- a held voice shimmers and sounds
        # hollow. The lock fades in with the interval, so the song's own key is
        # still the exact identity, and an attack frame keeps its own phase.
        if self._lock_blend > 0.0:
            regions = self._peak_regions(magnitude)
            locked = np.exp(1j * (self._sum_phase[regions] - phase[regions]))
            if self._lock_blend >= 1.0:
                rotation = locked
            else:
                blend = self._lock_blend
                rotation = (1.0 - blend) * rotation + blend * locked
                rotation = rotation / np.maximum(np.abs(rotation), 1e-9)

        frame = np.empty((self.WINDOW, self.channels), dtype=np.float32)
        for channel in range(self.channels):
            frame[:, channel] = np.fft.irfft(
                spectra[channel] * rotation, self.WINDOW) * self._window

        # Overlap-add, and normalize by the windows that actually overlap here:
        # the synthesis hop moves with the ratio (and walks during a bend), so a
        # single constant gain would be wrong for every hop but one. Dividing
        # by the accumulated weight also makes the identity (ratio 1) exact.
        self._pending += frame
        self._wsum += self._window * self._window
        divisor = np.maximum(self._wsum[:synthesis_hop], 1e-6)[:, None]
        published = (self._pending[:synthesis_hop] / divisor).astype(np.float32)
        self._pending = np.concatenate(
            (self._pending[synthesis_hop:],
             np.zeros((synthesis_hop, self.channels), dtype=np.float32)), axis=0)
        self._wsum = np.concatenate(
            (self._wsum[synthesis_hop:], np.zeros(synthesis_hop)), axis=0)
        if self._fir is not None:
            published = self._lowpass(published)
        published = self._level_match(published, signal)
        if len(self._st):
            self._st = np.concatenate((self._st, published), axis=0)
        else:
            self._st = published
        # The analysis hop is fixed and the synthesis hop carries the ratio: a
        # ratio above 1 produces more samples than it consumed, which is the
        # stretch the read then gives back.
        self._a += analysis_hop

    def _level_match(self, published, signal):
        """Put back the level the overlap-add loses on noise, breath and air.

        The divisor above assumes the frames of a hop add in phase, which is
        true of a sustained partial and false of everything else: a frame of
        noise is rotated against the one before it, so its frames add in
        power and the written hop is `sqrt(sum w^4) / sum w^2` of what it was
        read from -- measured 0.61-0.68 of white noise at every interval, and
        heard as a voice that lost its body while the instruments kept
        theirs. Lifting the written hop to the level of the audio it came from
        restores it (see the module docstring for the table), and it only ever
        lifts: a hop that is already as loud as its source is left alone.

        At the song's own key the gain is on its way back to unity, so the
        identity stays byte for byte. The measurement is per 43 ms of written
        audio -- long enough that one broadband hit cannot move the gain, short
        enough that a swell is followed rather than averaged.
        """
        self._out_power += float((published ** 2).sum())
        self._out_samples += published.size
        # Counted across every channel, so the window is 43 ms of audio for a
        # mono voice and for a stereo song alike.
        if self._out_samples < self.WINDOW * self.channels:
            return (published * self._gain).astype(np.float32)
        heard = float(np.sqrt(self._out_power / self._out_samples))
        source = float(np.sqrt((signal ** 2).mean()))
        self._out_power = 0.0
        self._out_samples = 0
        if self._semitones and heard > 1e-6 and source > 1e-6:
            want = min(self.LEVEL_MAX, max(1.0, source / heard))
            self._gain += (want - self._gain) * self.LEVEL_MATCH
        else:
            self._gain += (1.0 - self._gain) * self.LEVEL_RELEASE
        return (published * self._gain).astype(np.float32)

    def _reference_pick(self, magnitudes):
        """Which channel's phase each bin is measured on -- and it must HOLD.

        The loudest channel is the honest reference: a sum cancels the width
        of a voice, and a phase taken from near-silence is noise. But the
        loudest must not flicker. On a bin whose two sides are equal -- which
        is what an out-of-phase partial is -- the winner changes with the last
        bit of the FFT, and a reference that jumps by pi every hop cancels
        that partial in the overlap-add: measured on a moving side partial,
        the side channel lost 63 % of its level at -12 and wobbled 4x worse
        than the shipped line. So a channel only TAKES a bin when it is
        clearly louder than the one holding it.
        """
        if self._pick is None:
            self._pick = np.argmax(magnitudes, axis=0)
            return self._pick
        held = np.take_along_axis(magnitudes, self._pick[None, :], axis=0)[0]
        best = np.argmax(magnitudes, axis=0)
        self._pick = np.where(magnitudes.max(axis=0) > held * self.REFERENCE_HOLD,
                              best, self._pick)
        return self._pick

    def _peak_regions(self, magnitude):
        """The spectral peak each bin is locked to, or the bin itself.

        A peak is a bin at least as loud as its neighbours and not negligible.
        Only the peak and the bins within LOCK_BINS of it are locked: a peak's
        skirt is usually another partial's leakage, and dragging it with this
        peak modulates it at the hop rate (measured worse on a moving side
        partial), while the main lobe is what carries the partial's coherence.
        """
        bins = len(magnitude)
        left = np.empty(bins)
        left[0] = magnitude[0]
        left[1:] = magnitude[:-1]
        right = np.empty(bins)
        right[-1] = magnitude[-1]
        right[:-1] = magnitude[1:]
        floor = float(magnitude.max()) * self.PEAK_FLOOR
        peaks = np.nonzero((magnitude >= left) & (magnitude > right)
                           & (magnitude > floor))[0]
        owner = np.arange(bins)
        for peak in peaks:
            lo = max(0, int(peak) - self.LOCK_BINS)
            hi = min(bins, int(peak) + self.LOCK_BINS + 1)
            owner[lo:hi] = peak
        return owner

    def _input_slice(self, start, length):
        """``length`` samples from absolute ``start``, silence where the song is not."""
        out = np.zeros((length, self.channels), dtype=np.float32)
        total = self._in_pos + len(self._in)
        lo = max(self._in_pos, start)
        hi = min(total, start + length)
        if hi > lo:
            out[lo - start:hi - start] = self._in[lo - self._in_pos:hi - self._in_pos]
        short = start + length - max(total, start)
        if short > 0:
            self.padded_samples += short
        return out

    def _read(self, frames):
        """Read ``frames`` stretched samples, stepping by the ratio."""
        positions = self._q + np.arange(frames, dtype=np.float64) * self._ratio
        base = np.floor(positions).astype(np.int64)
        frac = (positions - base).astype(np.float32)
        rel = base - self._st_pos
        x = self._st
        p0 = x[rel - 1]
        p1 = x[rel]
        p2 = x[rel + 1]
        p3 = x[rel + 2]
        a = -0.5 * p0 + 1.5 * p1 - 1.5 * p2 + 0.5 * p3
        b = p0 - 2.5 * p1 + 2.0 * p2 - 0.5 * p3
        c = -0.5 * p0 + 0.5 * p2
        t = frac[:, None]
        return ((a * t + b) * t + c) * t + p1

    def _make_lowpass(self, ratio):
        taps = np.arange(self.TAPS) - (self.TAPS - 1) / 2.0
        cutoff = 0.5 * 0.94 / ratio       # cycles/sample; Nyquist is 0.5
        kernel = 2.0 * cutoff * np.sinc(2.0 * cutoff * taps) * np.hamming(self.TAPS)
        total = kernel.sum()
        if abs(total) < 1e-9:
            self._fir = None
            self._fir_ratio = 0.0
            return
        kernel = (kernel / total).astype(np.float32)
        # Carry the history over: the filter is switched on (or retuned) around
        # a signal that is already running through it. Starting it with silence
        # would make the first samples ramp up out of zero -- a click in the
        # middle of a song.
        needed = self.TAPS - 1
        if self._fir is not None:
            tail = self._fir[1]
        elif len(self._st) >= needed:
            tail = self._st[-needed:]
        else:
            tail = np.zeros((needed, self.channels), dtype=np.float32)
        self._fir = (np.array(kernel, copy=True), np.array(tail, copy=True))
        self._fir_ratio = ratio

    def _lowpass(self, block):
        kernel, tail = self._fir
        data = np.concatenate((tail, block), axis=0)
        out = np.empty((len(block), self.channels), dtype=np.float32)
        for channel in range(self.channels):
            out[:, channel] = np.convolve(data[:, channel], kernel, mode="valid")
        self._fir = (kernel, np.array(data[-(len(kernel) - 1):], copy=True))
        return out

    def _trim(self):
        """Drop what neither the next frame nor the read head can reach again."""
        keep_input = int(self._a) - (self.WINDOW + self.HOP)
        drop = keep_input - self._in_pos
        if drop > self.WINDOW:
            self._in = self._in[drop:]
            self._in_pos += drop

        keep_stretched = int(np.floor(self._q)) - 2
        drop = keep_stretched - self._st_pos
        if drop > self.HOP:
            self._st = self._st[drop:]
            self._st_pos += drop

    def _to_pcm(self, samples):
        clipped = np.clip(samples, -1.0, 1.0)
        return (clipped * 32767.0).round().astype("<i2").tobytes()


class GuitarTransposer(ChromaticTransposer):
    """The same shifter on a guitar's window: the fast profile Harmony uses.

    `ChromaticTransposer` analyses 43 ms at a time for a reason -- a song's
    bass line reaches below a guitar's range, and a song that is being played
    back is allowed to wait. A line-in guitar is not: its harmony copy is heard
    beside the note that is still ringing, so the analysis window IS the delay
    the second voice arrives late by. At 43 + 11 ms of window and hop the
    fastest copy (`lookahead_for`) sits 53 ms behind the string, which a player
    hears as a slap or a phasey double rather than as a second voice.

    1024 samples is 21 ms -- the shortest window that still spans a period of
    the lowest string (E2, 82 Hz, 12 ms) and resolves its partials, which are
    82 Hz apart against a 47 Hz bin. The copy then lands 26.7 ms behind the
    string with the lead held back to match, and what that costs is the bottom
    octave of a BASS guitar, where a 21 ms window cannot hold a full period,
    plus some smearing of a hard pick attack. That is the trade a playable
    harmony makes; the song's own shifter keeps its longer window.

    Nothing else changes: the same phase vocoder, the same ratio arithmetic,
    the same delay (``lookahead`` counts stretched samples, read at the ratio).
    """

    WINDOW = 1024       # 21 ms: the guitar's own window (see the docstring)
    HOP = 256           # 4x overlap, so an attack still lands mid-window
