"""Instrument input regressions without audio, MIDI devices, files, or a server."""

import ast
from collections import defaultdict
from pathlib import Path
import re
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock


LIBS = Path(__file__).resolve().parents[1] / "libs"


class FakeSamples:
    def __init__(self):
        self.requested = []
        self.ready = set()
        self.failed = set()

    def request(self, paths):
        self.requested.append(tuple(paths))

    def status(self, paths):
        if any(path in self.failed for path in paths):
            return "failed"
        return "ready" if all(path in self.ready for path in paths) else "loading"

    def get(self, path, kind="stereo"):
        return object() if path in self.ready else None


class FakeDrums:
    def __init__(self, calls):
        self._active_kit = "default"
        self.calls = calls
        self.preload = Mock(side_effect=AssertionError("input must not bulk preload"))

    @staticmethod
    def is_valid_kit(kit):
        return kit in ("default", "salamander")

    def set_active_kit(self, kit):
        self._active_kit = kit

    @staticmethod
    def is_valid_pad(pad):
        return type(pad) is int and 0 <= pad < 18

    @staticmethod
    def pad_defs(kit):
        return tuple(
            (str(pad), None if kit == "salamander" and pad == 9
             else f"drums/{kit}/Drums.hit.{pad}.ogg", 1.0, None)
            for pad in range(18)
        )

    def play_hit(self, peer, pad, *args, **kwargs):
        self.calls.append(("audio", pad))
        return None


def build_handler(filename, class_name, speech, pressed):
    """Run the actual handler class with pure owner/engine fakes."""
    tree = ast.parse((LIBS / filename).read_text(encoding="utf-8"))
    class_node = next(node for node in tree.body if isinstance(node, ast.ClassDef))
    pygame_names = {
        node.attr for node in ast.walk(class_node)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name) and node.value.id == "pygame"
        and (node.attr.startswith("K_") or node.attr in ("KEYDOWN", "KEYUP"))
    }
    pygame_names.add("K_f")  # A configurable drum key is not hardcoded in the handler.
    pygame = SimpleNamespace(**{
        name: index for index, name in enumerate(sorted(pygame_names), start=1)
    })
    pygame.error = RuntimeError
    pygame.key = SimpleNamespace(get_pressed=lambda: pressed)
    profile = SimpleNamespace(MIN_NOTE=24, MAX_NOTE=95, volume=lambda value: value + 100)
    namespace = {
        "pygame": pygame, "speak": speech.append, "re": re, "time": time,
        "consts": SimpleNamespace(CHANNEL_MAP=4),
        "PIANO_MIDI_PROFILE": profile, "DRUM_MIDI_PROFILE": profile,
        "drum_keyconfig": SimpleNamespace(key_to_pad=lambda config: config),
    }
    exec(compile(ast.Module(body=[class_node], type_ignores=[]), filename, "exec"), namespace)
    return namespace[class_name], pygame


class InstrumentLoadingInputTests(unittest.TestCase):
    def make_handler(self, kind="piano", held_enter=False):
        speech, calls = [], []
        pressed = defaultdict(bool)
        cls, pygame = build_handler(
            f"{kind}_handler.py", "PianoHandler" if kind == "piano" else "DrumHandler",
            speech, pressed,
        )
        pressed[pygame.K_RETURN] = held_enter
        samples = FakeSamples()
        piano = SimpleNamespace(
            set_soft_pedal=Mock(), set_chorus=Mock(), set_pitch_bend=Mock(),
            stop_note=Mock(),
            play_note=lambda peer, note, *args, **kwargs: calls.append(("audio", note)),
        )
        drums = FakeDrums(calls)
        game = SimpleNamespace(
            audio_mngr=SimpleNamespace(
                instrument_samples=samples, piano=piano, drums=drums,
                load_buffer=Mock(side_effect=AssertionError("blocking decode")),
            ),
            network=SimpleNamespace(send=Mock()),
            midi_hub=SimpleNamespace(
                acquire=Mock(return_value=SimpleNamespace(
                    profile_id="piano" if kind == "piano" else "drumset")),
                release=Mock(), poll=Mock(),
            ),
            keyconfig={pygame.K_f: 1} if kind == "drum" else {},
        )
        owner = SimpleNamespace(
            game=game, piano_mode=False, drum_mode=False, _midi_lease=None,
            player=SimpleNamespace(x=1, y=2, z=3), map=None,
            _is_megaphone_owner=lambda: False,
            _attach_music_timeline=lambda packet: packet,
            _send_jam_note=lambda event, packet: calls.append((event, dict(packet))),
        )
        handler = cls(owner)
        return SimpleNamespace(
            handler=handler, pygame=pygame, speech=speech, calls=calls,
            pressed=pressed, samples=samples, game=game, owner=owner,
        )

    @staticmethod
    def key(fixture, key, down=True, repeat=False):
        return fixture.handler.handle_event(SimpleNamespace(
            type=fixture.pygame.KEYDOWN if down else fixture.pygame.KEYUP,
            key=key, repeat=repeat,
        ))

    def test_piano_start_requests_current_complete_transposed_map(self):
        f = self.make_handler()
        f.handler.octave = 5
        f.handler.transpose = 2
        expected = {
            f"piano/Piano.mf.{f.handler._apply_transpose(note)}.ogg"
            for note in f.handler.get_key_to_note().values()
        }
        f.handler.start()
        self.assertEqual(set(f.samples.requested[0]), expected)
        self.assertGreater(len(expected), 18)
        self.assertEqual(f.speech, [])
        f.game.audio_mngr.load_buffer.assert_not_called()
        f.game.midi_hub.acquire.assert_called_once_with(f.owner, "piano")

    def test_preparation_is_silent_but_failure_is_announced_once(self):
        for kind in ("piano", "drum"):
            with self.subTest(kind=kind):
                f = self.make_handler(kind)
                f.handler.start()
                f.handler.poll()
                self.assertEqual(f.speech, [])
                self.assertEqual(f.handler._sample_status, "loading")
                f.samples.ready.update(f.handler._sample_paths)
                f.handler.poll()
                f.handler.poll()
                self.assertEqual(f.speech, [])
                self.assertEqual(f.handler._sample_status, "ready")
                f.samples.failed.add(f.handler._sample_paths[0])
                f.handler.poll()
                f.handler.poll()
                self.assertEqual(len(f.speech), 1)
                self.assertIn("Check the game sound files", f.speech[-1])

    def test_cached_reentry_does_not_speak_over_control_instructions(self):
        for kind in ("piano", "drum"):
            with self.subTest(kind=kind):
                f = self.make_handler(kind)
                f.handler.start()
                f.samples.ready.update(f.handler._sample_paths)
                f.handler.stop()
                f.speech.clear()
                f.handler.start()
                f.handler.poll()
                self.assertEqual(f.handler._sample_status, "ready")
                self.assertEqual(f.speech, [])
                self.assertTrue(f.handler.active)

    def test_cancelled_preparation_does_not_announce_late_failure(self):
        for kind in ("piano", "drum"):
            with self.subTest(kind=kind):
                f = self.make_handler(kind)
                f.handler.start()
                requested = f.handler._sample_paths
                self.key(f, f.pygame.K_ESCAPE)
                f.samples.failed.add(requested[0])
                f.handler.poll()
                self.assertEqual(f.speech, [])
                self.assertFalse(f.handler.active)

    def test_escape_cancels_loading_and_late_ready_does_not_reactivate(self):
        for kind in ("piano", "drum"):
            with self.subTest(kind=kind):
                f = self.make_handler(kind, held_enter=True)
                f.handler.start()
                requested = f.handler._sample_paths
                self.key(f, f.pygame.K_ESCAPE)
                f.samples.ready.update(requested)
                f.handler.poll()
                self.assertFalse(f.handler.active)
                self.assertEqual(f.handler._sample_paths, ())
                self.assertEqual(f.speech, [])
                f.game.midi_hub.release.assert_called_once()
                f.game.midi_hub.poll.assert_not_called()

    def test_held_enter_repeat_is_ignored_until_release(self):
        for kind in ("piano", "drum"):
            with self.subTest(kind=kind):
                f = self.make_handler(kind, held_enter=True)
                f.handler.start()
                self.key(f, f.pygame.K_RETURN)
                self.key(f, f.pygame.K_RETURN, repeat=True)
                self.assertTrue(f.handler.active)
                self.key(f, f.pygame.K_RETURN, down=False)
                self.key(f, f.pygame.K_RETURN)
                self.assertFalse(f.handler.active)

    def test_already_released_enter_can_exit_immediately_without_timer(self):
        for kind in ("piano", "drum"):
            with self.subTest(kind=kind):
                f = self.make_handler(kind)
                f.handler.start()
                self.key(f, f.pygame.K_RETURN, repeat=True)
                self.assertTrue(f.handler.active)
                self.key(f, f.pygame.K_RETURN)
                self.assertFalse(f.handler.active)

    def test_keypad_enter_uses_the_same_release_guard(self):
        for kind in ("piano", "drum"):
            with self.subTest(kind=kind):
                f = self.make_handler(kind)
                f.pressed[f.pygame.K_KP_ENTER] = True
                f.handler.start()
                self.key(f, f.pygame.K_KP_ENTER)
                self.assertTrue(f.handler.active)
                self.key(f, f.pygame.K_KP_ENTER, down=False)
                self.key(f, f.pygame.K_KP_ENTER)
                self.assertFalse(f.handler.active)

    def test_uninitialized_keyboard_snapshot_does_not_block_start(self):
        for kind in ("piano", "drum"):
            with self.subTest(kind=kind):
                f = self.make_handler(kind)
                f.pygame.key.get_pressed = Mock(side_effect=f.pygame.error("not initialized"))
                f.handler.start()
                self.key(f, f.pygame.K_RETURN)
                self.assertFalse(f.handler.active)

    def test_cold_piano_press_is_not_replayed_after_ready_or_on_keyup(self):
        f = self.make_handler()
        f.handler.start()
        self.key(f, f.pygame.K_q)
        self.assertIsNone(f.handler._pressed_notes[f.pygame.K_q])
        f.samples.ready.update(f.handler._sample_paths)
        f.handler.poll()
        self.key(f, f.pygame.K_q, repeat=True)
        self.key(f, f.pygame.K_q, down=False)
        self.assertEqual(f.calls, [])
        f.game.audio_mngr.piano.stop_note.assert_not_called()
        self.key(f, f.pygame.K_q)
        self.assertEqual(f.calls, [("audio", "C4"), ("play_piano_note", {"note": "C4"})])
        self.key(f, f.pygame.K_q, down=False)
        f.game.audio_mngr.piano.stop_note.assert_called_once_with("local", "C4")

    def test_ready_note_can_play_while_other_piano_samples_are_loading(self):
        f = self.make_handler()
        f.handler.start()
        f.samples.ready.add("piano/Piano.mf.C4.ogg")
        self.assertTrue(f.handler.play_local_note("C4", velocity=70))
        self.assertEqual(f.handler._sample_status, "loading")
        self.assertEqual(f.calls[-1], ("play_piano_note", {"note": "C4", "velocity": 70}))

    def test_cold_midi_note_outside_keymap_is_requested_not_replayed(self):
        f = self.make_handler()
        f.handler.start()
        f.samples.ready.update(f.handler._sample_paths)
        f.handler.poll()
        self.assertFalse(f.handler.play_local_note("C1", velocity=90))
        self.assertIn("piano/Piano.mf.C1.ogg", f.handler._sample_paths)
        f.samples.ready.add("piano/Piano.mf.C1.ogg")
        f.handler.poll()
        self.assertEqual(f.calls, [])
        self.assertTrue(f.handler.play_local_note("C1", velocity=90))

    def test_multiple_cold_midi_notes_load_without_speech(self):
        f = self.make_handler()
        f.handler.start()
        for note in ("C1", "Db1", "D1", "Eb1"):
            self.assertFalse(f.handler.play_local_note(note, velocity=90))
        self.assertEqual(f.speech, [])

    def test_octave_and_transpose_request_full_updated_map_without_decode(self):
        f = self.make_handler()
        f.handler.start()
        old_paths = f.handler._sample_paths
        self.key(f, f.pygame.K_RIGHT)
        self.assertNotEqual(f.handler._sample_paths, old_paths)
        expected = {
            f"piano/Piano.mf.{f.handler._apply_transpose(note)}.ogg"
            for note in f.handler.get_key_to_note().values()
        }
        self.assertEqual(set(f.samples.requested[-1]), expected)
        self.key(f, f.pygame.K_F2)
        expected = {
            f"piano/Piano.mf.{f.handler._apply_transpose(note)}.ogg"
            for note in f.handler.get_key_to_note().values()
        }
        self.assertEqual(set(f.samples.requested[-1]), expected)
        f.game.audio_mngr.load_buffer.assert_not_called()

    def test_drum_start_uses_current_kit_and_skips_silent_pads(self):
        f = self.make_handler("drum")
        f.handler.start(kit="salamander")
        self.assertEqual(len(f.handler._sample_paths), 17)
        self.assertTrue(all("/salamander/" in path for path in f.handler._sample_paths))
        self.assertEqual(len(f.samples.requested), 1)
        f.game.audio_mngr.drums.preload.assert_not_called()
        self.assertFalse(f.handler.play_local_hit(9))
        self.assertFalse(f.handler.play_local_hit(True))
        self.assertFalse(f.handler.play_local_hit(999))
        self.assertEqual(f.calls, [])

    def test_cold_drum_press_never_replays_and_fresh_ready_hit_replicates(self):
        f = self.make_handler("drum")
        f.handler.start()
        self.key(f, f.pygame.K_f)
        f.samples.ready.update(f.handler._sample_paths)
        f.handler.poll()
        self.key(f, f.pygame.K_f, repeat=True)
        self.key(f, f.pygame.K_f, down=False)
        self.assertEqual(f.calls, [])
        self.key(f, f.pygame.K_f)
        self.assertEqual(f.calls, [("audio", 1), ("play_drum_hit", {"pad": 1, "velocity": 127})])

    def test_inactive_handlers_drop_late_midi_actions(self):
        f = self.make_handler()
        self.assertFalse(f.handler.play_local_note("C4"))
        self.assertEqual(f.samples.requested, [])
        self.assertEqual(f.calls, [])
        f = self.make_handler("drum")
        self.assertFalse(f.handler.play_local_hit(1))
        self.assertEqual(f.samples.requested, [])
        self.assertEqual(f.calls, [])

    def test_cancel_then_new_session_tracks_new_mapping_without_speech(self):
        f = self.make_handler()
        f.handler.start()
        previous = f.handler._sample_paths
        f.handler.stop()
        f.handler.octave = 1
        f.handler.start()
        f.samples.ready.update(previous)
        f.handler.poll()
        self.assertEqual(f.handler._sample_status, "loading")
        f.samples.ready.update(f.handler._sample_paths)
        f.handler.poll()
        self.assertEqual(f.handler._sample_status, "ready")
        self.assertEqual(f.speech, [])

    def test_escape_releases_notes_played_during_partial_readiness(self):
        f = self.make_handler()
        f.handler.start()
        f.samples.ready.add("piano/Piano.mf.C4.ogg")
        self.key(f, f.pygame.K_q)
        self.key(f, f.pygame.K_ESCAPE)
        f.game.audio_mngr.piano.stop_note.assert_called_once_with("local", "C4")
        self.assertEqual(f.handler._pressed_notes, {})


if __name__ == "__main__":
    unittest.main()
