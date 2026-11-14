"""Party Voice: the shapers a session's voice leg carries, and the door to them.

`libs/voice_chat._speech_line_for(mega, party)` shapes a press made inside a
Party Sync session with the same three shapers a broadcast press gets -- the
Voice Effect pedal, Auto-Tune and the harmony copies -- because the Server
delivers a member's voice to the session and nowhere else. The pedal page was
already reachable from the world (`Alt + the Speak Zone key`); Auto-Tune and
Harmony lived only under Music Bot Settings, which a guest is not allowed to
open. This file holds that gap shut:

1. **The page**: exactly the three shapers, and nothing that belongs to a song
   (no output device, no reverb, no queue) -- with its three dials only while
   the Auto-Tune switch is on, as Settings builds it.
2. **The door**: a Music Bot player still gets the pedal page (Alt+V unchanged),
   a guest in a session gets Party Voice, a guest outside one gets neither, and
   the Developer's Music Bot lock closes the party door too.
3. **The loop**: a dial's Back returns to Party Voice (`_voice_page_back`),
   while the Music Bot's own chain still returns to Settings.
4. **The locks**: a locked feature's line does not exist (`gate.filter_items`),
   and the page itself still opens for what is left.

No game, OpenAL, ffmpeg, network or audio device is required.
"""

import os
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from libs import gameplay  # noqa: E402
from libs.music_bot import autotune  # noqa: E402
from libs.music_bot import gate as music_bot_gate  # noqa: E402
from libs.music_bot import menus as menus_module  # noqa: E402


class FakeMenu:
    built = []

    def __init__(self, game, title, parrent=None):
        self.title = title
        self.items = []
        FakeMenu.built.append(self)

    def add_items(self, items):
        self.items.extend(items)

    def speak_current_item(self):
        return None


def labels_in(menu):
    """Every line's text, read the way the menu itself reads it."""
    return [item[0]() if callable(item[0]) else item[0] for item in menu.items]


class ThePartyVoicePage(unittest.TestCase):
    def setUp(self):
        FakeMenu.built = []
        self.addCleanup(mock.patch.stopall)
        music_bot_gate.apply_locked({})
        self.bot = mock.MagicMock()
        self.bot.autotune_enabled = False
        self.bot.autotune_label.return_value = "OFF"
        self.bot.autotune_speed_label.return_value = autotune.speed_label()
        self.bot.autotune_scale_label.return_value = "Chromatic"
        self.bot.autotune_amount_label.return_value = autotune.amount_label()
        self.bot.autotune_harmony_summary.return_value = "Off"
        self.bot.voice_effect_label.return_value = "Off"
        self.bot._find_gameplay.return_value = SimpleNamespace(
            add_substate=lambda item: None, pop_last_substate=lambda: None)
        self.handler = menus_module.MusicBotMenus(self.bot)
        mock.patch("libs.menu.Menu", FakeMenu).start()
        mock.patch("libs.menus.set_default_sounds").start()
        self.speak = mock.patch("libs.music_bot.menus.speak").start()

    def labels(self):
        self.handler._open_party_voice_menu()
        return labels_in(FakeMenu.built[-1])

    def test_the_page_is_the_three_shapers_and_a_back(self):
        self.assertEqual(self.labels(), ["Voice Effect: Off",
                                         "Auto-Tune: OFF",
                                         "Harmony: Off",
                                         "Back"])
        self.assertEqual(FakeMenu.built[-1].title, "Party Voice")

    def test_nothing_that_belongs_to_a_song_is_on_the_page(self):
        # A guest is not playing a song: an output device, a reverb, an
        # equalizer or a queue line would be a control with nothing behind it.
        labels = self.labels()
        for word in ("Output", "Reverb", "Equalizer", "Queue", "Muffle",
                     "Crossfade", "Broadcast"):
            self.assertFalse([label for label in labels if word in label],
                             f"{word} should not be on Party Voice: {labels}")

    def test_the_dials_exist_only_while_the_tuner_is_on(self):
        self.bot.autotune_enabled = True
        self.bot.autotune_label.return_value = "ON"
        labels = self.labels()
        self.assertEqual(labels.index("Auto-Tune: ON") + 1,
                         labels.index(f"Retune Speed: {autotune.speed_label()}"))
        self.assertIn(f"Tune Amount: {autotune.amount_label()}", labels)
        self.assertEqual(labels[-1], "Back")

    def test_a_locked_pedal_leaves_the_page_standing(self):
        music_bot_gate.apply_locked({"voice_fx": "Voice Effects"})
        labels = self.labels()
        self.assertFalse([label for label in labels
                          if label.startswith("Voice Effect")])
        self.assertIn("Auto-Tune: OFF", labels)

    def test_a_locked_tuner_takes_its_switch_and_harmony_with_it(self):
        # Harmony sits under the same feature id on this page: it is a mode of
        # Auto-Tune, and a Developer who locked the tuner did not leave the
        # copies switched on behind it.
        music_bot_gate.apply_locked({"auto_tune": "Auto-Tune"})
        self.assertEqual(self.labels(), ["Voice Effect: Off", "Back"])

    def test_the_voice_effect_row_opens_the_standalone_pedal_page(self):
        self.handler._open_party_voice_menu()
        def text(item):
            return item[0]() if callable(item[0]) else item[0]

        row = [item for item in FakeMenu.built[-1].items
               if text(item) == "Voice Effect: Off"][0]
        with mock.patch.object(self.handler,
                               "_open_voice_effect_menu") as page:
            row[1]()
        page.assert_called_once_with(standalone=True)

    def test_a_dial_backs_into_party_voice_not_into_settings(self):
        gp = SimpleNamespace(pop_last_substate=lambda: None)
        back = mock.Mock()
        with mock.patch.object(self.handler, "_open_settings_menu") as settings:
            self.handler._voice_page_back(gp, back)
        back.assert_called_once_with()
        self.assertFalse(settings.called)
        # The Music Bot's own chain keeps the visit it came from.
        with mock.patch.object(self.handler, "_open_settings_menu") as settings:
            self.handler._voice_page_back(gp, None)
        settings.assert_called_once_with()


class FakeGameplay:
    open_voice_effect_menu = gameplay.Gameplay.open_voice_effect_menu
    _party_voice_allowed = gameplay.Gameplay._party_voice_allowed
    _can_use_music_bot = gameplay.Gameplay._can_use_music_bot

    def __init__(self, music_bot, allowed=False, party_sync=None):
        self.music_bot = music_bot
        self.can_use_music_bot = allowed
        self.is_staff = False
        self.is_builder = False
        self.is_technician = False
        self.party_sync = party_sync


class ThePartyVoiceDoor(unittest.TestCase):
    def setUp(self):
        self.addCleanup(mock.patch.stopall)
        music_bot_gate.apply_locked({})
        self.bot = mock.MagicMock()

    def test_a_music_bot_player_still_gets_the_pedal_page(self):
        FakeGameplay(self.bot, allowed=True).open_voice_effect_menu()
        self.bot._open_voice_effect_menu.assert_called_once_with(standalone=True)
        self.assertFalse(self.bot._open_party_voice_menu.called)

    def test_a_guest_in_a_session_gets_party_voice(self):
        door = FakeGameplay(self.bot,
                            party_sync=SimpleNamespace(session_id="session-1"))
        door.open_voice_effect_menu()
        self.bot._open_party_voice_menu.assert_called_once_with()
        self.assertFalse(self.bot._open_voice_effect_menu.called)

    def test_a_guest_outside_a_session_is_silent(self):
        for state in (None, SimpleNamespace(session_id="")):
            door = FakeGameplay(self.bot, party_sync=state)
            door.open_voice_effect_menu()
        self.assertFalse(self.bot._open_party_voice_menu.called)
        self.assertFalse(self.bot._open_voice_effect_menu.called)

    def test_the_music_bot_lock_closes_the_party_door_too(self):
        music_bot_gate.apply_locked({"bot": "Music Bot"})
        door = FakeGameplay(self.bot,
                            party_sync=SimpleNamespace(session_id="session-1"))
        door.open_voice_effect_menu()
        self.assertFalse(self.bot._open_party_voice_menu.called)

    def test_the_voice_effect_lock_still_stops_the_pedal_page(self):
        door = FakeGameplay(self.bot, allowed=True)
        with mock.patch.object(music_bot_gate, "blocked",
                               return_value=True) as blocked:
            door.open_voice_effect_menu()
        blocked.assert_called_once_with("voice_fx")
        self.assertFalse(self.bot._open_voice_effect_menu.called)

    def test_no_music_bot_object_is_silent(self):
        door = FakeGameplay(None,
                            party_sync=SimpleNamespace(session_id="session-1"))
        door.music_bot = None
        door.open_voice_effect_menu()


if __name__ == "__main__":
    unittest.main()
