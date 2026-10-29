"""The four tone bands a voice effect carries of its own -- pinned.

`voice_effects/tone.py` is the setting and `dsp._ToneStack` is the filter, and
what this file holds them to is:

1. **Flat is not a filter.** A preset nobody has equalised is byte-for-byte the
   pedal it was, which is the whole reason the shelf can gain a tone control
   without re-tuning a hundred and forty-four sounds that were measured by ear.
2. **Each band moves its own place in the voice**, and not its neighbours: a
   low shelf takes the chest, a bell takes the mud or the words, a high shelf
   takes the breath. Measured on tones, not read from the table.
3. **A band moves without a click.** The stage glides to its saved value over a
   few frames and keeps the filter's state while it does, so a slider press is
   not a step at a frame boundary.
4. **The tone belongs to one preset and one path.** A tone saved for `Radio`
   does not colour the in-game radio a player listens to (the receive path
   builds the same processor and does not ask for the bands), and moving a
   band on one preset leaves every other preset exactly where it was.
5. **It is a slider a blind player can use.** Tab, arrows, Page Up/Down, Home
   and `R`, with every press spoken, saved once when the page closes.
6. **The three controls around the bands are a mixer's.** A low cut takes the
   rumble off the bottom before the bands see it, a mix puts the pedal beside
   the voice instead of over it, a level matches one preset to the next, and
   `M` speaks the level of the last frame that left.

No game, OpenAL, ffmpeg or network is required.
"""

import os
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

import numpy as np
import pygame

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from libs import menu as menu_module  # noqa: E402
from libs.music_bot import gate as music_bot_gate  # noqa: E402
from libs.music_bot import menus as menus_module  # noqa: E402
from libs.music_bot.menu_pages import voice_effect as voice_effect_pages  # noqa: E402
from libs.music_bot.voice_effects import dsp  # noqa: E402
from libs.music_bot.voice_effects import tone  # noqa: E402
from libs.music_bot import voice_effects  # noqa: E402

SR = voice_effects.SAMPLE_RATE
FRAME = voice_effects.FRAME_BYTES          # one 20 ms mono16 frame


# --------------------------------------------------------------- the signals

def sinusoid(freq=400.0, amplitude=0.05, seconds=0.5):
    t = np.arange(int(SR * seconds)) / SR
    return (amplitude * np.sin(2.0 * np.pi * freq * t)).astype(np.float32)


def noise(amplitude=0.2, seconds=0.5, seed=11):
    rng = np.random.default_rng(seed)
    return (rng.uniform(-1.0, 1.0, int(SR * seconds)) * amplitude).astype(
        np.float32)


def to_pcm(signal):
    return np.clip(signal * 32768.0, -32768, 32767).astype("<i2").tobytes()


def from_pcm(data):
    return np.frombuffer(bytes(data), dtype="<i2").astype(np.float64) / 32768.0


def play(processor, signal):
    """Push a whole signal through a processor, frame by frame."""
    pcm = to_pcm(signal)
    out = bytearray()
    for start in range(0, len(pcm) - FRAME + 1, FRAME):
        shaped = processor.process(pcm[start:start + FRAME])
        assert len(shaped) == FRAME, "the tone stage changed a frame's size"
        out += shaped
    return from_pcm(out)


def rms(samples):
    return float(np.sqrt(np.mean(np.square(samples))))


def gain_db(after, before):
    """How much a band moved a tone, in decibels."""
    return 20.0 * np.log10(max(1e-12, rms(after)) / max(1e-12, rms(before)))


def frames_of(processor, signal):
    """Every shaped frame as an array, for the click check."""
    pcm = to_pcm(signal)
    return [from_pcm(processor.process(pcm[start:start + FRAME]))
            for start in range(0, len(pcm) - FRAME + 1, FRAME)]


# --------------------------------------------------------------- option store

class ToneCase(unittest.TestCase):
    """A test's own settings file: the developer's real one is never touched."""

    def setUp(self):
        self.store = {}
        mock.patch.object(
            tone.options, "get",
            side_effect=lambda key, default=None: self.store.get(key, default)
        ).start()
        mock.patch.object(
            tone.options, "set",
            side_effect=lambda key, value, autosave=True:
            self.store.__setitem__(key, value)
        ).start()
        self.addCleanup(mock.patch.stopall)
        self.addCleanup(music_bot_gate.clear)
        music_bot_gate.clear()


# ------------------------------------------------------------------- the table

class TheToneTableTests(ToneCase):
    def test_four_bands_in_the_order_tab_walks_them(self):
        self.assertEqual([row[1] for row in tone.BANDS],
                         ["Low", "Body", "Presence", "Air"])
        self.assertEqual([row[0] for row in tone.BANDS],
                         ["low", "body", "presence", "air"])
        # The two ends are shelves and the two middles are bells: that is the
        # difference between "less top" and "less hiss on the s".
        self.assertEqual([row[3] for row in tone.BANDS],
                         ["lowshelf", "bell", "bell", "highshelf"])
        self.assertEqual(tone.BAND_IDS, tuple(row[0] for row in tone.BANDS))
        self.assertEqual(tone.MAX_DB, 12)

    def test_an_untouched_preset_is_flat_and_costs_no_setting(self):
        self.assertEqual(tone.summary("fuzz"), "Flat")
        self.assertEqual(tone.gains("fuzz"), (0, 0, 0, 0))
        self.assertFalse(tone.has_tone("fuzz"))
        self.assertEqual(tone.description("fuzz"),
                         "Flat: this effect is exactly as it was made.")
        self.assertEqual(self.store, {})

    def test_a_moved_band_is_named_with_its_sign(self):
        tone.set_band("fuzz", "low", 3)
        tone.set_band("fuzz", "air", -2)
        self.assertEqual(tone.summary("fuzz"), "+3 Low, -2 Air")
        self.assertIn("plus 3 decibels on Low", tone.description("fuzz"))
        self.assertIn("minus 2 decibels on Air", tone.description("fuzz"))
        self.assertTrue(tone.has_tone("fuzz"))

    def test_values_are_clamped_and_flat_is_an_absent_key(self):
        self.assertEqual(tone.set_band("amp", "body", 40), 12)
        self.assertEqual(tone.set_band("amp", "presence", -40), -12)
        self.assertEqual(tone.values("amp"),
                         {"low": 0, "body": 12, "presence": -12, "air": 0})
        tone.set_band("amp", "body", 0)
        self.assertEqual(self.store[tone._OPTION]["amp"],
                         {"presence": -12})
        # A band the table does not offer writes nothing at all.
        self.assertIsNone(tone.set_band("amp", "bass", 4))
        # ... and a saved value an older file left out of range is read back
        # inside it rather than handed to a filter.
        self.store[tone._OPTION]["amp"]["presence"] = 99
        self.assertEqual(tone.gains("amp"), (0, 0, 12, 0))

    def test_one_preset_is_one_key_and_reset_forgets_only_that_one(self):
        tone.set_band("amp", "low", 4)
        tone.set_band("fuzz", "air", 6)
        self.assertEqual(set(self.store[tone._OPTION]), {"amp", "fuzz"})
        tone.reset("amp")
        self.assertEqual(tone.values("amp")["low"], 0)
        self.assertEqual(tone.values("fuzz")["air"], 6)

    def test_every_band_is_a_choice_the_engine_understands(self):
        for band_id, _label, hz, kind, q, note in tone.BANDS:
            with self.subTest(band=band_id):
                self.assertIn(kind, ("lowshelf", "bell", "highshelf"))
                self.assertGreater(hz, 20.0)
                self.assertLess(hz, SR / 2.0)
                if kind == "bell":
                    self.assertGreater(q, 0.0)
                self.assertTrue(note.strip())


# ------------------------------------------------------------------- the stage

class TheToneStageTests(ToneCase):
    def test_flat_is_byte_for_byte_the_pedal_it_was(self):
        signal = noise()
        for preset in ("amp", "fuzz", "rad_cb", "choir"):
            with self.subTest(preset=preset):
                plain = _all_bytes(dsp._EffectProcessor(preset), signal)
                toned = _all_bytes(dsp._EffectProcessor(preset, with_tone=True),
                                   signal)
                self.assertEqual(plain, toned)

    def test_a_tone_saved_for_another_preset_does_not_follow_it(self):
        tone.set_band("fuzz", "low", 12)
        signal = sinusoid(100.0)
        plain = _all_bytes(dsp._EffectProcessor("amp"), signal)
        toned = _all_bytes(dsp._EffectProcessor("amp", with_tone=True), signal)
        self.assertEqual(plain, toned)

    def test_each_band_moves_its_own_place_in_the_voice(self):
        cases = (("low", 100.0, 3000.0),
                 ("body", 400.0, 9000.0),
                 ("presence", 2800.0, 100.0),
                 ("air", 9000.0, 400.0))
        for band_id, at_centre, elsewhere in cases:
            with self.subTest(band=band_id):
                flat_centre = play(dsp._EffectProcessor("direct"),
                                   sinusoid(at_centre))
                flat_elsewhere = play(dsp._EffectProcessor("direct"),
                                      sinusoid(elsewhere))
                tone.set_band("direct", band_id, 12)
                up_centre = play(dsp._EffectProcessor("direct", with_tone=True),
                                 sinusoid(at_centre))
                up_elsewhere = play(
                    dsp._EffectProcessor("direct", with_tone=True),
                    sinusoid(elsewhere))
                self.assertGreater(gain_db(up_centre, flat_centre), 4.5,
                                   f"{band_id} does not lift its own band")
                self.assertLess(abs(gain_db(up_elsewhere, flat_elsewhere)), 2.0,
                                f"{band_id} moved a band it does not own")
                tone.reset("direct")

    def test_a_cut_is_the_same_band_the_other_way(self):
        flat = play(dsp._EffectProcessor("direct"), sinusoid(400.0))
        tone.set_band("direct", "body", -12)
        cut = play(dsp._EffectProcessor("direct", with_tone=True),
                   sinusoid(400.0))
        self.assertLess(gain_db(cut, flat), -7.0)

    def test_every_fixed_preset_round_trips_at_both_ends_of_the_range(self):
        # The three boards read their own saved tables, so they are driven by
        # their own tests; what this pins is the shelf the tone sits behind.
        signal = noise(0.2, 0.1)
        for preset in dsp._SPECS:
            for value in (12, -12):
                for row in tone.BANDS:
                    tone.set_band(preset, row[0], value, save=False)
                with self.subTest(preset=preset, db=value):
                    processor = dsp._EffectProcessor(preset, with_tone=True)
                    for start in range(0, len(to_pcm(signal)), FRAME):
                        shaped = processor.process(
                            to_pcm(signal)[start:start + FRAME])
                        self.assertEqual(len(shaped), FRAME)
                        samples = from_pcm(shaped)
                        self.assertTrue(np.all(np.isfinite(samples)))
                        self.assertLess(float(np.max(np.abs(samples))), 1.0)
                    tone.reset(preset, save=False)

    def test_a_boosted_fixed_preset_is_levelled_for_the_room(self):
        """Flat is the pedal as tuned; moved, it is a loud board and is trimmed.

        `_loudness_trim` is normally a board's (a fixed preset was tuned by ear
        against a PA). A preset with a band pushed up is no longer that
        measurement, so from the first decibel it is held to `VOICE_LOUDNESS`
        instead of arriving at a room's compressor as a square wave.
        """
        signal = sinusoid(400.0, 0.5)
        flat_processor = dsp._EffectProcessor("direct")
        flat = play(flat_processor, signal)
        self.assertFalse(flat_processor._levelled)
        tone.set_band("direct", "body", 12)
        hot_processor = dsp._EffectProcessor("direct", with_tone=True)
        hot = play(hot_processor, signal)
        self.assertTrue(hot_processor._tone.active())
        self.assertGreater(rms(flat), voice_effects.VOICE_LOUDNESS)
        self.assertLessEqual(rms(hot[SR // 4:]),
                             voice_effects.VOICE_LOUDNESS * 1.2)
        # ... and flat again is not levelled, so the pedal is back as it was.
        tone.reset("direct")
        again = play(dsp._EffectProcessor("direct", with_tone=True), signal)
        self.assertEqual(rms(again), rms(flat))

    def test_a_band_glides_to_its_value_instead_of_jumping(self):
        processor = dsp._EffectProcessor("direct", with_tone=True)
        pcm = to_pcm(sinusoid(400.0))
        processor.process(pcm[:FRAME])
        self.assertEqual(processor._tone._applied, [0.0, 0.0, 0.0, 0.0])
        tone.set_band("direct", "body", 12)
        for _ in range(4):
            processor.process(pcm[:FRAME])
        self.assertEqual(processor._tone._applied[1], 12.0)

    def test_a_moved_band_does_not_click_at_a_frame_boundary(self):
        processor = dsp._EffectProcessor("amp", with_tone=True)
        pcm = to_pcm(sinusoid(180.0, 0.15, 0.6))
        frames = []
        for index, start in enumerate(range(0, len(pcm) - FRAME + 1, FRAME)):
            if index == 5:
                tone.set_band("amp", "low", 12)
            if index == 15:
                tone.set_band("amp", "low", -12)
            frames.append(from_pcm(processor.process(pcm[start:start + FRAME])))
        # A click is a step at a frame edge bigger than the waveform's own
        # movement; every seam the moves make stays inside that movement.
        inside = max(float(np.max(np.abs(np.diff(frame))))
                     for frame in frames[2:])
        seams = [abs(float(frames[index][0] - frames[index - 1][-1]))
                 for index in range(2, len(frames))]
        self.assertLessEqual(max(seams), inside)

    def test_flattening_a_tone_releases_the_room_trim(self):
        """A flat tone must not switch the trim off on the frame it lands.

        The gain the trim was holding is still on the last frame, so returning
        the block untrimmed the moment `active()` goes false is a step of that
        gain -- the same click the 0 dB latch fixes, one frame later.
        """
        processor = dsp._EffectProcessor("direct", with_tone=True)
        pcm = to_pcm(sinusoid(400.0, 0.5, 3.0))
        levels = []
        for index, start in enumerate(range(0, len(pcm) - FRAME + 1, FRAME)):
            if index == 5:
                tone.set_band("direct", "body", 12)
            if index == 20:
                tone.reset("direct")
            levels.append(rms(from_pcm(
                processor.process(pcm[start:start + FRAME]))))
        # No step at the flatten: the level climbs as the gain releases.
        self.assertLess(max(levels[20:24]), max(levels[18:20]) * 1.35)
        self.assertLess(levels[21], levels[20] * 1.1)
        # ... and the pedal ends up exactly itself again: no band left on and
        # the trim back at unity, which is the byte-for-byte state a fresh
        # processor is in (pinned by the flat test above).
        self.assertAlmostEqual(processor._loudness_gain, 1.0, places=4)
        self.assertFalse(processor._tone.active())
    def test_a_band_gliding_past_zero_does_not_burst(self):
        """The trim must not release on the frame a band crosses 0 dB.

        The glide from +12 to -12 passes through 0 on its way, and a loudness
        trim that read only the applied gain released for that one frame -- a
        riptide of 2x the level in the middle of the movement, heard as a thump
        under the words. `_ToneStack.active()` therefore latches until the
        saved value is flat too.
        """
        processor = dsp._EffectProcessor("amp", with_tone=True)
        pcm = to_pcm(sinusoid(180.0, 0.15, 0.6))
        levels = []
        before = None
        for index, start in enumerate(range(0, len(pcm) - FRAME + 1, FRAME)):
            if index == 5:
                tone.set_band("amp", "low", 12)
            if index == 20:
                before = levels[-1]
                tone.set_band("amp", "low", -12)
            levels.append(rms(from_pcm(
                processor.process(pcm[start:start + FRAME]))))
        self.assertIsNotNone(before)
        self.assertLessEqual(max(levels[20:]), before * 1.25)
        self.assertGreater(rms(play(dsp._EffectProcessor("amp"),
                                    sinusoid(180.0, 0.15, 0.6))), 0.05)

    def test_the_radio_receive_path_does_not_take_the_saved_tone(self):
        """The same processor class is the in-game radio; only the line asks."""
        tone.set_band("radio", "low", 12)
        tone.set_band("radio", "air", 12)
        signal = sinusoid(400.0, 0.1)
        receive = _all_bytes(dsp._EffectProcessor("radio"), signal)
        self.store = {}
        dry = _all_bytes(dsp._EffectProcessor("radio"), signal)
        self.assertEqual(receive, dry)
        # The broadcast line is the one that asks for the bands.
        self.store = {tone._OPTION: {"radio": {"low": 12, "air": 12}}}
        line = _all_bytes(dsp._EffectProcessor("radio", with_tone=True), signal)
        self.assertNotEqual(line, dry)

    def test_the_broadcast_line_is_the_thing_that_asks(self):
        """`VoiceEffectLine` is the only caller that asks for the bands."""
        self.store[dsp.OPTION] = "amp"
        tone.set_band("amp", "low", 12)
        frame = to_pcm(sinusoid(100.0, 0.05, 0.02))[:FRAME]
        shaped = dsp.VoiceEffectLine().shape(frame)
        dry = dsp._EffectProcessor("amp").process(frame)
        self.assertNotEqual(shaped, dry)
        self.assertEqual(len(shaped), FRAME)


def _all_bytes(processor, signal):
    pcm = to_pcm(signal)
    out = bytearray()
    for start in range(0, len(pcm) - FRAME + 1, FRAME):
        out += processor.process(pcm[start:start + FRAME])
    return bytes(out)


# ------------------------------------------------------------------ the slider

class FakeMenu:
    """The menu builder's record of what it would have shown."""

    built = []

    def __init__(self, game, title, **kwargs):
        self.game = game
        self.title = title
        self.items = []
        FakeMenu.built.append(self)

    def add_items(self, items):
        self.items.extend(items)

    def speak_current_item(self):
        return None


def label_of(item):
    return item[0]() if callable(item[0]) else item[0]


class FakeGameplay:
    """A substate stack that actually pushes and pops."""

    def __init__(self):
        self.substates = []

    def add_substate(self, item):
        self.substates.append(item)
        return item

    def pop_last_substate(self):
        return self.substates.pop() if self.substates else None

    can_broadcast_megaphone = True


def make_bot(effect="amp"):
    bot = mock.MagicMock()
    bot.voice_effect = effect
    bot.voice_effect_label.return_value = voice_effects.effect_label(effect)
    bot._find_gameplay.return_value = FakeGameplay()
    return bot


class TheToneSliderTests(ToneCase):
    def setUp(self):
        super().setUp()
        FakeMenu.built = []
        mock.patch("libs.menu.Menu", FakeMenu).start()
        mock.patch("libs.menus.set_default_sounds").start()
        self.said = []
        mock.patch.object(menus_module, "speak",
                          side_effect=self.said.append).start()
        mock.patch("libs.options.save").start()
        self.bot = make_bot()
        self.handler = menus_module.MusicBotMenus(self.bot)
        self.gp = self.bot._find_gameplay.return_value

    def _open(self, preset="amp", stored=None):
        if stored:
            self.store[tone._OPTION] = stored
        self.handler._open_voice_tone_menu(preset)
        self.assertEqual(len(self.gp.substates), 1)
        slider = self.gp.substates[-1]
        self.assertIsInstance(slider,
                              voice_effect_pages._VoiceToneSlider)
        slider.enter()
        return slider

    def _press(self, slider, key, mod=0):
        return slider.update([pygame.event.Event(pygame.KEYDOWN, key=key,
                                                 mod=mod)])

    def test_the_page_opens_on_the_saved_strip(self):
        slider = self._open("amp", {"amp": {"low": 3, "cut": 80,
                                            "mix": 60, "level": -2}})
        self.assertEqual(slider.preset, "amp")
        self.assertEqual(slider.name, "Guitar Amp")
        self.assertEqual(slider.values["low"], 3)
        self.assertEqual(slider.values["air"], 0)
        self.assertEqual(slider.values["cut"], 80)
        self.assertEqual(slider.values["mix"], 60)
        self.assertEqual(slider.values["level"], -2)
        # The cut is the first row, because it is what the bands see first.
        self.assertEqual(slider.current_index, 0)

    def test_a_band_survives_a_restart(self):
        """The setting is in the options file, so a new page reads it back."""
        slider = self._open("amp")
        self._press(slider, pygame.K_TAB)          # Low Cut -> Low
        self._press(slider, pygame.K_UP)
        self._press(slider, pygame.K_TAB)          # Low -> Body
        self._press(slider, pygame.K_PAGEUP)
        self._press(slider, pygame.K_RETURN)
        self.gp.substates.clear()
        again = self._open("amp")
        self.assertEqual(again.values["low"], 1)
        self.assertEqual(again.values["body"], 6)

    def test_tab_arrows_page_and_home_move_one_band_and_speak_it(self):
        slider = self._open("amp")
        self._press(slider, pygame.K_TAB)
        self.assertEqual(slider.current_index, 1)
        self._press(slider, pygame.K_UP)
        self.assertEqual(tone.values("amp")["low"], 1)
        self.assertEqual(slider.values["low"], 1)
        self._press(slider, pygame.K_PAGEUP)
        self.assertEqual(tone.values("amp")["low"], 7)
        self._press(slider, pygame.K_DOWN)
        self.assertEqual(tone.values("amp")["low"], 6)
        self._press(slider, pygame.K_HOME)
        self.assertEqual(tone.values("amp")["low"], 0)
        # Shift+Tab walks backwards and stops at the cut: it does not wrap.
        self._press(slider, pygame.K_TAB, mod=pygame.KMOD_SHIFT)
        self.assertEqual(slider.current_index, 0)
        self._press(slider, pygame.K_TAB, mod=pygame.KMOD_SHIFT)
        self.assertEqual(slider.current_index, 0)
        said = " ".join(self.said).lower()
        self.assertIn("low", said)
        self.assertIn("flat", said)

    def test_tab_stops_at_the_last_row_and_reads_it_again(self):
        """Tab at the bottom keeps Level, and still speaks it.

        A ring of thirteen rows sends the press after the last row back to
        the cut, which is a player setting Level and finding themselves at
        the bottom of the voice. The end row is read again instead, so the
        press says where the walk stopped.
        """
        slider = self._open("amp")
        last = len(tone.ROW_IDS) - 1
        for _ in range(last):
            self._press(slider, pygame.K_TAB)
        self.assertEqual(slider.current_index, last)
        self.assertEqual(slider._current()[0], "level")
        self.said.clear()
        for _ in range(4):
            self._press(slider, pygame.K_TAB)
            self.assertEqual(slider.current_index, last)
        # Four presses at the end of the strip: four readings of Level, none
        # of them the cut, and the page has not closed or moved a value.
        self.assertEqual(len(self.said), 4)
        for line in self.said:
            self.assertTrue(line.lower().startswith("level"), line)
        self.assertFalse(slider._closed)
        self.assertFalse(slider._touched)
        # And Shift+Tab from there walks back into the strip, not to the cut.
        self._press(slider, pygame.K_TAB, mod=pygame.KMOD_SHIFT)
        self.assertEqual(slider.current_index, last - 1)

    def test_the_band_is_clamped_and_the_page_keeps_working(self):
        slider = self._open("amp")
        self._press(slider, pygame.K_TAB)          # the cut is first
        for _ in range(20):
            self._press(slider, pygame.K_PAGEUP)
        self.assertEqual(tone.values("amp")["low"], 12)
        for _ in range(20):
            self._press(slider, pygame.K_PAGEDOWN)
        self.assertEqual(tone.values("amp")["low"], -12)
        self.assertFalse(slider._closed)

    def test_r_puts_the_whole_strip_back_flat(self):
        slider = self._open("fuzz", {
            "fuzz": {"cut": 80, "low": 4, "body": -3, "presence": 2,
                     "air": 6, "mix": 55, "comp": -24, "ratio": 8,
                     "attack": 3, "release": 500, "level": -2}})
        self._press(slider, pygame.K_r)
        self.assertEqual(tone.summary("fuzz"), "Flat")
        self.assertEqual(slider.values,
                         {"cut": 0, "low": 0, "body": 0, "presence": 0,
                          "air": 0, "deess": 0, "highcut": 0, "mix": 100,
                          "comp": 0, "ratio": 4, "attack": 10,
                          "release": 250, "level": 0})
        self.assertIn("every row back", " ".join(self.said).lower())

    def test_a_visit_saves_once_on_close_and_not_on_every_press(self):
        slider = self._open("amp")
        self._press(slider, pygame.K_TAB)          # past the cut
        for _ in range(4):
            self._press(slider, pygame.K_UP)
        self._press(slider, pygame.K_TAB)
        self._press(slider, pygame.K_UP)
        # Live in memory the whole time, but not written to disk yet.
        self.assertEqual(tone.values("amp"), {"low": 4, "body": 1,
                                              "presence": 0, "air": 0})
        self.assertFalse(tone.options.set.call_args.kwargs.get("autosave", True))
        options_save = sys.modules["libs.options"].save
        options_save.assert_not_called()
        self._press(slider, pygame.K_RETURN)
        self.assertTrue(slider._closed)
        options_save.assert_called_once()
        self.assertEqual(self.gp.substates, [])

    def test_a_page_closed_without_a_press_writes_nothing(self):
        slider = self._open("amp")
        self._press(slider, pygame.K_ESCAPE)
        self.assertTrue(slider._closed)
        sys.modules["libs.options"].save.assert_not_called()
        tone.options.set.assert_not_called()

    def test_escape_is_the_same_close_as_enter(self):
        slider = self._open("amp")
        self._press(slider, pygame.K_TAB)
        self._press(slider, pygame.K_UP)
        self._press(slider, pygame.K_ESCAPE)
        self.assertTrue(slider._closed)
        self.assertEqual(tone.values("amp")["low"], 1)
        self.assertEqual(self.gp.substates, [])

    def test_off_has_no_page_and_says_why(self):
        self.bot.voice_effect = "off"
        self.handler._open_voice_tone_menu("off")
        self.assertEqual(self.gp.substates, [])
        said = " ".join(self.said).lower()
        self.assertIn("pick a voice effect", said)

    def test_the_effect_page_has_a_tone_line_for_the_effect_that_is_on(self):
        self.bot.voice_effect = "amp"
        self.handler._open_voice_effect_menu()
        menu = FakeMenu.built[-1]
        labels = [label_of(item) for item in menu.items]
        self.assertIn("Tone (Guitar Amp): Flat", labels)
        self.assertEqual(labels[-1], "Back")
        for item in menu.items:
            if label_of(item).startswith("Tone ("):
                item[1]()
                break
        else:
            self.fail("the Voice Effect page has no Tone line")
        slider = self.gp.substates[-1]
        self.assertIsInstance(slider, voice_effect_pages._VoiceToneSlider)
        self.assertEqual(slider.preset, "amp")

    def test_the_tone_line_reads_live(self):
        self.bot.voice_effect = "amp"
        self.handler._open_voice_effect_menu()
        menu = FakeMenu.built[-1]
        line = next(item for item in menu.items
                    if label_of(item).startswith("Tone ("))
        tone.set_band("amp", "low", 3, save=False)
        self.assertEqual(label_of(line), "Tone (Guitar Amp): +3 Low")
        # Picking another pedal moves the name on the page already open.
        self.bot.voice_effect = "choir"
        self.assertEqual(label_of(line), "Tone (Choir): Flat")

    def test_with_no_effect_on_the_line_says_so_and_opens_nothing(self):
        self.bot.voice_effect = "off"
        self.handler._open_voice_effect_menu()
        menu = FakeMenu.built[-1]
        labels = [label_of(item) for item in menu.items]
        self.assertIn("Tone: pick an effect first", labels)
        for item in menu.items:
            if label_of(item) == "Tone: pick an effect first":
                item[1]()
                break
        # The page itself stays on the stack; no slider was opened over it.
        self.assertEqual(self.gp.substates, [menu])
        self.assertIn("pick a voice effect", " ".join(self.said).lower())

    def test_the_cut_walks_its_ladder_and_home_turns_it_off(self):
        slider = self._open("amp")
        self._press(slider, pygame.K_UP)
        self.assertEqual(tone.cut("amp"), 60)
        self._press(slider, pygame.K_PAGEUP)
        self.assertEqual(tone.cut("amp"), 100)
        self._press(slider, pygame.K_HOME)
        self.assertEqual(tone.cut("amp"), 0)

    def test_the_mix_moves_in_fives_and_pages_in_twenty_fives(self):
        slider = self._open("amp")
        slider.current_index = tone.ROW_IDS.index("mix")
        self._press(slider, pygame.K_DOWN)
        self.assertEqual(tone.mix("amp"), 95)
        self._press(slider, pygame.K_PAGEDOWN)
        self.assertEqual(tone.mix("amp"), 70)
        self._press(slider, pygame.K_HOME)
        self.assertEqual(tone.mix("amp"), 100)

    def test_the_level_is_a_fader_and_m_speaks_the_meter(self):
        slider = self._open("amp")
        slider.current_index = tone.ROW_IDS.index("level")
        self._press(slider, pygame.K_UP)
        self.assertEqual(tone.level("amp"), 1)
        self._press(slider, pygame.K_PAGEDOWN)
        self.assertEqual(tone.level("amp"), -5)
        self._press(slider, pygame.K_m)
        self.assertIn("nothing has gone through",
                      " ".join(self.said).lower())
        tone.report("amp", 0.1, 0.2)
        self._press(slider, pygame.K_m)
        self.assertIn("peak", self.said[-1].lower())

    def test_the_compressor_rows_walk_their_ladders_and_speak(self):
        slider = self._open("amp")
        for _ in range(tone.ROW_IDS.index("comp")):
            self._press(slider, pygame.K_TAB)       # Low Cut -> ... -> Comp
        self.assertEqual(slider._current()[0], "comp")
        self._press(slider, pygame.K_UP)
        self.assertEqual(tone.compressor("amp")[0], -36)
        self.assertIn("compressing above minus 36 decibels",
                      " ".join(self.said).lower())
        self._press(slider, pygame.K_PAGEUP)        # two stops, not +6 dB
        self.assertEqual(tone.compressor("amp")[0], -24)
        self._press(slider, pygame.K_HOME)
        self.assertEqual(tone.compressor("amp")[0], 0)
        self._press(slider, pygame.K_TAB)           # Ratio
        self.assertEqual(slider._current()[0], "ratio")
        self._press(slider, pygame.K_UP)
        self.assertEqual(tone.compressor("amp")[1], 6)
        self.assertIn("six to one", " ".join(self.said).lower())
        self._press(slider, pygame.K_TAB)           # Attack
        self._press(slider, pygame.K_PAGEDOWN)
        self.assertEqual(tone.compressor("amp")[2], 1)
        self._press(slider, pygame.K_TAB)           # Release
        self._press(slider, pygame.K_UP)
        self.assertEqual(tone.compressor("amp")[3], 500)
        self.assertIn("500 milliseconds", " ".join(self.said).lower())

    def test_the_compressor_survives_a_restart(self):
        slider = self._open("amp")
        for _ in range(tone.ROW_IDS.index("comp")):
            self._press(slider, pygame.K_TAB)
        self._press(slider, pygame.K_UP)
        self._press(slider, pygame.K_TAB)
        self._press(slider, pygame.K_UP)
        self._press(slider, pygame.K_RETURN)
        self.gp.substates.clear()
        again = self._open("amp")
        self.assertEqual(again.values["comp"], -36)
        self.assertEqual(again.values["ratio"], 6)

    def test_a_board_page_offers_the_tone_of_its_own_preset(self):
        mock.patch.object(
            voice_effects.custom.options, "get",
            side_effect=lambda key, default=None: default).start()
        self.handler._open_custom_effect_menu()
        board = FakeMenu.built[-1]
        labels = [label_of(item) for item in board.items]
        self.assertIn("Tone (Custom Amp): Flat", labels)
        # ... above Back, and above Clear on the one board that has a Clear.
        self.assertEqual(labels[-1], "Back")
        for item in board.items:
            if label_of(item).startswith("Tone ("):
                item[1]()
                break
        slider = self.gp.substates[-1]
        self.assertEqual(slider.preset, voice_effects.CUSTOM_EFFECT)




# ------------------------------------------------------ the mark on the list

class TheTunedMarkTests(ToneCase):
    """A preset that has been reshaped says so where a player picks it.

    The shelf is a hundred and forty-four sounds with four bands each, and a
    player who has tuned six of them cannot remember which six. The mark is
    read live, so it appears the moment a band moves and disappears when `R`
    puts the bands back -- flat is the absence of a saved key, not four zeros.
    """

    def setUp(self):
        super().setUp()
        FakeMenu.built = []
        mock.patch("libs.menu.Menu", FakeMenu).start()
        mock.patch("libs.menus.set_default_sounds").start()
        self.said = []
        mock.patch.object(menus_module, "speak",
                          side_effect=self.said.append).start()
        mock.patch("libs.options.save").start()
        self.bot = make_bot("amp")
        self.handler = menus_module.MusicBotMenus(self.bot)
        self.gp = self.bot._find_gameplay.return_value

    def labels(self):
        return [label_of(item) for item in FakeMenu.built[-1].items]

    def test_only_the_preset_that_was_tuned_says_so(self):
        tone.set_band("amp", "low", 3, save=False)
        self.handler._open_voice_effect_menu("drive")
        labels = self.labels()
        self.assertIn("Guitar Amp (tuned, active)", labels)
        self.assertIn("Fuzz", labels)            # untouched, and not chosen
        self.assertNotIn("Fuzz (tuned)", labels)

    def test_the_mark_follows_the_bands_on_the_page_already_open(self):
        self.handler._open_voice_effect_menu("drive")
        line = next(item for item in FakeMenu.built[-1].items
                    if label_of(item) == "Guitar Amp (active)")
        tone.set_band("amp", "air", -2, save=False)
        self.assertEqual(label_of(line), "Guitar Amp (tuned, active)")
        # `R` on the slider forgets the key, so the line stops saying it.
        tone.reset("amp", save=False)
        self.assertEqual(label_of(line), "Guitar Amp (active)")

    def test_the_family_line_marks_the_pedal_it_names(self):
        tone.set_band("amp", "presence", 4, save=False)
        self.handler._open_voice_effect_menu()
        self.assertIn("Amp & Drive: Guitar Amp (tuned, active)", self.labels())
        # A family whose chosen pedal is untouched names it without the mark.
        self.bot.voice_effect = "plate"
        self.handler._open_voice_effect_menu()
        labels = self.labels()
        self.assertTrue(any(label.endswith("(active)") and "(tuned" not in label
                            for label in labels[1:]), labels)

    def test_a_board_line_carries_its_own_tone(self):
        # Custom Amp is the board at the bottom of the Amp & Drive family.
        tone.set_band(voice_effects.CUSTOM_EFFECT, "body", -5, save=False)
        self.handler._open_voice_effect_menu("drive")
        labels = self.labels()
        self.assertTrue(
            any(label.startswith("Custom Amp:") and label.endswith("(tuned)")
                for label in labels), labels)

    def test_tab_says_what_the_tone_is_doing(self):
        tone.set_band("amp", "low", 3, save=False)
        tone.set_band("amp", "air", -2, save=False)
        self.handler._open_voice_effect_menu("drive")
        item = next(item for item in FakeMenu.built[-1].items
                    if label_of(item).startswith("Guitar Amp"))
        note = item[3]() if callable(item[3]) else item[3]
        self.assertIn("Tone: +3 Low, -2 Air.", note)
        # An untouched preset keeps exactly the description it had.
        other = next(item for item in FakeMenu.built[-1].items
                     if label_of(item) == "Fuzz")
        note = other[3]() if callable(other[3]) else other[3]
        self.assertEqual(note, voice_effects.effect_note("fuzz"))
# ------------------------------------------------------- the strip around it

class TheStripTests(ToneCase):
    """The three controls a mixer puts around its EQ, and the rows they make."""

    def test_the_page_walks_thirteen_rows_in_a_mixer_order(self):
        self.assertEqual(tone.ROW_IDS, ("cut", "low", "body", "presence",
                                        "air", "deess", "highcut", "mix",
                                        "comp", "ratio", "attack", "release",
                                        "level"))
        self.assertEqual([entry[2] for entry in tone.ROWS],
                         ["cut", "db", "db", "db", "db", "deess",
                          "highcut", "mix", "comp", "ratio", "attack",
                          "release", "level"])
        self.assertEqual(tone.default_of("cut"), 0)
        self.assertEqual(tone.default_of("highcut"), 0)
        self.assertEqual(tone.default_of("deess"), 0)
        self.assertEqual(tone.default_of("mix"), 100)
        self.assertEqual(tone.default_of("level"), 0)
        self.assertEqual(tone.default_of("low"), 0)
        # The compressor opens off, at the ratio and times a voice wants when
        # it is turned on.
        self.assertEqual(tone.default_of("comp"), 0)
        self.assertEqual(tone.default_of("ratio"), 4)
        self.assertEqual(tone.default_of("attack"), 10)
        self.assertEqual(tone.default_of("release"), 250)

    def test_the_state_of_an_untouched_preset_is_what_it_opens_with(self):
        self.assertEqual(tone.state("amp"), {
            "cut": 0, "low": 0, "body": 0, "presence": 0, "air": 0,
            "deess": 0, "highcut": 0, "mix": 100, "comp": 0, "ratio": 4,
            "attack": 10, "release": 250, "level": 0})

    def test_every_row_is_saved_under_the_one_preset(self):
        tone.set_cut("amp", 80, save=False)
        tone.set_mix("amp", 60, save=False)
        tone.set_comp("amp", -18, save=False)
        tone.set_ratio("amp", 6, save=False)
        tone.set_level("amp", -3, save=False)
        self.assertEqual(tone.summary("amp"),
                         "Cut 80, Mix 60%, Comp -18 dB 6:1, Level -3 dB")
        self.assertEqual(tone.state("amp")["cut"], 80)
        self.assertEqual(tone.state("amp")["mix"], 60)
        self.assertEqual(tone.state("amp")["comp"], -18)
        self.assertEqual(tone.state("amp")["level"], -3)
        # Another preset is untouched by any of it.
        self.assertEqual(tone.summary("fuzz"), "Flat")

    def test_reset_forgets_the_whole_strip(self):
        tone.set_band("amp", "low", 3, save=False)
        tone.set_cut("amp", 80, save=False)
        tone.set_mix("amp", 60, save=False)
        tone.set_level("amp", -3, save=False)
        tone.reset("amp", save=False)
        self.assertEqual(tone.summary("amp"), "Flat")
        self.assertFalse(tone.has_tone("amp"))

    def test_a_row_at_what_the_preset_opens_with_is_not_a_saved_key(self):
        tone.set_mix("amp", 55, save=False)
        self.assertIn("mix", self.store[tone._OPTION]["amp"])
        tone.set_mix("amp", 100, save=False)
        self.assertNotIn("amp", self.store[tone._OPTION])

    def test_the_words_name_what_each_row_is(self):
        self.assertEqual(tone.cut_spoken(0), "off, the voice's own bottom")
        self.assertEqual(tone.cut_spoken(80), "80 hertz")
        self.assertEqual(tone.mix_spoken(100), "fully wet, the effect alone")
        self.assertEqual(tone.mix_spoken(0), "fully dry, the effect silent")
        self.assertEqual(tone.mix_spoken(60), "60 percent wet")
        self.assertEqual(tone.level_spoken(-3), "minus 3 decibels")


class TheLowCutTests(ToneCase):
    """The cut takes the bottom off before the bands see it."""

    def test_the_ladder_is_off_and_five_corners(self):
        self.assertEqual(tone.CUT_FREQS, (0, 60, 80, 100, 120, 160))
        self.assertEqual(tone.cut("amp"), 0)

    def test_a_value_off_the_ladder_snaps_to_the_nearest_corner(self):
        tone.set_cut("amp", 95)
        self.assertEqual(tone.cut("amp"), 100)
        tone.set_cut("amp", 5)
        self.assertEqual(tone.cut("amp"), 60)      # nearest offered corner
        tone.set_cut("amp", -4)
        self.assertEqual(tone.cut("amp"), 0)
        tone.set_cut("amp", 999)
        self.assertEqual(tone.cut("amp"), 160)

    def test_the_cut_can_be_turned_off_again_while_a_voice_runs(self):
        """Off is a response in its own right, not a missing one.

        `_retune_cut` runs the frame through the old corner and the new one and
        blends them, so turning the cut back to off needs the identity as two
        triples, exactly like any other retune. Splatting the one identity
        tuple there raised `TypeError` on the press instead of easing the
        bottom back -- a live client, 2026-10-29 -- and no test rendered a
        frame *after* the cut was switched off.
        """
        bottom = sinusoid(50.0, 0.2)
        tone.set_cut("direct", 100, save=False)
        processor = dsp._EffectProcessor("direct", with_tone=True)
        cut = play(processor, bottom)
        tone.set_cut("direct", 0, save=False)
        off = play(processor, bottom)          # the press that used to raise
        self.assertGreater(rms(off), rms(cut) * 1.5)
        # ... and back on again, the same way round.
        tone.set_cut("direct", 100, save=False)
        again = play(processor, bottom)
        self.assertLess(rms(again), rms(off))

    def test_the_cut_takes_the_bottom_and_leaves_the_words(self):
        bottom = sinusoid(50.0, 0.2)
        words = sinusoid(1000.0, 0.2)
        before_bottom = play(dsp._EffectProcessor("direct", with_tone=True),
                             bottom)
        before_words = play(dsp._EffectProcessor("direct", with_tone=True),
                            words)
        tone.set_cut("direct", 100, save=False)
        after_bottom = play(dsp._EffectProcessor("direct", with_tone=True),
                            bottom)
        after_words = play(dsp._EffectProcessor("direct", with_tone=True),
                           words)
        self.assertLess(gain_db(after_bottom, before_bottom), -6.0)
        self.assertGreater(gain_db(after_words, before_words), -1.5)


class TheHighCutTests(ToneCase):
    """The other end of the strip: where the top stops."""

    def test_the_ladder_is_off_and_six_corners(self):
        self.assertEqual(tone.HIGH_CUT_FREQS,
                         (0, 16000, 12000, 9000, 7000, 5500, 4000))
        self.assertEqual(tone.high_cut("amp"), 0)

    def test_a_value_off_the_ladder_snaps_to_the_nearest_corner(self):
        tone.set_high_cut("amp", 6000)
        self.assertEqual(tone.high_cut("amp"), 5500)
        tone.set_high_cut("amp", 100)
        self.assertEqual(tone.high_cut("amp"), 4000)
        tone.set_high_cut("amp", 99999)
        self.assertEqual(tone.high_cut("amp"), 16000)
        tone.set_high_cut("amp", -5)
        self.assertEqual(tone.high_cut("amp"), 0)

    def test_the_high_cut_takes_the_top_and_leaves_the_words(self):
        top = sinusoid(12000.0, 0.2)
        words = sinusoid(1000.0, 0.2)
        before_top = play(dsp._EffectProcessor("direct", with_tone=True), top)
        before_words = play(dsp._EffectProcessor("direct", with_tone=True),
                            words)
        tone.set_high_cut("direct", 4000, save=False)
        after_top = play(dsp._EffectProcessor("direct", with_tone=True), top)
        after_words = play(dsp._EffectProcessor("direct", with_tone=True),
                           words)
        self.assertLess(gain_db(after_top, before_top), -15.0)
        self.assertGreater(gain_db(after_words, before_words), -2.0)

    def test_the_high_cut_can_be_turned_off_again_while_a_voice_runs(self):
        """Off is a response like any other, on this edge as on the low one."""
        top = sinusoid(9000.0, 0.2)
        tone.set_high_cut("direct", 4000, save=False)
        processor = dsp._EffectProcessor("direct", with_tone=True)
        cut = play(processor, top)
        tone.set_high_cut("direct", 0, save=False)
        off = play(processor, top)            # the press that used to raise
        self.assertGreater(rms(off), rms(cut) * 2.0)
        tone.set_high_cut("direct", 7000, save=False)
        again = play(processor, top)
        self.assertLess(rms(again), rms(off))


class TheDeEssTests(ToneCase):
    """The one block on the strip that moves with the words, not before them."""

    def test_the_ladder_is_off_and_four_amounts(self):
        self.assertEqual(tone.DEESS_AMOUNTS, (0, 3, 6, 9, 12))
        self.assertEqual(tone.deess("amp"), 0)

    def test_a_sibilant_is_taken_down_and_the_words_are_left_alone(self):
        sibilant = sinusoid(8000.0, 0.3)
        words = sinusoid(200.0, 0.3)
        before_s = play(dsp._EffectProcessor("direct", with_tone=True),
                        sibilant)
        before_words = play(dsp._EffectProcessor("direct", with_tone=True),
                            words)
        tone.set_deess("direct", 12, save=False)
        after_s = play(dsp._EffectProcessor("direct", with_tone=True),
                       sibilant)
        after_words = play(dsp._EffectProcessor("direct", with_tone=True),
                           words)
        self.assertLess(gain_db(after_s, before_s), -8.0)
        self.assertAlmostEqual(gain_db(after_words, before_words), 0.0,
                              delta=0.2)

    def test_a_quiet_s_is_left_alone(self):
        quiet = sinusoid(8000.0, 0.004)
        before = play(dsp._EffectProcessor("direct", with_tone=True), quiet)
        tone.set_deess("direct", 12, save=False)
        after = play(dsp._EffectProcessor("direct", with_tone=True), quiet)
        self.assertAlmostEqual(gain_db(after, before), 0.0, delta=0.2)

    def test_the_amount_is_a_ceiling_and_not_a_ratio(self):
        loud = sinusoid(8000.0, 0.3)
        flat = play(dsp._EffectProcessor("direct", with_tone=True), loud)
        levels = {}
        for amount in (6, 12):
            tone.set_deess("direct", amount, save=False)
            levels[amount] = gain_db(
                play(dsp._EffectProcessor("direct", with_tone=True), loud),
                flat)
        self.assertLess(levels[6], -4.0)
        self.assertGreater(levels[6], -7.5)
        self.assertLess(levels[12], levels[6] - 3.0)

    def test_turning_the_row_off_is_a_ramp_not_a_step(self):
        """A `s` still sounding while the row goes off must not click.

        The gain is read out of the block itself (`out / block`), because the
        payload and the trigger are the same tone: an 8 kHz tone that is over
        the detector's threshold the whole time. A hard switch to off would
        jump from a quarter to one between two samples.
        """
        t = np.arange(FRAME // 2) / SR
        block = (0.3 * np.sin(2.0 * np.pi * 8000.0 * t)).astype(np.float32)
        deesser = dsp._DeEsser()
        for _ in range(4):
            deesser.process(block, 12)
        out = deesser.process(block, 0)          # the press that turns it off
        usable = np.abs(block) > 1e-4
        gains = (out.astype(np.float64)[usable]
                 / block.astype(np.float64)[usable])
        # The press lands on the frame it happens on, from the gain that was
        # already there -- then it opens across the 20 ms rather than at once.
        self.assertLess(float(gains[0]), 0.35)
        self.assertGreater(float(gains[-1]), 0.99)
        self.assertLess(float(np.abs(np.diff(gains)).max()), 0.02)

    def test_the_words_name_the_two_new_rows(self):
        self.assertEqual(tone.high_cut_spoken(0), "off, the voice's own top")
        self.assertEqual(tone.high_cut_spoken(7000),
                         "cutting above 7 kilohertz")
        self.assertEqual(tone.deess_spoken(0), "off, no de-essing")
        self.assertEqual(tone.deess_spoken(9),
                         "up to 9 decibels off the s")
        tone.set_deess("amp", 6, save=False)
        tone.set_high_cut("amp", 7000, save=False)
        self.assertEqual(tone.summary("amp"), "De-ess 6 dB, High Cut 7000")
        self.assertTrue(tone.has_tone("amp"))
        self.assertIn("de-essing up to 6 decibels off the s",
                      tone.description("amp"))
        self.assertIn("a high cut above 7 kilohertz",
                      tone.description("amp"))


class TheMixTests(ToneCase):
    """How much of the pedal is heard, beside the voice that carries it."""

    def test_the_mix_is_fully_wet_unless_it_is_moved(self):
        self.assertEqual(tone.mix("fuzz"), 100)

    def test_dry_is_the_voice_and_a_half_is_halfway_between(self):
        processor = dsp._EffectProcessor("fuzz", with_tone=True)
        dry = np.full(FRAME // 2, 0.1, dtype=np.float32)
        wet = np.full(FRAME // 2, 0.5, dtype=np.float32)
        tone.set_mix("fuzz", 0, save=False)
        out = dry
        for _ in range(20):
            out = processor._blend(dry, wet)
        self.assertEqual(out.tolist(), dry.tolist())
        tone.set_mix("fuzz", 50, save=False)
        processor = dsp._EffectProcessor("fuzz", with_tone=True)
        for _ in range(20):
            out = processor._blend(dry, wet)
        self.assertAlmostEqual(float(out[0]), 0.3, places=4)

    def test_a_lower_mix_is_quieter_each_step_of_the_way(self):
        signal = sinusoid(300.0, 0.2)
        levels = []
        for percent in (100, 60, 20, 0):
            tone.set_mix("fuzz", percent, save=False)
            out = play(dsp._EffectProcessor("fuzz", with_tone=True), signal)
            levels.append(rms(out))
        self.assertEqual(levels, sorted(levels, reverse=True))
        self.assertLess(levels[-1], levels[0])

    def test_a_mix_fades_rather_than_steps(self):
        """A 40 percent move between two frames is a ramp, not a corner."""
        processor = dsp._EffectProcessor("fuzz", with_tone=True)
        dry = np.zeros(FRAME // 2, dtype=np.float32)
        wet = np.ones(FRAME // 2, dtype=np.float32)
        tone.set_mix("fuzz", 0, save=False)
        first = processor._blend(dry, wet)
        # Fully wet to start, one frame of fade: the first sample is still
        # near the wet side and the last is already nearer the dry one.
        self.assertGreater(float(first[0]), float(first[-1]))
        self.assertGreater(float(first[0]), 0.7)


class TheLevelTests(ToneCase):
    """The fader that matches one preset to the next."""

    def test_the_level_is_zero_unless_it_is_moved(self):
        self.assertEqual(tone.level("direct"), 0)

    def test_the_level_lifts_and_lowers_the_whole_voice(self):
        signal = sinusoid(400.0, 0.1)
        flat = play(dsp._EffectProcessor("direct", with_tone=True), signal)
        tone.set_level("direct", 6, save=False)
        up = play(dsp._EffectProcessor("direct", with_tone=True), signal)
        tone.set_level("direct", -6, save=False)
        down = play(dsp._EffectProcessor("direct", with_tone=True), signal)
        self.assertAlmostEqual(gain_db(up, flat), 6.0, delta=0.4)
        self.assertAlmostEqual(gain_db(down, flat), -6.0, delta=0.4)

    def test_a_level_makes_the_preset_count_as_moved(self):
        tone.set_level("amp", -4, save=False)
        self.assertTrue(tone.has_tone("amp"))
        self.assertEqual(tone.summary("amp"), "Level -4 dB")


class TheMeterTests(ToneCase):
    """The number a fader is set against, and the honesty of it going quiet."""

    def test_the_meter_reads_the_frame_that_left(self):
        play(dsp._EffectProcessor("direct", with_tone=True),
             sinusoid(400.0, 0.2))
        reading = tone.reading("direct")
        self.assertIsNotNone(reading)
        rms_db, peak_db = reading
        self.assertGreater(peak_db, rms_db)
        self.assertLess(rms_db, 0.0)

    def test_a_reading_goes_stale_when_the_press_stops(self):
        tone.report("direct", 0.1, 0.2, now=10.0)
        self.assertIsNotNone(tone.reading("direct", now=11.0))
        self.assertIsNone(tone.reading("direct", now=20.0))

    def test_the_words_are_a_level_and_a_peak(self):
        tone.report("direct", 0.1, 0.5, now=10.0)
        said = tone.meter_spoken("direct", now=10.5).lower()
        self.assertIn("level minus 20", said)
        self.assertIn("peak minus 6", said)
        self.assertIn("nothing has gone through",
                      tone.meter_spoken("direct", now=30.0).lower())


# ------------------------------------------------------------- the compressor

class TheCompressorTests(ToneCase):
    """The level rider: a quiet sentence and a loud one arrive together.

    The four things that make it a compressor rather than a volume knob are
    measured here, not read off the table: the curve bends instead of switching
    (a soft knee), the detector listens over the rumble (a high-pass at 120 Hz),
    the attack and release are the times they say, and the fader after it is the
    make-up. The one thing it must never do is change a preset that has not been
    compressed -- that is checked byte for byte.
    """

    def _through(self, signal):
        return play(dsp._EffectProcessor("direct", with_tone=True), signal)

    def _reduction(self, signal, threshold=-24, ratio=8, attack=3, release=250):
        """The deepest pull of one signal through the compressor alone."""
        comp = dsp._Compressor()
        pcm = to_pcm(signal)
        out = []
        for start in range(0, len(pcm) - FRAME + 1, FRAME):
            out.append(comp.process(from_pcm(pcm[start:start + FRAME]).astype(
                np.float32), threshold, ratio, attack, release))
        return comp.reduction(), np.concatenate(out)

    def test_the_ladders_are_the_choices_the_page_offers(self):
        self.assertEqual(tone.COMP_THRESHOLDS, (0, -36, -30, -24, -18, -12, -6))
        self.assertEqual(tone.COMP_RATIOS, (2, 3, 4, 6, 8, 12, 20))
        self.assertEqual(tone.COMP_ATTACKS, (1, 3, 10, 30, 60))
        self.assertEqual(tone.COMP_RELEASES, (60, 120, 250, 500, 1000))
        # Off is the first stop of the threshold ladder, so the rest reads
        # quiet-to-loud rather than small-to-large.
        self.assertEqual(tone.COMP_THRESHOLDS[0], 0)
        for ladder in (tone.COMP_THRESHOLDS[1:], tone.COMP_RATIOS,
                       tone.COMP_ATTACKS, tone.COMP_RELEASES):
            self.assertEqual(list(ladder), sorted(ladder))
        self.assertEqual(tone.LADDERS["comp"], tone.COMP_THRESHOLDS)

    def test_a_threshold_off_the_ladder_snaps_to_a_stop(self):
        tone.set_comp("amp", -20)
        self.assertEqual(tone.compressor("amp")[0], -18)
        # Zero and anything above it is off -- not a threshold a frame can cross.
        tone.set_comp("amp", 6)
        self.assertEqual(tone.compressor("amp")[0], 0)
        tone.set_ratio("amp", 100)
        self.assertEqual(tone.compressor("amp")[1], 20)
        tone.set_attack("amp", 0)
        self.assertEqual(tone.compressor("amp")[2], 1)
        tone.set_release("amp", 400)
        self.assertEqual(tone.compressor("amp")[3], 500)

    def test_a_quiet_voice_is_left_alone_and_a_loud_one_is_pulled_down(self):
        quiet = sinusoid(1000.0, 0.05)
        loud = sinusoid(1000.0, 0.3)
        quiet_flat = self._through(quiet)
        loud_flat = self._through(loud)
        tone.set_comp("direct", -18, save=False)
        tone.set_ratio("direct", 4, save=False)
        self.assertAlmostEqual(gain_db(self._through(quiet), quiet_flat), 0.0,
                               delta=0.05)
        self.assertLess(gain_db(self._through(loud), loud_flat), -2.0)

    def test_the_curve_is_bent_rather_than_switched(self):
        """A soft knee: nothing below the knee, a lean at it, no step at it."""
        pulled = []
        for amplitude in (0.01, 0.05, 0.1, 0.2, 0.4):
            tone.set_comp("direct", -18, save=False)
            tone.set_ratio("direct", 4, save=False)
            processor = dsp._EffectProcessor("direct", with_tone=True)
            play(processor, sinusoid(1000.0, amplitude, 0.3))
            pulled.append(processor._tone.compression())
        self.assertLess(pulled[0], 0.05)
        self.assertLess(pulled[2], 0.1)          # 0.1 is 2 dB under the knee
        self.assertEqual(pulled, sorted(pulled))
        self.assertGreater(pulled[-1], 3.0)
        # Doubling the level is 6 dB; a hard switch would jump the whole 6 dB
        # at the threshold. The bent curve takes less than that at every step.
        for before, after in zip(pulled, pulled[1:]):
            self.assertLess(after - before, 6.0)

    def test_the_detector_listens_over_the_rumble(self):
        """The bottom of the room does not duck the words (a 120 Hz detector)."""
        low = self._reduction(sinusoid(45.0, 0.5, 0.4))[0]
        words = self._reduction(sinusoid(1000.0, 0.5, 0.4))[0]
        self.assertGreater(words, 8.0)
        self.assertLess(low, words / 4.0)
        # ... and a thump under a sentence changes what the sentence loses by
        # less than a decibel. A full-band detector would duck it with the thump.
        plain = sinusoid(1000.0, 0.2, 0.4)
        thumped = (plain + sinusoid(40.0, 0.8, 0.4)).astype(np.float32)
        flat_thumped = self._through(thumped)
        flat_words = self._through(plain)
        tone.set_comp("direct", -24, save=False)
        tone.set_ratio("direct", 8, save=False)
        self.assertAlmostEqual(gain_db(self._through(thumped), flat_thumped),
                               gain_db(self._through(plain), flat_words),
                               delta=1.0)

    def test_a_fast_attack_catches_more_of_a_burst_than_a_slow_one(self):
        burst = np.concatenate([np.zeros(4800, dtype=np.float32),
                                sinusoid(1000.0, 0.5, 0.2)])
        heads = {}
        for attack in (1, 60):
            _pull, out = self._reduction(burst, attack=attack)
            heads[attack] = rms(out[4800:7200])
        self.assertLess(heads[1], heads[60] * 0.6)

    def test_the_release_lets_go_at_the_time_it_says(self):
        signal = np.concatenate([sinusoid(1000.0, 0.5, 0.2),
                                 sinusoid(1000.0, 0.05, 0.3)])
        levels = {}
        for release in (60, 1000):
            _pull, out = self._reduction(signal, release=release)
            levels[release] = rms(out[9600 + 4800:9600 + 6720])
        # 100 ms after the loud part stopped, the fast release is nearly back
        # and the slow one is still holding the quiet tail down.
        self.assertGreater(levels[60], levels[1000] * 2.0)

    def test_re_engaging_after_a_break_starts_clean(self):
        """The detector does not carry the press that has already ended."""
        tone.set_comp("direct", -30, save=False)
        tone.set_ratio("direct", 12, save=False)
        tone.set_release("direct", 1000, save=False)
        used = dsp._EffectProcessor("direct", with_tone=True)
        loud = to_pcm(sinusoid(1000.0, 0.5, 0.2))
        for start in range(0, len(loud) - FRAME + 1, FRAME):
            used.process(loud[start:start + FRAME])
        self.assertGreater(used._tone.compression(), 4.0)
        tone.set_comp("direct", 0, save=False)
        for _ in range(10):                      # a second of silence, switched off
            used.process(bytes(FRAME))
        tone.set_comp("direct", -30, save=False)
        quiet = sinusoid(1000.0, 0.05, 0.2)
        self.assertAlmostEqual(
            gain_db(play(used, quiet),
                    play(dsp._EffectProcessor("direct", with_tone=True), quiet)),
            0.0, delta=1.0)

    def test_the_level_row_is_the_makeup(self):
        signal = sinusoid(1000.0, 0.3)
        flat = self._through(signal)
        tone.set_comp("direct", -24, save=False)
        tone.set_ratio("direct", 4, save=False)
        missing = -gain_db(self._through(signal), flat)
        self.assertGreater(missing, 2.0)
        tone.set_level("direct", int(round(missing)), save=False)
        self.assertAlmostEqual(gain_db(self._through(signal), flat), 0.0,
                               delta=1.0)

    def test_a_compressor_switched_off_is_the_pedal_it_was(self):
        tone.set_ratio("direct", 20)
        tone.set_attack("direct", 1)
        tone.set_release("direct", 1000)
        signal = noise()
        plain = _all_bytes(dsp._EffectProcessor("direct"), signal)
        tuned = _all_bytes(dsp._EffectProcessor("direct", with_tone=True),
                           signal)
        self.assertEqual(plain, tuned)

    def test_switching_it_off_is_ramped_rather_than_cut(self):
        """A hard let-go would be a step; the frame it moves on is a slope."""
        signal = sinusoid(1000.0, 0.3, 0.6)
        # What one sample-to-sample step of this tone is with nothing done to
        # it: the seam must not be larger than the waveform itself.
        steady = max(abs(float(value)) for value in np.diff(
            frames_of(dsp._EffectProcessor("direct", with_tone=True),
                      signal)[-1]))
        tone.set_comp("direct", -30, save=False)
        tone.set_ratio("direct", 12, save=False)
        tone.set_attack("direct", 1, save=False)
        processor = dsp._EffectProcessor("direct", with_tone=True)
        pcm = to_pcm(signal)
        out = []
        for index in range(len(pcm) // FRAME):
            if index == 8:
                tone.set_comp("direct", 0, save=False)
            out.append(from_pcm(processor.process(
                pcm[index * FRAME:(index + 1) * FRAME])))
        seam = max(abs(float(out[index + 1][0] - out[index][-1]))
                   for index in (7, 8, 9))
        self.assertLessEqual(seam, steady * 1.1)

    def test_the_meter_reports_how_far_it_pulled(self):
        tone.report("direct", 0.1, 0.5, reduction=3.4, now=10.0)
        self.assertAlmostEqual(tone.reduction("direct", now=10.5), 3.4)
        said = tone.meter_spoken("direct", now=10.5).lower()
        self.assertIn("the compressor pulled 3 decibels", said)
        # A stale reading is not a reading, for the reduction as for the level.
        self.assertEqual(tone.reduction("direct", now=30.0), 0.0)
        # And half a decibel is not worth saying.
        tone.report("direct", 0.1, 0.5, reduction=0.4, now=10.0)
        self.assertNotIn("pulled", tone.meter_spoken("direct", now=10.5))

    def test_only_a_threshold_counts_as_a_moved_strip(self):
        tone.set_ratio("amp", 12, save=False)
        tone.set_attack("amp", 1, save=False)
        self.assertFalse(tone.has_tone("amp"))
        tone.set_comp("amp", -18, save=False)
        self.assertTrue(tone.has_tone("amp"))

    def test_the_words_name_each_compressor_row(self):
        self.assertEqual(tone.comp_spoken(0), "off, no compression")
        self.assertEqual(tone.comp_spoken(-18),
                         "compressing above minus 18 decibels")
        self.assertEqual(tone.ratio_spoken(4), "four to one")
        self.assertEqual(tone.ratio_spoken(12), "twelve to one")
        self.assertEqual(tone.attack_spoken(3), "3 milliseconds")
        self.assertEqual(tone.release_spoken(1000), "1000 milliseconds")
        tone.set_comp("amp", -18, save=False)
        self.assertIn("compression above minus 18 decibels at 4 to one",
                      tone.description("amp"))
        # The times are only worth saying when they are not what a voice wants
        # anyway, which is why an untouched pair stays out of the sentence.
        self.assertNotIn("attack", tone.description("amp"))
        tone.set_attack("amp", 3, save=False)
        self.assertIn("3 milliseconds of attack", tone.description("amp"))
        tone.set_release("amp", 500, save=False)
        self.assertIn("500 milliseconds of release", tone.description("amp"))


# --------------------------------------------------------------- the whole strip

class TheChannelStripSweepTests(ToneCase):
    """Every row, every value, with a frame still running.

    The cut's off branch raised `TypeError` on a live key press (2026-10-29)
    and every test of the strip had rendered *before* the row changed, never
    after it. This walks all thirteen rows at every value they offer, out of
    what the preset opens with and back to it, and then the page's own keys
    over the same ground: a row that cannot be reached, cannot be left, or
    cannot be put back is a failure here rather than a crash in somebody's
    ears.
    """

    #: Every value each row offers. The bands and the level are walked whole;
    #: the ladders are already finite.
    VALUES = {
        "cut": tone.CUT_FREQS,
        "low": tuple(range(-12, 13)),
        "body": tuple(range(-12, 13)),
        "presence": tuple(range(-12, 13)),
        "air": tuple(range(-12, 13)),
        "mix": tuple(range(0, 101, 5)),
        "deess": tone.DEESS_AMOUNTS,
        "highcut": tone.HIGH_CUT_FREQS,
        "comp": tone.COMP_THRESHOLDS,
        "ratio": tone.COMP_RATIOS,
        "attack": tone.COMP_ATTACKS,
        "release": tone.COMP_RELEASES,
        "level": tuple(range(-12, 13)),
    }

    @staticmethod
    def _frames():
        """A quiet part and a loud part, so every block is asked to move."""
        signal = np.concatenate([sinusoid(220.0, 0.01, 0.12),
                                 sinusoid(220.0, 0.4, 0.12)])
        pcm = to_pcm(signal)
        return [pcm[start:start + FRAME]
                for start in range(0, len(pcm) - FRAME + 1, FRAME)]

    @staticmethod
    def _render(processor, frames, count=1):
        for index in range(count):
            processor.process(frames[index % len(frames)])

    def test_every_row_at_every_value_while_frames_run(self):
        frames = self._frames()
        for preset in ("direct", "fuzz", dsp.CUSTOM_EFFECT):
            for row_id, values in self.VALUES.items():
                with self.subTest(preset=preset, row=row_id):
                    processor = dsp._EffectProcessor(preset, with_tone=True)
                    self._render(processor, frames, 3)
                    for value in values:
                        self.assertIsNotNone(
                            tone.set_row(preset, row_id, value, save=False))
                        self._render(processor, frames)
                    # ... and back, one value at a time and then in one press.
                    for value in reversed(values):
                        tone.set_row(preset, row_id, value, save=False)
                        self._render(processor, frames)
                    tone.set_row(preset, row_id, tone.default_of(row_id),
                                 save=False)
                    self._render(processor, frames, 2)
                    tone.reset(preset, save=False)
                    self._render(processor, frames, 2)

    def test_every_row_reset_while_frames_run(self):
        frames = self._frames()
        for preset in ("direct", "radio", dsp.CUSTOM_EFFECT):
            with self.subTest(preset=preset):
                processor = dsp._EffectProcessor(preset, with_tone=True)
                for row_id, values in self.VALUES.items():
                    tone.set_row(preset, row_id, values[-1], save=False)
                self._render(processor, frames, 2)
                tone.reset(preset, save=False)
                self._render(processor, frames, 2)

    def test_the_page_can_walk_and_leave_every_row(self):
        FakeMenu.built = []
        mock.patch("libs.menu.Menu", FakeMenu).start()
        mock.patch("libs.menus.set_default_sounds").start()
        mock.patch.object(menus_module, "speak", side_effect=lambda text: None).start()
        mock.patch("libs.options.save").start()
        bot = make_bot()
        handler = menus_module.MusicBotMenus(bot)
        gp = bot._find_gameplay.return_value
        handler._open_voice_tone_menu("amp")
        slider = gp.substates[-1]
        slider.enter()

        def press(key, mod=0):
            slider.update([pygame.event.Event(pygame.KEYDOWN, key=key,
                                              mod=mod)])

        for index, row_id in enumerate(tone.ROW_IDS):
            with self.subTest(row=row_id):
                # The walk itself is pinned above; this sweep starts on the
                # row directly, so it cannot depend on which way Tab faces.
                slider.current_index = index
                # Every arrow, past both ends of every row, then Home.
                for key in (pygame.K_UP, pygame.K_PAGEUP,
                            pygame.K_PAGEDOWN, pygame.K_DOWN):
                    for _ in range(len(self.VALUES[row_id]) + 4):
                        press(key)
                press(pygame.K_HOME)
                self.assertEqual(slider.values[row_id],
                                 tone.default_of(row_id))
                press(pygame.K_m)
                self.assertFalse(slider._closed)
        press(pygame.K_r)
        press(pygame.K_RETURN)
        self.assertTrue(slider._closed)
        self.assertEqual(gp.substates, [])


if __name__ == "__main__":
    unittest.main()
