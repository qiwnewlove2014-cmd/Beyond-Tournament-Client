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
from libs.music_bot import voice_effect_builder  # noqa: E402
from libs.music_bot import voice_effect_custom  # noqa: E402
from libs.music_bot import voice_effect_recipes  # noqa: E402
from libs.music_bot import voice_effects  # noqa: E402


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
    bot.autotune_harmony_label.return_value = "Off"
    bot.autotune_harmony_summary.return_value = "Off"
    bot.voice_effect = "off"
    bot.voice_effect_label.return_value = "Off"
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
                       self.handler._open_harmony_menu,
                       self.handler._open_autotune_harmony_menu,
                       self.handler._open_voice_effect_menu,
                       self.handler._open_custom_effect_menu,
                       lambda: self.handler._open_custom_param_menu("drive"),
                       self.handler._open_voice_builder_menu,
                       self.handler._open_builder_recipes_menu,
                       lambda: self.handler._open_builder_recipes_menu(
                           "machine"),
                       lambda: self.handler._open_custom_param_menu(
                           "space", voice_effect_builder),
                       self.handler._open_eq_menu,
                       self.handler._open_transpose_menu,
                       self.handler._open_speech_transpose_menu):
            with self.subTest(menu=opener.__name__):
                opener()
                for item in self.last_menu().items:
                    self.assertGreaterEqual(len(item), 4,
                                            f"{item[0]} has no description")
                    self.assertTrue(text_of(item).strip())

    def test_the_settings_page_offers_the_voice_effect(self):
        self.handler._open_settings_menu()
        labels = [label_of(item) for item in self.last_menu().items]
        self.assertIn("Voice Effect: Off", labels)
        for item in self.last_menu().items:
            if label_of(item) == "Voice Effect: Off":
                self.assertIn("guitar amp", text_of(item).lower())

    def test_the_voice_effect_page_marks_the_chosen_pedal(self):
        """The mark follows the choice on the page that is already open."""
        self.handler._open_voice_effect_menu()
        menu = self.last_menu()
        self.assertIn("Off (active)", [label_of(item) for item in menu.items])
        self.bot.voice_effect = "amp"
        labels = [label_of(item) for item in menu.items]
        # The family that holds it says which pedal is chosen, so the top page
        # answers "where am I" without being opened.
        self.assertIn("Amp & Drive: Guitar Amp (active)", labels)
        self.assertNotIn("Off (active)", labels)
        # Picking a pedal is what the keyboard does, not a direct setter.
        for item in menu.items:
            if label_of(item) == "Amp & Drive: Guitar Amp (active)":
                item[1]()
                break
        else:
            self.fail("the top page has no Amp & Drive line")
        group = self.last_menu()
        for item in group.items:
            if label_of(item) == "Fuzz":
                item[1]()
                break
        else:
            self.fail("the family page has no Fuzz line")
        self.bot.set_voice_effect.assert_called_once_with("fuzz")

    def test_the_amp_page_puts_the_custom_board_last_and_opens_it(self):
        """The board is the bottom line of Amp & Drive, and it is not a pedal."""
        self.handler._open_voice_effect_menu("drive")
        menu = self.last_menu()
        labels = [label_of(item) for item in menu.items]
        self.assertEqual(labels[-1], "Back")
        self.assertTrue(labels[-2].startswith("Custom Amp:"), labels[-2])
        self.assertEqual(
            sum(1 for label in labels if label.startswith("Custom Amp")), 1)
        for item in menu.items:
            if label_of(item).startswith("Custom Amp:"):
                item[1]()
                break
        else:
            self.fail("the amp page has no Custom Amp line")
        # Opening it chooses it: a player who asked for their own amp has
        # already decided, and the knobs are on the page that opens.
        self.bot.set_voice_effect.assert_called_once_with("custom")
        board = self.last_menu()
        names = [label_of(item).split(":")[0] for item in board.items]
        for _param_id, name, _note, _choices, _default in (
                voice_effect_custom.PARAMS):
            self.assertIn(name, names)
        for item in board.items:
            self.assertGreaterEqual(len(item), 4)
            self.assertTrue(text_of(item).strip())

    def test_a_knob_page_writes_the_board_and_applies_it(self):
        board = {}
        mock.patch.object(
            voice_effect_custom.options, "get",
            side_effect=lambda key, default=None: board.get(key, default)
        ).start()
        mock.patch.object(
            voice_effect_custom.options, "set",
            side_effect=lambda key, value, autosave=True:
            board.__setitem__(key, value)
        ).start()
        self.handler._open_custom_effect_menu()
        self.handler._open_custom_param_menu("drive")
        menu = self.last_menu()
        labels = [label_of(item) for item in menu.items]
        self.assertIn("Crunch (active)", labels)
        self.assertEqual(labels[-1], "Back")
        for item in menu.items:
            if label_of(item) == "Saturated":
                item[1]()
                break
        else:
            self.fail("the drive page has no Saturated line")
        self.bot.set_voice_effect.assert_called_with("custom")
        self.assertEqual(voice_effect_custom.values()["drive"], "sat")

    def test_the_voice_effect_page_ends_with_the_voice_builder(self):
        """Custom Voice is the page's last choice, after the seven families."""
        self.handler._open_voice_effect_menu()
        menu = self.last_menu()
        labels = [label_of(item) for item in menu.items]
        self.assertEqual(labels[-1], "Back")
        self.assertTrue(labels[-2].startswith("Custom Voice:"), labels[-2])
        # It is a line of its own, not a family: the seven are above it.
        for _group_id, name, _note, _members in voice_effects.VOICE_EFFECT_GROUPS:
            self.assertTrue(any(label.startswith(name) for label in labels))
        for item in menu.items:
            if label_of(item).startswith("Custom Voice:"):
                item[1]()
                break
        else:
            self.fail("the Voice Effect page has no Custom Voice line")
        self.bot.set_voice_effect.assert_called_once_with("voice_build")
        board = self.last_menu()
        names = [label_of(item).split(":")[0] for item in board.items]
        for _param_id, name, _note, _choices, _default in (
                voice_effect_builder.PARAMS):
            self.assertIn(name, names)
        # The ten slots, the starting line above them, Clear below them, Back.
        self.assertEqual(len(board.items), len(voice_effect_builder.PARAMS) + 3)
        for item in board.items:
            self.assertGreaterEqual(len(item), 4)
            self.assertTrue(text_of(item).strip())

    def test_a_voice_builder_slot_page_writes_the_board_and_applies_it(self):
        board = {}
        mock.patch.object(
            voice_effect_builder.options, "get",
            side_effect=lambda key, default=None: board.get(key, default)
        ).start()
        mock.patch.object(
            voice_effect_builder.options, "set",
            side_effect=lambda key, value, autosave=True:
            board.__setitem__(key, value)
        ).start()
        self.handler._open_voice_builder_menu()
        self.handler._open_custom_param_menu("space", voice_effect_builder)
        menu = self.last_menu()
        labels = [label_of(item) for item in menu.items]
        self.assertIn("Off (active)", labels)
        self.assertGreaterEqual(len(labels), 11)
        for item in menu.items:
            if label_of(item) == "Cave":
                item[1]()
                break
        else:
            self.fail("the Space page has no Cave line")
        self.bot.set_voice_effect.assert_called_with("voice_build")
        self.assertEqual(voice_effect_builder.values()["space"], "cave")

    def test_the_voice_board_begins_with_fifty_finished_voices(self):
        """`Start From:` is the board's first line, and a press is a sound."""
        saved = {}
        mock.patch.object(
            voice_effect_builder.options, "get",
            side_effect=lambda key, default=None: saved.get(key, default)
        ).start()
        mock.patch.object(
            voice_effect_builder.options, "set",
            side_effect=lambda key, value, autosave=True:
            saved.__setitem__(key, value)
        ).start()
        self.handler._open_voice_builder_menu()
        self.assertTrue(label_of(self.last_menu().items[0]).startswith(
            "Start From:"), label_of(self.last_menu().items[0]))
        self.last_menu().items[0][1]()
        families = [label_of(item) for item in self.last_menu().items]
        self.assertEqual(
            families,
            [name for _id, name, _note in voice_effect_recipes.FAMILIES]
            + ["Back"])
        for item in self.last_menu().items:
            if label_of(item) == "Machine":
                item[1]()
                break
        else:
            self.fail("the starting points have no Machine family")
        page = self.last_menu()
        self.assertIn("Machine", page.title)
        self.assertEqual(len(page.items), 11)
        for item in page.items:
            if label_of(item) == "Vocoder Stack":
                item[1]()
                break
        else:
            self.fail("the Machine family has no Vocoder Stack")
        self.bot.set_voice_effect.assert_called_with("voice_build")
        self.assertEqual(voice_effect_builder.values()["texture"], "vocoder")
        self.assertEqual(voice_effect_builder.values()["pitch"], "octave")
        self.assertEqual(voice_effect_recipes.current_label(), "Vocoder Stack")
        said = menus_module.speak.call_args.args[0]
        self.assertIn("Vocoder Stack", said)
        self.assertIn("synth", said)
        # The pick stays on the family page -- fifty voices are for trying one
        # after another -- and the mark moves on the page already open.
        self.assertIs(self.last_menu(), page)
        self.assertEqual(page.title, "Music Bot Custom Voice: Machine")
        self.assertIn("Vocoder Stack (active)",
                      [label_of(item) for item in page.items])

    def test_every_pedal_is_in_exactly_one_family(self):
        """Two levels can lose a row and still look complete; this is the check."""
        self.assertEqual(voice_effects.catalogue_problems(), [])
        self.handler._open_voice_effect_menu()
        top = [label_of(item) for item in self.last_menu().items]
        for _group_id, name, _note, members in voice_effects.VOICE_EFFECT_GROUPS:
            self.assertTrue(any(label.split(":")[0] == name for label in top),
                            f"{name} is not on the top page")
            self.assertTrue(members)

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


class ThePagesStayOpenTests(unittest.TestCase):
    """Choosing is not leaving, and the board is rebuilt on the way back.

    A page of values is a set to try -- which body, which room, which texture,
    which of the fifty voices -- so a pick leaves the page open and its
    `(active)` mark moves on the page already open. The board underneath is the
    one thing that cannot be updated in place, because its title is a snapshot
    of the summary it opened with; it is rebuilt when the player walks back to
    it, never on the press, so trying ten recipes is ten voices and not ten
    rebuilds.
    """

    def setUp(self):
        FakeMenu.built = []
        self.addCleanup(mock.patch.stopall)
        self.addCleanup(music_bot_gate.clear)
        music_bot_gate.clear()
        self.bot = make_bot()
        self.stack = []
        gp = SimpleNamespace(
            add_substate=self.stack.append,
            pop_last_substate=lambda: self.stack.pop() if self.stack else None,
            can_broadcast_megaphone=True)
        self.bot._find_gameplay.return_value = gp
        self.handler = menus_module.MusicBotMenus(self.bot)
        mock.patch("libs.menu.Menu", FakeMenu).start()
        mock.patch("libs.menus.set_default_sounds").start()
        mock.patch("libs.music_bot.menus.speak").start()
        # A board in memory: the player's own settings file is never read.
        self.saved = {}
        mock.patch.object(
            voice_effect_builder.options, "get",
            side_effect=lambda key, default=None: self.saved.get(key, default)
        ).start()
        mock.patch.object(
            voice_effect_builder.options, "set",
            side_effect=lambda key, value, autosave=True:
            self.saved.__setitem__(key, value)
        ).start()

    def top(self):
        return self.stack[-1]

    def press(self, menu, label):
        """Press the line that speaks `label`, wherever it sits on the page."""
        for item in menu.items:
            if label_of(item) == label:
                item[1]()
                return
        self.fail(f"{label!r} is not on the page")

    def test_a_slot_page_stays_open_and_its_title_follows_the_value(self):
        self.handler._open_voice_builder_menu()
        self.handler._open_custom_param_menu("space", voice_effect_builder)
        page = self.top()
        self.assertEqual(len(self.stack), 2)
        self.press(page, "Cave")
        # The same page, still on the stack, with nothing rebuilt.
        self.assertIs(self.top(), page)
        self.assertEqual(len(self.stack), 2)
        self.assertEqual(voice_effect_builder.values()["space"], "cave")
        self.assertEqual(page.title, "Music Bot Custom Voice Space: Cave")
        labels = [label_of(item) for item in page.items]
        self.assertIn("Cave (active)", labels)
        self.assertNotIn("Off (active)", labels)
        self.assertEqual(menus_module.speak.call_args.args[0].split(".")[0],
                         "Space: Cave")

    def test_the_starting_points_stay_and_the_board_is_rebuilt_on_the_way_back(self):
        self.handler._open_voice_builder_menu()
        board = self.top()
        self.assertTrue(label_of(board.items[0]).startswith("Start From:"))
        self.press(board, label_of(board.items[0]))
        families = self.top()
        self.press(families, "Machine")
        page = self.top()
        self.assertEqual(len(self.stack), 3)
        self.press(page, "Vocoder Stack")
        # A pick is not a departure: the family page is still where we are,
        # and the board underneath is the same object it was.
        self.assertIs(self.top(), page)
        self.assertEqual(len(self.stack), 3)
        self.assertIs(self.stack[0], board)
        self.assertEqual(voice_effect_recipes.current_label(), "Vocoder Stack")
        self.assertIn("Vocoder Stack (active)",
                      [label_of(item) for item in page.items])
        # Back once returns to the five families, still above the board.
        self.press(page, "Back")
        self.assertIs(self.top(), families)
        self.assertEqual(len(self.stack), 2)
        # Back again lands on the board -- rebuilt, because its title was a
        # snapshot written before the recipe moved the slots.
        self.press(families, "Back")
        self.assertEqual(len(self.stack), 1)
        rebuilt = self.top()
        self.assertIsNot(rebuilt, board)
        self.assertIn("Vocoder", rebuilt.title)
        self.assertTrue(label_of(rebuilt.items[0]).startswith("Start From:"))
        self.assertIn("Vocoder Stack", label_of(rebuilt.items[0]))

    def test_clear_is_the_voice_boards_line_and_sits_above_back(self):
        self.handler._open_voice_builder_menu()
        labels = [label_of(item) for item in self.top().items]
        self.assertEqual(labels[-1], "Back")
        self.assertTrue(labels[-2].startswith("Clear:"), labels[-2])
        # The amp board has its own page and no Clear: one board, one way out.
        self.handler._open_custom_effect_menu()
        self.assertFalse(any(label_of(item).startswith("Clear:")
                             for item in self.top().items))

    def test_clear_asks_first_and_no_keeps_everything(self):
        self.handler._open_voice_builder_menu()
        board = self.top()
        voice_effect_builder.set_value("space", "cave")
        self.press(board, "Clear: back to the defaults")
        question = self.top()
        self.assertIsNot(question, board)
        self.assertEqual([label_of(item) for item in question.items],
                         ["Yes, clear the board", "No, keep it"])
        self.press(question, "No, keep it")
        self.assertIs(self.top(), board)
        self.assertEqual(voice_effect_builder.values()["space"], "cave")

    def test_yes_clears_ten_slots_and_forgets_the_starting_point(self):
        self.handler._open_voice_builder_menu()
        board = self.top()
        voice_effect_recipes.apply("vocoder")
        self.assertEqual(voice_effect_recipes.current_label(), "Vocoder Stack")
        self.press(board, "Clear: back to the defaults")
        self.press(self.top(), "Yes, clear the board")
        # The board is still the page: its slots and its title are read fresh.
        self.assertIs(self.top(), board)
        self.assertEqual(voice_effect_builder.values(),
                         voice_effect_builder.filled({}))
        self.assertEqual(voice_effect_recipes.current_label(),
                         voice_effect_recipes.MY_OWN)
        self.assertEqual(
            board.title,
            f"Music Bot {voice_effects.effect_label(voice_effects.BUILDER_EFFECT)}: "
            f"{voice_effect_builder.summary()}")
        self.assertIn("My Own", label_of(board.items[0]))
        self.assertIn("cleared",
                      menus_module.speak.call_args.args[0].lower())


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

    def test_tab_on_a_custom_knob_says_what_the_knob_does(self):
        menu = self.menu_with(self.handler._open_custom_effect_menu)
        self.at(menu, "Drive: Crunch")
        with mock.patch.object(menu_module.speech, "speak") as speak:
            menu.update([tab()])
        self.assertIn("hard the amp is pushed",
                      speak.call_args.args[0].lower())
        self.assertEqual(speak.call_args.kwargs.get("id"), "menu_help")

    def test_tab_on_a_voice_builder_slot_says_what_the_slot_does(self):
        menu = self.menu_with(self.handler._open_voice_builder_menu)
        self.at(menu, "Space: Off")
        with mock.patch.object(menu_module.speech, "speak") as speak:
            menu.update([tab()])
        self.assertIn("where the voice is heard from",
                      speak.call_args.args[0].lower())
        self.assertEqual(speak.call_args.kwargs.get("id"), "menu_help")

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
