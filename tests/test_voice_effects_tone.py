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

    def test_the_page_opens_on_the_saved_bands(self):
        slider = self._open("amp", {"amp": {"low": 3}})
        self.assertEqual(slider.preset, "amp")
        self.assertEqual(slider.name, "Guitar Amp")
        self.assertEqual(slider.values["low"], 3)
        self.assertEqual(slider.values["air"], 0)
        self.assertEqual(slider.current_index, 0)

    def test_a_band_survives_a_restart(self):
        """The setting is in the options file, so a new page reads it back."""
        slider = self._open("amp")
        self._press(slider, pygame.K_UP)
        self._press(slider, pygame.K_TAB)
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
        self.assertEqual(tone.values("amp")["body"], 1)
        self.assertEqual(slider.values["body"], 1)
        self._press(slider, pygame.K_PAGEUP)
        self.assertEqual(tone.values("amp")["body"], 7)
        self._press(slider, pygame.K_DOWN)
        self.assertEqual(tone.values("amp")["body"], 6)
        self._press(slider, pygame.K_HOME)
        self.assertEqual(tone.values("amp")["body"], 0)
        # Shift+Tab walks backwards, wrapped.
        self._press(slider, pygame.K_TAB, mod=pygame.KMOD_SHIFT)
        self.assertEqual(slider.current_index, 0)
        self._press(slider, pygame.K_TAB, mod=pygame.KMOD_SHIFT)
        self.assertEqual(slider.current_index, 3)
        said = " ".join(self.said).lower()
        self.assertIn("body", said)
        self.assertIn("flat", said)

    def test_the_band_is_clamped_and_the_page_keeps_working(self):
        slider = self._open("amp")
        for _ in range(20):
            self._press(slider, pygame.K_PAGEUP)
        self.assertEqual(tone.values("amp")["low"], 12)
        for _ in range(20):
            self._press(slider, pygame.K_PAGEDOWN)
        self.assertEqual(tone.values("amp")["low"], -12)
        self.assertFalse(slider._closed)

    def test_r_puts_all_four_back_flat(self):
        slider = self._open("fuzz", {
            "fuzz": {"low": 4, "body": -3, "presence": 2, "air": 6}})
        self._press(slider, pygame.K_r)
        self.assertEqual(tone.summary("fuzz"), "Flat")
        self.assertEqual(slider.values,
                         {"low": 0, "body": 0, "presence": 0, "air": 0})
        self.assertIn("every band flat", " ".join(self.said).lower())

    def test_a_visit_saves_once_on_close_and_not_on_every_press(self):
        slider = self._open("amp")
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


if __name__ == "__main__":
    unittest.main()


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
