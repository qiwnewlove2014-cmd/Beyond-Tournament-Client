"""Starting Party Sync without leaving the session page.

Two small UI rules:

* Ctrl+F8 is the session's door even before a session exists, so a host does
  not have to open the Music Bot first just to press Start.
* Pressing Start keeps that page open. The server's first state push turns
  the same menu into the host controls in place and says the session started;
  it does not pop the host back to the world or open the invite picker.

No game, audio device, network or OpenAL is required.
"""

import os
import unittest
from types import SimpleNamespace
from unittest import mock

from libs.music_bot import gate as music_bot_gate
from libs.music_bot.menus import MusicBotMenus


class FakeMenu:
    def __init__(self, game, title, **kwargs):
        self.game = game
        self.title = title
        self.items = []
        self.pos = -1

    def add_items(self, items):
        self.items.extend(items)

    def speak_current_item(self):
        return None


class StartPageTests(unittest.TestCase):
    def setUp(self):
        music_bot_gate.clear()
        self.addCleanup(music_bot_gate.clear)
        self.addCleanup(mock.patch.stopall)
        mock.patch("libs.menu.Menu", FakeMenu).start()
        mock.patch("libs.menus.set_default_sounds").start()
        self.speak = mock.patch("libs.music_bot.menus.speak").start()

        self.gp = SimpleNamespace(substates=[], pop_calls=0)
        self.gp.add_substate = lambda menu: (
            self.gp.substates.append(menu) or menu
        )

        def pop_last_substate():
            self.gp.pop_calls += 1
            if self.gp.substates:
                return self.gp.substates.pop()

        self.gp.pop_last_substate = pop_last_substate
        self.bot = mock.MagicMock()
        self.bot._find_gameplay.return_value = self.gp
        self.bot._party_sync_pair.return_value = (self.gp, None)
        self.bot.song_requests_switch_item.return_value = (
            "Song requests: OFF", lambda: None, None,
            "Whether this session takes requests.",
        )
        self.handler = MusicBotMenus(self.bot)

    def _open_start_page(self, standalone=False):
        self.handler._open_party_sync_menu(standalone=standalone)
        return self.gp.substates[-1]

    def test_start_keeps_the_page_open_and_waits_for_the_server(self):
        menu = self._open_start_page()
        self.assertEqual(menu.items[0][0], "Start Party Sync session")

        menu.items[0][1]()

        self.bot._party_sync_send.assert_called_once_with("party_sync_start")
        self.assertIs(self.gp.substates[-1], menu)
        self.assertEqual(self.gp.pop_calls, 0)
        self.assertTrue(menu._party_sync_starting)
        self.speak.assert_called_with(
            "Starting Party Sync session. Waiting for the server..."
        )

    def test_the_state_refreshes_the_same_page_without_an_extra_menu(self):
        menu = self._open_start_page()
        menu.items[0][1]()
        ps = SimpleNamespace(
            role="host", guests=[], host_name="Anh", queue=[],
            now_playing="", cinema_cabinet="",
            end_session=lambda: None,
        )
        self.bot._party_sync_pair.return_value = (self.gp, ps)
        self.speak.reset_mock()

        self.assertTrue(self.handler.refresh_party_sync_menu())

        self.assertIs(self.gp.substates[-1], menu)
        self.assertEqual(self.gp.pop_calls, 0)
        self.assertFalse(menu._party_sync_starting)
        labels = [item[0] for item in menu.items]
        self.assertNotIn("Start Party Sync session", labels)
        self.assertIn("Invite players (any map)", labels)
        self.assertEqual(menu.pos, 0)
        self.speak.assert_called_with(
            "Party Sync started. Invite players from this menu."
        )

    def test_standalone_back_closes_to_the_world(self):
        menu = self._open_start_page(standalone=True)
        back = next(item for item in menu.items if item[0] == "Back")
        back[1]()
        self.assertEqual(self.gp.pop_calls, 1)
        self.bot._show_mode_menu.assert_not_called()

    def test_external_audio_keeps_its_parent_party_page(self):
        ps = SimpleNamespace(
            role="host", guests=[], host_name="Anh",
            cinema_cabinet="", queue=[], now_playing="",
            end_session=lambda: None,
        )
        self.bot._party_sync_pair.return_value = (self.gp, ps)
        self.handler._open_external_audio_menu = mock.MagicMock()
        self.handler._open_party_sync_menu(standalone=True)
        menu = self.gp.substates[-1]
        external = next(
            item for item in menu.items
            if item[0] == "External audio from a music program"
        )

        external[1]()

        self.handler._open_external_audio_menu.assert_called_once_with()
        self.assertIs(self.gp.substates[-1], menu)
        self.assertEqual(self.gp.pop_calls, 0)


class _Source:
    def _source(self, *parts):
        path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            *parts
        )
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read()

    def _function(self, source, name):
        start = source.index(f"    def {name}(")
        rest = source[start:]
        return rest[:rest.index("\n    def ", 1)]


class ShortcutAndStateWiringTests(_Source, unittest.TestCase):
    def test_ctrl_f8_opens_the_start_page_before_a_session_exists(self):
        body = self._function(
            self._source("libs", "gameplay.py"), "party_sync_leave_key"
        )
        self.assertIn('getattr(bot, "_open_party_sync_menu", None)', body)
        self.assertIn("opener(standalone=True)", body)
        self.assertIn("_can_use_music_bot()", body)

    def test_a_state_push_refreshes_the_open_page(self):
        body = self._function(
            self._source("libs", "event_handeler.py"), "party_sync_state"
        )
        self.assertIn('getattr(bot, "refresh_party_sync_menu", None)', body)
        self.assertIn("self.game.put(refresh_menu)", body)

    def test_the_quick_menu_offers_external_audio(self):
        body = self._function(
            self._source("libs", "gameplay.py"),
            "_open_party_sync_quick_menu",
        )
        self.assertIn(
            '"External audio from a music program", do_external_audio, None,',
            body,
        )
        self.assertIn(
            'getattr(bot_for_audio, "_open_external_audio_menu", None)', body
        )
        self.assertIn("may_stream and may_stream(own_name)", body)

    def test_the_quick_menu_never_marks_you_as_another_map(self):
        body = self._function(
            self._source("libs", "gameplay.py"),
            "_open_party_sync_quick_menu",
        )
        self.assertIn("same_player(name, own_name)", body)
        self.assertIn("not is_you and channel is not None", body)

    def test_the_quick_menu_invites_any_map(self):
        body = self._function(
            self._source("libs", "gameplay.py"),
            "_open_party_sync_quick_menu",
        )
        self.assertIn('"Invite players (any map)"', body)

    def test_external_audio_back_pops_to_its_parent(self):
        source = self._source("libs", "music_bot", "menu_pages", "external.py")
        body = source.split("def back_to_party_menu", 1)[1]
        body = body.split("def go_pick", 1)[0]
        self.assertIn("close_top()", body)
        self.assertIn("if not gp.substates:", body)


if __name__ == "__main__":
    unittest.main()
