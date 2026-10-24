"""Tab, in the Music Bot: what the line you are on actually does.

A Music Bot label has to stay short enough to arrow past -- "Auto-Tune Scale: C
Harmonic Minor" names the setting and explains nothing -- so the explanation
rides beside the line as the fourth field of the menu item, and Tab speaks the
one the cursor is on (`libs/menu.speak_current_help`).

What this file pins is the rule the Music Bot holds itself to: **every line of
every feature menu carries a description**, and the description says what a
player needs to know -- what it changes, what they will hear, and what it costs.
A line added without one is a line only its author can use, and Tab is where a
player goes when the name is all they have.

The scale list is the worked example: "Harmonic Minor" is a name, and "a minor
key with its seventh raised a semitone..." is the reason somebody picks it.

No game, audio device, network or OpenAL is required.
"""

import os
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

import pygame

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from libs import menu as menu_module  # noqa: E402
from libs.menu import Menu  # noqa: E402
from libs.music_bot import autotune  # noqa: E402
from libs.music_bot import gate as music_bot_gate  # noqa: E402
from libs.music_bot import menus as menus_module  # noqa: E402


class FakeMenu:
    """The menu builder's own record of what it would have shown."""

    built = []

    def __init__(self, game, title, **kwargs):
        self.game = game
        self.title = title
        self.intro = kwargs.get("intro", "")
        self.items = []
        FakeMenu.built.append(self)

    def add_items(self, items):
        self.items.extend(items)

    def speak_current_item(self):
        return None


class _Game:
    """Enough of a game for a real Menu: the open sound is all it touches."""

    def __init__(self):
        self.direct_soundgroup = SimpleNamespace(
            play=lambda *args, **kwargs: None)


def make_bot():
    bot = mock.MagicMock()
    bot.queue_mode = False
    bot.next_up_queue = []
    bot.transpose_semitones = 0
    bot.speech_transpose_semitones = 0
    bot.transpose_label.return_value = "Original key"
    bot.speech_transpose_label.return_value = "Original key"
    bot.output_device_label.return_value = "Same as the game"
    bot._eq_profile_label.return_value = "Normal (Flat)"
    bot.water_muffle_enabled = True
    bot.reverb_enabled = True
    bot.crossfade_enabled = False
    bot.autotune_enabled = False
    bot.autotune_speed = autotune.DEFAULT_SPEED
    bot.autotune_mode = autotune.DEFAULT_MODE
    bot.autotune_root = autotune.DEFAULT_ROOT
    bot.autotune_label.return_value = "OFF"
    bot.autotune_speed_label.return_value = autotune.speed_label()
    bot.autotune_scale_label.return_value = autotune.scale_label()
    bot.subtitle_label.return_value = "Subtitles: OFF"
    bot.cinema_routing_allowed.return_value = True
    bot.cinema_listening_allowed.return_value = True
    bot.cinema_target_label = "Cinema Speakers: none"
    bot.cinema_rooms_label = "Cinema Rooms: ON"
    bot.instruments_cinema_label = "Instruments in Rooms: ON"
    bot.speech_cinema_label = "Voice in Rooms: ON"
    bot.own_sound_label = "Your Sound: your body"
    bot._find_gameplay.return_value = SimpleNamespace(
        add_substate=lambda item: None,
        pop_last_substate=lambda: None,
        can_broadcast_megaphone=True,
    )
    # The recorder is a real object in the game and its labels are built from
    # the settings, so the fake answers the way it does: a fade of 0 is Off.
    recorder = mock.MagicMock()
    recorder.fade_in_seconds.return_value = 0.0
    recorder.fade_out_seconds.return_value = 0.0
    recorder.fade_time_text.side_effect = (
        lambda seconds: "Off" if not seconds else f"{float(seconds):g} seconds")
    recorder.fade_in_setting_label.return_value = "Fade In at Start: Off"
    recorder.fade_out_setting_label.return_value = "Fade Out at Stop: Off"
    bot.audio_recorder = recorder
    return bot


def tab():
    return pygame.event.Event(pygame.KEYDOWN, key=pygame.K_TAB, mod=pygame.KMOD_NONE)


def text_of(item):
    """A line's description, whether it is written down or built on demand."""
    description = item[3] if len(item) > 3 else ""
    return description() if callable(description) else str(description or "")


def label_of(item):
    """A line's own text, whether it is written down or built on demand.

    `libs/menu.py` reads a callable label when it speaks the line, which is how
    a live mark like "(active)" follows the setting it names; a test that
    compared the raw tuple would be reading the function instead of the line.
    """
    return item[0]() if callable(item[0]) else item[0]


class EveryLineIsDescribedTests(unittest.TestCase):
    def setUp(self):
        FakeMenu.built = []
        self.addCleanup(mock.patch.stopall)
        self.addCleanup(music_bot_gate.clear)
        music_bot_gate.clear()
        self.bot = make_bot()
        self.handler = menus_module.MusicBotMenus(self.bot)
        mock.patch("libs.menu.Menu", FakeMenu).start()
        mock.patch("libs.menus.set_default_sounds").start()
        mock.patch("libs.music_bot.menus.speak").start()

    def last_menu(self):
        return FakeMenu.built[-1]

    def test_every_line_of_the_mode_menu_is_described(self):
        self.handler._show_mode_menu()
        menu = self.last_menu()
        self.assertGreater(len(menu.items), 10)
        for item in menu.items:
            with self.subTest(line=item[0]):
                self.assertGreaterEqual(len(item), 4,
                                        "the line carries no description")
                self.assertTrue(text_of(item).strip())

    def test_a_description_says_what_the_feature_is_for_not_just_its_name(self):
        self.handler._show_mode_menu()
        labels = [item[0] for item in self.last_menu().items]
        for item in self.last_menu().items:
            if item[0] == "Personal Music Feed":
                self.assertIn("Favorites", text_of(item))
            if item[0] == "Record Audio":
                self.assertIn("file", text_of(item))
        self.assertIn("Personal Music Feed", labels)

    def test_every_line_of_the_settings_menu_is_described(self):
        self.handler._open_settings_menu()
        for item in self.last_menu().items:
            with self.subTest(line=item[0]):
                self.assertGreaterEqual(len(item), 4)
                self.assertTrue(text_of(item).strip())

    def test_every_line_of_the_settings_submenus_is_described(self):
        for opener in (self.handler._open_autotune_speed_menu,
                       self.handler._open_autotune_amount_menu,
                       self.handler._open_eq_menu,
                       self.handler._open_transpose_menu,
                       self.handler._open_speech_transpose_menu):
            with self.subTest(menu=opener.__name__):
                opener()
                for item in self.last_menu().items:
                    self.assertGreaterEqual(len(item), 4,
                                            f"{item[0]} has no description")
                    self.assertTrue(text_of(item).strip())

    def test_the_scale_menu_describes_every_scale_it_lists(self):
        self.handler._open_autotune_scale_menu()
        menu = self.last_menu()
        labels = [label_of(item) for item in menu.items]
        self.assertIn("Harmonic Minor", labels)
        self.assertIn("Melodic Minor", labels)
        for item in menu.items:
            self.assertGreaterEqual(len(item), 4)
            self.assertTrue(text_of(item).strip())
        for item in menu.items:
            if label_of(item) == "Harmonic Minor":
                # What it sounds like, and why the seventh is raised.
                self.assertIn("seventh", text_of(item).lower())
            if label_of(item) == "Melodic Minor":
                self.assertIn("sixth", text_of(item).lower())

    def test_the_help_menu_leads_with_the_tab_line(self):
        self.handler._show_help_menu()
        menu = self.last_menu()
        self.assertTrue(menu.items[0][0].startswith("Tab"))
        self.assertIn("Tab", menu.intro)
        self.assertGreaterEqual(len(menu.items[0]), 4)


class TheRestOfTheFeatureMenusAreDescribedTests(unittest.TestCase):
    """Party Sync, Downloads, Recording and the Queue say what their lines do.

    These are the menus a staff member opens once a session and then has to
    remember: a listener can be kicked, a download can be cancelled, a
    recording can split. A line that only names the button is the line the
    wrong button gets pressed on.
    """

    def setUp(self):
        FakeMenu.built = []
        self.addCleanup(mock.patch.stopall)
        self.addCleanup(music_bot_gate.clear)
        music_bot_gate.clear()
        self.bot = make_bot()
        self.handler = menus_module.MusicBotMenus(self.bot)
        mock.patch("libs.menu.Menu", FakeMenu).start()
        mock.patch("libs.menus.set_default_sounds").start()
        mock.patch("libs.music_bot.menus.speak").start()

    def last_menu(self):
        return FakeMenu.built[-1]

    def assert_described(self, menu, skip=0):
        items = menu.items[:len(menu.items) - skip] if skip else menu.items
        self.assertGreater(len(items), 0)
        for item in items:
            with self.subTest(line=item[0]):
                self.assertGreaterEqual(len(item), 4,
                                        f"{item[0]} carries no description")
                self.assertTrue(text_of(item).strip())

    def party_pair(self, role="host", guests=()):
        ps = SimpleNamespace(
            role=role, guests=list(guests), host_name="Anh", queue=[],
            now_playing="", end_session=lambda: None)
        self.bot._party_sync_pair.return_value = (self.bot._find_gameplay(), ps)
        return ps

    def test_the_party_sync_menu_for_a_host(self):
        self.party_pair()
        self.bot.song_requests_switch_item.return_value = (
            "Song Requests: OFF (listeners type /m)", lambda: None,
            None, "Whether this session takes requests.")
        self.handler._open_party_sync_menu()
        self.assert_described(self.last_menu())

    def test_the_party_sync_menu_for_a_listener(self):
        self.party_pair(role="guest")
        self.handler._open_party_sync_menu()
        self.assert_described(self.last_menu())

    def test_the_party_menu_before_a_session_exists(self):
        self.bot._party_sync_pair.return_value = (
            self.bot._find_gameplay(), None)
        self.handler._open_party_sync_menu()
        self.assert_described(self.last_menu())

    def test_the_kick_menu(self):
        ps = self.party_pair()
        ps.guests = [{"name": "Bob"}]
        self.handler._open_party_kick_menu(ps)
        self.assert_described(self.last_menu())

    def test_the_party_queue_view_describes_its_close_button(self):
        self.party_pair(role="guest")
        self.handler.open_party_queue_view()
        menu = self.last_menu()
        # The queue's own lines are song titles; the button is what must talk.
        self.assertEqual(menu.items[-1][0], "Close")
        self.assertTrue(text_of(menu.items[-1]).strip())

    def test_the_download_center(self):
        self.bot.current_source = "youtube"
        self.bot.current_target = "https://www.youtube.com/watch?v=abc"
        self.bot.download_mgr.has_saved_folder_setting.return_value = True
        self.bot.download_mgr.is_active.return_value = True
        self.bot.playlist_mgr.get_favorites.return_value = [
            {"title": "A Song", "target": "x"}]
        self.bot.playlist_mgr.get_playlist_names.return_value = ["Chill"]
        with mock.patch.object(menus_module, "is_supported_music_url",
                               return_value=True):
            self.handler._open_download_menu()
        self.assert_described(self.last_menu())

    def test_the_playlist_picker_for_downloads(self):
        self.bot.playlist_mgr.get_playlist_names.return_value = ["Chill"]
        self.handler._open_download_playlist_menu()
        self.assert_described(self.last_menu())

    def test_the_record_audio_menu(self):
        self.handler._open_recording_menu()
        self.assert_described(self.last_menu())

    def test_the_recording_settings_menu(self):
        self.handler._open_recording_settings_menu()
        self.assert_described(self.last_menu())

    def test_the_recording_settings_menu_names_both_fades(self):
        self.handler._open_recording_settings_menu()
        labels = [label_of(item) for item in self.last_menu().items]
        self.assertIn("Fade In at Start: Off", labels)
        self.assertIn("Fade Out at Stop: Off", labels)

    def test_the_recording_fade_pages(self):
        for kind in ("in", "out"):
            with self.subTest(kind=kind):
                self.handler._open_recording_fade_menu(kind)
                self.assert_described(self.last_menu())

    def test_the_recording_reset_confirmation(self):
        self.handler._open_recording_reset_confirmation()
        self.assert_described(self.last_menu())

    def test_the_queue_menu_describes_its_buttons_but_not_the_song_titles(self):
        self.handler._open_queue_menu()
        menu = self.last_menu()
        labels = [item[0] for item in menu.items]
        self.assertIn("Clear Queue", labels)
        self.assertIn("Back", labels)
        for item in menu.items:
            if item[0] in ("Clear Queue", "Back"):
                self.assertGreaterEqual(len(item), 4)
                self.assertTrue(text_of(item).strip())


class TheLibraryMenusAreDescribedTests(unittest.TestCase):
    """The personal library, the subtitle pages and the song pickers.

    These menus are made of *content* -- a song title is a title, a search
    result is a result -- so only their action lines are described. Every one of
    them is: the buttons are where somebody stands when they are about to
    delete a playlist or take a song out of their Favorites.
    """

    def setUp(self):
        FakeMenu.built = []
        self.addCleanup(mock.patch.stopall)
        self.addCleanup(music_bot_gate.clear)
        music_bot_gate.clear()
        self.bot = make_bot()
        self.handler = menus_module.MusicBotMenus(self.bot)
        mock.patch("libs.menu.Menu", FakeMenu).start()
        mock.patch("libs.menus.set_default_sounds").start()
        mock.patch("libs.music_bot.menus.speak").start()

    def last_menu(self):
        return FakeMenu.built[-1]

    def assert_button_described(self, label):
        found = [item for item in self.last_menu().items if item[0] == label]
        self.assertTrue(found, f"{label} is not in the menu")
        self.assertGreaterEqual(len(found[0]), 4,
                                f"{label} carries no description")
        self.assertTrue(text_of(found[0]).strip())

    def assert_every_line_described(self):
        for item in self.last_menu().items:
            with self.subTest(line=item[0]):
                self.assertGreaterEqual(len(item), 4,
                                        f"{item[0]} carries no description")
                self.assertTrue(text_of(item).strip())

    def test_the_personal_feed(self):
        self.handler._show_personal_feed_menu()
        for label in ("Start shuffled Favorites feed", "Next feed song",
                      "Back"):
            self.assert_button_described(label)

    def test_the_playlists_page(self):
        self.bot.playlist_mgr.get_playlist_names.return_value = ["Chill"]
        self.handler._open_playlists_menu()
        for label in ("Save Current Song to Favorites", "All Favorites",
                      "Create New Playlist", "Playlist: Chill", "Back"):
            self.assert_button_described(label)

    def test_the_favorites_list(self):
        self.bot.playlist_mgr.get_favorites.return_value = [
            {"title": "A Song", "target": "x", "source": "youtube"}]
        self.handler._show_favorites_menu()
        for label in ("Play All Favorites", "Back"):
            self.assert_button_described(label)

    def test_a_playlist_page(self):
        self.bot.playlist_mgr.get_playlist_tracks.return_value = [
            {"title": "A Song", "target": "x", "source": "youtube"}]
        self.handler._show_custom_playlist_menu("Chill")
        for label in ("Play All Tracks", "Delete Playlist", "Back"):
            self.assert_button_described(label)

    def test_the_track_actions(self):
        with mock.patch.object(menus_module, "is_supported_music_url",
                               return_value=True):
            self.handler._show_track_action_menu(
                "A Song", "https://www.youtube.com/watch?v=abc", "youtube",
                is_favorite=True, playlist_name="Chill")
        for label in ("Play Now", "Download This Track",
                      "Remove from Favorites", "Remove from Chill", "Back"):
            self.assert_button_described(label)

    def test_the_search_results_page_closes_with_a_described_button(self):
        self.handler._show_results_menu([{"title": "A Song", "duration": 269}])
        self.assert_button_described("Cancel")

    def test_the_result_actions(self):
        self.bot.queue_mode = False
        self.bot.search_results = [{
            "title": "A Song", "duration": 269,
            "webpage_url": "https://www.youtube.com/watch?v=abc",
        }]
        self.handler._on_result_selected(0, self.bot._find_gameplay())
        for label in ("Play Now", "Play Next (Add to Queue)", "Download Song",
                      "Save to Favorites", "Add to Playlist...", "Cancel"):
            self.assert_button_described(label)

    def test_the_add_to_playlist_picker(self):
        self.bot.playlist_mgr.get_playlist_names.return_value = ["Chill"]
        self.handler._prompt_add_track_to_playlist("A Song", "x")
        for label in ("Chill", "Cancel"):
            self.assert_button_described(label)

    def test_the_party_song_picker_can_say_no(self):
        self.handler.open_song_pick_menu(
            "rid", [{"title": "A Song", "duration": 269}])
        self.assert_button_described("Nothing, thanks")

    def test_the_subtitle_language_page(self):
        self.handler._open_subtitle_language_menu()
        self.assert_every_line_described()

    def test_the_subtitle_timing_page(self):
        self.handler._open_subtitle_timing_menu()
        self.assert_every_line_described()


class TabSpeaksTheDescriptionTests(unittest.TestCase):
    """The real Menu, with the real items the Music Bot builds."""

    def setUp(self):
        FakeMenu.built = []
        self.addCleanup(mock.patch.stopall)
        self.addCleanup(music_bot_gate.clear)
        music_bot_gate.clear()
        self.bot = make_bot()
        self.handler = menus_module.MusicBotMenus(self.bot)
        mock.patch("libs.menu.Menu", FakeMenu).start()
        mock.patch("libs.menus.set_default_sounds").start()
        mock.patch("libs.music_bot.menus.speak").start()

    def menu_with(self, opener):
        opener()
        menu = Menu(_Game(), "Music Bot Settings")
        menu.items = list(FakeMenu.built[-1].items)
        return menu

    def at(self, menu, label):
        """Stand the cursor on a line, by the words it would speak."""
        labels = [item[0]() if callable(item[0]) else item[0]
                  for item in menu.items]
        menu.pos = labels.index(label)
        return menu

    def test_tab_speaks_the_line_about_the_autotune_scale(self):
        # The scale line is in the settings list only while Auto-Tune is on.
        self.bot.autotune_enabled = True
        self.bot.autotune_label.return_value = "ON"
        menu = self.menu_with(self.handler._open_settings_menu)
        self.at(menu, f"Auto-Tune Scale: {autotune.scale_label()}")
        with mock.patch.object(menu_module.speech, "speak") as speak:
            menu.update([tab()])
        said = speak.call_args.args[0]
        self.assertIn("harmonic", said.lower())
        self.assertIn("melodic", said.lower())
        self.assertEqual(speak.call_args.kwargs.get("id"), "menu_help")

    def test_tab_on_a_harmonic_minor_line_says_what_the_scale_is(self):
        self.handler._open_autotune_scale_menu()
        menu = self.menu_with(self.handler._open_autotune_scale_menu)
        self.at(menu, "Harmonic Minor")
        with mock.patch.object(menu_module.speech, "speak") as speak:
            menu.update([tab()])
        self.assertIn("seventh", speak.call_args.args[0].lower())

    def test_asking_does_not_choose_the_line_or_move_the_cursor(self):
        chosen = []
        self.bot.autotune_enabled = True
        self.bot.autotune_label.return_value = "ON"
        menu = self.menu_with(self.handler._open_settings_menu)
        index = self.at(menu, f"Auto-Tune Scale: {autotune.scale_label()}").pos
        item = menu.items[index]
        menu.items[index] = (item[0], lambda: chosen.append("chosen"), *item[2:])
        with mock.patch.object(menu_module.speech, "speak"):
            menu.update([tab()])
        self.assertEqual(menu.pos, index)
        self.assertEqual(chosen, [])

    def test_tab_speaks_the_recording_menu_lines(self):
        menu = self.menu_with(self.handler._open_recording_menu)
        self.at(menu, "Recording Settings")
        with mock.patch.object(menu_module.speech, "speak") as speak:
            menu.update([tab()])
        said = speak.call_args.args[0]
        self.assertIn("splits", said.lower())
        self.assertEqual(speak.call_args.kwargs.get("id"), "menu_help")

    def test_tab_speaks_a_fade_page_line(self):
        menu = self.menu_with(
            lambda: self.handler._open_recording_fade_menu("in"))
        self.at(menu, "Off (active)")
        with mock.patch.object(menu_module.speech, "speak") as speak:
            menu.update([tab()])
        self.assertIn("no fade", speak.call_args.args[0].lower())

    def test_the_help_menus_tab_line_explains_tab(self):
        self.handler._show_help_menu()
        menu = Menu(_Game(), "Music Bot Controls Help")
        menu.items = list(FakeMenu.built[-1].items)
        menu.pos = 0
        # The line's own words are spoken through the menu module's `speak`
        # (the same one every Music Bot line uses), not the Menu's.
        with mock.patch.object(menus_module, "speak") as speak:
            menu.update([pygame.event.Event(pygame.KEYDOWN, key=pygame.K_RETURN,
                                            mod=pygame.KMOD_NONE)])
        said = speak.call_args.args[0]
        self.assertIn("Tab reads what the line", said)


class TheGateKeepsTheDescriptionTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(music_bot_gate.clear)
        music_bot_gate.clear()

    def test_a_kept_line_keeps_its_description(self):
        music_bot_gate.apply_locked({"seek": "Seeking"})
        lines = music_bot_gate.filter_items([
            ("search", "Search YouTube", lambda: None, "It searches."),
            (None, "Help", lambda: None, "It helps."),
            ("seek", "Seek", lambda: None, "Hidden."),
        ])
        self.assertEqual([line[0] for line in lines], ["Search YouTube", "Help"])
        # Menu shape: (label, action, preview_sound, description). The
        # description is the fourth field, never the preview slot.
        self.assertIsNone(lines[0][2])
        self.assertEqual(lines[0][3], "It searches.")
        self.assertEqual(lines[1][3], "It helps.")

    def test_a_line_with_no_description_is_still_two_fields(self):
        lines = music_bot_gate.filter_items([("search", "Search", lambda: None)])
        self.assertEqual(len(lines[0]), 2)


if __name__ == "__main__":
    unittest.main()
