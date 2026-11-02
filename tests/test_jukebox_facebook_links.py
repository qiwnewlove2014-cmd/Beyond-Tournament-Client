"""The jukebox URL line accepts a Facebook page, and a live has no length.

A pasted link is read once for its title and its canonical page, then queued;
a Facebook live carries no duration, so the cabinet must keep playing instead
of advancing after a guessed five minutes. No game, OpenAL, ffmpeg, yt-dlp or
network is required: the reader is a stand-in and the worker runs on start.
"""

import os
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from libs import consts
from libs.jukebox import menu


class RunNowThread:
    def __init__(self, target=None, daemon=None, **kwargs):
        self._target = target

    def start(self):
        if self._target is not None:
            self._target()


class FakeYoutubeDL:
    info = {}
    asked = []

    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def extract_info(self, url, download=False):
        FakeYoutubeDL.asked.append(url)
        assert download is False
        return dict(FakeYoutubeDL.info)


class JukeboxFacebookLinkTests(unittest.TestCase):
    def submit(self, url, info):
        FakeYoutubeDL.info = info
        FakeYoutubeDL.asked = []
        sent = []
        game = SimpleNamespace(
            put=lambda callback: callback(),
            network=SimpleNamespace(send=lambda *args: sent.append(args)),
        )
        gp = SimpleNamespace(pop_last_substate=lambda: None)
        with mock.patch.dict(sys.modules, {
                "yt_dlp": SimpleNamespace(YoutubeDL=FakeYoutubeDL),
        }), mock.patch("libs.jukebox.menu.threading.Thread", RunNowThread), \
                mock.patch("libs.jukebox.menu.speak"):
            menu._on_url_submit(game, gp, "jb1", url)
        return sent

    def test_a_facebook_reel_is_queued_with_its_own_page(self):
        sent = self.submit(
            "facebook.com/reel/123",
            {"id": "123", "title": "Reel", "duration": 30,
             "webpage_url": "https://www.facebook.com/reel/123"},
        )
        self.assertEqual(sent, [(
            consts.CHANNEL_MISC, "jukebox_queue_add",
            {"id": "jb1", "title": "Reel",
             "url": "https://www.facebook.com/reel/123", "duration": 30},
        )])

    def test_a_facebook_live_is_queued_with_no_length(self):
        sent = self.submit(
            "https://www.facebook.com/page/live",
            {"id": "live", "title": "Live", "is_live": True,
             "webpage_url": "https://www.facebook.com/page/live"},
        )
        payload = sent[0][2]
        self.assertEqual(payload["duration"], 0)
        self.assertEqual(payload["url"], "https://www.facebook.com/page/live")

    def test_a_facebook_link_without_a_page_url_uses_the_pasted_page(self):
        sent = self.submit(
            "https://fb.watch/abc/",
            {"id": "123", "title": "Video", "duration": 12},
        )
        self.assertEqual(sent[0][2]["url"], "https://fb.watch/abc/")

    def test_another_site_is_refused_before_it_is_read(self):
        sent = []
        FakeYoutubeDL.asked = []
        game = SimpleNamespace(
            put=lambda callback: callback(),
            network=SimpleNamespace(send=lambda *args: sent.append(args)),
        )
        gp = SimpleNamespace(pop_last_substate=lambda: None)
        with mock.patch.dict(sys.modules, {
                "yt_dlp": SimpleNamespace(YoutubeDL=FakeYoutubeDL),
        }), mock.patch("libs.jukebox.menu.threading.Thread", RunNowThread), \
                mock.patch("libs.jukebox.menu.speak") as say:
            menu._on_url_submit(game, gp, "jb1", "https://example.com/song")

        self.assertEqual(sent, [])
        self.assertEqual(FakeYoutubeDL.asked, [])
        say.assert_called_once_with(
            "Only YouTube and Facebook links are supported on the jukebox.")


if __name__ == "__main__":
    unittest.main()
