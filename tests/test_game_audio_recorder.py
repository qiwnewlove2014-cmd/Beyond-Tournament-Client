import math
import os
from array import array
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
import wave
from unittest import mock

from libs import game_audio_recorder as recorder_module
from libs.game_audio_recorder import (
    PROCESS_LOOPBACK_FLOOR_GUIDANCE,
    PROCESS_LOOPBACK_PROBE_GUIDANCE,
    GameAudioRecorderManager,
    MicrophoneOverlayBuffer,
    _MicrophoneMixer,
    _ProcessedMicrophoneOverlay,
    _SegmentedWaveWriter,
    process_loopback_supported,
    process_loopback_unavailable_message,
    wait_for_process_loopback_verdict,
)
from libs.voice_chat import VoiceChatRecord


class _ImmediateGame:
    def put(self, callback):
        callback()


class _FakeBackend:
    instances = []

    def __init__(self, process_id):
        self.process_id = process_id
        self.__class__.instances.append(self)

    def record(self, output_path, stop_event, started, **kwargs):
        self.output_path = output_path
        self.kwargs = kwargs
        started()
        if not stop_event.wait(2):
            raise AssertionError("test recorder was not stopped")
        return 48_000, 1.0, (os.fspath(output_path),)


class _FakeSystemBackend(_FakeBackend):
    instances = []


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
        _FakeSystemBackend.instances.clear()
        self.messages = []
        self.manager = GameAudioRecorderManager(
            _ImmediateGame(),
            lambda: None,
            backend_factory=_FakeBackend,
            system_backend_factory=_FakeSystemBackend,
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
        # The recording pages moved to menu_pages/recording.py on 2026-10-26;
        # the Mode page's line stayed in menus.py, so read the pair as one.
        root = Path(__file__).resolve().parents[1] / "libs" / "music_bot"
        source = ((root / "menus.py").read_text(encoding="utf-8")
                  + (root / "menu_pages" / "recording.py").read_text(encoding="utf-8"))
        # The line names the Server feature it belongs to, so the Music Bot's
        # feature gate can hide it (libs/music_bot/gate.py), and it carries the
        # sentence Tab reads for it (a four-field line).
        self.assertIn("(\"recording\", \"Record Audio\", go_record_audio,", source)
        self.assertIn("def _open_recording_menu(self):", source)
        self.assertIn("self._bot.audio_recorder.choose_folder", source)
        self.assertIn("(self._bot.audio_recorder.menu_label, start_or_stop,", source)
        self.assertIn("(\"Recording Settings\", go_settings,", source)
        self.assertIn("def _open_recording_settings_menu(self):", source)
        self.assertIn("self._bot.audio_recorder.capture_scope_label", source)
        self.assertIn("self._bot.audio_recorder.computer_audio_setting_label", source)
        self.assertIn("self._bot.audio_recorder.microphone_setting_label", source)
        self.assertIn("self._bot.audio_recorder.recorded_voice_label", source)
        self.assertIn("self._bot.audio_recorder.cycle_recorded_voice", source)
        self.assertIn("self._bot.audio_recorder.countdown_setting_label", source)
        self.assertIn("self._bot.audio_recorder.split_setting_label", source)
        self.assertIn("self._bot.audio_recorder.announce_setting_label", source)
        self.assertIn("def _open_recording_reset_confirmation(self):", source)
        self.assertIn("(\"Cancel and Keep Current Settings\", cancel,", source)
        self.assertIn("self._bot.audio_recorder.open_folder", source)
        self.assertIn("self._bot.audio_recorder.menu_action()", source)
        # The recorder is closed by the bot's own shutdown, not by a menu.
        controller = (Path(__file__).resolve().parents[1] / "libs" / "music_bot" / "controller.py").read_text(encoding="utf-8")
        self.assertIn("self.audio_recorder.close()", controller)
        voice_source = (
            Path(__file__).resolve().parents[1] / "libs" / "voice_chat.py"
        ).read_text(encoding="utf-8")
        self.assertIn("audio_recorder.feed_transmitted_microphone(", voice_source)
        self.assertIn("audio_recorder.recorded_voice_is_studio()", voice_source)
        self.assertIn("locally_rendered=bool(voice_using_mega)", voice_source)
        self.assertIn("music_bot = self._find_music_bot(gp)", voice_source)

    def test_saved_folder_starts_immediately_with_timestamped_filename(self):
        with tempfile.TemporaryDirectory() as temp, \
                mock.patch(
                    "libs.game_audio_recorder.options.get",
                    side_effect=lambda key, default=None: (
                        temp if key == "music_bot_recording_folder" else default
                    ),
                ), \
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

    def test_settings_cycle_persist_and_restore_defaults(self):
        stored = {}

        def get_option(key, default=None):
            return stored.get(key, default)

        def set_option(key, value):
            stored[key] = value

        with mock.patch("libs.game_audio_recorder.options.get", side_effect=get_option), \
                mock.patch("libs.game_audio_recorder.options.set", side_effect=set_option):
            self.assertEqual(self.manager.microphone_setting_label(), "Include My Transmitted Voice: Off")
            self.manager.toggle_microphone()
            self.assertEqual(self.manager.microphone_setting_label(), "Include My Transmitted Voice: On")

            self.assertEqual(
                self.manager.computer_audio_setting_label(),
                "Include Screen Reader and Computer Audio: Off",
            )
            self.assertEqual(
                self.manager.capture_scope_label(),
                "Capture Scope: All Audio Rendered by Beyond Tournament",
            )
            self.manager.toggle_computer_audio()
            self.assertEqual(
                self.manager.computer_audio_setting_label(),
                "Include Screen Reader and Computer Audio: On",
            )
            self.assertEqual(
                self.manager.capture_scope_label(),
                "Capture Scope: Default Windows Output, Including Screen Reader",
            )

            self.assertEqual(self.manager.countdown_seconds(), 3)
            self.manager.cycle_countdown()
            self.assertEqual(self.manager.countdown_seconds(), 5)

            self.assertEqual(self.manager.split_minutes(), 0)
            self.manager.cycle_split_minutes()
            self.assertEqual(self.manager.split_minutes(), 30)

            self.assertTrue(self.manager.announce_details())
            self.manager.toggle_announce_details()
            self.assertFalse(self.manager.announce_details())

            self.assertTrue(self.manager.restore_setting_defaults())
            self.assertFalse(self.manager.include_microphone())
            self.assertFalse(self.manager.include_computer_audio())
            self.assertEqual(self.manager.countdown_seconds(), 3)
            self.assertEqual(self.manager.split_minutes(), 0)
            self.assertTrue(self.manager.announce_details())

    def test_fades_ship_off_and_are_restored_to_off(self):
        stored = {}
        with mock.patch("libs.game_audio_recorder.options.get",
                        side_effect=lambda key, default=None: stored.get(key, default)), \
                mock.patch("libs.game_audio_recorder.options.set",
                           side_effect=lambda key, value: stored.__setitem__(key, value)):
            self.assertEqual(self.manager.fade_in_seconds(), 0.0)
            self.assertEqual(self.manager.fade_out_seconds(), 0.0)
            self.assertEqual(self.manager.fade_in_setting_label(), "Fade In at Start: Off")
            self.assertEqual(self.manager.fade_out_setting_label(), "Fade Out at Stop: Off")

            self.manager.set_fade_in(2)
            self.manager.set_fade_out(0.5)
            self.assertEqual(self.manager.fade_in_setting_label(), "Fade In at Start: 2 seconds")
            self.assertEqual(self.manager.fade_out_setting_label(), "Fade Out at Stop: 0.5 seconds")

            # A typed or edited preference is clamped where it is read: the
            # value becomes sample counts, and a fade longer than the take
            # would eat all of it.
            self.manager.set_fade_out(999)
            self.assertEqual(self.manager.fade_out_seconds(), 30.0)
            self.assertIn("30 seconds", self.manager.fade_out_setting_label())

            self.assertTrue(self.manager.restore_setting_defaults())
            self.assertEqual(self.manager.fade_in_seconds(), 0.0)
            self.assertEqual(self.manager.fade_out_seconds(), 0.0)
            self.assertEqual(self.manager.fade_in_setting_label(), "Fade In at Start: Off")

    def test_one_second_is_said_in_the_singular(self):
        self.assertEqual(self.manager.fade_time_text(0), "Off")
        self.assertEqual(self.manager.fade_time_text(1), "1 second")
        self.assertEqual(self.manager.fade_time_text(2), "2 seconds")
        self.assertEqual(self.manager.fade_time_text(0.5), "0.5 seconds")

    def test_recording_snapshots_the_fade_times_it_started_with(self):
        """The fade belongs to the take: changing the setting cannot reach back.

        A recording is a file already open on disk, so the times are read once,
        when the countdown starts. That is also what makes Stop safe: the
        writer was told the length it has to fade over before a frame existed.
        """
        values = {
            "music_bot_recording_countdown_seconds": 0,
            "music_bot_recording_fade_in_seconds": 2.0,
            "music_bot_recording_fade_out_seconds": 3.0,
        }
        with tempfile.TemporaryDirectory() as temp, \
                mock.patch(
                    "libs.game_audio_recorder.options.get",
                    side_effect=lambda key, default=None: values.get(key, default),
                ), mock.patch("libs.game_audio_recorder.options.set"):
            output = Path(temp) / "faded.wav"
            self.assertTrue(self.manager.start_to_path(output))
            self.assertTrue(_wait_for(lambda: self.manager.state() == "recording"))
            backend = _FakeBackend.instances[0]
            self.assertEqual(backend.kwargs["fade_in_seconds"], 2.0)
            self.assertEqual(backend.kwargs["fade_out_seconds"], 3.0)
            self.manager.stop()
            self.assertTrue(_wait_for(lambda: self.manager.state() == "idle"))

    def test_recording_snapshots_microphone_and_split_settings(self):
        values = {
            "music_bot_recording_include_microphone": True,
            "music_bot_recording_include_computer_audio": False,
            "music_bot_recording_countdown_seconds": 0,
            "music_bot_recording_split_minutes": 30,
            "music_bot_recording_announce_details": False,
        }
        with tempfile.TemporaryDirectory() as temp, \
                mock.patch(
                    "libs.game_audio_recorder.options.get",
                    side_effect=lambda key, default=None: values.get(key, default),
                ):
            output = Path(temp) / "recording.wav"
            self.assertTrue(self.manager.start_to_path(output))
            self.assertTrue(_wait_for(lambda: self.manager.state() == "recording"))
            backend = _FakeBackend.instances[0]
            self.assertIsInstance(backend.kwargs["microphone_overlay"],
                                  MicrophoneOverlayBuffer)
            self.assertFalse(self.manager.recorded_voice_is_studio())
            self.assertEqual(backend.kwargs["split_minutes"], 30)
            self.assertEqual(backend.kwargs["fade_in_seconds"], 0.0)
            self.assertEqual(backend.kwargs["fade_out_seconds"], 0.0)
            self.assertTrue(self.manager.feed_transmitted_microphone(b"\x01\x00" * 4))
            self.assertFalse(
                self.manager.feed_transmitted_microphone(
                    b"\x01\x00" * 4,
                    locally_rendered=True,
                )
            )
            self.manager.stop()
            self.assertTrue(_wait_for(lambda: self.manager.state() == "idle"))
        self.assertIn("Recording.", self.messages)
        self.assertNotIn("Ready, 3.", self.messages)
        self.assertIn("Recording saved.", self.messages)

    def test_recorded_voice_ships_transmitted_and_cycles_to_studio(self):
        stored = {}
        with mock.patch(
            "libs.game_audio_recorder.options.get",
            side_effect=lambda key, default=None: stored.get(key, default),
        ), mock.patch(
            "libs.game_audio_recorder.options.set",
            side_effect=lambda key, value: stored.__setitem__(key, value),
        ), mock.patch.object(
            self.manager, "_studio_available", return_value=True,
        ):
            self.assertEqual(self.manager.recorded_voice_label(),
                             "Recorded Voice: Transmitted")
            self.manager.cycle_recorded_voice()
            self.assertEqual(self.manager.recorded_voice_label(),
                             "Recorded Voice: Studio Condenser")
            self.assertIn("polished", self.messages[-1])
            self.manager.cycle_recorded_voice()
            self.assertEqual(self.manager.recorded_voice_label(),
                             "Recorded Voice: Transmitted")
            self.assertTrue(self.manager.restore_setting_defaults())
            self.assertEqual(stored[recorder_module.RECORDED_VOICE_OPTION],
                             recorder_module.RECORDED_VOICE_TRANSMITTED)

    def test_studio_choice_is_ignored_when_voice_effects_are_locked(self):
        stored = {
            recorder_module.RECORDED_VOICE_OPTION:
                recorder_module.RECORDED_VOICE_STUDIO,
        }
        with mock.patch(
            "libs.game_audio_recorder.options.get",
            side_effect=lambda key, default=None: stored.get(key, default),
        ), mock.patch(
            "libs.game_audio_recorder.options.set",
            side_effect=lambda key, value: stored.__setitem__(key, value),
        ), mock.patch.object(
            self.manager, "_studio_available", return_value=False,
        ):
            self.assertEqual(self.manager.recorded_voice_label(),
                             "Recorded Voice: Transmitted")
            self.manager.cycle_recorded_voice()
            self.assertIn("locked by staff", self.messages[-1])
            self.assertEqual(stored[recorder_module.RECORDED_VOICE_OPTION],
                             recorder_module.RECORDED_VOICE_STUDIO)

    def test_studio_take_polishes_the_microphone_overlay(self):
        values = {
            recorder_module.RECORDED_VOICE_OPTION:
                recorder_module.RECORDED_VOICE_STUDIO,
            "music_bot_recording_include_microphone": True,
            "music_bot_recording_countdown_seconds": 0,
        }
        with tempfile.TemporaryDirectory() as temp, mock.patch(
            "libs.game_audio_recorder.options.get",
            side_effect=lambda key, default=None: values.get(key, default),
        ), mock.patch.object(
            self.manager, "_studio_available", return_value=True,
        ):
            output = Path(temp) / "studio.wav"
            self.assertTrue(self.manager.start_to_path(output))
            self.assertTrue(_wait_for(lambda: self.manager.state() == "recording"))
            self.assertTrue(self.manager.recorded_voice_is_studio())
            backend = _FakeBackend.instances[0]
            overlay = backend.kwargs["microphone_overlay"]
            self.assertIsInstance(overlay, _ProcessedMicrophoneOverlay)
            samples = array("h", (
                int(12000 * math.sin(2.0 * math.pi * 440.0 * index / 48_000))
                for index in range(960)
            ))
            raw = samples.tobytes()
            self.assertTrue(self.manager.feed_transmitted_microphone(raw))
            shaped = overlay.take_mono16(960)
            self.assertEqual(len(shaped), len(raw))
            self.assertNotEqual(shaped, raw)
            self.manager.stop()
            self.assertTrue(_wait_for(lambda: self.manager.state() == "idle"))
            self.assertFalse(self.manager.recorded_voice_is_studio())

    def test_the_processed_overlay_applies_effect_then_trim(self):
        class Doubler:
            def process(self, pcm):
                samples = array("h")
                samples.frombytes(pcm)
                return array("h", (sample * 2 for sample in samples)).tobytes()

        overlay = MicrophoneOverlayBuffer()
        overlay.put_mono16(array("h", [100, -100]).tobytes())
        processed = _ProcessedMicrophoneOverlay(overlay, Doubler(), 0.5)
        self.assertEqual(processed.take_mono16(2),
                         array("h", [100, -100]).tobytes())

    def test_computer_audio_setting_selects_system_loopback_backend(self):
        values = {
            "music_bot_recording_include_computer_audio": True,
            "music_bot_recording_countdown_seconds": 0,
        }
        with tempfile.TemporaryDirectory() as temp, \
                mock.patch(
                    "libs.game_audio_recorder.options.get",
                    side_effect=lambda key, default=None: values.get(key, default),
                ):
            self.assertTrue(self.manager.start_to_path(Path(temp) / "computer-audio.wav"))
            self.assertTrue(_wait_for(lambda: self.manager.state() == "recording"))
            self.assertEqual(_FakeBackend.instances, [])
            self.assertEqual(len(_FakeSystemBackend.instances), 1)
            self.manager.stop()
            self.assertTrue(_wait_for(lambda: self.manager.state() == "idle"))

    def test_computer_audio_request_does_not_require_process_loopback_build(self):
        with tempfile.TemporaryDirectory() as temp:
            values = {
                "music_bot_recording_folder": temp,
                "music_bot_recording_include_computer_audio": True,
                "music_bot_recording_countdown_seconds": 0,
            }
            with mock.patch(
                    "libs.game_audio_recorder.options.get",
                    side_effect=lambda key, default=None: values.get(key, default),
                ), mock.patch(
                    "libs.game_audio_recorder.system_loopback_supported",
                    return_value=True,
                ), mock.patch(
                    "libs.game_audio_recorder.process_loopback_supported",
                    return_value=False,
                ):
                self.manager.request_start()
                self.assertTrue(_wait_for(lambda: self.manager.state() == "recording"))
                self.assertEqual(_FakeBackend.instances, [])
                self.assertEqual(len(_FakeSystemBackend.instances), 1)
                self.manager.stop()
                self.assertTrue(_wait_for(lambda: self.manager.state() == "idle"))

    def test_settings_cannot_change_during_recording(self):
        with tempfile.TemporaryDirectory() as temp, \
                mock.patch("libs.game_audio_recorder.options.get", return_value=False), \
                mock.patch("libs.game_audio_recorder.options.set") as set_option:
            self.assertTrue(self.manager.start_to_path(Path(temp) / "recording.wav"))
            self.assertTrue(_wait_for(lambda: self.manager.state() == "recording"))
            self.manager.toggle_microphone()
            set_option.assert_not_called()
            self.manager.stop()
            self.assertTrue(_wait_for(lambda: self.manager.state() == "idle"))
        self.assertIn("Stop the current recording before changing recording settings.", self.messages)


class ProcessLoopbackProbeTests(unittest.TestCase):
    """Capability-probe verdicts and guidance for game-only recording."""

    def setUp(self):
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
        with recorder_module._process_loopback_probe_lock:
            recorder_module._process_loopback_probe.update(
                {"started": False, "done": False, "supported": None}
            )
            recorder_module._process_loopback_probe_event.clear()

    def test_verdict_false_rejects_game_only_before_countdown(self):
        with recorder_module._process_loopback_probe_lock:
            recorder_module._process_loopback_probe["done"] = True
            recorder_module._process_loopback_probe["supported"] = False
            recorder_module._process_loopback_probe_event.set()
        self.assertFalse(process_loopback_supported())
        with mock.patch("libs.game_audio_recorder.os.name", "nt"), \
                mock.patch("libs.game_audio_recorder._windows_build", return_value=19045):
            self.manager.request_start()
        self.assertEqual(self.manager.state(), "idle")
        self.assertEqual(_FakeBackend.instances, [])
        self.assertIn(PROCESS_LOOPBACK_PROBE_GUIDANCE, self.messages)

    def test_probe_starts_once_and_caches_verdict(self):
        calls = []
        original = recorder_module._run_process_loopback_probe

        def fake_run():
            calls.append(1)
            with recorder_module._process_loopback_probe_lock:
                recorder_module._process_loopback_probe["done"] = True
                recorder_module._process_loopback_probe["supported"] = False
                recorder_module._process_loopback_probe_event.set()

        recorder_module._run_process_loopback_probe = fake_run
        try:
            with mock.patch("libs.game_audio_recorder.os.name", "nt"):
                self.assertTrue(recorder_module.start_process_loopback_probe())
                self.assertFalse(recorder_module.start_process_loopback_probe())
            self.assertEqual(len(calls), 1)
            # Verdict is now cached, so a fresh probe must not start again.
            self.assertFalse(recorder_module.start_process_loopback_probe())
            self.assertEqual(len(calls), 1)
            self.assertFalse(process_loopback_supported())
        finally:
            recorder_module._run_process_loopback_probe = original

    def test_unavailable_message_uses_floor_or_probe_guidance(self):
        with mock.patch("libs.game_audio_recorder.os.name", "nt"), \
                mock.patch("libs.game_audio_recorder._windows_build", return_value=19000):
            self.assertEqual(
                process_loopback_unavailable_message(),
                PROCESS_LOOPBACK_FLOOR_GUIDANCE,
            )
        self.assertNotIn(PROCESS_LOOPBACK_FLOOR_GUIDANCE, self.messages)

    def test_wait_returns_none_without_started_probe(self):
        self.assertIsNone(wait_for_process_loopback_verdict(0.01))

    def test_verdict_true_allows_game_only_start(self):
        with recorder_module._process_loopback_probe_lock:
            recorder_module._process_loopback_probe["done"] = True
            recorder_module._process_loopback_probe["supported"] = True
            recorder_module._process_loopback_probe_event.set()
        with mock.patch("libs.game_audio_recorder.os.name", "nt"), \
                mock.patch("libs.game_audio_recorder._windows_build", return_value=19045):
            self.assertTrue(process_loopback_supported())


class GameAudioPcmTests(unittest.TestCase):
    @staticmethod
    def _pcm(samples):
        values = array("h", samples)
        return values.tobytes()

    def test_microphone_overlay_limits_instead_of_clipping(self):
        stereo = self._pcm([27_000, -27_000, 100, -100])
        mono = self._pcm([7_000, 7_000])
        mixer = _MicrophoneMixer()
        mixed = array("h")
        mixed.frombytes(mixer.mix(stereo, mono))
        # 34,000 would have clipped; the soft knee bends the sum under the
        # ceiling without moving one channel against the other.
        self.assertLess(max(abs(sample) for sample in mixed), 32_767)
        self.assertGreater(mixed[0], 0)
        self.assertLess(mixed[1], 0)
        # Below the knee the sum is exact: a quiet game bed is untouched.
        quiet = self._pcm([1_000, -1_000, 100, -100])
        quiet_voice = self._pcm([100, 100])
        below = array("h")
        below.frombytes(mixer.mix(quiet, quiet_voice))
        self.assertEqual(below.tolist(), [1_100, -900, 200, 0])
        # A silent microphone is the game's own recording, byte for byte.
        silent = self._pcm([0, 0])
        self.assertEqual(mixer.mix(stereo, silent), stereo)

    def test_microphone_buffer_holds_a_partial_block(self):
        overlay = MicrophoneOverlayBuffer()
        overlay.put_mono16(self._pcm([1, 2, 3]))
        # A 5-frame request must not pad the missing 2 frames with silence:
        # that silence is what put a hole in a voice frame the Studio chain
        # then processed.
        self.assertIsNone(overlay.take_mono16(5))
        result = array("h")
        result.frombytes(overlay.take_mono16(3))
        self.assertEqual(result.tolist(), [1, 2, 3])
        self.assertIsNone(overlay.take_mono16(1))

    def test_microphone_buffer_splits_one_frame_into_consecutive_blocks(self):
        overlay = MicrophoneOverlayBuffer()
        samples = list(range(960))
        overlay.put_mono16(self._pcm(samples))
        first = array("h")
        first.frombytes(overlay.take_mono16(480))
        second = array("h")
        second.frombytes(overlay.take_mono16(480))
        self.assertEqual(first.tolist() + second.tolist(), samples)

    def test_segmented_writer_splits_without_overwriting(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "recording.wav"
            writer = _SegmentedWaveWriter(output)
            writer.max_frames = 2
            writer.write(self._pcm([1, 1, 2, 2, 3, 3, 4, 4, 5, 5]))
            writer.close()

            self.assertEqual(len(writer.paths), 3)
            frame_counts = []
            for path in writer.paths:
                with wave.open(path, "rb") as recording:
                    frame_counts.append(recording.getnframes())
                    self.assertEqual(recording.getnchannels(), 2)
            self.assertEqual(frame_counts, [2, 2, 1])

    def test_no_fade_writes_the_file_it_always_wrote(self):
        """Both fades off is the old writer, byte for byte.

        The setting ships off, and a player who never touches it must get the
        same recording this code produced before the fade existed.
        """
        with tempfile.TemporaryDirectory() as temp:
            plain = Path(temp) / "plain.wav"
            zero = Path(temp) / "zero.wav"
            pcm = self._pcm([1000, -1000] * 40)
            for path, writer in (
                (plain, _SegmentedWaveWriter(plain)),
                (zero, _SegmentedWaveWriter(zero, 0, 0.0, 0.0)),
            ):
                writer.write(pcm)
                writer.close()
            self.assertEqual(plain.read_bytes(), zero.read_bytes())

    def test_fade_in_starts_at_silence_and_reaches_full_level(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "fade-in.wav"
            writer = _SegmentedWaveWriter(output, 0, fade_in_seconds=1.0)
            writer.write(self._pcm([20_000, 20_000] * 96_000))  # two seconds
            writer.close()
            with wave.open(str(output), "rb") as recording:
                self.assertEqual(recording.getnframes(), 96_000)
                samples = array("h")
                samples.frombytes(recording.readframes(recording.getnframes()))
            # Frame 0 is the ramp's own zero, half a second in is half level,
            # and the first frame past the ramp is the sample as captured.
            self.assertEqual(samples[0], 0)
            self.assertAlmostEqual(samples[24_000 * 2] / 20_000, 0.5, delta=0.01)
            self.assertEqual(samples[48_000 * 2], 20_000)
            self.assertEqual(samples[95_999 * 2], 20_000)

    def test_fade_out_falls_to_silence_at_the_end(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "fade-out.wav"
            writer = _SegmentedWaveWriter(output, 0, fade_out_seconds=0.5)
            writer.write(self._pcm([20_000, 20_000] * 48_000))  # one second
            writer.close()
            with wave.open(str(output), "rb") as recording:
                self.assertEqual(recording.getnframes(), 48_000)
                samples = array("h")
                samples.frombytes(recording.readframes(recording.getnframes()))
            # The tail is held back until Stop and written with the ramp on it:
            # the last frame is silence, the middle of the window is half.
            self.assertLessEqual(abs(samples[-2]), 1)
            self.assertAlmostEqual(samples[36_000 * 2] / 20_000, 0.5, delta=0.01)
            # Everything before the window is the capture, untouched.
            self.assertEqual(samples[23_999 * 2], 20_000)

    def test_fade_out_tail_finishes_the_open_part_instead_of_a_stub(self):
        """A split lands on whole parts: the held tail does not open a new one."""
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "split-fade.wav"
            writer = _SegmentedWaveWriter(output, 0, fade_out_seconds=0.5)
            writer.max_frames = 48_000  # one second per part
            writer.write(self._pcm([5000, 5000] * 96_000))  # two seconds
            writer.close()
            self.assertEqual(len(writer.paths), 2)
            with wave.open(writer.paths[1], "rb") as recording:
                self.assertEqual(recording.getnframes(), 48_000)

    def test_a_fade_longer_than_the_recording_still_ends_quietly(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "short.wav"
            writer = _SegmentedWaveWriter(
                output, 0, fade_in_seconds=5.0, fade_out_seconds=5.0)
            writer.write(self._pcm([9000, 9000] * 4800))  # 0.1 seconds
            writer.close()
            with wave.open(str(output), "rb") as recording:
                self.assertEqual(recording.getnframes(), 4800)
                samples = array("h")
                samples.frombytes(recording.readframes(recording.getnframes()))
            self.assertEqual(samples[0], 0)
            self.assertLessEqual(abs(samples[-2]), 1)

    def test_closing_twice_does_not_write_the_held_tail_twice(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "twice.wav"
            writer = _SegmentedWaveWriter(output, 0, fade_out_seconds=0.25)
            writer.write(self._pcm([700, 700] * 48_000))
            writer.close()
            writer.close()
            with wave.open(str(output), "rb") as recording:
                self.assertEqual(recording.getnframes(), 48_000)


class VoiceChatRecorderRoutingTests(unittest.TestCase):
    def test_gameplay_music_bot_wins_over_open_stack_menu(self):
        direct_music_bot = object()
        unrelated_stack_bot = object()
        recorder = object.__new__(VoiceChatRecord)
        recorder.player = SimpleNamespace(
            gameplay=SimpleNamespace(music_bot=direct_music_bot),
        )
        recorder.game = SimpleNamespace(
            stack=[SimpleNamespace(music_bot=unrelated_stack_bot)],
        )
        self.assertIs(recorder._find_music_bot(), direct_music_bot)

    def test_stack_remains_a_fallback_when_gameplay_is_unavailable(self):
        fallback_music_bot = object()
        recorder = object.__new__(VoiceChatRecord)
        recorder.player = SimpleNamespace(gameplay=None)
        recorder.game = SimpleNamespace(
            stack=[SimpleNamespace(music_bot=fallback_music_bot)],
        )
        self.assertIs(recorder._find_music_bot(), fallback_music_bot)


if __name__ == "__main__":
    unittest.main()
