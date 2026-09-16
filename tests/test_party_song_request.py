"""Party Sync song requests: `/p <song>` in the party room.

A guest hears the host's music, but the queue lives on the HOST's machine
(YouTubeSearcher + next_up_queue are client-side; the server has no music-bot
queue at all). So a request travels three legs — guest -> server (membership
and rate limit) -> host (search, queue) -> the room — and this file pins the
two client ends of it:

* what a party chat line means (`/p` is read as a request in the Party Sync
  room only, and never becomes a chat message, so nothing about map chat or
  server commands changes),
* the host's own rules (libs/music_bot/song_requests.py: the switch, the
  cooldown, the repeat memory, the request quota, the queue entry that says
  whose song it is),
* and the wiring: the request packet is sent by a guest and answered by the
  host, the switch only exists for a host, and the server has the schemas and
  routes that carry both directions.
"""

import os
import sys
import threading
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from libs.music_bot import controller as controller_mod
from libs.music_bot import song_requests
from libs.music_bot.song_requests import SongRequestBoard
from libs.party_sync import (SONG_REQUEST_COMMANDS, PartySyncState,
                             clean_song_query, near_song_command,
                             parse_chat_request, parse_song_request)


# ── what a party chat line means ────────────────────────────────────────

class TestChatRequest(unittest.TestCase):
    def test_the_slash_forms_ask_for_a_song(self):
        for line in ("/m hello", "/M hello", "/p hello", "/P hello",
                     "/play hello", "  /m  hello  ", "/m hello   world"):
            self.assertEqual(parse_chat_request(line), "hello world" if "world" in line
                             else "hello", line)

    def test_every_spelling_the_client_ever_advertised_still_works(self):
        # `/m` is the one the menus say; `/p` and `/play` shipped first. A
        # spelling that stopped being honoured would not be refused -- the line
        # would be SAID IN THE ROOM as chat instead, which is a worse failure
        # than not having the alias at all.
        self.assertEqual(SONG_REQUEST_COMMANDS[0], "/m")
        for spelling in SONG_REQUEST_COMMANDS:
            self.assertEqual(parse_chat_request(f"{spelling} a song"), "a song",
                             spelling)
            self.assertEqual(parse_chat_request(spelling.upper() + " a song"),
                             "a song", spelling)
        for old in ("/p", "/play"):
            self.assertIn(old, SONG_REQUEST_COMMANDS)

    def test_a_bare_command_is_an_empty_query(self):
        for line in ("/m", "/M", "/p", "/P", "/play", " /m "):
            self.assertEqual(parse_chat_request(line), "", line)

    def test_ordinary_chat_is_left_alone(self):
        for line in ("hello", "/help", "hello /m there", "", None, 5,
                     "/mn hello", "//m x"):
            self.assertIsNone(parse_chat_request(line), repr(line))

    def test_a_query_is_one_line_and_capped(self):
        self.assertEqual(clean_song_query("a\n\nb\tc"), "a b c")
        self.assertEqual(len(clean_song_query("x" * 500)), 100)
        self.assertEqual(clean_song_query(None), "")


class TestRequestPacket(unittest.TestCase):
    def test_a_valid_request_is_normalized(self):
        parsed = parse_song_request({
            "from": " Bob ", "query": "  a   b ", "request_id": "r7",
        })
        self.assertEqual(parsed, {"from": "Bob", "query": "a b", "request_id": "r7"})

    def test_a_request_without_a_name_a_song_or_an_id_is_ignored(self):
        for payload in (None, {}, {"from": "Bob"}, {"query": "x", "request_id": "r1"},
                        {"from": "Bob", "query": "  ", "request_id": "r1"},
                        {"from": "Bob", "query": "x", "request_id": ""}):
            self.assertIsNone(parse_song_request(payload), payload)


# ── the session's switch travels with the state ─────────────────────────

class TestSessionFlag(unittest.TestCase):
    def _state(self, **extra):
        payload = {
            "session_id": "alice:1",
            "host": {"name": "Alice", "voice_channel": 20},
            "guests": [{"name": "Bob", "voice_channel": 21}],
        }
        payload.update(extra)
        return payload

    def test_the_hosts_switch_arrives_with_the_state(self):
        ps = PartySyncState()
        self.assertFalse(ps.song_requests)          # closed until told
        ps.apply_state(self._state(song_requests=True), "Bob")
        self.assertTrue(ps.song_requests)
        ps.apply_state(self._state(song_requests=False), "Bob")
        self.assertFalse(ps.song_requests)

    def test_a_payload_without_the_field_reads_closed(self):
        ps = PartySyncState()
        ps.apply_state(self._state(), "Bob")
        self.assertFalse(ps.song_requests)
        # Only a real boolean opens it: a string "true" is not a switch.
        ps.apply_state(self._state(song_requests="true"), "Bob")
        self.assertFalse(ps.song_requests)

    def test_ending_the_session_closes_it(self):
        ps = PartySyncState()
        ps.apply_state(self._state(song_requests=True), "Bob")
        ps.end_session()
        self.assertFalse(ps.song_requests)
        self.assertIsNone(ps.role)


# ── the host's rules ───────────────────────────────────────────────────

class TestRequestBoard(unittest.TestCase):
    def setUp(self):
        self.now = 1000.0
        self.board = SongRequestBoard(clock=lambda: self.now)

    def test_an_open_session_serves_a_request(self):
        self.assertIsNone(self.board.refusal("Bob", "a song"))

    def test_closed_bot_off_and_empty_queries_are_refused(self):
        self.assertIn("closed", self.board.refusal("Bob", "x", open_=False))
        self.assertIn("not running",
                      self.board.refusal("Bob", "x", bot_running=False))
        self.assertIn("Which song", self.board.refusal("Bob", "   "))
        self.assertIn("name", self.board.refusal("", "x"))

    def test_one_request_at_a_time(self):
        self.board.note_served("Bob", "a song")
        reason = self.board.refusal("Bob", "another song")
        self.assertIn("One request at a time", reason)
        self.now += song_requests.COOLDOWN_S + 1
        self.assertIsNone(self.board.refusal("Bob", "another song"))
        # Somebody else is not affected by Bob's cooldown.
        self.assertIsNone(self.board.refusal("Carol", "a song"))

    def test_the_same_song_is_not_asked_for_twice(self):
        self.board.note_served("Bob", "A Song")
        self.now += song_requests.COOLDOWN_S + 1
        self.assertIn("already asked", self.board.refusal("Bob", "a song"))
        self.now += song_requests.DEDUPE_S
        self.assertIsNone(self.board.refusal("Bob", "a song"))

    def test_listeners_share_a_request_quota_in_the_queue(self):
        queue = [{"title": "host song"}]
        self.assertIsNone(self.board.refusal("Bob", "mine", queue=queue))
        queue += [{"title": f"guest {i}", "requested_by": "Bob"}
                  for i in range(song_requests.MAX_IN_QUEUE)]
        self.assertIn("full", self.board.refusal("Carol", "one more", queue=queue))
        # The host's own songs never use up the listeners' slots.
        self.assertEqual(self.board.count_in_queue(queue),
                         song_requests.MAX_IN_QUEUE)

    def test_forgetting_clears_a_requester_or_the_session(self):
        self.board.note_served("Bob", "a song")
        self.board.forget("Bob")
        self.assertIsNone(self.board.refusal("Bob", "a song"))
        self.board.note_served("Carol", "x")
        self.board.forget()
        self.assertIsNone(self.board.refusal("Carol", "x"))

    def test_the_queue_line_says_whose_song_it_is(self):
        self.assertEqual(song_requests.requested_by({"requested_by": "Bob"}), "Bob")
        self.assertEqual(song_requests.requested_by({"title": "x"}), "")
        self.assertEqual(song_requests.requested_by(None), "")
        self.assertEqual(
            song_requests.request_line("A Song", "Bob"),
            "Queued: A Song (requested by Bob)")
        self.assertEqual(
            song_requests.request_line("A Song", "Bob", waiting=3),
            "Queued: A Song (requested by Bob; 3 waiting)")
        self.assertEqual(
            song_requests.request_line("A Song", "Bob", started=True),
            "Playing now: A Song (requested by Bob)")


# ── a mistyped command ─────────────────────────────────────────────────

class TestNearMiss(unittest.TestCase):
    """A near miss of the request command is a hint, not a chat line.

    Party chat is plain text: a slash line this room does not understand is
    not refused, it is SAID IN THE ROOM. So a typo has to be caught here, or
    the request is announced to everybody as somebody's message.
    """

    def test_a_mistyped_command_gets_a_hint_that_names_it(self):
        for line, word in (("/mp a song", "/mp"), ("/mm", "/mm"),
                           ("/mmm a song", "/mmm"), ("/pl x", "/pl"),
                           ("/playy", "/playy"), ("/m.", "/m."),
                           ("/pn a song", "/pn"), ("//m x", "//m"),
                           ("/x hey", "/x")):
            hint = near_song_command(line)
            self.assertTrue(hint, line)
            self.assertIn(word, hint, line)          # what the game read
            self.assertIn("/m ", hint, line)        # and what to type
            self.assertNotIn("\n", hint)

    def test_a_real_command_is_never_a_near_miss(self):
        # `parse_chat_request` owns these; the hint must not shadow it.
        for line in ("/m a song", "/M a song", "/p a song", "/play a song",
                     "/m", "/P"):
            self.assertIsNone(near_song_command(line), line)
            self.assertIsNotNone(parse_chat_request(line), line)

    def test_ordinary_chat_is_left_alone(self):
        # Guessing at a message is worse than sending it: only a line one edit
        # from a request command is worth interrupting.
        for line in ("hello", "/help", "/help me", "/who", "/hello",
                     "/mmates hello", "/myname is bob", "/no", "/", "  ",
                     "", None, 5, "/เล่น x", "/messages"):
            self.assertIsNone(near_song_command(line), repr(line))

    def test_the_mistyped_word_is_short_and_one_line(self):
        hint = near_song_command("/" + "m" * 200)
        self.assertIn("/" + "m" * 23, hint)
        self.assertNotIn("/" + "m" * 24, hint)


class TestNearMissInTheRoom(unittest.TestCase):
    """The typo is spoken instead of being said in the room."""

    def test_a_typo_is_not_sent_and_the_input_stays_open(self):
        gp, network = make_gameplay()
        popped = []
        gp.pop_last_substate = lambda: popped.append(True)
        with mock.patch("libs.gameplay.speak") as said:
            gp.party_sync_chat2("/mp a song")
        self.assertEqual(network.sent, [])
        self.assertEqual(popped, [])                 # the typo can be fixed
        self.assertIn("/mp", said.call_args[0][0])
        self.assertIn("/m ", said.call_args[0][0])

    def test_a_message_that_only_looks_like_a_command_still_goes(self):
        gp, network = make_gameplay()
        with mock.patch("libs.gameplay.speak"):
            gp.party_sync_chat2("/mmates hello everyone")
        self.assertEqual([e for _, e, _ in network.sent], ["party_sync_chat"])


# ── the host's bot ─────────────────────────────────────────────────────

class ImmediateThread:
    """A Thread that runs its target at once (the search is a worker)."""

    def __init__(self, target=None, daemon=None, **kwargs):
        self._target = target

    def start(self):
        if self._target is not None:
            self._target()


class Recorder:
    def __init__(self):
        self.sent = []
        self.puts = []

    def send(self, channel, event, data=None):
        self.sent.append((channel, event, data))

    def put(self, fn):
        self.puts.append(fn)
        fn()


def make_bot(host=True, open_=True, enabled=True):
    bot = controller_mod.MapMusicBot.__new__(controller_mod.MapMusicBot)
    bot.game = SimpleNamespace(put=lambda fn: fn(), network=Recorder())
    bot.enabled = enabled
    bot.playing = True
    bot.is_loading_stream = False
    bot.searching = False
    bot.next_up_queue = []
    bot.queue_mode = False
    bot.song_requests_open = open_
    bot.song_requests = SongRequestBoard()
    bot.song_picks = song_requests.PendingPicks()
    bot._local_pick_seq = 0
    bot.party_sync_force_upload = False
    ps = PartySyncState()
    ps.role = "host" if host else "guest"
    ps.session_id = "s1"
    ps.host_name = "Alice"
    ps.song_requests = open_
    bot._party_sync_pair = lambda: (SimpleNamespace(game=bot.game), ps)
    bot._find_gameplay = lambda: SimpleNamespace(game=bot.game)
    return bot


class TestHostSide(unittest.TestCase):
    def _patch(self, results):
        class FakeSearcher:
            calls = []

            @staticmethod
            def search(query, count=1):
                FakeSearcher.calls.append((query, count))
                return list(results)

        said = mock.MagicMock()
        return (
            mock.patch.object(controller_mod, "YouTubeSearcher", FakeSearcher),
            mock.patch.object(controller_mod.threading, "Thread", ImmediateThread),
            mock.patch.object(controller_mod, "speak", said),
            FakeSearcher,
            said,
        )

    def test_a_served_request_offers_the_asker_a_choice(self):
        # A search returns five ways to play one song, so the asker picks: the
        # host offers what it found and queues NOTHING until the choice lands.
        patcher, thread_patch, speak_patch, searcher, _said = self._patch([
            {"title": "Studio", "webpage_url": "https://y/1", "url": "u1",
             "duration": 269},
            {"title": "Live", "webpage_url": "https://y/2", "duration": 300},
            {"title": "Unplayable"},
        ])
        bot = make_bot()
        with patcher, thread_patch, speak_patch:
            served = bot.queue_song_request("Bob", "a song", "r3")
        self.assertTrue(served)
        self.assertEqual(searcher.calls,
                         [("a song", song_requests.CANDIDATE_LIMIT)])
        self.assertEqual(bot.next_up_queue, [])
        channel, event, payload = bot.game.network.sent[-1]
        self.assertEqual(event, "party_sync_song_choices")
        self.assertEqual(payload["to"], "Bob")
        self.assertEqual(payload["request_id"], "r3")
        self.assertEqual(payload["query"], "a song")
        self.assertEqual(payload["items"], [
            {"title": "Studio", "duration": 269},
            {"title": "Live", "duration": 300},
        ])
        # The URLs stay on this machine: the pick comes back as an index.
        self.assertNotIn("http", str(payload["items"]))

    def test_the_pick_is_queued_under_the_askers_name(self):
        bot = make_bot()
        bot.song_picks.add("r3", "Bob", "a song", [
            {"title": "Studio", "webpage_url": "https://y/1", "url": "u1"},
            {"title": "Live", "webpage_url": "https://y/2"},
        ])
        with mock.patch.object(controller_mod, "speak"):
            self.assertTrue(bot.serve_song_pick("Bob", "r3", 1))
        entry = bot.next_up_queue[0]
        self.assertEqual(entry["title"], "Live")
        self.assertEqual(entry["requested_by"], "Bob")
        self.assertEqual(entry["webpage_url"], "https://y/2")
        channel, event, payload = bot.game.network.sent[-1]
        self.assertEqual(event, "party_sync_song_result")
        self.assertEqual(payload["to"], "Bob")
        self.assertEqual(payload["request_id"], "r3")
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["title"], "Live")
        self.assertEqual(payload["waiting"], 1)

    def test_only_the_asker_answers_their_own_request(self):
        bot = make_bot()
        bot.song_picks.add("r3", "Bob", "a song", [
            {"title": "A Song", "webpage_url": "https://y/1"},
        ])
        with mock.patch.object(controller_mod, "speak"):
            self.assertFalse(bot.serve_song_pick("Carol", "r3", 0))
            self.assertIn("somebody else",
                          bot.game.network.sent[-1][2]["reason"])
            self.assertFalse(bot.serve_song_pick("Bob", "r3", 7))
            self.assertIn("not one of the songs",
                          bot.game.network.sent[-1][2]["reason"])
            self.assertFalse(bot.serve_song_pick("Bob", "gone", 0))
            self.assertIn("no longer open",
                          bot.game.network.sent[-1][2]["reason"])
            # Nothing was queued by any of that, and the real pick still works.
            self.assertEqual(bot.next_up_queue, [])
            self.assertTrue(bot.serve_song_pick("Bob", "r3", 0))
            # One request, one song: the same id cannot be answered twice.
            self.assertFalse(bot.serve_song_pick("Bob", "r3", 0))
        self.assertEqual(len(bot.next_up_queue), 1)

    def test_withdrawing_a_request_says_nothing_to_the_room(self):
        bot = make_bot()
        bot.song_picks.add("r3", "Bob", "a song", [
            {"title": "A Song", "webpage_url": "https://y/1"},
        ])
        with mock.patch.object(controller_mod, "speak"):
            self.assertFalse(bot.serve_song_pick("Bob", "r3", -1))
        self.assertEqual(bot.game.network.sent, [])
        self.assertEqual(bot.next_up_queue, [])
        # ...and the request is gone, so it cannot be picked afterwards.
        with mock.patch.object(controller_mod, "speak"):
            self.assertFalse(bot.serve_song_pick("Bob", "r3", 0))

    def test_a_refusal_never_reaches_the_search(self):
        patcher, thread_patch, speak_patch, searcher, _said = self._patch([])
        for kwargs, expected in (
            ({"open_": False}, "closed"),
            ({"enabled": False}, "not running"),
        ):
            bot = make_bot(**kwargs)
            with patcher, thread_patch, speak_patch:
                self.assertFalse(bot.queue_song_request("Bob", "a song", "r1"))
            self.assertEqual(searcher.calls, [])
            payload = bot.game.network.sent[-1][2]
            self.assertFalse(payload["ok"])
            self.assertIn(expected, payload["reason"])
            self.assertEqual(payload["to"], "Bob")

    def test_no_results_is_answered_as_a_refusal(self):
        patcher, thread_patch, speak_patch, searcher, _said = self._patch([])
        bot = make_bot()
        with patcher, thread_patch, speak_patch:
            self.assertTrue(bot.queue_song_request("Bob", "nothing", "r2"))
        payload = bot.game.network.sent[-1][2]
        self.assertFalse(payload["ok"])
        self.assertIn("No song found", payload["reason"])
        self.assertEqual(bot.next_up_queue, [])

    def test_the_hosts_own_request_opens_the_picker_here(self):
        patcher, thread_patch, speak_patch, searcher, said = self._patch([
            {"title": "Mine", "webpage_url": "https://y/2"},
        ])
        bot = make_bot()
        opened = []
        bot.open_song_pick_menu = lambda *a, **k: opened.append((a, k))
        with patcher, thread_patch, speak_patch:
            self.assertTrue(bot.queue_song_request("Alice", "mine"))
        # A host asking their own bot relays nothing and queues nothing yet:
        # the same picker opens on this machine.
        self.assertEqual(bot.game.network.sent, [])
        self.assertEqual(len(opened), 1)
        self.assertEqual(bot.next_up_queue, [])
        local_key = opened[0][0][0]
        self.assertTrue(
            local_key.startswith(song_requests.LOCAL_REQUEST_ID), local_key)
        with mock.patch.object(controller_mod, "speak") as spoken:
            self.assertTrue(bot.serve_song_pick("Alice", local_key, 0))
        self.assertEqual(bot.next_up_queue[-1]["requested_by"], "Alice")
        self.assertIn("Mine", spoken.call_args[0][0])

    def test_each_local_search_gets_its_own_key(self):
        # The host's own /p keys every search separately: a picker left open
        # from an earlier request resolves its index against the list IT was
        # shown, never against a newer one (which would queue another song).
        bot = make_bot()
        keys = []
        bot.open_song_pick_menu = lambda *a, **k: keys.append(a[0])
        results = [{"title": "First", "webpage_url": "https://y/1"}]
        bot._offer_song_choices("Alice", "mine", None, results)
        bot._offer_song_choices("Alice", "mine again", None, results)
        self.assertEqual(len(keys), 2)
        self.assertNotEqual(keys[0], keys[1])
        self.assertTrue(all(key.startswith(song_requests.LOCAL_REQUEST_ID)
                            for key in keys), keys)
        with mock.patch.object(controller_mod, "speak"):
            self.assertTrue(bot.serve_song_pick("Alice", keys[0], 0))
        self.assertEqual(bot.next_up_queue[-1]["title"], "First")

    def test_the_switch_is_announced_to_the_session(self):
        bot = make_bot(open_=True)
        self.assertFalse(bot.toggle_song_requests())        # now closed
        self.assertFalse(bot.song_requests_open)
        channel, event, payload = bot.game.network.sent[-1]
        self.assertEqual(event, "party_sync_song_requests")
        self.assertEqual(payload, {"open": False})
        self.assertTrue(bot.toggle_song_requests())         # and open again
        self.assertEqual(bot.game.network.sent[-1][2], {"open": True})
        # A guest never announces anything: the queue is not theirs.
        guest = make_bot(host=False)
        guest.announce_song_requests()
        self.assertEqual(guest.game.network.sent, [])

    def test_a_blocked_request_does_not_open_the_cooldown(self):
        bot = make_bot(open_=False)
        patcher, thread_patch, speak_patch, _searcher, _said = self._patch([])
        with patcher, thread_patch, speak_patch:
            bot.queue_song_request("Bob", "a song", "r1")
        self.assertIsNone(bot.song_requests.refusal("Bob", "a song"))


# ── what the player types ──────────────────────────────────────────────

def make_gameplay(role="guest", requests_open=True, bot=None):
    from libs.gameplay import Gameplay
    gp = Gameplay.__new__(Gameplay)
    network = Recorder()
    gp.game = SimpleNamespace(network=network, put=lambda fn: fn())
    ps = PartySyncState()
    ps.role = role
    ps.host_name = "Alice"
    ps.session_id = "s1"
    ps.song_requests = requests_open
    gp.party_sync = ps
    gp.music_bot = bot
    gp.substates = []
    gp.pop_last_substate = lambda: None
    gp.cancel = lambda *a, **k: None
    return gp, network


class TestTypedInput(unittest.TestCase):
    def test_a_guest_asks_the_host(self):
        gp, network = make_gameplay()
        with mock.patch("libs.gameplay.speak"):
            gp.party_sync_chat2("/m a song")
        self.assertEqual(len(network.sent), 1)
        channel, event, payload = network.sent[0]
        self.assertEqual(event, "party_sync_song_request")
        self.assertEqual(payload, {"query": "a song"})
        # Never also a chat line: a request is its own event.
        self.assertNotIn("party_sync_chat", [e for _, e, _ in network.sent])

    def test_a_closed_party_says_so_and_sends_nothing(self):
        gp, network = make_gameplay(requests_open=False)
        with mock.patch("libs.gameplay.speak") as said:
            gp.party_sync_chat2("/p a song")
        self.assertEqual(network.sent, [])
        self.assertIn("not taking song requests", said.call_args[0][0])

    def test_a_bare_command_explains_the_usage(self):
        gp, network = make_gameplay()
        with mock.patch("libs.gameplay.speak") as said:
            gp.party_sync_chat2("/m")
        self.assertEqual(network.sent, [])
        self.assertIn("/m", said.call_args[0][0])

    def test_ordinary_chat_still_goes_as_chat(self):
        gp, network = make_gameplay()
        with mock.patch("libs.gameplay.speak"):
            gp.party_sync_chat2("/help me")
        self.assertEqual([e for _, e, _ in network.sent], ["party_sync_chat"])



    def test_the_host_queues_their_own_request_locally(self):
        bot = make_bot()
        asked = []
        bot.queue_song_request = lambda who, query, rid=None: asked.append(
            (who, query, rid))
        gp, network = make_gameplay(role="host", bot=bot)
        with mock.patch("libs.gameplay.speak"), \
                mock.patch("libs.gameplay.options") as opts:
            opts.get.return_value = "Alice"
            gp.party_sync_chat2("/p a song")
        self.assertEqual(asked, [("Alice", "a song", None)])
        self.assertEqual(network.sent, [])

    def test_outside_a_session_a_request_is_refused(self):
        gp, network = make_gameplay(role=None)
        with mock.patch("libs.gameplay.speak") as said:
            gp.party_sync_chat2("/p a song")
        self.assertEqual(network.sent, [])
        self.assertIn("not in a Party Sync session", said.call_args[0][0])


# ── the host hears the request and the room hears the answer ───────────

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

    def test_the_typo_check_runs_before_the_line_is_sent(self):
        body = self._function(
            self._source("libs", "gameplay.py"), "party_sync_chat2")
        hint_at = body.index("near_song_command")
        self.assertLess(hint_at, body.index('"party_sync_chat"'))
        self.assertLess(hint_at, body.index("self.pop_last_substate()"))

    def test_the_request_packet_is_handled_by_the_host(self):
        body = self._function(
            self._source("libs", "event_handeler.py"), "party_sync_song_request")
        self.assertIn("parse_song_request", body)
        self.assertIn("queue_song_request", body)
        self.assertIn("self.game.put", body)      # the bot may start a stream

    def test_the_switch_is_pushed_once_per_session(self):
        body = self._function(
            self._source("libs", "event_handeler.py"), "party_sync_state")
        self.assertIn("announce_song_requests", body)
        self.assertIn("_party_song_flag_session", body)

    def test_a_session_end_forgets_who_asked_for_what(self):
        source = self._source("libs", "event_handeler.py")
        for name in ("party_sync_ended", "party_sync_kicked"):
            self.assertIn("_clear_party_song_requests",
                          self._function(source, name))

    def test_the_switch_left_the_music_bot_menu(self):
        # A line about what a session's listeners may ask for, parked among
        # the bot's own settings, read like a bot setting: it belongs with the
        # session controls instead.
        body = self._function(
            self._source("libs", "music_bot", "controller.py"),
            "_show_mode_menu")
        self.assertNotIn("Song Requests:", body)

    def test_the_host_sets_it_in_the_party_menus(self):
        # Both doors onto the session -- the Party Sync menu inside the Music
        # Bot menu and the Ctrl+F8 quick menu -- offer the host's switch.
        controller = self._source("libs", "music_bot", "controller.py")
        self.assertIn("song_requests_switch_item",
                      self._function(controller, "_open_party_sync_menu"))
        quick = self._function(
            self._source("libs", "gameplay.py"),
            "_open_party_sync_quick_menu")
        self.assertIn("song_requests_switch_item", quick)

    def test_one_builder_feeds_both_party_menus(self):
        body = self._function(
            self._source("libs", "music_bot", "controller.py"),
            "song_requests_switch_item")
        self.assertIn('role", None) != "host"', body)
        self.assertIn("Song Requests:", body)

    def test_a_guest_reads_whether_requests_are_open(self):
        body = self._function(
            self._source("libs", "music_bot", "controller.py"),
            "_open_party_sync_menu")
        self.assertIn("song_requests_label", body)

    def test_the_server_carries_both_directions(self):
        server = self._source("..", "server", "libs", "event_handeler.ts")
        for event in ("party_sync_song_request", "party_sync_song_result",
                      "party_sync_song_requests", "party_sync_song_choices",
                      "party_sync_song_pick"):
            self.assertIn(f"events['{event}']", server, event)
        validator = self._source("..", "server", "libs", "packet_validator.ts")
        for event in ("party_sync_song_request:", "party_sync_song_result:",
                      "party_sync_song_requests:", "party_sync_song_choices:",
                      "party_sync_song_pick:"):
            self.assertIn(event, validator, event)
        manager = self._source("..", "server", "libs", "party_sync.ts")
        self.assertIn("song_requests: session.songRequests", manager)
        self.assertIn("PARTY_SYNC_REQUEST_COOLDOWN_MS", manager)


if __name__ == "__main__":
    unittest.main()
