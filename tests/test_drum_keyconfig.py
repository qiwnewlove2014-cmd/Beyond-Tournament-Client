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
        for binding in drum_keyconfig.DRUM_BINDINGS:
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


if __name__ == "__main__":
    unittest.main()
