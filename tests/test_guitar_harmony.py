"""The line-in guitar's own Harmony: second voices on what leaves the machine.

The guitar does not use Auto-Tune here -- a string is tuned before it is played
-- so these tests are about the copies alone: that they are the Music Bot's two
intervals at their plain size, that they are mixed into the SAME 20 ms frame
(one stream still leaves), that the lead is held back so the chord is a chord,
and that the player's own ear and the note tracker never see any of it.

Latency is part of the contract, not an accident: 1280 samples (26.7 ms) for a
copy above the string, and the line says so (`latency_samples`), because that
number is why the guitar gets `GuitarTransposer` instead of the microphone's
107 ms line.
"""
import collections
import unittest
from types import SimpleNamespace
from unittest import mock

import numpy as np

from libs import instrument_harmony, instrument_input, options, voice_chat
from libs.music_bot import harmony

SR = 48000
FRAME = 960
FRAME_SAMPLES = instrument_input.InstrumentInput.FRAME_SAMPLES


def tone(seconds, freq=220.0, amplitude=0.3):
    t = np.arange(int(SR * seconds)) / SR
    return (amplitude * np.sin(2.0 * np.pi * freq * t)).astype(np.float32)


def to_pcm(samples):
    return (np.clip(samples, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()


def from_pcm(data):
    return np.frombuffer(bytes(data), dtype="<i2").astype(np.float32) / 32768.0


def values(**kwargs):
    """The options a line reads, with the interval pair it starts from."""
    table = {
        instrument_harmony.SWITCH_OPTION: True,
        "music_bot_autotune_harmony": "third",
        "music_bot_autotune_harmony2": "off",
    }
    table.update(kwargs)
    return lambda key, default=None: table.get(key, default)


def run(signal, lookup=None):
    """Feed one tone through the real line, frame by frame, and collect it."""
    line = instrument_harmony.GuitarHarmony()
    out = bytearray()
    data = to_pcm(signal)
    with mock.patch.object(options, "get", lookup or values()):
        for start in range(0, len(data), FRAME * 2):
            out += line.shape(data[start:start + FRAME * 2])
    line.raw = bytes(out)
    return line, from_pcm(line.raw)


def band_level(block, hz, sr=SR):
    """The amplitude of a steady partial in a block of audio."""
    window = np.hanning(len(block))
    spectrum = np.abs(np.fft.rfft(block * window)) / (len(block) / 4.0)
    freqs = np.fft.rfftfreq(len(block), 1.0 / sr)
    index = int(np.argmin(np.abs(freqs - hz)))
    return float(spectrum[max(0, index - 4):index + 5].max())


class TheSwitchTests(unittest.TestCase):
    """Off is Off: the same bytes, and no copies built."""

    def test_off_leaves_the_frame_exactly_as_it_arrived(self):
        signal = tone(0.5)
        line, audio = run(signal, values(**{instrument_harmony.SWITCH_OPTION: False}))
        self.assertEqual(audio.size, signal.size)
        self.assertEqual(line.raw, to_pcm(signal))
        self.assertEqual(line._voices, [])
        self.assertEqual(line.latency_samples(), 0)

    def test_the_options_line_names_the_intervals_it_will_sing(self):
        with mock.patch.object(options, "get",
                               values(**{"music_bot_autotune_harmony": "third",
                                         "music_bot_autotune_harmony2":
                                             "bass_octave"})):
            self.assertIn("on", instrument_harmony.switch_label())
            self.assertIn("Third", instrument_harmony.switch_label())
            self.assertIn("Bass Octave", instrument_harmony.switch_label())
        with mock.patch.object(options, "get",
                               values(**{instrument_harmony.SWITCH_OPTION: False})):
            self.assertTrue(instrument_harmony.switch_label().endswith("off"))

    def test_on_with_both_slots_off_says_so_instead_of_silently_doing_nothing(self):
        lookup = values(**{"music_bot_autotune_harmony": "off"})
        with mock.patch.object(options, "get", lookup):
            self.assertIn("Off", instrument_harmony.switch_label())
            line, audio = run(tone(0.2), lookup)
            self.assertEqual(line._voices, [])
            self.assertEqual(audio.size, tone(0.2).size)


class TheCopiesTests(unittest.TestCase):
    """The copies are the Music Bot's intervals, at their plain size."""

    def test_a_third_and_a_fifth_make_three_equal_voices(self):
        line, audio = run(tone(2.0), values(
            **{"music_bot_autotune_harmony": "third",
               "music_bot_autotune_harmony2": "fifth"}))
        self.assertEqual(sorted(v.interval for v in line._voices),
                         ["fifth", "third"])
        block = audio[SR:SR + 28800]
        lead = band_level(block, 220.0)
        self.assertGreater(lead, 0.04)
        self.assertAlmostEqual(band_level(block, 277.18), lead, delta=0.02)
        self.assertAlmostEqual(band_level(block, 329.63), lead, delta=0.02)

    def test_every_interval_lands_on_its_own_note(self):
        for interval, hz in (("third", 277.18), ("fifth", 329.63),
                             ("sixth", 369.99), ("seventh", 415.30),
                             ("octave", 440.0)):
            with self.subTest(interval=interval):
                _line, audio = run(tone(2.0), values(
                    **{"music_bot_autotune_harmony": interval}))
                self.assertGreater(band_level(audio[SR:SR + 28800], hz), 0.04,
                                   f"the {interval} is not at {hz} Hz")

    def test_a_bass_copy_is_an_undertone_and_takes_half_a_share(self):
        line, audio = run(tone(2.0), values(
            **{"music_bot_autotune_harmony": "bass_octave"}))
        block = audio[SR:SR + 28800]
        lead = band_level(block, 220.0)
        bass = band_level(block, 110.0)
        self.assertGreater(bass, 0.04)
        self.assertAlmostEqual(lead, 0.3 / (1.0 + harmony.BASS_SHARE),
                               delta=0.03)
        self.assertAlmostEqual(bass / lead, harmony.BASS_SHARE, delta=0.15)

    def test_the_same_interval_in_both_slots_is_one_copy(self):
        lookup = values(**{"music_bot_autotune_harmony": "third",
                           "music_bot_autotune_harmony2": "third"})
        line, _audio = run(tone(0.4), lookup)
        self.assertEqual([v.interval for v in line._voices], ["third"])
        with mock.patch.object(options, "get", lookup):
            self.assertEqual(instrument_harmony.harmony_ids(), ("third",))

    def test_the_intervals_are_read_per_frame(self):
        """Changing the Music Bot's Harmony under a chord joins the new voice."""
        line = instrument_harmony.GuitarHarmony()
        first = values(**{"music_bot_autotune_harmony": "third"})
        later = values(**{"music_bot_autotune_harmony": "sixth"})
        with mock.patch.object(options, "get", first):
            for _ in range(5):
                line.shape(to_pcm(tone(0.02)))
        self.assertEqual([v.interval for v in line._voices], ["third"])
        with mock.patch.object(options, "get", later):
            for _ in range(1):
                line.shape(to_pcm(tone(0.02)))
            intervals = [v.interval for v in line._voices if v.wanted]
        self.assertEqual(intervals, ["sixth"])
        # ...and the copy it replaced is faded, not cut.
        self.assertIn("third", [v.interval for v in line._voices])


class TheTimelineTests(unittest.TestCase):
    """The lead is held back to the copy, and the number is the 26.7 ms."""

    def test_a_copy_above_the_string_costs_twenty_six_milliseconds(self):
        for interval in ("third", "fifth", "sixth", "seventh", "octave"):
            with self.subTest(interval=interval):
                line, _audio = run(tone(0.4), values(
                    **{"music_bot_autotune_harmony": interval}))
                self.assertEqual(line.latency_samples(),
                                 instrument_harmony.GuitarHarmony.LOOKAHEAD)
                self.assertAlmostEqual(line.latency_samples() / 48.0, 26.67,
                                       places=2)

    def test_the_slowest_copy_sets_the_lead_delay(self):
        line, _audio = run(tone(0.4), values(
            **{"music_bot_autotune_harmony": "third",
               "music_bot_autotune_harmony2": "bass_octave"}))
        self.assertEqual(line.latency_samples(), 2560)      # 53.3 ms, not 107
        for voice in line._voices:
            held = (voice.transposer.delay_samples() + round(voice._align))
            self.assertAlmostEqual(held, line.latency_samples(), delta=2)

    def test_the_lead_arrives_exactly_on_the_copy_timeline(self):
        """An impulse reaches the output `latency_samples` later, not before."""
        line = instrument_harmony.GuitarHarmony()
        lookup = values()
        with mock.patch.object(options, "get", lookup):
            for _ in range(4):                       # let the line open
                line.shape(bytes(FRAME * 2))
            impulse = np.zeros(FRAME, dtype=np.float32)
            impulse[100] = 0.8
            out = bytearray(line.shape(to_pcm(impulse)))
            for _ in range(3):                       # the lead is a delay
                out += line.shape(bytes(FRAME * 2))
        audio = from_pcm(bytes(out))
        delay = line.latency_samples()
        share = 1.0 / (1.0 + 1.0)                    # lead and one equal copy
        self.assertAlmostEqual(float(audio[delay + 100]), 0.8 * share, delta=0.05)
        self.assertLess(float(np.max(np.abs(audio[:delay]))), 0.05,
                        "the lead arrived before the copy")

    def test_closing_returns_the_lead_to_the_live_string(self):
        line = instrument_harmony.GuitarHarmony()
        source = tone(4.0)
        data = to_pcm(source)
        with mock.patch.object(options, "get", values()):
            for start in range(0, SR * 2, FRAME * 2):
                line.shape(data[start:start + FRAME * 2])
        self.assertEqual(line.latency_samples(), line.LOOKAHEAD)
        tail = bytearray()
        with mock.patch.object(options, "get",
                               values(**{instrument_harmony.SWITCH_OPTION: False})):
            for start in range(SR * 2, len(data), FRAME * 2):
                tail += line.shape(data[start:start + FRAME * 2])
            after = from_pcm(line.shape(data[-FRAME * 2:]))
        self.assertEqual(line._voices, [])
        self.assertEqual(line.latency_samples(), 0)
        self.assertGreater(float(np.sqrt(np.mean(after ** 2))), 0.1,
                           "the line never handed the guitar back")
        self.assertEqual(len(tail), len(data) - SR * 2, "a frame was lost")


class TheTransitionTests(unittest.TestCase):
    """Opening, closing and a bass copy joining leave no step in the audio.

    The lead's delay is what makes the chord, and it is also a jump the moment
    the line opens or closes (26 ms of audio repeated or skipped), while a copy
    that joins has to arrive from silence. All three are faded per sample:
    the number to beat is the tone's own slope, about 0.0086 per sample for a
    0.3 sine at 220 Hz (measured 0.0087 after the fix, against 0.097 before
    it -- which is the click a fade stepped once per frame makes).
    """

    SLOPE = 0.3 * 2.0 * np.pi * 220.0 / SR

    def frames(self, signal):
        data = to_pcm(signal)
        return [data[start:start + FRAME * 2]
                for start in range(0, len(data), FRAME * 2)]

    def test_switching_the_switch_on_and_off_does_not_click(self):
        chunks = self.frames(tone(3.0))
        line = instrument_harmony.GuitarHarmony()
        out = bytearray()
        marks = []
        position = 0
        for state, count in (("off", 20), ("on", 30), ("off", 30), ("on", 30)):
            lookup = (values() if state == "on" else
                      values(**{instrument_harmony.SWITCH_OPTION: False}))
            marks.append(position)
            for _ in range(count):
                with mock.patch.object(options, "get", lookup):
                    out += line.shape(chunks[position])
                position += 1
        audio = from_pcm(bytes(out))
        for mark in marks[1:]:
            window = audio[max(0, mark * FRAME - 2 * FRAME):
                           mark * FRAME + 3 * FRAME]
            step = float(np.max(np.abs(np.diff(window))))
            self.assertLess(step, 2.5 * self.SLOPE,
                            f"a step of {step:.4f} at sample {mark * FRAME}")

    def test_a_bass_copy_joining_under_a_treble_one_does_not_click(self):
        chunks = self.frames(tone(3.0))
        line = instrument_harmony.GuitarHarmony()
        out = bytearray()
        position = 0
        with mock.patch.object(options, "get", values()):
            for _ in range(30):
                out += line.shape(chunks[position])
                position += 1
        steady = float(np.max(np.abs(np.diff(from_pcm(bytes(out))))))
        lookup = values(**{"music_bot_autotune_harmony2": "bass_octave"})
        with mock.patch.object(options, "get", lookup):
            for _ in range(40):
                out += line.shape(chunks[position])
                position += 1
        step = float(np.max(np.abs(np.diff(from_pcm(bytes(out))))))
        self.assertLess(step, max(2.5 * self.SLOPE, 1.5 * steady),
                        f"a step of {step:.4f} where the bass copy joined")
        self.assertEqual(line.latency_samples(), 2560)


class TheRoutingTests(unittest.TestCase):
    """What leaves is shaped; the ear and the tracker are not."""

    FRAME = b"\x01\x02" * FRAME_SAMPLES

    def make_input(self, bot=None):
        rec = instrument_input.InstrumentInput.__new__(
            instrument_input.InstrumentInput)
        rec.recording = True
        rec.running = True
        rec.stereo = False
        rec._guitar_voice = None
        rec.frames = collections.deque(maxlen=8)
        rec.notes = collections.deque(maxlen=8)
        rec._monitor_pending = bytearray()
        rec._relay_pending = bytearray()
        rec.tracker = SimpleNamespace(fed=[], feed=lambda frame: rec.tracker.fed.append(frame))
        gameplay = SimpleNamespace(
            player=SimpleNamespace(x=0.0, y=0.0, z=0.0),
            megaphone=object(),
            voice_chat_using_megaphone=False,
        )
        rec.game = SimpleNamespace(stack=[gameplay], put=lambda fn: fn())
        rec._find_music_bot = lambda: bot
        rec.voice_leg = []
        rec._feed_guitar_voice = lambda raw, force_mega=False: rec.voice_leg.append(
            raw)
        return rec

    def test_the_voice_leg_carries_the_harmony_and_the_tracker_the_dry_frame(self):
        rec = self.make_input()
        with mock.patch.object(options, "get", values()):
            rec._emit_frame(self.FRAME)
        self.assertEqual(len(rec.voice_leg), 1)
        self.assertNotEqual(rec.voice_leg[0], self.FRAME)
        self.assertEqual(len(rec.voice_leg[0]), len(self.FRAME))
        # The tracker heard the string itself, not the copies.
        dry = np.frombuffer(self.FRAME, dtype="<i2").astype(np.float32) / 32768.0
        self.assertTrue(np.array_equal(rec.tracker.fed[-1], dry))

    def test_the_broadcast_leg_is_shaped_the_same_way(self):
        bot = SimpleNamespace(broadcast_enabled=True,
                              broadcast_to_megaphone=False,
                              guitar_pcm_queue=collections.deque(maxlen=10))
        rec = self.make_input(bot)
        with mock.patch.object(options, "get", values()):
            rec._emit_frame(self.FRAME)
        self.assertEqual(len(bot.guitar_pcm_queue), 1)
        self.assertEqual(rec.voice_leg, [])

    def test_the_players_own_ear_is_never_shaped(self):
        rec = self.make_input()
        with mock.patch.object(options, "get", values()):
            rec._stage(bytes(FRAME_SAMPLES * 2))
        self.assertEqual(len(rec.frames), 2)            # two 10 ms chunks
        for chunk in rec.frames:
            self.assertEqual(len(chunk), 480 * 2)       # 10 ms, raw
        self.assertEqual(b"".join(rec.frames), bytes(FRAME_SAMPLES * 2))

    def test_off_is_still_byte_identical_through_the_frame_routing(self):
        rec = self.make_input()
        with mock.patch.object(options, "get",
                               values(**{instrument_harmony.SWITCH_OPTION: False})):
            rec._emit_frame(self.FRAME)
        self.assertEqual(rec.voice_leg, [self.FRAME])

    def test_a_new_session_does_not_replay_the_old_one(self):
        rec = self.make_input()
        with mock.patch.object(options, "get", values()):
            for _ in range(4):
                rec._emit_frame(self.FRAME)
        rec._harmony.reset()
        self.assertEqual(rec._harmony.latency_samples(), 0)


class TheStaffGateTests(unittest.TestCase):
    """A Developer's Auto-Tune lock is this switch's lock too."""

    def tearDown(self):
        from libs.music_bot import gate
        gate.clear()

    def test_a_locked_auto_tune_hides_the_line_and_stops_the_copies(self):
        from libs.music_bot import gate
        gate.apply_locked({"auto_tune": "Auto-Tune"})
        self.assertEqual(instrument_harmony.menu_item(lambda: None), [])
        signal = tone(0.3)
        line, _audio = run(signal)
        self.assertEqual(line._voices, [])
        self.assertEqual(line.raw, to_pcm(signal))
        gate.clear()
        item = instrument_harmony.menu_item(lambda: None)
        self.assertEqual(len(item), 1)
        with mock.patch.object(options, "get", values()):
            self.assertIn("Third", str(item[0][0]()))


class _FakeMenu:
    """Enough of the real menu to build one and read its rows."""

    def __init__(self, *args, **kwargs):
        self.title = args[1] if len(args) > 1 else kwargs.get("title", "")
        self.items = []
        self.music_paths = []
        self.turning_sensitivity_item_text = "Turning sensitivity"
        self.turning_mode_item_text = "Turning mode"
        self.voice_chat_mode_item_text = "Voice chat mode"

    def add_items(self, items):
        self.items.extend(items)

    def set_music(self, path):
        self.music_paths.append(path)


class _FakeGame:
    audio_mngr = SimpleNamespace(hrtf=SimpleNamespace(current_model="default"))

    def toggle_item(self, name, *args):
        return name


def build(builder, *args, **kwargs):
    """Run a menu builder against a fake Menu and return the page it made."""
    from libs import menus

    created = []

    def make_menu(*a, **k):
        m = _FakeMenu(*a, **k)
        created.append(m)
        return m

    with mock.patch.object(menus.menu, "Menu", side_effect=make_menu), \
            mock.patch.object(menus, "OptionsMenu", side_effect=make_menu), \
            mock.patch.object(menus, "set_default_sounds"), \
            mock.patch.object(menus.server_config, "is_production_build",
                              return_value=True):
        builder(*args, **kwargs)
    return created[0]


def open_options(**kwargs):
    from libs import menus
    return build(menus.options_menu, _FakeGame(), lambda: None,
                 replace_call=lambda _menu: None, **kwargs)


class TheInstrumentPageTests(unittest.TestCase):
    """The guitar's two Options lines became a page of their own.

    Options is the longest list in the game; the line-in device and the second
    voice built from it are a family and nothing else in that list is about a
    guitar.  These pin where they live, what the page says, and that picking a
    device comes back to the page instead of throwing the player out of the
    menus.
    """

    def tearDown(self):
        from libs.music_bot import gate
        gate.clear()

    def test_options_no_longer_carries_the_instrument_lines(self):
        from libs import menus
        menu = open_options()
        texts = [item[0] for item in menu.items]
        self.assertFalse(any(isinstance(t, str) and t.startswith(
            "Select instrument input device") for t in texts))
        self.assertFalse(any(isinstance(t, str) and t.startswith(
            "Guitar Harmony") for t in texts))
        self.assertTrue(any(t is menus.instrument_page_label for t in texts))

    def test_the_options_line_opens_the_page(self):
        from libs import menus
        menu = open_options()
        row = next(item for item in menu.items
                   if item[0] is menus.instrument_page_label)
        opened = []
        with mock.patch.object(menus, "instrument_menu",
                               lambda *a, **k: opened.append(k)):
            row[1]()
        self.assertEqual(len(opened), 1)
        self.assertFalse(opened[0]["in_game"])

    def test_the_page_holds_the_device_and_the_switch(self):
        from libs import menus
        page = build(menus.instrument_menu, _FakeGame(), lambda: None,
                     replace_call=lambda _menu: None)
        labels = [item[0] for item in page.items]
        self.assertTrue(labels[0].startswith(
            "Select instrument input device - currently set to"))
        self.assertEqual(labels[-1], "Back")
        with mock.patch.object(options, "get", values()):
            switch = [item for item in page.items
                      if callable(item[0]) and "Guitar Harmony" in item[0]()]
        self.assertEqual(len(switch), 1)

    def test_the_page_line_says_both_facts(self):
        from libs import menus
        with mock.patch.object(options, "get", values()):
            on = menus.instrument_page_label()
        self.assertIn("system default", on)
        self.assertIn("Third", on)
        with mock.patch.object(options, "get",
                               values(**{instrument_harmony.SWITCH_OPTION: False})):
            self.assertIn("Guitar Harmony. off", menus.instrument_page_label())

    def test_a_locked_auto_tune_takes_the_row_and_the_claim(self):
        from libs import menus
        from libs.music_bot import gate
        gate.apply_locked({"auto_tune": "Auto-Tune"})
        page = build(menus.instrument_menu, _FakeGame(), lambda: None,
                     replace_call=lambda _menu: None)
        self.assertEqual([item[0] for item in page.items], [
            "Select instrument input device - currently set to system default",
            "Back",
        ])
        self.assertNotIn("Guitar Harmony", menus.instrument_page_label())

    def test_picking_a_device_comes_back_to_the_page(self):
        from libs import menus

        class Gameplay:
            popped = 0

            @staticmethod
            def pop_last_substate():
                Gameplay.popped += 1

        parent = Gameplay()
        page = build(menus.instrument_menu, _FakeGame(), lambda: None,
                     replace_call=lambda _menu: None, parent=parent,
                     in_game=True)
        captured = {}
        with mock.patch.object(menus, "input_menu",
                               lambda *a, **k: captured.update(k)):
            page.items[0][1]()
        self.assertEqual(captured["target"], "instrument")
        self.assertTrue(captured["in_game"])
        captured["func_call"]()
        self.assertEqual(Gameplay.popped, 1)

    def test_from_the_title_screen_the_device_pick_rebuilds_the_page(self):
        from libs import menus
        page = build(menus.instrument_menu, _FakeGame(), lambda: None,
                     replace_call=lambda _menu: None)
        captured = {}
        with mock.patch.object(menus, "input_menu",
                               lambda *a, **k: captured.update(k)):
            page.items[0][1]()
        with mock.patch.object(menus, "instrument_menu") as rebuild:
            captured["func_call"]()
        rebuild.assert_called_once()


class TheMicrophoneSharesTheLineTests(unittest.TestCase):
    """A press with copies and no tuner is this line, at 26.7 ms.

    Nothing is tuning the lead, so nothing needs the tuner's look-ahead: the
    microphone's Harmony-only press used to build `AutoTuneLine` (107 ms) and
    hold a tenth of a second for a detection it never ran. These pin the
    shared line's numbers from the microphone's side, and the release, which a
    guitar never asks for.
    """

    def feed(self, line, samples, frames):
        data = to_pcm(samples)
        out = bytearray()
        for start in range(0, frames * FRAME_SAMPLES * 2, FRAME_SAMPLES * 2):
            out += line.shape(data[start:start + FRAME_SAMPLES * 2])
        return out

    def test_a_third_holds_the_lead_26_point_7_ms(self):
        line = harmony.FastHarmonyLine(active=lambda: True)
        with mock.patch.object(options, "get", values()):
            self.feed(line, tone(0.2), 4)
            self.assertEqual(line.latency_samples(),
                             harmony.FastHarmonyLine.LOOKAHEAD)
            self.assertEqual(len(line._voices), 1)
        self.assertAlmostEqual(
            line.latency_samples() / 48.0, 26.67, places=2)

    def test_a_bass_copy_holds_the_lead_for_the_slower_copy(self):
        line = harmony.FastHarmonyLine(active=lambda: True)
        with mock.patch.object(options, "get", values(
                **{"music_bot_autotune_harmony": "bass_octave"})):
            self.feed(line, tone(0.2), 4)
            self.assertEqual(line.latency_samples(),
                             harmony.FastHarmonyLine.MAX_LEAD)
        self.assertAlmostEqual(
            line.latency_samples() / 48.0, 53.33, places=2)

    def test_the_release_hands_back_the_audio_the_line_was_holding(self):
        line = harmony.FastHarmonyLine(active=lambda: True)
        with mock.patch.object(options, "get", values()):
            self.feed(line, tone(0.2), 4)
            tail = line.tail_frames()
        self.assertEqual(len(tail), 1)          # 1280 samples: one 20 ms frame
        self.assertGreater(float(np.abs(from_pcm(tail[0])).max()), 0.05)

    def test_switching_off_hands_the_live_lead_back(self):
        state = {"on": True}
        line = harmony.FastHarmonyLine(active=lambda: state["on"])
        with mock.patch.object(options, "get", values()):
            self.feed(line, tone(0.2), 4)
            state["on"] = False
            self.feed(line, tone(0.2), 4)
            self.assertEqual(line.latency_samples(), 0)
            silence = bytes(FRAME_SAMPLES * 2)
            self.assertEqual(line.shape(silence), silence)


class TheLatencyReportTests(unittest.TestCase):
    """The numbers the feature promises, in one place."""

    def test_treble_copies_are_twenty_six_point_seven_milliseconds(self):
        self.assertAlmostEqual(
            instrument_harmony.GuitarHarmony.LOOKAHEAD / 48.0, 26.67, places=2)
        from libs.music_bot.transpose import ChromaticTransposer
        self.assertAlmostEqual(ChromaticTransposer.LOOKAHEAD / 48.0, 106.67,
                               places=2)          # what it replaced
        self.assertLess(instrument_harmony.GuitarHarmony.LOOKAHEAD,
                        ChromaticTransposer.LOOKAHEAD / 2)


if __name__ == "__main__":
    unittest.main()
