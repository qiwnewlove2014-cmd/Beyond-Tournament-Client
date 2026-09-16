"""The folder and save prompts must not cost the game its frame, or its notes.

The measured symptom: pressing "Set Recording Folder" (or starting a download
that has to ask for a folder) hitched the game, and the sound hitched with it.
The cause is Tk, not the file browser: building it -- loading Tcl/Tk and
creating the first window -- holds the interpreter for ~80 ms, so a worker
thread doing it starves the game loop anyway. On the machine this was written
on a 20 ms frame loop ran 0.7 ms late when idle and 81.7 ms late while a worker
thread built Tk (``import tkinter`` alone was 40 ms of wall time and no delay:
that part releases the interpreter for its file I/O).

These tests pin the shape that fixes it: the dialog is built in its own
process, its answer comes back as UTF-8 bytes, a cancel is not a failure, and
no module the running game imports builds Tk at all.
"""

import io
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from libs import folder_dialog
from libs import game_audio_recorder as recorder_module
from libs.music_bot import music_downloader as downloader_module
from libs.music_bot.music_downloader import MusicDownloadManager


CLIENT_DIR = os.path.abspath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir))
THAI_FOLDER = "C:\\Users\\ผู้ใช้\\Music"


def _read(path):
    """Text, with its encoding named: a Windows console codepage is not UTF-8."""
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read()


def _wait_for(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


class _FakePipe:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class _FakeStdout(_FakePipe):
    def __init__(self, answer, delay=0.0):
        _FakePipe.__init__(self)
        self.answer = answer
        self.delay = delay

    def read(self):
        if self.delay:
            time.sleep(self.delay)
        return self.answer


class _FakeProcess:
    """Just enough of a Popen for the dialog helper, and nothing more."""

    def __init__(self, answer=b"", code=0, delay=0.0):
        self.stdin = _FakePipe()
        self.stdout = _FakeStdout(answer, delay)
        self.code = code
        self.returncode = None
        self.killed = False

    def wait(self, timeout=None):
        self.returncode = self.code
        return self.code

    def poll(self):
        return self.returncode

    def kill(self):
        self.killed = True
        self.returncode = -9


class _ImmediateGame:
    def put(self, callback):
        callback()


class FolderDialogProtocolTests(unittest.TestCase):
    """How the parent asks, and what it makes of each answer."""

    def setUp(self):
        self.calls = []
        self.process = _FakeProcess()
        patcher = mock.patch.object(folder_dialog.subprocess, "Popen", self._popen)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _popen(self, command, **options):
        self.calls.append((command, options))
        return self.process

    def _answer(self, answer=b"", code=0, delay=0.0):
        """What the child says back on the next ask."""
        self.process = _FakeProcess(answer=answer, code=code, delay=delay)
        return self.process

    def _command(self):
        self.assertTrue(self.calls, "no child was started")
        return self.calls[-1][0]

    def _options(self):
        return self.calls[-1][1]

    def test_a_thai_folder_name_comes_back_exactly(self):
        self._answer(answer=THAI_FOLDER.encode("utf-8"))
        self.assertEqual(folder_dialog.ask_folder("Select folder", "C:\\"),
                         (THAI_FOLDER, ""))

    def test_the_choice_is_read_back_as_utf8_not_the_console_codepage(self):
        # The same bytes read the way a text-mode pipe would read them (the
        # console's own ANSI codepage) mean something else entirely, and cp874
        # is what this machine would have used.
        raw = THAI_FOLDER.encode("utf-8")
        self.assertNotEqual(raw.decode("utf-8"), raw.decode("cp874", "replace"))
        self._answer(answer=raw)
        self.assertEqual(folder_dialog.ask_folder("Select folder"),
                         (raw.decode("utf-8"), ""))

    def test_the_child_is_started_by_path_with_an_argv_list(self):
        folder_dialog.ask_folder("Select folder", "C:\\Music")
        command, options = self._command(), self._options()
        self.assertIsInstance(command, list)
        self.assertEqual(command[0], sys.executable)
        self.assertTrue(os.path.isabs(command[1]), command[1])
        self.assertTrue(command[1].endswith("beyond_tournament.py"))
        self.assertEqual(command[2], folder_dialog.FILE_DIALOG_FLAG)
        self.assertEqual(command[3], folder_dialog.FOLDER)
        self.assertEqual(command[4], "Select folder")
        self.assertEqual(command[5], "C:\\Music")
        self.assertFalse(options["shell"])
        self.assertEqual(options["creationflags"],
                         getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.assertTrue(options["close_fds"])
        self.assertEqual(options["cwd"], CLIENT_DIR)
        self.assertNotIn("PYTHONPATH", options["env"])
        self.assertEqual(options["env"]["PYTHONDONTWRITEBYTECODE"], "1")
        self.assertIs(options["stdout"], subprocess.PIPE)
        self.assertIs(options["stderr"], subprocess.DEVNULL)

    def test_the_save_prompt_names_a_file_and_the_folder_prompt_a_folder(self):
        folder_dialog.ask_save_file("Save it", "C:\\Music", "recording.wav")
        self.assertEqual(self._command()[3], folder_dialog.SAVE)
        self.assertEqual(self._command()[4:],
                         ["Save it", "C:\\Music", "recording.wav"])

    def test_a_cancelled_dialog_is_not_a_failure(self):
        self._answer(answer=b"")
        self.assertEqual(folder_dialog.ask_folder("Select folder"), ("", ""))

    def test_a_failed_child_is_reported_rather_than_read_as_a_cancel(self):
        self._answer(answer=b"", code=3)
        folder, problem = folder_dialog.ask_folder("Select folder")
        self.assertEqual(folder, "")
        self.assertTrue(problem)
        self.assertIn("3", problem)

    def test_a_child_that_cannot_be_started_is_reported(self):
        with mock.patch.object(folder_dialog.subprocess, "Popen",
                               side_effect=OSError("cannot spawn")):
            folder, problem = folder_dialog.ask_folder("Select folder")
        self.assertEqual(folder, "")
        self.assertIn("cannot spawn", problem)

    def test_a_child_that_never_answers_is_killed_not_waited_on_for_ever(self):
        process = self._answer(answer=b"", delay=0.5)
        folder, problem = folder_dialog.ask_folder("Select folder", timeout=0.02)
        self.assertEqual(folder, "")
        self.assertTrue(problem)
        self.assertTrue(process.killed)

    def test_our_pipe_is_the_childs_lifetime_and_is_released_afterwards(self):
        process = self._answer(answer=b"C:\\Music")
        folder_dialog.ask_folder("Select folder")
        # The child watches its own stdin: closing that early would take a
        # dialog the player is still looking at down with it, so it is handed
        # over open and released once the child has answered.
        self.assertIs(self._options()["stdin"], subprocess.PIPE)
        self.assertTrue(process.stdin.closed)
        self.assertTrue(process.stdout.closed)

    def test_the_title_is_bounded_and_never_empty(self):
        folder_dialog.ask_folder("x" * 5000)
        self.assertEqual(len(self._command()[4]), folder_dialog.MAX_TITLE)
        folder_dialog.ask_folder("")
        self.assertTrue(self._command()[4])


class FolderDialogChildTests(unittest.TestCase):
    """The half that runs in its own process."""

    def test_worker_main_writes_the_choice_as_utf8_bytes(self):
        stdout = SimpleNamespace(buffer=io.BytesIO())
        with mock.patch.object(folder_dialog, "_exit_with_parent"), \
                mock.patch.object(folder_dialog, "_show", return_value=THAI_FOLDER), \
                mock.patch.object(sys, "stdout", stdout), \
                mock.patch.object(sys, "stderr", io.StringIO()):
            code = folder_dialog.worker_main([folder_dialog.FOLDER, "Select folder"])
        self.assertEqual(code, 0)
        self.assertEqual(stdout.buffer.getvalue(), THAI_FOLDER.encode("utf-8"))

    def test_worker_main_reports_a_dialog_it_could_not_build(self):
        stdout = SimpleNamespace(buffer=io.BytesIO())
        stderr = io.StringIO()
        with mock.patch.object(folder_dialog, "_exit_with_parent"), \
                mock.patch.object(folder_dialog, "_show",
                                  side_effect=RuntimeError("no Tcl")), \
                mock.patch.object(sys, "stdout", stdout), \
                mock.patch.object(sys, "stderr", stderr):
            code = folder_dialog.worker_main([folder_dialog.FOLDER])
        self.assertEqual(code, 1)
        self.assertEqual(stdout.buffer.getvalue(), b"")
        self.assertIn("no Tcl", stderr.getvalue())

    def test_a_dialog_nothing_is_waiting_for_does_not_outlive_the_game(self):
        exits = []
        stdin = SimpleNamespace(fileno=lambda: 0)
        with mock.patch.object(sys, "stdin", stdin), \
                mock.patch.object(folder_dialog.os, "read", lambda fd, count: b""), \
                mock.patch.object(folder_dialog.os, "_exit", exits.append):
            folder_dialog._exit_with_parent()
            self.assertTrue(_wait_for(lambda: exits == [0]),
                            "the child kept its window after the game closed")

    def test_the_wait_is_raw_so_it_cannot_abort_at_shutdown(self):
        # A buffered read holds its lock while it blocks, and a daemon thread
        # still holding that lock as the interpreter finalizes aborts the whole
        # child (_enter_buffered_busy) -- which reached the game as a failed
        # dialog even though the answer had already been written out.
        reads = []
        stdin = SimpleNamespace(fileno=lambda: 7,
                                buffer=SimpleNamespace(read=lambda count: b""))
        with mock.patch.object(sys, "stdin", stdin), \
                mock.patch.object(folder_dialog.os, "read",
                                  lambda fd, count: (reads.append((fd, count)), b"")[1]), \
                mock.patch.object(folder_dialog.os, "_exit", lambda code: None):
            folder_dialog._exit_with_parent()
            self.assertTrue(_wait_for(lambda: reads == [(7, 1)]))

    def test_a_real_child_hands_the_choice_back_as_utf8_bytes(self):
        # The whole handshake for real, without a window to click: a real
        # interpreter, the real pipe our lifetime is tied to, and a Thai path
        # that only survives if both ends agree it is UTF-8.
        script = (
            "import sys; from libs import folder_dialog; "
            "folder_dialog._show = lambda *args: sys.argv[1]; "
            "raise SystemExit(folder_dialog.worker_main(['folder', 'Test', '']))"
        )
        process = subprocess.Popen(
            [sys.executable, "-c", script, THAI_FOLDER],
            cwd=CLIENT_DIR, shell=False, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        try:
            raw = process.stdout.read()
            code = process.wait(timeout=60)
        finally:
            for stream in (process.stdin, process.stdout, process.stderr):
                stream.close()
        self.assertEqual(code, 0)
        self.assertEqual(raw, THAI_FOLDER.encode("utf-8"))

    def test_a_child_with_no_stdin_does_not_watch_for_one(self):
        for stdin in (None, SimpleNamespace()):
            with mock.patch.object(sys, "stdin", stdin), \
                    mock.patch.object(folder_dialog.threading, "Thread") as thread:
                folder_dialog._exit_with_parent()
            thread.assert_not_called()

    def _fake_tkinter(self, calls, answer=""):
        class _Root:
            destroyed = False

            def withdraw(self):
                pass

            def attributes(self, *_args):
                pass

            def destroy(self):
                self.destroyed = True

        root = _Root()

        def record(name):
            return lambda **options: (calls.append((name, options)), answer)[1]

        filedialog = SimpleNamespace(
            askdirectory=record("askdirectory"),
            asksaveasfilename=record("asksaveasfilename"),
        )
        tkinter = SimpleNamespace(Tk=lambda: root, filedialog=filedialog)
        return tkinter, filedialog, root

    def test_the_folder_prompt_asks_for_an_existing_folder(self):
        calls = []
        tkinter, filedialog, root = self._fake_tkinter(calls, answer="C:\\Music")
        with mock.patch.dict(sys.modules, {"tkinter": tkinter,
                                           "tkinter.filedialog": filedialog}):
            choice = folder_dialog._show(folder_dialog.FOLDER, "Pick a folder", "", "")
        self.assertEqual(choice, "C:\\Music")
        name, options = calls[-1]
        self.assertEqual(name, "askdirectory")
        self.assertTrue(options["mustexist"])
        self.assertNotIn("initialdir", options)
        self.assertTrue(root.destroyed)

    def test_the_save_prompt_offers_a_wav_that_may_be_overwritten(self):
        calls = []
        tkinter, filedialog, root = self._fake_tkinter(
            calls, answer="C:\\Music\\take.wav")
        with mock.patch.dict(sys.modules, {"tkinter": tkinter,
                                           "tkinter.filedialog": filedialog}):
            choice = folder_dialog._show(folder_dialog.SAVE, "Save it",
                                         "C:\\Music", "take.wav")
        self.assertEqual(choice, "C:\\Music\\take.wav")
        name, options = calls[-1]
        self.assertEqual(name, "asksaveasfilename")
        self.assertEqual(options["defaultextension"], ".wav")
        self.assertEqual(options["filetypes"], [("WAV audio", "*.wav")])
        self.assertTrue(options["confirmoverwrite"])
        self.assertEqual(options["initialdir"], "C:\\Music")
        self.assertEqual(options["initialfile"], "take.wav")
        self.assertTrue(root.destroyed)


class NoGameModuleBuildsTkTests(unittest.TestCase):
    """The defect itself: Tk built in the process that plays the sound."""

    def test_the_game_never_builds_tk_for_a_folder_or_a_save_name(self):
        for relative in ("libs/game_audio_recorder.py",
                         "libs/music_bot/music_downloader.py"):
            source = _read(os.path.join(CLIENT_DIR, relative))
            self.assertNotIn("tkinter", source, relative)
            self.assertNotIn("askdirectory", source, relative)
            self.assertIn("folder_dialog", source, relative)

    def test_the_entry_script_answers_the_flag_before_any_game_import(self):
        source = _read(os.path.join(CLIENT_DIR, "beyond_tournament.py"))
        branch = 'sys.argv[1] == "%s"' % folder_dialog.FILE_DIALOG_FLAG
        self.assertIn(branch, source)
        self.assertLess(source.index(branch), source.index("import pygame"))
        # ...and above the compiled-output redirect, which would point the
        # child's stdout at the game's log instead of the pipe we read it from.
        self.assertLess(source.index(branch),
                        source.index("\n    _configure_compiled_output()"))


class TheRecorderAsksTheHelperTests(unittest.TestCase):
    def setUp(self):
        self.messages = []
        self.manager = recorder_module.GameAudioRecorderManager(
            _ImmediateGame(), lambda: None,
            backend_factory=lambda process_id: None,
            system_backend_factory=lambda process_id: None,
            countdown_interval=0,
        )
        # Both _announce and _accept_folder_choice speak through _speak; a real
        # voice in a test run is never what is being checked here.
        self.manager._speak = self.messages.append

    def tearDown(self):
        self.manager.close()

    def test_a_folder_choice_arrives_from_the_helper_process(self):
        with tempfile.TemporaryDirectory() as temp, \
                mock.patch.object(recorder_module.folder_dialog, "ask_folder",
                                  return_value=(temp, "")) as ask, \
                mock.patch.object(recorder_module.options, "set") as save_option:
            self.manager._configuring_folder = True
            self.manager._choose_folder_worker(temp)
        ask.assert_called_once()
        self.assertEqual(ask.call_args[0][1], temp)
        save_option.assert_called_once_with("music_bot_recording_folder", temp)
        self.assertFalse(self.manager._configuring_folder)

    def test_a_cancelled_folder_choice_changes_nothing(self):
        with mock.patch.object(recorder_module.folder_dialog, "ask_folder",
                               return_value=("", "")), \
                mock.patch.object(recorder_module.options, "set") as save_option:
            self.manager._configuring_folder = True
            self.manager._choose_folder_worker(os.path.expanduser("~"))
        save_option.assert_not_called()
        self.assertIn("Recording folder was not changed.", self.messages)
        self.assertFalse(self.manager._configuring_folder)

    def test_a_dialog_that_could_not_open_says_so_and_frees_the_next_press(self):
        with mock.patch.object(recorder_module.folder_dialog, "ask_folder",
                               return_value=("", "no Tcl")):
            self.manager._configuring_folder = True
            self.manager._choose_folder_worker(os.path.expanduser("~"))
        self.assertIn("The recording folder dialog could not be opened.",
                      self.messages)
        self.assertFalse(self.manager._configuring_folder)

    def test_the_save_prompt_names_the_file_through_the_helper_process(self):
        with tempfile.TemporaryDirectory() as temp:
            chosen = os.path.join(temp, "take.wav")
            with mock.patch.object(recorder_module.folder_dialog, "ask_save_file",
                                   return_value=(chosen, "")) as ask, \
                    mock.patch.object(self.manager, "_accept_selected_path") as accept:
                self.manager._state = "selecting"
                self.manager._select_output_path()
        title, initial_dir, initial_file = ask.call_args[0]
        self.assertTrue(title)
        self.assertTrue(initial_dir.endswith("Beyond Tournament Recordings"))
        self.assertIn("Beyond Tournament Recording ", initial_file)
        self.assertTrue(initial_file.endswith(".wav"))
        accept.assert_called_once_with(chosen)

    def test_a_save_prompt_that_could_not_open_cancels_the_recording(self):
        with mock.patch.object(recorder_module.folder_dialog, "ask_save_file",
                               return_value=("", "no Tcl")), \
                mock.patch.object(self.manager, "_accept_selected_path") as accept:
            self.manager._state = "selecting"
            self.manager._select_output_path()
        accept.assert_not_called()
        self.assertEqual(self.manager.state(), "idle")
        self.assertIn("The recording file dialog could not be opened.",
                      self.messages)


class TheDownloaderAsksTheHelperTests(unittest.TestCase):
    def setUp(self):
        self.game = SimpleNamespace(put=lambda callback: callback())
        self.manager = MusicDownloadManager(self.game, lambda: None, "C:/game/ffmpeg.exe")
        self.manager._progress_bar = mock.Mock()

    def test_the_selector_arrives_from_the_helper_process(self):
        with tempfile.TemporaryDirectory() as temp, \
                mock.patch.object(downloader_module.folder_dialog, "ask_folder",
                                  return_value=(temp, "")) as ask:
            chosen = []
            self.manager._open_folder_selector(chosen.append, "cancelled")
            self.assertTrue(_wait_for(lambda: chosen))
        self.assertEqual(chosen, [os.path.abspath(temp)])
        self.assertEqual(ask.call_args[0][1], "")
        self.assertFalse(self.manager._folder_dialog_open)

    def test_a_cancelled_selector_says_so(self):
        spoken = []
        with mock.patch.object(downloader_module.folder_dialog, "ask_folder",
                               return_value=("", "")), \
                mock.patch.object(downloader_module, "speak", spoken.append):
            self.manager._open_folder_selector(lambda folder: None, "cancelled")
            self.assertTrue(_wait_for(lambda: spoken))
        self.assertIn("cancelled", spoken)
        self.assertFalse(self.manager._folder_dialog_open)

    def test_a_selector_that_could_not_open_says_so(self):
        spoken = []
        with mock.patch.object(downloader_module.folder_dialog, "ask_folder",
                               return_value=("", "no Tcl")), \
                mock.patch.object(downloader_module, "speak", spoken.append):
            self.manager._open_folder_selector(lambda folder: None, "cancelled")
            self.assertTrue(_wait_for(lambda: spoken))
        self.assertTrue(any("Could not open" in message for message in spoken))
        self.assertFalse(self.manager._folder_dialog_open)


class TheDownloadBackendIsPreloadedTests(unittest.TestCase):
    """yt-dlp's import is ~700 ms and must not be paid at the download press."""

    def setUp(self):
        self.was_set = downloader_module._PRELOAD_STARTED.is_set()
        downloader_module._PRELOAD_STARTED.clear()

        def restore():
            downloader_module._PRELOAD_STARTED.clear()
            if self.was_set:
                downloader_module._PRELOAD_STARTED.set()

        self.addCleanup(restore)

    def _fake_threads(self, started):
        class _Thread:
            def __init__(self, target=None, name=None, daemon=None):
                started.append({"target": target, "name": name, "daemon": daemon})

            def start(self):
                pass

        return mock.patch.object(downloader_module.threading, "Thread", _Thread)

    def test_the_import_is_started_once_in_the_background(self):
        started = []
        with self._fake_threads(started):
            downloader_module._preload_download_backend()
            downloader_module._preload_download_backend()
        self.assertEqual(len(started), 1)
        self.assertEqual(started[0]["name"], "music-download-preload")
        self.assertTrue(started[0]["daemon"])

    def test_the_preload_loads_yt_dlp_and_never_raises(self):
        started = []
        with self._fake_threads(started):
            downloader_module._preload_download_backend()
        with mock.patch.dict(sys.modules, {"yt_dlp": mock.MagicMock()}):
            started[0]["target"]()
            self.assertIn("yt_dlp", sys.modules)

    def test_asking_for_a_download_starts_the_preload_before_the_menus(self):
        manager = MusicDownloadManager(self.game(), lambda: None, "C:/game/ffmpeg.exe")
        manager._progress_bar = mock.Mock()
        request = [{"title": "Song", "target": "https://youtu.be/abcdefghijk"}]
        with mock.patch.object(downloader_module, "_preload_download_backend") as preload, \
                mock.patch.object(MusicDownloadManager, "_show_format_menu") as menu:
            manager.configure(request, "music")
        preload.assert_called_once()
        menu.assert_called_once()

    def test_a_selection_with_nothing_downloadable_starts_nothing(self):
        manager = MusicDownloadManager(self.game(), lambda: None, "C:/game/ffmpeg.exe")
        manager._progress_bar = mock.Mock()
        with mock.patch.object(downloader_module, "_preload_download_backend") as preload:
            manager.configure([{"title": "Local", "target": "C:/music/song.mp3"}], "music")
        preload.assert_not_called()

    @staticmethod
    def game():
        return SimpleNamespace(put=lambda callback: callback())


if __name__ == "__main__":
    unittest.main()
