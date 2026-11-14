"""Party Sync's cinema room line on the Ctrl+F8 quick menu.

A host's room link lived on one page only -- the Party Sync page of the Music
Bot menu -- so a host sitting in free gameplay had to walk that menu mid-song to
reach it. Ctrl+F8 is the session's own door (the roster, the invite picker, the
requests switch, End), so the line belongs there too.

These checks pin the four things that make that line right:

  * **it is the host's**, in the host branch, between the requests switch and
    End -- a guest's side of the same menu is the "Listen in cinema room" line
    and is a different decision;
  * **it reads the session's state** (`cinema_cabinet`), so the label says which
    cabinet is linked and `OFF` when none is;
  * **it asks no rank and no gate**, because the party's room is the host's own
    stream in the host's own cabinet. The Developer/Contributor rule belongs to
    the Music Bot's routing line (`Cinema Speakers: ...`), which takes a cabinet
    over for the bot; the server's `party_sync_cinema` handler matches this;
  * **it is one builder and one event**: the chooser is
    `MapMusicBot._open_party_cinema_menu` and the write is `party_sync_cinema`
    inside it, never a second sender beside it.

The last point is what keeps the two doors from drifting: the Music Bot page and
Ctrl+F8 must offer the same page and the same event.

Pins: `client/libs/gameplay.py` (`_open_party_sync_quick_menu`),
`client/libs/music_bot/menu_pages/party.py` (`_open_party_cinema_menu`).
"""

import os
import unittest


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


class QuickMenuCinemaTests(_Source, unittest.TestCase):
    def setUp(self):
        self.body = self._function(
            self._source("libs", "gameplay.py"),
            "_open_party_sync_quick_menu",
        )
        self.page = self._function(
            self._source("libs", "music_bot", "menu_pages", "party.py"),
            "_open_party_cinema_menu",
        )

    def test_the_hosts_quick_menu_offers_the_room(self):
        self.assertIn("_open_party_cinema_menu", self.body)
        self.assertIn("Party cinema room: OFF", self.body)

    def test_the_line_sits_with_the_hosts_actions(self):
        # After the /p switch (the host's other session-wide choice) and before
        # End, so a host reads it while running the session, not after it.
        self.assertLess(
            self.body.index("song_requests_switch_item"),
            self.body.index("_open_party_cinema_menu"),
        )
        self.assertLess(
            self.body.index("_open_party_cinema_menu"),
            self.body.index("End Party Sync session"),
        )

    def test_the_label_is_the_session_state(self):
        self.assertIn("cinema_cabinet", self.body)
        self.assertIn("jukebox {linked}", self.body)

    def test_no_rank_and_no_gate_are_asked(self):
        # The bot's routing line keeps the rank (`cinema_routing_allowed`) and
        # the gate; the party's link must not inherit either.
        self.assertNotIn("cinema_routing_allowed", self.body)
        self.assertNotIn("cinema_speakers", self.body)

    def test_one_builder_and_one_event(self):
        # The quick menu never sends the packet itself: the chooser that both
        # doors open is the one that writes it.
        # The exact packet, not the guest's separate `party_sync_cinema_listen`
        # line that shares the first seventeen characters.
        self.assertNotIn('"party_sync_cinema"', self.body)
        self.assertIn("party_sync_set_cinema", self.page)
        self.assertIn('"Off (private party feed only)"', self.page)

    def test_the_music_bot_routing_line_still_asks_the_rank(self):
        handler = self._source("libs", "music_bot", "cinema_handler.py")
        self.assertIn("def cinema_routing_allowed", handler)
        self.assertIn("can_use_cinema_speakers", handler)


class ServerHandlerTests(_Source, unittest.TestCase):
    """The Server's half of the same rule, read as text.

    `tools/party_sync_test.js` T22 proves the same two facts on the handler
    source; this is the client-side half of the pair, so a change on one side of
    the contract cannot pass unnoticed.
    """

    def setUp(self):
        self.handler = self._source(
            "..", "server", "libs", "event_handeler.ts")
        start = self.handler.index("this.events['party_sync_cinema']")
        self.link = self.handler[start:self.handler.index(
            "this.events['party_sync_cinema_listen']")]

    def test_the_link_asks_no_rank(self):
        self.assertNotIn("canUseCinemaSpeakers", self.link)

    def test_the_link_still_checks_the_cabinet(self):
        self.assertIn('elementName !== "jukebox"', self.link)


if __name__ == "__main__":
    unittest.main()
