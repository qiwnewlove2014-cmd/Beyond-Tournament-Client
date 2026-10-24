"""Auto-Tune: the broadcast voice, snapped to the note it belongs to.

Speech Key (`test_music_bot_speech_key.py`) moves a whole voice by one interval.
Auto-Tune measures the note being sung every 20 ms and moves the voice by the
distance to the nearest note of the chromatic grid, through the same line and
the same shifter -- so the PA's frame sequence, its timeline and a room's
playout are still untouched, and a listener still retunes nothing.

Pinned here, in the order that matters:

1. **The decision**: a tone on the grid asks for no move, a flat one is pushed
   up and a sharp one down, the answer never exceeds half a semitone, silence
   and noise say nothing at all, and an octave error is not an error (a
   semitone grid repeats by the octave, so it asks for the same move).
2. **The result**: a 20-cent-flat tone played through the line comes out on the
   note, measured with the project's own detector.
3. **The line**: one frame in is one frame out at every Retune Speed, the
   release hands the look-ahead back as whole frames, and Instant and Very slow
   are a frame apart in how fast the ratio arrives.
4. **Which voice is tuned**: the broadcast leg only, never proximity chat; and
   Auto-Tune replaces the speech key while it is on, because a chromatic grid
   already has a note every semitone.
5. **The dials**: the two menu lines, their labels, the speed list, the
   persisted options, and the Server's feature gate hiding both lines.

No game, OpenAL, ffmpeg or network is required.
"""

import os
import sys
import threading
import time
import unittest
from types import SimpleNamespace
from unittest import mock

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from libs import consts, voice_chat  # noqa: E402
from libs.music_bot import MapMusicBot  # noqa: E402
from libs.music_bot import autotune  # noqa: E402
from libs.music_bot import gate as music_bot_gate  # noqa: E402
from libs.music_bot import menus as menus_module  # noqa: E402
from libs.music_bot.transpose import ChromaticTransposer  # noqa: E402
from libs.pitch import yin_pitch, yin_pitch_and_clarity  # noqa: E402
from libs.voice_chat import (AUTOTUNE_AMOUNT_OPTION,  # noqa: E402
                             AUTOTUNE_MODE_OPTION, AUTOTUNE_OPTION,
                             AUTOTUNE_ROOT_OPTION, AUTOTUNE_SPEED_OPTION,
                             AutoTuneLine, SpeechKeyLine, VOICE_FRAME_BYTES,
                             VoiceChatRecord)

SR = 48000
FRAME = VOICE_FRAME_BYTES             # one 20 ms mono16 frame at 48 kHz, in bytes
SAMPLES = FRAME // 2                  # the same frame, in samples

#: The note every string in this file is measured against, and how far off the
#: grid it is, in cents (negative = flat).
A4 = 440.0
A3 = 220.0
Bb3 = 233.0819   # a semitone above A3: the far side of the boundary below


# --------------------------------------------------------------- the signals


def tone(seconds=3.0, freq=A3, amplitude=0.4, cents=0.0):
    """A steady tone, ``cents`` away from ``freq`` (a singer who misses)."""
    hz = freq * 2.0 ** (cents / 1200.0)
    t = np.arange(int(SR * seconds)) / SR
    return (amplitude * np.sin(2.0 * np.pi * hz * t)).astype(np.float32)


def to_pcm(signal):
    return np.clip(signal * 32768.0, -32768, 32767).astype("<i2").tobytes()


def from_pcm(data):
    return np.frombuffer(bytes(data), dtype="<i2").astype(np.float64) / 32768.0


def frames_of(signal):
    return [signal[start:start + SAMPLES]
            for start in range(0, len(signal) - SAMPLES + 1, SAMPLES)]


def play(line, signal):
    """Push a whole signal through a line; assert the frame contract as it goes."""
    out = bytearray()
    for chunk in frames_of(signal):
        shaped = line.shape(to_pcm(chunk))
        assert len(shaped) == FRAME, "a tuned frame changed the frame count"
        out += shaped
    return bytes(out)


def cents_from(hz, target):
    return 1200.0 * np.log2(hz / target)


def peak_step(signal):
    """The largest sample-to-sample step in a signal: how a click shows up."""
    return float(np.abs(np.diff(np.asarray(signal, dtype=np.float64))).max())


def voice_tone(seconds=1.2, freq=A3, cents=0.0, amplitude=0.3):
    """A note with harmonics, for the click checks.

    A pure sine is a degenerate signal for a phase vocoder: one bin carries the
    whole waveform, and its first ratio step moves every sample at once. A
    voice-like signal is what the line is for, and it is what these tests use.
    """
    hz = freq * 2.0 ** (cents / 1200.0)
    t = np.arange(int(SR * seconds)) / SR
    mono = sum((1.0 / k) * np.sin(2.0 * np.pi * hz * k * t + 0.7 * k)
               for k in range(1, 7))
    return (mono / np.abs(mono).max() * amplitude).astype(np.float32)


class FakeInput:
    """A capture device that hands over one delivery period of a steady tone."""

    def __init__(self, samples, signal=None):
        self.samples = samples
        self.signal = signal
        self.captured = []

    @property
    def available_samples(self):
        return self.samples

    def capture_samples(self, buf):
        count = len(buf) // 2
        if self.signal is None:
            buf[:] = np.zeros(count, dtype="<i2").tobytes()
        else:
            chunk = self.signal[:count]
            buf[:] = np.clip(chunk * 32768.0, -32768, 32767).astype("<i2").tobytes()
            self.signal = self.signal[count:]
        self.captured.append(count)
        self.samples = 0


class FakeCompression:
    def __init__(self, channel=consts.CHANNEL_VOICECHAT):
        self.channel = channel
        self.frames = []

    def put(self, value):
        self.frames.append(bytes(value))

    def set_channel(self, channel):
        self.channel = channel


def options_returning(**values):
    """A stand-in for libs.options.get with the Music Bot's voice settings."""

    def get(key, default=None):
        return values.get(key, default)

    return get


def options_on(**values):
    """The same, for a press whose Auto-Tune switch is ON.

    `AutoTuneLine` reads the switch every frame (turning it off has to hand the
    voice back mid-press), so a test that wants a tuned voice has to say so.
    """
    return options_returning(**dict(values, **{AUTOTUNE_OPTION: True}))


def options_from(values):
    """A stand-in that reads a dict the test can change between frames."""

    def get(key, default=None):
        return values.get(key, default)

    return get


# ------------------------------------------------------------- the decision


class DetectorTests(unittest.TestCase):
    def tuner(self, signal):
        tuner = autotune.AutoTuner()
        for chunk in frames_of(signal):
            tuner.track(chunk)
        return tuner

    def test_a_tone_on_the_grid_asks_for_no_move(self):
        for hz in (110.0, A3, A4):
            with self.subTest(hz=hz):
                tuner = self.tuner(tone(0.4, freq=hz))
                self.assertLess(abs(tuner.offset), 0.05,
                                f"{hz} Hz was pulled {tuner.offset:+.2f} semitones")

    def test_a_flat_note_is_pushed_up_and_a_sharp_one_down(self):
        flat = self.tuner(tone(0.4, cents=-20.0))
        sharp = self.tuner(tone(0.4, cents=+25.0))
        self.assertAlmostEqual(flat.offset, 0.20, delta=0.01)
        self.assertAlmostEqual(sharp.offset, -0.25, delta=0.01)

    def test_a_pure_tone_is_named_within_a_cent(self):
        """The detector's accuracy IS the tuner's accuracy.

        This is what the difference function's own-overlap energy buys: on the
        old biased one a 82.4 Hz tone read 6-14 cents sharp, so a tuner would
        have held a low voice that far off the note for as long as it sang.
        """
        for hz in (82.4, 110.0, 196.0, A3, 349.2, A4, 784.0):
            for cents in (-40.0, 0.0, 40.0):
                with self.subTest(hz=hz, cents=cents):
                    heard, _clarity = yin_pitch_and_clarity(
                        tone(0.2, freq=hz, cents=cents), min_freq=70.0,
                        max_freq=1000.0)
                    self.assertLess(abs(cents_from(heard, hz * 2.0 ** (cents / 1200.0))), 1.0)

    def test_the_window_has_to_fill_before_it_says_anything(self):
        """Two frames are 40 ms: less than three periods of a low voice."""
        tuner = autotune.AutoTuner()
        chunks = frames_of(tone(0.2))
        self.assertIsNone(tuner.track(chunks[0]))
        self.assertIsNone(tuner.track(chunks[1]))
        self.assertIsNotNone(tuner.track(chunks[2]))

    def test_silence_says_nothing_and_does_not_retune(self):
        tuner = self.tuner(tone(0.4, cents=-20.0))
        held = tuner.offset
        for _ in range(5):
            self.assertIsNone(tuner.track(np.zeros(SAMPLES, dtype=np.float32)))
        self.assertEqual(tuner.offset, held)
        self.assertIsNone(tuner.detected_hz)

    def test_noise_is_refused(self):
        rng = np.random.default_rng(11)
        tuner = autotune.AutoTuner()
        answers = [tuner.track((rng.standard_normal(SAMPLES) * 0.15).astype(np.float32))
                   for _ in range(12)]
        self.assertEqual(answers, [None] * 12)
        self.assertLess(tuner.clarity, autotune.AutoTuner.MIN_CLARITY)

    def test_an_octave_error_asks_for_the_same_move(self):
        """A semitone grid repeats by the octave, so the octave cannot matter."""
        low = self.tuner(tone(0.4, freq=A3, cents=-20.0))
        high = self.tuner(tone(0.4, freq=A4, cents=-20.0))
        self.assertAlmostEqual(low.offset, high.offset, delta=0.02)

    def test_the_correction_never_exceeds_half_a_semitone(self):
        for hz in np.arange(80.0, 800.0, 7.0):
            value = autotune.AutoTuner.grid_correction(float(hz))
            self.assertIsNotNone(value)
            self.assertLessEqual(abs(value), 0.5 + 1e-9)
        self.assertIsNone(autotune.AutoTuner.grid_correction(0.0))
        self.assertIsNone(autotune.AutoTuner.grid_correction(None))

    def test_the_detector_is_the_projects_own_yin(self):
        """The same function the line-in guitar already uses, on a voice."""
        hz, clarity = yin_pitch_and_clarity(
            tone(0.2)[:2880], min_freq=70.0, max_freq=1000.0)
        self.assertLess(abs(cents_from(hz, A3)), 1.0)
        self.assertGreater(clarity, 0.9)


class NoteHoldTests(unittest.TestCase):
    """The decision layer: a note is held, a change is confirmed, a move ramps.

    Three measured artifacts, one test each. A singer whose vibrato crosses the
    boundary between two notes used to have the target flip by a semitone every
    frame (heard as a wobble, not as tuning). A note change used to jump the
    ratio in a single 20 ms frame (heard as a chirp on the consonant). And one
    noisy frame used to name a note of its own (heard as the voice jumping away
    and back).
    """

    #: One analysis window: the three frames `detect` is handed by the line.
    WINDOW = SAMPLES * autotune.AutoTuner.WINDOW_FRAMES

    def window(self, hz=A3, cents=0.0):
        chunk = tone(self.WINDOW / float(SR), freq=hz, cents=cents)
        self.assertEqual(len(chunk), self.WINDOW)   # one contiguous window
        return chunk

    def feed(self, tuner, count, hz=A3, cents=0.0):
        return [tuner.detect(self.window(hz, cents)) for _ in range(count)]

    def test_a_vibrato_across_a_note_boundary_does_not_move_the_target(self):
        """A3+30c and Bb3-30c are the same distance either side of the line.

        A vibrato that swings 20 cents around that boundary puts every other
        run of frames nearer the other note. Held, the target never moves: the
        correction then follows the singer's own pitch by a few tenths of a
        semitone instead of swapping which note it is measured against.
        """
        tuner = autotune.AutoTuner()
        self.feed(tuner, 6, A3, +30.0)
        self.assertEqual(tuner.held_note, 57)
        for _ in range(8):
            self.feed(tuner, 3, A3, +30.0)
            self.feed(tuner, 3, Bb3, -30.0)
            self.assertEqual(
                tuner.held_note, 57,
                f"the target followed the vibrato to MIDI {tuner.held_note}")

    def test_a_neighbouring_note_is_confirmed_before_the_target_moves(self):
        """A pitch just over the line has to be meant before the target follows.

        0.6 of a semitone above A3 is nearer Bb3 by 0.2 -- but a vibrato that
        swings that far is not a note change, so the target waits out the vote.
        The next test is the other half: a note a whole tone away is followed at
        once, because nothing oscillates that widely and comes back.
        """
        tuner = autotune.AutoTuner()
        self.feed(tuner, 8, A3)
        self.assertEqual(tuner.held_note, 57)
        just_over = A3 * 2.0 ** (60.0 / 1200.0)   # 0.6 of a semitone up
        switched = None
        for index in range(1, 21):
            tuner.detect(self.window(just_over))
            if tuner.held_note != 57:
                switched = index
                break
        self.assertIsNotNone(switched, "the target never followed a sung Bb")
        self.assertGreaterEqual(switched, autotune.AutoTuner.TARGET_CONFIRM)
        self.assertEqual(tuner.held_note, 58)

    def test_a_note_that_has_clearly_been_left_is_followed_at_once(self):
        """A fifth up is not a wobble around the note; it is another note."""
        tuner = autotune.AutoTuner()
        self.feed(tuner, 8, A3)
        switched = None
        for index in range(1, 9):
            tuner.detect(self.window(midi_hz(64)))
            if tuner.held_note == 64:
                switched = index
                break
        self.assertIsNotNone(switched, "a slide of a fifth waited out a vote")
        self.assertLess(switched, autotune.AutoTuner.TARGET_CONFIRM)

    def test_a_note_change_bends_over_frames_and_never_in_one_step(self):
        """Half a semitone sharp to half a semitone flat, while the note holds."""
        tuner = autotune.AutoTuner()
        self.feed(tuner, 6, A3, +45.0)     # sharp: pushed down by 0.45
        before = tuner.offset
        self.assertAlmostEqual(before, -0.45, delta=0.02)
        steps = []
        for _ in range(6):
            tuner.detect(self.window(A3, -45.0))   # flat: pushed up by 0.45
            steps.append(tuner.offset)
        previous = before
        for step in steps:
            self.assertLessEqual(
                abs(step - previous), autotune.AutoTuner.RAMP_SEMITONES + 1e-6,
                f"the ratio moved {abs(step - previous):.2f} semitones in one frame")
            previous = step
        self.assertAlmostEqual(steps[-1], +0.45, delta=0.02)
        self.assertLess(abs(steps[0] - before), 0.9,
                        "the whole bend arrived in the first frame")

    def test_a_pending_vote_does_not_drag_the_voice_off_the_note(self):
        """The measurement that asked for the freeze.

        A tone 45 cents flat is tuned up by 0.45. The pitch then moves to 0.65
        of a semitone above A3 -- nearer Bb3, but inside the quarter tone of
        doubt, so the target is still held. Pulling onto A3 would now be a pull
        of -0.5, the wrong way entirely, for the length of the vote; the
        correction is frozen instead, and the vote's own answer is only 0.35
        away from where the voice already is.
        """
        tuner = autotune.AutoTuner()
        self.feed(tuner, 6, A3, -45.0)
        applied = tuner.offset
        self.assertAlmostEqual(applied, 0.45, delta=0.02)
        just_over = A3 * 2.0 ** (65.0 / 1200.0)
        answers = self.feed(tuner, autotune.AutoTuner.TARGET_CONFIRM - 1,
                            just_over)
        self.assertEqual(tuner.held_note, 57)
        for answer in answers:
            self.assertAlmostEqual(answer, applied, delta=0.02)
        self.feed(tuner, 4, just_over)
        self.assertEqual(tuner.held_note, 58)
        self.assertLess(abs(tuner.offset - 0.35), 0.02,
                        "the vote's answer did not arrive after it won")

    def test_one_frame_cannot_name_a_note_of_its_own(self):
        """A consonant or a click is one frame, and it is not a note."""
        tuner = autotune.AutoTuner()
        self.feed(tuner, 8, A3, -30.0)
        held, before = tuner.held_note, tuner.offset
        outlier = tuner.detect(self.window(midi_hz(69)))
        self.assertEqual(tuner.held_note, held)
        self.assertAlmostEqual(outlier, before, delta=0.01)

    def test_a_breath_does_not_release_the_note(self):
        """The ratio stands through silence, and so does the note under it."""
        tuner = autotune.AutoTuner()
        self.feed(tuner, 8, A3, -30.0)
        held, before = tuner.held_note, tuner.offset
        for _ in range(4):
            self.assertIsNone(tuner.detect(
                np.zeros(self.WINDOW, dtype=np.float32)))
        self.assertEqual(tuner.held_note, held)
        self.assertEqual(tuner.offset, before)


class SpeedTests(unittest.TestCase):
    def test_normal_is_the_songs_own_glide(self):
        """A tuner at Normal moves a voice as fast as a key change moves a song."""
        self.assertEqual(autotune.speed_glide(autotune.DEFAULT_SPEED),
                         ChromaticTransposer.GLIDE)

    def test_the_speeds_run_fastest_to_slowest(self):
        glides = [glide for _label, glide, _note in autotune.SPEEDS]
        self.assertEqual(glides, sorted(glides, reverse=True))
        self.assertEqual(glides[0], 1.0)
        self.assertGreater(len(glides), 3)

    def test_a_saved_speed_that_is_not_one_is_clamped(self):
        self.assertEqual(autotune.clamp_speed(-20), 0)
        self.assertEqual(autotune.clamp_speed(999), len(autotune.SPEEDS) - 1)
        self.assertEqual(autotune.clamp_speed("nonsense"), autotune.DEFAULT_SPEED)
        self.assertEqual(autotune.clamp_speed(None), autotune.DEFAULT_SPEED)
        self.assertEqual(autotune.speed_label(0), autotune.SPEEDS[0][0])


# --------------------------------------------------------------- the line


class LineTests(unittest.TestCase):
    def test_a_frame_in_is_a_frame_out_at_every_speed(self):
        signal = tone(1.0, cents=-15.0)
        for index in range(len(autotune.SPEEDS)):
            with self.subTest(speed=autotune.SPEEDS[index][0]):
                line = AutoTuneLine(speed=index)
                with mock.patch.object(
                        voice_chat.options, "get",
                        options_on(**{AUTOTUNE_SPEED_OPTION: index})):
                    for chunk in frames_of(signal):
                        self.assertEqual(len(line.shape(to_pcm(chunk))), FRAME)

    def test_a_flat_tone_comes_out_on_the_note(self):
        flat = tone(3.0, cents=-20.0)
        line = AutoTuneLine(speed=0)
        with mock.patch.object(
                voice_chat.options, "get",
                options_on(**{AUTOTUNE_SPEED_OPTION: 0})):
            out = from_pcm(play(line, flat))
        sung = yin_pitch(out[SAMPLES * 10:SAMPLES * 40], min_freq=70.0, max_freq=1000.0)
        missed = cents_from(sung, A3)
        self.assertLess(abs(missed), 5.0,
                        f"the note is still {missed:+.1f} cents off")

    def test_instant_arrives_in_one_frame_and_slow_takes_many(self):
        """Both are measured on the first frame that has anything to say."""
        signal = tone(1.0, cents=-20.0)
        chunks = frames_of(signal)
        wanted = 2.0 ** (0.20 / 12.0)

        def first_correction(speed):
            line = AutoTuneLine(speed=speed)
            with mock.patch.object(
                    voice_chat.options, "get",
                    options_on(**{AUTOTUNE_SPEED_OPTION: speed})):
                for chunk in chunks:
                    line.shape(to_pcm(chunk))
                    if line.tuner.detected_hz is not None:
                        return line
            self.fail("the detector never named a note")

        fast = first_correction(0)
        slow = first_correction(len(autotune.SPEEDS) - 1)
        # Instant is at its target in the same frame it is asked for it; Very
        # slow has moved an eighth of the way. The detector's window is deeper
        # than the line, which is why the measurement starts a look-ahead in.
        self.assertAlmostEqual(fast.transposer._ratio, fast.transposer._target,
                               delta=1e-9)
        self.assertLess(abs(fast.transposer._target - wanted), 0.002)
        self.assertLess(slow.transposer._ratio - 1.0, 0.25 * (wanted - 1.0))

    def test_the_detector_hears_the_audio_the_read_head_is_reading(self):
        """The regression that a held note cannot show, and a slide can.

        The ratio chosen this frame is applied to the audio one look-ahead old.
        Detecting on the NEWEST frame instead tunes that audio by the pitch of a
        note that has not been played yet: on this slide (600 cents a second) it
        sits 63 cents sharp of what it should be, and the measured output was
        38-48 cents flat -- a third of a semitone, the feature gone.
        """
        seconds = 2.0
        t = np.arange(int(SR * seconds)) / SR
        chirp = (0.3 * np.sin(2.0 * np.pi * np.cumsum(200.0 * 2.0 ** (t / seconds)) / SR))
        line = AutoTuneLine(speed=0)
        played = 0
        with mock.patch.object(
                voice_chat.options, "get",
                options_on(**{AUTOTUNE_SPEED_OPTION: 0})):
            while line.tuner.detected_hz is None and played < len(chirp) - SAMPLES:
                line.shape(to_pcm(chirp[played:played + SAMPLES]))
                played += SAMPLES
        self.assertIsNotNone(line.tuner.detected_hz)
        delay = line.transposer.delay_samples()
        window = autotune.AutoTuner.WINDOW_FRAMES * SAMPLES
        # The centre of the window it analysed: one look-ahead and half a
        # window back from the frame just written.
        heard_at = played - delay - window / 2.0
        newest_at = played - SAMPLES / 2.0
        heard = 200.0 * 2.0 ** ((heard_at / SR) / seconds)
        newest = 200.0 * 2.0 ** ((newest_at / SR) / seconds)
        self.assertLess(abs(cents_from(line.tuner.detected_hz, heard)), 15.0)
        self.assertGreater(abs(cents_from(line.tuner.detected_hz, newest)), 40.0)

    def test_a_sung_scale_lands_each_note_on_its_own_note(self):
        """The headline: it follows the singer from note to note."""
        # Each of these is between 30 and 45 cents off its nearest note.
        melody = (205.0, 226.0, 249.0, 279.0)
        line = AutoTuneLine(speed=0)
        out = bytearray()
        with mock.patch.object(
                voice_chat.options, "get",
                options_on(**{AUTOTUNE_SPEED_OPTION: 0})):
            for hz in melody:
                out += play(line, tone(0.8, freq=hz))
        audio = from_pcm(bytes(out))
        for index, hz in enumerate(melody):
            with self.subTest(sung=hz):
                midi = 69.0 + 12.0 * np.log2(hz / 440.0)
                note = 440.0 * 2.0 ** ((round(midi) - 69.0) / 12.0)
                # The middle of each note's own output block, clear of both
                # seams: the block is 0.8 s and the line's delay is ~0.1 s, so
                # the last five frames of a block are already the next note.
                end = SAMPLES * 40 * (index + 1)
                heard = yin_pitch(audio[end - SAMPLES * 16:end - SAMPLES * 8],
                                  min_freq=70.0, max_freq=1000.0)
                self.assertLess(abs(cents_from(heard, note)), 8.0,
                                f"{hz} Hz did not land on {note:.2f} Hz")

    def test_the_dial_moves_the_line_already_running(self):
        line = AutoTuneLine(speed=autotune.DEFAULT_SPEED)
        with mock.patch.object(
                voice_chat.options, "get",
                options_returning(**{AUTOTUNE_SPEED_OPTION: 0})):
            line.shape(to_pcm(frames_of(tone(0.1))[0]))
        self.assertEqual(line.speed, 0)
        self.assertEqual(line.transposer.glide, autotune.speed_glide(0))

    def test_the_tail_is_whole_frames(self):
        line = AutoTuneLine(speed=2)
        with mock.patch.object(voice_chat.options, "get", options_returning()):
            play(line, tone(1.0, cents=-10.0))
            tail = line.tail_frames(FRAME)
        expected = (line.transposer.delay_samples() * 2) // FRAME
        self.assertGreater(expected, 0)
        self.assertEqual(len(tail), expected)
        for chunk in tail:
            self.assertEqual(len(chunk), FRAME)

    def test_the_line_does_not_pad_the_stream(self):
        """``padded_samples`` is zero once it runs: the look-ahead is real."""
        line = AutoTuneLine(speed=0)
        with mock.patch.object(voice_chat.options, "get", options_returning()):
            play(line, tone(1.0, cents=-10.0))
        self.assertEqual(line.transposer.padded_samples, 0)


class SwitchTests(unittest.TestCase):
    """Turning Auto-Tune off and on while the voice is already going out.

    The switch was read once, when the press started: a player who turned it
    off mid-sentence kept a tuned voice until they stopped talking and started
    again, which is the report this pins. It is read per frame now -- off walks
    the ratio back to 1.0 at the Retune Speed in force (a glide, not a cut) and
    on resumes, because the line keeps the press's samples either way.
    """

    SUNG = -40.0      # a note this far flat, and the grid's pull up to the note

    def segments(self):
        values = {AUTOTUNE_OPTION: True, AUTOTUNE_SPEED_OPTION: 2}
        line = AutoTuneLine(speed=2)
        sung = voice_tone(1.2, cents=self.SUNG)
        with mock.patch.object(voice_chat.options, "get", options_from(values)):
            tuned = from_pcm(play(line, sung))
            values[AUTOTUNE_OPTION] = False
            natural = from_pcm(play(line, sung))
            values[AUTOTUNE_OPTION] = True
            again = from_pcm(play(line, sung))
        return line, tuned, natural, again

    @staticmethod
    def heard(segment):
        # The last half of the block, well clear of the release's look-ahead.
        return yin_pitch(segment[SAMPLES * 30:SAMPLES * 55],
                         min_freq=70.0, max_freq=1000.0)

    def test_turning_it_off_hands_the_voice_back_mid_press(self):
        _line, tuned, natural, _again = self.segments()
        self.assertLess(abs(cents_from(self.heard(tuned), A3)), 5.0)
        self.assertLess(
            abs(cents_from(self.heard(natural), A3 * 2.0 ** (self.SUNG / 1200.0))),
            8.0, "the voice did not go back to the note the singer sang")

    def test_turning_it_back_on_tunes_again_in_the_same_press(self):
        _line, _tuned, _natural, again = self.segments()
        self.assertLess(abs(cents_from(self.heard(again), A3)), 5.0)

    def test_the_switch_glides_and_never_cuts_the_voice(self):
        """Off is a correction ending, not a mute and not a click.

        A hard switch would put the voice's own pitch back in one sample -- a
        step the size of the waveform. The glide lands it over about 100 ms,
        and the level never drops: measured, the worst step in the three blocks
        is 1.5 times the signal's own (the same allowance the key shifter's own
        bend test gives), where a hard cut would be ten.
        """
        _line, tuned, natural, again = self.segments()
        sung_step = peak_step(voice_tone(0.2, cents=self.SUNG))
        for index, segment in enumerate((tuned, natural, again)):
            with self.subTest(segment=index):
                self.assertLess(peak_step(segment), sung_step * 2.0)
                self.assertGreater(float(np.abs(segment).max()), 0.1)


# -------------------------------------------------------- which press is tuned


class RecorderGateTests(unittest.TestCase):
    def recorder(self):
        rec = VoiceChatRecord.__new__(VoiceChatRecord)
        rec._speech_line = None
        return rec

    def test_proximity_voice_is_never_tuned(self):
        rec = self.recorder()
        with mock.patch.object(voice_chat, "autotune_enabled", lambda: True):
            self.assertIsNone(rec._speech_line_for(False))
        self.assertIsNone(rec._speech_line)

    def test_autotune_builds_the_line_for_a_broadcast(self):
        rec = self.recorder()
        with mock.patch.object(voice_chat, "autotune_enabled", lambda: True), \
                mock.patch.object(voice_chat, "speech_key_semitones", lambda: 0):
            line = rec._speech_line_for(True)
            self.assertIsInstance(line, AutoTuneLine)
            self.assertIs(rec._speech_line_for(True), line)

    def test_autotune_replaces_the_speech_key_while_it_is_on(self):
        """A chromatic grid has a note every semitone: the key cannot move it."""
        rec = self.recorder()
        with mock.patch.object(voice_chat, "autotune_enabled", lambda: True), \
                mock.patch.object(voice_chat, "speech_key_semitones", lambda: 7):
            self.assertIsInstance(rec._speech_line_for(True), AutoTuneLine)

    def test_the_key_dial_still_builds_its_own_line_when_autotune_is_off(self):
        rec = self.recorder()
        with mock.patch.object(voice_chat, "autotune_enabled", lambda: False), \
                mock.patch.object(voice_chat, "speech_key_semitones", lambda: 7):
            line = rec._speech_line_for(True)
        self.assertIsInstance(line, SpeechKeyLine)
        self.assertEqual(line.semitones, 7)

    def test_no_dial_and_no_autotune_is_no_line_at_all(self):
        rec = self.recorder()
        with mock.patch.object(voice_chat, "autotune_enabled", lambda: False), \
                mock.patch.object(voice_chat, "speech_key_semitones", lambda: 0):
            self.assertIsNone(rec._speech_line_for(True))


# ------------------------------------------------------------- the capture


class CaptureTests(unittest.TestCase):
    def recorder(self, speed=0):
        rec = VoiceChatRecord.__new__(VoiceChatRecord)
        rec.running = True
        rec.recording = True
        rec.stereo = False
        rec.player = SimpleNamespace(
            gameplay=SimpleNamespace(voice_chat_using_megaphone=True))
        rec.game = SimpleNamespace()
        rec.audio_input = FakeInput(VOICE_FRAME_BYTES // 2, signal=tone(1.0, cents=-12.0))
        rec.vc_compression = FakeCompression()
        rec._speech_line = None
        rec.speed = speed
        return rec

    def run_once(self, rec):
        worker = threading.Thread(target=rec.run, daemon=True)
        with mock.patch.object(
                voice_chat.options, "get",
                options_returning(**{AUTOTUNE_OPTION: True,
                                     AUTOTUNE_SPEED_OPTION: rec.speed})):
            worker.start()
            deadline = time.monotonic() + 2.0
            while not rec.vc_compression.frames and time.monotonic() < deadline:
                time.sleep(0.002)
            rec.running = False
            worker.join(2)
        self.assertFalse(worker.is_alive())
        return rec.vc_compression.frames

    def test_the_broadcast_frame_is_a_tuned_frame_of_the_same_size(self):
        rec = self.recorder()
        frames = self.run_once(rec)
        self.assertEqual(len(frames), 1)
        self.assertEqual(len(frames[0]), VOICE_FRAME_BYTES)
        self.assertIsInstance(rec._speech_line, AutoTuneLine)

    def test_the_pa_channel_is_unchanged_by_the_tuning(self):
        rec = self.recorder()
        self.run_once(rec)
        self.assertEqual(rec.vc_compression.channel, consts.CHANNEL_MEGAPHONE)

    def test_the_monitor_is_fed_the_tuned_frame(self):
        rec = self.recorder()
        with mock.patch.object(voice_chat, "_feed_local_megaphone_direct") as fed:
            self.run_once(rec)
        self.assertTrue(fed.called)
        self.assertEqual({len(call.args[1]) for call in fed.call_args_list},
                         {VOICE_FRAME_BYTES})


class ReleaseTests(unittest.TestCase):
    def test_the_release_hands_the_look_ahead_back_after_the_last_frame(self):
        rec = VoiceChatRecord.__new__(VoiceChatRecord)
        rec._speech_line = AutoTuneLine(speed=0)
        rec.audio_input = FakeInput(VOICE_FRAME_BYTES, signal=tone(0.2, cents=-10.0))
        rec.vc_compression = FakeCompression(consts.CHANNEL_MEGAPHONE)
        rec.game = SimpleNamespace()
        rec.player = SimpleNamespace(gameplay=None)
        expected = (rec._speech_line.transposer.delay_samples() * 2) // FRAME
        with mock.patch.object(voice_chat.options, "get", options_returning()):
            rec.voice_chat_finish2()
        frames = rec.vc_compression.frames
        self.assertEqual(len(frames), 1 + expected)
        for chunk in frames:
            self.assertEqual(len(chunk), VOICE_FRAME_BYTES)
        self.assertIsNone(rec._speech_line)

    def test_a_dead_microphone_drops_the_line_instead_of_raising(self):
        rec = VoiceChatRecord.__new__(VoiceChatRecord)
        rec._speech_line = AutoTuneLine(speed=0)

        class Dead:
            @property
            def available_samples(self):
                raise voice_chat.cyal.exceptions.CyalError("unplugged")

        rec.audio_input = Dead()
        rec.vc_compression = FakeCompression()
        rec.game = SimpleNamespace()
        rec.player = SimpleNamespace(gameplay=None)
        rec.voice_chat_finish2()
        self.assertIsNone(rec._speech_line)


# ---------------------------------------------------------------- the dials


class BotOptionTests(unittest.TestCase):
    def bot(self):
        bot = MapMusicBot.__new__(MapMusicBot)
        bot.autotune_enabled = False
        bot.autotune_speed = autotune.DEFAULT_SPEED
        return bot

    def test_the_switch_persists(self):
        bot = self.bot()
        with mock.patch("libs.music_bot.controller.options.set") as saved:
            self.assertTrue(bot.toggle_autotune())
            saved.assert_called_with(AUTOTUNE_OPTION, True)
            self.assertFalse(bot.toggle_autotune())
            saved.assert_called_with(AUTOTUNE_OPTION, False)
        self.assertEqual(bot.autotune_label(), "OFF")

    def test_the_speed_persists_and_is_clamped(self):
        bot = self.bot()
        with mock.patch("libs.music_bot.controller.options.set") as saved:
            self.assertEqual(bot.set_autotune_speed(99), len(autotune.SPEEDS) - 1)
            saved.assert_called_with(AUTOTUNE_SPEED_OPTION, len(autotune.SPEEDS) - 1)
        self.assertEqual(bot.autotune_speed_label(),
                         autotune.SPEEDS[-1][0])

    def test_the_defaults_leave_the_voice_alone(self):
        with mock.patch.object(voice_chat.options, "get", options_returning()):
            self.assertFalse(voice_chat.autotune_enabled())
            self.assertEqual(voice_chat.autotune_speed_index(),
                             autotune.DEFAULT_SPEED)


def labels_in(menu):
    """Every line's text, read the way the menu itself reads it.

    A label may be a callable -- `libs/menu.py` says so on `add_items`: "if [0]
    is a callable it will use its return value to speak its label" -- which is
    how a live mark like "(active)" follows the setting it names instead of
    freezing at the moment the list was built.
    """
    return [item[0]() if callable(item[0]) else item[0] for item in menu.items]


class FakeMenu:
    built = []

    def __init__(self, game, title, parrent=None):
        self.title = title
        self.items = []
        FakeMenu.built.append(self)

    def add_items(self, items):
        self.items.extend(items)

    def speak_current_item(self):
        return None


class MenuTests(unittest.TestCase):
    def setUp(self):
        FakeMenu.built = []
        self.addCleanup(mock.patch.stopall)
        self.bot = mock.MagicMock()
        self.bot.transpose_semitones = 0
        self.bot.speech_transpose_semitones = 0
        self.bot.transpose_label.return_value = "Original key"
        self.bot.speech_transpose_label.return_value = "Original key"
        self.bot.output_device_label.return_value = "Same as the game"
        self.bot._eq_profile_label.return_value = "Normal (Flat)"
        self.bot.water_muffle_enabled = True
        self.bot.reverb_enabled = True
        self.bot.crossfade_enabled = False
        self.bot.autotune_enabled = False
        self.bot.autotune_speed = autotune.DEFAULT_SPEED
        self.bot.autotune_amount = autotune.DEFAULT_AMOUNT
        self.bot.autotune_amount_label.return_value = autotune.amount_label()
        self.bot.autotune_label.return_value = "OFF"
        self.bot.autotune_speed_label.return_value = autotune.speed_label(
            autotune.DEFAULT_SPEED)
        self.bot.autotune_mode = autotune.DEFAULT_MODE
        self.bot.autotune_root = autotune.DEFAULT_ROOT
        self.bot.autotune_scale_label.return_value = autotune.scale_label(
            autotune.DEFAULT_MODE, autotune.DEFAULT_ROOT)
        self.bot._find_gameplay.return_value = SimpleNamespace(
            add_substate=lambda item: None, pop_last_substate=lambda: None)
        self.handler = menus_module.MusicBotMenus(self.bot)
        mock.patch("libs.menu.Menu", FakeMenu).start()
        mock.patch("libs.menus.set_default_sounds").start()
        self.speak = mock.patch("libs.music_bot.menus.speak").start()

    def settings_labels(self):
        # A line is (label, action[, preview]) with a description beside it; the
        # labels are what these tests read, whatever else the line carries.
        self.handler._open_settings_menu()
        return [item[0]() if callable(item[0]) else item[0]
                for item in FakeMenu.built[-1].items]

    def test_the_settings_menu_carries_the_switch_and_hides_the_dials(self):
        labels = self.settings_labels()
        self.assertIn("Auto-Tune: OFF", labels)
        self.assertEqual(labels.index("Auto-Tune: OFF"),
                         labels.index("Speech Key: Original key") + 1)
        # Off, the two dials are not in the list at all: they change nothing a
        # player can hear until the switch above them is on, and three
        # Auto-Tune lines that mostly do nothing bury the rest of the page.
        self.assertFalse([label for label in labels
                          if label.startswith(("Auto-Tune Scale",
                                               "Retune Speed",
                                               "Tune Amount"))])
        # The muffle stays last (pinned by the speech key's own suite too).
        self.assertEqual(labels[-2], "Underwater Muffle: ON")

    def test_turning_it_on_puts_the_three_dials_under_the_switch(self):
        self.bot.autotune_enabled = True
        self.bot.autotune_label.return_value = "ON"
        labels = self.settings_labels()
        self.assertEqual(labels.index("Auto-Tune: ON") + 1,
                         labels.index(f"Retune Speed: {autotune.speed_label()}"))
        self.assertEqual(labels.index(f"Retune Speed: {autotune.speed_label()}") + 1,
                         labels.index("Auto-Tune Scale: Chromatic"))
        self.assertEqual(labels.index("Auto-Tune Scale: Chromatic") + 1,
                         labels.index("Tune Amount: 100%"))

    def test_the_speech_line_says_who_is_in_charge_while_autotune_is_on(self):
        self.bot.autotune_enabled = True
        labels = self.settings_labels()
        self.assertIn("Speech Key: replaced by Auto-Tune", labels)
        index = labels.index("Speech Key: replaced by Auto-Tune")
        action = FakeMenu.built[-1].items[index][1]
        action()
        self.assertTrue(self.speak.called)
        self.assertIn("nothing to move", " ".join(
            str(call.args[0]) for call in self.speak.call_args_list))

    def test_the_switch_line_toggles_and_says_so(self):
        labels = self.settings_labels()
        index = labels.index("Auto-Tune: OFF")
        FakeMenu.built[-1].items[index][1]()
        self.assertTrue(self.bot.toggle_autotune.called)
        self.assertIn("Auto-Tune on", str(self.speak.call_args_list[-1].args[0]))

    def test_the_speed_menu_lists_every_speed_and_marks_the_current(self):
        self.handler._open_autotune_speed_menu()
        menu = FakeMenu.built[-1]
        labels = labels_in(menu)
        for index, (name, _glide, _note) in enumerate(autotune.SPEEDS):
            expected = f"{name} (active)" if index == autotune.DEFAULT_SPEED else name
            self.assertIn(expected, labels)
        self.assertEqual(menu.title,
                         f"Music Bot Retune Speed: {autotune.speed_label()}")
        index = labels.index(f"{autotune.SPEEDS[0][0]}")
        menu.items[index][1]()
        self.bot.set_autotune_speed.assert_called_with(0)

    def test_the_servers_gate_hides_both_lines(self):
        self.addCleanup(music_bot_gate.clear)
        music_bot_gate.apply_locked({"auto_tune": "Auto-Tune"})
        labels = self.settings_labels()
        self.assertNotIn("Auto-Tune: OFF", labels)
        self.assertNotIn(f"Retune Speed: {autotune.speed_label()}", labels)
        # The rest of the menu is untouched.
        self.assertIn("Speech Key: Original key", labels)
        self.assertIn("Underwater Muffle: ON", labels)


# ------------------------------------------------------------------ the scales


def midi_hz(midi):
    return 440.0 * 2.0 ** ((midi - 69.0) / 12.0)


class ScaleTests(unittest.TestCase):
    def test_chromatic_is_every_note_and_names_no_key(self):
        self.assertEqual(autotune.scale_classes("chromatic"), ())
        self.assertEqual(autotune.scale_label("chromatic", 0), "Chromatic")
        # A semitone grid is transposition-invariant: it cannot be in a key.
        self.assertEqual(autotune.scale_label("chromatic", 9), "Chromatic")

    def test_chromatic_answers_exactly_what_the_grid_answered(self):
        for hz in (82.4, 220.0, 311.13, 700.0):
            self.assertEqual(
                autotune.AutoTuner.scale_correction(hz, "chromatic", 0),
                autotune.AutoTuner.grid_correction(hz))
            self.assertEqual(
                autotune.AutoTuner.scale_correction(hz, "chromatic", 7),
                autotune.AutoTuner.grid_correction(hz))

    def test_every_answer_lands_on_a_note_of_its_scale(self):
        for mode, root in (("major", 0), ("minor", 9),
                           ("major_pentatonic", 0), ("minor_pentatonic", 2)):
            classes = {(root + offset) % 12
                       for offset in autotune.scale_classes(mode)}
            for midi in range(60, 72):
                for detune in (-0.45, -0.2, 0.0, 0.3, 0.45):
                    with self.subTest(mode=mode, root=root, midi=midi, detune=detune):
                        correction = autotune.AutoTuner.scale_correction(
                            midi_hz(midi + detune), mode, root)
                        landed = midi + detune + correction
                        self.assertAlmostEqual(landed, round(landed), places=6)
                        self.assertIn(int(round(landed)) % 12, classes)

    def test_a_seven_note_scale_moves_a_voice_by_one_semitone_at_most(self):
        for mode in ("major", "minor"):
            widest = max(
                abs(autotune.AutoTuner.scale_correction(midi_hz(m + step / 20.0),
                                                        mode, 0))
                for m in range(60, 72) for step in range(20))
            self.assertLessEqual(widest, 1.0 + 1e-9)
            self.assertAlmostEqual(widest, 1.0, delta=0.01)

    def test_a_five_note_scale_can_reach_a_semitone_and_a_half(self):
        """The widest gap in a pentatonic is three semitones, so 1.5 is the pull."""
        for mode in ("major_pentatonic", "minor_pentatonic"):
            widest = max(
                abs(autotune.AutoTuner.scale_correction(midi_hz(m + step / 20.0),
                                                        mode, 0))
                for m in range(60, 72) for step in range(20))
            self.assertLessEqual(widest, 1.5 + 1e-9)
            self.assertGreater(widest, 1.4)

    def test_the_harmonic_minor_is_the_minor_with_a_raised_seventh(self):
        self.assertEqual(autotune.scale_classes("harmonic_minor"),
                         (0, 2, 3, 5, 7, 8, 11))
        self.assertEqual(autotune.scale_label("harmonic_minor", 9),
                         "A Harmonic Minor")
        # Seven notes and a three-semitone gap (Ab to B), so the pull is
        # measured from the GAP and not from how many notes the scale has.
        self.assertEqual(autotune.widest_gap("harmonic_minor"), 3)
        self.assertEqual(autotune.widest_pull("harmonic_minor"), 1.5)

    def test_the_melodic_minor_raises_the_sixth_and_the_seventh(self):
        self.assertEqual(autotune.scale_classes("melodic_minor"),
                         (0, 2, 3, 5, 7, 9, 11))
        self.assertEqual(autotune.widest_gap("melodic_minor"), 2)
        self.assertEqual(autotune.widest_pull("melodic_minor"), 1.0)

    def test_every_scale_is_inside_the_ceiling_the_filter_was_pinned_at(self):
        """`MAX_PULL` is measured from the catalogue: a new scale cannot widen it."""
        self.assertEqual(autotune.MAX_PULL, 1.5)
        for mode_id, _label, _classes, _note in autotune.SCALES:
            with self.subTest(mode=mode_id):
                self.assertLessEqual(autotune.widest_pull(mode_id),
                                     autotune.MAX_PULL)

    def test_a_note_outside_the_harmonic_minor_lands_on_one_of_its_notes(self):
        # C harmonic minor is C D Eb F G Ab B: A natural is not in it, and is
        # one semitone above Ab (two below B), so it comes down onto Ab.
        self.assertAlmostEqual(
            autotune.AutoTuner.scale_correction(midi_hz(69),
                                                "harmonic_minor", 0), -1.0)
        # G# IS in the scale, and is left where it is.
        self.assertAlmostEqual(
            autotune.AutoTuner.scale_correction(midi_hz(68),
                                                "harmonic_minor", 0), 0.0)

    def test_the_harmonic_minor_never_pulls_more_than_a_semitone_and_a_half(self):
        widest = max(
            abs(autotune.AutoTuner.scale_correction(midi_hz(m + step / 20.0),
                                                    "harmonic_minor", 0))
            for m in range(60, 72) for step in range(20))
        self.assertLessEqual(widest, 1.5 + 1e-9)
        self.assertGreater(widest, 1.4)

    def test_every_scale_is_described_and_says_how_far_it_reaches(self):
        for mode_id, _label, _classes, note in autotune.SCALES:
            with self.subTest(mode=mode_id):
                self.assertTrue(note.strip(), f"{mode_id} has no description")
                self.assertEqual(autotune.scale_note(mode_id), note)
        self.assertIn("half a semitone", autotune.scale_says("chromatic"))
        self.assertIn("semitone at most", autotune.scale_says("major"))
        self.assertIn("semitone and a half",
                      autotune.scale_says("harmonic_minor"))

    def test_an_out_of_scale_note_takes_the_lower_neighbour(self):
        """D# in C major is exactly a semitone from D and from E."""
        self.assertAlmostEqual(
            autotune.AutoTuner.scale_correction(midi_hz(63), "major", 0), -1.0)
        # In C major pentatonic F# is one above G and two below E: up.
        self.assertAlmostEqual(
            autotune.AutoTuner.scale_correction(midi_hz(66), "major_pentatonic", 0),
            1.0)

    def test_a_scale_that_does_not_exist_is_the_default(self):
        self.assertEqual(autotune.clamp_mode("lydian"), autotune.DEFAULT_MODE)
        self.assertEqual(autotune.clamp_mode(None), autotune.DEFAULT_MODE)
        self.assertEqual(autotune.clamp_root(-1), 11)
        self.assertEqual(autotune.clamp_root(13), 1)
        self.assertEqual(autotune.clamp_root("nonsense"), autotune.DEFAULT_ROOT)
        self.assertEqual(autotune.root_name(6), "F#")
        self.assertEqual(autotune.scale_label("major", 9), "A Major")

    def test_the_tuner_follows_the_scale_it_is_set_to(self):
        d_sharp = midi_hz(63)          # just outside C major
        tuner = autotune.AutoTuner(mode="major", root=0)
        for chunk in frames_of(tone(0.4, freq=d_sharp)):
            tuner.track(chunk)
        self.assertAlmostEqual(tuner.offset, -1.0, delta=0.02)
        tuner.set_scale("chromatic", 0)
        for chunk in frames_of(tone(0.4, freq=d_sharp)):
            tuner.track(chunk)
        self.assertLess(abs(tuner.offset), 0.05)

    def test_the_amount_takes_a_share_and_never_widens_the_move(self):
        # Forty cents flat: the whole correction is +0.40 semitones on the
        # chromatic grid. Half of it is +0.20, and none of it is 0.0 -- not
        # None, which would mean "hold the last correction".
        flat = tone(0.6, cents=-40.0)
        tuner = autotune.AutoTuner()
        for chunk in frames_of(flat):
            tuner.track(chunk)
        full = tuner.offset
        self.assertGreater(full, 0.0)
        tuner.set_amount(50)
        for chunk in frames_of(flat):
            tuner.track(chunk)
        self.assertAlmostEqual(tuner.offset, full * 0.5, delta=0.01)
        tuner.set_amount(0)
        for chunk in frames_of(flat):
            tuner.track(chunk)
        self.assertEqual(tuner.offset, 0.0)
        # Still listening to the note: 0% is a share of zero, not a deaf tuner.
        self.assertIsNotNone(tuner.detected_hz)

    def test_the_amount_takes_any_whole_percent_and_no_more(self):
        # Typed values are kept, not snapped to a step: 63% is 63%.
        self.assertEqual(autotune.clamp_amount(63), 63)
        self.assertEqual(autotune.clamp_amount(250), 100)    # the note is the end
        self.assertEqual(autotune.clamp_amount(-5), 0)
        self.assertEqual(autotune.clamp_amount("rubbish"), 100)
        self.assertEqual(autotune.amount_label(), "100%")
        self.assertEqual(autotune.parse_amount("63"), 63)
        self.assertEqual(autotune.parse_amount(" 63% "), 63)
        self.assertIsNone(autotune.parse_amount("half"))
        self.assertIsNone(autotune.parse_amount("63.5"))


class ScaleLineTests(unittest.TestCase):
    def test_the_line_reads_the_scale_per_frame(self):
        line = AutoTuneLine(speed=0)
        with mock.patch.object(voice_chat.options, "get", options_on(
                **{AUTOTUNE_SPEED_OPTION: 0, AUTOTUNE_MODE_OPTION: "major",
                   AUTOTUNE_ROOT_OPTION: 9})):
            for chunk in frames_of(tone(0.6, freq=midi_hz(59.6))):
                line.shape(to_pcm(chunk))
        self.assertEqual((line.tuner.mode, line.tuner.root), ("major", 9))

    def test_the_line_reads_the_amount_per_frame(self):
        line = AutoTuneLine(speed=0)
        with mock.patch.object(voice_chat.options, "get", options_on(
                **{AUTOTUNE_SPEED_OPTION: 0, AUTOTUNE_AMOUNT_OPTION: 50})):
            for chunk in frames_of(tone(0.6, cents=-20.0)):
                line.shape(to_pcm(chunk))
        self.assertEqual(line.tuner.amount, 50)

    def test_a_note_outside_the_scale_is_moved_onto_it(self):
        d_sharp = midi_hz(63)          # D#4, one semitone above D
        line = AutoTuneLine(speed=0)
        with mock.patch.object(voice_chat.options, "get", options_on(
                **{AUTOTUNE_SPEED_OPTION: 0, AUTOTUNE_MODE_OPTION: "major",
                   AUTOTUNE_ROOT_OPTION: 0})):
            out = from_pcm(play(line, tone(3.0, freq=d_sharp)))
        heard = yin_pitch(out[SAMPLES * 10:SAMPLES * 40], min_freq=70.0,
                          max_freq=1000.0)
        self.assertLess(abs(cents_from(heard, midi_hz(62))), 10.0,
                        f"{heard:.2f} Hz is not D4")


class ScaleMenuTests(unittest.TestCase):
    setUp = MenuTests.setUp

    def settings_labels(self):
        return MenuTests.settings_labels(self)

    def test_the_settings_list_gains_one_line_for_all_of_it(self):
        self.bot.autotune_enabled = True
        self.bot.autotune_label.return_value = "ON"
        labels = self.settings_labels()
        self.assertIn("Auto-Tune Scale: Chromatic", labels)
        self.assertEqual(labels.index("Auto-Tune Scale: Chromatic"),
                         labels.index(f"Retune Speed: {autotune.speed_label()}") + 1)
        self.assertIn("Tune Amount: 100%", labels)
        self.assertEqual(labels.index("Tune Amount: 100%"),
                         labels.index("Auto-Tune Scale: Chromatic") + 1)
        # The list did not grow into five Auto-Tune lines: four is all of it.
        self.assertEqual(len([label for label in labels
                              if label.startswith(("Auto-Tune", "Retune",
                                                   "Tune Amount"))]), 4)

    def test_chromatic_offers_no_key_to_choose(self):
        self.handler._open_autotune_scale_menu()
        menu = FakeMenu.built[-1]
        self.assertEqual(menu.title, "Music Bot Auto-Tune Scale: Chromatic")
        labels = labels_in(menu)
        self.assertIn("Chromatic (active)", labels)
        self.assertIn("Major", labels)
        self.assertIn("Minor Pentatonic", labels)
        self.assertFalse([label for label in labels
                          if label.startswith("Root Note")])
        menu.items[labels.index("Major")][1]()
        self.bot.set_autotune_mode.assert_called_with("major")

    def test_choosing_a_scale_brings_the_key_row_with_it(self):
        # The key row exists only while the scale has a key. Choosing one
        # rebuilds the list, so the row is there to read on the next key press
        # instead of needing the menu reopened to be discovered.
        self.bot.autotune_mode = "chromatic"

        def remember(mode):
            self.bot.autotune_mode = mode

        self.bot.set_autotune_mode.side_effect = remember
        self.handler._open_autotune_scale_menu()
        labels = labels_in(FakeMenu.built[-1])
        self.assertFalse([label for label in labels
                          if label.startswith("Root Note")])
        FakeMenu.built[-1].items[labels.index("Major")][1]()
        relabelled = labels_in(FakeMenu.built[-1])
        self.assertEqual(relabelled[0], "Root Note: C")
        self.assertIn("Major (active)", relabelled)
        # ...and going back to Chromatic takes it away rather than leaving a
        # row that no longer means anything.
        FakeMenu.built[-1].items[relabelled.index("Chromatic")][1]()
        chromatically = labels_in(FakeMenu.built[-1])
        self.assertFalse([label for label in chromatically
                          if label.startswith("Root Note")])
        self.assertIn("Chromatic (active)", chromatically)

    def test_a_scale_with_a_key_offers_the_twelve_keys(self):
        self.bot.autotune_mode = "major"
        self.bot.autotune_root = 9
        self.bot.set_autotune_root.side_effect = lambda index: setattr(
            self.bot, "autotune_root", index)
        self.handler._open_autotune_scale_menu()
        menu = FakeMenu.built[-1]
        labels = labels_in(menu)
        self.assertIn("Root Note: A", labels)
        menu.items[labels.index("Root Note: A")][1]()
        keys = FakeMenu.built[-1]
        self.assertEqual(keys.title, "Music Bot Auto-Tune Root: A")
        names = labels_in(keys)
        self.assertIn("A (active)", names)
        self.assertEqual(len(names), 13)          # twelve keys and Back
        keys.items[names.index("F#")][1]()
        self.bot.set_autotune_root.assert_called_with(6)
        # The twelve keys stay open after one is picked, so the mark has to
        # move with the choice rather than wait for the menu to be reopened.
        moved = labels_in(keys)
        self.assertIn("F# (active)", moved)
        self.assertNotIn("A (active)", moved)

    def test_the_mark_moves_without_leaving_the_list(self):
        # Reported: choosing Major while Minor was marked left "Minor (active)"
        # on the list until Music Bot Settings was left and reopened. Both
        # scales are diatonic, so the list is not rebuilt, and the mark was a
        # plain string written when the list was built. It is read whenever the
        # line is spoken now, so the mark follows the choice with the list
        # standing exactly as it was.
        self.bot.autotune_mode = "minor"

        def remember(mode):
            self.bot.autotune_mode = mode

        self.bot.set_autotune_mode.side_effect = remember
        self.handler._open_autotune_scale_menu()
        menu = FakeMenu.built[-1]
        self.assertIn("Minor (active)", labels_in(menu))
        before = labels_in(menu)
        menu.items[before.index("Major")][1]()
        # The same list, still open: nothing was popped or rebuilt.
        self.assertIs(FakeMenu.built[-1], menu)
        after = labels_in(menu)
        self.assertIn("Major (active)", after)
        self.assertNotIn("Minor (active)", after)

    def test_the_gate_hides_the_switch_and_both_dials(self):
        # Hidden by the switch being off is not the same answer as locked: the
        # lock has to take all three away even while Auto-Tune is on.
        self.addCleanup(music_bot_gate.clear)
        self.bot.autotune_enabled = True
        self.bot.autotune_label.return_value = "ON"
        music_bot_gate.apply_locked({"auto_tune": "Auto-Tune"})
        labels = self.settings_labels()
        self.assertNotIn("Auto-Tune Scale: Chromatic", labels)
        self.assertNotIn(f"Retune Speed: {autotune.speed_label()}", labels)
        self.assertNotIn("Tune Amount: 100%", labels)
        self.assertNotIn("Auto-Tune: ON", labels)
        self.assertIn("Speech Key: replaced by Auto-Tune", labels)

    def test_the_amount_menu_lists_five_steps_and_marks_the_current(self):
        self.bot.autotune_amount = 100
        self.bot.set_autotune_amount.side_effect = (
            lambda percent: setattr(self.bot, "autotune_amount", percent))
        self.handler._open_autotune_amount_menu()
        menu = FakeMenu.built[-1]
        labels = labels_in(menu)
        # Five steps, the Custom Percentage row, and Back.
        self.assertEqual(len(labels), len(autotune.TUNE_AMOUNTS) + 2)
        for percent, _note in autotune.TUNE_AMOUNTS:
            expected = (f"{percent}% (active)" if percent == 100
                        else f"{percent}%")
            self.assertIn(expected, labels)
        menu.items[labels.index("50%")][1]()
        self.bot.set_autotune_amount.assert_called_with(50)
        # The mark follows the choice on the list that is still open.
        self.assertIn("50% (active)", labels_in(menu))

    def test_the_custom_row_takes_a_percentage_that_is_not_a_step(self):
        self.bot.autotune_amount = 100
        self.bot.set_autotune_amount.side_effect = (
            lambda percent: setattr(self.bot, "autotune_amount", percent))
        self.handler._open_autotune_amount_menu()
        menu = FakeMenu.built[-1]
        labels = labels_in(menu)
        menu.items[labels.index("Custom Percentage")][1]()
        run = self.bot.game.input.run
        self.assertTrue(run.called)
        kwargs = run.call_args.kwargs
        # The field itself refuses anything outside 0..100 while typing.
        self.assertEqual((kwargs.get("min_val"), kwargs.get("max_val")), (0, 100))
        kwargs["handeler"]("63")
        self.bot.set_autotune_amount.assert_called_with(63)
        self.assertIn("63%", str(self.speak.call_args_list[-1].args[0]))

    def test_a_typed_word_leaves_the_amount_alone(self):
        self.bot.autotune_amount = 75
        self.handler._open_autotune_amount_menu()
        labels = labels_in(FakeMenu.built[-1])
        FakeMenu.built[-1].items[labels.index("Custom Percentage")][1]()
        kwargs = self.bot.game.input.run.call_args.kwargs
        kwargs["handeler"]("half")
        self.assertFalse(self.bot.set_autotune_amount.called)
        self.assertIn("still 75%", str(self.speak.call_args_list[-1].args[0]))
        # Escape submits an empty string: the amount is untouched and says so.
        kwargs["handeler"]("")
        self.assertFalse(self.bot.set_autotune_amount.called)

    def test_clear_puts_every_setting_back_and_switches_it_off(self):
        self.bot.autotune_enabled = True
        self.bot.autotune_mode = "major"
        self.bot.autotune_root = 9
        self.bot.autotune_speed = len(autotune.SPEEDS) - 1
        self.handler._open_autotune_scale_menu()
        menu = FakeMenu.built[-1]
        labels = labels_in(menu)
        self.assertIn("Clear Auto-Tune Tuning", labels)
        menu.items[labels.index("Clear Auto-Tune Tuning")][1]()
        self.bot.set_autotune_mode.assert_called_with(autotune.DEFAULT_MODE)
        self.bot.set_autotune_root.assert_called_with(autotune.DEFAULT_ROOT)
        self.bot.set_autotune_speed.assert_called_with(autotune.DEFAULT_SPEED)
        self.bot.set_autotune_amount.assert_called_with(autotune.DEFAULT_AMOUNT)
        self.bot.toggle_autotune.assert_called_with()
        self.assertIn("cleared",
                      str(self.speak.call_args_list[-1].args[0]).lower())


if __name__ == "__main__":
    unittest.main()
