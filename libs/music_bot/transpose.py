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
* **One phase for all channels.** The advance is measured on the sum of the
  channels and applied as a rotation to each one, so the left/right phase
  relationship -- the stereo image -- is untouched while both sides keep their
  own magnitudes.

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

A key change glides over a handful of frames rather than jumping: the ratio
walks toward its target once per frame, so the pitch bends into the new key
instead of clicking into it. Zero semitones means the original key; a stream
that never engaged the shifter is byte-for-byte untouched (the streamer never
builds one), and a stream that glides back to zero keeps its delay until the
song ends rather than jumping forward mid-line.

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
    GLIDE = 0.55         # per-frame approach to a new ratio (~5 frames to settle)
    SNAP = 0.002         # stop gliding this close: a ratio this small is inaudible
    # The read cannot ask for a stretched sample the vocoder has not produced,
    # and the vocoder cannot produce a frame before the window after it exists:
    # one read step plus the window, at the largest ratio the dial allows (2x).
    LOOKAHEAD = 5120     # 5120 STRETCHED samples: read at the ratio, that is
                         # 53 ms with the key an octave up, 107 ms at the song's
                         # own key, and 213 ms an octave down

    def __init__(self, channels=2, sample_rate=SAMPLE_RATE):
        self.channels = max(1, int(channels))
        self.sample_rate = int(sample_rate)
        n = np.arange(self.WINDOW)
        self._window = (0.5 - 0.5 * np.cos(2.0 * np.pi * n / self.WINDOW)).astype(np.float32)
        self._omega = (2.0 * np.pi * np.arange(self.WINDOW // 2 + 1) / self.WINDOW)
        self._semitones = 0
        self._target = 1.0
        self._ratio = 1.0
        # Input ring: the samples this line has been handed but not yet used.
        self._in = np.zeros((0, self.channels), dtype=np.float32)
        self._in_pos = 0
        self._a = 0.0                 # analysis position of the next frame
        self._prev_phase = None
        self._prev_total = 0.0
        self._sum_phase = np.zeros(self.WINDOW // 2 + 1)
        # Stretched stream (the vocoder's output) and the read head into it.
        # It opens with LOOKAHEAD silent samples: that is the line's delay, and
        # it is what keeps the read from ever running past produced audio.
        self._st = np.zeros((self.LOOKAHEAD, self.channels), dtype=np.float32)
        self._st_pos = 0
        self._pending = np.zeros((self.WINDOW, self.channels), dtype=np.float32)
        # The overlap weight is seeded with its steady-state value: the first
        # frames then fade in over one window instead of dividing by a weight
        # that is still zero at the edges of the very first frame.
        self._wsum = np.full(self.WINDOW, self._steady_weight(self.HOP))
        self._q = 0.0
        self._fir = None
        self._fir_ratio = 0.0
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

    def reset(self):
        """Forget the stream: the next frame is a fresh song."""
        self._in = np.zeros((0, self.channels), dtype=np.float32)
        self._in_pos = 0
        self._a = 0.0
        self._prev_phase = None
        self._prev_total = 0.0
        self._sum_phase = np.zeros(self.WINDOW // 2 + 1)
        self._st = np.zeros((self.LOOKAHEAD, self.channels), dtype=np.float32)
        self._st_pos = 0
        self._pending = np.zeros((self.WINDOW, self.channels), dtype=np.float32)
        self._wsum = np.full(self.WINDOW, self._steady_weight(self.HOP))
        self._q = 0.0
        self._fir = None
        self._fir_ratio = 0.0
        self.padded_samples = 0

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
        return max(1, int(round(self.LOOKAHEAD / self._ratio)))

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

    # --------------------------------------------------------------- internals
    def _steady_weight(self, hop):
        """The Hann overlap-add weight a steady hop accumulates (4x hop = 1.5)."""
        return (self.WINDOW / float(hop)) * 0.375

    def _glide(self):
        target = self._target
        if abs(target - self._ratio) < self.SNAP:
            settled = True
            self._ratio = target
        else:
            settled = False
            self._ratio += (target - self._ratio) * self.GLIDE
        # The low-pass is only touched once the bend has arrived: replacing its
        # carry-over in the middle of a bend is a step at a frame boundary.
        if not settled:
            return
        if self._ratio > self.FILTER_FROM:
            if self._fir is None or abs(self._ratio - self._fir_ratio) > 0.01:
                self._make_lowpass(self._ratio)
        elif self._fir is not None:
            self._fir = None
            self._fir_ratio = 0.0

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

        mid = spectra[0] if self.channels == 1 else np.sum(spectra, axis=0)
        phase = np.angle(mid)
        magnitude = np.abs(mid)
        total = float(magnitude.sum())

        if self._prev_phase is None:
            # The first frame IS the song: no rotation to apply yet.
            self._sum_phase = np.array(phase, copy=True)
        else:
            step = phase - self._prev_phase - self._omega * analysis_hop
            step = (step + np.pi) % (2.0 * np.pi) - np.pi
            true_omega = self._omega + step / analysis_hop
            if total > 2.0 * self._prev_total > 0.0:
                # An attack: taking the analysis phase keeps the transient's
                # edge instead of smearing it over the previous accumulator.
                self._sum_phase = np.array(phase, copy=True)
            else:
                self._sum_phase = self._sum_phase + true_omega * synthesis_hop
        self._prev_phase = phase
        self._prev_total = total

        # Phase locking every bin to a spectral peak was tried and dropped: it
        # keeps a harmonic's shape, but it also ties an attack's bins to the
        # peaks around it, and measured clicks came back at 0.25-0.5 of their
        # level on a downward shift (0.49-0.74 without it) -- softer attacks
        # for a steadier chord. The per-bin accumulator is the better trade.
        rotation = np.exp(1j * (self._sum_phase - phase))
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
        if len(self._st):
            self._st = np.concatenate((self._st, published), axis=0)
        else:
            self._st = published
        # The analysis hop is fixed and the synthesis hop carries the ratio: a
        # ratio above 1 produces more samples than it consumed, which is the
        # stretch the read then gives back.
        self._a += analysis_hop

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
