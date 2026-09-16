"""Party Sync: the host's play queue, shared with the session (read-only).

A listener hears the host's music but cannot see what is coming: the queue is
on the HOST's machine (YouTubeSearcher + next_up_queue are client-side and the
server keeps no music-bot queue at all). So the host relays a bounded snapshot
and every listener renders what arrives. This file pins both ends:

* what the host shares (libs/music_bot/song_requests.py `queue_share`) and the
  one wording both menus use (`queue_lines`),
* what a listener accepts off the wire (libs/party_sync.py `parse_queue`, the
  `PartySyncState` mirror, the state push that carries the same snapshot),
* the host's own relay rules (sent the moment it changes, repeated so a lost
  packet or a late joiner catches up, silent for anybody not hosting),
* and the wiring: the S2C handler, the loop, and the read-only line in the
  listener's two party menus.
"""

import os
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from libs.music_bot import controller as controller_mod
from libs.music_bot import song_requests
from libs.party_sync import (MAX_QUEUE_ITEMS, MAX_QUEUE_TITLE_LEN,
                             PartySyncState, parse_queue)


# ── what the host shares ────────────────────────────────────────────────

class TestQueueShare(unittest.TestCase):
    def test_the_order_and_the_requester_travel(self):
        queue = [
            {"title": "First", "requested_by": "Bob"},
            {"title": "Second"},
        ]
        self.assertEqual(song_requests.queue_share(queue), [
            {"title": "First", "by": "Bob"},
            {"title": "Second", "by": ""},
        ])

    def test_a_track_with_nothing_to_read_is_not_shared(self):
        queue = [
            {"title": "   "},
            {},
            None,
            "not a track",
            {"title": "real"},
        ]
        self.assertEqual(song_requests.queue_share(queue),
                         [{"title": "real", "by": ""}])

    def test_the_list_and_the_titles_are_bounded(self):
        queue = [{"title": f"song {i}"} for i in range(40)]
        shared = song_requests.queue_share(queue)
        self.assertEqual(len(shared), song_requests.SHARE_LIMIT)
        self.assertEqual(shared[0]["title"], "song 0")

        pasted = song_requests.queue_share(
            [{"title": "a\n\nb\tc" + "x" * 400}])
        self.assertEqual(pasted[0]["title"],
                         ("a b c" + "x" * 400)[:song_requests.SHARE_TITLE_MAX])
        self.assertNotIn("\n", pasted[0]["title"])

    def test_an_empty_queue_is_an_empty_list(self):
        for queue in (None, [], (), "nope"):
            self.assertEqual(song_requests.queue_share(queue), [])


class TestQueueLines(unittest.TestCase):
    def test_the_playing_song_comes_first_and_the_rest_are_numbered(self):
        lines = song_requests.queue_lines(
            [{"title": "A Song", "by": "Bob"}, {"title": "Host Song", "by": ""}],
            "Now Playing",
        )
        self.assertEqual(lines, [
            "Playing now: Now Playing",
            "1. A Song (requested by Bob)",
            "2. Host Song",
        ])

    def test_an_empty_queue_is_a_sentence(self):
        self.assertEqual(song_requests.queue_lines([]), ["The queue is empty."])
        self.assertEqual(song_requests.queue_lines([{"title": "  "}]),
                         ["The queue is empty."])
        self.assertEqual(song_requests.queue_lines(None, None),
                         ["The queue is empty."])

    def test_junk_in_the_list_does_not_shift_the_numbers(self):
        lines = song_requests.queue_lines(
            [{"title": "A"}, None, {"title": ""}, {"title": "B"}])
        self.assertEqual(lines, ["1. A", "2. B"])


# ── one queue, one set of lines ─────────────────────────────────────────
# The host's own Play Queue menu describes the same queue a listener reads,
# and both render it with the builders above. These tests are the gate: if a
# menu ever grows a wording of its own ("3. A Song - requested by Bob"), the
# host and the room start reading out two different stories about one list.

class FakeMenu:
    """Drop-in for libs.menu.Menu capturing the title and items it was built
    with (the real one needs a game, a renderer and a speaker)."""

    instances = []

    def __init__(self, game, title, parrent=None):
        self.game = game
        self.title = title
        self.items = []
        FakeMenu.instances.append(self)

    def add_items(self, items):
        self.items = list(items)


def open_menu(bot, opener):
    FakeMenu.instances = []
    with mock.patch("libs.menu.Menu", FakeMenu), \
            mock.patch("libs.menus.set_default_sounds"):
        opener()
    return FakeMenu.instances[-1]


def open_host_queue(bot):
    """The host's own Play Queue menu for this bot's next_up_queue."""
    gp = SimpleNamespace(substates=[], add_substate=lambda _menu: None)
    bot._find_gameplay = lambda: gp
    return open_menu(bot, bot._open_queue_menu)


def open_listener_queue(bot):
    """The listener's read-only view of the host's queue."""
    ps = bot._party_sync_pair()[1]
    gp = bot._party_sync_pair()[0]
    gp.substates = []
    gp.add_substate = lambda _menu: None
    return open_menu(bot, bot.open_party_queue_view)


def body_lines(menu, ends):
    return [label for label, _ in menu.items if label not in ends]


class TestTheBuilders(unittest.TestCase):
    def test_the_label_counts_only_what_can_be_counted(self):
        self.assertEqual(song_requests.queue_label(3), "Play Queue (3 waiting)")
        self.assertEqual(song_requests.queue_label(1), "Play Queue (1 waiting)")
        for empty in (0, None, "", "nope", -4):
            self.assertEqual(song_requests.queue_label(empty),
                             "Play Queue (empty)", repr(empty))

    def test_the_now_playing_line_is_a_whole_line_or_nothing(self):
        self.assertEqual(song_requests.now_playing_line(" A   Song "),
                         "Playing now: A Song")
        self.assertEqual(song_requests.now_playing_line("a\nb"),
                         "Playing now: a b")
        self.assertEqual(song_requests.now_playing_line(""), "")
        self.assertEqual(song_requests.now_playing_line(None), "")

    def test_a_track_line_says_whose_song_it_is_or_nothing_at_all(self):
        self.assertEqual(song_requests.track_line(1, "A Song"), "1. A Song")
        self.assertEqual(song_requests.track_line(2, "A Song", "Bob"),
                         "2. A Song (requested by Bob)")
        self.assertEqual(song_requests.track_line(3, "  ", " "), "3. Unknown")


class TestOneWording(unittest.TestCase):
    QUEUE = [
        {"title": "A Song", "requested_by": "Bob"},
        {"title": "Host Pick"},
    ]

    def test_the_host_and_the_listener_get_the_same_lines(self):
        bot = make_bot(host=True, playing=True, title="Now Playing",
                       queue=self.QUEUE)
        host = open_host_queue(bot)
        # The listener reads what the host relayed for this very queue.
        bot._party_sync_pair()[1].queue = song_requests.queue_share(
            bot.next_up_queue)
        bot._party_sync_pair()[1].now_playing = "Now Playing"
        listener = open_listener_queue(bot)
        # Same lines, in the same order (the callbacks differ by design: the
        # host's own items re-read themselves, a report's do nothing).
        self.assertEqual(
            body_lines(host, ("Clear Queue", "Back")),
            [label for label, _ in listener.items[:-1]],
            "the host's body is the listener's body")
        self.assertEqual(host.title, listener.title)
        self.assertEqual(host.title, "Play Queue (2 waiting)")
        self.assertEqual(body_lines(host, ("Clear Queue", "Back")), [
            "Playing now: Now Playing",
            "1. A Song (requested by Bob)",
            "2. Host Pick",
        ])
        self.assertIn(("Close", listener.items[-1][1]), listener.items)

    def test_an_empty_queue_is_the_same_sentence_on_both_sides(self):
        bot = make_bot(host=True, playing=False, title="", queue=[])
        host = open_host_queue(bot)
        listener = open_listener_queue(bot)
        self.assertEqual(host.title, listener.title)
        self.assertEqual(host.title, "Play Queue (empty)")
        self.assertEqual(body_lines(host, ("Clear Queue", "Back")),
                         [song_requests.empty_line()])
        self.assertEqual(listener.items[0][0], song_requests.empty_line())

    def test_a_song_on_air_keeps_its_line_when_nothing_is_waiting(self):
        bot = make_bot(host=True, playing=True, title="Now Playing", queue=[])
        self.assertEqual(
            body_lines(open_host_queue(bot), ("Clear Queue", "Back")),
            ["Playing now: Now Playing"])

    def test_the_queue_menu_speaks_its_own_line_back(self):
        bot = make_bot(host=True, playing=True, title="Now Playing",
                       queue=self.QUEUE)
        menu = open_host_queue(bot)
        with mock.patch("libs.music_bot.controller.speak") as speak:
            for label, callback in menu.items:
                if label.startswith("1."):
                    callback()
        speak.assert_called_once_with("1. A Song (requested by Bob)")


class TestNoSecondWording(unittest.TestCase):
    """The wording may only be composed in `song_requests`."""

    #: The one module allowed to spell a queue line out.
    OWNER = os.path.join("libs", "music_bot", "song_requests.py")
    #: Every client module that speaks for the music bot's queue.
    READERS = (
        os.path.join("libs", "music_bot", "controller.py"),
        os.path.join("libs", "party_sync.py"),
        os.path.join("libs", "gameplay.py"),
        os.path.join("libs", "event_handeler.py"),
    )
    #: Fragments of the builders above: a menu that contains one of these is
    #: composing the wording itself instead of asking for it.
    PHRASES = ("Play Queue (", "(requested by", "Playing now:",
               "The queue is empty")

    def _source(self, *parts):
        path = os.path.join(os.path.dirname(__file__), "..", *parts)
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read()

    def _function(self, source, name):
        marker = f"def {name}("
        start = source.index(marker)
        rest = source[start:]
        return rest[:rest.index("\n    def ", 1)]

    def test_the_wording_is_composed_in_song_requests(self):
        owner = self._source(self.OWNER)
        for phrase in self.PHRASES:
            self.assertIn(phrase, owner, phrase)

    def test_no_queue_reader_spells_a_line_out(self):
        for path in self.READERS:
            source = self._source(path)
            for phrase in self.PHRASES:
                self.assertNotIn(phrase, source,
                                 f"{path} composes '{phrase}' itself")

    def test_both_menus_ask_the_shared_builders(self):
        controller = self._source("libs", "music_bot", "controller.py")
        host = self._function(controller, "_open_queue_menu")
        listener = self._function(controller, "open_party_queue_view")
        mode = self._function(controller, "_show_mode_menu")
        for body in (host, listener):
            self.assertIn("song_requests.queue_lines", body)
        self.assertIn("song_requests.queue_label", host)
        # ...and the listener's title comes from the one label reader.
        self.assertIn("party_queue_label", listener)
        self.assertIn("song_requests.queue_label", self._function(
            controller, "party_queue_label"))
        # The Music Bot menu's "Play Queue (N waiting)" line is the same one.
        self.assertIn("song_requests.queue_label", mode)
        # ...and one reader for "what is playing", not one per menu.
        self.assertIn("_now_playing_title", host)
        self.assertIn("_now_playing_title", self._function(
            controller, "announce_party_queue"))


# ── what a listener accepts off the wire ────────────────────────────────

class TestParseQueue(unittest.TestCase):
    def test_a_valid_payload_is_normalized(self):
        parsed = parse_queue({
            "now_playing": " A   Song ",
            "items": [{"title": " Next ", "by": " Bob "}, {"title": "x"}],
        })
        self.assertEqual(parsed, {
            "now_playing": "A Song",
            "items": [{"title": "Next", "by": "Bob"}, {"title": "x", "by": ""}],
        })

    def test_a_malformed_payload_reads_as_an_empty_queue(self):
        for payload in (None, [], "x", 5, {"items": "nope"},
                        {"items": [None, 3, {}, {"title": "  "}]}):
            self.assertEqual(parse_queue(payload),
                             {"now_playing": "", "items": []}, repr(payload))

    def test_everything_is_bounded_and_one_line(self):
        parsed = parse_queue({
            "now_playing": "a\nb" + "y" * 400,
            "items": [{"title": f"t{i}\nz", "by": "n" * 60} for i in range(40)],
        })
        self.assertEqual(len(parsed["now_playing"]), MAX_QUEUE_TITLE_LEN)
        self.assertNotIn("\n", parsed["now_playing"])
        self.assertEqual(len(parsed["items"]), MAX_QUEUE_ITEMS)
        self.assertNotIn("\n", parsed["items"][0]["title"])
        self.assertLessEqual(len(parsed["items"][0]["by"]), 32)


class TestStateMirror(unittest.TestCase):
    def _state(self, **extra):
        payload = {
            "session_id": "alice:1",
            "host": {"name": "Alice", "voice_channel": 20},
            "guests": [{"name": "Bob", "voice_channel": 21}],
        }
        payload.update(extra)
        return payload

    def test_the_queue_arrives_with_the_state(self):
        ps = PartySyncState()
        ps.apply_state(self._state(
            now_playing="Now",
            queue=[{"title": "A", "by": "Bob"}],
        ), "Bob")
        self.assertEqual(ps.now_playing, "Now")
        self.assertEqual(ps.queue, [{"title": "A", "by": "Bob"}])

    def test_a_payload_without_a_queue_reads_empty(self):
        ps = PartySyncState()
        ps.apply_state(self._state(), "Bob")
        self.assertEqual(ps.now_playing, "")
        self.assertEqual(ps.queue, [])
        # Garbage is not somebody else's queue.
        ps.apply_state(self._state(now_playing=5, queue="nope"), "Bob")
        self.assertEqual(ps.queue, [])

    def test_ending_the_session_forgets_the_queue(self):
        ps = PartySyncState()
        ps.apply_state(self._state(now_playing="Now",
                                   queue=[{"title": "A"}]), "Bob")
        ps.end_session()
        self.assertEqual(ps.now_playing, "")
        self.assertEqual(ps.queue, [])


# ── the host's relay ────────────────────────────────────────────────────

class Recorder:
    def __init__(self):
        self.sent = []

    def send(self, channel, event, data=None):
        self.sent.append((channel, event, data))


def make_bot(host=True, playing=True, title="Now", queue=()):
    bot = controller_mod.MapMusicBot.__new__(controller_mod.MapMusicBot)
    bot.game = SimpleNamespace(put=lambda fn: fn(), network=Recorder())
    bot.playing = playing
    bot.paused = False
    bot.current_title = title
    bot.next_up_queue = [dict(track) for track in queue]
    ps = PartySyncState()
    ps.role = "host" if host else "guest"
    ps.session_id = "s1"
    ps.host_name = "Alice"
    gp = SimpleNamespace(game=bot.game, party_sync=ps)
    bot._find_gameplay = lambda: gp
    bot._party_sync_pair = lambda: (gp, ps)
    return bot


def queue_events(bot):
    return [s for s in bot.game.network.sent if s[1] == "party_sync_queue"]


class TestHostRelay(unittest.TestCase):
    def _patch_clock(self):
        self.now = 1000.0
        return mock.patch.object(
            controller_mod, "time",
            SimpleNamespace(monotonic=lambda: self.now))

    def test_the_queue_goes_out_the_moment_it_changes(self):
        bot = make_bot(playing=False, title="", queue=[{"title": "A"}])
        with self._patch_clock():
            self.assertTrue(bot.announce_party_queue())
            # Nothing changed and the repeat interval has not passed: a frame
            # of the loop must not cost a packet.
            self.assertFalse(bot.announce_party_queue())
            sent = queue_events(bot)
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][2], {
            "now_playing": "",
            "items": [{"title": "A", "by": ""}],
        })

    def test_a_new_request_appears_at_once(self):
        bot = make_bot(playing=False, title="", queue=[{"title": "A"}])
        with self._patch_clock():
            bot.announce_party_queue()
            bot.next_up_queue.append({"title": "B", "requested_by": "Bob"})
            bot.announce_party_queue()
            sent = queue_events(bot)
        self.assertEqual(len(sent), 2)
        self.assertEqual(sent[-1][2]["items"], [
            {"title": "A", "by": ""},
            {"title": "B", "by": "Bob"},
        ])

    def test_a_lost_packet_is_covered_by_the_repeat(self):
        bot = make_bot(playing=False, title="", queue=[{"title": "A"}])
        with self._patch_clock():
            bot.announce_party_queue()
            self.now += bot.PARTY_QUEUE_INTERVAL - 0.1
            bot.announce_party_queue()
            self.assertEqual(len(queue_events(bot)), 1)
            self.now += 0.2
            self.assertTrue(bot.announce_party_queue())
        self.assertEqual(len(queue_events(bot)), 2)

    def test_what_is_playing_travels_only_while_it_plays(self):
        bot = make_bot(playing=True, title="A Song", queue=[])
        with self._patch_clock():
            bot.announce_party_queue()
            self.assertEqual(queue_events(bot)[-1][2]["now_playing"], "A Song")
        bot = make_bot(playing=False, title="A Song", queue=[{"title": "x"}])
        with self._patch_clock():
            bot.announce_party_queue()
            self.assertEqual(queue_events(bot)[-1][2]["now_playing"], "")

    def test_nothing_to_say_is_said_once_and_then_not_at_all(self):
        bot = make_bot(playing=False, title="", queue=[])
        with self._patch_clock():
            # An empty, stopped session is still news once (the room may have
            # been reading a queue that has just played out)...
            self.assertTrue(bot.announce_party_queue())
            # ...and then silence, even after the repeat interval.
            self.now += bot.PARTY_QUEUE_INTERVAL * 3
            self.assertFalse(bot.announce_party_queue())
        self.assertEqual(len(queue_events(bot)), 1)

    def test_an_emptied_queue_is_sent(self):
        bot = make_bot(playing=False, title="", queue=[{"title": "A"}])
        with self._patch_clock():
            bot.announce_party_queue()
            bot.next_up_queue = []
            self.assertTrue(bot.announce_party_queue())
            sent = queue_events(bot)
        self.assertEqual(sent[-1][2]["items"], [])

    def test_a_listener_never_relays_anything(self):
        bot = make_bot(host=False, playing=True, title="Now",
                       queue=[{"title": "A"}])
        with self._patch_clock():
            self.assertFalse(bot.announce_party_queue())
            self.assertFalse(bot.announce_party_queue(force=True))
        self.assertEqual(queue_events(bot), [])

    def test_the_snapshot_is_bounded_on_the_way_out(self):
        bot = make_bot(playing=False, title="",
                       queue=[{"title": f"s{i}"} for i in range(40)])
        with self._patch_clock():
            bot.announce_party_queue()
        self.assertEqual(len(queue_events(bot)[0][2]["items"]),
                         song_requests.SHARE_LIMIT)

    def test_the_label_counts_only_what_is_waiting(self):
        bot = make_bot()
        ps = bot._party_sync_pair()[1]
        ps.queue = [{"title": "A"}, {"title": "B"}]
        self.assertEqual(bot.party_queue_label(), "Play Queue (2 waiting)")
        ps.queue = []
        self.assertEqual(bot.party_queue_label(), "Play Queue (empty)")

    def test_a_client_with_no_session_is_not_given_one(self):
        # The relay is asked every frame: a music bot that has never used
        # Party Sync must not have a session created for it by asking.
        bot = make_bot()
        gp = bot._find_gameplay()
        gp.party_sync = None
        self.assertFalse(bot.announce_party_queue())
        self.assertIsNone(gp.party_sync)


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

    def test_the_host_relays_from_the_frame_loop(self):
        body = self._function(
            self._source("libs", "music_bot", "controller.py"), "loop")
        self.assertIn("announce_party_queue", body)

    def test_a_roster_change_re_sends_the_queue(self):
        body = self._function(
            self._source("libs", "event_handeler.py"), "party_sync_state")
        # Somebody just joined and their state only carries what the host last
        # shared, so the host re-sends now rather than on the next change.
        self.assertIn('getattr(bot, "announce_party_queue", None)', body)
        self.assertIn("force=True", body)

    def test_the_listener_stores_what_arrives(self):
        body = self._function(
            self._source("libs", "event_handeler.py"), "party_sync_queue")
        self.assertIn("parse_queue", body)
        self.assertIn("ps.queue", body)

    def test_a_listener_reads_the_queue_without_touching_it(self):
        controller = self._source("libs", "music_bot", "controller.py")
        view = self._function(controller, "open_party_queue_view")
        self.assertIn("song_requests.queue_lines", view)
        self.assertIn('("Close"', view)
        # Read-only: every line is a report, so nothing is added that acts.
        self.assertNotIn("_party_sync_send", view)
        self.assertNotIn("next_up_queue", view)

    def test_both_of_a_listeners_party_menus_offer_it(self):
        controller = self._source("libs", "music_bot", "controller.py")
        party = self._function(controller, "_open_party_sync_menu")
        self.assertIn("party_queue_label", party)
        self.assertIn("open_party_queue_view", party)
        quick = self._function(
            self._source("libs", "gameplay.py"), "_open_party_sync_quick_menu")
        self.assertIn("party_queue_label", quick)

    def test_the_server_carries_the_relay_and_the_snapshot(self):
        handler = self._source("..", "server", "libs", "event_handeler.ts")
        self.assertIn("events['party_sync_queue']", handler)
        validator = self._source("..", "server", "libs", "packet_validator.ts")
        self.assertIn("party_sync_queue:", validator)
        manager = self._source("..", "server", "libs", "party_sync.ts")
        self.assertIn("shareQueue", manager)
        self.assertIn("queue: session.queueItems", manager)
        self.assertIn("now_playing: session.nowPlaying", manager)


if __name__ == "__main__":
    unittest.main()
