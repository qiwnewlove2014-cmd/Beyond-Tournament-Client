"""Music Bot Mode's "Play a YouTube link" line.

Staff already hold the link they want (a chat message, a browser tab) and had
to search for the song to reach it. The line sits directly under "Search
YouTube": no search, no result list -- the link is read, then played, or queued
when Queue Mode is on, exactly as a picked search result would be. The two
paths are pinned together here so they cannot drift: the queue branch must call
the same `_enqueue_track` a search result does, and the play branch must hand
the canonical page to `_start_youtube_stream_from_search`.

No game, OpenAL, ffmpeg, yt-dlp or network is required.
"""

import os
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from libs.music_bot import MapMusicBot, YouTubeSearcher


class FakeMenu:
    """Drop-in for libs.menu.Menu capturing the items it was built with."""

    def __init__(self, *args, **kwargs):
        self.items = []

    def add_items(self, items):
        self.items = list(items)

    def speak_current_item(self):
        pass


class RunNowThread:
    """The read worker, run on start so the test needs no timing."""

    def __init__(self, target=None, daemon=None, **kwargs):
        self._target = target

    def start(self):
        if self._target is not None:
            self._target()


def make_bot(link_resolver=None):
    bot = MapMusicBot.__new__(MapMusicBot)
    bot._gp = None
    bot.game = SimpleNamespace(put=lambda fn: fn())
    bot.searching = False
    bot.queue_mode = False
    bot.next_up_queue = []
    bot.playing = False
    bot.is_loading_stream = False
    bot._find_gameplay = lambda: None
    bot._test_reader = link_resolver or (lambda url: None)
    bot.resolved = []
    bot.enqueued = []

    def start_stream(title, webpage_url, direct_url, http_headers=None,
                     preserve_queue=False):
        bot.resolved.append((title, webpage_url, direct_url))

    def enqueue(title, target, source="youtube", http_headers=None,
                webpage_url="", direct_url="", requested_by=""):
        bot.enqueued.append((title, target, source, webpage_url))
        return 0

    bot._start_youtube_stream_from_search = start_stream
    bot._enqueue_track = enqueue
    return bot


class LinkLineTests(unittest.TestCase):
    def menu(self):
        bot = make_bot()
        game = SimpleNamespace()
        gameplay = SimpleNamespace(
            add_substate=lambda item: None,
            pop_last_substate=lambda: None,
            can_broadcast_megaphone=False,
            can_use_cinema_speakers=False,
        )
        bot.game = game
        bot._gp = gameplay
        bot._find_gameplay = lambda: gameplay
        bot.queue_mode = False
        bot.next_up_queue = []
        bot.subtitle_label = lambda: "Subtitles: OFF"
        bot.cinema_routing_allowed = lambda: False
        bot.cinema_listening_allowed = lambda: False
        captured = []
        gameplay.add_substate = captured.append
        with mock.patch("libs.menu.Menu", FakeMenu), \
                mock.patch("libs.menus.set_default_sounds"):
            bot._show_mode_menu()
        labels = [label() if callable(label) else label
                  for label, _action in captured[-1].items]
        return labels

    def test_the_link_line_sits_directly_under_search(self):
        labels = self.menu()
        index = labels.index("Search YouTube")
        self.assertEqual(labels[index + 1], "Play a YouTube link", labels)

    def test_the_line_opens_the_link_input(self):
        bot = make_bot()
        opened = []
        bot._open_link_input = lambda: opened.append(True)
        # The action on the line is what a press runs.
        bot._gp = SimpleNamespace(pop_last_substate=lambda: None)
        captured = []

        class Gameplay(SimpleNamespace):
            pass

        gameplay = Gameplay(add_substate=captured.append,
                            pop_last_substate=lambda: None,
                            can_broadcast_megaphone=False,
                            can_use_cinema_speakers=False)
        bot.game = SimpleNamespace()
        bot._find_gameplay = lambda: gameplay
        bot.subtitle_label = lambda: "Subtitles: OFF"
        bot.cinema_routing_allowed = lambda: False
        bot.cinema_listening_allowed = lambda: False
        with mock.patch("libs.menu.Menu", FakeMenu), \
                mock.patch("libs.menus.set_default_sounds"):
            bot._show_mode_menu()
        labels = [label() if callable(label) else label
                  for label, _action in captured[-1].items]
        action = dict(zip(labels, [a for _l, a in captured[-1].items]))[
            "Play a YouTube link"]
        action()
        self.assertEqual(opened, [True])


class LinkSubmitTests(unittest.TestCase):
    def submit(self, bot, raw):
        """Run the whole submit path with the reader and the worker under control."""
        with mock.patch("libs.music_bot.controller.YouTubeSearcher.read_link",
                        side_effect=bot._test_reader), \
                mock.patch("libs.music_bot.controller.threading.Thread",
                           RunNowThread):
            bot._on_link_submit(raw)

    def test_a_bare_link_is_given_a_scheme_and_read(self):
        seen = []

        def read(url):
            seen.append(url)
            return {"title": "Song", "duration": 180,
                    "webpage_url": "https://www.youtube.com/watch?v=abc"}

        bot = make_bot(read)
        self.submit(bot, "  youtu.be/abc  ")

        self.assertEqual(seen, ["https://youtu.be/abc"])
        self.assertEqual(bot.resolved,
                         [("Song", "https://www.youtube.com/watch?v=abc", "")])

    def test_an_http_link_is_upgraded_to_https(self):
        seen = []
        bot = make_bot(lambda url: seen.append(url) or None)
        self.submit(bot, "http://youtu.be/abc")
        self.assertEqual(seen, ["https://youtu.be/abc"])

    def test_anything_that_is_not_youtube_is_refused_before_it_is_read(self):
        seen = []
        bot = make_bot(lambda url: seen.append(url) or None)
        with mock.patch("libs.music_bot.controller.speak") as say:
            self.submit(bot, "https://example.com/song")

        self.assertEqual(seen, [])
        self.assertEqual(bot.resolved, [])
        say.assert_called_once_with(
            "Only YouTube links can be played on the Music Bot.")

    def test_an_empty_input_is_cancelled(self):
        bot = make_bot(lambda url: self.fail("must not resolve an empty link"))
        with mock.patch("libs.music_bot.controller.speak") as say:
            self.submit(bot, "   ")
        say.assert_called_once_with("Link cancelled.")

    def test_queue_mode_queues_the_link_instead_of_playing_it(self):
        bot = make_bot(lambda url: {"title": "Song", "duration": 180,
                                    "webpage_url": "https://youtu.be/abc"})
        bot.queue_mode = True
        self.submit(bot, "https://youtu.be/abc")

        self.assertEqual(bot.resolved, [])
        self.assertEqual(bot.enqueued,
                         [("Song", "https://youtu.be/abc", "youtube",
                           "https://youtu.be/abc")])

    def test_a_link_that_cannot_be_read_says_so_and_plays_nothing(self):
        bot = make_bot(lambda url: None)
        with mock.patch("libs.music_bot.controller.speak") as say:
            self.submit(bot, "https://youtu.be/dead")

        self.assertEqual(bot.resolved, [])
        say.assert_any_call("Reading the link...")
        say.assert_called_with("Could not read that YouTube link. Please check it.")


class ReadLinkTests(unittest.TestCase):
    """Reading the page, not the stream: title, length, canonical URL."""

    def read(self, info, url="https://youtu.be/abc"):
        class YoutubeDL:
            def __init__(self, *args, **kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def extract_info(self, target, download=False):
                self.target = target
                assert download is False
                return info

        module = SimpleNamespace(YoutubeDL=YoutubeDL)
        with mock.patch.dict(sys.modules, {"yt_dlp": module}):
            return YouTubeSearcher.read_link(url)

    def test_a_short_link_gets_its_canonical_watch_page(self):
        result = self.read({"id": "abc", "title": "A Song", "duration": 233.7})
        self.assertEqual(result, {"title": "A Song", "duration": 233,
                                  "webpage_url": "https://www.youtube.com/watch?v=abc"})

    def test_a_playlist_link_reads_its_first_video(self):
        result = self.read({"entries": [
            {"id": "first", "title": "First", "duration": 60},
            None,
            {"id": "second", "title": "Second", "duration": 90},
        ]}, url="https://www.youtube.com/playlist?list=PL123")
        self.assertEqual(result["title"], "First")
        self.assertEqual(result["webpage_url"],
                         "https://www.youtube.com/watch?v=first")

    def test_a_page_that_cannot_be_read_is_none(self):
        self.assertIsNone(self.read(None))


if __name__ == "__main__":
    unittest.main()
