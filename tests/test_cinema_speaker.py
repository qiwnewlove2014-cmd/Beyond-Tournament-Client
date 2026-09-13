"""Offline regressions for the Cinema Speaker System; no device, no OpenAL.

The load-bearing test here is parity: the ``front_only`` profile must
reproduce the plain two-source jukebox byte for byte, because the whole
safety argument for the cinema layer is that a room with no cinema speakers
sounds exactly like it did before the feature existed.
"""

import array
import unittest
from types import SimpleNamespace

from libs.audio.cinema import (ChannelAnalyzer, CinemaLayout, CinemaRenderer,
                               CinemaSpeakerSpec, CinemaSpeakerHost,
                               acquire_renderer, get_profile, mid_side,
                               mix_channels, release_all, release_renderer,
                               set_enabled, source_layout)
from libs.audio.cinema.layout import (bearing_from, slot_for_bearing)
from libs.audio.cinema.profiles import PROFILES


def stereo_bytes(left, right):
    """Interleaved s16le stereo, the layout both jukebox transports produce."""
    samples = array.array("h")
    for l_value, r_value in zip(left, right):
        samples.append(l_value)
        samples.append(r_value)
    return samples.tobytes()


def mono_bytes(values):
    samples = array.array("h")
    samples.extend(values)
    return samples.tobytes()


def to_mono_values(data):
    samples = array.array("h")
    samples.frombytes(data)
    return list(samples)


LEFT = [1000, -2000, 3000, -4, 32767, -32768]
RIGHT = [-1000, 500, 2999, 4, 32760, 100]
# The renderer is fed the SPLIT mono channels, exactly as both jukebox
# transports hand them over after splitting the interleaved frame.
LEFT_PCM = mono_bytes(LEFT)
RIGHT_PCM = mono_bytes(RIGHT)
FRAME = stereo_bytes(LEFT, RIGHT)


class ChannelMathTests(unittest.TestCase):
    def test_mid_and_side_reconstruct_the_original_channels(self):
        mid, side = mid_side(mono_bytes(LEFT), mono_bytes(RIGHT))
        mid_values, side_values = to_mono_values(mid), to_mono_values(side)
        # L = M + S and R = M - S, so the image is recoverable, not lost.
        # Integer mid/side costs at most one LSB when the sum of the pair is
        # odd and halves cannot be stored; that is -90 dBFS, far below the
        # noise floor, and the front pair never goes through mid/side at all
        # (its weights are exactly (1, 0) and (0, 1)).
        for recovered, original in zip((m + s for m, s in zip(mid_values, side_values)), LEFT):
            self.assertLessEqual(abs(recovered - original), 1)
        for recovered, original in zip((m - s for m, s in zip(mid_values, side_values)), RIGHT):
            self.assertLessEqual(abs(recovered - original), 1)

    def test_mid_side_stays_inside_int16_at_the_extremes(self):
        mid, side = mid_side(mono_bytes([32767, -32768]), mono_bytes([-32768, 32767]))
        for value in to_mono_values(mid) + to_mono_values(side):
            self.assertGreaterEqual(value, -32768)
            self.assertLessEqual(value, 32767)

    def test_unity_weights_are_bit_exact(self):
        left = mono_bytes(LEFT)
        right = mono_bytes(RIGHT)
        self.assertEqual(mix_channels(left, right, 1.0, 0.0), left)
        self.assertEqual(mix_channels(left, right, 0.0, 1.0), right)

    def test_side_weight_is_the_difference(self):
        left = mono_bytes([1000, -1000])
        right = mono_bytes([0, 0])
        self.assertEqual(mix_channels(left, right, 0.5, -0.5), mono_bytes([500, -500]))

    def test_overlapping_full_channels_clamp_instead_of_wrapping(self):
        left = mono_bytes([32767])
        right = mono_bytes([32767, ])
        self.assertEqual(to_mono_values(mix_channels(left, right, 1.0, 1.0)), [32767])


class SourceLayoutTests(unittest.TestCase):
    def test_unknown_layout_tokens_mean_decide_from_audio(self):
        self.assertEqual(source_layout("mono"), "mono")
        self.assertEqual(source_layout(" MONO "), "mono")
        self.assertEqual(source_layout("stereo"), "stereo")
        self.assertEqual(source_layout("auto"), "auto")
        for value in (None, "", "quadraphonic", 7):
            self.assertEqual(source_layout(value), "auto")


class ChannelAnalyzerTests(unittest.TestCase):
    def analyzer(self):
        return ChannelAnalyzer(window=4)

    def test_identical_channels_are_mono(self):
        analyzer = self.analyzer()
        for _ in range(4):
            analyzer.observe(mono_bytes(LEFT), mono_bytes(LEFT))
        self.assertEqual(analyzer.layout, "mono")
        self.assertTrue(analyzer.confident)

    def test_separated_channels_stay_stereo(self):
        analyzer = self.analyzer()
        for _ in range(4):
            analyzer.observe(mono_bytes(LEFT), mono_bytes(RIGHT))
        self.assertEqual(analyzer.layout, "stereo")

    def test_silence_carries_no_evidence(self):
        analyzer = self.analyzer()
        silence = mono_bytes([0, 0, 0, 0])
        for _ in range(10):
            analyzer.observe(silence, silence)
        self.assertEqual(analyzer.layout, "stereo")
        self.assertFalse(analyzer.confident)

    def test_a_brief_centred_hit_does_not_flip_a_stereo_song(self):
        analyzer = self.analyzer()
        for _ in range(4):
            analyzer.observe(mono_bytes(LEFT), mono_bytes(RIGHT))
        analyzer.observe(mono_bytes(LEFT), mono_bytes(LEFT))
        self.assertEqual(analyzer.layout, "stereo")

    def test_reset_forgets_the_previous_song(self):
        analyzer = self.analyzer()
        for _ in range(4):
            analyzer.observe(mono_bytes(LEFT), mono_bytes(LEFT))
        analyzer.reset()
        self.assertEqual(analyzer.layout, "stereo")
        self.assertFalse(analyzer.confident)


class LayoutTests(unittest.TestCase):
    ANCHOR = (10.0, 20.0, 0.0)

    def test_ring_uses_the_engine_axis_convention(self):
        layout = CinemaLayout(self.ANCHOR)
        # X is left to right, Y is backward to forward.
        self.assertGreater(layout.position("front_c")[1], self.ANCHOR[1])
        self.assertLess(layout.position("side_l")[0], self.ANCHOR[0])
        self.assertGreater(layout.position("side_r")[0], self.ANCHOR[0])
        self.assertLess(layout.position("rear_l")[1], self.ANCHOR[1])
        self.assertEqual(layout.position("rear_l")[2], self.ANCHOR[2])

    def test_ring_keeps_the_front_pair_equidistant(self):
        layout = CinemaLayout(self.ANCHOR)
        left = layout.distance_from(self.ANCHOR, "front_l")
        right = layout.distance_from(self.ANCHOR, "front_r")
        self.assertAlmostEqual(left, right)

    def test_placed_speakers_override_the_ring(self):
        spec = CinemaSpeakerSpec("front_c", (10.0, 26.0, 3.0))
        layout = CinemaLayout(self.ANCHOR, [spec])
        self.assertEqual(layout.position("front_c"), (10.0, 26.0, 3.0))
        self.assertEqual(layout.slots, ("front_l", "front_c", "front_r", "side_l",
                                        "side_r", "rear_l", "rear_r"))

    def test_auto_speakers_are_snapped_to_the_nearest_slot(self):
        specs = [
            CinemaSpeakerSpec("auto", (10.0, 26.0, 0.0)),   # straight ahead
            CinemaSpeakerSpec("auto", (16.0, 20.0, 0.0)),   # hard right
            CinemaSpeakerSpec("auto", (4.0, 20.0, 0.0)),    # hard left
        ]
        layout = CinemaLayout(self.ANCHOR, specs, use_ring=False)
        self.assertEqual(sorted(layout.slots), ["front_c", "side_l", "side_r"])

    def test_unknown_slots_and_broken_positions_are_dropped(self):
        specs = [
            CinemaSpeakerSpec("ceiling", (0, 0, 5)),
            SimpleNamespace(slot="front_l", position=("nope", 0, 0)),
            SimpleNamespace(slot="front_r", position=(11.0, 20.0, 0.0)),
        ]
        layout = CinemaLayout(self.ANCHOR, specs, use_ring=False)
        self.assertEqual(layout.slots, ("front_r",))

    def test_max_delay_is_reported_for_the_jam_note_sync(self):
        layout = CinemaLayout(self.ANCHOR, [CinemaSpeakerSpec("rear_l", (0, 0, 0), delay_ms=25.0)])
        self.assertAlmostEqual(layout.max_delay_s, 0.025)

    def test_bearing_and_slot_mapping_are_symmetric(self):
        self.assertEqual(slot_for_bearing(0), "front_c")
        self.assertEqual(slot_for_bearing(30), "front_r")
        self.assertEqual(slot_for_bearing(-30), "front_l")
        self.assertEqual(slot_for_bearing(90), "side_r")
        self.assertEqual(slot_for_bearing(-90), "side_l")
        self.assertEqual(slot_for_bearing(200), "rear_l")
        self.assertEqual(slot_for_bearing(180), "rear_r")
        self.assertAlmostEqual(bearing_from((0, 0, 0), (0, 5, 0)), 0.0)
        self.assertAlmostEqual(bearing_from((0, 0, 0), (5, 0, 0)), 90.0)


class ProfileTests(unittest.TestCase):
    def test_front_only_is_the_plain_jukebox_pair(self):
        profile = get_profile("front_only")
        self.assertEqual(profile.weight("front_l"), (1.0, 0.0))
        self.assertEqual(profile.weight("front_r"), (0.0, 1.0))

    def test_stereo_profiles_never_feed_two_slots_the_same_content(self):
        # Two speakers with identical content comb filter at the listener,
        # which is the failure mode a multi-speaker room must never have.
        # mono_spread is the deliberate exception: it renders a mono
        # programme, where there is no image left to protect.
        for name in ("front_only", "front_stage", "surround", "theatre"):
            profile = PROFILES[name]
            weights = list(profile.weights.values())
            self.assertEqual(len(weights), len(set(weights)), name)

    def test_added_speakers_share_energy_equally(self):
        # Every speaker beyond the front pair (the centre included) shares one
        # equal-power budget, so a louder room never clips the master bus.
        self.assertEqual(get_profile("front_only").surround_scale(), 1.0)
        self.assertEqual(get_profile("front_stage").surround_scale(), 1.0)
        for name in ("surround", "theatre", "mono_spread"):
            profile = get_profile(name)
            self.assertAlmostEqual(profile.surround_scale(),
                                   1 / len(profile.surround_slots()) ** 0.5)
        self.assertAlmostEqual(get_profile("surround").surround_scale(), 1 / 3 ** 0.5)
        self.assertAlmostEqual(get_profile("theatre").surround_scale(), 1 / 5 ** 0.5)

    def test_unknown_profile_falls_back_to_the_parity_pair(self):
        self.assertEqual(get_profile("hallucinated").name, "front_only")
        self.assertEqual(get_profile(None).name, "front_only")


class RendererTests(unittest.TestCase):
    ANCHOR = (0.0, 0.0, 0.0)

    def renderer(self, profile="front_only", **kwargs):
        return CinemaRenderer(self.ANCHOR, profile, **kwargs)

    def test_front_only_is_byte_identical_to_the_source_frames(self):
        renderer = self.renderer()
        from libs.music_bot import AudioStreamer
        left, right = AudioStreamer._split_stereo_16(FRAME)
        rendered = renderer.render(left, right)
        self.assertEqual([slot for slot, _ in rendered], ["front_l", "front_r"])
        self.assertEqual(rendered[0][1], left)
        self.assertEqual(rendered[1][1], right)

    def test_front_only_never_copies_the_frame(self):
        renderer = self.renderer()
        left, right = b"\x01\x02\x03\x04", b"\x05\x06\x07\x08"
        rendered = renderer.render(left, right)
        self.assertIs(rendered[0][1], left)
        self.assertIs(rendered[1][1], right)

    def test_the_centre_speaker_carries_the_mid(self):
        renderer = self.renderer("front_stage")
        rendered = dict(renderer.render(LEFT_PCM, RIGHT_PCM))
        self.assertEqual(to_mono_values(rendered["front_c"]),
                         [(l_value + r_value) >> 1 for l_value, r_value in zip(LEFT, RIGHT)])

    def test_side_speakers_carry_the_difference_not_the_front(self):
        renderer = self.renderer("surround")
        rendered = dict(renderer.render(LEFT_PCM, RIGHT_PCM))
        scale = get_profile("surround").surround_scale()
        for value, l_value, r_value in zip(to_mono_values(rendered["side_l"]), LEFT, RIGHT):
            self.assertAlmostEqual(value, (l_value - r_value) * 0.5 * scale, delta=1)
        # The front pair is still the untouched original image.
        self.assertEqual(to_mono_values(rendered["front_l"]), LEFT)
        self.assertEqual(to_mono_values(rendered["front_r"]), RIGHT)

    def test_rear_pair_differs_from_the_front_pair(self):
        renderer = self.renderer("theatre")
        rendered = dict(renderer.render(LEFT_PCM, RIGHT_PCM))
        self.assertNotEqual(rendered["rear_l"], rendered["front_l"])
        self.assertNotEqual(rendered["rear_l"], rendered["front_r"])

    def test_front_pair_survives_the_room_scaling(self):
        renderer = self.renderer("theatre")
        rendered = dict(renderer.render(LEFT_PCM, RIGHT_PCM))
        self.assertEqual(to_mono_values(rendered["front_l"]), LEFT)
        self.assertEqual(to_mono_values(rendered["front_r"]), RIGHT)

    def test_a_mono_programme_never_reaches_the_stereo_weights(self):
        renderer = self.renderer("theatre")
        mono = mono_bytes(LEFT)
        rendered = []
        for _ in range(10):
            rendered = renderer.render(mono, mono)
        self.assertEqual(renderer.active_kind, "mono")
        feeds = dict(rendered)
        # The stereo weights would have collapsed the sides to silence and fed
        # three identical front speakers. Instead every speaker in the room
        # carries the programme at an equal-power share (there is no image to
        # protect in a mono source, so the screen wall is not exempt either).
        scale = 1 / len(renderer.slots) ** 0.5
        self.assertEqual(len(renderer.slots), 7)
        for slot in renderer.slots:
            for value, source in zip(to_mono_values(feeds[slot]), LEFT):
                self.assertAlmostEqual(value, source * scale, delta=2)

    def test_declared_layout_skips_detection(self):
        renderer = self.renderer("front_only", declared_layout="mono", detect_channels=False)
        feeds = dict(renderer.render(LEFT_PCM, RIGHT_PCM))
        self.assertEqual(renderer.active_kind, "mono")
        # A stream the server already declared mono is taken at its word:
        # the mid is the programme, and no evidence was gathered to guess.
        # The two speakers share one equal-power budget, so the room is not
        # louder than the mono programme it is playing.
        scale = 1 / 2 ** 0.5
        for value, l_value, r_value in zip(to_mono_values(feeds["front_l"]), LEFT, RIGHT):
            self.assertAlmostEqual(value, ((l_value + r_value) >> 1) * scale, delta=1)
        self.assertFalse(renderer.channel.confident)

    def test_max_speakers_drops_the_back_of_the_room_first(self):
        renderer = self.renderer("theatre", max_speakers=3)
        self.assertEqual(renderer.plan()[0][0], "front_l")
        self.assertEqual(sorted(slot for slot, _, _ in renderer.plan()),
                         ["front_c", "front_l", "front_r"])

    def test_an_empty_frame_renders_nothing(self):
        self.assertEqual(self.renderer().render(b"", b""), [])

    def test_profile_can_be_swapped_live(self):
        renderer = self.renderer("front_only")
        self.assertEqual(len(renderer.plan()), 2)
        renderer.set_profile("surround")
        self.assertEqual(len(renderer.plan()), 5)

    def test_the_room_is_exactly_the_profiles_speaker_set(self):
        # A mono passage is spread across the speakers the room already has,
        # so a two-speaker room never quietly grows seven sources.
        self.assertEqual(self.renderer("front_only").slots, ("front_l", "front_r"))
        self.assertEqual(self.renderer("surround").slots,
                         ("front_l", "front_c", "front_r", "side_l", "side_r"))
        self.assertEqual(len(self.renderer("theatre").slots), 7)


class CinemaSpeakerHostTests(unittest.TestCase):
    ANCHOR = (0.0, 0.0, 0.0)

    def test_a_disabled_host_returns_no_renderer(self):
        # The entire safety argument: with cinema off the caller keeps its
        # original two-source code path.
        host = CinemaSpeakerHost(enabled=False)
        self.assertIsNone(host.acquire("box", self.ANCHOR))
        self.assertEqual(len(host), 0)

    def test_an_enabled_host_is_idempotent_for_the_same_room(self):
        host = CinemaSpeakerHost(enabled=True, profile="theatre")
        first = host.acquire("box", self.ANCHOR)
        self.assertIs(host.acquire("box", self.ANCHOR), first)
        self.assertEqual(len(host), 1)

    def test_a_moved_cabinet_rebuilds_but_keeps_the_verdict(self):
        host = CinemaSpeakerHost(enabled=True)
        first = host.acquire("box", self.ANCHOR)
        for _ in range(10):
            first.channel.observe(mono_bytes(LEFT), mono_bytes(LEFT))
        moved = host.acquire("box", (50.0, 0.0, 0.0))
        self.assertIsNot(moved, first)
        self.assertEqual(moved.channel.layout, "mono")
        self.assertEqual(moved.layout.anchor, (50.0, 0.0, 0.0))

    def test_disabling_releases_every_room(self):
        host = CinemaSpeakerHost(enabled=True)
        host.acquire("box", self.ANCHOR)
        host.set_enabled(False)
        self.assertEqual(len(host), 0)
        self.assertIsNone(host.acquire("box", self.ANCHOR))

    def test_per_cabinet_profile_overrides_the_default(self):
        host = CinemaSpeakerHost(enabled=True, profile="front_only")
        renderer = host.acquire("box", self.ANCHOR, profile="surround")
        self.assertEqual(renderer.profile.name, "surround")

    def test_module_helpers_attach_and_detach_the_host(self):
        game = SimpleNamespace()
        self.assertIsNone(acquire_renderer(game, "box", self.ANCHOR))
        self.assertTrue(set_enabled(game, True))
        renderer = acquire_renderer(game, "box", self.ANCHOR, profile="front_stage")
        self.assertEqual(renderer.profile.name, "front_stage")
        self.assertIs(release_renderer(game, "box"), renderer)
        self.assertEqual(release_all(game), 0)


if __name__ == "__main__":
    unittest.main()
