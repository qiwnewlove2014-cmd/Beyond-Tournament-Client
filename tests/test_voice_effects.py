"""The broadcast voice through a guitar amp -- pinned.

`libs/music_bot/voice_effects.py` is the pedal: a player with no guitar can still
put their *voice* through an amp, a fuzz, a small radio or a bitcrusher. Nothing
here is a note -- the words stay words.

What this file holds the feature to:

1. **A frame in is a frame out.** The PA's sequence, a cinema room's playout and
   a listener's decoder count 20 ms frames, so a pedal that changes the count is
   a pedal that desynchronises the room. Every preset must round-trip.
2. **The pedals do what their names say.** The amp drives a quiet voice; the
   cabinet takes the top off; the fuzz is dirtier than the amp; the gate closes
   between words; the crusher holds a sample; the clean preset carries a second
   copy. These are measured, not asserted from a table.
3. **Off is byte-for-byte the old voice**, and a preset changed mid-sentence is
   crossfaded, not cut -- a hard switch is a step, and a step is a click.
4. **The effect WRAPS the line the press already had.** Auto-Tune and Harmony
   keep working, the release still hands the inner line's look-ahead back, and
   the effect is read per frame so it can be chosen while speaking.
5. **Only the broadcast leg is shaped**, and the Developer's `voice_fx` lock
   silences a preset a player already saved.

No game, OpenAL, ffmpeg or network is required.
"""

import os
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from libs import voice_chat  # noqa: E402
from libs.music_bot import MapMusicBot  # noqa: E402
from libs.music_bot import voice_effects  # noqa: E402

SR = voice_effects.SAMPLE_RATE
FRAME = voice_effects.FRAME_BYTES          # one 20 ms mono16 frame


# --------------------------------------------------------------- the signals


def tone(freq=220.0, amplitude=0.4, seconds=1.0):
    t = np.arange(int(SR * seconds)) / SR
    return (amplitude * np.sin(2.0 * np.pi * freq * t)).astype(np.float32)


def noise(amplitude=0.2, seconds=1.0, seed=7):
    rng = np.random.default_rng(seed)
    return (rng.uniform(-1.0, 1.0, int(SR * seconds)) * amplitude).astype(
        np.float32)


def to_pcm(signal):
    return np.clip(signal * 32768.0, -32768, 32767).astype("<i2").tobytes()


def from_pcm(data):
    return np.frombuffer(bytes(data), dtype="<i2").astype(np.float64) / 32768.0


def play(processor, pcm):
    """Push a whole signal through a processor, frame by frame."""
    out = bytearray()
    for start in range(0, len(pcm) - FRAME + 1, FRAME):
        shaped = processor.process(pcm[start:start + FRAME])
        assert len(shaped) == FRAME, "the pedal changed a frame's size"
        out += shaped
    return from_pcm(out)


def rms(samples):
    return float(np.sqrt(np.mean(np.square(samples))))


def harmonics(samples, fundamental=220.0, count=6):
    """Energy in the harmonics over the energy in the fundamental."""
    window = np.hanning(len(samples))
    spectrum = np.abs(np.fft.rfft(samples * window)) ** 2
    bin_hz = SR / len(samples)

    def energy(freq):
        index = int(round(freq / bin_hz))
        return float(spectrum[max(0, index - 2):index + 3].sum())

    return sum(energy(fundamental * k) for k in range(2, count + 1)) / max(
        1e-12, energy(fundamental))


def high_share(samples, corner=6000.0):
    window = np.hanning(len(samples))
    spectrum = np.abs(np.fft.rfft(samples * window)) ** 2
    freqs = np.fft.rfftfreq(len(samples), 1.0 / SR)
    return float(spectrum[freqs >= corner].sum() / max(1e-12, spectrum.sum()))


# --------------------------------------------------------------- the catalogue


class TheCatalogueTests(unittest.TestCase):
    def test_off_is_the_default_and_comes_first(self):
        self.assertEqual(voice_effects.VOICE_EFFECTS[0][0], "off")
        self.assertEqual(voice_effects.DEFAULT_EFFECT, "off")
        self.assertEqual(voice_effects.effect_label(None), "Off")
        self.assertEqual(voice_effects.effect_label("nonsense"), "Off")

    def test_every_preset_has_a_short_name_and_a_description(self):
        ids = [effect_id for effect_id, _label, _note in
               voice_effects.VOICE_EFFECTS]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertIn("amp", ids)
        self.assertIn("clean", ids)
        self.assertIn("fuzz", ids)
        self.assertIn("radio", ids)
        self.assertIn("crush", ids)
        for effect_id in ("octave", "tremolo", "wah", "phaser", "echo",
                          "robot"):
            self.assertIn(effect_id, ids)
        for effect_id, label, note in voice_effects.VOICE_EFFECTS:
            with self.subTest(preset=effect_id):
                self.assertTrue(label.strip())
                # Short enough to arrow past; the description is Tab's job.
                self.assertLessEqual(len(label), 20)
                self.assertGreater(len(note), 60)

    def test_an_unknown_id_is_clamped_to_off(self):
        self.assertEqual(voice_effects.clamp_effect("wobble"), "off")
        self.assertEqual(voice_effects.clamp_effect("AMP"), "off")
        self.assertEqual(voice_effects.clamp_effect("fuzz"), "fuzz")

    def test_the_developer_can_take_a_saved_preset_away(self):
        with mock.patch.object(voice_effects.options, "get",
                               side_effect=lambda key, default=None: "amp"):
            with mock.patch.object(voice_effects.gate, "is_locked",
                                   return_value=False):
                self.assertEqual(voice_effects.effect_id(), "amp")
                self.assertTrue(voice_effects.enabled())
            with mock.patch.object(voice_effects.gate, "is_locked",
                                   return_value=True):
                self.assertEqual(voice_effects.effect_id(), "off")
                self.assertFalse(voice_effects.enabled())

    def test_the_setting_is_persisted_and_clamped(self):
        bot = MapMusicBot.__new__(MapMusicBot)
        with mock.patch.object(voice_effects.options, "set") as saved:
            self.assertEqual(bot.set_voice_effect("fuzz"), "fuzz")
            self.assertEqual(bot.voice_effect, "fuzz")
            saved.assert_called_once_with(voice_effects.OPTION, "fuzz")
            self.assertEqual(bot.set_voice_effect("not a pedal"), "off")
            self.assertEqual(bot.voice_effect_label(), "Off")


# ---------------------------------------------------------------- the pedals


class TheProcessorTests(unittest.TestCase):
    def processor(self, preset):
        return voice_effects._EffectProcessor(preset)

    def test_a_frame_in_is_a_frame_out(self):
        pcm = to_pcm(tone(seconds=0.5))
        for preset in ("amp", "clean", "fuzz", "radio", "crush", "octave",
                       "tremolo", "wah", "phaser", "echo", "robot"):
            with self.subTest(preset=preset):
                processor = self.processor(preset)
                for start in range(0, len(pcm) - FRAME + 1, FRAME):
                    self.assertEqual(
                        len(processor.process(pcm[start:start + FRAME])), FRAME)

    def test_a_silent_frame_stays_silent(self):
        silence = bytes(FRAME)
        for preset in ("amp", "clean", "fuzz", "radio", "crush", "octave",
                       "tremolo", "wah", "phaser", "echo", "robot"):
            with self.subTest(preset=preset):
                self.assertEqual(
                    self.processor(preset).process(silence), silence)

    def test_the_amp_drives_a_quiet_voice(self):
        """A whisper into the amp comes out a shout, not a whisper."""
        quiet = to_pcm(tone(amplitude=0.05, seconds=0.5))
        before = from_pcm(quiet)
        after = play(self.processor("amp"), quiet)
        # Skip the first frames: the gate has to open and the cabinet has to
        # fill. The steady middle is what a listener hears.
        middle = slice(FRAME * 4, FRAME * 20)
        self.assertGreater(rms(after[middle]), 3.0 * rms(before[middle]))

    def test_the_cabinet_takes_the_top_off(self):
        signal = to_pcm(noise(amplitude=0.15, seconds=0.5))
        before = from_pcm(signal)
        after = play(self.processor("amp"), signal)
        self.assertLess(high_share(after), 0.5 * high_share(before))

    def test_the_fuzz_is_dirtier_than_the_amp(self):
        signal = to_pcm(tone(amplitude=0.2, seconds=0.5))
        amp = play(self.processor("amp"), signal)
        fuzz = play(self.processor("fuzz"), signal)
        middle = slice(FRAME * 4, FRAME * 20)
        self.assertGreater(harmonics(fuzz[middle]), harmonics(amp[middle]))

    def test_the_gate_closes_between_words(self):
        """A room's noise floor does not come out of the amp as a hiss."""
        quiet = to_pcm(noise(amplitude=0.003, seconds=0.6))
        after = play(self.processor("amp"), quiet)
        # Eight frames to close, then it is shut: what is left is the filter
        # tail, well under the noise that went in.
        self.assertLess(rms(after[-FRAME * 4:]), rms(from_pcm(quiet)) * 0.1)

    def test_the_crusher_holds_each_sample(self):
        processor = self.processor("crush")
        ramp = np.linspace(-0.5, 0.5, FRAME, dtype=np.float32)
        held = processor._crush_block(ramp)
        self.assertEqual(len(held), FRAME)
        # 8 kHz hold at 48 kHz is six samples of each value; the 6-bit
        # quantiser keeps most neighbours equal too.
        equal_pairs = np.count_nonzero(np.diff(held) == 0.0)
        self.assertGreater(equal_pairs, FRAME * 0.5)

    def test_the_clean_preset_carries_a_second_copy(self):
        """The chorus's doubled voice, measured as correlation at its delay.

        A sine at the 12 ms base delay is the honest probe: a swept delay
        decorrelates broadband noise (which is why a chorus sounds wide), but a
        periodic signal still lines up with its own copy.
        """
        signal = to_pcm(tone(freq=1000.0 / 12.0, amplitude=0.3, seconds=0.1))
        after = play(self.processor("clean"), signal)
        samples = after[FRAME:FRAME * 4]
        samples = samples - samples.mean()
        lag = int(round(0.012 * SR))
        direct = float(np.dot(samples[:-lag], samples[lag:]) /
                       max(1e-12, np.dot(samples, samples)))
        self.assertGreater(direct, 0.5)

    def test_no_preset_ever_leaves_full_scale(self):
        loud = to_pcm(tone(amplitude=0.95, seconds=0.5))
        for preset in ("amp", "clean", "fuzz", "radio", "crush", "octave",
                       "tremolo", "wah", "phaser", "echo", "robot"):
            with self.subTest(preset=preset):
                after = play(self.processor(preset), loud)
                self.assertLessEqual(float(np.max(np.abs(after))), 1.0)
                self.assertFalse(np.any(np.isnan(after)))


class TheAddOnPedalsTests(unittest.TestCase):
    """The pedals that are one block each: what they add is what is measured."""

    def test_the_octave_adds_the_octave(self):
        signal = to_pcm(tone(amplitude=0.2, seconds=0.5))
        after = play(voice_effects._EffectProcessor("octave"), signal)
        samples = after[FRAME * 4:]
        window = np.hanning(len(samples))
        spectrum = np.abs(np.fft.rfft(samples * window)) ** 2
        freqs = np.fft.rfftfreq(len(samples), 1.0 / SR)

        def energy(hz):
            return float(spectrum[(freqs > hz - 15) & (freqs < hz + 15)].sum())

        # A clean voice has almost nothing at the second harmonic; this pedal
        # is there for that note.
        self.assertGreater(energy(440.0), 0.15 * energy(220.0))
        dry = from_pcm(signal)[FRAME * 4:]
        self.assertFalse(np.allclose(samples, dry))

    def test_the_tremolo_pulses_at_its_rate(self):
        signal = to_pcm(tone(amplitude=0.2, seconds=1.0))
        after = play(voice_effects._EffectProcessor("tremolo"), signal)
        samples = after[FRAME * 2:]
        # A 5 ms RMS envelope, then its own spectrum: the pulse rate is what a
        # listener hears as the speed, not a number in the spec.
        powers = samples ** 2
        window = max(1, int(0.005 * SR))
        envelope = np.sqrt(np.convolve(powers, np.ones(window) / window,
                                       mode="valid"))
        # The pulse itself: about a tenth of full level at the bottom.
        self.assertLess(float(envelope.min()), 0.35 * float(envelope.max()))
        centred = envelope - envelope.mean()
        spectrum = np.abs(np.fft.rfft(centred * np.hanning(len(centred))))
        rate = float(np.argmax(spectrum)) * SR / len(envelope)
        self.assertGreater(rate, 4.0)
        self.assertLess(rate, 7.0)

    def test_the_wah_is_a_band_pass(self):
        signal = to_pcm(noise(amplitude=0.15, seconds=1.0))
        after = play(voice_effects._EffectProcessor("wah"), signal)
        samples = after[FRAME * 4:]
        window = np.hanning(len(samples))
        spectrum = np.abs(np.fft.rfft(samples * window)) ** 2
        freqs = np.fft.rfftfreq(len(samples), 1.0 / SR)
        total = spectrum.sum()
        self.assertLess(float(spectrum[freqs < 200.0].sum()) / total, 0.02)
        self.assertLess(float(spectrum[freqs > 4000.0].sum()) / total, 0.02)

    def test_the_phaser_mixes_a_swept_copy(self):
        signal = to_pcm(tone(amplitude=0.2, seconds=0.5))
        after = play(voice_effects._EffectProcessor("phaser"), signal)
        dry = from_pcm(signal)
        difference = after[FRAME * 4:] - dry[FRAME * 4:]
        self.assertGreater(rms(difference), 0.5 * rms(dry[FRAME * 4:]))

    def test_the_echo_repeats_at_its_delay(self):
        burst = np.zeros(SR, dtype=np.float32)
        burst[:FRAME // 2] = 0.5
        after = play(voice_effects._EffectProcessor("echo"), to_pcm(burst))
        first = rms(after[int(0.12 * SR):int(0.18 * SR)])
        second = rms(after[int(0.26 * SR):int(0.32 * SR)])
        self.assertGreater(first, 0.005)
        self.assertGreater(second, 0.001)
        self.assertLess(second, first)

    def test_the_robot_puts_sidebands_around_the_voice(self):
        signal = to_pcm(tone(amplitude=0.2, seconds=0.5))
        after = play(voice_effects._EffectProcessor("robot"), signal)
        samples = after[FRAME * 4:]
        window = np.hanning(len(samples))
        spectrum = np.abs(np.fft.rfft(samples * window)) ** 2
        freqs = np.fft.rfftfreq(len(samples), 1.0 / SR)

        def energy(hz):
            return float(spectrum[(freqs > hz - 20) & (freqs < hz + 20)].sum())

        # 220 multiplied by a 55 Hz carrier: 165 and 275 appear, and the
        # original pitch is reduced rather than doubled.
        self.assertGreater(energy(275.0), 2.0 * energy(220.0))


# ------------------------------------------------------------------- the line


class _InnerLine:
    """A stand-in for AutoTuneLine / FastHarmonyLine / SpeechKeyLine."""

    def __init__(self, semitones=0):
        self.semitones = semitones
        self.frames = 0
        self.released = 0

    def set_semitones(self, semitones):
        self.semitones = semitones
        return semitones

    def shape(self, pcm):
        self.frames += 1
        return bytes(pcm)

    def tail_frames(self, frame_bytes=FRAME):
        self.released += 1
        return [bytes(frame_bytes)]


class TheLineTests(unittest.TestCase):
    def test_off_returns_the_frame_untouched(self):
        line = voice_effects.VoiceEffectLine(None)
        frame = to_pcm(tone(seconds=0.02))
        with mock.patch.object(voice_effects, "effect_id", return_value="off"):
            self.assertEqual(line.shape(frame), frame)

    def test_the_preset_is_read_every_frame(self):
        """A preset chosen mid-sentence lands on the words already leaving."""
        line = voice_effects.VoiceEffectLine(None)
        frame = to_pcm(tone(seconds=0.02))
        with mock.patch.object(voice_effects, "effect_id", return_value="off"):
            dry = line.shape(frame)
        with mock.patch.object(voice_effects, "effect_id", return_value="amp"):
            wet = line.shape(frame)
        with mock.patch.object(voice_effects, "effect_id", return_value="fuzz"):
            fuzz = line.shape(frame)
        self.assertEqual(dry, frame)
        self.assertNotEqual(wet, dry)
        self.assertNotEqual(fuzz, wet)

    def test_turning_the_effect_off_crossfades_instead_of_cutting(self):
        line = voice_effects.VoiceEffectLine(None)
        frame = to_pcm(tone(amplitude=0.4, seconds=0.02))
        with mock.patch.object(voice_effects, "effect_id", return_value="amp"):
            line.shape(frame)
            wet = line.shape(frame)
        with mock.patch.object(voice_effects, "effect_id", return_value="off"):
            faded = line.shape(frame)
        # The first samples are still (mostly) the processed voice; later in
        # the frame the dry voice is arriving. A hard cut would be dry from its
        # first sample, and a click.
        first_gap = float(np.sqrt(np.mean(
            (from_pcm(faded[:FRAME // 4]) - from_pcm(frame[:FRAME // 4])) ** 2)))
        last_gap = float(np.sqrt(np.mean(
            (from_pcm(faded[-FRAME // 4:]) - from_pcm(frame[-FRAME // 4:])) ** 2)))
        self.assertNotEqual(faded[:8], frame[:8])
        self.assertLess(last_gap, first_gap * 0.5)
        self.assertNotEqual(faded, wet)

    def test_it_keeps_the_inner_lines_interval(self):
        inner = _InnerLine(semitones=7)
        line = voice_effects.VoiceEffectLine(inner)
        self.assertEqual(line.semitones, 7)
        self.assertEqual(line.set_semitones(-2), -2)
        self.assertEqual(inner.semitones, -2)

    def test_the_inner_line_still_shapes_first(self):
        inner = _InnerLine()
        line = voice_effects.VoiceEffectLine(inner)
        frame = to_pcm(tone(seconds=0.02))
        with mock.patch.object(voice_effects, "effect_id", return_value="off"):
            line.shape(frame)
        self.assertEqual(inner.frames, 1)

    def test_the_release_hands_the_look_ahead_back(self):
        inner = _InnerLine()
        line = voice_effects.VoiceEffectLine(inner)
        with mock.patch.object(voice_effects, "effect_id", return_value="amp"):
            frames = line.tail_frames()
        self.assertEqual(inner.released, 1)
        self.assertEqual(len(frames), 1)
        self.assertEqual(len(frames[0]), FRAME)

    def test_off_releases_the_dry_look_ahead(self):
        inner = _InnerLine()
        line = voice_effects.VoiceEffectLine(inner)
        with mock.patch.object(voice_effects, "effect_id", return_value="off"):
            frames = line.tail_frames()
        self.assertEqual(frames, [bytes(FRAME)])


# --------------------------------------------------------------- the voice path


class TheVoicePathTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(voice_chat, "speech_key_semitones",
                          return_value=0).start()
        mock.patch.object(voice_chat, "autotune_enabled",
                          return_value=False).start()
        mock.patch.object(voice_chat, "harmony_enabled",
                          return_value=False).start()

    def press(self):
        return SimpleNamespace(_speech_line=None)

    def test_only_the_broadcast_leg_is_shaped(self):
        with mock.patch.object(voice_effects, "effect_id",
                               return_value="amp"):
            self.assertIsNone(
                voice_chat.VoiceChatRecord._speech_line_for(self.press(),
                                                            mega=False))

    def test_an_effect_with_no_other_line_is_the_whole_line(self):
        with mock.patch.object(voice_effects, "effect_id",
                               return_value="amp"):
            line = voice_chat.VoiceChatRecord._speech_line_for(
                self.press(), mega=True)
        self.assertIsInstance(line, voice_effects.VoiceEffectLine)
        self.assertIsNone(line.inner)

    def test_off_and_no_other_line_builds_no_line_at_all(self):
        with mock.patch.object(voice_effects, "effect_id", return_value="off"):
            self.assertIsNone(
                voice_chat.VoiceChatRecord._speech_line_for(self.press(),
                                                            mega=True))

    def test_the_effect_wraps_autotune_rather_than_replacing_it(self):
        mock.patch.object(voice_chat, "autotune_enabled",
                          return_value=True).start()
        with mock.patch.object(voice_effects, "effect_id",
                               return_value="amp"):
            line = voice_chat.VoiceChatRecord._speech_line_for(
                self.press(), mega=True)
        self.assertIsInstance(line, voice_effects.VoiceEffectLine)
        self.assertIsInstance(line.inner, voice_chat.AutoTuneLine)

    def test_a_preset_chosen_mid_press_wraps_the_running_line(self):
        key_line = voice_chat.SpeechKeyLine(semitones=2)
        record = SimpleNamespace(_speech_line=key_line)
        with mock.patch.object(voice_effects, "effect_id", return_value="off"):
            self.assertIs(
                voice_chat.VoiceChatRecord._speech_line_for(record, mega=True),
                key_line)
        with mock.patch.object(voice_effects, "effect_id",
                               return_value="fuzz"):
            wrapped = voice_chat.VoiceChatRecord._speech_line_for(record,
                                                                  mega=True)
        self.assertIsInstance(wrapped, voice_effects.VoiceEffectLine)
        self.assertIs(wrapped.inner, key_line)
        self.assertIs(record._speech_line, wrapped)


if __name__ == "__main__":
    unittest.main()
