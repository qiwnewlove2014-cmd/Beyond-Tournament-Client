import unittest
import os
import re
import tempfile

import pygame

from libs import drum_keyconfig
from libs.drums import DrumAudio
from libs.keyconfig import Keyconfig
from libs.midi.profiles import DRUM_MIDI_PROFILE

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER_LIBS = os.path.join(HERE, "..", "..", "server", "libs")
PACKET_VALIDATOR = os.path.join(SERVER_LIBS, "packet_validator.ts")
INSTRUMENT_HANDLER = os.path.join(SERVER_LIBS, "instrument_handler.ts")


class FakeKeyconfig:
    def __init__(self, keys=None):
        self.keys = dict(keys or {})
        self.save_count = 0

    def get(self, function, default):
        return self.keys.get(function, default)

    def set(self, key_code, function, autosave=True):
        self.keys[function] = key_code
        if autosave:
            self.save()

    def unset(self, function, autosave=True):
        self.keys.pop(function, None)
        if autosave:
            self.save()

    def save(self):
        self.save_count += 1


class DrumKeyconfigTests(unittest.TestCase):
    def test_defaults_preserve_the_pad_contract(self):
        keyconfig = FakeKeyconfig()
        resolved = drum_keyconfig.key_to_pad(keyconfig)
        self.assertEqual(len(resolved), 18)
        for binding in drum_keyconfig.DRUM_BINDINGS:
            self.assertEqual(resolved[binding.default_key], binding.pad)

    def test_saved_binding_replaces_only_its_keyboard_action(self):
        keyconfig = FakeKeyconfig({"drum_kick": pygame.K_b})
        resolved = drum_keyconfig.key_to_pad(keyconfig)
        self.assertEqual(resolved[pygame.K_b], 0)
        self.assertNotIn(pygame.K_SPACE, resolved)
        self.assertEqual(DRUM_MIDI_PROFILE.note_to_pad(36), 0)

    def test_alternate_key_triggers_the_same_pad_without_changing_midi(self):
        keyconfig = FakeKeyconfig({"drum_kick_alt": pygame.K_b})
        resolved = drum_keyconfig.key_to_pad(keyconfig)
        self.assertEqual(resolved[pygame.K_SPACE], 0)
        self.assertEqual(resolved[pygame.K_b], 0)
        self.assertEqual(DRUM_MIDI_PROFILE.note_to_pad(35), 0)
        self.assertEqual(DRUM_MIDI_PROFILE.note_to_pad(36), 0)

    def test_reserved_exit_keys_are_rejected(self):
        keyconfig = FakeKeyconfig()
        for key_code in drum_keyconfig.RESERVED_DRUM_KEYS:
            self.assertIsNotNone(
                drum_keyconfig.validate_key(
                    keyconfig, "drum_kick", key_code
                )
            )

    def test_conflict_checks_only_other_drum_actions(self):
        keyconfig = FakeKeyconfig({
            "move_forward": pygame.K_f,
            "drum_snare": pygame.K_f,
        })
        self.assertIsNone(
            drum_keyconfig.validate_key(
                keyconfig, "drum_snare", pygame.K_f
            )
        )
        error = drum_keyconfig.validate_key(
            keyconfig, "drum_kick", pygame.K_f
        )
        self.assertIn("Snare", error)

    def test_primary_and_alternate_cannot_share_a_key(self):
        keyconfig = FakeKeyconfig({"drum_kick_alt": pygame.K_b})
        error = drum_keyconfig.validate_key(
            keyconfig, "drum_kick", pygame.K_b
        )
        self.assertIn("Kick alternate", error)

        error = drum_keyconfig.validate_key(
            FakeKeyconfig(), "drum_kick_alt", pygame.K_SPACE
        )
        self.assertIn("Kick primary", error)

    def test_duplicate_file_entries_resolve_deterministically(self):
        keyconfig = FakeKeyconfig({
            "drum_kick": pygame.K_b,
            "drum_snare": pygame.K_b,
        })
        resolved = drum_keyconfig.key_to_pad(keyconfig)
        self.assertEqual(resolved[pygame.K_b], 0)
        self.assertNotIn(1, resolved.values())

    def test_restore_defaults_uses_one_file_write(self):
        keyconfig = FakeKeyconfig({
            "drum_kick": pygame.K_b,
            "drum_kick_alt": pygame.K_g,
            "drum_snare_alt": pygame.K_h,
        })
        drum_keyconfig.restore_defaults(keyconfig)
        self.assertEqual(keyconfig.save_count, 1)
        for binding in drum_keyconfig.DRUM_BINDINGS:
            self.assertEqual(
                keyconfig.keys[binding.function], binding.default_key
            )
            self.assertNotIn(binding.alternate_function, keyconfig.keys)

    def test_clear_one_alternate_preserves_the_primary(self):
        binding = drum_keyconfig.DRUM_BINDINGS[0]
        keyconfig = FakeKeyconfig({
            binding.function: pygame.K_b,
            binding.alternate_function: pygame.K_g,
        })
        drum_keyconfig.clear_alternate(keyconfig, binding)
        self.assertEqual(keyconfig.keys[binding.function], pygame.K_b)
        self.assertNotIn(binding.alternate_function, keyconfig.keys)
        self.assertEqual(keyconfig.save_count, 1)

    def test_clear_all_releases_defaults_for_immediate_rebinding(self):
        keyconfig = FakeKeyconfig({
            "drum_kick": pygame.K_b,
            "drum_kick_alt": pygame.K_g,
        })
        drum_keyconfig.clear_all(keyconfig)

        self.assertEqual(keyconfig.save_count, 1)
        self.assertEqual(drum_keyconfig.key_to_pad(keyconfig), {})
        self.assertTrue(all(
            keyconfig.keys[binding.function] is None
            for binding in drum_keyconfig.DRUM_BINDINGS
        ))
        self.assertIsNone(
            drum_keyconfig.validate_key(keyconfig, "drum_kick", pygame.K_z)
        )

        keyconfig.set(pygame.K_z, "drum_kick")
        self.assertEqual(drum_keyconfig.key_to_pad(keyconfig), {pygame.K_z: 0})

    def test_cleared_primaries_survive_keyconfig_save_and_reload(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_file = os.path.join(temp_dir, "keyconfig.json")
            keyconfig = Keyconfig(config_file)
            drum_keyconfig.clear_all(keyconfig)

            restored = Keyconfig(config_file)
            self.assertEqual(drum_keyconfig.key_to_pad(restored), {})
            self.assertTrue(all(
                restored.keys[binding.function] is None
                for binding in drum_keyconfig.DRUM_BINDINGS
            ))


class DrumPadServerContractTests(unittest.TestCase):
    """Every pad the client can play must be a pad the Server will relay.

    The Server owns that rule: a hit whose pad is past the schema's maximum is
    dropped before it is relayed, so it is heard by the performer (whose own
    client plays a local prediction) and by nobody else -- on the plain feed
    *and* out of a cinema room, which is only ever fed on the listener's
    machine. Pad 17 (the dedicated Rim, its own key) shipped in that state once,
    because adding a pad to the kits is a change nobody is reminded to make in
    the Server. These tests read the Server's own numbers so the two move
    together.
    """

    def setUp(self):
        if not os.path.exists(PACKET_VALIDATOR):
            self.skipTest("server sources are not in this checkout")

    def _schema_bounds(self):
        """``(min, max)`` from the play_drum_hit schema, out of its own source."""
        with open(PACKET_VALIDATOR, encoding="utf-8") as handle:
            schema = handle.read()
        entry = re.search(r"play_drum_hit:\s*\{(.*?)\n    \},", schema, re.S)
        self.assertIsNotNone(
            entry, "play_drum_hit is missing from the packet schema")
        pad = re.search(r"pad:\s*\{[^}]*\bmin:\s*(-?\d+)[^}]*\bmax:\s*(\d+)",
                        entry.group(1))
        self.assertIsNotNone(pad, "the pad bound is missing from the schema")
        return int(pad.group(1)), int(pad.group(2))

    def _handler_max(self):
        """The maximum InstrumentHandler.play_drum_hit accepts itself."""
        with open(INSTRUMENT_HANDLER, encoding="utf-8") as handle:
            handler = handle.read()
        guard = re.search(r"pad\s*>\s*(\d+)", handler)
        self.assertIsNotNone(
            guard, "InstrumentHandler no longer bounds the drum pad")
        return int(guard.group(1))

    def test_the_schema_covers_every_pad_the_kits_define(self):
        low, high = self._schema_bounds()
        self.assertEqual(low, 0)
        for kit_name, pads in DrumAudio.KITS.items():
            self.assertEqual(
                len(pads), len(DrumAudio.PAD_DEFS),
                f"kit {kit_name!r} does not define the canonical pad count")
            self.assertLessEqual(
                len(pads) - 1, high,
                f"pad {len(pads) - 1} of kit {kit_name!r} would be dropped by "
                "the Server's play_drum_hit schema")
        self.assertEqual(
            high, len(DrumAudio.PAD_DEFS) - 1,
            "a pad the kits no longer define is still accepted (or a new one "
            "is not): the schema's maximum must name the last pad")

    def test_every_configured_drum_key_is_a_pad_the_server_relays(self):
        low, high = self._schema_bounds()
        for _kit, _label, table in drum_keyconfig.binding_groups():
            for binding in table:
                self.assertGreaterEqual(binding.pad, low)
                self.assertLessEqual(
                    binding.pad, high,
                    f"the {binding.label!r} key plays pad {binding.pad}, which the "
                    "Server drops before relaying it")

    def test_the_server_guard_and_the_schema_agree(self):
        _, high = self._schema_bounds()
        self.assertEqual(
            self._handler_max(), high,
            "the schema and the handler disagree about the last pad, so one of "
            "them silently drops a hit the other lets through")


class PerKitTests(unittest.TestCase):
    """The ``per`` kit: one drum's strokes and its bell, on four pads and keys.

    It is a hand drum rather than a drum kit, so it sounds on four pads and
    keeps its own key table (``drum_keyconfig.KIT_BINDINGS``). Its samples are
    **true stereo** -- hi leans left, lo leans right, the slap sits centred --
    because the engine has no per-hit pan: a kit's image is the file's own (the
    local hit plays the file with ``direct_channels``, the remote one plays its
    split L/R). The samples arrived dual-mono, which is heard as one point in the
    middle however it is routed, so the pan is baked into the files by
    ``tools/percussion_pan_bake.py`` and this is what says so.
    """

    SOUNDING = {0: "Per lo", 1: "Per Hi", 16: "Cabell", 17: "Per P"}

    def kit_root(self):
        return os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")

    def test_the_kit_sounds_only_the_pads_it_has(self):
        pads = DrumAudio.KITS["per"]
        self.assertEqual(
            len(pads), len(DrumAudio.PAD_DEFS),
            "every kit must define the canonical pad count")
        sounding = {index: pad[0] for index, pad in enumerate(pads) if pad[1]}
        self.assertEqual(sounding, self.SOUNDING)

    def test_every_sounding_pad_points_at_a_real_file(self):
        root = self.kit_root()
        for index, name in self.SOUNDING.items():
            path = DrumAudio.KITS["per"][index][1]
            full = os.path.join(root, "data", path.replace("/", os.sep))
            self.assertTrue(os.path.exists(full), f"pad {index} ({name}): {full}")

    def test_the_leaning_samples_are_true_stereo_not_dual_mono(self):
        """Side channel at digital silence is the bug this kit was reported as."""
        import audioop
        import math
        from libs.safe_vorbis import load_vorbis_pcm

        root = self.kit_root()
        for index in (0, 1):      # lo leans right, hi leans left; P is centred
            path = DrumAudio.KITS["per"][index][1]
            relative = os.path.relpath(
                os.path.join(root, "data", path.replace("/", os.sep)))
            pcm = load_vorbis_pcm(relative, max_pcm_bytes=32 * 1024 * 1024)
            self.assertEqual(pcm.channels, 2)
            left = audioop.tomono(pcm.buffer, 2, 1.0, 0.0)
            right = audioop.tomono(pcm.buffer, 2, 0.0, 1.0)
            side = audioop.add(left, audioop.mul(right, 2, -1.0), 2)
            side_db = 20 * math.log10(max(audioop.rms(side, 2), 1) / 32768.0)
            self.assertGreater(
                side_db, -60.0,
                f"pad {index} is dual-mono (side at {side_db:.1f} dBFS), so a "
                "listener hears it centred however the room routes it")

    def test_the_scales_sit_the_pieces_at_one_level(self):
        """The pieces must read as one instrument, not one quiet piece.

        Measured by ``tools/percussion_pan_bake.py``; the values are stored in
        the kit because the engine applies them per hit. The bell is shorter and
        peakier than the strokes, so its scale is the loudness-matched 0.75 that
        still leaves the loudest hit 3.1 dB under full scale.
        """
        scales = {index: DrumAudio.KITS["per"][index][2] for index in self.SOUNDING}
        self.assertGreater(scales[0], 0.0)
        self.assertGreater(scales[1], 0.0)
        self.assertGreaterEqual(scales[16], 0.0)
        self.assertLessEqual(scales[16], 1.0,
                             "the bell is peaky; a scale past 1 clips it")
        self.assertGreaterEqual(scales[17], 1.0,
                                "the slap lost ~9.5 LU to its longer tail")


class PerTunedKitTests(unittest.TestCase):
    """The tuned per drum: five notes of one stroke, same length, same drum.

    OpenAL's source pitch is varispeed -- a fifth down makes a 1.6 s hit 2.1 s
    long -- so the notes are baked files instead (``tools/percussion_tune_bake.py``
    stretches time, then resamples). These tests pin what that buys: every note is
    the source's own length, and the five notes rise in order with the pentatonic's
    own span between A3 and G4 (a fifth up plus a tone, ~17.8 semitones).
    """

    NOTES = ((0, "Per A3"), (1, "Per C4"), (2, "Per D4"), (3, "Per E4"),
             (4, "Per G4"), (5, "Per lo"), (6, "Per P"))
    SOURCE = "drums/per/per Hi.ogg"

    def root(self):
        return os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")

    def decode(self, path):
        from libs.safe_vorbis import load_vorbis_pcm

        full = os.path.join(self.root(), "data", path.replace("/", os.sep))
        pcm = load_vorbis_pcm(os.path.relpath(full), max_pcm_bytes=32 * 1024 * 1024)
        return pcm

    def test_the_kit_sounds_the_five_notes_and_two_rhythm_pieces(self):
        pads = DrumAudio.KITS["per_tuned"]
        self.assertEqual(len(pads), len(DrumAudio.PAD_DEFS))
        sounding = {index: pad[0] for index, pad in enumerate(pads) if pad[1]}
        self.assertEqual(sounding, dict(self.NOTES))

    def test_every_sounding_pad_points_at_a_real_file(self):
        root = self.root()
        for index, _name in self.NOTES:
            path = DrumAudio.KITS["per_tuned"][index][1]
            full = os.path.join(root, "data", path.replace("/", os.sep))
            self.assertTrue(os.path.exists(full), f"pad {index}: {full}")

    def test_every_note_is_the_source_strokes_own_length(self):
        source = self.decode(self.SOURCE)
        source_seconds = len(source.buffer) / (2 * source.channels) / source.frequency
        for index in range(5):
            pcm = self.decode(DrumAudio.KITS["per_tuned"][index][1])
            seconds = len(pcm.buffer) / (2 * pcm.channels) / pcm.frequency
            self.assertAlmostEqual(
                seconds, source_seconds, delta=0.02,
                msg=f"pad {index} is {seconds:.3f}s of a {source_seconds:.3f}s "
                    "stroke: the pitch change must not change the length")

    def test_the_five_notes_rise_in_order(self):
        import numpy as np

        centroids = []
        for index in range(5):
            pcm = self.decode(DrumAudio.KITS["per_tuned"][index][1])
            samples = np.frombuffer(pcm.buffer, dtype=np.int16)
            mono = samples.reshape(-1, pcm.channels).mean(axis=1)[:8192]
            spectrum = np.abs(np.fft.rfft(mono * np.hanning(len(mono))))
            freqs = np.fft.rfftfreq(len(mono), 1.0 / pcm.frequency)
            centroids.append(float(np.sum(freqs * spectrum) / np.sum(spectrum)))
        self.assertEqual(centroids, sorted(centroids),
                         "A3 C4 D4 E4 G4 must rise in pitch")
        span_cents = 1200.0 * np.log2(centroids[-1] / centroids[0])
        self.assertAlmostEqual(
            span_cents, 1200.0 * np.log2(392.0 / 220.0), delta=250.0,
            msg=f"the A3..G4 span measures {span_cents:.0f} cents")

    def test_its_keys_are_the_number_row_one_to_seven(self):
        table = drum_keyconfig.bindings("per_tuned")
        self.assertEqual([binding.pad for binding in table],
                         [index for index, _name in self.NOTES])
        self.assertEqual([binding.default_key for binding in table],
                         [pygame.K_1, pygame.K_2, pygame.K_3, pygame.K_4,
                          pygame.K_5, pygame.K_6, pygame.K_7])


class PerKeyBindingTests(unittest.TestCase):
    """A kit that brings its own keys stores and validates only its own."""

    def test_the_per_kit_brings_its_own_keys(self):
        table = drum_keyconfig.bindings("per")
        self.assertEqual([binding.pad for binding in table], [0, 1, 17, 16])
        self.assertEqual(
            [binding.function for binding in table],
            ["drum_per_lo", "drum_per_hi", "drum_per_p", "drum_per_cabell"])
        self.assertEqual(
            [binding.default_key for binding in table],
            [pygame.K_3, pygame.K_1, pygame.K_2, pygame.K_4],
            "1 is the left-hand treble, 2 the slap, 3 the right-hand bass, 4 the bell")
        self.assertIs(drum_keyconfig.bindings("default"),
                      drum_keyconfig.DRUM_BINDINGS)
        self.assertIs(drum_keyconfig.bindings(None), drum_keyconfig.DRUM_BINDINGS)

    def test_a_per_binding_does_not_move_the_drum_kits_key(self):
        keyconfig = FakeKeyconfig({"drum_per_hi": pygame.K_a})
        drums = drum_keyconfig.key_to_pad(keyconfig)
        self.assertEqual(drums[pygame.K_f], 1)
        self.assertNotIn(pygame.K_a, drums)
        per = drum_keyconfig.key_to_pad(keyconfig, kit="per")
        self.assertEqual(per[pygame.K_a], 1)
        self.assertNotIn(pygame.K_f, per)

    def test_a_key_the_drum_kit_uses_is_free_in_the_per_kit(self):
        """The two tables are never active together, so Space may be either's."""
        keyconfig = FakeKeyconfig()
        self.assertIsNone(drum_keyconfig.validate_key(
            keyconfig, "drum_per_lo", pygame.K_SPACE, kit="per"))
        self.assertIsNone(drum_keyconfig.validate_key(
            keyconfig, "drum_kick", pygame.K_SPACE))

    def test_the_conflict_check_is_scoped_to_the_one_kit(self):
        error = drum_keyconfig.validate_key(
            FakeKeyconfig(), "drum_per_lo", pygame.K_1, kit="per")
        self.assertIn("Per Hi primary", error)
        self.assertIsNone(drum_keyconfig.validate_key(
            FakeKeyconfig(), "drum_per_lo", pygame.K_j, kit="per"),
            "J plays a hi-hat in the drum kit, which is a different table")
        self.assertIn("Closed Hi-Hat primary", drum_keyconfig.validate_key(
            FakeKeyconfig(), "drum_kick", pygame.K_j))

    def test_clearing_the_keys_clears_every_kit_in_one_write(self):
        keyconfig = FakeKeyconfig({"drum_per_hi": pygame.K_a})
        drum_keyconfig.clear_all(keyconfig)
        self.assertEqual(keyconfig.save_count, 1)
        self.assertEqual(drum_keyconfig.key_to_pad(keyconfig), {})
        self.assertEqual(drum_keyconfig.key_to_pad(keyconfig, kit="per"), {})
        self.assertTrue(all(
            keyconfig.keys[binding.function] is None
            for binding in drum_keyconfig.bindings("per")))

    def test_restoring_defaults_reaches_the_per_keys_too(self):
        keyconfig = FakeKeyconfig({
            "drum_per_hi": pygame.K_a,
            "drum_per_hi_alt": pygame.K_b,
        })
        drum_keyconfig.restore_defaults(keyconfig)
        self.assertEqual(keyconfig.save_count, 1)
        self.assertEqual(
            drum_keyconfig.key_to_pad(keyconfig, kit="per"),
            {pygame.K_3: 0, pygame.K_1: 1, pygame.K_2: 17, pygame.K_4: 16})
        self.assertNotIn("drum_per_hi_alt", keyconfig.keys)

    def test_the_handler_asks_the_kit_being_played(self):
        from types import SimpleNamespace
        from libs.drum_handler import DrumHandler

        game = SimpleNamespace(
            keyconfig=FakeKeyconfig({"drum_per_hi": pygame.K_a}),
            audio_mngr=SimpleNamespace(
                drums=SimpleNamespace(active_kit="per")),
        )
        handler = DrumHandler(SimpleNamespace(game=game))
        resolved = handler.get_key_to_pad()
        self.assertEqual(resolved[pygame.K_a], 1, "the per kit's own F binding")
        self.assertEqual(resolved[pygame.K_3], 0, "the bass stroke moved to 3")
        self.assertNotIn(pygame.K_f, resolved,
                         "the drum kit's snare key is not a per key")

        game.audio_mngr.drums.active_kit = "default"
        resolved = handler.get_key_to_pad()
        self.assertEqual(resolved[pygame.K_f], 1)
        self.assertNotIn(pygame.K_a, resolved)


class DrumMidiCoverageTests(unittest.TestCase):
    """Every pad the kits define has to be reachable from a MIDI controller.

    Adding a pad to the kits is a change nothing reminds the MIDI profile to
    follow: the chromatic span stopped at pad 16, so a MIDI drummer could not
    reach pad 17 (the dedicated Rim) with any note -- it stayed a keyboard-only
    pad. These read the pad count out of the kits themselves.
    """

    def _reachable_pads(self):
        profile = DRUM_MIDI_PROFILE
        pads = set()
        for note in range(0, 128):
            pad = profile.note_to_pad(note)
            if pad is not None:
                pads.add(pad)
        return pads

    def test_every_pad_the_kits_define_is_reachable(self):
        pad_count = len(DrumAudio.KITS[DrumAudio.DEFAULT_KIT])
        reachable = self._reachable_pads()
        for pad in range(pad_count):
            self.assertIn(
                pad, reachable,
                f"pad {pad} cannot be played by any MIDI note, so a MIDI "
                "drummer can never reach it")
        self.assertNotIn(-1, reachable)

    def test_the_chromatic_span_covers_exactly_the_pads_the_kits_define(self):
        profile = DRUM_MIDI_PROFILE
        span = profile.CHROMATIC_LAST_NOTE - profile.CHROMATIC_FIRST_NOTE + 1
        self.assertEqual(
            span, len(DrumAudio.KITS[DrumAudio.DEFAULT_KIT]),
            "the chromatic span no longer hands out one note per pad, so a new "
            "pad is unreachable (or a note plays a pad that no longer exists)")
        self.assertEqual(profile.note_to_pad(profile.CHROMATIC_FIRST_NOTE), 0)
        self.assertEqual(
            profile.note_to_pad(profile.CHROMATIC_LAST_NOTE), span - 1)

    def test_every_key_binding_and_every_midi_note_name_the_same_pad(self):
        """The pad a key plays and the pad a MIDI note plays are one contract."""
        for binding in drum_keyconfig.DRUM_BINDINGS:
            self.assertLess(
                binding.pad, len(DrumAudio.KITS[DrumAudio.DEFAULT_KIT]),
                f"the {binding.label!r} key plays pad {binding.pad}, which no "
                "kit sounds")
            for kit_name, pads in DrumAudio.KITS.items():
                self.assertLess(
                    binding.pad, len(pads),
                    f"the {binding.label!r} key plays pad {binding.pad}, which "
                    f"kit {kit_name!r} does not define")


class HongKongKitTests(unittest.TestCase):
    """The Hong Kong set: a whole acoustic kit that arrived as one folder.

    Its 18 files were recorded with their own names (``kick.ogg``, ``h ope.ogg``,
    ``cabell.ogg``) rather than the default kit's ``Drums.hit.<Name>.ogg`` scheme,
    so nothing in the folder says which pad a file belongs to -- the registry
    does. A pad whose path is wrong is a piece of the kit that is silent with no
    error anywhere, and a file no pad names is a piece nobody can hit, which is
    why both halves are checked here rather than just the file names.

    ``ride.ogg`` is also why the scales are peak-measured: it measures -49 LUFS
    beside a -11.5 dBTP peak (a long wash tail), so a loudness-derived scale
    would have asked for 11 and clipped by 9 dB.
    """

    KIT = "hongkong"

    def kit_root(self):
        return os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")

    def kit_folder(self):
        return os.path.join(self.kit_root(), "data", "drums", "HongkongDrum Set")

    def test_the_kit_is_a_complete_drum_kit(self):
        pads = DrumAudio.KITS[self.KIT]
        self.assertEqual(len(pads), len(DrumAudio.PAD_DEFS))
        missing = [index for index, pad in enumerate(pads) if pad[1] is None]
        self.assertEqual(missing, [],
                         "this set ships every piece, so no pad may be silent")
        self.assertEqual(
            DrumAudio.KITS[self.KIT][2][0], "Rim (Snare 2)",
            "the set's second snare is what pad 2 plays")
        self.assertEqual(DrumAudio.KITS[self.KIT][17][0], "Rim")

    def test_every_pad_points_at_a_real_file(self):
        root = self.kit_root()
        for index, (name, path, scale, polyphony) in enumerate(
                DrumAudio.KITS[self.KIT]):
            full = os.path.join(root, "data", path.replace("/", os.sep))
            self.assertTrue(os.path.exists(full), f"pad {index} ({name}): {full}")
            self.assertGreater(scale, 0.0, f"pad {index} would play silence")
            self.assertGreaterEqual(polyphony, 1)

    def test_no_single_hit_reaches_full_scale_and_none_is_left_quiet(self):
        """The set sits at full scale on purpose, and never past it.

        It first shipped 2.9 dB lower and the owner heard the whole kit as "a
        little quiet"; the raise stops where a single hit stops, because the
        price of going further is clipping -- which is distortion, not volume.
        The engine plays a hit at gain 3.0 (`volume 300`), so the promise is
        the file's peak plus its scale plus 9.54 dB, and a pad below the
        ceiling by a decibel is the quiet piece report all over again.

        The floor is -2 dB rather than 0 because this reads the *sample* peak:
        the open hi-hat's attack overshoots its samples by 1.45 dB once the
        signal is reconstructed (`ffmpeg`'s true peak, which is what the
        scales were set from), and oversampled peaks are the higher of the
        two. A whole set left 3 dB quiet -- the state this was reported as --
        fails the floor, which is what it is for.
        """
        import audioop
        import math
        from libs.safe_vorbis import load_vorbis_pcm

        root = self.kit_root()
        for index, (name, path, scale, _polyphony) in enumerate(
                DrumAudio.KITS[self.KIT]):
            relative = os.path.relpath(
                os.path.join(root, "data", path.replace("/", os.sep)))
            pcm = load_vorbis_pcm(relative, max_pcm_bytes=32 * 1024 * 1024)
            peak = audioop.max(pcm.buffer, 2) / 32768.0
            hit_db = 20 * math.log10(peak) + 20 * math.log10(scale) + 9.542
            self.assertLessEqual(
                hit_db, 0.0,
                f"pad {index} ({name}) peaks at {hit_db:+.2f} dBFS at volume "
                "300: one hit is already clipping")
            self.assertGreater(
                hit_db, -2.0,
                f"pad {index} ({name}) sits {hit_db:+.2f} dBFS, well under the "
                "ceiling the rest of the set was raised to")

    def test_the_folder_holds_no_file_no_pad_plays(self):
        named = {os.path.basename(path)
                 for _, path, _, _ in DrumAudio.KITS[self.KIT]}
        on_disk = {name for name in os.listdir(self.kit_folder())
                   if name.lower().endswith(".ogg")}
        self.assertEqual(sorted(on_disk - named), [],
                         "a sample in the kit folder that no pad names is a "
                         "piece of the set nobody can hit")
        self.assertEqual(sorted(named - on_disk), [])


if __name__ == "__main__":
    unittest.main()
