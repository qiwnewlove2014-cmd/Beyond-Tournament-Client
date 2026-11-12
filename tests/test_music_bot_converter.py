"""The Audio File Converter: the naming, the command, the batch and its page.

The third file tool beside Record Audio and Music Download Center. It takes the
files a player already has -- their own selection, a folder, or the Picks -- and
writes every one of them as one format, beside the original unless a folder was
chosen.

Two rules are the whole reason this file exists. **The original is only ever an
input**, so the destination naming is pinned against every way it could land on
a source or on an earlier result; and **a batch ends with a sentence**, because
a player hears the progress and nothing else. ffmpeg is never run here: the
subprocess seam is faked and the counts, the names and what is spoken are the
things under test.
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from libs.music_bot.converter import (  # noqa: E402
    AudioConverter, DEFAULT_FORMAT_KEY, FORMATS, FORMAT_BY_KEY, FORMAT_OPTION,
    FOLDER_OPTION)
from libs.music_bot.menu_pages.converter import ConverterMenus  # noqa: E402


class _ImmediateThread:
    """A thread that runs where it was started, so a dialog can be tested."""

    def __init__(self, target=None, daemon=None, **kwargs):
        self.target = target

    def start(self):
        self.target()

    def join(self, timeout=None):
        return None

    def is_alive(self):
        return False


class FakeMenu:
    last = None

    def __init__(self, game, title, parrent=None, intro=""):
        self.title = title
        self.intro = intro
        self.items = []
        self.pos = 0
        self.spoken = 0
        FakeMenu.last = self

    def add_items(self, items):
        self.items = list(items)

    def speak_current_item(self):
        self.spoken += 1


class ConverterTestCase(unittest.TestCase):
    def setUp(self):
        self.said = []
        self.saved = {}
        self.addCleanup(mock.patch.stopall)
        mock.patch("libs.music_bot.converter.speak",
                   lambda text, **kwargs: self.said.append(text)).start()
        mock.patch(
            "libs.music_bot.converter.options.get",
            lambda key, default=None: self.saved.get(key, default)).start()
        mock.patch(
            "libs.music_bot.converter.options.set",
            lambda key, value: self.saved.__setitem__(key, value)).start()
        self.sources = tempfile.TemporaryDirectory()
        self.addCleanup(self.sources.cleanup)
        self.out = tempfile.TemporaryDirectory()
        self.addCleanup(self.out.cleanup)
        self.converter = self.make_converter()

    def make_converter(self, ffmpeg_path="ffmpeg"):
        return AudioConverter(SimpleNamespace(put=lambda callback: callback()),
                              ffmpeg_path=ffmpeg_path)

    def write(self, name, folder=None, text="audio"):
        path = os.path.join(folder or self.sources.name, name)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        return path

    def fake_ffmpeg(self, failing=(), cancelled_by=None):
        """Stand in for ffmpeg: write the destination, answer the count."""
        def run(cmd, **kwargs):
            destination = cmd[-1]
            if os.path.basename(cmd[cmd.index("-i") + 1]) in failing:
                # A real failure can leave a half-written file behind.
                with open(destination, "wb") as handle:
                    handle.write(b"partial")
                return SimpleNamespace(returncode=1, stdout=b"", stderr=b"no")
            with open(destination, "wb") as handle:
                handle.write(b"converted")
            if cancelled_by is not None:
                self.converter._cancel_event.set()
            return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")
        return run


class TheDestinationNeverOverwritesTests(ConverterTestCase):
    def test_a_new_format_sits_beside_the_original_under_its_own_name(self):
        source = self.write("Song.wav")
        self.assertEqual(self.converter.destination_for(source, "mp3_192"),
                         os.path.join(self.sources.name, "Song.mp3"))

    def test_a_name_already_there_gets_a_number(self):
        self.write("Song.mp3")
        source = self.write("Song.wav")
        self.assertEqual(self.converter.destination_for(source, "mp3_192"),
                         os.path.join(self.sources.name, "Song (2).mp3"))

    def test_a_file_in_its_own_format_is_numbered_rather_than_written_over(self):
        source = self.write("Song.mp3")
        destination = self.converter.destination_for(source, "mp3_192")
        self.assertEqual(destination,
                         os.path.join(self.sources.name, "Song (2).mp3"))
        self.assertNotEqual(os.path.normcase(os.path.abspath(destination)),
                            os.path.normcase(os.path.abspath(source)))

    def test_a_third_run_keeps_counting(self):
        self.write("Song.mp3")
        self.write("Song (2).mp3")
        source = self.write("Song.wav")
        self.assertEqual(self.converter.destination_for(source, "mp3_192"),
                         os.path.join(self.sources.name, "Song (3).mp3"))

    def test_a_chosen_folder_is_used_instead(self):
        source = self.write("Song.wav")
        self.assertEqual(
            self.converter.destination_for(source, "flac", folder=self.out.name),
            os.path.join(self.out.name, "Song.flac"))

    def test_a_folder_that_cannot_be_made_says_so_instead_of_writing(self):
        source = self.write("Song.wav")
        blocked = os.path.join(source, "inside-a-file")
        self.assertEqual(
            self.converter.destination_for(source, "mp3_192", folder=blocked),
            "")

    def test_the_opening_line_says_where_the_files_go(self):
        """The picker closed on the source; the sentence answers "where?"."""
        folder = tempfile.gettempdir()
        self.saved[FOLDER_OPTION] = os.path.abspath(folder)
        only = self.write("A.wav")
        with mock.patch("libs.music_bot.converter.subprocess.run",
                        self.fake_ffmpeg()):
            self.assertTrue(self.converter.start([only]))
            self.converter._worker.join(10)
        self.assertIn(
            f"Converting 1 file to MP3, 192 kbps, into {os.path.abspath(folder)}.",
            self.said)

    def test_the_destination_summary_names_the_saved_folder_or_the_original(self):
        self.assertEqual(self.converter.destination_summary(),
                         "beside each original")
        self.saved[FOLDER_OPTION] = os.path.abspath(self.out.name)
        self.assertEqual(self.converter.destination_summary(),
                         f"into {os.path.abspath(self.out.name)}")
        self.saved[FOLDER_OPTION] = os.path.join(self.out.name, "gone")
        self.assertEqual(self.converter.destination_summary(),
                         "beside each original")


class TheCommandTests(ConverterTestCase):
    def test_the_command_can_never_overwrite_and_takes_audio_only(self):
        command = self.converter._command("C:\\a.wav", "C:\\a.mp3",
                                          FORMAT_BY_KEY["mp3_192"])
        self.assertEqual(command[0], "ffmpeg")
        self.assertIn("-n", command)
        self.assertIn("-vn", command)
        self.assertEqual(command[command.index("-i") + 1], "C:\\a.wav")
        self.assertEqual(command[-1], "C:\\a.mp3")
        self.assertIn("libmp3lame", command)
        self.assertIn("-map_metadata", command, "the tags would be lost")

    def test_every_format_carries_its_own_args_and_extension(self):
        keys = [fmt["key"] for fmt in FORMATS]
        self.assertEqual(len(keys), len(set(keys)))
        self.assertEqual({fmt["extension"] for fmt in FORMATS},
                         {".mp3", ".ogg", ".wav", ".flac"})
        for fmt in FORMATS:
            self.assertTrue(fmt["args"], fmt["key"])
            self.assertTrue(fmt["note"].strip(), fmt["key"])
            self.assertTrue(fmt["extension"].startswith("."), fmt["key"])

    def test_the_default_is_the_middle_mp3(self):
        self.assertEqual(self.converter.format_key(), DEFAULT_FORMAT_KEY)
        self.assertEqual(self.converter.format()["label"], "MP3, 192 kbps")


class TheBatchTests(ConverterTestCase):
    def run_batch(self, paths, label="your selection"):
        self.assertTrue(self.converter.start(paths, label))
        self.converter._worker.join(10)

    def test_every_file_is_converted_and_no_original_is_touched(self):
        first = self.write("A.wav", text="original-a")
        second = self.write("B.wav", text="original-b")
        with mock.patch("libs.music_bot.converter.subprocess.run",
                        self.fake_ffmpeg()):
            self.run_batch([first, second])

        self.assertTrue(os.path.isfile(os.path.join(self.sources.name, "A.mp3")))
        self.assertTrue(os.path.isfile(os.path.join(self.sources.name, "B.mp3")))
        for path, text in ((first, "original-a"), (second, "original-b")):
            with open(path, encoding="utf-8") as handle:
                self.assertEqual(handle.read(), text, "an original was written")
        # Membership, not order: a group of tests sharing one interpreter can
        # stand a stub in for `threading.Thread`, which runs the worker inside
        # `start()` and reorders these sentences. What is said, and that the
        # batch is complete, is the rule.
        self.assertCountEqual(self.said, [
            "Converting 2 files to MP3, 192 kbps, beside each original.",
            "1 of 2 done.",
            "Converted 2 files to MP3, 192 kbps.",
        ])

    def test_one_file_reads_as_one_file(self):
        only = self.write("A.wav")
        with mock.patch("libs.music_bot.converter.subprocess.run",
                        self.fake_ffmpeg()):
            self.run_batch([only])
        self.assertCountEqual(
            self.said,
            ["Converting 1 file to MP3, 192 kbps, beside each original.",
             "Converted 1 file to MP3, 192 kbps."])

    def test_a_failed_file_does_not_stop_the_rest_and_leaves_nothing_behind(self):
        bad = self.write("Bad.wav")
        good = self.write("Good.wav")
        with mock.patch("libs.music_bot.converter.subprocess.run",
                        self.fake_ffmpeg(failing=("Bad.wav",))):
            self.run_batch([bad, good])

        self.assertFalse(os.path.exists(os.path.join(self.sources.name, "Bad.mp3")),
                         "a half-written output was left behind")
        self.assertTrue(os.path.isfile(os.path.join(self.sources.name, "Good.mp3")))
        self.assertTrue(os.path.isfile(bad))
        self.assertIn("Converted 1 file to MP3, 192 kbps, 1 did not convert.",
                      self.said)

    def test_a_missing_file_is_counted_and_the_batch_continues(self):
        present = self.write("A.wav")
        gone = os.path.join(self.sources.name, "gone.wav")
        with mock.patch("libs.music_bot.converter.subprocess.run",
                        self.fake_ffmpeg()):
            self.run_batch([gone, present])
        self.assertIn("Converted 1 file to MP3, 192 kbps, 1 missing.",
                      self.said)
        self.assertTrue(os.path.isfile(present))

    def test_cancel_stops_before_the_next_file(self):
        first = self.write("A.wav")
        second = self.write("B.wav")
        with mock.patch("libs.music_bot.converter.subprocess.run",
                        self.fake_ffmpeg(cancelled_by=True)):
            self.run_batch([first, second])
        cancelled = [line for line in self.said
                     if line.startswith("Conversion cancelled. ")]
        self.assertTrue(cancelled, self.said)
        self.assertIn("Converted 1 file", cancelled[-1])
        self.assertFalse(os.path.exists(os.path.join(self.sources.name, "B.mp3")))

    def test_nothing_to_convert_says_so(self):
        self.assertFalse(self.converter.start([]))
        self.assertEqual(self.said, ["Nothing to convert."])

    def test_one_file_picked_twice_is_converted_once(self):
        same = self.write("A.wav")
        with mock.patch("libs.music_bot.converter.subprocess.run",
                        self.fake_ffmpeg()):
            self.run_batch([same, same])
        self.assertIn("Converting 1 file to MP3, 192 kbps, "
                      "beside each original.", self.said)

    def test_a_second_batch_while_one_runs_is_refused(self):
        self.converter._worker = mock.Mock()
        self.converter._worker.is_alive.return_value = True
        self.assertFalse(self.converter.start(["C:\\a.wav"]))
        self.assertIn("already running", self.said[-1])

    def test_a_missing_ffmpeg_is_said_rather_than_crashed(self):
        converter = self.make_converter(ffmpeg_path=None)
        with mock.patch("libs.music_bot.media.FFMPEG_PATH", None):
            self.assertFalse(converter.start(["C:\\a.wav"]))
        self.assertEqual(self.said[-1],
                         "ffmpeg was not found, so files cannot be converted.")

    def test_the_progress_line_keeps_the_last_batch(self):
        only = self.write("A.wav")
        with mock.patch("libs.music_bot.converter.subprocess.run",
                        self.fake_ffmpeg()):
            self.run_batch([only])
        self.assertEqual(self.converter.progress_menu_label(),
                         "Conversion: Converted 1 file to MP3, 192 kbps.")
        self.converter.speak_status()
        self.assertIn("Converted 1 file", self.said[-1])


class TheSettingsTests(ConverterTestCase):
    def test_a_format_is_remembered_and_read_back(self):
        self.converter.set_format("flac")
        self.assertEqual(self.saved[FORMAT_OPTION], "flac")
        self.assertEqual(self.converter.format_menu_label(),
                         "Target Format: FLAC, lossless")
        self.assertIn("FLAC", self.said[-1])

    def test_a_format_nobody_has_is_ignored(self):
        self.converter.set_format("mp4")
        self.assertNotIn(FORMAT_OPTION, self.saved)
        self.assertEqual(self.converter.format_key(), DEFAULT_FORMAT_KEY)

    def test_the_format_cannot_change_under_a_running_batch(self):
        self.converter._worker = mock.Mock()
        self.converter._worker.is_alive.return_value = True
        self.converter.set_format("wav")
        self.assertNotIn(FORMAT_OPTION, self.saved)

    def test_the_default_output_is_beside_the_original(self):
        self.assertEqual(self.converter.output_menu_label(),
                         "Output: Next to the Original")
        self.assertFalse(self.converter.has_saved_folder())

    def test_a_chosen_folder_is_remembered_and_can_be_forgotten(self):
        self.converter.set_folder(self.out.name)
        self.assertEqual(self.saved[FOLDER_OPTION], os.path.abspath(self.out.name))
        self.assertTrue(self.converter.has_saved_folder())
        self.assertEqual(self.converter.output_menu_label(),
                         f"Output: {os.path.abspath(self.out.name)}")
        self.converter.clear_folder()
        self.assertEqual(self.saved[FOLDER_OPTION], "")
        self.assertEqual(self.converter.output_menu_label(),
                         "Output: Next to the Original")

    def test_a_folder_that_went_away_falls_back_to_the_original(self):
        self.saved[FOLDER_OPTION] = os.path.join(self.out.name, "gone")
        self.assertEqual(self.converter.configured_folder(), "")
        self.assertIn("unavailable", self.converter.output_menu_label())

    def test_announcements_can_be_turned_off(self):
        self.assertEqual(self.converter.announce_menu_label(),
                         "Announce Each File: On")
        self.converter.toggle_announce()
        self.assertEqual(self.converter.announce_menu_label(),
                         "Announce Each File: Off")


class ThePageTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(mock.patch.stopall)
        mock.patch("libs.music_bot.converter.speak",
                   lambda text, **kwargs: None).start()
        self.saved = {}
        mock.patch(
            "libs.music_bot.converter.options.get",
            lambda key, default=None: self.saved.get(key, default)).start()
        mock.patch(
            "libs.music_bot.converter.options.set",
            lambda key, value: self.saved.__setitem__(key, value)).start()
        self.page = ConverterMenus()
        self.page._say = lambda text, **kwargs: None
        self.gameplay = SimpleNamespace(
            substates=[],
            add_substate=lambda page: self.gameplay.substates.append(page),
            pop_last_substate=lambda: self.gameplay.substates.pop(),
        )
        self.bot = self.make_bot()
        self.page._bot = self.bot
        mock.patch("libs.menu.Menu", FakeMenu).start()
        mock.patch("libs.menus.set_default_sounds", lambda menu: None).start()

    def make_bot(self, picks=(), label=""):
        from libs.music_bot import MapMusicBot

        bot = MapMusicBot.__new__(MapMusicBot)
        bot.game = SimpleNamespace(put=lambda callback: callback())
        bot.picks = [dict(pick) for pick in picks]
        bot._find_gameplay = lambda: self.gameplay
        bot._show_mode_menu = lambda: None
        return bot

    def label_of(self, item):
        """A line's text, whether it is written down or built on demand.

        A label that is a stub method answers with a `Mock`, whose
        `startswith` is itself a truthy `Mock`: a test that searched the raw
        slots would match the first line of the page every time.
        """
        label = item[0]() if callable(item[0]) else item[0]
        return label if isinstance(label, str) else ""

    def labels(self):
        return [self.label_of(item) for item in FakeMenu.last.items]

    def line(self, prefix):
        for item in FakeMenu.last.items:
            if self.label_of(item).startswith(prefix):
                return item
        self.fail(f"no line starts with {prefix!r}: {self.labels()}")

    def test_the_page_holds_the_folder_the_format_and_the_three_doors(self):
        self.page._open_converter_menu()
        self.assertEqual(self.labels(), [
            "Output: Next to the Original",
            "Set Output Folder...",
            "Target Format: MP3, 192 kbps",
            "Convert Files I Choose...",
            "Convert a Folder of Files...",
            "Conversion: Nothing has been converted yet",
            "Back",
        ])
        for item in FakeMenu.last.items:
            self.assertGreaterEqual(len(item), 4)
            self.assertTrue(item[3].strip(), item[0])

    def test_a_locked_converter_says_whose_decision_it_was(self):
        said = []
        self.page._say = lambda text, **kwargs: said.append(text)
        with mock.patch("libs.music_bot.gate.blocked",
                        lambda feature_id: (said.append(feature_id), True)[1]):
            self.page._open_converter_menu()
        self.assertEqual(said, ["converter"])
        self.assertEqual(self.gameplay.substates, [])

    def test_the_page_opens_by_separating_the_output_from_the_choice(self):
        """Two folder pickers ask two questions; the intro says which is which."""
        self.page._open_converter_menu()
        intro = FakeMenu.last.intro
        self.assertIn("Output is where converted files go", intro)
        self.assertIn("which files to convert", intro)


    def test_a_saved_folder_adds_the_way_back_to_the_original(self):
        self.saved[FOLDER_OPTION] = tempfile.gettempdir()
        self.page._open_converter_menu()
        self.assertIn("Save Next to the Original", self.labels())

    def test_picks_are_a_source_only_when_there_are_some(self):
        self.page._open_converter_menu()
        self.assertNotIn("Convert Picks (2)...", self.labels())

        self.page._bot = self.make_bot(picks=[
            {"title": "A", "target": "C:\\songs\\A.wav", "source": "local"},
            {"title": "B", "target": "C:\\songs\\B.wav", "source": "local"},
        ])
        self.page._open_converter_menu()
        self.assertIn("Convert Picks (2)...", self.labels())

    def test_cancel_only_exists_while_a_batch_runs(self):
        self.page._open_converter_menu()
        self.assertNotIn("Cancel Conversion", self.labels())

        converter = self.page._bot._converter()
        converter._worker = mock.Mock()
        converter._worker.is_alive.return_value = True
        self.page._open_converter_menu()
        self.assertIn("Cancel Conversion", self.labels())

    def test_convert_picks_hands_the_targets_over_in_order(self):
        self.page._bot = self.make_bot(picks=[
            {"title": "B", "target": "C:\\songs\\B.wav", "source": "local"},
            {"title": "A", "target": "C:\\songs\\A.wav", "source": "local"},
        ])
        converter = mock.Mock()
        self.page._bot._converter = lambda: converter
        self.page._open_converter_menu()
        self.line("Convert Picks")[1]()
        converter.start.assert_called_once_with(["C:\\songs\\B.wav",
                                                 "C:\\songs\\A.wav"], "Picks")

    def test_the_settings_page_marks_the_format_in_use(self):
        self.page._open_converter_settings_menu()
        labels = self.labels()
        self.assertIn("MP3, 192 kbps (active)", labels)
        self.assertIn("FLAC, lossless", labels)
        self.assertIn("Announce Each File: On", labels)
        self.assertEqual(labels[-1], "Back")
        self.assertEqual(len(labels), len(FORMATS) + 2)

    def test_choosing_a_format_stays_on_the_page(self):
        self.page._open_converter_settings_menu()
        before = len(self.gameplay.substates)
        self.line("FLAC")[1]()
        self.assertEqual(self.saved[FORMAT_OPTION], "flac")
        self.assertEqual(len(self.gameplay.substates), before,
                         "a page of values closed itself")

    def test_the_file_dialog_asks_for_media_files_and_starts_the_batch(self):
        converter = mock.Mock()
        self.page._bot._converter = lambda: converter
        asked = {}

        def ask_files(title, initial="", pattern="*.*", **kwargs):
            asked["title"] = title
            asked["pattern"] = pattern
            return "C:\\a.wav\nC:\\b.flac\n", ""

        with mock.patch("libs.folder_dialog.ask_files", ask_files), \
                mock.patch("threading.Thread", _ImmediateThread):
            self.page._choose_converter_files()
        self.assertIn("*.mp3", asked["pattern"])
        converter.start.assert_called_once_with(["C:\\a.wav", "C:\\b.flac"],
                                                "your selection")

    def test_a_folder_of_files_is_scanned_not_a_classroom_of_them(self):
        controller = mock.Mock()
        self.page._bot._converter = lambda: controller
        with mock.patch("libs.folder_dialog.ask_folder",
                        lambda title, **kwargs: ("C:\\songs", "")), \
                mock.patch("libs.music_bot.track_source.music_files_in",
                           lambda folder: ["C:\\songs\\A.wav",
                                           "C:\\songs\\B.wav"]), \
                mock.patch("threading.Thread", _ImmediateThread):
            self.page._choose_converter_folder()
        controller.start.assert_called_once_with(
            ["C:\\songs\\A.wav", "C:\\songs\\B.wav"], "songs")

    def test_an_empty_folder_answers_instead_of_starting_nothing(self):
        converter = mock.Mock()
        self.page._bot._converter = lambda: converter
        said = []
        self.page._say = lambda text, **kwargs: said.append(text)
        with mock.patch("libs.folder_dialog.ask_folder",
                        lambda title, **kwargs: ("C:\\songs", "")), \
                mock.patch("libs.music_bot.track_source.music_files_in",
                           lambda folder: []), \
                mock.patch("threading.Thread", _ImmediateThread):
            self.page._choose_converter_folder()
        self.assertFalse(converter.start.called)
        self.assertEqual(said[-1], "No audio files were found in that folder.")


class TheFileToolStaysOutOfTheFrameLoopTests(unittest.TestCase):
    """No Tk here, and no shell in the middle of a batch.

    The dialogs are `libs/folder_dialog.py`'s child process, exactly as the
    recorder and the download center reach theirs, and ffmpeg is run from an
    argv list with no shell, so a path with a space or a Thai letter in it
    cannot be split into two arguments.
    """

    def source(self, relative):
        return (Path(__file__).resolve().parents[1] / relative).read_text(
            encoding="utf-8")

    def test_the_job_never_builds_tk(self):
        engine = self.source("libs/music_bot/converter.py")
        self.assertNotIn("tkinter", engine)
        self.assertNotIn("askdirectory", engine)

    def test_the_page_asks_through_the_dialog_helper(self):
        page = self.source("libs/music_bot/menu_pages/converter.py")
        self.assertNotIn("tkinter", page)
        self.assertIn("folder_dialog", page)

    def test_ffmpeg_is_an_argv_list_and_never_a_shell_string(self):
        engine = self.source("libs/music_bot/converter.py")
        self.assertIn("subprocess.run(", engine)
        self.assertNotIn("shell=True", engine)

    def test_the_mode_menu_offers_one_line_and_no_format_switches(self):
        menu = self.source("libs/music_bot/menus.py")
        self.assertEqual(menu.count('"Audio File Converter"'), 1)
        # Its own catalogue row in server/libs/music_bot_gate.ts, so the
        # Developer's /musicbotgate form lists it beside Recording.
        self.assertIn('("converter", "Audio File Converter"', menu)
        self.assertNotIn("Target Format:", menu)


if __name__ == "__main__":
    unittest.main()
