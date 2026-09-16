"""The picker a /p request opens: the asker chooses which result is queued.

A search returns five ways to play one song (versions, live takes, uploads),
and only the person who typed the song knows which one they meant. So the host
searches, the RESULTS ARE OFFERED to the asker, and the asker's pick comes back
as an index into the host's own list. Covers:

- the candidate list the host keeps (`candidates`, `offered`, `PendingPicks`)
  and the reason a pick cannot be served,
- the wire parsers (`parse_song_choices`, `parse_song_pick`),
- the picker menu itself: one line per result (`choice_line`), the withdraw
  line, and that it opens on a client whose own music bot is switched off,
- the wiring: the guest sends `party_sync_song_pick`, the host serves a pick
  on the main thread, and a session end drops every open request,
- the server side: an open request is remembered against the asker, a pick is
  relayed only for their own request, and the table is bounded.

No game, OpenAL, network or yt-dlp is required.
"""

import os
import re
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from libs import event_handeler as event_module
from libs.music_bot import controller as controller_mod
from libs.music_bot import song_requests
from libs.party_sync import (MAX_CHOICES, parse_song_choices, parse_song_pick)


def make_gp():
    gp = SimpleNamespace()
    gp.substates = []
    gp.add_substate = lambda menu: gp.substates.append(menu)
    gp.pop_last_substate = lambda: gp.substates and gp.substates.pop()
    return gp


def make_bot(enabled=False):
    """A MapMusicBot with no playback at all (the picker needs none)."""
    bot = controller_mod.MapMusicBot.__new__(controller_mod.MapMusicBot)
    bot.game = SimpleNamespace(put=lambda fn: fn())
    bot.enabled = enabled
    bot.song_picks = song_requests.PendingPicks()
    gp = make_gp()
    bot._find_gameplay = lambda: gp
    return bot, gp


class FakeMenu:
    instances = []

    def __init__(self, game, title, parrent=None):
        self.game = game
        self.title = title
        self.items = []
        FakeMenu.instances.append(self)

    def add_items(self, items):
        self.items = list(items)


def open_menu(bot, *args, **kwargs):
    FakeMenu.instances = []
    with mock.patch("libs.menu.Menu", FakeMenu), \
            mock.patch("libs.menus.set_default_sounds"):
        bot.open_song_pick_menu(*args, **kwargs)
    return FakeMenu.instances[-1] if FakeMenu.instances else None


# ── the candidates the host keeps ───────────────────────────────────────

class TestChoiceLine(unittest.TestCase):
    def test_the_line_is_the_song_and_its_time(self):
        self.assertEqual(song_requests.choice_line("A Song", 269),
                         "A Song (4:29)")
        self.assertEqual(song_requests.choice_line("A Song", 5),
                         "A Song (0:05)")
        # No duration from the search is no duration in the line, never "0:00".
        self.assertEqual(song_requests.choice_line("A Song"), "A Song")
        self.assertEqual(song_requests.choice_line("A Song", 0), "A Song")
        self.assertEqual(song_requests.choice_line("A Song", "nope"), "A Song")

    def test_no_line_carries_a_position_number(self):
        # A reader picks a line by scrolling to it; a leading "3." would only
        # make a screen reader say a number that means nothing outside this
        # menu before it says the song (the pick travels as the menu position).
        for seconds in (None, 269, "nope"):
            line = song_requests.choice_line("A Song", seconds)
            self.assertFalse(re.match(r"^\d+\.\s", line), line)

    def test_a_title_is_one_line_and_never_empty(self):
        self.assertEqual(song_requests.choice_line(" a\n\nb\tc "),
                         "a b c")
        self.assertEqual(song_requests.choice_line("   "), "Unknown")


class TestCandidates(unittest.TestCase):
    def test_only_playable_results_are_offered(self):
        found = song_requests.candidates([
            {"title": "canonical", "webpage_url": "https://y/1"},
            {"title": "direct only", "url": "https://googlevideo/2"},
            {"title": "nothing to play"},
            None,
            "not a result",
        ])
        self.assertEqual([c["title"] for c in found],
                         ["canonical", "direct only"])

    def test_what_a_queue_entry_needs_travels_with_the_candidate(self):
        found = song_requests.candidates([{
            "title": " A Song ",
            "webpage_url": "https://y/1",
            "url": "https://googlevideo/1",
            "http_headers": {"User-Agent": "x"},
            "duration": 200.7,
        }])
        self.assertEqual(found[0], {
            "title": "A Song",
            "duration": 200,
            "webpage_url": "https://y/1",
            "direct_url": "https://googlevideo/1",
            "http_headers": {"User-Agent": "x"},
        })

    def test_the_list_and_every_field_are_bounded(self):
        many = [{"title": f"s{i}", "webpage_url": f"https://y/{i}"}
                for i in range(30)]
        self.assertEqual(len(song_requests.candidates(many)),
                         song_requests.CANDIDATE_LIMIT)
        found = song_requests.candidates(
            [{"title": "a\nb" + "x" * 400, "webpage_url": "https://y/1",
              "duration": -5}])
        self.assertNotIn("\n", found[0]["title"])
        self.assertEqual(len(found[0]["title"]),
                         song_requests.SHARE_TITLE_MAX)
        self.assertEqual(found[0]["duration"], 0)

    def test_what_travels_is_names_and_nothing_else(self):
        offered = song_requests.offered(song_requests.candidates([
            {"title": "A Song", "webpage_url": "https://y/1",
             "url": "https://googlevideo/1", "duration": 60},
        ]))
        self.assertEqual(offered, [{"title": "A Song", "duration": 60}])
        self.assertNotIn("http", str(offered))


class TestPendingPicks(unittest.TestCase):
    RESULTS = [
        {"title": "Studio", "webpage_url": "https://y/1", "url": "u1"},
        {"title": "Live", "webpage_url": "https://y/2"},
    ]

    def test_a_pick_resolves_against_the_hosts_own_results(self):
        picks = song_requests.PendingPicks()
        picks.add("r1", "Bob", "a song", self.RESULTS)
        self.assertEqual(picks.offered("r1"), [
            {"title": "Studio", "duration": 0},
            {"title": "Live", "duration": 0},
        ])
        self.assertEqual(picks.query("r1"), "a song")
        found, note = picks.pick("r1", "Bob", 1)
        self.assertIsNone(note)
        self.assertEqual(found["webpage_url"], "https://y/2")
        # One request, one song.
        self.assertEqual(picks.pick("r1", "Bob", 0)[0], None)

    def test_only_the_asker_may_answer_a_request(self):
        picks = song_requests.PendingPicks()
        picks.add("r1", "Bob", "a song", self.RESULTS)
        found, note = picks.pick("r1", "Carol", 0)
        self.assertIsNone(found)
        self.assertIn("somebody else", note)
        # ...and Bob's own request is still open afterwards.
        self.assertEqual(picks.pick("r1", "Bob", 0)[0]["title"], "Studio")

    def test_a_pick_outside_the_list_is_refused(self):
        picks = song_requests.PendingPicks()
        picks.add("r1", "Bob", "a song", self.RESULTS)
        for index in (2, 99, "nope", None):
            found, note = picks.pick("r1", "Bob", index)
            self.assertIsNone(found, repr(index))
            self.assertIn("not one of the songs", note)

    def test_withdrawing_closes_the_request(self):
        picks = song_requests.PendingPicks()
        picks.add("r1", "Bob", "a song", self.RESULTS)
        found, note = picks.pick("r1", "Bob", -1)
        self.assertIsNone(found)
        self.assertIs(note, song_requests.WITHDRAWN)
        self.assertEqual(picks.offered("r1"), [])
        self.assertEqual(len(picks), 0)

    def test_a_request_that_is_not_here_has_no_choices(self):
        picks = song_requests.PendingPicks()
        self.assertEqual(picks.offered("r9"), [])
        self.assertEqual(picks.query("r9"), "")
        found, note = picks.pick("r9", "Bob", 0)
        self.assertIsNone(found)
        self.assertEqual(note, song_requests.NO_CHOICES)

    def test_a_search_with_nothing_to_play_is_not_offered(self):
        picks = song_requests.PendingPicks()
        self.assertEqual(picks.add("r1", "Bob", "x", [{"title": "no url"}]), [])
        self.assertEqual(len(picks), 0)

    def test_the_table_is_bounded_and_drops_the_oldest(self):
        picks = song_requests.PendingPicks(limit=2)
        for i in range(3):
            picks.add(f"r{i}", "Bob", f"song {i}", self.RESULTS)
        self.assertEqual(len(picks), 2)
        self.assertEqual(picks.offered("r0"), [])
        self.assertTrue(picks.offered("r2"))

    def test_an_ended_session_forgets_everything(self):
        picks = song_requests.PendingPicks()
        picks.add("r1", "Bob", "a song", self.RESULTS)
        picks.add("r2", "Carol", "another", self.RESULTS)
        picks.forget("r1")
        self.assertEqual(len(picks), 1)
        picks.forget()
        self.assertEqual(len(picks), 0)


# ── what arrives off the wire ───────────────────────────────────────────

class TestParseChoices(unittest.TestCase):
    def test_a_valid_offer_is_normalized(self):
        parsed = parse_song_choices({
            "request_id": " r3 ",
            "query": " a   song ",
            "items": [
                {"title": " Studio ", "duration": 269},
                {"title": "Live"},
            ],
        })
        self.assertEqual(parsed, {
            "request_id": "r3",
            "query": "a song",
            "items": [{"title": "Studio", "duration": 269},
                      {"title": "Live", "duration": 0}],
        })

    def test_an_offer_with_nothing_to_choose_is_no_menu(self):
        for payload in (None, [], "x", {"items": []},
                        {"request_id": "r1", "items": [None, 3, {}]},
                        {"items": [{"title": "A"}]},
                        {"request_id": "r1"}):
            self.assertIsNone(parse_song_choices(payload), repr(payload))

    def test_everything_is_bounded_and_one_line(self):
        parsed = parse_song_choices({
            "request_id": "r1",
            "items": [{"title": "a\nb" + "x" * 400, "duration": 10 ** 9}]
            + [{"title": f"s{i}"} for i in range(50)],
        })
        self.assertEqual(len(parsed["items"]), MAX_CHOICES)
        self.assertNotIn("\n", parsed["items"][0]["title"])
        self.assertEqual(parsed["items"][0]["duration"], 0)


class TestParsePick(unittest.TestCase):
    def test_a_valid_pick_reads(self):
        self.assertEqual(parse_song_pick(
            {"from": "Bob", "request_id": "r3", "index": 2}),
            {"from": "Bob", "request_id": "r3", "index": 2})
        # -1 is the withdraw the menu's last line sends.
        self.assertEqual(parse_song_pick(
            {"from": "Bob", "request_id": "r3", "index": -1})["index"], -1)

    def test_a_pick_without_a_real_request_or_index_is_dropped(self):
        for payload in (None, [], "x", {"from": "Bob", "request_id": "r1"},
                        {"from": "Bob", "index": 0},
                        {"request_id": "r1", "index": 0},
                        {"from": "Bob", "request_id": "r1", "index": "two"},
                        {"from": "Bob", "request_id": "r1", "index": -2},
                        {"from": "Bob", "request_id": "r1", "index": 999}):
            self.assertIsNone(parse_song_pick(payload), repr(payload))


# ── the menu itself ─────────────────────────────────────────────────────

class TestThePicker(unittest.TestCase):
    ITEMS = [{"title": "Studio", "duration": 269},
             {"title": "Live", "duration": 0}]

    def test_the_picker_lists_the_offered_songs_with_one_line_each(self):
        bot, gp = make_bot()
        menu = open_menu(bot, "r3", self.ITEMS, "a song")
        self.assertEqual(menu.title, "Pick a song: a song")
        self.assertEqual([label for label, _ in menu.items],
                         ["Studio (4:29)", "Live", "Nothing, thanks"])
        self.assertEqual(len(gp.substates), 1)

    def test_choosing_sends_the_index_of_the_line_that_was_chosen(self):
        bot, gp = make_bot()
        picked = []
        open_menu(bot, "r3", self.ITEMS, "a song", on_pick=picked.append)
        with mock.patch("libs.music_bot.controller.speak") as said:
            for label, callback in FakeMenu.instances[-1].items:
                if label == "Live":
                    callback()
        self.assertEqual(picked, [1])
        self.assertEqual(gp.substates, [])                 # the menu closed
        said.assert_called_once_with("Live")               # read back

    def test_the_last_line_withdraws_instead_of_picking(self):
        bot, gp = make_bot()
        picked = []
        open_menu(bot, "r3", self.ITEMS, "a song", on_pick=picked.append)
        for label, callback in FakeMenu.instances[-1].items:
            if label == "Nothing, thanks":
                callback()
        self.assertEqual(picked, [-1])
        self.assertEqual(gp.substates, [])

    def test_a_listener_with_their_own_music_bot_off_still_gets_the_picker(self):
        # The asker may have no music bot running at all: a picker needs no
        # playback, no sources and no bot state -- only a menu.
        bot, gp = make_bot(enabled=False)
        menu = open_menu(bot, "r3", self.ITEMS, "a song")
        self.assertIsNotNone(menu)
        self.assertEqual(len(menu.items), 3)

    def test_nothing_to_choose_says_so_instead_of_opening_an_empty_menu(self):
        bot, gp = make_bot()
        with mock.patch("libs.music_bot.controller.speak") as said:
            menu = open_menu(bot, "r3", [], "a song")
        self.assertIsNone(menu)
        self.assertEqual(gp.substates, [])
        said.assert_called_once_with(song_requests.NO_CHOICES)

    def test_a_result_that_cannot_be_read_is_skipped_not_shown_blank(self):
        bot, gp = make_bot()
        menu = open_menu(bot, "r3", [None, self.ITEMS[0]], "a song")
        self.assertEqual([label for label, _ in menu.items],
                         ["Studio (4:29)", "Nothing, thanks"])


# ── wiring ──────────────────────────────────────────────────────────────

class TestWiring(unittest.TestCase):
    def _source(self, *parts):
        path = os.path.join(os.path.dirname(__file__), "..", *parts)
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read()

    def _function(self, source, name):
        marker = f"def {name}("
        start = source.index(marker)
        rest = source[start:]
        return rest[:rest.index("\n    def ", 1)]

    def _ts_method(self, source, name):
        """One TypeScript method body (deeper `}` lines do not end it)."""
        for marker in (f"\n    {name}(", f"\n    private {name}("):
            if marker in source:
                rest = source[source.index(marker):]
                return rest[:rest.index("\n    }", 1)]
        raise AssertionError(f"no method {name}")

    def test_the_asker_opens_the_picker_and_answers_with_an_index(self):
        handler = event_module.EventHandeler.__new__(
            event_module.EventHandeler)
        bot, gp = make_bot()
        sent = []
        handler.gameplay = SimpleNamespace(music_bot=bot)
        handler.game = SimpleNamespace(
            gameplay=handler.gameplay, put=lambda fn: fn(),
            network=SimpleNamespace(
                send=lambda channel, event, data=None: sent.append((event, data))))
        FakeMenu.instances = []
        with mock.patch("libs.menu.Menu", FakeMenu), \
                mock.patch("libs.menus.set_default_sounds"):
            handler.party_sync_song_choices({
                "request_id": "r3", "query": "a song",
                "items": [{"title": "Studio", "duration": 269}],
            })
            self.assertEqual(len(gp.substates), 1)
            for _label, callback in FakeMenu.instances[-1].items:
                callback()
                break
        self.assertEqual(sent, [("party_sync_song_pick",
                                 {"request_id": "r3", "index": 0})])

    def test_a_junk_offer_opens_no_menu(self):
        handler = event_module.EventHandeler.__new__(
            event_module.EventHandeler)
        bot, gp = make_bot()
        handler.gameplay = SimpleNamespace(music_bot=bot)
        handler.game = SimpleNamespace(put=lambda fn: fn())
        handler.party_sync_song_choices({"items": []})
        self.assertEqual(gp.substates, [])

    def test_the_host_serves_a_pick_on_the_main_thread(self):
        body = self._function(
            self._source("libs", "event_handeler.py"), "party_sync_song_pick")
        self.assertIn("parse_song_pick", body)
        self.assertIn("serve_song_pick", body)
        self.assertIn('pick["from"]', body)
        self.assertIn("self.game.put", body)

    def test_the_aske_side_never_names_a_song(self):
        # The pick is an index; a title or a URL from a client would be a way
        # to name a song into somebody else's queue.
        body = self._function(
            self._source("libs", "event_handeler.py"),
            "party_sync_song_choices")
        self.assertIn('"index": index', body)
        for forbidden in ("title", "url", "webpage"):
            self.assertNotIn(f'"{forbidden}"', body)

    def test_a_session_end_drops_the_open_requests(self):
        body = self._function(
            self._source("libs", "event_handeler.py"),
            "_clear_party_song_requests")
        self.assertIn("song_picks", body)

    def test_one_wording_for_a_result_line(self):
        # The host's own search menu and an asker's picker show the same
        # results, so both ask song_requests for the line.
        controller = self._source("libs", "music_bot", "controller.py")
        results = self._function(controller, "_show_results_menu")
        self.assertIn("song_requests.choice_line", results)
        # And neither menu invents a position number of its own: the reader
        # hears the song, and the pick travels as the menu position.
        self.assertNotIn("i + 1", results)
        self.assertNotIn("index + 1", results)
        picker = self._function(controller, "open_song_pick_menu")
        self.assertIn("song_requests.choice_line", picker)
        self.assertNotIn("index + 1", picker)
        self.assertIn("song_requests.NO_CHOICES", picker)

    def test_the_server_remembers_who_a_request_belongs_to(self):
        manager = self._source("..", "server", "libs", "party_sync.ts")
        self.assertIn("pendingPicks: new Map()", manager)
        request = self._ts_method(manager, "songRequest")
        self.assertIn("session.pendingPicks.set(requestId, key)", request)
        self.assertIn("PARTY_SYNC_MAX_PENDING_PICKS", request)
        self.assertIn("session.pendingPicks.delete(answered)",
                      self._ts_method(manager, "songResult"))
        self.assertIn("session.pendingPicks.clear()",
                      self._ts_method(manager, "endSession"))
        pick = self._ts_method(manager, "songPick")
        self.assertIn("session.pendingPicks.get(requestId) !== key", pick)
        self.assertIn("index < -1", pick)
        handler = self._source("..", "server", "libs", "event_handeler.ts")
        for event in ("party_sync_song_choices", "party_sync_song_pick"):
            self.assertIn(f"events['{event}']", handler, event)


if __name__ == "__main__":
    unittest.main()
