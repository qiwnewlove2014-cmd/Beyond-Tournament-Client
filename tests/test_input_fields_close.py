"""Every prompt the client opens with `game.input.run` closes itself again.

Reported from play: Escape on the Auto-Tune Tune Amount field froze the game.
The cause is the shape of a text field rather than that menu: `game.input.run`
returns a substate whose `ck` answers True on every frame it is open -- that is
how it keeps the player's keys -- so the handler the caller passes is the only
thing that can take it off the stack. A handler that speaks and returns leaves
the field on top for good: the menu underneath never hears a key again and
neither does the player.

`test_music_bot_autotune.CustomAmountFieldTests` drives that field through the
real `Virtual_input` and `State.update`. This file walks the other prompts the
client opens and asserts the same contract on each: an empty submit -- exactly
what Escape sends -- leaves no callable substate behind. It is the audit of
every `input.run` call site in `libs/`, kept as a test so the next field cannot
quietly join the family.

The prompts that deliberately keep the field open are not here, because leaving
it open is their answer: a chat message that is too long (so it can be edited),
a near-miss `/m` in Party Sync (so the typo can be fixed), and a song request
that could not be sent. None of them can freeze the game -- the field is still
the thing taking keys, and Escape closes it.
"""

import os
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from libs import (buffer, gameplay, jukebox, menus,  # noqa: E402
                  state as state_module, tickets)
from libs.music_bot import controller  # noqa: E402


class FakeNetwork:
    def __init__(self):
        self.sent = []

    def send(self, *args, **kwargs):
        self.sent.append(args)


def open_field():
    """A gameplay whose top substate is an open text field."""
    gp = state_module.State(object())
    gp.game = SimpleNamespace(network=FakeNetwork())
    gp.substates.append(lambda: True)
    return gp


def field_is_gone(gp):
    return not any(callable(item) for item in gp.substates)


class GameplayPromptsTests(unittest.TestCase):
    """Where the player spends the most time: chat, map chat and the buffer."""

    def setUp(self):
        for target in ("libs.state.speak", "libs.gameplay.speak"):
            mock.patch(target, lambda *args, **kwargs: None).start()
        self.addCleanup(mock.patch.stopall)

    def test_chat_closes_on_escape_and_after_a_message(self):
        for message in ("", "hello"):
            with self.subTest(message=message):
                gp = open_field()
                gameplay.Gameplay.chat2(gp, message)
                self.assertTrue(field_is_gone(gp))
                if message:
                    self.assertEqual(gp.game.network.sent[0][1], "chat")

    def test_map_chat_closes_on_escape_and_after_a_message(self):
        for message in ("", "hello"):
            with self.subTest(message=message):
                gp = open_field()
                gameplay.Gameplay.map_chat2(gp, message)
                self.assertTrue(field_is_gone(gp))

    def test_the_buffer_search_closes_on_escape_and_after_a_word(self):
        class FakeBuffer:
            index = 0
            items = [SimpleNamespace(text="nothing yet"),
                     SimpleNamespace(text="the dead body is upstairs")]

            def speak_item(self):
                pass

        for message in ("", "dead"):
            with self.subTest(message=message):
                gp = open_field()
                with mock.patch.object(buffer, "buffers", [FakeBuffer()]), \
                        mock.patch.object(buffer, "bufferindex", 0):
                    gameplay.Gameplay.buffer_find(gp, message)
                self.assertTrue(field_is_gone(gp))

    def test_party_sync_chat_closes_on_escape(self):
        gp = open_field()
        gameplay.Gameplay.party_sync_chat2(gp, "")
        self.assertTrue(field_is_gone(gp))


class JukeboxPromptsTests(unittest.TestCase):
    """The cabinet's own search and paste-a-link fields."""

    def test_the_search_field_closes(self):
        gp = open_field()
        with mock.patch.object(jukebox.menu, "speak", lambda *a, **k: None):
            jukebox.menu._on_search_submit(gp.game, gp, 1, "")
        self.assertTrue(field_is_gone(gp))

    def test_the_link_field_closes(self):
        gp = open_field()
        with mock.patch.object(jukebox.menu, "speak", lambda *a, **k: None):
            jukebox.menu._on_url_submit(gp.game, gp, 1, "")
        self.assertTrue(field_is_gone(gp))


class MusicBotPromptsTests(unittest.TestCase):
    """The bot's search, link and playlist-name fields."""

    def bot(self, gp):
        bot = controller.MapMusicBot.__new__(controller.MapMusicBot)
        bot.game = gp.game
        bot._gp = gp
        bot.searching = False
        bot._find_gameplay = lambda: gp
        bot._open_playlists_menu = lambda: None
        return bot

    def test_the_search_field_closes(self):
        gp = open_field()
        with mock.patch.object(controller, "speak", lambda *a, **k: None):
            self.bot(gp)._on_search_submit("")
        self.assertTrue(field_is_gone(gp))

    def test_the_link_field_closes(self):
        gp = open_field()
        with mock.patch.object(controller, "speak", lambda *a, **k: None):
            self.bot(gp)._on_link_submit("")
        self.assertTrue(field_is_gone(gp))

    def test_the_playlist_name_field_closes(self):
        gp = open_field()
        with mock.patch.object(controller, "speak", lambda *a, **k: None):
            self.bot(gp)._on_create_playlist_submit("")
        self.assertTrue(field_is_gone(gp))


class TicketPromptsTests(unittest.TestCase):
    """Feedback's four fields, each of which returns to a menu instead."""

    def setUp(self):
        for patch in (
                mock.patch.object(tickets, "speak", lambda *a, **k: None),
                mock.patch.object(tickets, "Menu",
                                  lambda *a, **k: SimpleNamespace(
                                      add_items=lambda items: None)),
                mock.patch.object(tickets, "set_default_sounds",
                                  lambda *a, **k: None)):
            patch.start()
            self.addCleanup(patch.stop)

    def manager(self, gp):
        return tickets.Tickets(
            SimpleNamespace(network=FakeNetwork(), gameplay=gp))

    def test_sending_feedback_closes_on_escape_and_after_a_message(self):
        for message in ("", "the door does not open"):
            with self.subTest(message=message):
                gp = open_field()
                self.manager(gp).submit_feedback("general", message)
                self.assertTrue(field_is_gone(gp))
                # The field is replaced by a menu, never simply dropped.
                self.assertTrue(gp.substates)

    def test_a_reply_closes_on_escape(self):
        gp = open_field()
        self.manager(gp).send_reply({"id": 1}, "")
        self.assertTrue(field_is_gone(gp))

    def test_an_edit_closes_on_escape(self):
        gp = open_field()
        self.manager(gp).edit_ticket2({"id": 1}, "")
        self.assertTrue(field_is_gone(gp))

    def test_a_too_short_deletion_reason_is_replaced_by_a_fresh_field(self):
        """Refusing the answer reopens the field rather than leaving it used."""
        gp = open_field()
        field = gp.substates[-1]
        game = SimpleNamespace(
            network=FakeNetwork(), gameplay=gp,
            input=SimpleNamespace(run=lambda *a, **k: (lambda: True)))
        tickets.Tickets(game).confirm_delete_reason({"id": 1}, "")
        self.assertEqual(len(gp.substates), 1)
        self.assertIsNot(gp.substates[-1], field)

    def test_a_deletion_reason_closes_with_a_menu(self):
        gp = open_field()
        self.manager(gp).confirm_delete_reason({"id": 1}, "lost to a bug")
        self.assertTrue(field_is_gone(gp))
        self.assertTrue(gp.substates)


class TheSettingsPromptsReplaceTheFieldTests(unittest.TestCase):
    """`menus.py`'s prompts close with `func_call()`, not a pop.

    They are listed so the audit covers every call site: each one asks for the
    menu back, on an empty submit too. The field is replaced rather than popped,
    which is why a checker looking only for `pop_last_substate` would miss them.
    """

    def test_an_empty_submit_still_asks_for_the_menu(self):
        for configure2, value in (
                (menus.configure_jitter_buffer2, "60"),
                (menus.configure_host2, "example.org"),
                (menus.configure_port2, "7777")):
            for message in ("", value):
                with self.subTest(configure=configure2.__name__,
                                  message=message):
                    closed = []
                    game = SimpleNamespace(audio_mngr=SimpleNamespace(
                        silent_buffer=bytearray()))
                    with mock.patch.object(
                            menus, "options",
                            SimpleNamespace(
                                get=lambda key, default=None: default,
                                set=lambda *a, **k: None)), \
                            mock.patch.object(menus.speech, "speak",
                                              lambda *a, **k: None):
                        configure2(game, message, lambda: closed.append(True))
                    self.assertEqual(len(closed), 1,
                                     "the menu was not asked for")


if __name__ == "__main__":
    unittest.main()
