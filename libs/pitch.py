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
    n = len(frame)
    tau_min = max(2, int(sr / max_freq))
    tau_max = min(n - 1, int(sr / min_freq))
    if tau_max <= tau_min:
        return None
    nfft = 2 ** int(np.ceil(np.log2(n + tau_max)))
    xpad = np.zeros(nfft)
    xpad[:n] = frame
    X = np.fft.rfft(xpad)
    r = np.real(np.fft.irfft(X * np.conj(X)))[:tau_max + 1]
    d = np.empty(tau_max + 1)
    d[0] = 0.0
    d[1:] = 2.0 * (r[0] - r[1:])
    running = 0.0
    cmnd = np.ones(tau_max + 1)
    for tau in range(1, tau_max + 1):
        running += d[tau]
        cmnd[tau] = d[tau] * tau / max(running, 1e-9)
    for tau in range(tau_min, tau_max):
        if cmnd[tau] < threshold:
            while tau + 1 < tau_max and cmnd[tau + 1] < cmnd[tau]:
                tau += 1
            return sr / tau
    tau = int(np.argmin(cmnd[tau_min:])) + tau_min
    return (sr / tau) if cmnd[tau] < 0.3 else None


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
