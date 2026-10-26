"""The Vocoder Lab board: a carrier you can name, not just a preset.

`voice_effect_vocoder.py` is the third knob board on the Voice Effect page and
the only one that is not a whole effect: it configures the carrier of the
six-band vocoder the Vocoder Lab family already uses. What this file holds it
to:

1. **Three modes, one carrier.** Current is the classic 110 Hz saw; Tune Note
   is Root + Octave, and it is deliberately monotone; Interval is that note
   moved by a scale degree. The rows a mode cannot use are hidden, not left on
   the page doing nothing.
2. **The note is the note.** C3 is 130.81 Hz and C4 is middle C, from the same
   equal-temperament formula the rest of the project uses. An interval is
   snapped to the nearest note of the chosen scale, in the direction it asks
   for, so a third is major over one root and minor over another.
3. **Noise has no note.** The noise carrier is allowed in Current and refused
   in Tune Note/Interval, with the refusal spoken and the carrier left on Saw.
   A saved board that had Noise does not come back as a note-locked whisper.
4. **It is one more board, not a second DSP.** `spec()` returns the same
   dictionary `_SPECS` holds, the processor is rebuilt when its revision moves,
   a frame in is a frame out, and the output stays under the peak ceiling.
5. **The Server owns the permission.** `can_use_vocoder_lab` is the only
   answer; a saved board on an account without it reads as Off, which is what
   the capture thread actually asks for.

No game, OpenAL, ffmpeg or network is required.
"""

import os
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from libs.music_bot import MapMusicBot  # noqa: E402
from libs.music_bot import voice_effect_catalogue  # noqa: E402
from libs.music_bot import voice_effect_vocoder  # noqa: E402
from libs.music_bot import voice_effects  # noqa: E402

SR = voice_effects.SAMPLE_RATE
FRAME = voice_effects.FRAME_BYTES


def tone(freq=220.0, amplitude=0.4, seconds=1.0):
    t = np.arange(int(SR * seconds)) / SR
    return (amplitude * np.sin(2.0 * np.pi * freq * t)).astype(np.float32)


def to_pcm(signal):
    return np.clip(signal * 32768.0, -32768, 32767).astype("<i2").tobytes()


def from_pcm(data):
    return np.frombuffer(bytes(data), dtype="<i2").astype(np.float64) / 32768.0


class TheBoardTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(mock.patch.stopall)
        self.addCleanup(voice_effects.set_vocoder_lab_allowed, False)
        voice_effects.set_vocoder_lab_allowed(False)
        self.saved = {}
        mock.patch.object(
            voice_effect_vocoder.options, "get",
            side_effect=lambda key, default=None: self.saved.get(key, default)
        ).start()
        mock.patch.object(
            voice_effect_vocoder.options, "set",
            side_effect=lambda key, value, autosave=True:
            self.saved.__setitem__(key, value)
        ).start()

    def test_a_fresh_board_is_the_classic_machine(self):
        values = voice_effect_vocoder.values()
        for param_id, _name, _note, _choices, default in (
                voice_effect_vocoder.PARAMS):
            self.assertEqual(values[param_id], default)
        spec = voice_effect_vocoder.spec()
        self.assertEqual(spec["vocoder"], (2.6, 0.90, 110.0, "saw", 1.0, 0.0))
        self.assertEqual(voice_effect_vocoder.summary(), "Current 110 Hz")

    def test_every_row_has_a_name_a_description_and_a_valid_default(self):
        for param_id, name, note, choices, default in (
                voice_effect_vocoder.PARAMS):
            with self.subTest(row=param_id):
                self.assertTrue(name.strip())
                self.assertGreater(len(note), 60)
                stored = [choice[1] for choice in choices]
                self.assertIn(default, stored)
                for label, value, choice_note in choices:
                    self.assertTrue(label.strip())
                    self.assertTrue(str(value).strip())
                    self.assertGreater(len(choice_note), 30)

    def test_current_hides_the_note_rows_and_tune_hides_the_scale(self):
        def shown():
            return [row[0] for row in voice_effect_vocoder.rows()]

        self.assertEqual(shown(), ["mode", "wave", "shift", "breath", "mix"])
        voice_effect_vocoder.set_value("mode", "tune")
        self.assertIn("root", shown())
        self.assertIn("octave", shown())
        self.assertNotIn("scale", shown())
        self.assertNotIn("interval", shown())
        voice_effect_vocoder.set_value("mode", "interval")
        self.assertIn("scale", shown())
        self.assertIn("interval", shown())

    def test_a_tune_note_is_the_note_it_names(self):
        voice_effect_vocoder.set_value("mode", "tune")
        voice_effect_vocoder.set_value("root", "C")
        voice_effect_vocoder.set_value("octave", "3")
        self.assertAlmostEqual(voice_effect_vocoder.note_hz("C", 3),
                               130.8128, places=3)
        self.assertAlmostEqual(
            voice_effect_vocoder.carrier_hz(), 130.8128, places=3)
        self.assertAlmostEqual(
            voice_effect_vocoder.spec()["vocoder"][2], 130.8128, places=3)
        self.assertEqual(voice_effect_vocoder.summary(),
                         "Tune Note C3 (130.81 Hz)")
        voice_effect_vocoder.set_value("octave", "4")
        self.assertAlmostEqual(voice_effect_vocoder.note_hz("C", 4),
                               261.6256, places=3)
        self.assertAlmostEqual(voice_effect_vocoder.note_hz("A", 4), 440.0,
                               places=6)

    def test_an_interval_is_spelled_in_the_scale(self):
        voice_effect_vocoder.set_value("mode", "interval")
        voice_effect_vocoder.set_value("root", "C")
        voice_effect_vocoder.set_value("octave", "3")
        voice_effect_vocoder.set_value("interval", "third")
        voice_effect_vocoder.set_value("scale", "major")
        self.assertEqual(
            voice_effect_vocoder.interval_semitones("major", "C", "third"), 4)
        self.assertAlmostEqual(
            voice_effect_vocoder.carrier_hz(), 164.8138, places=3)
        voice_effect_vocoder.set_value("scale", "minor")
        self.assertEqual(
            voice_effect_vocoder.interval_semitones("minor", "C", "third"), 3)
        self.assertAlmostEqual(
            voice_effect_vocoder.carrier_hz(), 155.5635, places=3)
        self.assertEqual(voice_effect_vocoder.summary(),
                         "Interval C3 + Third (Minor)")

    def test_the_plain_sizes_are_used_on_a_chromatic_grid(self):
        self.assertEqual(
            voice_effect_vocoder.interval_semitones("chromatic", "C", "third"),
            4)
        self.assertEqual(
            voice_effect_vocoder.interval_semitones("chromatic", "C",
                                                    "bass_third"), -3)
        self.assertEqual(
            voice_effect_vocoder.interval_semitones("chromatic", "C",
                                                    "bass_fifth"), -5)
        self.assertEqual(
            voice_effect_vocoder.interval_semitones("chromatic", "C",
                                                    "bass_octave"), -12)

    def test_a_bass_third_is_snapped_like_the_harmony_menu(self):
        # C major keeps A (a minor third below C); C minor has no A, so the
        # nearest note below is Ab -- a major third. E major has no C#, so the
        # nearest note below is C, also a major third.
        self.assertEqual(
            voice_effect_vocoder.interval_semitones("major", "C",
                                                    "bass_third"), -3)
        self.assertEqual(
            voice_effect_vocoder.interval_semitones("minor", "C",
                                                    "bass_third"), -4)
        self.assertEqual(
            voice_effect_vocoder.interval_semitones("major", "E",
                                                    "bass_third"), -4)
        self.assertEqual(
            voice_effect_vocoder.interval_semitones("major", "C",
                                                    "bass_fifth"), -5)
        self.assertEqual(
            voice_effect_vocoder.interval_semitones("major", "C",
                                                    "octave"), 12)

    def test_noise_is_refused_when_a_note_is_locked(self):
        voice_effect_vocoder.set_value("mode", "tune")
        self.assertEqual(voice_effect_vocoder.set_value("wave", "noise"),
                         "saw")
        self.assertEqual(voice_effect_vocoder.values()["wave"], "saw")
        self.assertEqual(voice_effect_vocoder.spec()["vocoder"][3], "saw")
        self.assertIn("Noise has no note",
                      voice_effect_vocoder.refusal("wave", "noise", "saw"))
        # Current is allowed the noise carrier the family already has.
        voice_effect_vocoder.set_value("mode", "current")
        self.assertEqual(voice_effect_vocoder.set_value("wave", "noise"),
                         "noise")
        self.assertEqual(voice_effect_vocoder.spec()["vocoder"][3], "noise")
        # ... and switching away takes the noise with it, in one save.
        self.assertEqual(voice_effect_vocoder.set_value("mode", "interval"),
                         "interval")
        self.assertEqual(voice_effect_vocoder.values()["wave"], "saw")

    def test_a_saved_board_that_moved_or_lost_a_row_still_reads(self):
        self.saved[voice_effect_vocoder._OPTION] = {"mode": "tune"}
        values = voice_effect_vocoder.values()
        self.assertEqual(values["root"], "C")
        self.assertEqual(values["octave"], "3")
        self.assertEqual(values["wave"], "saw")
        self.saved[voice_effect_vocoder._OPTION] = {
            "mode": "tune", "wave": "noise"}
        self.assertEqual(voice_effect_vocoder.values()["wave"], "saw")

    def test_an_unknown_value_keeps_the_default_and_an_unknown_row_is_none(self):
        self.assertEqual(voice_effect_vocoder.set_value("mode", "nonsense"),
                         "current")
        self.assertIsNone(voice_effect_vocoder.set_value("no_such_row", "x"))
        self.assertEqual(voice_effect_vocoder.values()["mode"], "current")

    def test_the_board_round_trips_a_frame_like_every_pedal(self):
        voice_effect_vocoder.set_value("mode", "tune")
        processor = voice_effects._EffectProcessor(voice_effects.VOX_LAB_EFFECT)
        pcm = to_pcm(tone(seconds=0.2))
        for start in range(0, len(pcm) - FRAME + 1, FRAME):
            shaped = processor.process(pcm[start:start + FRAME])
            self.assertEqual(len(shaped), FRAME)
            self.assertLess(np.max(np.abs(from_pcm(shaped))), 0.71)

    def test_a_row_change_is_a_new_revision_and_rebuilds_the_line(self):
        voice_effect_vocoder.set_value("mode", "tune")
        line = voice_effects.VoiceEffectLine()
        pcm = to_pcm(tone(seconds=0.2))
        voice_effects.set_vocoder_lab_allowed(True)
        with mock.patch.object(voice_effects, "effect_id",
                               return_value=voice_effects.VOX_LAB_EFFECT):
            first = line.shape(pcm[:FRAME])
            self.assertEqual(len(first), FRAME)
            processor = line._processor
            self.assertIsNotNone(processor)
            voice_effect_vocoder.set_value("root", "D")
            line.shape(pcm[:FRAME])
            self.assertIsNot(line._processor, processor)

    def test_the_board_is_one_line_in_the_vocoder_family(self):
        ids = [row[0] for row in voice_effects.VOICE_EFFECTS]
        self.assertIn(voice_effects.VOX_LAB_EFFECT, ids)
        self.assertEqual(voice_effects.group_of(voice_effects.VOX_LAB_EFFECT),
                         "synth")
        self.assertNotIn(voice_effects.VOX_LAB_EFFECT,
                         voice_effects.STANDALONE_EFFECTS)
        note = voice_effects.effect_note(voice_effects.VOX_LAB_EFFECT)
        self.assertIn("Current 110 Hz", note)
        self.assertIn("board", note.lower())
        family_note = voice_effects.group_note("synth")
        self.assertIn("board", family_note.lower())
        self.assertEqual(voice_effects.catalogue_problems(), [])
        self.assertEqual(voice_effect_catalogue.catalogue_problems(), [])

    def test_the_board_note_follows_the_board(self):
        voice_effect_vocoder.set_value("mode", "interval")
        voice_effect_vocoder.set_value("interval", "fifth")
        note = voice_effects.effect_note(voice_effects.VOX_LAB_EFFECT)
        self.assertIn("Interval C3 + Fifth (Major)", note)
        self.assertIn("saw wave", note.lower())


class ThePermissionTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(mock.patch.stopall)
        self.addCleanup(voice_effects.set_vocoder_lab_allowed, False)

    def bot(self, allowed):
        bot = MapMusicBot.__new__(MapMusicBot)
        bot._find_gameplay = lambda: SimpleNamespace(
            can_use_vocoder_lab=bool(allowed))
        return bot

    def test_the_server_flag_is_the_only_answer(self):
        bot = self.bot(False)
        self.assertFalse(bot.vocoder_lab_allowed())
        self.assertFalse(voice_effects.vocoder_lab_allowed())
        bot = self.bot(True)
        self.assertTrue(bot.vocoder_lab_allowed())
        # ... and the audio path's copy moves with it, with no second source.
        self.assertTrue(voice_effects.vocoder_lab_allowed())

    def test_setting_the_board_without_permission_falls_back_to_off(self):
        bot = self.bot(False)
        with mock.patch.object(voice_effects.options, "set") as saved:
            self.assertEqual(bot.set_voice_effect("vox_lab"), "off")
            saved.assert_called_once_with(voice_effects.OPTION, "off")
        self.assertEqual(bot.voice_effect, "off")
        bot = self.bot(True)
        with mock.patch.object(voice_effects.options, "set") as saved:
            self.assertEqual(bot.set_voice_effect("vox_lab"),
                             voice_effects.VOX_LAB_EFFECT)
            saved.assert_called_once_with(voice_effects.OPTION, "vox_lab")

    def test_a_saved_board_without_the_server_permission_reads_as_off(self):
        with mock.patch.object(voice_effects.options, "get",
                               side_effect=lambda key, default=None: "vox_lab"):
            with mock.patch.object(voice_effects.gate, "is_locked",
                                   return_value=False):
                voice_effects.set_vocoder_lab_allowed(False)
                self.assertEqual(voice_effects.effect_id(), "off")
                self.assertFalse(voice_effects.enabled())
                voice_effects.set_vocoder_lab_allowed(True)
                self.assertEqual(voice_effects.effect_id(), "vox_lab")
                self.assertTrue(voice_effects.enabled())
