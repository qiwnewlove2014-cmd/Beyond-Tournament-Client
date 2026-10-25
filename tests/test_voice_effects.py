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
6. **The Custom Voice board's fifty starting points are sounds, not labels.**
   Every recipe is a value the ten slots offer, no two recipes are the same
   voice, and each one plays -- a frame in, a frame out, audible and under
   the ceiling.

No game, OpenAL, ffmpeg or network is required.
"""

import os
import sys
import time
import unittest
from types import SimpleNamespace
from unittest import mock

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from libs import voice_chat  # noqa: E402
from libs.music_bot import MapMusicBot  # noqa: E402
from libs.music_bot import voice_effect_builder  # noqa: E402
from libs.music_bot import voice_effect_catalogue  # noqa: E402
from libs.music_bot import voice_effect_custom  # noqa: E402
from libs.music_bot import voice_effect_recipes  # noqa: E402
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


def band_share(samples, low, high):
    """The share of a signal's energy between two frequencies."""
    window = np.hanning(len(samples))
    spectrum = np.abs(np.fft.rfft(samples * window)) ** 2
    freqs = np.fft.rfftfreq(len(samples), 1.0 / SR)
    inside = (freqs >= low) & (freqs <= high)
    return float(spectrum[inside].sum() / max(1e-12, spectrum.sum()))


def tone_share(samples, hz, width=10.0):
    """The share of the energy sitting on one frequency."""
    window = np.hanning(len(samples))
    spectrum = np.abs(np.fft.rfft(samples * window)) ** 2
    freqs = np.fft.rfftfreq(len(samples), 1.0 / SR)
    inside = (freqs >= hz - width) & (freqs <= hz + width)
    return float(spectrum[inside].sum() / max(1e-12, spectrum.sum()))


def harmonic_ratio(samples, fundamental=220.0, other=440.0):
    """How loud one partial is against another: the octave, in one number."""
    return tone_share(samples, other) / max(1e-12,
                                             tone_share(samples, fundamental))


def saw(amplitude=0.3, seconds=1.0, freq=180.0):
    t = np.arange(int(SR * seconds)) / SR
    return (amplitude * (2.0 * ((t * freq) % 1.0) - 1.0)).astype(np.float32)


def peak_freq(samples, floor=80.0):
    spectrum = np.abs(np.fft.rfft(samples * np.hanning(len(samples))))
    freqs = np.fft.rfftfreq(len(samples), 1.0 / SR)
    above = freqs > floor
    return float(freqs[above][int(np.argmax(spectrum[above]))])


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
                          "robot", "flanger", "vibrato", "stutter", "cave",
                          "alien", "sub_octave", "envelope_wah", "gate",
                          "reverse", "chorus", "vinyl", "body", "shimmer",
                          "whisper", "growl", "doubler", "vocoder",
                          "talkbox", "hyperpop", "tapestop", "cloud",
                          "air", "pump", "freeze", "step", "swell",
                          "crunch", "overdrive", "boost", "lead", "modern",
                          "tweed", "brown", "hotrod", "chime", "sag",
                          "direct", "acoustic", "jazz", "room_cab", "doom",
                          "stoner", "velcro", "octavia", "fuzzsub",
                          "twelve", "custom", "voice_build"):
            self.assertIn(effect_id, ids)
        for effect_id, label, note in voice_effects.VOICE_EFFECTS:
            with self.subTest(preset=effect_id):
                self.assertTrue(label.strip())
                # Short enough to arrow past; the description is Tab's job.
                self.assertLessEqual(len(label), 20)
                self.assertGreater(len(note), 60)

    def test_a_standalone_board_is_reachable_and_in_a_family_is_an_error(self):
        """A row on the top page is reachable without belonging to a family."""
        self.assertIn(voice_effects.BUILDER_EFFECT,
                      voice_effects.STANDALONE_EFFECTS)
        self.assertIsNone(voice_effects.group_of(voice_effects.BUILDER_EFFECT))
        self.assertEqual(voice_effects.catalogue_problems(), [])
        with mock.patch.object(voice_effect_catalogue, "STANDALONE_EFFECTS",
                               ()):
            # ...and if the catalogue ever loses the line, the check says so.
            problems = voice_effects.catalogue_problems()
        self.assertIn("no family holds it: voice_build", problems)

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
                       "tremolo", "wah", "phaser", "echo", "robot",
                       "flanger", "vibrato", "stutter", "cave", "alien",
                       "sub_octave", "envelope_wah", "gate", "reverse",
                       "chorus", "vinyl", "body", "shimmer", "whisper",
                       "growl", "doubler", "vocoder", "talkbox", "hyperpop",
                       "tapestop", "cloud", "air", "pump", "freeze",
                       "step", "swell", "tube", "presence", "plate",
                       "gated", "throw", "dub", "wash", "choir", "riser",
                       "scream", "crunch", "overdrive", "boost", "lead",
                       "modern", "tweed", "brown", "hotrod", "chime", "sag",
                       "direct", "acoustic", "jazz", "room_cab", "doom",
                       "stoner", "velcro", "octavia", "fuzzsub", "twelve",
                       "custom", "voice_build"):
            with self.subTest(preset=preset):
                processor = self.processor(preset)
                for start in range(0, len(pcm) - FRAME + 1, FRAME):
                    self.assertEqual(
                        len(processor.process(pcm[start:start + FRAME])), FRAME)

    def test_a_silent_frame_stays_silent(self):
        silence = bytes(FRAME)
        for preset in ("amp", "clean", "fuzz", "radio", "crush", "octave",
                       "tremolo", "wah", "phaser", "echo", "robot",
                       "flanger", "vibrato", "stutter", "cave", "alien",
                       "sub_octave", "envelope_wah", "gate", "reverse",
                       "chorus", "vinyl", "body", "shimmer", "whisper",
                       "growl", "doubler", "vocoder", "talkbox", "hyperpop",
                       "tapestop", "cloud", "air", "pump", "freeze",
                       "step", "swell", "tube", "presence", "plate",
                       "gated", "throw", "dub", "wash", "choir", "riser",
                       "scream", "crunch", "overdrive", "boost", "lead",
                       "modern", "tweed", "brown", "hotrod", "chime", "sag",
                       "direct", "acoustic", "jazz", "room_cab", "doom",
                       "stoner", "velcro", "octavia", "fuzzsub", "twelve",
                       "custom", "voice_build"):
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
                       "tremolo", "wah", "phaser", "echo", "robot",
                       "flanger", "vibrato", "stutter", "cave", "alien",
                       "sub_octave", "envelope_wah", "gate", "reverse",
                       "chorus", "vinyl", "body", "shimmer", "whisper",
                       "growl", "doubler", "vocoder", "talkbox", "hyperpop",
                       "tapestop", "cloud", "air", "pump", "freeze",
                       "step", "swell", "tube", "presence", "plate",
                       "gated", "throw", "dub", "wash", "choir", "riser",
                       "scream", "crunch", "overdrive", "boost", "lead",
                       "modern", "tweed", "brown", "hotrod", "chime", "sag",
                       "direct", "acoustic", "jazz", "room_cab", "doom",
                       "stoner", "velcro", "octavia", "fuzzsub", "twelve",
                       "custom", "voice_build"):
            with self.subTest(preset=preset):
                after = play(self.processor(preset), loud)
                # The ceiling, not full scale: a pedal hands the PA and a cinema
                # room's effect sends 3 dB of headroom (see PEAK_CEILING).
                self.assertLessEqual(float(np.max(np.abs(after))),
                                     voice_effects.PEAK_CEILING + 1e-3)
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

    def test_the_flanger_mixes_a_swept_short_copy(self):
        signal = to_pcm(tone(amplitude=0.2, seconds=0.5))
        after = play(voice_effects._EffectProcessor("flanger"), signal)
        dry = from_pcm(signal)[FRAME * 4:]
        difference = after[FRAME * 4:] - dry
        self.assertGreater(rms(difference), 0.25 * rms(dry))

    def test_the_vibrato_moves_the_pitch(self):
        """A delay that only wobbles makes the pitch rise and fall."""
        signal = to_pcm(tone(freq=1000.0, amplitude=0.2, seconds=1.0))
        after = play(voice_effects._EffectProcessor("vibrato"), signal)
        window = int(0.1 * SR)
        rates = []
        for start in range(FRAME * 2, len(after) - window, window):
            segment = after[start:start + window]
            crossings = np.count_nonzero(np.diff(np.signbit(segment)))
            rates.append(crossings / (2.0 * window / SR))
        self.assertGreater(max(rates) / min(rates), 1.08)

    def test_the_stutter_repeats_what_was_just_said(self):
        # Five frames of a tone, then silence: the repeat window must still
        # carry the tone, and the dry window after it must be silent.
        signal = np.zeros(SR, dtype=np.float32)
        half = FRAME // 2
        signal[:half] = (0.4 * np.sin(2.0 * np.pi * 220.0
                                       * np.arange(half) / SR)).astype(np.float32)
        after = play(voice_effects._EffectProcessor("stutter"),
                     to_pcm(signal))
        self.assertGreater(rms(after[int(0.10 * SR):int(0.20 * SR)]), 0.02)
        self.assertLess(rms(after[int(0.20 * SR):int(0.30 * SR)]), 0.001)

    def test_the_cave_rings_after_the_voice_stops(self):
        burst = np.zeros(SR, dtype=np.float32)
        burst[:240] = 0.5
        after = play(voice_effects._EffectProcessor("cave"), to_pcm(burst))
        early = rms(after[int(0.08 * SR):int(0.12 * SR)])
        late = rms(after[int(0.20 * SR):int(0.30 * SR)])
        self.assertGreater(early, 0.002)
        self.assertGreater(early, late)

    def test_the_alien_slides_its_sidebands(self):
        # One second, because the carrier's own sweep is 0.7 Hz: half a second
        # has not visited enough of the slide for the sidebands to appear.
        signal = to_pcm(tone(freq=1000.0, amplitude=0.2, seconds=1.0))
        after = play(voice_effects._EffectProcessor("alien"), signal)
        samples = after[FRAME * 4:]
        window = np.hanning(len(samples))
        spectrum = np.abs(np.fft.rfft(samples * window)) ** 2
        freqs = np.fft.rfftfreq(len(samples), 1.0 / SR)

        def energy(low, high):
            return float(spectrum[(freqs > low) & (freqs < high)].sum())

        self.assertGreater(energy(650.0, 750.0),
                           0.2 * energy(980.0, 1020.0))
        self.assertGreater(energy(1250.0, 1350.0),
                           0.2 * energy(980.0, 1020.0))


class TheSecondWaveOfPedalsTests(unittest.TestCase):
    """The last six: a divider, an envelope, a chop, a reverse, two rooms."""

    @staticmethod
    def bands(samples):
        window = np.hanning(len(samples))
        spectrum = np.abs(np.fft.rfft(samples * window)) ** 2
        freqs = np.fft.rfftfreq(len(samples), 1.0 / SR)

        def energy(low, high):
            return float(spectrum[(freqs > low) & (freqs < high)].sum())

        return energy

    def test_the_sub_octave_adds_the_note_below(self):
        signal = to_pcm(tone(amplitude=0.2, seconds=0.5))
        after = play(voice_effects._EffectProcessor("sub_octave"), signal)
        energy = self.bands(after[FRAME * 4:])
        # The divider makes a square one octave down: 110 Hz, not a copy of
        # the 220 Hz dry voice.
        self.assertGreater(energy(100.0, 120.0), 2.0 * energy(210.0, 230.0))

    def test_the_envelope_wah_follows_the_voice(self):
        rng = np.random.default_rng(4)
        ratios = {}
        for name, amplitude in (("quiet", 0.02), ("loud", 0.2)):
            signal = (rng.uniform(-1.0, 1.0, SR) * amplitude).astype(np.float32)
            after = play(voice_effects._EffectProcessor("envelope_wah"),
                         to_pcm(signal))
            energy = self.bands(after[FRAME * 8:])
            ratios[name] = (energy(1100.0, 2200.0)
                            / max(1e-12, energy(250.0, 600.0)))
        # A quiet voice sits low; a loud one opens the filter far above it.
        self.assertGreater(ratios["loud"], 5.0 * ratios["quiet"])

    def test_the_gate_chops_the_voice(self):
        signal = to_pcm(tone(amplitude=0.2, seconds=1.0))
        after = play(voice_effects._EffectProcessor("gate"), signal)
        samples = after[FRAME * 4:]
        window = int(0.005 * SR)
        envelope = np.sqrt(np.convolve(samples ** 2,
                                       np.ones(window) / window,
                                       mode="valid"))
        self.assertLess(float(envelope.min()), 0.1 * float(envelope.max()))

    def test_the_reverse_plays_the_slice_backwards(self):
        # Five frames, each a tone burst louder than the one before it. The
        # reversed slice must start with the LOUDEST frame and end with the
        # quietest, which is what makes it a reverse and not a stutter.
        levels = (0.1, 0.2, 0.3, 0.4, 0.5)
        frames = []
        for level in levels:
            frames.append(level * np.sin(2.0 * np.pi * 220.0
                                         * np.arange(FRAME // 2) / SR))
        signal = np.concatenate(frames).astype(np.float32)
        silence = np.zeros(SR - len(signal), dtype=np.float32)
        after = play(voice_effects._EffectProcessor("reverse"),
                     to_pcm(np.concatenate((signal, silence))))
        first = rms(after[int(0.12 * SR):int(0.14 * SR)])
        last = rms(after[int(0.18 * SR):int(0.20 * SR)])
        self.assertGreater(first, 0.03)
        self.assertGreater(first, 2.0 * last)

    def test_the_wood_body_resonates_on_its_own_a(self):
        """The box answers a low A, not everything it is handed."""
        near = play(voice_effects._EffectProcessor("body"),
                    to_pcm(tone(freq=110.0, amplitude=0.2, seconds=0.6)))
        far = play(voice_effects._EffectProcessor("body"),
                   to_pcm(tone(freq=140.0, amplitude=0.2, seconds=0.6)))
        self.assertGreater(rms(near[FRAME * 6:]), 1.5 * rms(far[FRAME * 6:]))

    def test_the_chorus_carries_three_copies(self):
        signal = to_pcm(tone(amplitude=0.2, seconds=0.5))
        after = play(voice_effects._EffectProcessor("chorus"), signal)
        dry = from_pcm(signal)[FRAME * 4:]
        self.assertGreater(rms(after[FRAME * 4:] - dry), 0.2 * rms(dry))


class TheThirdWaveOfPedalsTests(unittest.TestCase):
    """The last six: a record, a box, a shifter, a hush, a chest, a double."""

    def test_the_vinyl_crackles_only_while_the_voice_speaks(self):
        silent = bytes(FRAME)
        self.assertEqual(voice_effects._EffectProcessor("vinyl").process(silent),
                         silent)
        after = play(voice_effects._EffectProcessor("vinyl"),
                     to_pcm(tone(amplitude=0.3, seconds=1.0)))
        # A crackle is a jump far above a 220 Hz tone's own slope (~0.01).
        self.assertGreater(float(np.abs(np.diff(after)).max()), 0.06)

    def test_the_shimmer_moves_the_tone_up_by_its_hertz(self):
        """A 440 Hz tone gains 454 and loses 426: a shifter, not a ring mod."""
        after = play(voice_effects._EffectProcessor("shimmer"),
                     to_pcm(tone(freq=440.0, amplitude=0.3, seconds=1.2)))
        window = after[FRAME * 10:FRAME * 50]
        spectrum = np.abs(np.fft.rfft(window * np.hanning(window.size)))
        freqs = np.fft.rfftfreq(window.size, 1.0 / SR)

        def level(hz):
            return float(spectrum[int(np.argmin(np.abs(freqs - hz)))])

        self.assertGreater(level(454.0), 3.0 * level(426.0))
        self.assertGreater(level(454.0), 0.1 * level(440.0))

    def test_the_whisper_is_breath_not_pitch(self):
        signal = to_pcm(tone(amplitude=0.3, seconds=0.6))
        dry = from_pcm(signal)[FRAME * 4:]
        after = play(voice_effects._EffectProcessor("whisper"), signal)
        self.assertGreater(high_share(after[FRAME * 4:], 4000.0), 0.25)
        self.assertLess(rms(after[FRAME * 4:]), 2.0 * rms(dry))

    def test_the_growl_keeps_the_chest_and_drops_the_top(self):
        low = tone(freq=300.0, amplitude=0.3, seconds=0.4)
        high = tone(freq=3000.0, amplitude=0.3, seconds=0.4)
        low_after = play(voice_effects._EffectProcessor("growl"), to_pcm(low))
        high_after = play(voice_effects._EffectProcessor("growl"), to_pcm(high))
        # The chest is lifted over the dry voice; the top is taken away.
        self.assertGreater(rms(low_after[FRAME * 4:]),
                           1.1 * rms(low[FRAME * 2:]))
        self.assertLess(rms(high_after[FRAME * 4:]),
                        0.5 * rms(high[FRAME * 2:]))

    def test_the_doubler_carries_one_copy_at_the_22ms_delay(self):
        """What is left after the dry share is the voice, 22 ms late."""
        # 80 Hz, so the correlation has one peak in the searched band: at
        # 300 Hz a 22 ms delay is three periods off the nearest peak. The block
        # is fed in 20 ms frames, the way the line feeds it -- its ring is a
        # frame-sized delay line, not a whole-signal buffer.
        dry = tone(freq=80.0, amplitude=0.2, seconds=0.5)
        processor = voice_effects._EffectProcessor("doubler")
        shaped = np.concatenate([processor._doubler_block(dry[start:start + 960])
                                 for start in range(0, len(dry), 960)])
        copy = shaped - (1.0 - 0.55) * dry
        best, best_lag = -1.0, 0
        for lag in range(int(0.018 * SR), int(0.026 * SR)):
            score = float(np.dot(copy[lag:], dry[:-lag]))
            if score > best:
                best, best_lag = score, lag
        self.assertAlmostEqual(best_lag / SR, 0.022, delta=0.0015)

    def test_a_frame_boundary_is_not_a_click(self):
        """The bug behind the buzz: a ring read before it was written.

        Every wobbling delay here is shorter than a frame, so a block read
        before its own write falls on the previous wrap of the ring for every
        sample past the delay -- a step at every frame boundary, heard as a
        50 Hz buzz under the effect. That is also why some presets seemed to
        have no wobble: the wobble was there, buried in the buzz.
        """
        signal = to_pcm(tone(freq=300.0, amplitude=0.3, seconds=1.0))
        samples = FRAME // 2          # 20 ms of mono16 at 48 kHz
        for preset in ("vibrato", "flanger", "chorus", "vinyl", "doubler",
                       "shimmer", "cloud", "tapestop", "swell", "pump",
                       "step", "riser", "plate", "throw", "gated",
                       "choir"):
            with self.subTest(preset=preset):
                after = play(voice_effects._EffectProcessor(preset), signal)
                frames = after[samples * 4:samples * 44].reshape(-1, samples)
                inside = float(np.abs(np.diff(frames, axis=1)).max())
                edges = np.abs(frames[1:, 0] - frames[:-1, -1])
                self.assertLessEqual(float(edges.max()), 1.6 * inside)


class TheModernPedalsTests(unittest.TestCase):
    """The ten production pedals: the sound a current record is made of."""

    def test_the_vocoder_speaks_through_its_carrier(self):
        """A 300 Hz voice comes out on the carrier's harmonics, not at 300.

        That is the difference between a vocoder and an EQ: the excitation is
        replaced, the envelopes are the voice's own.
        """
        after = play(voice_effects._EffectProcessor("vocoder"),
                     to_pcm(tone(freq=300.0, amplitude=0.3, seconds=1.0)))
        window = after[FRAME * 10:]
        spectrum = np.abs(np.fft.rfft(window * np.hanning(window.size))) ** 2
        freqs = np.fft.rfftfreq(window.size, 1.0 / SR)

        def level(hz):
            return float(spectrum[(freqs > hz - 12) & (freqs < hz + 12)].sum())

        self.assertGreater(level(330.0), 6.0 * level(300.0))
        self.assertGreater(level(330.0), 0.05 * level(220.0))

    def test_the_vocoder_keeps_the_band_the_voice_used(self):
        low = play(voice_effects._EffectProcessor("vocoder"),
                   to_pcm(tone(freq=700.0, amplitude=0.3, seconds=1.0)))
        high = play(voice_effects._EffectProcessor("vocoder"),
                    to_pcm(tone(freq=2400.0, amplitude=0.3, seconds=1.0)))
        low = low[FRAME * 10:]
        high = high[FRAME * 10:]
        self.assertGreater(band_share(low, 400.0, 1600.0),
                           4.0 * band_share(low, 1600.0, 5000.0))
        self.assertGreater(band_share(high, 1600.0, 5000.0),
                           4.0 * band_share(high, 100.0, 400.0))

    def test_the_talkbox_is_the_growl_the_other_way_up(self):
        dry = saw(seconds=1.0)
        after = play(voice_effects._EffectProcessor("talkbox"), to_pcm(dry))
        self.assertGreater(band_share(after[FRAME * 5:], 800.0, 4000.0),
                           2.5 * band_share(dry[FRAME * 5:],
                                            800.0, 4000.0))

    def test_air_grows_top_harmonics_that_were_not_there(self):
        dry = saw(seconds=1.0)
        after = play(voice_effects._EffectProcessor("air"), to_pcm(dry))
        self.assertGreater(band_share(after[FRAME * 5:], 4000.0, 20000.0),
                           2.0 * band_share(dry[FRAME * 5:],
                                            4000.0, 20000.0))

    def test_hyperpop_lifts_the_quiet_word_toward_the_loud_one(self):
        levels = {}
        for name, amplitude in (("quiet", 0.02), ("loud", 0.3)):
            signal = tone(amplitude=amplitude, seconds=0.6)
            after = play(voice_effects._EffectProcessor("hyperpop"),
                         to_pcm(signal))
            levels[name] = rms(after[FRAME * 5:])
            levels[name + "_in"] = rms(signal[FRAME * 5:])
        # The quiet voice is pushed up and the loud one is held down, so the
        # gap between them closes -- which is the whole sound of the pedal.
        self.assertGreater(levels["quiet"], 2.0 * levels["quiet_in"])
        self.assertLess(levels["loud"], levels["loud_in"])
        self.assertLess(levels["loud"] / levels["quiet"],
                        0.5 * levels["loud_in"] / levels["quiet_in"])

    def test_the_tape_stop_drops_the_pitch_and_starts_again(self):
        after = play(voice_effects._EffectProcessor("tapestop"),
                     to_pcm(tone(freq=400.0, amplitude=0.3, seconds=2.0)))

        def at(seconds):
            start = int(seconds * SR)
            return peak_freq(after[start:start + 480])

        self.assertAlmostEqual(at(0.20), 400.0, delta=15.0)
        self.assertLess(at(1.20), 150.0)
        self.assertAlmostEqual(at(1.55), 400.0, delta=20.0)

    def test_the_pump_ducks_twice_a_second(self):
        after = play(voice_effects._EffectProcessor("pump"),
                     to_pcm(tone(amplitude=0.3, seconds=1.5)))
        samples = after[FRAME * 4:]
        # 40 ms of smoothing: shorter windows carry the tone's own ripple and
        # would be counted as a dip of their own.
        window = 1920
        envelope = np.sqrt(np.convolve(samples ** 2,
                                       np.ones(window) / window, mode="valid"))
        self.assertLess(float(envelope.min()), 0.4 * float(envelope.max()))
        quiet = envelope < 0.5 * float(envelope.max())
        dips = int(np.sum(quiet[1:] & ~quiet[:-1]))
        self.assertGreaterEqual(dips, 2)
        self.assertLessEqual(dips, 4)

    def test_the_cloud_leaves_a_haze_behind_the_word(self):
        burst = np.concatenate((tone(300.0, 0.3, 0.4),
                                np.zeros(int(0.8 * SR), dtype=np.float32)))
        after = play(voice_effects._EffectProcessor("cloud"), to_pcm(burst))
        self.assertGreater(rms(after[int(0.42 * SR):int(0.44 * SR)]), 0.02)
        self.assertLess(rms(after[int(0.85 * SR):int(0.90 * SR)]), 0.002)

    def test_the_freeze_holds_the_slice_then_lets_it_go(self):
        burst = np.concatenate((tone(300.0, 0.3, 0.5),
                                np.zeros(int(2.0 * SR), dtype=np.float32)))
        after = play(voice_effects._EffectProcessor("freeze"), to_pcm(burst))
        # Held while the recapture is still the words that were spoken...
        self.assertGreater(rms(after[int(0.80 * SR):int(0.82 * SR)]), 0.04)
        # ...and gone once the recapture has caught silence: a freeze must not
        # leave the room humming for ever.
        self.assertLess(rms(after[int(1.85 * SR):int(1.90 * SR)]), 0.005)

    def test_the_step_filter_moves_between_bands(self):
        after = play(voice_effects._EffectProcessor("step"),
                     to_pcm(saw(seconds=1.0)))
        ratios = []
        samples = FRAME // 2                     # 20 ms of mono16 at 48 kHz
        for start in range(samples * 3, samples * 40, samples):
            frame = after[start:start + samples]
            if rms(frame) < 0.01:
                continue
            ratios.append(band_share(frame, 1800.0, 5000.0)
                          / max(1e-9, band_share(frame, 100.0, 600.0)))
        # The pattern's bright steps and its dark steps, in one second.
        self.assertGreater(max(ratios), 3.0 * min(ratios))

    def test_the_swell_grows_a_ghost_and_cuts_it(self):
        """Measured on the block itself, so what is left *is* the ghost.

        The chain's own filters would swamp this measurement, so the pedal is
        fed raw blocks: what comes back minus what went in is exactly the
        backwards copy, and its envelope over a cycle is the pedal's shape.
        """
        processor = voice_effects._EffectProcessor("swell")
        dry = tone(220.0, 0.2, 2.0)
        ghosts = []
        for start in range(0, len(dry) - 960 + 1, 960):
            chunk = dry[start:start + 960]
            ghosts.append(rms(processor._swell_block(chunk) - chunk))
        # The first cycle has nothing behind it to play; the ghost is measured
        # where the ring holds a second of the voice. Frame 50 is 1.00 s (early
        # in the swell), 72 is 1.45 s (its peak), 77 is 1.55 s (cut).
        self.assertLess(ghosts[50], 0.3 * ghosts[72])
        self.assertGreater(ghosts[72], 0.01)
        self.assertEqual(ghosts[77], 0.0)


class TheStudioPedalsTests(unittest.TestCase):
    """The record chain: each piece is what its name says."""

    def test_the_page_is_two_levels_with_nothing_lost(self):
        self.assertEqual(voice_effects.catalogue_problems(), [])
        self.assertEqual(len(voice_effects.VOICE_EFFECTS), 70)   # 69 + Off
        self.assertEqual(len(voice_effects.VOICE_EFFECT_GROUPS), 7)
        self.assertEqual(
            sum(len(members) for *_rest, members
                in voice_effects.VOICE_EFFECT_GROUPS), 68)

    def test_tube_warmth_is_gentler_than_the_amp(self):
        # A pure tone has no harmonics of its own: whatever comes out is the
        # valve's, which is the only way to tell warmth from a saw's own edge.
        signal = to_pcm(tone(220.0, 0.3, 1.0))
        dry = harmonics(from_pcm(signal)[FRAME * 5:], 220.0)
        tube = harmonics(play(voice_effects._EffectProcessor("tube"), signal)
                         [FRAME * 5:], 220.0)
        amp = harmonics(play(voice_effects._EffectProcessor("amp"), signal)
                        [FRAME * 5:], 220.0)
        self.assertGreater(tube, 1.2 * dry)   # it does add harmonics...
        self.assertLess(tube, amp)            # ...but it is not an amp

    def test_presence_lifts_the_three_kilohertz_band(self):
        signal = to_pcm(saw(seconds=1.0))
        dry = from_pcm(signal)[FRAME * 5:]
        after = play(voice_effects._EffectProcessor("presence"), signal)
        self.assertGreater(band_share(after[FRAME * 5:], 2000.0, 5000.0),
                           1.5 * band_share(dry, 2000.0, 5000.0))

    def test_the_gated_room_is_cut_where_the_plate_is_not(self):
        burst = np.concatenate((tone(300.0, 0.3, 0.35),
                                np.zeros(int(1.2 * SR), dtype=np.float32)))
        plate = play(voice_effects._EffectProcessor("plate"), to_pcm(burst))
        gated = play(voice_effects._EffectProcessor("gated"), to_pcm(burst))
        late = slice(int(0.72 * SR), int(0.80 * SR))
        early = slice(int(0.34 * SR), int(0.42 * SR))
        self.assertGreater(rms(gated[early]), 0.02)
        self.assertLess(rms(gated[late]), 0.002)
        self.assertGreater(rms(plate[late]), rms(gated[late]))

    def test_the_throw_comes_back_at_three_eighths(self):
        burst = np.concatenate((tone(400.0, 0.4, 0.08),
                                np.zeros(int(0.9 * SR), dtype=np.float32)))
        after = play(voice_effects._EffectProcessor("throw"), to_pcm(burst))
        self.assertGreater(rms(after[int(0.38 * SR):int(0.42 * SR)]), 0.02)
        self.assertGreater(rms(after[int(0.76 * SR):int(0.80 * SR)]), 0.01)
        # ...and nothing in between: one tap, not a smear.
        self.assertLess(rms(after[int(0.60 * SR):int(0.64 * SR)]), 0.004)

    def test_dub_gets_darker_with_every_repeat(self):
        burst = np.concatenate((saw(0.4, 0.08, 200.0),
                                np.zeros(int(0.9 * SR), dtype=np.float32)))
        after = play(voice_effects._EffectProcessor("dub"), to_pcm(burst))
        first = after[int(0.26 * SR):int(0.34 * SR)]
        second = after[int(0.50 * SR):int(0.58 * SR)]
        self.assertGreater(rms(first), 0.005)
        self.assertLess(band_share(second, 1500.0, 20000.0),
                        band_share(first, 1500.0, 20000.0))

    def test_the_wash_keeps_more_room_than_voice(self):
        burst = np.concatenate((tone(300.0, 0.3, 0.30),
                                np.zeros(int(0.9 * SR), dtype=np.float32)))
        after = play(voice_effects._EffectProcessor("wash"), to_pcm(burst))
        self.assertGreater(rms(after[int(0.40 * SR):int(0.44 * SR)]), 0.05)

    def test_choir_adds_the_octave_above(self):
        signal = to_pcm(tone(freq=220.0, amplitude=0.3, seconds=0.6))
        after = play(voice_effects._EffectProcessor("choir"), signal)
        window = after[FRAME * 6:]
        spectrum = np.abs(np.fft.rfft(window * np.hanning(window.size))) ** 2
        freqs = np.fft.rfftfreq(window.size, 1.0 / SR)

        def level(hz):
            return float(spectrum[(freqs > hz - 10) & (freqs < hz + 10)].sum())

        dry = from_pcm(signal)[FRAME * 6:]
        dry_spectrum = np.abs(np.fft.rfft(dry * np.hanning(dry.size))) ** 2

        def dry_level(hz):
            return float(dry_spectrum[(freqs > hz - 10) & (freqs < hz + 10)].sum())

        self.assertGreater(level(440.0) / max(1e-12, level(220.0)),
                           3.0 * dry_level(440.0) / max(1e-12, dry_level(220.0)))

    def test_the_riser_climbs_and_then_rests(self):
        after = play(voice_effects._EffectProcessor("riser"),
                     to_pcm(tone(400.0, 0.3, 2.4)))
        start = peak_freq(after[int(0.16 * SR):int(0.20 * SR)])
        top = peak_freq(after[int(0.40 * SR):int(0.44 * SR)])
        self.assertAlmostEqual(start, 400.0, delta=25.0)
        self.assertGreater(top, 1.35 * start)
        # After the ramp the wet is gone and only the dry voice is left: its
        # level is the plain tone's, not the climbing copy's.
        self.assertLess(rms(after[int(0.75 * SR):int(0.85 * SR)]), 0.12)

    def test_scream_is_dirtier_than_the_fuzz(self):
        signal = to_pcm(tone(amplitude=0.25, seconds=0.6))
        scream = harmonics(play(voice_effects._EffectProcessor("scream"),
                                signal)[FRAME * 6:], 220.0)
        fuzz = harmonics(play(voice_effects._EffectProcessor("fuzz"),
                              signal)[FRAME * 6:], 220.0)
        self.assertGreater(scream, fuzz)

# --------------------------------------------------------------- the amp shelf


class TheAmpShelfTests(unittest.TestCase):
    """The twenty amps: each shelf is the corner, band, cabinet and bias it names."""

    SHELF = ("crunch", "overdrive", "boost", "lead", "modern", "tweed",
             "brown", "hotrod", "chime", "sag", "direct", "acoustic",
             "jazz", "room_cab", "doom", "stoner", "velcro", "octavia",
             "fuzzsub", "twelve")

    def shaped(self, preset, signal):
        return play(voice_effects._EffectProcessor(preset), signal)

    def test_the_shelf_is_in_the_amp_family_and_in_the_catalogue(self):
        members = voice_effects.group_members("drive")
        for preset in self.SHELF:
            with self.subTest(preset=preset):
                self.assertIn(preset, members)
                self.assertTrue(voice_effects.effect_label(preset))
        # The original six, the twenty, and the board.
        self.assertEqual(len(members), 27)

    def test_the_drive_ladder_gets_dirtier_step_by_step(self):
        signal = to_pcm(tone(220.0, 0.30, 0.6))
        dry = harmonics(from_pcm(signal)[FRAME * 5:], 220.0)
        crunch = harmonics(self.shaped("crunch", signal)[FRAME * 5:], 220.0)
        overdrive = harmonics(self.shaped("overdrive", signal)[FRAME * 5:],
                              220.0)
        lead = harmonics(self.shaped("lead", signal)[FRAME * 5:], 220.0)
        self.assertGreater(crunch, dry + 0.01)
        self.assertGreater(overdrive, 1.5 * crunch)
        self.assertGreater(lead, 1.3 * overdrive)

    def test_the_direct_box_keeps_the_top_the_cabinet_takes(self):
        signal = to_pcm(noise(0.10, 0.6))
        direct = high_share(self.shaped("direct", signal)[FRAME * 5:])
        amp = high_share(self.shaped("amp", signal)[FRAME * 5:])
        self.assertGreater(direct, 0.20)
        self.assertLess(amp, 0.02)
        self.assertGreater(direct, 10.0 * amp)

    def test_jazz_is_the_darkest_clean_and_chime_the_brightest(self):
        signal = to_pcm(noise(0.10, 0.6))
        jazz = high_share(self.shaped("jazz", signal)[FRAME * 5:])
        chime = high_share(self.shaped("chime", signal)[FRAME * 5:])
        self.assertLess(jazz, 0.01)
        self.assertGreater(chime, 0.05)

    def test_twelve_string_adds_the_octave_above(self):
        signal = to_pcm(tone(220.0, 0.30, 0.8))
        after = self.shaped("twelve", signal)[FRAME * 6:]
        dry = from_pcm(signal)[FRAME * 6:]
        self.assertGreater(harmonic_ratio(after), 0.004)
        self.assertLess(harmonic_ratio(dry), 0.001)

    def test_doom_and_the_fuzz_sub_put_the_octave_below(self):
        signal = to_pcm(tone(220.0, 0.30, 0.8))
        for preset in ("doom", "fuzzsub"):
            with self.subTest(preset=preset):
                after = self.shaped(preset, signal)[FRAME * 6:]
                self.assertGreater(harmonic_ratio(after, 220.0, 110.0), 0.20)
        dry = from_pcm(signal)[FRAME * 6:]
        self.assertLess(harmonic_ratio(dry, 220.0, 110.0), 0.001)

    def test_the_octave_fuzz_is_dirtier_than_the_plain_fuzz(self):
        signal = to_pcm(tone(220.0, 0.25, 0.6))
        octavia = harmonics(self.shaped("octavia", signal)[FRAME * 6:], 220.0)
        fuzz = harmonics(self.shaped("fuzz", signal)[FRAME * 6:], 220.0)
        self.assertGreater(octavia, fuzz)

    def test_sag_dips_under_a_loud_passage_and_recovers(self):
        loud = tone(220.0, 0.40, 0.8)
        quiet = tone(220.0, 0.04, 0.8)
        signal = to_pcm(np.concatenate((loud, quiet)))
        after = self.shaped("sag", signal)
        start = rms(after[int(SR * 0.05):int(SR * 0.15)])
        end = rms(after[int(SR * 0.70):int(SR * 0.80)])
        self.assertLess(end, 0.95 * start)
        join = int(SR * 0.8)
        early = rms(after[join:join + int(SR * 0.10)])
        late = rms(after[join + int(SR * 0.60):join + int(SR * 0.72)])
        self.assertGreater(late, 1.2 * early)
        # ...and a plain amp does neither: this is not a compressor.
        steady = self.shaped("amp", signal)
        self.assertAlmostEqual(
            rms(steady[int(SR * 0.05):int(SR * 0.15)]),
            rms(steady[int(SR * 0.70):int(SR * 0.80)]), delta=0.01)

    def test_velcro_tears_thirteen_times_a_second(self):
        signal = to_pcm(tone(220.0, 0.30, 1.0))
        after = self.shaped("velcro", signal)[FRAME * 5:]
        blocks = after[:after.size // 480 * 480].reshape(-1, 480)
        envelope = np.abs(blocks).max(axis=1)
        centred = envelope - envelope.mean()
        spectrum = np.abs(np.fft.rfft(centred * np.hanning(centred.size)))
        rates = np.fft.rfftfreq(centred.size, 480.0 / SR)
        peak = float(rates[int(np.argmax(spectrum))])
        self.assertGreater(peak, 10.0)
        self.assertLess(peak, 16.0)


# ---------------------------------------------------------------- the board


class TheCustomBoardTests(unittest.TestCase):
    """The board: one table, one spec, and a revision the line can see."""

    def setUp(self):
        self.addCleanup(mock.patch.stopall)
        self.board = {}
        mock.patch.object(
            voice_effect_custom.options, "get",
            side_effect=lambda key, default=None: self.board.get(key, default)
        ).start()
        mock.patch.object(
            voice_effect_custom.options, "set",
            side_effect=lambda key, value, autosave=True:
            self.board.__setitem__(key, value)
        ).start()

    def test_a_fresh_board_is_the_table_defaults(self):
        values = voice_effect_custom.values()
        for param_id, _name, _note, _choices, default in (
                voice_effect_custom.PARAMS):
            self.assertEqual(values[param_id], default)
        self.assertEqual(voice_effect_custom.summary(),
                         "Crunch, Valve, Classic")
        self.assertEqual(voice_effect_custom.spec()["lp"], 4200.0)

    def test_every_knob_moves_the_setting_it_names(self):
        voice_effect_custom.set_value("drive", "sat")
        self.assertEqual(voice_effect_custom.spec()["drive"], 20.0)
        voice_effect_custom.set_value("bass", "telephone")
        self.assertEqual(voice_effect_custom.spec()["hp"], 400.0)
        voice_effect_custom.set_value("treble", "dark")
        self.assertEqual(voice_effect_custom.spec()["lp"], 2400.0)
        self.assertNotIn("formant", voice_effect_custom.spec())
        voice_effect_custom.set_value("mid", "bark")
        self.assertEqual(voice_effect_custom.spec()["formant"][0], 900.0)
        voice_effect_custom.set_value("gate", "hard")
        self.assertEqual(voice_effect_custom.spec()["gate"], 0.020)
        voice_effect_custom.set_value("output", "loud")
        self.assertEqual(voice_effect_custom.spec()["out"], 1.15)

    def test_drive_off_takes_the_shaper_with_it(self):
        voice_effect_custom.set_value("drive", "off")
        spec = voice_effect_custom.spec()
        self.assertIsNone(spec["shape"])
        self.assertEqual(spec["drive"], 1.0)

    def test_the_five_characters_are_five_shapers(self):
        shapes = {}
        voice_effect_custom.set_value("drive", "high")
        for value in ("clean", "warm", "valve", "hard", "fuzz"):
            voice_effect_custom.set_value("character", value)
            spec = voice_effect_custom.spec()
            shapes[value] = (spec["shape"], spec["bias"])
        self.assertEqual(len(set(shapes.values())), 5)
        self.assertEqual(shapes["clean"], (None, 0.0))
        self.assertEqual(shapes["valve"][0], "tanh")
        self.assertEqual(shapes["fuzz"][0], "hard")
        self.assertGreater(shapes["fuzz"][1], shapes["hard"][1])

    def test_a_value_the_knob_does_not_offer_keeps_the_default(self):
        self.assertEqual(voice_effect_custom.set_value("drive", "nonsense"),
                         "crunch")
        self.assertIsNone(voice_effect_custom.set_value("no_such_knob", "x"))

    def test_a_saved_board_that_lost_a_knob_reads_from_the_table(self):
        # A setting written by an older build (or hand-edited) must never be a
        # pedal that cannot be built: every missing knob is its default.
        self.board["music_bot_voice_effect_custom"] = {"drive": "gone"}
        values = voice_effect_custom.values()
        self.assertEqual(values["drive"], "crunch")
        self.assertEqual(values["gate"], "soft")

    def test_the_board_round_trips_a_frame_like_every_pedal(self):
        processor = voice_effects._EffectProcessor(voice_effects.CUSTOM_EFFECT)
        pcm = to_pcm(tone(seconds=0.4))
        for start in range(0, len(pcm) - FRAME + 1, FRAME):
            self.assertEqual(len(processor.process(pcm[start:start + FRAME])),
                             FRAME)

    def test_a_knob_change_is_a_new_revision(self):
        before = voice_effect_custom.revision()
        voice_effect_custom.set_value("drive", "sat")
        self.assertGreater(voice_effect_custom.revision(), before)

    def test_the_line_rebuilds_when_a_knob_moves_under_a_sounding_voice(self):
        line = voice_effects.VoiceEffectLine()
        pcm = to_pcm(tone(seconds=0.2))
        with mock.patch.object(voice_effects, "effect_id",
                               return_value=voice_effects.CUSTOM_EFFECT):
            first = line.shape(pcm[:FRAME])
            self.assertEqual(len(first), FRAME)
            processor = line._processor
            self.assertIsNotNone(processor)
            voice_effect_custom.set_value("drive", "sat")
            line.shape(pcm[:FRAME])
            self.assertIsNot(line._processor, processor)

    def test_the_board_is_one_row_in_the_amp_family_with_a_live_note(self):
        self.assertIn(voice_effects.CUSTOM_EFFECT,
                      voice_effects.group_members("drive"))
        note = voice_effects.effect_note(voice_effects.CUSTOM_EFFECT)
        self.assertIn("Crunch", note)
        self.assertIn("guitar", note.lower())
        voice_effect_custom.set_value("drive", "sat")
        self.assertIn("Saturated",
                      voice_effects.effect_note(voice_effects.CUSTOM_EFFECT))


class TheVoiceBuilderTests(unittest.TestCase):
    """Custom Voice: ten slots, one choice each, and every choice a block."""

    def setUp(self):
        self.addCleanup(mock.patch.stopall)
        self.board = {}
        mock.patch.object(
            voice_effect_builder.options, "get",
            side_effect=lambda key, default=None: self.board.get(key, default)
        ).start()
        mock.patch.object(
            voice_effect_builder.options, "set",
            side_effect=lambda key, value, autosave=True:
            self.board.__setitem__(key, value)
        ).start()

    def test_the_board_offers_at_least_fifty_forms(self):
        forms = sum(len(row[3]) for row in voice_effect_builder.PARAMS)
        self.assertGreaterEqual(forms, 50)
        self.assertEqual(len(voice_effect_builder.PARAMS), 10)

    def test_every_slot_is_a_different_choice_set(self):
        ids = [row[0] for row in voice_effect_builder.PARAMS]
        self.assertEqual(len(ids), len(set(ids)))
        for param_id in ("colour", "tone", "body", "movement", "pitch",
                         "space", "texture", "rhythm", "gate", "output"):
            self.assertIn(param_id, ids)

    def test_a_fresh_board_is_an_amp_with_nothing_else_on(self):
        values = voice_effect_builder.values()
        for param_id, _name, _note, _choices, default in (
                voice_effect_builder.PARAMS):
            self.assertEqual(values[param_id], default)
        self.assertEqual(voice_effect_builder.summary(), "Crunch")
        spec = voice_effect_builder.spec()
        self.assertEqual(spec["shape"], "tanh")
        self.assertEqual(spec["lp"], 4200.0)
        for block in ("tremolo", "echo", "vocoder", "stutter", "octave"):
            self.assertNotIn(block, spec)

    def test_every_slot_writes_the_block_it_names(self):
        cases = (
            ("colour", "off", "shape", None),
            ("colour", "fuzz", "shape", "hard"),
            ("tone", "dark", "lp", 2400.0),
            ("tone", "off", "lp", 12000.0),
            ("body", "bark", "formant", (900.0, 1.20, 0.42, 1.7)),
            ("movement", "tremolo", "tremolo", (5.2, 0.85)),
            ("movement", "chorus", "chorus3", ((7.0, 9.0, 12.0), 0.35, 1.6,
                                                0.5)),
            ("pitch", "sub", "sub_octave", 0.50),
            ("pitch", "choir", "octave", 0.30),
            ("space", "cave", "cave", ((31.0, 47.0, 61.0, 83.0), 0.45, 0.55)),
            ("space", "gated", "gated", None),
            ("texture", "vocoder", "vocoder", (2.6, 0.9)),
            ("texture", "crush", "crush", (6, 8000.0)),
            ("rhythm", "pump", "pump", (128.0, 0.8, 0.22)),
            ("rhythm", "freeze", "freeze", (8, 60)),
            ("gate", "hard", "gate", 0.020),
            ("output", "loud", "out", 1.15),
        )
        for slot, value, key, expected in cases:
            with self.subTest(slot=slot, choice=value):
                voice_effect_builder.set_value(slot, value)
                spec = voice_effect_builder.spec()
                self.assertIn(key, spec)
                if expected is not None:
                    self.assertEqual(spec[key], expected)

    def test_off_is_the_absence_of_the_block(self):
        voice_effect_builder.set_value("movement", "tremolo")
        self.assertIn("tremolo", voice_effect_builder.spec())
        voice_effect_builder.set_value("movement", "off")
        self.assertNotIn("tremolo", voice_effect_builder.spec())
        voice_effect_builder.set_value("body", "flat")
        self.assertNotIn("formant", voice_effect_builder.spec())

    def test_a_talkbox_takes_the_body_band_for_its_own(self):
        voice_effect_builder.set_value("body", "bark")
        self.assertEqual(voice_effect_builder.spec()["formant"][0], 900.0)
        voice_effect_builder.set_value("texture", "talkbox")
        self.assertEqual(voice_effect_builder.spec()["formant"][0], 2300.0)

    def test_the_summary_names_only_what_is_on(self):
        voice_effect_builder.set_value("movement", "wah")
        voice_effect_builder.set_value("space", "cave")
        self.assertEqual(voice_effect_builder.summary(), "Crunch, Wah, Cave")
        for slot, _name, _note, _choices, default in (
                voice_effect_builder.PARAMS):
            if slot in ("colour", "gate", "output", "tone", "body"):
                continue
            voice_effect_builder.set_value(slot, "off")
        voice_effect_builder.set_value("colour", "off")
        self.assertEqual(voice_effect_builder.summary(), "Dry")

    def test_a_saved_board_that_lost_a_slot_reads_from_the_table(self):
        self.board["music_bot_voice_effect_builder"] = {"space": "gone"}
        values = voice_effect_builder.values()
        self.assertEqual(values["space"], "off")
        self.assertEqual(values["colour"], "crunch")

    def test_the_board_round_trips_a_frame_like_every_pedal(self):
        processor = voice_effects._EffectProcessor(voice_effects.BUILDER_EFFECT)
        pcm = to_pcm(tone(seconds=0.4))
        for start in range(0, len(pcm) - FRAME + 1, FRAME):
            self.assertEqual(len(processor.process(pcm[start:start + FRAME])),
                             FRAME)

    def test_two_blocks_that_share_an_oscillator_keep_their_own_rates(self):
        # The chop has its own phase (it did not, until the board could put a
        # Chop in Rhythm next to a Tremolo in Movement). One shared LFO would
        # move both by the *sum* of the rates, so each oscillator's own travel
        # is what this pins.
        voice_effect_builder.set_value("movement", "tremolo")
        voice_effect_builder.set_value("rhythm", "chop")
        processor = voice_effects._EffectProcessor(voice_effects.BUILDER_EFFECT)
        frame = to_pcm(tone(0.30, 1.0))[:FRAME]
        processor.process(frame)
        processor.process(frame)
        seconds = 2 * (len(frame) // 2) / SR
        self.assertAlmostEqual(
            processor._chop_phase,
            (2.0 * np.pi * 8.0 * seconds) % (2.0 * np.pi), places=5)
        self.assertAlmostEqual(
            processor._lfo,
            (2.0 * np.pi * 5.2 * seconds) % (2.0 * np.pi), places=5)

    def test_a_slot_change_is_a_new_revision(self):
        before = voice_effect_builder.revision()
        voice_effect_builder.set_value("space", "cave")
        self.assertGreater(voice_effect_builder.revision(), before)

    def test_clear_puts_every_slot_back_and_forgets_the_starting_point(self):
        """The board's Clear line: ten slots, one save, nothing remembered."""
        voice_effect_recipes.apply("oracle")
        self.assertEqual(voice_effect_recipes.current_label(), "Dub Oracle")
        before = voice_effect_builder.revision()
        cleared = voice_effect_builder.clear()
        self.assertEqual(cleared, voice_effect_builder.filled({}))
        self.assertEqual(voice_effect_builder.values(), cleared)
        for param_id, _name, _note, _choices, default in (
                voice_effect_builder.PARAMS):
            self.assertEqual(voice_effect_builder.values()[param_id], default)
        self.assertEqual(voice_effect_recipes.current_label(),
                         voice_effect_recipes.MY_OWN)
        self.assertEqual(voice_effect_builder.recipe(), "")
        # One save and one rebuild, like a recipe: ten writes would be ten
        # heard crossfades.
        self.assertEqual(voice_effect_builder.revision(), before + 1)

    def test_a_cleared_board_is_still_a_sound(self):
        voice_effect_builder.clear()
        processor = voice_effects._EffectProcessor(voice_effects.BUILDER_EFFECT)
        out = play(processor, to_pcm(tone(seconds=0.30, amplitude=0.4)))
        self.assertTrue(np.all(np.isfinite(out)))
        self.assertGreater(rms(out), 0.01)
        self.assertLessEqual(float(np.max(np.abs(out))),
                             voice_effects.PEAK_CEILING + 1e-6)

    def test_the_line_rebuilds_when_a_slot_moves(self):
        line = voice_effects.VoiceEffectLine()
        pcm = to_pcm(tone(seconds=0.2))
        with mock.patch.object(voice_effects, "effect_id",
                               return_value=voice_effects.BUILDER_EFFECT):
            self.assertEqual(len(line.shape(pcm[:FRAME])), FRAME)
            processor = line._processor
            self.assertIsNotNone(processor)
            voice_effect_builder.set_value("space", "cave")
            line.shape(pcm[:FRAME])
            self.assertIsNot(line._processor, processor)

    def test_the_catalogue_row_is_a_live_description(self):
        self.assertIn("Crunch",
                      voice_effects.effect_note(voice_effects.BUILDER_EFFECT))
        voice_effect_builder.set_value("movement", "zap")
        self.assertEqual(voice_effect_builder.values()["movement"], "off")
        voice_effect_builder.set_value("space", "space")
        self.assertEqual(voice_effect_builder.values()["space"], "off")


class TheRoomLevelTests(unittest.TestCase):
    """What a board hands a PA or a cinema cabinet, in average level.

    A peak ceiling alone let a board be a square wave at 0.70: RMS 0.5 where a
    spoken voice is 0.08. The room's chain (a bass shelf, a speech compressor, a
    safety limiter that only engages above 0.85) was built for the voice, so the
    square went through and broke up -- reported as a buzz out of a PA speaker
    or a cinema cabinet. The two boards are levelled on the way out; the fixed
    pedals were each tuned against a PA when they were made, and are left alone.
    """

    def setUp(self):
        self.addCleanup(mock.patch.stopall)
        self.builder = {}
        self.amp = {}
        mock.patch.object(
            voice_effect_builder.options, "get",
            side_effect=lambda key, default=None: self.builder.get(key, default)
        ).start()
        mock.patch.object(
            voice_effect_builder.options, "set",
            side_effect=lambda key, value, autosave=True:
            self.builder.__setitem__(key, value)
        ).start()
        mock.patch.object(
            voice_effect_custom.options, "get",
            side_effect=lambda key, default=None: self.amp.get(key, default)
        ).start()
        mock.patch.object(
            voice_effect_custom.options, "set",
            side_effect=lambda key, value, autosave=True:
            self.amp.__setitem__(key, value)
        ).start()

    def settled(self, processor, signal):
        """The output after the trim has caught up (the first frames may be hot)."""
        return play(processor, signal)

    def test_every_recipe_hands_a_room_a_voice_average(self):
        signal = to_pcm(tone(seconds=1.2, amplitude=0.30))
        for recipe_id, label, _family, _note, _board in (
                voice_effect_recipes.RECIPES):
            with self.subTest(recipe=label):
                voice_effect_recipes.apply(recipe_id)
                processor = voice_effects._EffectProcessor(
                    voice_effects.BUILDER_EFFECT)
                out = self.settled(processor, signal)
                self.assertLessEqual(
                    rms(out), voice_effects.VOICE_LOUDNESS * 1.10,
                    f"{label} is louder than a voice a room can take")
                self.assertLessEqual(float(np.max(np.abs(out))),
                                     voice_effects.PEAK_CEILING + 1e-6)

    def test_the_worst_amp_board_is_levelled_too(self):
        # The loudest a player can make the amp board: every knob at its top,
        # the gate open and the output trim up (a fuzz, a bright cabinet and a
        # presence lift at once).
        for slot, value in (("drive", "sat"), ("character", "fuzz"),
                            ("bass", "full"), ("mid", "presence"),
                            ("treble", "bright"), ("gate", "off"),
                            ("output", "loud")):
            voice_effect_custom.set_value(slot, value)
        processor = voice_effects._EffectProcessor(voice_effects.CUSTOM_EFFECT)
        out = self.settled(processor, to_pcm(tone(seconds=1.2, amplitude=0.30)))
        self.assertLessEqual(rms(out), voice_effects.VOICE_LOUDNESS * 1.10)
        self.assertLessEqual(float(np.max(np.abs(out))),
                             voice_effects.PEAK_CEILING + 1e-6)

    def test_a_fixed_pedal_is_left_exactly_as_it_was(self):
        """The presets were tuned against a PA; only the boards are levelled."""
        self.assertFalse(voice_effects._EffectProcessor("amp")._levelled)
        self.assertTrue(voice_effects._EffectProcessor(
            voice_effects.CUSTOM_EFFECT)._levelled)
        self.assertTrue(voice_effects._EffectProcessor(
            voice_effects.BUILDER_EFFECT)._levelled)

    def test_a_quiet_board_is_handed_back_untouched(self):
        processor = voice_effects._EffectProcessor(voice_effects.BUILDER_EFFECT)
        quiet = np.full(voice_effects.FRAME_BYTES // 2, 0.05, dtype=np.float32)
        self.assertIs(processor._loudness_trim(quiet), quiet)
        self.assertEqual(processor._loudness_gain, 1.0)

    def test_the_trim_continues_across_the_frame_it_ends_on(self):
        """No step between frames: the last sample of one is the first of next.

        A gain that jumps between frames is a 50 Hz buzz (the same rule the
        Hyperpop follower and the step filter follow), so the ramp starts where
        the previous frame ended.
        """
        processor = voice_effects._EffectProcessor(voice_effects.BUILDER_EFFECT)
        block = np.full(voice_effects.FRAME_BYTES // 2, 0.60, dtype=np.float32)
        first = processor._loudness_trim(block)
        gain = processor._loudness_gain
        self.assertLess(gain, 1.0)
        second = processor._loudness_trim(block)
        self.assertAlmostEqual(float(second[0]), float(first[-1]), places=6)
        self.assertLess(float(np.max(np.diff(second))), 1e-3)


class TheRecipeTests(unittest.TestCase):
    """The board's first line: fifty finished voices, one press each.

    A recipe is not a pedal -- it writes the board and nothing else -- so what
    these pin is the two claims that makes: every recipe is a board the table
    can actually build (no value the slot does not offer, no two recipes the
    same voice), and every one of them is a sound when it is played.
    """

    def setUp(self):
        self.addCleanup(mock.patch.stopall)
        self.board = {}
        mock.patch.object(
            voice_effect_builder.options, "get",
            side_effect=lambda key, default=None: self.board.get(key, default)
        ).start()
        mock.patch.object(
            voice_effect_builder.options, "set",
            side_effect=lambda key, value, autosave=True:
            self.board.__setitem__(key, value)
        ).start()

    def test_fifty_starting_points_in_five_families_of_ten(self):
        self.assertEqual(voice_effect_recipes.count(), 50)
        self.assertEqual(len(voice_effect_recipes.FAMILIES), 5)
        for family_id, name, note in voice_effect_recipes.FAMILIES:
            with self.subTest(family=family_id):
                self.assertEqual(
                    len(voice_effect_recipes.family_members(family_id)), 10)
                self.assertTrue(name.strip())
                self.assertTrue(note.strip())

    def test_the_table_has_nothing_to_lie_about(self):
        self.assertEqual(voice_effect_recipes.problems(), [])

    def test_the_families_offer_every_recipe_exactly_once(self):
        offered = []
        for family_id, _name, _note in voice_effect_recipes.FAMILIES:
            offered.extend(voice_effect_recipes.family_members(family_id))
        self.assertEqual(sorted(offered),
                         sorted(row[0] for row in voice_effect_recipes.RECIPES))
        self.assertEqual(len(offered), len(set(offered)))

    def test_no_two_recipes_are_the_same_voice(self):
        boards = [tuple(sorted(voice_effect_builder.filled(row[4]).items()))
                  for row in voice_effect_recipes.RECIPES]
        self.assertEqual(len(set(boards)), 50)

    def test_picking_one_fills_the_whole_board_in_a_single_revision(self):
        before = voice_effect_builder.revision()
        chosen = voice_effect_recipes.apply("oracle")
        row = voice_effect_recipes.recipe("oracle")
        self.assertEqual(chosen, voice_effect_builder.filled(row[4]))
        self.assertEqual(voice_effect_builder.values(), chosen)
        self.assertEqual(voice_effect_recipes.current_id(), "oracle")
        self.assertEqual(voice_effect_recipes.current_label(), "Dub Oracle")
        # One save and one rebuild: ten slot writes would be ten crossfades.
        self.assertEqual(voice_effect_builder.revision(), before + 1)

    def test_a_slot_moved_afterwards_reads_as_my_own(self):
        voice_effect_recipes.apply("oracle")
        voice_effect_builder.set_value("gate", "hard")
        self.assertEqual(voice_effect_recipes.current_id(), "")
        self.assertEqual(voice_effect_recipes.current_label(),
                         voice_effect_recipes.MY_OWN)
        self.assertIn(voice_effect_recipes.MY_OWN,
                      voice_effect_recipes.start_note())

    def test_the_board_line_names_the_voice_it_came_from(self):
        voice_effect_recipes.apply("vocoder")
        self.assertIn("Vocoder Stack", voice_effect_recipes.start_note())

    def test_a_recipe_the_table_does_not_offer_changes_nothing(self):
        voice_effect_recipes.apply("oracle")
        before = voice_effect_builder.values()
        self.assertIsNone(voice_effect_recipes.apply("no_such_voice"))
        self.assertEqual(voice_effect_builder.values(), before)
        self.assertIsNone(voice_effect_recipes.board("no_such_voice"))
        self.assertEqual(voice_effect_recipes.recipe_label("no_such_voice"),
                         voice_effect_recipes.MY_OWN)

    def test_a_picked_recipe_survives_a_restart(self):
        voice_effect_recipes.apply("cloud_choir")
        self.assertEqual(
            self.board["music_bot_voice_effect_recipe"], "cloud_choir")
        # A second visit to the board reads the saved dict, not the press.
        self.assertEqual(voice_effect_builder.values()["pitch"], "choir")
        self.assertEqual(voice_effect_recipes.current_label(), "Cloud Choir")

    def test_every_recipe_is_a_sound_and_never_a_silence(self):
        """Played, not just built: one frame in, one frame out, and audible."""
        signal = to_pcm(tone(seconds=0.30, amplitude=0.4))
        frames = len(signal) // FRAME
        for recipe_id, label, _family, _note, _board in (
                voice_effect_recipes.RECIPES):
            with self.subTest(recipe=label):
                voice_effect_recipes.apply(recipe_id)
                processor = voice_effects._EffectProcessor(
                    voice_effects.BUILDER_EFFECT)
                out = play(processor, signal)
                self.assertEqual(len(out), frames * FRAME // 2)
                self.assertTrue(np.all(np.isfinite(out)))
                self.assertGreater(rms(out), 0.01, f"{label} is silent")
                self.assertLessEqual(float(np.max(np.abs(out))),
                                     voice_effects.PEAK_CEILING + 1e-6)

    def test_a_recipe_costs_a_fraction_of_a_frame(self):
        """The talker's own thread pays this per frame, so it has to fit."""
        signal = to_pcm(tone(seconds=0.20))
        frames = len(signal) // FRAME
        worst = 0.0
        for recipe_id, label, _family, _note, _board in (
                voice_effect_recipes.RECIPES):
            voice_effect_recipes.apply(recipe_id)
            processor = voice_effects._EffectProcessor(
                voice_effects.BUILDER_EFFECT)
            started = time.perf_counter()
            play(processor, signal)
            cost = (time.perf_counter() - started) / frames * 1000.0
            self.assertLess(cost, 12.0, f"{label} costs {cost:.1f} ms a frame")
            worst = max(worst, cost)
        self.assertLess(worst, 12.0)


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
