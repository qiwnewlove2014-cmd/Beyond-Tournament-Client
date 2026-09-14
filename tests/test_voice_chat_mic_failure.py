"""A dead or unplugged microphone must never escape as a game crash.

Covers the InvalidDeviceError path from crash report af47d137 (player
weerachai): pressing the voice key after the mic handle died used to
propagate into the main-loop recovery and eject the player to the menu.
"""
import threading
import time
import unittest
from types import SimpleNamespace
from unittest import mock

import cyal.exceptions

from libs import gameplay
from libs.voice_chat import VoiceChatRecord


class _DeadMic:
    def start(self):
        raise cyal.exceptions.InvalidDeviceError("Invalid OpenAL device")

    def stop(self):
        raise cyal.exceptions.InvalidDeviceError("Invalid OpenAL device")


class TestVoiceChatStartWithDeadMic(unittest.TestCase):
    def make_gp(self):
        recorder = SimpleNamespace(
            audio_input=_DeadMic(),
            recording=False,
            close=mock.Mock(),
            voice_chat_finish=mock.Mock(),
        )
        gp = object.__new__(gameplay.Gameplay)
        gp.voice_chat = recorder
        gp._default_vc_compression = object()
        gp.pa_test_mode = False
        gp.wmanager = SimpleNamespace(activeWeapon=None)
        return gp, recorder

    def test_start_failure_stays_in_game_and_retires_the_recorder(self):
        gp, recorder = self.make_gp()
        with mock.patch("libs.gameplay.speak") as speak, \
                mock.patch("libs.logger.log"), \
                mock.patch.object(gameplay.options, "get", return_value=True):
            # Must not raise: this used to reach the main-loop recovery.
            gameplay.Gameplay.voice_chat_start(gp, 0)
        self.assertIsNone(gp.voice_chat)
        recorder.close.assert_called_once()
        self.assertFalse(recorder.recording)
        self.assertFalse(gp.voice_chat_using_megaphone)
        speak.assert_called_once_with("Microphone unavailable.")

    def test_stop_failure_still_clears_recording_state(self):
        recorder = SimpleNamespace(
            audio_input=_DeadMic(),
            recording=True,
            voice_chat_finish=mock.Mock(),
        )
        gp = object.__new__(gameplay.Gameplay)
        gp.voice_chat = recorder
        gp.game = SimpleNamespace(
            call_after=mock.Mock(),
            direct_soundgroup=SimpleNamespace(play=mock.Mock()),
        )
        with mock.patch.object(gameplay.options, "get", return_value=True):
            gameplay.Gameplay.voice_chat_stop(gp, 0)  # must not raise
        self.assertFalse(recorder.recording)
        gp.game.call_after.assert_called_once()


class TestCaptureWorkerSurvivesDeadMic(unittest.TestCase):
    def test_dead_mic_mid_recording_stops_recording_without_killing_thread(self):
        class DeadCapture:
            @property
            def available_samples(self):
                raise cyal.exceptions.InvalidDeviceError("Invalid OpenAL device")

        rec = VoiceChatRecord.__new__(VoiceChatRecord)
        rec.running = True
        rec.recording = True
        rec.stereo = False
        rec.audio_input = DeadCapture()
        rec.player = None
        rec.game = None
        worker = threading.Thread(target=rec.run, daemon=True)
        with mock.patch.object(gameplay.options, "get", return_value=True):
            worker.start()
            deadline = time.monotonic() + 2
            while rec.recording and time.monotonic() < deadline:
                time.sleep(0.005)
            rec.running = False
            worker.join(2)
        self.assertFalse(rec.recording)
        self.assertFalse(worker.is_alive())


if __name__ == "__main__":
    unittest.main()
