"""The radio family -- twenty channels, from four bits to a cockpit headset.

`libs/music_bot/voice_effects.py` has a `Radio` pedal in Amp & Drive, and it is
a voice through a small speaker. This family is the other half of the idea: what
makes a voice a *transmission* rather than merely a voice through a filter.

The room and desk channels that used to sit at the top of the same ladder are
now the `PA & Venue` and `Broadcast Desk` families. The split is pinned here
beside the radio it came from.

What is pinned here, and why each one is a measurement rather than a table:

1. **The ladder is real.** The bottom of the family is a four-bit toy and the top
   is a cockpit headset, and the difference is mostly the channel's width. So
   the share of the output above 5 kHz is measured at three rungs -- and it has
   to climb, not merely differ.
2. **A radio hisses.** A band-limited voice is a telephone; a radio has a noise
   floor of its own, and that floor is added after the gate precisely so it does
   not follow the words. Measured twice: on silence (the channel is not quiet
   because the speaker is not talking) and across a spoken gap (the floor is the
   same in the gap as in the silence, and well under the words).
3. **Every channel is usable**: audible, under `PEAK_CEILING` for a soft voice
   and for a shout, and a frame in is a frame out.
4. **The descriptions are the menu**: twenty rows, twenty names, no two alike.

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

FAMILY = "broadcast"


# --------------------------------------------------------------- the signals


def saw(amplitude=0.25, seconds=1.4, freq=300.0):
    t = np.arange(int(SR * seconds)) / SR
    return (amplitude * (2.0 * ((t * freq) % 1.0) - 1.0)).astype(np.float32)


def silence(seconds=1.0):
    return np.zeros(int(SR * seconds), dtype=np.float32)


def words_and_a_gap(word=0.4, gap=0.5):
    """A word, a pause, and the same word again."""
    return np.concatenate([saw(0.25, word, 200.0),
                           silence(gap),
                           saw(0.25, word, 240.0)])


def to_pcm(signal):
    return np.clip(signal * 32768.0, -32768, 32767).astype("<i2").tobytes()


def from_pcm(data):
    return np.frombuffer(bytes(data), dtype="<i2").astype(np.float64) / 32768.0


def play(processor, pcm):
    out = bytearray()
    for start in range(0, len(pcm) - FRAME + 1, FRAME):
        shaped = processor.process(pcm[start:start + FRAME])
        assert len(shaped) == FRAME, "the pedal changed a frame's size"
        out += shaped
    return from_pcm(out)


# --------------------------------------------------------------- the measures


def rms(samples):
    return float(np.sqrt(np.mean(np.square(samples))))


def above(samples, corner):
    """The share of the energy a channel puts above one frequency."""
    window = np.hanning(len(samples))
    spectrum = np.abs(np.fft.rfft(samples * window)) ** 2
    freqs = np.fft.rfftfreq(len(samples), 1.0 / SR)
    return float(spectrum[freqs >= corner].sum() / max(1e-12, spectrum.sum()))


# --------------------------------------------------------------- the family


class TheRadioFamilyTests(unittest.TestCase):
    """Twenty channels in a ladder, and the hiss that makes them channels."""

    def channels(self):
        return voice_effects.group_members(FAMILY)

    def played(self, effect_id, signal=None):
        pcm = to_pcm(signal if signal is not None
                     else saw())
        return play(voice_effects._EffectProcessor(effect_id), pcm)[FRAME * 10:]

    def test_the_family_is_twenty_and_nothing_is_lost(self):
        self.assertEqual(voice_effects.catalogue_problems(), [])
        self.assertEqual(len(self.channels()), 20)
        self.assertEqual(voice_effects.group_label(FAMILY), "Radio")
        rows = {row[0]: row[1] for row in voice_effects.VOICE_EFFECTS}
        labels = [row[1] for row in voice_effects.VOICE_EFFECTS]
        self.assertEqual(len(labels), len(set(labels)), "two pedals, one name")
        for effect_id in self.channels():
            with self.subTest(channel=effect_id):
                self.assertIn(effect_id, rows)
                self.assertTrue(voice_effects.effect_note(effect_id).strip())

    def test_the_ladder_climbs(self):
        """From a four-bit toy to a cockpit headset: the top opens up.

        Measured on a 300 Hz saw, so the probe itself carries energy all the way
        up; what is left above 5 kHz is the channel's own corner. The rungs are
        the tiers a player walks through -- toy, air traffic, cockpit -- and
        each has to be wider than the one under it, by a real margin rather
        than by a rounding error. The studios and venue rooms are their own
        shelves now.
        """
        rungs = ["rad_8bit", "rad_airtraffic", "rad_cockpit"]
        shares = {effect_id: above(self.played(effect_id), 5000.0)
                  for effect_id in rungs}
        for lower, higher in zip(rungs, rungs[1:]):
            with self.subTest(lower=lower, higher=higher):
                self.assertGreater(shares[higher], shares[lower], shares)
        self.assertLess(shares["rad_8bit"], 0.001)
        self.assertGreater(shares["rad_cockpit"], 0.002)
        # The neighbouring rungs are not a strict order -- a police set is
        # narrower than a CB on purpose, being built for a siren rather than a
        # conversation -- but the two ends are worlds apart.
        self.assertGreater(shares["rad_cockpit"], 5.0 * shares["rad_8bit"])
        # The narrow end really is narrow: above 3.4 kilohertz a four-bit channel
        # keeps a quarter of a percent, where the cockpit keeps a few.
        self.assertLess(above(self.played("rad_8bit"), 3400.0), 0.01)
        self.assertGreater(above(self.played("rad_cockpit"), 3400.0), 0.02)
        self.assertGreater(above(self.played("rad_cockpit"), 3400.0),
                           5.0 * above(self.played("rad_8bit"), 3400.0))

    def test_a_radio_hisses_when_nobody_is_talking(self):
        """The channel's own floor: silence in, a floor out.

        This is the difference between a radio and a telephone, and it is
        measured with no voice at all -- the noise is added after the gate, so
        the gate closing is exactly what a hiss must survive.
        """
        for effect_id in ("rad_8bit", "rad_cb", "rad_walkie", "rad_am",
                          "rad_shortwave", "rad_numbers"):
            with self.subTest(channel=effect_id):
                floor = rms(play(voice_effects._EffectProcessor(effect_id),
                                 to_pcm(silence()))[FRAME * 8:])
                self.assertGreater(floor, 0.01)
        # The clean shelves are their own families now; their silence is
        # pinned by TheSplitShelvesTests below.

    def test_the_hiss_does_not_follow_the_words(self):
        """A whisper follows the speaker; a channel floor does not.

        A word, a pause and another word: the floor in the pause has to be the
        same as the floor in a channel with nothing ever said into it (within a
        quarter), and well under the words it is under.
        """
        spoken = play(voice_effects._EffectProcessor("rad_cb"),
                      to_pcm(words_and_a_gap()))
        word = rms(spoken[int(0.15 * SR):int(0.35 * SR)])
        gap = rms(spoken[int(0.55 * SR):int(0.80 * SR)])
        silent = rms(play(voice_effects._EffectProcessor("rad_cb"),
                          to_pcm(silence(0.8)))[FRAME * 8:])
        self.assertGreater(gap, 0.01)
        self.assertLess(gap, 0.5 * word)
        self.assertAlmostEqual(gap, silent, delta=0.3 * silent)

    def test_every_channel_is_usable_soft_and_loud(self):
        """A quiet voice and a shout both leave the pedal in one piece."""
        for effect_id in self.channels():
            with self.subTest(channel=effect_id):
                for amplitude in (0.25, 0.9):
                    after = self.played(effect_id, saw(amplitude))
                    self.assertGreater(rms(after), 0.08)
                    self.assertLessEqual(float(np.max(np.abs(after))),
                                         voice_effects.PEAK_CEILING + 1e-6)


VENUE = ("rad_terminal", "rad_station", "rad_stadium", "rad_church",
         "rad_cinema", "rad_trailer")
DESK = ("rad_fm", "rad_studio", "rad_news", "rad_mission")


class TheSplitShelvesTests(unittest.TestCase):
    """The ten channels that left Radio: six rooms and four desks."""

    def test_the_ten_moved_to_two_named_families(self):
        self.assertEqual(voice_effects.catalogue_problems(), [])
        self.assertEqual(voice_effects.group_members("venue"), VENUE)
        self.assertEqual(voice_effects.group_members("desk"), DESK)
        self.assertEqual(voice_effects.group_label("venue"), "PA & Venue")
        self.assertEqual(voice_effects.group_label("desk"), "Broadcast Desk")
        for effect_id in VENUE + DESK:
            with self.subTest(channel=effect_id):
                self.assertNotIn(effect_id, voice_effects.group_members("broadcast"))
                self.assertIn(voice_effects.group_of(effect_id),
                              ("venue", "desk"))
                self.assertTrue(voice_effects.effect_note(effect_id).strip())

    def test_the_clean_shelves_are_quiet_between_words(self):
        for effect_id in VENUE + DESK:
            with self.subTest(channel=effect_id):
                floor = rms(play(voice_effects._EffectProcessor(effect_id),
                                 to_pcm(silence()))[FRAME * 8:])
                self.assertLess(floor, 0.01)

    def test_every_split_channel_is_usable_soft_and_loud(self):
        for effect_id in VENUE + DESK:
            with self.subTest(channel=effect_id):
                for amplitude in (0.25, 0.9):
                    after = play(voice_effects._EffectProcessor(effect_id),
                                 to_pcm(saw(amplitude)))[FRAME * 10:]
                    self.assertGreater(rms(after), 0.08)
                    self.assertLessEqual(float(np.max(np.abs(after))),
                                         voice_effects.PEAK_CEILING + 1e-6)


if __name__ == "__main__":
    unittest.main()
