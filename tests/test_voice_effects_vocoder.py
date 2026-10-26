"""The vocoder family -- twenty machines, and the words still in them.

`libs/music_bot/voice_effects.py` has had one vocoder since the modern pedals:
six bands of the voice drive a 110 Hz saw, the mouth survives and the voice does
not. This file holds the family of twenty that followed it to the same rule, and
to the one thing a vocoder must never lose.

What is pinned here:

1. **The carrier is the instrument.** The note of the saw, its waveform, where
   its band bank sits and whether it has noise on it are the only things that
   change between the twenty -- so each is measured, not asserted from a table:
   the peak of the output sits on the carrier's own note where the carrier is
   pitched, a bigger machine measures lower, a smaller one higher.
2. **A machine that cannot reach the top band is not a machine.** A sine and a
   triangle carrier were both tried and both dropped: on a 140 Hz saw the sine
   put 100% of the output's energy on its own fundamental, the triangle 93%
   below 120 Hz. That is a tone with the words taken out -- the same failure as
   the Whispered family's snare hit -- and `VOCODER_WAVES` refuses it.
3. **Every machine speaks.** On a signal whose formant jumps between two vowels,
   the bottom four bands of the output follow the bottom four of the input frame
   by frame. The top band is left out because speech carries little up there,
   so its correlation is noise even for the vocoder that shipped last week.
4. **Every machine is audible and leaves the chain room**: above the noise floor
   of the test, under `PEAK_CEILING`, and a frame in is a frame out.

No game, OpenAL, ffmpeg or network is required.
"""

import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from libs.music_bot import voice_effects  # noqa: E402

SR = voice_effects.SAMPLE_RATE
FRAME = voice_effects.FRAME_BYTES          # one 20 ms mono16 frame

FAMILY = "synth"


# --------------------------------------------------------------- the signals


def saw(amplitude=0.3, seconds=1.2, freq=140.0):
    t = np.arange(int(SR * seconds)) / SR
    return (amplitude * (2.0 * ((t * freq) % 1.0) - 1.0)).astype(np.float32)


def vowel_pair(seconds=2.0, fundamental=140.0):
    """A signal whose formant jumps between two vowels, every quarter second.

    This is the one thing a vocoder has to carry: not a pitch, but where the
    mouth is. A preset that cannot follow it is a tone with the words taken
    out, whatever it measures elsewhere.
    """
    t = np.arange(int(SR * seconds)) / SR
    formant = np.where((t % 0.5) < 0.25, 700.0, 2600.0)
    signal = np.zeros(t.size, dtype=np.float32)
    for k in range(1, 60):
        freq = fundamental * k
        if freq > 16000.0:
            break
        signal += (
            (1.0 / k)
            * np.exp(-((freq - formant) ** 2) / (2.0 * 320.0 ** 2))
            * np.sin(2.0 * np.pi * freq * t)
        ).astype(np.float32)
    return signal / float(np.max(np.abs(signal))) * 0.25


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


# --------------------------------------------------------------- the measures


def rms(samples):
    return float(np.sqrt(np.mean(np.square(samples))))


def band_share(samples, low, high):
    """The share of a signal's energy between two frequencies."""
    window = np.hanning(len(samples))
    spectrum = np.abs(np.fft.rfft(samples * window)) ** 2
    freqs = np.fft.rfftfreq(len(samples), 1.0 / SR)
    inside = (freqs >= low) & (freqs <= high)
    return float(spectrum[inside].sum() / max(1e-12, spectrum.sum()))


def centroid(samples):
    """Where the middle of the signal's energy sits, in hertz."""
    window = np.hanning(len(samples))
    spectrum = np.abs(np.fft.rfft(samples * window)) ** 2
    freqs = np.fft.rfftfreq(len(samples), 1.0 / SR)
    return float((freqs * spectrum).sum() / max(1e-12, spectrum.sum()))


def flatness(samples):
    """How noise-like a signal is: the geometric mean over the arithmetic one.

    A buzz with one strong note and quiet harmonics scores near zero; a band of
    noise, whose energy is spread, scores far higher. It is what separates the
    noise carrier from every pitched one.
    """
    window = np.hanning(len(samples))
    spectrum = np.abs(np.fft.rfft(samples * window)) ** 2
    return float(np.exp(np.mean(np.log(spectrum + 1e-12)))
                 / max(1e-12, float(np.mean(spectrum))))


def peak_freq(samples, floor=40.0):
    spectrum = np.abs(np.fft.rfft(samples * np.hanning(len(samples))))
    freqs = np.fft.rfftfreq(len(samples), 1.0 / SR)
    above = freqs > floor
    return float(freqs[above][int(np.argmax(spectrum[above]))])


def band_envelopes(samples, frames):
    """Per frame, the RMS in each of the vocoder's own six bands."""
    out = []
    freqs = np.fft.rfftfreq(FRAME, 1.0 / SR)
    for at in range(0, frames * FRAME, FRAME):
        spectrum = np.abs(np.fft.rfft(samples[at:at + FRAME])) ** 2
        out.append([float(np.sqrt(spectrum[(freqs >= hz / 1.26)
                                           & (freqs < hz * 1.26)].sum()))
                    for hz in voice_effects.VOCODER_BANDS])
    return np.array(out)


# --------------------------------------------------------------- the family


class TheVocoderFamilyTests(unittest.TestCase):
    """Twenty machines that speak the words instead of the voice."""

    def machines(self):
        return voice_effects.group_members(FAMILY)

    def played(self, effect_id, signal=None):
        pcm = signal if signal is not None else to_pcm(saw())
        return play(voice_effects._EffectProcessor(effect_id), pcm)[FRAME * 10:]

    def test_the_family_is_twenty_and_nothing_is_lost(self):
        self.assertEqual(voice_effects.catalogue_problems(), [])
        self.assertEqual(len(self.machines()), 20)
        self.assertEqual(voice_effects.group_label(FAMILY), "Vocoder Lab")
        rows = {row[0]: row[1] for row in voice_effects.VOICE_EFFECTS}
        labels = [row[1] for row in voice_effects.VOICE_EFFECTS]
        self.assertEqual(len(labels), len(set(labels)), "two pedals, one name")
        for effect_id in self.machines():
            with self.subTest(preset=effect_id):
                self.assertIn(effect_id, rows)
                self.assertTrue(voice_effects.effect_note(effect_id).strip())

    def test_no_two_machines_are_the_same_spec(self):
        """Twenty settings, not one row copied twenty times."""
        specs = [voice_effects.spec_for(effect_id)
                 for effect_id in self.machines()]
        self.assertEqual(len(specs), 20)
        for index, spec in enumerate(specs):
            with self.subTest(preset=self.machines()[index]):
                self.assertNotIn(spec, specs[:index])

    def test_every_machine_speaks_the_words(self):
        """The voice's band envelopes survive: that is what a vocoder is."""
        pcm = to_pcm(vowel_pair())
        samples = from_pcm(pcm)
        frames = samples.size // FRAME
        wanted = band_envelopes(samples[:frames * FRAME], frames)
        for effect_id in self.machines():
            with self.subTest(preset=effect_id):
                after = play(voice_effects._EffectProcessor(effect_id), pcm)
                got = band_envelopes(after[-frames * FRAME:], frames)
                tracked = [float(np.corrcoef(wanted[:, band], got[:, band])[0, 1])
                           for band in range(4)]
                self.assertGreater(float(np.mean(tracked)), 0.55, tracked)

    def test_every_machine_is_audible_and_leaves_the_chain_room(self):
        for effect_id in self.machines():
            with self.subTest(preset=effect_id):
                after = self.played(effect_id)
                self.assertGreater(rms(after), 0.04)
                self.assertLessEqual(float(np.max(np.abs(after))),
                                     voice_effects.PEAK_CEILING + 1e-6)

    def test_the_carrier_is_the_instrument(self):
        """The note, the band bank and the breath, each one measured."""
        classic = self.played("vox_classic")
        self.assertAlmostEqual(peak_freq(classic), 110.0, delta=6.0)
        self.assertAlmostEqual(peak_freq(self.played("vox_chip")), 220.0,
                               delta=8.0)
        self.assertAlmostEqual(peak_freq(self.played("vox_gnome")), 330.0,
                               delta=10.0)
        # A bigger machine sits lower: the carrier's note and where its bands
        # are both come down, and together they are what the ear hears as size.
        self.assertLess(centroid(self.played("vox_giant")),
                        centroid(self.played("vox_deep")))
        self.assertLess(centroid(self.played("vox_deep")), centroid(classic))
        self.assertGreater(centroid(self.played("vox_gnome")),
                           2.0 * centroid(classic))
        # A noise carrier has no note at all -- that is what makes it a whisper
        # rather than a buzz, and spectral flatness is the measure of it.
        self.assertGreater(flatness(self.played("vox_noise")),
                           20.0 * flatness(classic))
        # A breath of noise on the saw is heard as air above the words, which a
        # band-limited carrier cannot fake.
        self.assertGreater(
            band_share(self.played("vox_breath"), 3800.0, 20000.0),
            2.0 * band_share(classic, 3800.0, 20000.0))

    def test_a_carrier_that_cannot_reach_the_top_band_is_not_one(self):
        """The family's one rule, and the two waveforms it refused.

        A sine and a triangle were both tried and both dropped before this
        shipped: measured on a 140 Hz saw, the sine carrier put 100% of the
        output's energy on its own fundamental and the triangle 93% below
        120 Hz -- a tone where the words used to be. What is left has to stay
        broadband enough to reach the top of speech.
        """
        self.assertNotIn("sine", voice_effects.VOCODER_WAVES)
        self.assertNotIn("triangle", voice_effects.VOCODER_WAVES)
        for effect_id in self.machines():
            with self.subTest(preset=effect_id):
                self.assertGreater(
                    band_share(self.played(effect_id), 900.0, 20000.0), 0.02)


if __name__ == "__main__":
    unittest.main()
