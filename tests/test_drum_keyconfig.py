import unittest

import pygame

from libs import drum_keyconfig
from libs.midi.profiles import DRUM_MIDI_PROFILE


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
        self.assertEqual(len(resolved), 17)
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


if __name__ == "__main__":
    unittest.main()
