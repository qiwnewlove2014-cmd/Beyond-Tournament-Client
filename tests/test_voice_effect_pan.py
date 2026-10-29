"""The Pan Delay -- a delay the listener hears move.

`libs/music_bot/voice_effects/pan.py` is the one preset that is not only a shape
on the mono frame: a voice packet has no left or right in it, so the *movement*
is applied by each listener's own machine, from three bytes the Server relays
beside the voice. This file pins both halves.

1. **The settings are typed.** 20-2000 whole milliseconds, saved, clamped, and a
   value that is not a number changes nothing. `Motion` is three choices.
2. **The delay is real.** Three repeats, exactly the typed delay apart, each
   quieter than the one before it -- measured, not asserted from a table.
3. **The movement is a curve, not a wobble.** It starts at one ear, crosses the
   middle at half a delay and reaches the other ear one delay later; Front-Back
   moves along the listener's forward axis and Off moves nothing.
4. **The transport is three bytes and gated.** It is sent only to a Server that
   advertised `voice_motion_v1`, only while something is moving (plus one centre
   packet when it stops), and a listener whose entity is not here ignores it.
5. **The listener places the voice on their own axes.** The body's position is
   never moved; one metre along the listener's right (or forward) is added when
   the source is placed, and a movement that stops arriving is centred by the
   voice frames themselves.

No game, OpenAL, ffmpeg or network is required: the source, the network and the
audio inbox are all stand-ins.
"""

import os
import sys
import time
import unittest
from types import SimpleNamespace
from unittest import mock

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from libs import consts, event_handeler, voice_chat  # noqa: E402
from libs.music_bot.voice_effects import builder  # noqa: E402
from libs.music_bot.voice_effects import dsp  # noqa: E402
from libs.music_bot.voice_effects import pan  # noqa: E402
from libs.music_bot import voice_effects  # noqa: E402
from libs.objects.entity import Entity  # noqa: E402

SR = voice_effects.SAMPLE_RATE
FRAME = voice_effects.FRAME_BYTES


# ------------------------------------------------------------------- signals


def tone(freq=300.0, amplitude=0.3, seconds=0.02):
    t = np.arange(int(SR * seconds)) / SR
    return (amplitude * np.sin(2.0 * np.pi * freq * t)).astype(np.float32)


def to_pcm(signal):
    return np.clip(signal * 32768.0, -32768, 32767).astype("<i2").tobytes()


def from_pcm(data):
    return np.frombuffer(bytes(data), dtype="<i2").astype(np.float64) / 32768.0


def play_frames(processor, frames):
    """Feed silence except for the frames handed in; return each frame's peak."""
    peaks = []
    for index in range(frames):
        if index < 5:
            block = to_pcm(tone())
        else:
            block = bytes(FRAME)
        out = from_pcm(processor.process(block))
        peaks.append(float(np.max(np.abs(out))))
    return peaks


# ---------------------------------------------------------------- the settings


class ThePanSettingsTests(unittest.TestCase):
    """Two settings, both saved, both clamped, both live."""

    def setUp(self):
        self.addCleanup(mock.patch.stopall)
        self.saved = {}
        mock.patch.object(
            pan.options, "get",
            side_effect=lambda key, default=None: self.saved.get(key, default)
        ).start()
        mock.patch.object(
            pan.options, "set",
            side_effect=lambda key, value, autosave=True:
            self.saved.__setitem__(key, value)
        ).start()

    def test_the_page_opens_on_three_hundred_milliseconds_and_stereo(self):
        self.assertEqual(pan.delay_ms(), 300)
        self.assertEqual(pan.motion(), "stereo")
        self.assertEqual(pan.summary(), "300 ms, Stereo")
        self.assertEqual(pan.MIN_DELAY_MS, 20)
        self.assertEqual(pan.MAX_DELAY_MS, 2000)

    def test_a_typed_number_is_what_is_stored(self):
        self.assertEqual(pan.set_delay_ms("250"), 250)
        self.assertEqual(pan.delay_ms(), 250)
        self.assertEqual(pan.set_delay_ms("250ms"), 250)
        self.assertEqual(pan.set_delay_ms("0.3 s"), 300)

    def test_the_range_is_the_range(self):
        self.assertEqual(pan.set_delay_ms("5"), pan.MIN_DELAY_MS)
        self.assertEqual(pan.set_delay_ms("9000"), pan.MAX_DELAY_MS)
        self.assertEqual(pan.set_delay_ms("-40"), pan.MIN_DELAY_MS)

    def test_text_that_is_not_a_number_changes_nothing(self):
        pan.set_delay_ms("480")
        for text in ("", "   ", "soon", "1/3"):
            with self.subTest(text=text):
                self.assertIsNone(pan.set_delay_ms(text))
                self.assertEqual(pan.delay_ms(), 480)

    def test_the_movement_is_one_of_three_and_a_bad_one_is_ignored(self):
        self.assertEqual(pan.set_motion("front_back"), "front_back")
        self.assertEqual(pan.motion(), "front_back")
        self.assertIsNone(pan.set_motion("sideways"))
        self.assertEqual(pan.motion(), "front_back")
        self.assertEqual(pan.motion_label(), "Front-Back")
        self.assertTrue(pan.motion_note("stereo").strip())
        self.assertEqual(pan.motion_note("sideways"), "")

    def test_a_saved_value_the_page_no_longer_offers_reads_as_the_default(self):
        self.saved[pan.DELAY_OPTION] = "not a number"
        self.saved[pan.MOTION_OPTION] = "vibrate"
        self.assertEqual(pan.delay_ms(), pan.DEFAULT_DELAY_MS)
        self.assertEqual(pan.motion(), "stereo")

    def test_every_change_is_a_revision(self):
        before = pan.revision()
        pan.set_delay_ms("420")
        self.assertGreater(pan.revision(), before)
        middle = pan.revision()
        pan.set_motion("front_back")
        self.assertGreater(pan.revision(), middle)

    def test_the_spec_is_the_chain_plus_this_page(self):
        pan.set_delay_ms("500")
        pan.set_motion("front_back")
        spec = pan.spec()
        self.assertEqual(spec["pan_delay"], (0.5, pan.TAPS, pan.METRES))
        self.assertEqual(spec["pan_motion"], "front_back")
        self.assertEqual(spec["hp"], 120.0)
        self.assertEqual(spec["lp"], 6500.0)
        self.assertIsNone(spec["shape"])

    def test_reset_puts_both_back(self):
        pan.set_delay_ms("900")
        pan.set_motion("off")
        self.assertEqual(pan.clear(), 300)
        self.assertEqual(pan.motion(), "stereo")
        self.assertEqual(pan.preset(), pan.CUSTOM_PRESET)

    def test_every_named_setting_is_a_delay_a_movement_and_a_sentence(self):
        ids = [stored for stored, *_rest in pan.PRESETS]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertIn(pan.CUSTOM_PRESET, ids)
        for stored, label, milliseconds, mode, note in pan.PRESETS:
            with self.subTest(preset=stored):
                self.assertTrue(label.strip())
                self.assertGreater(len(note), 60)
                if stored == pan.CUSTOM_PRESET:
                    # The player's own two numbers: nothing stored, read live.
                    self.assertIsNone(milliseconds)
                    self.assertIsNone(mode)
                    continue
                self.assertGreaterEqual(milliseconds, pan.MIN_DELAY_MS)
                self.assertLessEqual(milliseconds, pan.MAX_DELAY_MS)
                self.assertIn(mode, ("stereo", "front_back", "off"))
        # ...and the board's slot can offer them without a second table.
        self.assertEqual(
            pan.preset_choices(),
            tuple((label, stored, note)
                  for stored, label, _ms, _mode, note in pan.PRESETS))

    def test_a_named_setting_writes_both_numbers_and_the_mark(self):
        self.assertEqual(pan.set_preset("ping_pong"), "Ping-Pong")
        self.assertEqual(pan.delay_ms(), 120)
        self.assertEqual(pan.motion(), "stereo")
        self.assertEqual(pan.preset(), "ping_pong")
        self.assertEqual(pan.preset_label(), "Ping-Pong")
        self.assertEqual(pan.summary(), "120 ms, Stereo")
        self.assertEqual(pan.spec()["pan_delay"], (0.12, pan.TAPS, pan.METRES))
        self.assertIsNone(pan.set_preset("not_a_preset"))
        self.assertEqual(pan.preset(), "ping_pong")

    def test_typing_or_choosing_by_hand_makes_the_pair_the_players_own(self):
        pan.set_preset("wide_sweep")
        pan.set_delay_ms("480")
        self.assertEqual(pan.preset(), pan.CUSTOM_PRESET)
        pan.set_preset("front_walk")
        pan.set_motion("off")
        self.assertEqual(pan.preset(), pan.CUSTOM_PRESET)
        # The mark is what `custom` means: 480 ms would be `custom` either way,
        # but a chosen name keeps its own numbers until they are moved.
        pan.set_preset("deep_behind")
        self.assertEqual((pan.delay_ms(), pan.motion()), (1000, "front_back"))

    def test_a_custom_mark_follows_the_page_and_a_name_does_not(self):
        pan.set_preset("custom")
        pan.set_delay_ms("700")
        self.assertEqual(pan.preset_values(pan.CUSTOM_PRESET), (700, "stereo"))
        self.assertEqual(pan.preset_values("slow_drift"), (1700, "stereo"))
        pan.set_preset("slow_drift")
        pan.set_delay_ms("250")
        # Typing after a name does not rename the preset; it leaves it, which is
        # what the mark says.
        self.assertEqual(pan.preset(), pan.CUSTOM_PRESET)
        self.assertEqual(pan.preset_values("slow_drift"), (1700, "stereo"))

    def test_the_spec_keys_are_the_pages_own_two_keys(self):
        pan.set_preset("front_walk")
        keys = pan.spec_keys("front_walk")
        self.assertEqual(keys["pan_delay"], (0.4, pan.TAPS, pan.METRES))
        self.assertEqual(keys["pan_motion"], "front_back")
        self.assertEqual(pan.spec_keys("custom")["pan_delay"],
                         (0.4, pan.TAPS, pan.METRES))
        self.assertEqual(pan.spec_keys("custom")["pan_motion"], "front_back")

    def test_clear_text_names_the_delay_and_the_movement(self):
        pan.set_delay_ms("640")
        pan.set_motion("front_back")
        text = pan.note()
        self.assertIn("640", text)
        self.assertIn("front-back", text)
        self.assertIn("milliseconds", text)


# ------------------------------------------------------------------ catalogue


class ThePanCatalogueTests(unittest.TestCase):
    def test_the_preset_is_a_line_of_its_own_on_the_voice_effect_page(self):
        """A typed setting cannot be a row of fixed steps, so it is not in a
        family: it sits beside Custom Voice, on the page the families are on."""
        self.assertEqual(voice_effects.catalogue_problems(), [])
        self.assertIn(voice_effects.PAN_EFFECT,
                      voice_effects.STANDALONE_EFFECTS)
        self.assertIsNone(voice_effects.group_of(voice_effects.PAN_EFFECT))
        self.assertNotIn(voice_effects.PAN_EFFECT,
                         voice_effects.group_members("motion"))
        self.assertEqual(voice_effects.effect_label(voice_effects.PAN_EFFECT),
                         "Pan Delay")
        self.assertGreater(
            len(voice_effects.effect_note(voice_effects.PAN_EFFECT)), 60)

    def test_the_motion_channel_is_outside_the_voice_channel_range(self):
        # get_available_voice_channel() hands players 21-255, so a movement
        # packet on one of those would be decoded as somebody's voice.
        self.assertEqual(consts.CHANNEL_VOICE_MOTION, 254)
        self.assertGreater(consts.CHANNEL_VOICE_MOTION,
                           consts.CHANNEL_MEGAPHONE_TIMELINE)


# --------------------------------------------------------------------- sound


class ThePanDelayTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(mock.patch.stopall)
        self.saved = {}
        mock.patch.object(
            pan.options, "get",
            side_effect=lambda key, default=None: self.saved.get(key, default)
        ).start()
        mock.patch.object(
            pan.options, "set",
            side_effect=lambda key, value, autosave=True:
            self.saved.__setitem__(key, value)
        ).start()

    def test_three_repeats_exactly_the_delay_apart_and_falling(self):
        peaks = play_frames(voice_effects._EffectProcessor(
            voice_effects.PAN_EFFECT), 50)
        # 300 ms is 15 frames.
        first = max(peaks[14:17])
        second = max(peaks[29:32])
        third = max(peaks[44:47])
        self.assertGreater(first, 0.02)
        self.assertGreater(second, 0.005)
        self.assertGreater(third, 0.002)
        self.assertGreater(first, second)
        self.assertGreater(second, third)
        # ...and nothing between the word and its first repeat.
        for quiet in range(6, 14):
            with self.subTest(frame=quiet):
                self.assertLess(peaks[quiet], 0.001)

    def test_the_typed_delay_moves_the_repeats(self):
        pan.set_delay_ms("500")
        peaks = play_frames(voice_effects._EffectProcessor(
            voice_effects.PAN_EFFECT), 60)
        self.assertLess(max(peaks[6:23]), 0.001)
        self.assertGreater(max(peaks[24:27]), 0.02)

    def test_a_frame_in_is_a_frame_out_and_silence_stays_silent(self):
        processor = voice_effects._EffectProcessor(voice_effects.PAN_EFFECT)
        for _ in range(6):
            self.assertEqual(len(processor.process(bytes(FRAME))), FRAME)
            out = from_pcm(processor.process(bytes(FRAME)))
            self.assertEqual(float(np.max(np.abs(out))), 0.0)

    def test_a_loud_voice_never_leaves_the_ceiling(self):
        processor = voice_effects._EffectProcessor(voice_effects.PAN_EFFECT)
        for _ in range(4):
            out = from_pcm(processor.process(to_pcm(tone(amplitude=0.95))))
            self.assertLessEqual(float(np.max(np.abs(out))),
                                 voice_effects.PEAK_CEILING + 1e-6)


class ThePanPresetSoundTests(unittest.TestCase):
    """A named setting is a delay and a movement, and both are heard."""

    def setUp(self):
        self.addCleanup(mock.patch.stopall)
        self.saved = {}
        mock.patch.object(
            pan.options, "get",
            side_effect=lambda key, default=None: self.saved.get(key, default)
        ).start()
        mock.patch.object(
            pan.options, "set",
            side_effect=lambda key, value, autosave=True:
            self.saved.__setitem__(key, value)
        ).start()

    def test_a_named_setting_moves_the_repeats_and_the_crossing(self):
        pan.set_preset("ping_pong")            # 120 ms: six frames
        peaks = play_frames(voice_effects._EffectProcessor(
            voice_effects.PAN_EFFECT), 30)
        self.assertGreater(max(peaks[5:8]), 0.02)
        processor = voice_effects._EffectProcessor(voice_effects.PAN_EFFECT)
        for _ in range(6):
            processor.process(bytes(FRAME))
        self.assertLess(processor.motion()[0], 0.0)   # six frames: the crossing

    def test_front_walk_moves_the_other_axis(self):
        pan.set_preset("front_walk")
        processor = voice_effects._EffectProcessor(voice_effects.PAN_EFFECT)
        processor.process(bytes(FRAME))
        right, forward = processor.motion()
        self.assertEqual(right, 0.0)
        self.assertGreater(forward, 0.9)


class ThePanMovementTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(mock.patch.stopall)
        self.saved = {}
        mock.patch.object(
            pan.options, "get",
            side_effect=lambda key, default=None: self.saved.get(key, default)
        ).start()
        mock.patch.object(
            pan.options, "set",
            side_effect=lambda key, value, autosave=True:
            self.saved.__setitem__(key, value)
        ).start()

    def positions(self, frames=32):
        processor = voice_effects._EffectProcessor(voice_effects.PAN_EFFECT)
        places = []
        for _ in range(frames):
            processor.process(bytes(FRAME))
            places.append(processor.motion())
        return places

    def test_the_voice_starts_on_one_ear_and_reaches_the_other_a_delay_later(self):
        places = self.positions(32)
        self.assertAlmostEqual(places[0][0], 1.0, delta=0.05)
        self.assertAlmostEqual(places[7][0], 0.0, delta=0.15)     # 150 ms: half
        self.assertAlmostEqual(places[15][0], -1.0, delta=0.08)   # 300 ms: left
        self.assertAlmostEqual(places[30][0], 1.0, delta=0.10)    # back again
        self.assertEqual([place[1] for place in places], [0.0] * 32)

    def test_front_back_moves_along_the_other_axis(self):
        pan.set_motion("front_back")
        places = self.positions(16)
        self.assertAlmostEqual(places[0][0], 0.0)
        self.assertAlmostEqual(places[0][1], 1.0, delta=0.05)
        self.assertAlmostEqual(places[15][1], -1.0, delta=0.08)

    def test_off_keeps_the_delay_and_drops_the_movement(self):
        pan.set_motion("off")
        places = self.positions(20)
        for right, forward in places:
            self.assertEqual((right, forward), (0.0, 0.0))

    def test_any_other_preset_sits_at_the_centre(self):
        for preset in ("amp", "echo", "trem"):
            with self.subTest(preset=preset):
                processor = voice_effects._EffectProcessor(preset)
                processor.process(bytes(FRAME))
                self.assertEqual(processor.motion(), (0.0, 0.0))

    def test_the_line_answers_for_the_frame_it_just_shaped(self):
        line = voice_effects.VoiceEffectLine()
        with mock.patch.object(dsp, "effect_id",
                               return_value=voice_effects.PAN_EFFECT):
            line.shape(bytes(FRAME))
            right, forward = line.motion()
            self.assertGreater(abs(right), 0.9)
            self.assertEqual(forward, 0.0)

    def test_a_typed_delay_rebuilds_the_line(self):
        line = voice_effects.VoiceEffectLine()
        with mock.patch.object(dsp, "effect_id",
                               return_value=voice_effects.PAN_EFFECT):
            line.shape(bytes(FRAME))
            processor = line._processor
            pan.set_delay_ms("800")
            line.shape(bytes(FRAME))
            self.assertIsNot(line._processor, processor)


# ----------------------------------------------------------------- transport


class ThePanTransportTests(unittest.TestCase):
    def compression(self, supported=True, own_channel=42):
        # No thread: the method under test touches the network and two fields.
        comp = voice_chat.voice_chat_compression.__new__(
            voice_chat.voice_chat_compression)
        comp.game = SimpleNamespace(
            network=mock.Mock(),
            voice_motion_supported=supported,
            gameplay=SimpleNamespace(own_voice_channel=own_channel),
        )
        return comp

    def test_a_byte_is_a_place_from_minus_one_to_one(self):
        for value in (-1.0, -0.5, 0.0, 0.5, 1.0):
            with self.subTest(value=value):
                byte = voice_chat.voice_chat_compression._motion_byte(value)
                self.assertGreaterEqual(byte, 1)
                self.assertLessEqual(byte, 255)
                decoded = (byte - 128) / 127.0
                self.assertAlmostEqual(decoded, value, delta=0.01)

    def test_three_bytes_on_the_movement_channel(self):
        comp = self.compression()
        comp.pending_motion = (1.0, -1.0)
        comp._send_motion()
        comp.game.network.send.assert_called_once_with(
            consts.CHANNEL_VOICE_MOTION, "n/a", bytes((42, 255, 1)))

    def test_a_centred_frame_costs_nothing_until_the_movement_stops(self):
        comp = self.compression()
        comp.pending_motion = (0.0, 0.0)
        comp._send_motion()
        comp.game.network.send.assert_not_called()
        # ...but a movement that stops sends one centre, so the listener can
        # put the voice back beside the body without waiting for a timeout.
        comp.pending_motion = (1.0, 0.0)
        comp._send_motion()
        comp.pending_motion = (0.0, 0.0)
        comp._send_motion()
        self.assertEqual(comp.game.network.send.call_count, 2)
        self.assertEqual(comp.game.network.send.call_args[0][2],
                         bytes((42, 128, 128)))
        comp._send_motion()
        self.assertEqual(comp.game.network.send.call_count, 2)

    def test_a_server_that_did_not_advertise_it_gets_no_packets(self):
        comp = self.compression(supported=False)
        comp.pending_motion = (1.0, 0.0)
        comp._send_motion()
        comp.game.network.send.assert_not_called()

    def test_with_no_channel_yet_nothing_is_sent(self):
        comp = self.compression(own_channel=None)
        comp.pending_motion = (1.0, 0.0)
        comp._send_motion()
        comp.game.network.send.assert_not_called()


# ------------------------------------------------------------------ listener


class _Listener:
    """`EventHandeler._apply_voice_motion` with a map of voice channels."""

    _apply_voice_motion = event_handeler.EventHandeler._apply_voice_motion

    def __init__(self, entities):
        self.gameplay = SimpleNamespace(voice_channels=entities)


class ThePanListenerTests(unittest.TestCase):
    def test_three_bytes_become_a_place_on_the_listeners_axes(self):
        moved = {}
        entity = SimpleNamespace(
            set_voice_motion=lambda right, forward: moved.update(
                right=right, forward=forward))
        listener = _Listener({42: entity})
        listener._apply_voice_motion(bytes((42, 255, 1)))
        self.assertAlmostEqual(moved["right"], 1.0, delta=0.01)
        self.assertAlmostEqual(moved["forward"], -1.0, delta=0.01)
        listener._apply_voice_motion(bytes((42, 128, 128)))
        self.assertAlmostEqual(moved["right"], 0.0, places=3)

    def test_a_short_packet_or_an_absent_speaker_is_ignored(self):
        moved = []
        listener = _Listener({})
        listener._apply_voice_motion(bytes((42, 255)))
        listener._apply_voice_motion(bytes((99, 255, 1)))
        listener._apply_voice_motion(None)
        self.assertEqual(moved, [])

    def test_an_entity_without_the_hook_is_ignored(self):
        listener = _Listener({42: SimpleNamespace()})
        listener._apply_voice_motion(bytes((42, 255, 1)))


class ThePanPlacementTests(unittest.TestCase):
    """The voice source, on the listener's own right and forward axes."""

    def entity(self):
        entity = Entity.__new__(Entity)
        entity.x, entity.y, entity.z = 10.0, 20.0, 0.0
        entity.vc_source = SimpleNamespace(position=None)
        deferred = []
        entity.game = SimpleNamespace(audio_mngr=SimpleNamespace(
            orientation=(0.0, 1.0, 0.0, 0.0, 0.0, 1.0),
            defer_audio=lambda action: deferred.append(action),
        ))
        entity.deferred = deferred
        return entity

    def test_no_movement_leaves_the_voice_on_the_body(self):
        entity = self.entity()
        self.assertTrue(entity.place_voice_source())
        self.assertEqual(entity.vc_source.position, (10.0, 20.0, 0.0))

    def test_stereo_moves_along_the_listeners_right(self):
        entity = self.entity()
        entity.set_voice_motion(1.0, 0.0)
        self.assertEqual(len(entity.deferred), 1)
        entity.deferred[0]()
        # Facing +Y, right is +X: one metre beside the body.
        self.assertAlmostEqual(entity.vc_source.position[0], 11.0, places=5)
        self.assertAlmostEqual(entity.vc_source.position[1], 20.0, places=5)
        self.assertAlmostEqual(entity.vc_source.position[2], 0.0, places=5)

    def test_front_back_moves_along_the_listeners_forward(self):
        entity = self.entity()
        entity.set_voice_motion(0.0, -1.0)
        entity.deferred[0]()
        self.assertAlmostEqual(entity.vc_source.position[0], 10.0, places=5)
        self.assertAlmostEqual(entity.vc_source.position[1], 19.0, places=5)

    def test_the_body_itself_never_moves(self):
        entity = self.entity()
        entity.set_voice_motion(1.0, 0.0)
        entity.deferred[0]()
        self.assertEqual((entity.x, entity.y, entity.z), (10.0, 20.0, 0.0))

    def test_a_movement_that_stops_is_centred_by_the_timeout(self):
        entity = self.entity()
        entity.set_voice_motion(1.0, 0.0)
        entity.deferred.clear()
        entity._voice_motion_at = time.monotonic() - 2.0
        entity.expire_voice_motion()
        self.assertEqual(entity._voice_motion, (0.0, 0.0))
        self.assertEqual(len(entity.deferred), 1)
        entity.deferred[0]()
        self.assertAlmostEqual(entity.vc_source.position[0], 10.0, places=5)

    def test_a_fresh_movement_is_left_alone(self):
        entity = self.entity()
        entity.set_voice_motion(1.0, 0.0)
        entity.deferred.clear()
        entity.expire_voice_motion()
        self.assertEqual(entity._voice_motion, (1.0, 0.0))
        self.assertEqual(entity.deferred, [])


# ------------------------------------------------- the Custom Voice board


class ThePanBoardTests(unittest.TestCase):
    """The delay set here is a delay another function can play.

    The Custom Voice board's Pan Delay slot is generated from this module's own
    table, so the two pages cannot drift; `Custom (this page)` follows the two
    settings live, and the board is rebuilt when that page moves.
    """

    def setUp(self):
        self.addCleanup(mock.patch.stopall)
        self.saved = {}
        # One seam: `pan.options` and `builder.options`
        # are the same module, so the two features are saved together here.
        mock.patch.object(
            pan.options, "get",
            side_effect=lambda key, default=None: self.saved.get(key, default)
        ).start()
        mock.patch.object(
            pan.options, "set",
            side_effect=lambda key, value, autosave=True:
            self.saved.__setitem__(key, value)
        ).start()
        builder.clear()

    def labels(self):
        return [row[0] for row in builder.choices("pan")]

    def test_the_board_offers_every_name_the_page_does(self):
        labels = self.labels()
        self.assertEqual(labels[0], "Off")
        for _stored, label, _ms, _mode, _note in pan.PRESETS:
            self.assertIn(label, labels)
        self.assertEqual(len(labels), len(pan.PRESETS) + 1)

    def test_off_adds_nothing_and_a_name_adds_the_whole_block(self):
        self.assertNotIn("pan_delay", builder.spec())
        self.assertNotIn("pan_motion", builder.spec())
        builder.set_value("pan", "ping_pong")
        self.assertEqual(builder.spec()["pan_delay"],
                         (0.12, pan.TAPS, pan.METRES))
        self.assertEqual(builder.spec()["pan_motion"], "stereo")
        self.assertIn("Ping-Pong", builder.summary())
        builder.set_value("pan", "off")
        self.assertNotIn("pan_delay", builder.spec())

    def test_custom_plays_whatever_the_page_holds(self):
        builder.set_value("pan", "custom")
        self.assertTrue(builder.uses_pan_page())
        self.assertEqual(builder.spec()["pan_delay"][0], 0.3)
        pan.set_delay_ms("900")
        pan.set_motion("front_back")
        self.assertEqual(builder.spec()["pan_delay"],
                         (0.9, pan.TAPS, pan.METRES))
        self.assertEqual(builder.spec()["pan_motion"], "front_back")
        builder.set_value("pan", "wide_sweep")
        self.assertFalse(builder.uses_pan_page())

    def test_a_board_on_custom_is_rebuilt_when_that_page_moves(self):
        builder.set_value("pan", "custom")
        before = voice_effects._board_revision(voice_effects.BUILDER_EFFECT)
        pan.set_delay_ms("820")
        self.assertGreater(
            voice_effects._board_revision(voice_effects.BUILDER_EFFECT), before)
        # On a NAMED setting the two counts are independent again: the board
        # holds that name's numbers, so the page moving is not its business.
        builder.set_value("pan", "ping_pong")
        fixed = voice_effects._board_revision(voice_effects.BUILDER_EFFECT)
        pan.set_delay_ms("560")
        self.assertEqual(
            voice_effects._board_revision(voice_effects.BUILDER_EFFECT), fixed)

    def test_the_board_builds_a_processor_that_moves_the_voice(self):
        builder.set_value("pan", "front_walk")
        processor = voice_effects._EffectProcessor(voice_effects.BUILDER_EFFECT)
        processor.process(bytes(FRAME))
        right, forward = processor.motion()
        self.assertEqual(right, 0.0)
        self.assertGreater(forward, 0.9)


if __name__ == "__main__":
    unittest.main()
