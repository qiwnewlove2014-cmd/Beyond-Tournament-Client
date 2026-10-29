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
from libs.music_bot.menu_pages import library as library_pages  # noqa: E402
from libs.music_bot.voice_effects import builder  # noqa: E402
from libs.music_bot.voice_effects import custom  # noqa: E402
from libs.music_bot.voice_effects import pan  # noqa: E402
from libs.music_bot.voice_effects import recipes  # noqa: E402
from libs.music_bot.voice_effects import vocoder  # noqa: E402
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
    bot.vocoder_lab_allowed.return_value = True
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
    recorder.capture_scope_label.return_value = (
        "Capture Scope: All Audio Rendered by Beyond Tournament")
    recorder.computer_audio_setting_label.return_value = (
        "Include Screen Reader and Computer Audio: Off")
    recorder.microphone_setting_label.return_value = (
        "Include My Transmitted Voice: Off")
    recorder.recorded_voice_label.return_value = (
        "Recorded Voice: Transmitted")
    recorder.countdown_setting_label.return_value = (
        "Recording Countdown: 3 seconds")
    recorder.split_setting_label.return_value = "Split Long Recordings: Off"
    recorder.announce_setting_label.return_value = (
        "Announce Completed Recording Details: On")
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
        # The vocoder board reads its saved values from the real options file;
        # a menu test must not depend on what the developer running it chose.
        mock.patch.object(vocoder.options, "get",
                          return_value={}).start()
        for opener in (self.handler._open_autotune_speed_menu,
                       self.handler._open_autotune_amount_menu,
                       self.handler._open_harmony_menu,
                       self.handler._open_autotune_harmony_menu,
                       self.handler._open_voice_effect_menu,
                       self.handler._open_custom_effect_menu,
                       lambda: self.handler._open_custom_param_menu("drive"),
                       self.handler._open_voice_builder_menu,
                       self.handler._open_vocoder_lab_menu,
                       lambda: self.handler._open_custom_param_menu(
                           "wave", vocoder),
                       self.handler._open_builder_recipes_menu,
                       lambda: self.handler._open_builder_recipes_menu(
                           "machine"),
                       lambda: self.handler._open_custom_param_menu(
                           "space", builder),
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
                custom.PARAMS):
            self.assertIn(name, names)
        for item in board.items:
            self.assertGreaterEqual(len(item), 4)
            self.assertTrue(text_of(item).strip())

    def test_the_vocoder_family_ends_with_the_developer_board(self):
        """The board is the bottom line of Vocoder Lab, and it is not a preset."""
        mock.patch.object(vocoder.options, "get",
                          return_value={}).start()
        self.handler._open_voice_effect_menu("synth")
        labels = [label_of(item) for item in self.last_menu().items]
        self.assertEqual(labels[-1], "Back")
        self.assertTrue(labels[-2].startswith("Vocoder Lab:"), labels[-2])
        self.assertEqual(
            sum(1 for label in labels if label.startswith("Vocoder Lab:")), 1)
        for item in self.last_menu().items:
            if label_of(item).startswith("Vocoder Lab:"):
                item[1]()
                break
        else:
            self.fail("the family page has no Vocoder Lab board line")
        # Opening it chooses it: the board is what the player asked for.
        self.bot.set_voice_effect.assert_called_once_with(
            voice_effects.VOX_LAB_EFFECT)
        board = self.last_menu()
        names = [label_of(item).split(":")[0] for item in board.items]
        for row in vocoder.rows():
            self.assertIn(row[1], names)
        # Current owns no note: those rows are hidden, not shown doing nothing.
        self.assertNotIn("Root", names)
        self.assertNotIn("Scale", names)
        # The board's rows, its Tone line, and Back.
        self.assertEqual(len(board.items),
                         len(vocoder.rows()) + 2)
        for item in board.items:
            self.assertGreaterEqual(len(item), 4)
            self.assertTrue(text_of(item).strip())

    def test_the_vocoder_family_line_names_the_mode_while_it_is_chosen(self):
        """The board's catalogue name is the family's own, so the line says the
        mode and note instead of reading "Vocoder Lab: Vocoder Lab"."""
        mock.patch.object(vocoder.options, "get",
                          return_value={}).start()
        self.bot.voice_effect = "vox_lab"
        self.handler._open_voice_effect_menu()
        labels = [label_of(item) for item in self.last_menu().items]
        self.assertIn("Vocoder Lab: Current 110 Hz (active)", labels)

    def test_the_vocoder_board_is_not_offered_without_the_server_permission(self):
        self.bot.vocoder_lab_allowed.return_value = False
        self.handler._open_voice_effect_menu("synth")
        labels = [label_of(item) for item in self.last_menu().items]
        self.assertFalse(any(label.startswith("Vocoder Lab:")
                             for label in labels), labels)
        # ... and a direct call cannot open it either, so the line is not the
        # only thing standing between a non-Developer and the page.
        built = len(FakeMenu.built)
        self.handler._open_vocoder_lab_menu()
        self.assertEqual(len(FakeMenu.built), built)
        # The permission can also be taken away while the family page is open.
        # The line is a snapshot, so the press refuses in place and says why
        # instead of popping into a board that would not open.
        self.bot.vocoder_lab_allowed.return_value = True
        self.handler._open_voice_effect_menu("synth")
        self.bot.vocoder_lab_allowed.return_value = False
        built = len(FakeMenu.built)
        for item in self.last_menu().items:
            if label_of(item).startswith("Vocoder Lab:"):
                item[1]()
                break
        else:
            self.fail("the family page has no Vocoder Lab board line")
        self.assertEqual(len(FakeMenu.built), built)
        self.bot.set_voice_effect.assert_not_called()
        self.assertIn("Developer", menus_module.speak.call_args.args[0])

    def test_noise_is_refused_in_a_locked_note_mode_with_a_reason(self):
        saved = {vocoder._OPTION: {"mode": "tune"}}
        mock.patch.object(
            vocoder.options, "get",
            side_effect=lambda key, default=None: saved.get(key, default)
        ).start()
        mock.patch.object(
            vocoder.options, "set",
            side_effect=lambda key, value, autosave=True:
            saved.__setitem__(key, value)
        ).start()
        self.handler._open_vocoder_lab_menu()
        self.handler._open_custom_param_menu("wave", vocoder)
        for item in self.last_menu().items:
            if label_of(item) == "Noise":
                item[1]()
                break
        else:
            self.fail("the wave page has no Noise line")
        self.assertEqual(
            saved[vocoder._OPTION]["wave"], "saw")
        self.bot.set_voice_effect.assert_called_once_with(
            voice_effects.VOX_LAB_EFFECT)
        said = menus_module.speak.call_args.args[0].lower()
        self.assertIn("no note", said)
        self.assertIn("saw", said)

    def test_a_knob_page_writes_the_board_and_applies_it(self):
        board = {}
        mock.patch.object(
            custom.options, "get",
            side_effect=lambda key, default=None: board.get(key, default)
        ).start()
        mock.patch.object(
            custom.options, "set",
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
        self.assertEqual(custom.values()["drive"], "sat")

    def test_the_voice_effect_page_ends_with_the_voice_builder(self):
        """Custom Voice is the page's last choice, after the families."""
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
                builder.PARAMS):
            self.assertIn(name, names)
        # The ten slots, the starting line above them, the Tone line, Clear
        # below it, Back.
        self.assertEqual(len(board.items), len(builder.PARAMS) + 4)
        for item in board.items:
            self.assertGreaterEqual(len(item), 4)
            self.assertTrue(text_of(item).strip())

    def test_the_voice_effect_page_offers_the_pan_delay_at_the_top_level(self):
        """The one preset with a setting of its own is not hidden in a family.

        A typed delay cannot be a row of fixed steps, so Pan Delay opens a page
        rather than being a pedal -- which is the same reason Custom Voice sits
        on this page. It is a line of its own beside the families, and pressing
        it chooses the preset and opens the page.
        """
        # The page's own settings are read from the real options file; a menu
        # test must not depend on what the developer running it chose.
        mock.patch.object(pan.options, "get",
                          side_effect=lambda key, default=None: default).start()
        self.handler._open_voice_effect_menu()
        menu = self.last_menu()
        labels = [label_of(item) for item in menu.items]
        self.assertTrue(any(label.startswith("Pan Delay:") for label in labels),
                        labels)
        self.assertIn(voice_effects.PAN_EFFECT,
                      voice_effects.STANDALONE_EFFECTS)
        self.assertIsNone(voice_effects.group_of(voice_effects.PAN_EFFECT))
        # ...and no family page lists it as a pedal.
        self.handler._open_voice_effect_menu("motion")
        pedals = [label_of(item) for item in self.last_menu().items]
        self.assertFalse(any("Pan Delay" in label for label in pedals), pedals)
        self.handler._open_voice_effect_menu()
        for item in self.last_menu().items:
            if label_of(item).startswith("Pan Delay:"):
                item[1]()
                break
        else:
            self.fail("the Voice Effect page has no Pan Delay line")
        # Pressing the line chooses the preset (the page is opened by pressing
        # it, and the page itself selects it again on open), so every call it
        # makes is for the one preset.
        self.assertTrue(self.bot.set_voice_effect.called)
        for chosen in self.bot.set_voice_effect.call_args_list:
            self.assertEqual(chosen, mock.call("pan_delay"))
        page = [label_of(item) for item in self.last_menu().items]
        self.assertTrue(any(label.startswith("Delay:") for label in page), page)
        self.assertTrue(any(label.startswith("Motion:") for label in page), page)
        self.assertTrue(any(label.startswith("Preset:") for label in page), page)
        self.assertEqual(page[-1], "Back")
        # Preset opens the same names the Custom Voice board's Pan slot offers.
        for item in self.last_menu().items:
            if label_of(item).startswith("Preset:"):
                item[1]()
                break
        else:
            self.fail("the Pan Delay page has no Preset line")
        names = [label_of(item) for item in self.last_menu().items]
        for _stored, label, _ms, _mode, _note in pan.PRESETS:
            self.assertTrue(any(name.startswith(label) for name in names),
                            names)

    def test_the_custom_voice_board_carries_the_pan_delay_slot(self):
        """The board can play the delay the Pan Delay page sets."""
        self.handler._open_voice_builder_menu()
        labels = [label_of(item) for item in self.last_menu().items]
        self.assertTrue(any(label.startswith("Pan Delay") for label in labels),
                        labels)
        for item in self.last_menu().items:
            if label_of(item).startswith("Pan Delay"):
                item[1]()
                break
        else:
            self.fail("the Custom Voice board has no Pan Delay slot")
        choices = [label_of(item) for item in self.last_menu().items]
        self.assertTrue(any(name.startswith("Off") for name in choices), choices)
        for _stored, name, _ms, _mode, _note in pan.PRESETS:
            self.assertIn(name, choices)

    def test_a_voice_builder_slot_page_writes_the_board_and_applies_it(self):
        board = {}
        mock.patch.object(
            builder.options, "get",
            side_effect=lambda key, default=None: board.get(key, default)
        ).start()
        mock.patch.object(
            builder.options, "set",
            side_effect=lambda key, value, autosave=True:
            board.__setitem__(key, value)
        ).start()
        self.handler._open_voice_builder_menu()
        self.handler._open_custom_param_menu("space", builder)
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
        self.assertEqual(builder.values()["space"], "cave")

    def test_every_choice_page_ends_with_a_clear_for_that_one_setting(self):
        """`Clear: back to <default>` sits above Back on every page, named.

        The page of values has never said what the board opens with, and a
        default is not always the first row -- Colour opens on Crunch, the amp
        on Crunch too with its character on Valve and its gate on Soft. So the
        line the player presses says which value they are being put back on,
        read from the board's own table rather than typed out here.
        """
        for board in (custom, builder):
            for param_id, name, _note, _choices, _default in board.PARAMS:
                with self.subTest(board=board.EFFECT_ID, setting=name):
                    self.handler._open_custom_param_menu(param_id, board)
                    items = self.last_menu().items
                    labels = [label_of(item) for item in items]
                    self.assertEqual(labels[-1], "Back")
                    wanted = board.default_label(param_id)
                    self.assertTrue(wanted, "a setting with no default name")
                    self.assertEqual(labels[-2], f"Clear: back to {wanted}")
                    self.assertIn(wanted, text_of(items[-2]))

    def test_the_clear_line_puts_one_setting_back_and_leaves_the_others(self):
        saved = {}
        mock.patch.object(
            builder.options, "get",
            side_effect=lambda key, default=None: saved.get(key, default)
        ).start()
        mock.patch.object(
            builder.options, "set",
            side_effect=lambda key, value, autosave=True:
            saved.__setitem__(key, value)
        ).start()
        builder.set_values({"colour": "fuzz", "space": "cave",
                                          "texture": "vocoder"})
        self.handler._open_voice_builder_menu()
        self.handler._open_custom_param_menu("colour", builder)
        page = self.last_menu()
        before = builder.revision()
        for item in page.items:
            if label_of(item) == "Clear: back to Crunch":
                item[1]()
                break
        else:
            self.fail("the Colour page has no clear line")
        board = builder.values()
        self.assertEqual(board["colour"], "crunch")
        # The other nine slots are where the player left them: this is one
        # setting's way out, not the whole board's Clear.
        self.assertEqual(board["space"], "cave")
        self.assertEqual(board["texture"], "vocoder")
        self.assertEqual(builder.revision(), before + 1)
        self.bot.set_voice_effect.assert_called_with("voice_build")
        # No question, and no page to come back from: the page the press
        # happened on is the page that stays open, saying what it now holds.
        self.assertIs(self.last_menu(), page)
        self.assertIn("Colour: Crunch", page.title)

    def test_the_voice_board_begins_with_fifty_finished_voices(self):
        """`Start From:` is the board's first line, and a press is a sound."""
        saved = {}
        mock.patch.object(
            builder.options, "get",
            side_effect=lambda key, default=None: saved.get(key, default)
        ).start()
        mock.patch.object(
            builder.options, "set",
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
            [name for _id, name, _note in recipes.FAMILIES]
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
        self.assertEqual(builder.values()["texture"], "vocoder")
        self.assertEqual(builder.values()["pitch"], "octave")
        self.assertEqual(recipes.current_label(), "Vocoder Stack")
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
            builder.options, "get",
            side_effect=lambda key, default=None: self.saved.get(key, default)
        ).start()
        mock.patch.object(
            builder.options, "set",
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
        self.handler._open_custom_param_menu("space", builder)
        page = self.top()
        self.assertEqual(len(self.stack), 2)
        self.press(page, "Cave")
        # The same page, still on the stack, with nothing rebuilt.
        self.assertIs(self.top(), page)
        self.assertEqual(len(self.stack), 2)
        self.assertEqual(builder.values()["space"], "cave")
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
        self.assertEqual(recipes.current_label(), "Vocoder Stack")
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
        builder.set_value("space", "cave")
        self.press(board, "Clear: back to the defaults")
        question = self.top()
        self.assertIsNot(question, board)
        self.assertEqual([label_of(item) for item in question.items],
                         ["Yes, clear the board", "No, keep it"])
        self.press(question, "No, keep it")
        self.assertIs(self.top(), board)
        self.assertEqual(builder.values()["space"], "cave")

    def test_yes_clears_ten_slots_and_forgets_the_starting_point(self):
        self.handler._open_voice_builder_menu()
        board = self.top()
        recipes.apply("vocoder")
        self.assertEqual(recipes.current_label(), "Vocoder Stack")
        self.press(board, "Clear: back to the defaults")
        self.press(self.top(), "Yes, clear the board")
        # The board is still the page: its slots and its title are read fresh.
        self.assertIs(self.top(), board)
        self.assertEqual(builder.values(),
                         builder.filled({}))
        self.assertEqual(recipes.current_label(),
                         recipes.MY_OWN)
        self.assertEqual(
            board.title,
            f"Music Bot {voice_effects.effect_label(voice_effects.BUILDER_EFFECT)}: "
            f"{builder.summary()}")
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
        with mock.patch.object(library_pages, "is_supported_music_url",
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
        with mock.patch.object(library_pages, "is_supported_music_url",
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

    def test_tab_speaks_the_recorded_voice_line(self):
        menu = self.menu_with(self.handler._open_recording_settings_menu)
        self.at(menu, "Recorded Voice: Transmitted")
        with mock.patch.object(menu_module.speech, "speak") as speak:
            menu.update([tab()])
        said = speak.call_args.args[0].lower()
        self.assertIn("studio condenser", said)
        self.assertIn("transmitted", said)
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
        # Alt + the Speak Zone key opens the Voice Effect page from the world.
        labels = [label_of(item) for item in menu.items]
        voice = [item for item in menu.items
                 if "Voice Effect" in label_of(item)]
        self.assertTrue(voice, labels)
        self.assertIn("Alt +", label_of(voice[0]))
        self.assertTrue(voice[0][3], "the Voice Effect key needs a description")


class BackAndEscapeLeaveOnePageTests(unittest.TestCase):
    """A page of values closes onto its board, one press at a time.

    Every page in the Voice Effect tree re-opens the page under it -- that is
    also how the page is spoken again, since a pop itself says nothing -- and
    the knob pages were the one place that popped and re-opened nothing. Esc
    presses a page's last line, which is `Back`, so Esc on a knob page landed
    on whatever the board had itself replaced: Music Bot Settings, two levels
    up, with the board and every value just set gone. This is run against a
    real substate stack, because a fake that ignores `add_substate` cannot see
    a page that was never there.
    """

    def setUp(self):
        FakeMenu.built = []
        self.addCleanup(mock.patch.stopall)
        self.addCleanup(music_bot_gate.clear)
        music_bot_gate.clear()
        self.saved = {}
        mock.patch.object(
            builder.options, "get",
            side_effect=lambda key, default=None: self.saved.get(key, default)
        ).start()
        mock.patch.object(
            builder.options, "set",
            side_effect=lambda key, value, autosave=True:
            self.saved.__setitem__(key, value)
        ).start()
        self.bot = make_bot()
        self.handler = menus_module.MusicBotMenus(self.bot)
        self.stack = []
        self.bot._find_gameplay.return_value = SimpleNamespace(
            substates=self.stack,
            add_substate=self.add,
            pop_last_substate=self.pop,
            can_broadcast_megaphone=True,
        )
        mock.patch("libs.menu.Menu", FakeMenu).start()
        mock.patch("libs.menus.set_default_sounds").start()
        mock.patch("libs.music_bot.menus.speak").start()

    def add(self, item):
        self.stack.append(item)
        return item

    def pop(self):
        return self.stack.pop() if self.stack else None

    def press(self, menu, label):
        """Press the line that speaks these words (a prefix is enough)."""
        for item in menu.items:
            if label_of(item) == label:
                item[1]()
                return
        for item in menu.items:
            if label_of(item).startswith(label):
                item[1]()
                return
        self.fail(f"no line starting {label!r} on {menu.title!r}")

    def open_board_and_a_knob(self):
        self.handler._open_voice_effect_menu()
        self.press(self.stack[-1], "Custom Voice:")
        board = self.stack[-1]
        self.press(board, "Space: Off")
        return board, self.stack[-1]

    def test_back_on_a_knob_page_is_the_board_under_it(self):
        board, page = self.open_board_and_a_knob()
        self.assertIs(page, FakeMenu.built[-1])
        self.press(page, "Cave")
        # A choice is one write on the page already open -- it does not move.
        self.assertIs(self.stack[-1], page)
        self.assertEqual(builder.values()["space"], "cave")
        self.press(page, "Back")
        landed = self.stack[-1]
        self.assertIsNot(landed, page)
        self.assertIsNot(landed, board)
        # ... the board, rebuilt, titled with what it holds now rather than
        # what it held when it opened.
        self.assertEqual(landed.title,
                         f"Music Bot Custom Voice: "
                         f"{builder.summary()}")
        self.assertIn("Cave", landed.title)
        labels = [label_of(item) for item in landed.items]
        self.assertIn("Space: Cave", labels)
        self.assertEqual(labels[-1], "Back")
        self.assertEqual(len(self.stack), 1)

    def test_escape_on_a_knob_page_presses_that_page_s_own_back_line(self):
        _board, page = self.open_board_and_a_knob()
        self.press(page, "Cave")
        menu = Menu(_Game(), page.title)
        menu.items = list(page.items)
        menu.update([pygame.event.Event(pygame.KEYDOWN, key=pygame.K_ESCAPE,
                                        mod=pygame.KMOD_NONE)])
        landed = self.stack[-1]
        self.assertIsNot(landed, page)
        self.assertIn("Custom Voice", landed.title)
        self.assertIn("Space: Cave", [label_of(item) for item in landed.items])
        self.assertEqual(len(self.stack), 1)

    def test_the_board_s_own_escape_leaves_for_the_page_it_came_from(self):
        self.handler._open_voice_effect_menu()
        page = self.stack[-1]
        self.press(page, "Custom Voice:")
        board = self.stack[-1]
        menu = Menu(_Game(), board.title)
        menu.items = list(board.items)
        menu.update([pygame.event.Event(pygame.KEYDOWN, key=pygame.K_ESCAPE,
                                        mod=pygame.KMOD_NONE)])
        landed = self.stack[-1]
        self.assertIsNot(landed, board)
        self.assertIn("Music Bot Voice Effect", landed.title)
        self.assertEqual(len(self.stack), 1)

    def test_start_from_and_a_recipe_leave_one_page_at_a_time(self):
        """The path the player walks: board, Start From, a family, a voice.

        A recipe is a press that stays on the page it was pressed on (fifty
        voices are for trying one after another); Back climbs out one page per
        press -- the family, the five families, then the board, rebuilt with
        what the recipe put in it and speaking its title again.
        """
        self.handler._open_voice_builder_menu()
        self.press(self.stack[-1], "Start From:")
        families = self.stack[-1]
        self.press(families, "Machine")
        family_page = self.stack[-1]
        self.press(family_page, "Bitcrush King")
        self.assertIs(self.stack[-1], family_page)
        self.assertEqual(len(self.stack), 3)
        self.press(family_page, "Back")
        self.assertIs(self.stack[-1], families)
        self.assertEqual(len(self.stack), 2)
        self.press(families, "Back")
        self.assertEqual(len(self.stack), 1)
        landed = self.stack[-1]
        self.assertIn("Music Bot Custom Voice", landed.title)
        labels = [label_of(item) for item in landed.items]
        self.assertTrue(any(label.startswith("Start From: ")
                            for label in labels), labels[:2])

    def test_the_world_s_page_backs_out_to_the_world(self):
        """Alt+V's page is the same page, with Back going home to the game."""
        self.handler._open_voice_effect_menu(standalone=True)
        page = self.stack[-1]
        self.assertIn("Voice Effect", page.title)
        self.assertNotIn("Music Bot", page.title)
        self.press(page, "Back")
        self.assertEqual(self.stack, [])

    def test_a_board_under_the_world_s_page_keeps_its_way_home(self):
        """A board's Back is the Voice Effect page, still in world mode."""
        self.handler._open_voice_effect_menu(standalone=True)
        self.press(self.stack[-1], "Custom Voice:")
        board = self.stack[-1]
        self.press(board, "Back")
        landed = self.stack[-1]
        self.assertIn("Voice Effect", landed.title)
        self.assertNotIn("Music Bot", landed.title)
        self.press(landed, "Back")
        self.assertEqual(self.stack, [])

    def test_a_settings_visit_keeps_its_own_back(self):
        """The mode is per visit: Settings does not inherit the world's."""
        self.handler._open_voice_effect_menu(standalone=True)
        self.press(self.stack[-1], "Back")
        self.assertEqual(self.stack, [])
        self.handler._open_voice_effect_menu(standalone=False)
        page = self.stack[-1]
        self.assertIn("Music Bot Voice Effect", page.title)
        self.handler._open_settings_menu = lambda: self.stack.append("settings")
        self.press(page, "Back")
        self.assertEqual(self.stack, ["settings"])

    def test_no_page_of_the_board_leaves_a_second_copy_of_itself(self):
        """Re-opening the page under a page is one page, not a growing pile."""
        self.handler._open_voice_effect_menu()
        self.press(self.stack[-1], "Custom Voice:")
        depths = [len(self.stack)]
        for label in ("Space: Off", "Cave", "Back"):
            self.press(self.stack[-1], label)
            depths.append(len(self.stack))
        self.assertEqual(depths, [1, 1, 1, 1])
        # And the same from the board, which leaves for the Voice Effect page.
        self.press(self.stack[-1], "Back")
        self.assertEqual(len(self.stack), 1)
        self.assertIn("Music Bot Voice Effect", self.stack[-1].title)


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
