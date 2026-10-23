"""Monophonic pitch detection for the line-in guitar/bass.

Feeds 20 ms mono frames (48 kHz, like the voice chat capture) into YIN and
turns them into discrete note-on events with a velocity derived from the
input level. Low notes (bass) need a longer analysis window, so the tracker
stacks three 20 ms frames into a 60 ms window before detecting.

Kept dependency-light (numpy only) and free of any game imports so it can be
unit-tested standalone.
"""
import collections

import numpy as np

NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]

DEFAULT_SR = 48000


def yin_pitch(frame, sr=DEFAULT_SR, min_freq=40.0, max_freq=1500.0, threshold=0.15):
    """Monophonic YIN pitch detection on one frame.

    Returns the detected frequency in Hz, or None when the frame is
    unpitched/noisy. ``frame`` must be a 1-D float array.
    """
    return yin_pitch_and_clarity(frame, sr, min_freq, max_freq, threshold)[0]


def _refined_lag(cmnd, tau, tau_min, tau_max):
    """The lag between ``tau - 1`` and ``tau + 1``, by parabolic interpolation.

    A lag is a whole number of samples, and near the top of a voice's range
    that is a coarse grid: at 220 Hz one lag is 8 cents, so a perfectly tuned
    A was read as 221.2 Hz -- a correction that would have pulled every A down
    a quarter of the way to G#. The three points around the minimum describe a
    parabola; its vertex is the lag (de Cheveigne and Kawahara, the standard
    YIN refinement). Outside half a sample the points are not a parabola one
    can trust, so the whole lag is kept.
    """
    if tau <= tau_min or tau >= tau_max:
        return float(tau)
    left, middle, right = float(cmnd[tau - 1]), float(cmnd[tau]), float(cmnd[tau + 1])
    curvature = left - 2.0 * middle + right
    if abs(curvature) < 1e-12:
        return float(tau)
    delta = 0.5 * (left - right) / curvature
    if not (-0.5 <= delta <= 0.5):
        return float(tau)
    return max(float(tau_min), min(float(tau_max), tau + delta))


def yin_pitch_and_clarity(frame, sr=DEFAULT_SR, min_freq=40.0, max_freq=1500.0,
                          threshold=0.15):
    """YIN, with how periodic the answer was: ``(hz, clarity)``.

    A note-on tracker only needs the note, so ``yin_pitch`` returns just the
    frequency. A tuner needs to know when to KEEP OUT: a breath, a consonant,
    a room's noise all have a best lag, and following it would bend the voice
    to a note nobody sang. ``clarity`` is one minus YIN's cumulative mean
    normalized difference at the chosen lag -- 1.0 for a steady tone, low for
    noise -- and is 0.0 whenever there is no answer at all.

    The answer is sub-sample accurate (``_refined_lag``), and the search is
    vectorized: this runs on every microphone frame a broadcast voice sends, so
    the cumulative-mean-difference loop it replaced was 14 % of one core by
    itself. Cumsum is sequential, so the numbers are the loop's own.
    """
    n = len(frame)
    tau_min = max(2, int(sr / max_freq))
    tau_max = min(n - 1, int(sr / min_freq))
    if tau_max <= tau_min:
        return None, 0.0
    nfft = 2 ** int(np.ceil(np.log2(n + tau_max)))
    xpad = np.zeros(nfft)
    xpad[:n] = frame
    X = np.fft.rfft(xpad)
    r = np.real(np.fft.irfft(X * np.conj(X)))[:tau_max + 1]
    # The difference function, summed over each lag's OWN overlap:
    #   d(tau) = sum_{j < n-tau} (x[j] - x[j+tau])^2
    #          = prefix(tau) + suffix(tau) - 2 r(tau)
    # Comparing the overlap against the energy of the WHOLE frame instead (the
    # old 2*(r[0] - r[tau])) leaves out the samples a long lag never reads, so
    # a long lag looks better than it is and every answer comes out short: a
    # 220 Hz tone read as 221.2 (8 cents sharp), a 82.4 Hz tone (a low bass) as
    # 6-14 cents sharp -- a tuner would then hold the voice that far off. With
    # each lag's own overlap the same tones come back within a tenth of a cent.
    x = np.asarray(frame, dtype=np.float64)[:n]
    energy = np.cumsum(x * x)
    total = float(energy[-1])
    suffix = total - np.concatenate(([0.0], energy[:-1]))
    before = n - 1 - np.arange(tau_max + 1)
    prefix = np.where(before >= 0, energy[np.maximum(before, 0)], 0.0)
    d = np.empty(tau_max + 1)
    d[0] = 0.0
    d[1:] = np.maximum(0.0, prefix[1:] + suffix[1:tau_max + 1] - 2.0 * r[1:tau_max + 1])
    running = np.cumsum(d[1:])
    cmnd = np.ones(tau_max + 1)
    cmnd[1:] = d[1:] * np.arange(1, tau_max + 1) / np.maximum(running, 1e-9)
    below = np.nonzero(cmnd[tau_min:tau_max] < threshold)[0]
    if below.size:
        tau = int(below[0]) + tau_min
        while tau + 1 < tau_max and cmnd[tau + 1] < cmnd[tau]:
            tau += 1
        # The vertex is taken from d, not cmnd: cmnd carries the cumulative
        # normalisation's own tilt, which is harmless for choosing the lag and
        # enough to move the vertex a few cents.
        return sr / _refined_lag(d, tau, tau_min, tau_max), float(1.0 - cmnd[tau])
    tau = int(np.argmin(cmnd[tau_min:])) + tau_min
    if cmnd[tau] < 0.3:
        return sr / _refined_lag(d, tau, tau_min, tau_max), float(1.0 - cmnd[tau])
    return None, 0.0


def hz_to_midi(hz):
    return int(round(69 + 12 * np.log2(hz / 440.0)))


def midi_to_hz(midi):
    return 440.0 * (2.0 ** ((midi - 69) / 12.0))


def midi_to_name(midi):
    if midi < 0 or midi > 127:
        return None
    return f"{NOTE_NAMES[midi % 12]}{midi // 12 - 1}"


def frame_rms(frame):
    if frame.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(frame.astype(np.float64) ** 2)))


class PitchTracker:
    """Turns 20 ms PCM frames into discrete note-on events.

    - RMS gate: silence resets the tracker so a fresh pluck re-emits.
    - 60 ms analysis window (3 stacked frames) so bass notes are detectable.
    - Semitone quantization with a cents deadband so a slightly flat/sharp
      string does not flap between two adjacent notes.
    - Confirm-hold: a note must be seen for two consecutive windows before it
      is emitted, suppressing transient jitter at the cost of ~40 ms latency.
    """

    WINDOW_FRAMES = 3          # 60 ms analysis window
    CONFIRM_FRAMES = 2         # windows the new note must be seen
    RMS_GATE = 0.01            # below this, treat the input as silence
    CENT_DEADBAND = 25.0       # stay on the current note within +/-25 cents
    VELOCITY_SCALE = 200.0     # rms (0..~0.6) -> MIDI velocity 1..127

    def __init__(self, sr=DEFAULT_SR, min_freq=40.0, max_freq=1500.0):
        self.sr = sr
        self.min_freq = min_freq
        self.max_freq = max_freq
        self._window = collections.deque(maxlen=self.WINDOW_FRAMES)
        self._current_midi = None
        self._candidate_midi = None
        self._candidate_hits = 0

    def reset(self):
        self._window.clear()
        self._current_midi = None
        self._candidate_midi = None
        self._candidate_hits = 0

    def feed(self, frame):
        """Feed one 20 ms mono float32 frame.

        Returns (note_name, velocity) when a new note is detected, else None.
        """
        rms = frame_rms(frame)
        if rms < self.RMS_GATE:
            self.reset()
            return None

        self._window.append(frame)
        if len(self._window) < self.WINDOW_FRAMES:
            return None

        window = np.concatenate(self._window)
        hz = yin_pitch(window, sr=self.sr, min_freq=self.min_freq,
                       max_freq=self.max_freq)
        if hz is None:
            self.reset()
            return None

        midi = hz_to_midi(hz)
        if midi < 0 or midi > 127:
            return None

        # If the current note is still the best match (within the deadband),
        # stay on it and drop any pending candidate.
        if self._current_midi is not None:
            cur_cents = 1200.0 * abs(np.log2(hz / midi_to_hz(self._current_midi)))
            if cur_cents <= self.CENT_DEADBAND:
                self._candidate_midi = None
                self._candidate_hits = 0
                return None

        # Otherwise promote the candidate note once it has been seen enough.
        if midi == self._candidate_midi:
            self._candidate_hits += 1
        else:
            self._candidate_midi = midi
            self._candidate_hits = 1
        if self._candidate_hits < self.CONFIRM_FRAMES:
            return None

        self._current_midi = midi
        self._candidate_hits = 0
        velocity = max(1, min(127, int(round(rms * self.VELOCITY_SCALE))))
        return midi_to_name(midi), velocity
