import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest import mock

from libs.game_audio_recorder import GameAudioRecorderManager


class _ImmediateGame:
    def put(self, callback):
        callback()


class _FakeBackend:
    instances = []

    def __init__(self, process_id):
        self.process_id = process_id
        self.__class__.instances.append(self)

    def record(self, output_path, stop_event, started):
        self.output_path = output_path
        started()
        if not stop_event.wait(2):
            raise AssertionError("test recorder was not stopped")
        return 48_000, 1.0


def _wait_for(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


class GameAudioRecorderManagerTests(unittest.TestCase):
    def setUp(self):
        _FakeBackend.instances.clear()
        self.messages = []
        self.manager = GameAudioRecorderManager(
            _ImmediateGame(),
            lambda: None,
            backend_factory=_FakeBackend,
            countdown_interval=0,
        )
        self.manager._announce = self.messages.append

    def tearDown(self):
        self.manager.close()

    def test_countdown_starts_then_menu_stop_finishes_recording(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "recording.wav"
            self.assertTrue(self.manager.start_to_path(output))
            self.assertTrue(_wait_for(lambda: self.manager.state() == "recording"))
            self.assertEqual(self.manager.menu_label(), "Stop Recording")
            self.assertEqual(_FakeBackend.instances[0].process_id, os.getpid())

            self.manager.menu_action()
            self.assertTrue(_wait_for(lambda: self.manager.state() == "idle"))

        self.assertEqual(self.messages[:4], ["Ready, 3.", "2.", "1.", "Recording."])
        self.assertIn("Stopping recording.", self.messages)
        self.assertTrue(any(message.startswith("Recording saved.") for message in self.messages))

    def test_stop_during_countdown_cancels_without_opening_backend(self):
        manager = GameAudioRecorderManager(
            _ImmediateGame(),
            lambda: None,
            backend_factory=_FakeBackend,
            countdown_interval=0.25,
        )
        messages = []
        manager._announce = messages.append
        try:
            self.assertTrue(manager.start_to_path("unused.wav"))
            self.assertEqual(manager.state(), "countdown")
            manager.stop()
            self.assertTrue(_wait_for(lambda: manager.state() == "idle"))
            self.assertEqual(_FakeBackend.instances, [])
            self.assertIn("Recording setup cancelled.", messages)
        finally:
            manager.close()

    def test_dynamic_menu_uses_separate_recording_action(self):
        source = (Path(__file__).resolve().parents[1] / "libs" / "music_bot.py").read_text(encoding="utf-8")
        self.assertIn("(\"Record Audio\", go_record_audio)", source)
        self.assertIn("def _open_recording_menu(self):", source)
        self.assertIn("(\"Set Recording Folder\", self.audio_recorder.choose_folder)", source)
        self.assertIn("(self.audio_recorder.menu_label, start_or_stop)", source)
        self.assertIn("(\"Open Recording Folder\", self.audio_recorder.open_folder)", source)
        self.assertIn("self.audio_recorder.menu_action()", source)
        self.assertIn("self.audio_recorder.close()", source)

    def test_saved_folder_starts_immediately_with_timestamped_filename(self):
        with tempfile.TemporaryDirectory() as temp, \
                mock.patch("libs.game_audio_recorder.options.get", return_value=temp), \
                mock.patch("libs.game_audio_recorder.process_loopback_supported", return_value=True):
            self.manager.request_start()
            self.assertTrue(_wait_for(lambda: self.manager.state() == "recording"))
            output = Path(_FakeBackend.instances[0].output_path)
            self.assertEqual(output.parent, Path(temp))
            self.assertTrue(output.name.startswith("Beyond Tournament Recording "))
            self.assertEqual(output.suffix, ".wav")
            self.manager.stop()
            self.assertTrue(_wait_for(lambda: self.manager.state() == "idle"))

    def test_folder_status_and_collision_safe_filename(self):
        with tempfile.TemporaryDirectory() as temp, \
                mock.patch("libs.game_audio_recorder.options.get", return_value=temp):
            self.assertEqual(self.manager.folder_menu_label(), f"Recording folder: {temp}")
            first = Path(self.manager._next_recording_path(temp))
            first.touch()
            second = Path(self.manager._next_recording_path(temp))
            self.assertNotEqual(first, second)
            self.assertIn("(2)", second.stem)

    def test_first_save_as_choice_is_remembered_then_recorded(self):
        with tempfile.TemporaryDirectory() as temp, \
                mock.patch("libs.game_audio_recorder.options.set") as save_option:
            output = Path(temp) / "chosen.wav"
            self.manager._state = "selecting"
            self.manager._accept_selected_path(str(output))
            self.assertTrue(_wait_for(lambda: self.manager.state() == "recording"))
            save_option.assert_called_once_with("music_bot_recording_folder", temp)
            self.assertEqual(Path(_FakeBackend.instances[0].output_path), output)
            self.manager.stop()
            self.assertTrue(_wait_for(lambda: self.manager.state() == "idle"))

    def test_start_waits_while_folder_picker_is_open(self):
        with mock.patch("libs.game_audio_recorder.process_loopback_supported", return_value=True):
            self.manager._configuring_folder = True
            self.manager.request_start()
        self.assertEqual(self.manager.state(), "idle")
        self.assertEqual(_FakeBackend.instances, [])
        self.assertIn("Finish selecting the recording folder first.", self.messages)


if __name__ == "__main__":
    unittest.main()
