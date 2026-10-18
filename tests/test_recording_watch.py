"""Recording software is reported to staff, never punished.

These tests pin the five signals (a known recorder by process name, a recorder's
window title, Windows' live capture sessions, a capture session on a loopback
endpoint -- the PC's own audio being copied -- and a virtual audio device) and
the rule that makes the feature safe: nothing here closes the client.  Every
scan is injected, so this never reads the real process table, device list or
registry.
"""

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from libs import anti_cheat
from libs import consts
from libs import game_audio_recorder as recorder_module
from libs import recording_watch as watch


class ProcessClassificationTests(unittest.TestCase):
    def test_known_recorder_matches_name_path_and_case(self):
        for name in (
            "obs64.exe",
            "OBS64.EXE",
            "Obs.exe",
            r"C:\Program Files\obs-studio\bin\64bit\obs64.exe",
            "C:/Program Files/obs-studio/obs64.exe",
        ):
            self.assertEqual(watch.classify_recording_process(name), "OBS Studio", name)

    def test_a_name_without_its_extension_still_matches(self):
        self.assertEqual(watch.classify_recording_process("Audacity"), "Audacity")
        self.assertEqual(watch.classify_recording_process("REAPER"), "REAPER")

    def test_everyday_programs_are_not_recorders(self):
        for name in ("chrome.exe", "discord.exe", "python.exe", "notobs64.exe", "", None, "   "):
            self.assertIsNone(watch.classify_recording_process(name), repr(name))


class WindowClassificationTests(unittest.TestCase):
    def test_obs_versioned_title_is_recognised(self):
        self.assertEqual(
            watch.classify_recording_window("OBS 30.2.4 - Profile: untitled - Scenes: untitled"),
            "OBS Studio",
        )

    def test_other_recorder_titles_are_recognised(self):
        self.assertEqual(watch.classify_recording_window("Audacity 3.5.1"), "Audacity")
        self.assertEqual(watch.classify_recording_window("song.rpp - REAPER v7.0"), "REAPER")
        self.assertEqual(watch.classify_recording_window("Adobe Audition 2024"), "Adobe Audition")

    def test_a_title_that_merely_mentions_recording_is_ignored(self):
        for title in (
            "Jobs at Acme - Google Chrome",
            "how to record audio on windows - Google Chrome",
            "obs - Google Search",
            "",
            None,
        ):
            self.assertIsNone(watch.classify_recording_window(title), repr(title))


class CaptureSessionTests(unittest.TestCase):
    """The deep signal: which program owns a live capture session."""

    @staticmethod
    def _resolver(mapping):
        return lambda pid: mapping.get(pid, "")

    def test_a_capturing_program_is_named_from_its_pid(self):
        names = watch.capture_app_names(
            [4242], own_pids=(), resolve=self._resolver({4242: "studio_recorder.exe"})
        )
        self.assertEqual(names, ["studio_recorder.exe"])

    def test_this_game_is_never_named(self):
        # The Client's own voice chat and line-in instruments capture the
        # microphone; reporting the game itself would flag every player.
        names = watch.capture_app_names(
            [7], own_pids=(7,), resolve=self._resolver({7: "Beyond Tournament.exe"})
        )
        self.assertEqual(names, [])

    def test_the_running_process_is_excluded_by_default(self):
        names = watch.capture_app_names([os.getpid()], own_pids=None, resolve=lambda pid: "x.exe")
        self.assertEqual(names, [])

    def test_windows_audio_services_are_never_named(self):
        names = watch.capture_app_names(
            [1, 2],
            own_pids=(),
            resolve=self._resolver({1: "audiodg.exe", 2: "svchost.exe"}),
        )
        self.assertEqual(names, [])

    def test_ordinary_voice_chat_and_browsers_are_not_treated_as_recorders(self):
        mapping = {1: "Discord.exe", 2: "chrome.exe", 3: "TeamTalk5.exe", 4: "Zoom.exe"}
        names = watch.capture_app_names([1, 2, 3, 4], own_pids=(), resolve=self._resolver(mapping))
        self.assertEqual(names, [])

    def test_a_program_with_no_resolvable_name_is_skipped(self):
        names = watch.capture_app_names(
            [1, 2], own_pids=(), resolve=self._resolver({1: "", 2: "rec.exe"})
        )
        self.assertEqual(names, ["rec.exe"])

    def test_repeated_entries_are_named_once(self):
        names = watch.capture_app_names(
            [1, 2],
            own_pids=(),
            resolve=self._resolver({1: "recorder.exe", 2: "Recorder.exe"}),
        )
        self.assertEqual(names, ["recorder.exe"])


class LoopbackCaptureTests(unittest.TestCase):
    """Level two: the PC's own output being copied, rather than the room.

    A capture session on "Stereo Mix" or a virtual cable's output is the match
    being recorded.  The endpoint's name is the whole discriminator, which is
    why it is read per device and never guessed from the program.
    """

    @staticmethod
    def _resolver(mapping):
        return lambda pid: mapping.get(pid, "")

    def test_loopback_endpoints_are_recognised_by_name(self):
        for name in (
            "Stereo Mix (Realtek(R) Audio)",
            "Stereo Mix",
            "What U Hear (Sound Blaster)",
            "Wave Out Mix",
            "CABLE Output (VB-Audio Virtual Cable)",
            "VoiceMeeter Out B1",
        ):
            self.assertIsNotNone(watch.classify_loopback_capture_device(name), name)

    def test_a_microphone_is_never_a_loopback(self):
        # The floor: every real input on this kind of machine must stay quiet,
        # or the deep signal would fire for every player in voice chat.
        for name in (
            "Microphone (USB Audio Device)",
            "Microphone Array (AMD Audio Device)",
            "Line In (Realtek(R) Audio)",
            "Speakers (Realtek(R) Audio)",
            "",
            None,
        ):
            self.assertIsNone(watch.classify_loopback_capture_device(name), repr(name))

    def test_sessions_are_split_by_their_endpoint(self):
        microphone, loopback = watch.split_capture_sessions([
            (10, "Microphone (USB Audio Device)"),
            (11, "Stereo Mix (Realtek(R) Audio)"),
            (12, "CABLE Output (VB-Audio Virtual Cable)"),
            (0, "Stereo Mix (Realtek(R) Audio)"),
        ])
        self.assertEqual(microphone, [10])
        self.assertEqual([pid for pid, _device in loopback], [11, 12])

    def test_one_process_can_capture_both(self):
        microphone, loopback = watch.split_capture_sessions([
            (10, "Microphone Array (AMD Audio Device)"),
            (10, "Stereo Mix (Realtek(R) Audio)"),
        ])
        self.assertEqual(microphone, [10])
        self.assertEqual([pid for pid, _device in loopback], [10])

    def test_the_capturing_program_is_named_with_its_device(self):
        apps = watch.loopback_capture_apps(
            [(4242, "CABLE Output (VB-Audio Virtual Cable)")],
            own_pids=(),
            resolve=self._resolver({4242: "obs64.exe"}),
        )
        self.assertEqual(apps, [("obs64.exe", "CABLE Output (VB-Audio Virtual Cable)")])

    def test_a_microphone_session_is_never_a_loopback_finding(self):
        apps = watch.loopback_capture_apps(
            [(1, "Microphone (USB Audio Device)")],
            own_pids=(),
            resolve=self._resolver({1: "rec.exe"}),
        )
        self.assertEqual(apps, [])

    def test_the_game_itself_is_never_named_for_recording_the_pc(self):
        apps = watch.loopback_capture_apps(
            [(7, "Stereo Mix")],
            own_pids=(7,),
            resolve=self._resolver({7: "Beyond Tournament.exe"}),
        )
        self.assertEqual(apps, [])

    def test_the_running_process_is_excluded_by_default(self):
        apps = watch.loopback_capture_apps(
            [(os.getpid(), "Stereo Mix")], own_pids=None, resolve=lambda pid: "x.exe"
        )
        self.assertEqual(apps, [])

    def test_windows_audio_hosts_are_not_named(self):
        apps = watch.loopback_capture_apps(
            [(1, "Stereo Mix"), (2, "Stereo Mix")],
            own_pids=(),
            resolve=self._resolver({1: "audiodg.exe", 2: "rec.exe"}),
        )
        self.assertEqual(apps, [("rec.exe", "Stereo Mix")])

    def test_an_endpoint_that_cannot_be_named_is_empty_not_an_exception(self):
        self.assertEqual(watch.endpoint_friendly_name(""), "")
        self.assertEqual(watch.endpoint_friendly_name("{0.0.1.00000000}.{not-a-real-guid}"), "")

    def test_endpoint_names_are_cached_and_bounded(self):
        watch._endpoint_name_cache.clear()
        try:
            for index in range(watch.MAX_CACHED_ENDPOINT_NAMES + 5):
                watch.endpoint_friendly_name(f"{{0.0.1.00000000}}.{{missing-{index}}}")
            self.assertLessEqual(len(watch._endpoint_name_cache), watch.MAX_CACHED_ENDPOINT_NAMES)
        finally:
            watch._endpoint_name_cache.clear()


class ObserverTests(unittest.TestCase):
    def _observer(self, processes=("chrome.exe", "obs64.exe"), microphone=None):
        return watch.RecordingObserver(
            process_scan=lambda: processes,
            window_scan=lambda: (),
            device_scan=lambda: (),
            loopback_scan=lambda: (),
            microphone_scan=lambda: (
                microphone if microphone is not None else ["obs64.exe", "studio_recorder.exe"]
            ),
            own_names=("python.exe",),
        )

    def test_a_known_recorder_and_an_unnamed_microphone_user_are_both_named(self):
        findings = self._observer().poll()
        self.assertEqual(
            [(f.app, f.source) for f in findings],
            [("OBS Studio", "process"), ("studio_recorder.exe", "microphone")],
        )

    def test_a_known_recorder_holding_the_microphone_is_said_once(self):
        findings = self._observer().poll()
        self.assertEqual([f.app for f in findings].count("OBS Studio"), 1)
        self.assertNotIn("obs64.exe", [f.app for f in findings])

    def test_a_finding_is_offered_until_staff_was_actually_told(self):
        observer = self._observer()
        first = observer.poll()
        self.assertTrue(first)
        # Nothing marked: the next poll offers the same findings again, so a
        # recorder opened before login is not lost with no network to send it.
        self.assertEqual(observer.poll(), first)

        for finding in first:
            observer.mark_reported(finding)
        self.assertEqual(observer.poll(), [])

    def test_a_running_tool_is_not_announced_over_and_over(self):
        observer = self._observer()
        for finding in observer.poll():
            observer.mark_reported(finding)
        self.assertEqual(observer.poll(), [])

    def test_a_tool_that_closes_is_reported_again_when_it_returns(self):
        present = ["obs64.exe"]
        observer = watch.RecordingObserver(
            process_scan=lambda: tuple(present),
            window_scan=lambda: (),
            device_scan=lambda: (),
            loopback_scan=lambda: (),
            microphone_scan=lambda: [],
            own_names=(),
        )
        first = observer.poll()
        self.assertEqual([f.app for f in first], ["OBS Studio"])
        for finding in first:
            observer.mark_reported(finding)
        self.assertEqual(observer.poll(), [])

        present.clear()
        self.assertEqual(observer.poll(), [])
        present.append("obs64.exe")
        self.assertEqual([f.app for f in observer.poll()], ["OBS Studio"])

    def test_a_new_game_session_reports_an_open_tool_again(self):
        observer = self._observer()
        for finding in observer.poll():
            observer.mark_reported(finding)
        self.assertEqual(observer.poll(), [])

        observer.reset()
        self.assertEqual(len(observer.poll()), 2)

    def test_copying_the_pc_output_replaces_the_plain_running_line(self):
        observer = watch.RecordingObserver(
            process_scan=lambda: ("obs64.exe",),
            window_scan=lambda: (),
            device_scan=lambda: (),
            loopback_scan=lambda: [("obs64.exe", "Stereo Mix (Realtek(R) Audio)")],
            microphone_scan=lambda: (),
            own_names=("python.exe",),
        )
        findings = observer.poll()
        # One line per tool, and it is the narrower fact: the line says what is
        # being copied, not merely that the program is open.
        self.assertEqual([(f.app, f.source) for f in findings], [("OBS Studio", "loopback")])
        self.assertIn("Stereo Mix", findings[0].detail)

    def test_a_program_seen_only_on_a_loopback_endpoint_is_reported(self):
        observer = watch.RecordingObserver(
            process_scan=lambda: (),
            window_scan=lambda: (),
            device_scan=lambda: (),
            loopback_scan=lambda: [("ScreenRecorder.exe", "CABLE Output (VB-Audio Virtual Cable)")],
            microphone_scan=lambda: (),
            own_names=(),
        )
        self.assertEqual(
            [(f.app, f.source) for f in observer.poll()],
            [("ScreenRecorder.exe", "loopback")],
        )

    def test_the_loopback_net_honours_the_own_process_list(self):
        observer = watch.RecordingObserver(
            process_scan=lambda: (),
            window_scan=lambda: (),
            device_scan=lambda: (),
            loopback_scan=lambda: [("Beyond Tournament.exe", "Stereo Mix")],
            microphone_scan=lambda: (),
            own_names=("Beyond Tournament.exe",),
        )
        self.assertEqual(observer.poll(), [])

    def test_the_game_itself_is_never_reported_from_the_microphone_sessions(self):
        observer = watch.RecordingObserver(
            process_scan=lambda: (),
            window_scan=lambda: (),
            device_scan=lambda: (),
            loopback_scan=lambda: (),
            microphone_scan=lambda: ("python.exe",),
            own_names=("Python.EXE",),
        )
        self.assertEqual(observer.poll(), [])

    def test_a_window_title_reports_a_renamed_recorder(self):
        observer = watch.RecordingObserver(
            process_scan=lambda: ("mine.exe",),
            window_scan=lambda: ("OBS 30.2.4 - Profile: untitled",),
            device_scan=lambda: (),
            loopback_scan=lambda: (),
            microphone_scan=lambda: [],
            own_names=(),
        )
        self.assertEqual(
            [(f.app, f.source) for f in observer.poll()],
            [("OBS Studio", "window")],
        )

    def test_the_scans_are_capped_so_one_machine_cannot_flood_staff(self):
        processes = tuple(watch.KNOWN_RECORDING_PROCESSES.keys())
        findings = self._observer(processes=processes, microphone=[]).poll()
        self.assertLessEqual(len(findings), watch.MAX_FINDINGS_PER_SCAN)


class VirtualDeviceTests(unittest.TestCase):
    """A virtual cable is how sound is routed to a recorder elsewhere.

    The signal is deliberately a device, not a recorder: VoiceMeeter and VB-Cable
    are ordinary tools for streamers and musicians, so the finding says what is
    installed and the line says what it is -- nothing more.
    """

    def _observer(self, devices):
        return watch.RecordingObserver(
            process_scan=lambda: (),
            window_scan=lambda: (),
            device_scan=lambda: tuple(devices),
            loopback_scan=lambda: (),
            microphone_scan=lambda: [],
            own_names=(),
        )

    def test_the_common_virtual_devices_are_recognised(self):
        cases = {
            "OpenAL Soft on CABLE Input (VB-Audio Virtual Cable)": "VB-Audio virtual cable",
            "OpenAL Soft on Microphone (VB-Audio VoiceMeeter VAIO)": "VoiceMeeter",
            "OpenAL Soft on Line 1 (Virtual Audio Cable)": "Virtual Audio Cable",
            "OpenAL Soft on VoiceMeeter Aux Input": "VoiceMeeter",
        }
        for name, family in cases.items():
            with self.subTest(name=name):
                self.assertEqual(watch.classify_virtual_audio_device(name), family)

    def test_ordinary_devices_are_not_virtual_cables(self):
        for name in (
            "OpenAL Soft on Speakers (Realtek(R) Audio)",
            "OpenAL Soft on Microphone (USB Audio Device)",
            "OpenAL Soft on Headphones (Sony WH-1000XM5)",
            "",
            None,
        ):
            with self.subTest(name=name):
                self.assertIsNone(watch.classify_virtual_audio_device(name))

    def test_a_virtual_device_is_reported_once_with_its_own_name(self):
        observer = self._observer(
            [
                "OpenAL Soft on CABLE Input (VB-Audio Virtual Cable)",
                "OpenAL Soft on CABLE Output (VB-Audio Virtual Cable)",
            ]
        )
        findings = observer.poll()
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].app, "VB-Audio virtual cable")
        self.assertEqual(findings[0].source, "device")
        # The device's own name, without OpenAL's prefix, so staff can tell which
        # endpoint it is instead of reading the same sentence twice.
        self.assertEqual(findings[0].detail, "CABLE Input (VB-Audio Virtual Cable)")

    def test_a_virtual_device_is_forgotten_when_it_is_removed(self):
        devices = ["OpenAL Soft on VoiceMeeter Input"]
        observer = watch.RecordingObserver(
            process_scan=lambda: (),
            window_scan=lambda: (),
            device_scan=lambda: tuple(devices),
            loopback_scan=lambda: (),
            microphone_scan=lambda: [],
            own_names=(),
        )
        for finding in observer.poll():
            observer.mark_reported(finding)
        self.assertEqual(observer.poll(), [])

        devices.clear()
        self.assertEqual(observer.poll(), [])
        devices.append("OpenAL Soft on VoiceMeeter Input")
        self.assertEqual([f.source for f in observer.poll()], ["device"])

    def test_a_missing_device_list_only_costs_this_signal(self):
        observer = watch.RecordingObserver(
            process_scan=lambda: ("obs64.exe",),
            window_scan=lambda: (),
            device_scan=lambda: (),
            loopback_scan=lambda: (),
            microphone_scan=lambda: [],
            own_names=(),
        )
        self.assertEqual([f.app for f in observer.poll()], ["OBS Studio"])

    def test_the_device_query_is_cached_for_the_scan_cycle(self):
        # The watchdog asks every two to five seconds; enumerating OpenAL devices
        # on every scan would make this the most expensive part of the net, so
        # one query serves a minute of scans.
        calls = []

        def specifiers():
            calls.append("playback")
            return ["OpenAL Soft on Speakers (Realtek(R) Audio)"]

        def capture():
            calls.append("capture")
            return SimpleNamespace(
                devices=["OpenAL Soft on CABLE Input (VB-Audio Virtual Cable)"]
            )

        fake_cyal = SimpleNamespace(
            get_all_device_specifiers=specifiers,
            CaptureExtension=capture,
        )
        watch._virtual_device_cache.update({"at": float("-inf"), "names": ()})
        try:
            with mock.patch.object(watch.os, "name", "nt"), \
                    mock.patch.dict(sys.modules, {"cyal": fake_cyal}):
                first = watch.audio_device_names(now=0.0)
                second = watch.audio_device_names(now=30.0)
            self.assertEqual(first, second)
            self.assertIn("OpenAL Soft on CABLE Input (VB-Audio Virtual Cable)", first)
            # One playback query and one capture query served both reads.
            self.assertEqual(sorted(calls), ["capture", "playback"])
        finally:
            watch._virtual_device_cache.update({"at": float("-inf"), "names": ()})

    def test_an_audio_engine_that_cannot_be_asked_costs_only_this_signal(self):
        watch._virtual_device_cache.update({"at": float("-inf"), "names": ()})
        try:
            with mock.patch.object(watch.os, "name", "nt"), \
                    mock.patch.dict(sys.modules, {"cyal": None}):
                self.assertEqual(watch.audio_device_names(now=0.0), ())
        finally:
            watch._virtual_device_cache.update({"at": float("-inf"), "names": ()})


class InGameRecordingReportTests(unittest.TestCase):
    """The game's own recorder is telemetry for Developers, not a rule."""

    def setUp(self):
        watch._pending_in_game_reports.clear()

    def tearDown(self):
        watch._pending_in_game_reports.clear()

    def test_it_reports_the_session_scope_on_the_misc_channel(self):
        sent = []
        game = SimpleNamespace(network=SimpleNamespace(send=lambda *args: sent.append(args)))
        self.assertTrue(
            watch.report_in_game_recording(
                game, include_microphone=True, include_computer_audio=True
            )
        )
        self.assertEqual(len(sent), 1)
        channel, event, payload = sent[0]
        self.assertEqual(channel, consts.CHANNEL_MISC)
        self.assertEqual(event, "in_game_recording_started")
        self.assertEqual(
            payload,
            {"include_microphone": True, "include_computer_audio": True},
        )

    def test_the_flags_are_always_booleans(self):
        sent = []
        game = SimpleNamespace(network=SimpleNamespace(send=lambda *args: sent.append(args)))
        watch.report_in_game_recording(game, include_microphone=1, include_computer_audio=None)
        self.assertEqual(
            sent[0][2],
            {"include_microphone": True, "include_computer_audio": False},
        )

    def test_without_a_network_nothing_is_sent_and_nothing_raises(self):
        self.assertFalse(watch.report_in_game_recording(SimpleNamespace(network=None)))
        self.assertFalse(watch.report_in_game_recording(SimpleNamespace()))

    def test_a_failed_send_never_escapes_into_the_recording(self):
        def boom(*args, **kwargs):
            raise OSError("no route to host")

        game = SimpleNamespace(network=SimpleNamespace(send=boom))
        with mock.patch("builtins.print"):
            self.assertFalse(watch.report_in_game_recording(game))

    def test_a_start_that_could_not_be_sent_is_retried_later(self):
        offline = SimpleNamespace(network=None)
        self.assertFalse(watch.report_in_game_recording(offline, include_microphone=True))

        sent = []
        online = SimpleNamespace(network=SimpleNamespace(send=lambda *args: sent.append(args)))
        self.assertEqual(watch.flush_pending_in_game_recording(online), 1)
        self.assertEqual(sent[0][1], "in_game_recording_started")
        self.assertTrue(sent[0][2]["include_microphone"])
        self.assertEqual(watch.flush_pending_in_game_recording(online), 0)

    def test_a_retry_that_still_fails_keeps_the_report(self):
        def boom(*args, **kwargs):
            raise OSError("still offline")

        watch.report_in_game_recording(SimpleNamespace(network=None))
        with mock.patch("builtins.print"):
            self.assertEqual(
                watch.flush_pending_in_game_recording(SimpleNamespace(network=SimpleNamespace(send=boom))),
                0,
            )
        self.assertEqual(len(watch._pending_in_game_reports), 1)

    def test_the_queue_is_bounded(self):
        offline = SimpleNamespace(network=None)
        for index in range(watch.MAX_PENDING_IN_GAME_REPORTS + 3):
            watch.report_in_game_recording(offline, include_microphone=bool(index % 2))
        self.assertEqual(
            len(watch._pending_in_game_reports), watch.MAX_PENDING_IN_GAME_REPORTS
        )


class InGameRecorderHookTests(unittest.TestCase):
    """The recorder reports only once it really started, with what it captures."""

    def test_a_started_recording_reports_its_scope_once(self):
        class FakeBackend:
            def __init__(self, process_id):
                self.process_id = process_id

            def record(self, output_path, stop_event, started, **kwargs):
                started()
                stop_event.wait(2)
                return 48_000, 1.0, (os.fspath(output_path),)

        game = SimpleNamespace(put=lambda callback: callback(), network=None)
        with tempfile.TemporaryDirectory() as temp, mock.patch.object(
            recorder_module.recording_watch, "report_in_game_recording"
        ) as report:
            manager = recorder_module.GameAudioRecorderManager(
                game,
                lambda: None,
                backend_factory=FakeBackend,
                countdown_interval=0,
            )
            try:
                self.assertTrue(manager.start_to_path(Path(temp) / "recording.wav"))
                deadline = time.monotonic() + 2
                while manager.state() != "recording" and time.monotonic() < deadline:
                    time.sleep(0.005)
                self.assertEqual(manager.state(), "recording")
                manager.stop()
                while manager.state() != "idle" and time.monotonic() < deadline:
                    time.sleep(0.005)
            finally:
                manager.close()

        report.assert_called_once_with(
            game, include_microphone=False, include_computer_audio=False
        )

    def test_the_started_recording_reaches_the_wire_without_mocking(self):
        class FakeBackend:
            def __init__(self, process_id):
                self.process_id = process_id

            def record(self, output_path, stop_event, started, **kwargs):
                started()
                stop_event.wait(2)
                return 48_000, 1.0, (os.fspath(output_path),)

        sent = []
        game = SimpleNamespace(
            put=lambda callback: callback(),
            network=SimpleNamespace(send=lambda *args: sent.append(args)),
        )
        with tempfile.TemporaryDirectory() as temp:
            manager = recorder_module.GameAudioRecorderManager(
                game,
                lambda: None,
                backend_factory=FakeBackend,
                countdown_interval=0,
            )
            try:
                self.assertTrue(manager.start_to_path(Path(temp) / "recording.wav"))
                deadline = time.monotonic() + 2
                while manager.state() != "recording" and time.monotonic() < deadline:
                    time.sleep(0.005)
                manager.stop()
                while manager.state() != "idle" and time.monotonic() < deadline:
                    time.sleep(0.005)
            finally:
                manager.close()

        packet = [args for args in sent if args[1] == "in_game_recording_started"]
        self.assertEqual(len(packet), 1)
        self.assertEqual(
            packet[0][2],
            {"include_microphone": False, "include_computer_audio": False},
        )


class DegradedCoverageTests(unittest.TestCase):
    """A client that lost the deep signal must say so, not look quiet."""

    def setUp(self):
        self.original_game = anti_cheat._game
        self.original_degraded = anti_cheat._degraded_reported
        self.original_signal = watch._capture_signal["available"]
        self.sent = []
        anti_cheat._game = SimpleNamespace(
            network=SimpleNamespace(send=lambda *args: self.sent.append(args))
        )
        anti_cheat._degraded_reported = False

    def tearDown(self):
        anti_cheat._game = self.original_game
        anti_cheat._degraded_reported = self.original_degraded
        watch._capture_signal["available"] = self.original_signal

    def _degraded_events(self):
        return [args for args in self.sent if args[1] == "recording_watch_degraded"]

    def test_a_blind_client_reports_it_once(self):
        watch._capture_signal["available"] = False
        anti_cheat.report_recording_watch_coverage()
        events = self._degraded_events()
        self.assertEqual(len(events), 1)
        channel, event, payload = events[0]
        self.assertEqual(channel, consts.CHANNEL_MISC)
        self.assertEqual(payload["reason"], "capture_sessions_unavailable")
        self.assertTrue(payload["os_version"].startswith("Windows"))

        anti_cheat.report_recording_watch_coverage()
        self.assertEqual(len(self._degraded_events()), 1)

    def test_a_working_engine_and_an_untried_one_say_nothing(self):
        watch._capture_signal["available"] = True
        anti_cheat.report_recording_watch_coverage()
        watch._capture_signal["available"] = None
        anti_cheat.report_recording_watch_coverage()
        self.assertEqual(self._degraded_events(), [])

    def test_a_new_game_session_may_report_coverage_again(self):
        watch._capture_signal["available"] = False
        anti_cheat.report_recording_watch_coverage()
        anti_cheat.set_game_reference(anti_cheat._game)
        anti_cheat.report_recording_watch_coverage()
        self.assertEqual(len(self._degraded_events()), 2)

    def test_without_a_network_nothing_is_sent(self):
        watch._capture_signal["available"] = False
        anti_cheat._game = SimpleNamespace(network=None)
        anti_cheat.report_recording_watch_coverage()
        self.assertEqual(self.sent, [])


class WatchHeartbeatTests(unittest.TestCase):
    """Silence must be tellable apart from "nothing was recorded".

    The Server announces an online player whose watch stops reporting, so the
    heartbeat is what makes "no reports" mean something.  It is throttled by
    time rather than by scan count, and it rides the watchdog even when there is
    nothing to report.
    """

    def setUp(self):
        self.original_game = anti_cheat._game
        self.original_heartbeat = anti_cheat._last_watch_heartbeat
        self.sent = []
        anti_cheat._game = SimpleNamespace(
            network=SimpleNamespace(send=lambda *args: self.sent.append(args))
        )
        anti_cheat._last_watch_heartbeat = 0.0

    def tearDown(self):
        anti_cheat._game = self.original_game
        anti_cheat._last_watch_heartbeat = self.original_heartbeat

    def _beats(self):
        return [args for args in self.sent if args[1] == "recording_watch_alive"]

    def test_a_heartbeat_is_sent_on_the_misc_channel(self):
        anti_cheat.report_recording_watch_heartbeat()
        beats = self._beats()
        self.assertEqual(len(beats), 1)
        self.assertEqual(beats[0][0], consts.CHANNEL_MISC)

    def test_a_heartbeat_is_not_sent_again_within_the_interval(self):
        anti_cheat.report_recording_watch_heartbeat()
        anti_cheat.report_recording_watch_heartbeat()
        self.assertEqual(len(self._beats()), 1)

    def test_a_heartbeat_is_sent_again_after_the_interval(self):
        anti_cheat.report_recording_watch_heartbeat()
        anti_cheat._last_watch_heartbeat -= anti_cheat.RECORDING_WATCH_HEARTBEAT_S + 1
        anti_cheat.report_recording_watch_heartbeat()
        self.assertEqual(len(self._beats()), 2)

    def test_a_new_game_session_says_it_is_up_right_away(self):
        anti_cheat.report_recording_watch_heartbeat()
        self.assertEqual(len(self._beats()), 1)
        # The Server forgot everything about this player at login, so waiting a
        # whole minute for the first heartbeat would look like a silent watch.
        anti_cheat.set_game_reference(anti_cheat._game)
        anti_cheat.report_recording_watch_heartbeat()
        self.assertEqual(len(self._beats()), 2)

    def test_without_a_network_nothing_is_sent_and_nothing_raises(self):
        anti_cheat._game = SimpleNamespace(network=None)
        anti_cheat.report_recording_watch_heartbeat()
        self.assertEqual(self.sent, [])

    def test_a_failed_send_does_not_claim_the_beat(self):
        def refuse(*args):
            raise OSError("no route")

        anti_cheat._game = SimpleNamespace(network=SimpleNamespace(send=refuse))
        anti_cheat.report_recording_watch_heartbeat()
        self.assertEqual(anti_cheat._last_watch_heartbeat, 0.0)

    def test_a_match_is_checked_more_often_than_the_menu(self):
        anti_cheat._game = SimpleNamespace(
            network=None,
            gameplay=SimpleNamespace(game_started=True),
        )
        self.assertEqual(
            anti_cheat._recording_scan_seconds(), anti_cheat.RECORDING_SCAN_SECONDS_MATCH
        )
        anti_cheat._game = SimpleNamespace(
            network=None,
            gameplay=SimpleNamespace(game_started=False),
        )
        self.assertEqual(
            anti_cheat._recording_scan_seconds(), anti_cheat.RECORDING_SCAN_SECONDS_IDLE
        )

    def test_no_gameplay_yet_reads_as_out_of_a_match(self):
        anti_cheat._game = SimpleNamespace(network=None, gameplay=None)
        self.assertFalse(anti_cheat._in_match())
        anti_cheat._game = None
        self.assertEqual(
            anti_cheat._recording_scan_seconds(), anti_cheat.RECORDING_SCAN_SECONDS_IDLE
        )

    def test_an_in_match_scan_is_faster_than_the_menu(self):
        self.assertLess(
            anti_cheat.RECORDING_SCAN_SECONDS_MATCH, anti_cheat.RECORDING_SCAN_SECONDS_IDLE
        )


def _watchdog_source():
    return Path(anti_cheat.__file__).read_text(encoding="utf-8")


class WatchdogLoopTests(unittest.TestCase):
    """The loop itself: the heartbeat is not tied to the scan's cadence."""

    def test_the_heartbeat_runs_on_every_cycle_the_scan_may_skip(self):
        source = _watchdog_source()
        loop = source[source.index("def _speedhack_watchdog") :]
        self.assertIn("report_recording_watch_heartbeat()", loop)
        # Inside `if not skip_process_scan:` but outside the cadence branch: a
        # minute of silence must not depend on how often the process table is
        # read, or a slow scan would look like a dead watch.
        heartbeat = loop.index("report_recording_watch_heartbeat()")
        cadence = loop.index("_recording_scan_seconds()")
        self.assertLess(heartbeat, cadence)
        self.assertIn("if cycles - last_recording_cycle >= _recording_scan_seconds():", loop)
        self.assertNotIn("cycles % 5", loop)

    def test_the_diagnostic_switch_disables_both_scans(self):
        source = _watchdog_source()
        loop = source[source.index("def _speedhack_watchdog") :]
        self.assertEqual(loop.count("if not skip_process_scan"), 2)


class OwnProcessTests(unittest.TestCase):
    def test_this_process_is_always_excluded(self):
        names = [name.lower() for name in watch.own_process_names()]
        self.assertIn(os.path.basename(sys.executable or "").lower(), names)


if __name__ == "__main__":
    unittest.main()
