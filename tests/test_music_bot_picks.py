"""Choosing a folder of music, and the picks a player marks while listening.

Two staff doors that answer the same complaint -- "play music from this computer
without having to know how to select files":

* a folder is one answer, and everything in it is the batch (`music_files_in`,
  `TrackSourceHandler._choose_music_folder`);
* a song heard in passing can be marked where it plays, then the marked ones are
  copied into one folder afterwards (`MapMusicBot.toggle_current_pick`,
  `_copy_picks`, the Picks page).

The picker itself is the dialog helper's child process (see
`libs/folder_dialog`), so nothing here builds Tk; `ask_files` / `ask_folder` are
stood in for at the same seam the app calls them through.
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from libs import folder_dialog  # noqa: E402
from libs.music_bot import MapMusicBot  # noqa: E402
from libs.music_bot.track_source import (  # noqa: E402
    TrackSourceHandler, local_tracks, music_files_in)


class _ImmediateThread:
    """A Thread whose start() runs the dialog body now (no game loop here)."""

    def __init__(self, target, daemon=False):
        self._target = target

    def start(self):
        self._target()


def make_bot(**attributes):
    bot = MapMusicBot.__new__(MapMusicBot)
    bot.picks = []
    bot.current_title = ""
    bot.current_target = ""
    bot.current_source = ""
    bot.playing = False
    bot.game = SimpleNamespace(put=lambda callback: callback())
    for key, value in attributes.items():
        setattr(bot, key, value)
    return bot


class PickTests(unittest.TestCase):
    """Marking the song that is playing is what makes a cull-by-ear pass work."""

    def setUp(self):
        self.saved = []
        self.said = []
        self._patches = [
            mock.patch("libs.music_bot.controller.options.set",
                       lambda key, value: self.saved.append((key, value))),
            mock.patch("libs.speech.speak", lambda text, **kwargs: self.said.append(text)),
        ]
        for patch in self._patches:
            patch.start()

    def tearDown(self):
        for patch in self._patches:
            patch.stop()

    def playing_local(self, path="C:\\songs\\A.mp3", title="A"):
        return make_bot(current_source="local", current_target=path,
                        current_title=title, playing=True)

    def test_marking_a_local_file_keeps_it_and_says_how_many(self):
        bot = self.playing_local()
        self.assertTrue(bot.toggle_current_pick())
        self.assertEqual(bot.picks, [{"title": "A", "target": "C:\\songs\\A.mp3",
                                      "source": "local"}])
        self.assertEqual(self.saved, [("music_bot_picks", bot.picks)])
        self.assertEqual(self.said, ["Picked: A. 1 picked."])

    def test_marking_the_same_file_again_unpicks_it(self):
        bot = self.playing_local()
        bot.toggle_current_pick()
        self.assertFalse(bot.toggle_current_pick())
        self.assertEqual(bot.picks, [])
        self.assertEqual(self.said, ["Picked: A. 1 picked.",
                                     "Unpicked: A. 0 picked."])

    def test_a_song_from_the_internet_is_refused_with_the_reason(self):
        """A pick that could never leave the game is worse than a refusal."""
        bot = make_bot(current_source="youtube",
                       current_target="https://youtu.be/x", playing=True)
        self.assertFalse(bot.toggle_current_pick())
        self.assertEqual(bot.picks, [])
        self.assertEqual(self.said, ["Only a file from this computer can be picked."])
        self.assertEqual(self.saved, [])

    def test_nothing_playing_is_said_plainly(self):
        bot = make_bot()
        bot.toggle_current_pick()
        self.assertEqual(self.said, ["Nothing is playing."])

    def test_clearing_forgets_the_picks_but_not_the_files(self):
        bot = self.playing_local()
        bot.toggle_current_pick()
        bot.clear_picks()
        self.assertEqual(bot.picks, [])
        self.assertEqual(self.saved[-1], ("music_bot_picks", []))
        self.assertEqual(self.said[-1], "Picks cleared.")


class CopyPicksTests(unittest.TestCase):
    """A copy, never a move: a folder chosen by mistake must cost nothing."""

    def setUp(self):
        self.said = []
        self._patches = [mock.patch("libs.speech.speak",
                                    lambda text, **kwargs: self.said.append(text))]
        for patch in self._patches:
            patch.start()
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.target_folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.target_folder.cleanup)

    def tearDown(self):
        for patch in self._patches:
            patch.stop()

    def write(self, name, text="audio"):
        path = os.path.join(self.folder.name, name)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        return path

    def test_every_pick_is_copied_and_the_original_stays(self):
        first = self.write("A.mp3")
        second = self.write("B.flac")
        bot = make_bot(picks=[{"title": "A", "target": first, "source": "local"},
                              {"title": "B", "target": second, "source": "local"}])

        copied = bot._copy_picks(self.target_folder.name)

        self.assertEqual(copied, 2)
        self.assertTrue(os.path.isfile(os.path.join(self.target_folder.name, "A.mp3")))
        self.assertTrue(os.path.isfile(os.path.join(self.target_folder.name, "B.flac")))
        self.assertTrue(os.path.isfile(first), "the original was moved, not copied")
        self.assertEqual(self.said, ["Copied 2 files."])

    def test_a_pick_that_is_gone_is_reported_and_the_rest_still_copy(self):
        present = self.write("A.mp3")
        bot = make_bot(picks=[
            {"title": "Gone", "target": os.path.join(self.folder.name, "gone.mp3"),
             "source": "local"},
            {"title": "A", "target": present, "source": "local"},
        ])

        self.assertEqual(bot._copy_picks(self.target_folder.name), 1)
        self.assertEqual(self.said, ["Copied 1 file, 1 missing."])

    def test_one_file_reads_as_one_file(self):
        only = self.write("A.mp3")
        bot = make_bot(picks=[{"title": "A", "target": only, "source": "local"}])
        bot._copy_picks(self.target_folder.name)
        self.assertEqual(self.said, ["Copied 1 file."])


class PicksAreRememberedTests(unittest.TestCase):
    """The options file is the only memory: a restart must not lose the cull."""

    def test_the_load_drops_anything_that_is_not_a_pick(self):
        source = (Path(__file__).resolve().parents[1]
                  / "libs" / "music_bot" / "controller.py").read_text(encoding="utf-8")
        self.assertIn('options.get("music_bot_picks", [])', source)
        self.assertIn('isinstance(pick, dict) and pick.get("target")', source)

    def test_a_saved_pick_survives_a_save_and_load_round_trip(self):
        picks = [{"title": "A", "target": "C:\\songs\\A.mp3", "source": "local"}]
        stored = {}
        bot = make_bot(picks=[dict(pick) for pick in picks], current_source="local",
                       current_target="C:\\songs\\A.mp3", current_title="A",
                       playing=True)
        with mock.patch("libs.music_bot.controller.options.set",
                        lambda key, value: stored.update({key: value})), \
                mock.patch("libs.speech.speak", lambda text, **kwargs: None):
            bot.toggle_current_pick()          # unmark
            bot.toggle_current_pick()          # mark again
        self.assertEqual(stored["music_bot_picks"], picks)


class FolderScanTests(unittest.TestCase):
    """What a folder contributes: its own files, by name, and nothing deeper."""

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)

    def touch(self, name):
        path = os.path.join(self.folder.name, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("x")
        return path

    def test_only_supported_media_in_name_order(self):
        b = self.touch("b.flac")
        a = self.touch("A.MP3")
        self.touch("notes.txt")
        self.touch("art.jpg")
        self.assertEqual(music_files_in(self.folder.name), [a, b])

    def test_a_subfolder_is_not_walked_into(self):
        inner = self.touch(os.path.join("album", "c.mp3"))
        outer = self.touch("d.mp3")
        self.assertEqual(music_files_in(self.folder.name), [outer])
        self.assertTrue(os.path.isfile(inner))

    def test_a_folder_that_is_gone_is_an_empty_answer(self):
        self.assertEqual(music_files_in(os.path.join(self.folder.name, "nope")), [])

    def test_the_folder_becomes_the_batch(self):
        second = self.touch("2.mp3")
        first = self.touch("1.mp3")
        self.assertEqual([track["target"] for track in local_tracks(music_files_in(
            self.folder.name))], [first, second])


class ChooseFolderTests(unittest.TestCase):
    """One Enter on a folder is the whole selection."""

    def run_choice(self, answer):
        bot = make_bot()
        scheduled = []
        bot._start_local_file_stream = (
            lambda path, title: scheduled.append(("file", path, title)))
        bot._start_track_queue = (
            lambda tracks, label: scheduled.append(("queue", tracks, label)))
        with mock.patch("libs.folder_dialog.ask_folder", lambda *a, **k: answer), \
                mock.patch("libs.music_bot.track_source.threading.Thread",
                           _ImmediateThread), \
                mock.patch("libs.music_bot.track_source.speak"):
            TrackSourceHandler(bot)._choose_music_folder()
        return scheduled

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)

    def touch(self, name):
        path = os.path.join(self.folder.name, name)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("x")
        return path

    def test_a_folder_of_songs_becomes_one_local_batch(self):
        second = self.touch("B.mp3")
        first = self.touch("A.mp3")
        scheduled = self.run_choice((self.folder.name, ""))
        self.assertEqual(scheduled, [("queue", [
            {"title": "A", "target": first, "source": "local"},
            {"title": "B", "target": second, "source": "local"},
        ], "Local files")])

    def test_a_cancelled_or_empty_folder_starts_nothing(self):
        self.assertEqual(self.run_choice(("", "")), [])
        self.assertEqual(self.run_choice((self.folder.name, "")), [])


class TheDialogHelperAsksForFilesTests(unittest.TestCase):
    """`ask_files` is the multi-select half of the helper (see folder_dialog)."""

    def test_the_answer_is_one_path_per_line(self):
        """A path with a space in it stays one path: the caller splits lines."""
        import io

        class _Process:
            def __init__(self):
                self.stdin = io.StringIO()
                self.stdout = io.BytesIO(
                    b"C:\\my music\\A.mp3\nC:\\songs\\B.flac")

            def wait(self, **kwargs):
                return 0

        with mock.patch.object(folder_dialog.subprocess, "Popen",
                               lambda *args, **kwargs: _Process()):
            paths, problem = folder_dialog.ask_files("Choose", pattern="*.mp3")
        self.assertEqual(problem, "")
        self.assertEqual(paths.splitlines(),
                         ["C:\\my music\\A.mp3", "C:\\songs\\B.flac"])

    def test_the_pattern_is_the_callers_own(self):
        seen = []

        def popen(command, **kwargs):
            seen.append(command)
            raise OSError("stop here: the command was what this test wanted")

        with mock.patch.object(folder_dialog.subprocess, "Popen", popen):
            _paths, problem = folder_dialog.ask_files("Choose", pattern="*.flac *.mp3")
        self.assertIn("*.flac *.mp3", seen[0])
        self.assertIn(folder_dialog.FILES, seen[0])
        self.assertTrue(problem)


class PicksMenuTests(unittest.TestCase):
    """The page is the feature's only door, so it is opened here.

    A menu that raises only when a key is pressed is the failure mode the shim
    test exists for; this one walks the page itself: the title carries the
    count, the picks are lines, and Enter on one queues the rest behind it.
    """

    class FakeMenu:
        last = None

        def __init__(self, game, title, parrent=None):
            self.title = title
            self.items = []
            PicksMenuTests.FakeMenu.last = self

        def add_items(self, items):
            self.items = items

        def speak_current_item(self):
            pass

    def open_page(self, picks):
        from libs.music_bot.menu_pages.picks import PicksMenus

        gameplay = SimpleNamespace(
            substates=[],
            add_substate=lambda page: gameplay.substates.append(page),
            pop_last_substate=lambda: gameplay.substates.pop(),
        )
        bot = make_bot(picks=picks)
        bot._find_gameplay = lambda: gameplay
        started = []
        bot._start_track_queue = lambda tracks, label: started.append((tracks, label))
        handler = PicksMenus()
        handler._bot = bot
        handler._say = lambda text, **kwargs: None
        with mock.patch("libs.menu.Menu", self.FakeMenu),                 mock.patch("libs.menus.set_default_sounds", lambda menu: None):
            handler._open_picks_menu()
        return self.FakeMenu.last, started

    def picks(self):
        return [{"title": "A", "target": "C:\songs\A.mp3", "source": "local"},
                {"title": "B", "target": "C:\songs\B.mp3", "source": "local"}]

    def test_the_page_lists_the_picks_then_the_actions(self):
        menu, _started = self.open_page(self.picks())
        labels = [item[0] for item in menu.items]
        self.assertEqual(labels, ["1. A", "2. B", "Copy Picks to a Folder...",
                                  "Clear Picks", "Back"])

    def test_enter_on_a_pick_queues_it_and_the_ones_below_it(self):
        menu, started = self.open_page(self.picks())
        menu.items[1][1]()                 # the action of "2. B"
        self.assertEqual(started, [([{"title": "B", "target": "C:\songs\B.mp3",
                                      "source": "local"}], "Picks")])

    def test_an_empty_page_says_how_to_fill_it(self):
        menu, _started = self.open_page([])
        self.assertEqual(menu.items[0][0], "Nothing picked yet")
        self.assertIn("Mark a local file", menu.items[0][3])


class TheMarkKeyTests(unittest.TestCase):
    """The key that marks the file that is playing, and what it must not eat.

    Ctrl+F12, because F12 is the only key left: every letter answers to
    something, all of M's chords belong to `music_bot_control` (plain opens,
    Shift pauses, Ctrl stops, Ctrl+Shift speaks the status, Alt mutes to
    others) and F1-F11 have jobs of their own. The action is its own keyconfig
    name, so a player can move the key under the Ctrl -- and whatever it is
    moved to, the presses below must still behave.
    """

    def gameplay(self, substates=(), bot="bot", allowed=True, blocked=False):
        from libs.gameplay import Gameplay

        picked = []
        pressed = []
        self_gameplay = Gameplay.__new__(Gameplay)
        self_gameplay.substates = list(substates)
        self_gameplay.music_bot = (
            SimpleNamespace(toggle_current_pick=lambda: picked.append(True))
            if bot == "bot" else None)
        self_gameplay._can_use_music_bot = lambda: allowed
        self.gameplay_gate = mock.patch(
            "libs.gameplay.music_bot_gate.blocked",
            lambda feature_id: (pressed.append(feature_id), blocked)[1])
        self.gameplay_gate.start()
        self.addCleanup(self.gameplay_gate.stop)
        return self_gameplay, picked, pressed

    def test_the_default_is_a_key_nothing_else_answers_to(self):
        source = (Path(__file__).resolve().parents[1] / "libs" / "gameplay.py").read_text(
            encoding="utf-8")
        self.assertEqual(source.count("pygame.K_F12"), 1)
        self.assertIn('kc.get("music_bot_mark_pick", pygame.K_F12)', source)
        self.assertIn("if not mod & pygame.KMOD_CTRL:", source)
        # The chords it could have used are somebody else's.
        self.assertIn('kc.get("music_bot_toggle", pygame.K_m)', source)

    def test_the_chord_marks_the_file_that_is_playing(self):
        import pygame
        gameplay, picked, pressed = self.gameplay()
        gameplay.music_bot_mark_pick(pygame.KMOD_CTRL)
        self.assertEqual(picked, [True])
        self.assertEqual(pressed, ["picks"])

    def test_a_bare_press_of_the_key_does_nothing(self):
        """Ctrl is the chord: F12 on its own belongs to nothing."""
        import pygame
        gameplay, picked, pressed = self.gameplay()
        gameplay.music_bot_mark_pick(0)
        self.assertEqual(picked, [])
        self.assertEqual(pressed, [])

    def test_the_chord_still_marks_with_another_modifier_held(self):
        import pygame
        gameplay, picked, _pressed = self.gameplay()
        gameplay.music_bot_mark_pick(pygame.KMOD_CTRL | pygame.KMOD_SHIFT)
        self.assertEqual(picked, [True])

    def test_a_menu_owns_the_keys_while_it_is_open(self):
        import pygame
        gameplay, picked, _pressed = self.gameplay(substates=[object()])
        gameplay.music_bot_mark_pick(pygame.KMOD_CTRL)
        self.assertEqual(picked, [])

    def test_a_locked_music_bot_marks_nothing(self):
        import pygame
        gameplay, picked, pressed = self.gameplay(blocked=True)
        gameplay.music_bot_mark_pick(pygame.KMOD_CTRL)
        self.assertEqual(picked, [])
        self.assertEqual(pressed, ["picks"])

    def test_a_player_who_may_not_use_the_bot_is_ignored(self):
        import pygame
        gameplay, picked, _pressed = self.gameplay(allowed=False)
        gameplay.music_bot_mark_pick(pygame.KMOD_CTRL)
        self.assertEqual(picked, [])

    def test_a_gameplay_without_a_bot_does_not_raise(self):
        import pygame
        gameplay, picked, _pressed = self.gameplay(bot=None)
        gameplay.music_bot_mark_pick(pygame.KMOD_CTRL)
        self.assertEqual(picked, [])

    def test_the_mark_and_the_picks_page_have_their_own_catalogue_id(self):
        """`picks` is a row the Developer can lock on its own.

        It used to ride on `search` (the key) and `playlists` (the page), so a
        Developer who locked Search also lost the mark key with no line of its
        own in the form to explain it.
        """
        root = Path(__file__).resolve().parents[1]
        menu = (root / "libs" / "music_bot" / "menus.py").read_text(
            encoding="utf-8")
        self.assertIn('("picks", mark_label', menu)
        self.assertIn('("picks", self._bot.picks_label', menu)
        gameplay = (root / "libs" / "gameplay.py").read_text(encoding="utf-8")
        self.assertIn('music_bot_gate.blocked("picks")', gameplay)
        catalogue = (root.parent / "server" / "libs" / "music_bot_gate.ts").read_text(
            encoding="utf-8")
        self.assertIn('id: "picks"', catalogue)


class TheGameNeverBuildsTkForThisEitherTests(unittest.TestCase):
    def test_the_file_picker_is_the_helper_process(self):
        source = (Path(__file__).resolve().parents[1]
                  / "libs" / "music_bot" / "track_source.py").read_text(encoding="utf-8")
        self.assertNotIn("tkinter", source)
        self.assertNotIn("askopenfilenames", source)
        self.assertIn("folder_dialog", source)
        self.assertIn("ask_files", source)
