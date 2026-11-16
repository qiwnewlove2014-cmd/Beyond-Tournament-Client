"""The per-player "hide a Music Bot line" preference, and the page that undoes it.

`client/libs/music_bot/menu_prefs.py` is a listening preference of one machine,
keyed on the same id every line already carries for the Developer gate. These
tests pin the parts that make it safe: a hidden line is not built, a Developer
lock wins, `Back`/`Help`/`Lines I hide` can never be hidden, and the page that
offers lines back is built from what is stored rather than from a list of
features (so a new feature costs no edit here or there).

No game, OpenAL, ffmpeg or network is needed.
"""

import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from libs.music_bot import gate, menu_prefs
from libs.music_bot.menu_pages.settings import SettingsMenus

HERE = Path(__file__).resolve().parent
CLIENT = HERE.parent


def norm(text):
    return str(text or "").strip().lower()


class StoreCase(unittest.TestCase):
    """Every test starts with nothing stored, and the store is a dict in memory."""

    def setUp(self):
        self.stored = {}
        self.addCleanup(gate.clear)
        self._get = mock.patch.object(
            menu_prefs.options, "get",
            side_effect=lambda key, default=None: self.stored.get(key, default),
        ).start()
        self.addCleanup(mock.patch.stopall)
        self._set = mock.patch.object(
            menu_prefs.options, "set",
            side_effect=lambda key, value: self.stored.__setitem__(key, value),
        ).start()


class TheHiddenSetTests(StoreCase):
    def test_hiding_one_line_keeps_it_and_the_words_it_had(self):
        self.assertTrue(menu_prefs.hide("seek", "Seek"))
        self.assertTrue(menu_prefs.is_hidden("seek"))
        self.assertEqual(menu_prefs.hidden(), {"seek": "Seek"})
        self.assertEqual(menu_prefs.count(), 1)
        self.assertEqual(menu_prefs.label_of("seek"), "Seek")
        self.assertTrue(menu_prefs.show("seek"))
        self.assertFalse(menu_prefs.is_hidden("seek"))
        self.assertEqual(menu_prefs.count(), 0)

    def test_showing_a_line_that_was_never_hidden_says_so(self):
        self.assertFalse(menu_prefs.show("seek"))

    def test_toggling_answers_whether_it_is_hidden_afterwards(self):
        self.assertTrue(menu_prefs.toggle("eq", "Equalizer"))
        self.assertTrue(menu_prefs.is_hidden("eq"))
        self.assertFalse(menu_prefs.toggle("eq", "Equalizer"))
        self.assertFalse(menu_prefs.is_hidden("eq"))

    def test_an_unreadable_preference_hides_nothing(self):
        # Failing open is the whole safety direction: the full menu, never an
        # empty one nobody can explain.
        with mock.patch.object(menu_prefs.options, "get",
                               side_effect=RuntimeError("unreadable")):
            self.assertEqual(menu_prefs.hidden_ids(), frozenset())
            self.assertEqual(menu_prefs.count(), 0)

    def test_a_line_with_no_id_is_never_hideable(self):
        # `Back`, `Help`, `Music Bot Settings` and `Lines I hide` all name None,
        # which is exactly what keeps the way back open.
        self.assertFalse(menu_prefs.may_hide(None))
        self.assertFalse(menu_prefs.hide(None, "Back"))
        self.assertTrue(menu_prefs.may_hide("seek"))

    def test_show_everything_answers_how_many_came_back(self):
        menu_prefs.hide("seek", "Seek")
        menu_prefs.hide("eq", "Equalizer")
        self.assertEqual(menu_prefs.show_everything(), 2)
        self.assertEqual(menu_prefs.count(), 0)


class FilteringTests(StoreCase):
    def test_a_hidden_line_is_not_built_and_the_others_keep_their_help(self):
        menu_prefs.hide("seek", "Seek")
        kept = menu_prefs.filter_items([
            ("seek", "Seek", object(), "Jumps inside the track."),
            (None, "Back", object(), "Returns to the Music Bot menu."),
        ])
        self.assertEqual([label for label, *_rest in kept],
                         ["Back"])
        # Four fields, with the description past the preview slot: a help
        # sentence in the demo slot would be decoded and played as a sound.
        self.assertEqual(len(kept[0]), 4)
        self.assertIsNone(kept[0][2])
        self.assertEqual(kept[0][3], "Returns to the Music Bot menu.")

    def test_build_adds_the_lines_and_keeps_their_ids_in_step(self):
        menu = FakeMenu()
        menu_prefs.hide("eq", "Equalizer")
        menu_prefs.build(menu, [
            ("seek", "Seek", object(), "Jumps inside the track."),
            ("eq", "Equalizer", object(), "Bass, mid and treble."),
            (None, "Back", object(), "Goes back."),
        ])
        self.assertEqual([norm(item[0]) for item in menu.items], ["seek", "back"])
        self.assertEqual(menu.line_ids, ["seek", None])
        self.assertTrue(callable(menu.hide_current_line))

    def test_a_developer_lock_wins_over_a_player_who_shows_everything(self):
        # Two filters, one order: the gate first, so hiding can only ever take
        # more away than staff already took.
        gate.apply_locked({"seek": "Seek"})
        menu = FakeMenu()
        menu_prefs.build(menu, [
            ("seek", "Seek", object(), "Jumps inside the track."),
            ("eq", "Equalizer", object(), "Bass, mid and treble."),
        ])
        self.assertEqual([norm(item[0]) for item in menu.items], ["equalizer"])


class TheSpeechItUsesTests(unittest.TestCase):
    def test_it_speaks_through_the_client_and_not_a_silent_stand_in(self):
        """A wrong relative import lands on the fallback, which says nothing.

        `libs.music_bot.menu_prefs` is one level above the `menu_pages/` pages,
        so it needs `from ..speech import speak`; three dots pointed at the
        client root, raised ImportError, and every hide went silent while the
        line itself was hidden correctly.
        """
        self.assertEqual(menu_prefs.speak.__module__, "libs.speech")


class CtrlHTests(StoreCase):
    def _menu(self, items, ids):
        menu = FakeMenu()
        menu.add_items(items)
        menu.line_ids = list(ids)
        menu.pos = 0
        return menu

    def test_the_line_the_player_is_on_is_put_away_and_named_back(self):
        menu = self._menu(
            [("Search and play", object(), None, "Searches."),
             ("Volume", object(), None, "The bot's own volume.")],
            ["search", "volume"],
        )
        with mock.patch.object(menu_prefs, "speak") as speak:
            self.assertTrue(menu_prefs.hide_current(menu))
        self.assertTrue(menu_prefs.is_hidden("search"))
        self.assertEqual(menu_prefs.label_of("search"), "Search and play")
        self.assertEqual([norm(item[0]) for item in menu.items], ["volume"])
        self.assertEqual(menu.line_ids, ["volume"])
        said = speak.call_args_list[0][0][0]
        self.assertIn("Hidden: Search and play", said)
        self.assertIn("Lines I hide", said)

    def test_hiding_says_where_the_focus_landed_in_one_sentence(self):
        """The line that slides up must not be the only thing the player hears.

        Deleting an item leaves the cursor at the same index, pointing at a
        different line, so "the cursor did not move" and "the cursor moved"
        sound identical without the index and the count.
        """
        menu = self._menu(
            [("Search and play", object(), None, "Searches."),
             ("Volume", object(), None, "The bot's own volume."),
             ("Muffle", object(), None, "Muffles the song.")],
            ["search", "volume", "muffle"],
        )
        menu.pos = 1
        with mock.patch.object(menu_prefs, "speak") as speak, \
                mock.patch.object(menu, "speak_current_item") as speak_item:
            self.assertTrue(menu_prefs.hide_current(menu))
        # One utterance, not two: a second `speak` cuts the first off mid-word.
        self.assertEqual(len(speak.call_args_list), 1)
        speak_item.assert_not_called()
        said = speak.call_args[0][0]
        self.assertEqual(menu.pos, 1)
        self.assertIn("Hidden: Volume.", said)
        self.assertIn("2 lines left", said)
        self.assertIn("Now on line 2 of 2: Muffle", said)

    def test_a_line_that_must_stay_says_so_instead_of_going_away(self):
        menu = self._menu([("Back", object(), None, "Goes back.")], [None])
        with mock.patch.object(menu_prefs, "speak") as speak:
            self.assertFalse(menu_prefs.hide_current(menu))
        self.assertEqual(len(menu.items), 1)
        self.assertIn("cannot be hidden", speak.call_args[0][0])

    def test_hiding_the_last_line_leaves_a_sentence_rather_than_a_dead_menu(self):
        menu = self._menu([("Muffle", object(), None, "Muffles the song.")],
                          ["muffle"])
        with mock.patch.object(menu_prefs, "speak") as speak, \
                mock.patch.object(menu, "speak_current_item") as speak_item:
            self.assertTrue(menu_prefs.hide_current(menu))
        speak_item.assert_not_called()
        self.assertIn("Every line on this page is hidden",
                      [call[0][0] for call in speak.call_args_list][-1])

    def test_the_key_only_answers_a_menu_that_offers_it(self):
        # The hook is the menu's own handler, so every other menu in the game
        # keeps Ctrl+H for whatever it had it for (usually nothing).
        menu = self._menu([("Back", object(), None, "Goes back.")], [None])
        menu.hide_current_line = None
        self.assertFalse(callable(menu.hide_current_line))


class FakeMenu:
    """Drop-in for `libs.menu.Menu`, capturing the items it was built with."""

    def __init__(self):
        self.items = []
        self.pos = 0

    def add_items(self, items):
        self.items.extend(items)

    def speak_current_item(self):
        pass


class TheLinesIHidePageTests(StoreCase):
    def _bot(self):
        gp = SimpleNamespace(
            add_substate=lambda menu: made.append(menu),
            pop_last_substate=lambda: None,
        )
        made = []
        bot = SimpleNamespace(
            game=object(),
            _find_gameplay=lambda: gp,
        )
        bot.add_substate = gp.add_substate
        return bot, made

    def _open(self):
        made = []

        class Capturing(FakeMenu):
            def __init__(self, *args, **kwargs):
                super().__init__()
                made.append(self)

        handler = self._handler()
        with mock.patch("libs.menu.Menu", Capturing), \
                mock.patch("libs.menus.set_default_sounds"):
            handler._open_hidden_lines_menu()
        return made[-1], handler

    def _handler(self):
        bot = SimpleNamespace(
            game=object(),
            _find_gameplay=lambda: SimpleNamespace(
                add_substate=lambda menu: None,
                pop_last_substate=lambda: None,
            ),
        )

        class Handler(SettingsMenus):
            def __init__(self, owner):
                self._bot = owner
                self.said = []

            def _say(self, text):
                self.said.append(text)

        return Handler(bot)

    def test_a_hidden_line_is_one_press_away_from_coming_back(self):
        menu_prefs.hide("seek", "Seek")
        menu_prefs.hide("eq", "Equalizer")
        menu, _handler = self._open()
        labels = [norm(item[0]) for item in menu.items]
        self.assertEqual(labels, ["show equalizer", "show seek",
                                  "show every line again", "back"])

    def test_an_empty_list_says_which_key_hides_a_line(self):
        menu, _handler = self._open()
        labels = [norm(item[0]) for item in menu.items]
        self.assertEqual(labels, ["nothing is hidden", "back"])
        with mock.patch.object(menu_prefs, "speak"):
            menu.items[0][1]()
        self.assertEqual(len(menu.items), 2)

    def test_restoring_one_line_leaves_the_rest_where_they_are(self):
        menu_prefs.hide("seek", "Seek")
        menu_prefs.hide("eq", "Equalizer")
        menu, handler = self._open()
        restore = next(item[1] for item in menu.items
                       if norm(item[0]) == "show seek")
        _RecordingMenu.built = []
        with mock.patch("libs.menu.Menu", _RecordingMenu), \
                mock.patch("libs.menus.set_default_sounds"):
            restore()
        self.assertFalse(menu_prefs.is_hidden("seek"))
        self.assertTrue(menu_prefs.is_hidden("eq"))
        # The sentence rides in the page's own intro, so the new page's title
        # cannot cut it off, and it names the row the cursor lands on.
        rebuilt = _RecordingMenu.built[-1]
        self.assertIn("Shown again: Seek.", rebuilt.intro)
        self.assertIn("1 line still hidden", rebuilt.intro)
        self.assertIn("Now on line 1 of 1: Show Equalizer", rebuilt.intro)
        self.assertEqual(rebuilt.pos, 0)

    def test_restoring_lands_on_the_row_that_took_its_place(self):
        for feature_id, label in (("eq", "Equalizer"), ("muffle", "Muffle"),
                                  ("seek", "Seek")):
            menu_prefs.hide(feature_id, label)
        menu, _handler = self._open()
        restore = next(item[1] for item in menu.items
                       if norm(item[0]) == "show seek")
        _RecordingMenu.built = []
        with mock.patch("libs.menu.Menu", _RecordingMenu), \
                mock.patch("libs.menus.set_default_sounds"):
            restore()
        rebuilt = _RecordingMenu.built[-1]
        self.assertEqual([norm(item[0]) for item in rebuilt.items],
                         ["show equalizer", "show muffle",
                          "show every line again", "back"])
        self.assertEqual(rebuilt.pos, 1)
        self.assertIn("Now on line 2 of 2: Show Muffle", rebuilt.intro)

    def test_every_line_again_empties_the_store(self):
        menu_prefs.hide("seek", "Seek")
        menu_prefs.hide("eq", "Equalizer")
        menu, handler = self._open()
        restore_all = next(item[1] for item in menu.items
                           if norm(item[0]) == "show every line again")
        _RecordingMenu.built = []
        with mock.patch("libs.menu.Menu", _RecordingMenu), \
                mock.patch("libs.menus.set_default_sounds"):
            restore_all()
        self.assertEqual(menu_prefs.count(), 0)
        rebuilt = _RecordingMenu.built[-1]
        self.assertIn("Every line is back: 2 lines shown again.", rebuilt.intro)
        self.assertIn("Nothing is hidden now.", rebuilt.intro)
        # Still on this page, not dropped on Settings at the top.
        self.assertEqual(rebuilt.title, "Lines I hide")


class _RecordingMenu(FakeMenu):
    built = []

    def __init__(self, *args, **kwargs):
        super().__init__()
        self.title = args[1] if len(args) > 1 else kwargs.get("title", "")
        self.intro = kwargs.get("intro", "")
        _RecordingMenu.built.append(self)


class ThePagesAdoptOneCallTests(unittest.TestCase):
    """A new line needs no edit here: the pages build through one function."""

    def _source(self, relative):
        return (CLIENT / relative).read_text(encoding="utf-8")

    def test_the_music_bot_menu_and_the_settings_page_build_through_it(self):
        main = self._source("libs/music_bot/menus.py")
        self.assertIn("menu_prefs.build(m,", main)
        self.assertNotIn("gate.filter_items([", main)
        settings = self._source("libs/music_bot/menu_pages/settings.py")
        self.assertIn("menu_prefs.build(m,", settings)
        self.assertNotIn("gate.filter_items([", settings)

    def test_the_page_lists_that_hand_a_player_feature_lines_build_through_it(self):
        """The pages where hiding a line is what a player wants.

        Party Sync, the favorites and download lists and the voice boards are
        not here: their lines are a live session, the player's own songs or a
        row per instrument, none of which is a feature to switch off.
        """
        for relative in (
            "libs/music_bot/menu_pages/recording.py",
            "libs/music_bot/menu_pages/subtitles.py",
            "libs/music_bot/menu_pages/picks.py",
            "libs/music_bot/menu_pages/converter.py",
            "libs/music_bot/menu_pages/library.py",
            "libs/music_bot/menu_pages/external.py",
        ):
            source = self._source(relative)
            self.assertIn("menu_prefs.build(m,", source, relative)
            self.assertIn("from .. import menu_prefs", source, relative)

    def test_the_help_page_names_the_key(self):
        """A key nobody is told about is a key nobody uses."""
        source = self._source("libs/music_bot/menus.py")
        help_page = source.split("def _show_help_menu(self):", 1)[1]
        help_page = help_page.split("def _show_results_menu", 1)[0]
        self.assertIn('("Ctrl + H: Hide the line you are on"', help_page)
        self.assertIn("Lines I hide", help_page)
        self.assertIn("keeps its keys", help_page)

    def test_the_key_hook_lives_in_the_menu_and_not_in_the_music_bot(self):
        source = self._source("libs/menu.py")
        self.assertIn('callable(getattr(self, "hide_current_line", None))', source)
        self.assertIn("pg.KMOD_CTRL", source)


if __name__ == "__main__":
    unittest.main()
