"""Unit tests for the Music Bot seek (fast-forward/rewind) feature and for
playing local video containers.

Covers:
- clamp_seek_position / format_track_position pure math.
- AudioStreamer honoring start_paused (a seek performed while paused must
  keep the new stream silent until the bot resumes).
- MapMusicBot.seek_by guards + local restart wiring.
- MapMusicBot remote (YouTube) seek re-resolves and restarts at the target.
- gameplay F9/F10 Ctrl dispatch (seek) vs plain volume change, staff-gated.

No game, OpenAL, ffmpeg, or network is required.
"""

import os
import sys
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pygame

from libs import music_bot
from libs.music_bot import (
    AudioStreamer,
    MapMusicBot,
    clamp_seek_position,
    format_track_position,
)
from libs.gameplay import Gameplay


class TestPositionHelpers(unittest.TestCase):
    def test_clamp_forward_jump(self):
        self.assertEqual(clamp_seek_position(90.0, 10.0), 100.0)

    def test_clamp_backward_jump(self):
        self.assertEqual(clamp_seek_position(100.0, -10.0), 90.0)

    def test_clamp_back_to_zero_is_legal(self):
        # From 3s, a 10s rewind restarts the intro at 0.
        self.assertEqual(clamp_seek_position(3.0, -10.0), 0.0)

    def test_clamp_at_start_noop_returns_none(self):
        self.assertIsNone(clamp_seek_position(0.0, -10.0))
        self.assertIsNone(clamp_seek_position(-5.0, -10.0))

    def test_clamp_never_negative(self):
        self.assertEqual(clamp_seek_position(2.0, -99.0), 0.0)

    def test_format_track_position(self):
        self.assertEqual(format_track_position(0), "0:00")
        self.assertEqual(format_track_position(5), "0:05")
        self.assertEqual(format_track_position(65), "1:05")
        self.assertEqual(format_track_position(3600 + 125), "62:05")
        self.assertEqual(format_track_position(-3), "0:00")


class TestAudioStreamerPausedStart(unittest.TestCase):
    def test_default_starts_playing(self):
        streamer = AudioStreamer(SimpleNamespace(), "x", None)
        self.assertFalse(streamer.paused)

    def test_start_paused_keeps_stream_silent(self):
        streamer = AudioStreamer(
            SimpleNamespace(), "x", None, start_paused=True)
        self.assertTrue(streamer.paused)


class FakeStreamer:
    def __init__(self, position=100.0, alive=True,
                 audio_url="", http_headers=None):
        self._position = position
        self._alive = alive
        self.stopped = False
        # Mirror AudioStreamer's seek-reuse surface: an empty audio_url keeps
        # every pre-existing test on the slow re-resolve path.
        self.audio_url = audio_url
        self.http_headers = dict(http_headers or {})

    def is_alive(self):
        return self._alive

    def content_position(self):
        return self._position

    def stop(self):
        self.stopped = True


def make_bot(**overrides):
    """Hermetic MapMusicBot built with __new__ (no game/OpenAL needed).

    The default track is a LOCAL existing file so restarts happen
    synchronously on the caller thread (no resolver thread). Remote
    (YouTube) behaviour is exercised by the dedicated test class.
    """
    bot = MapMusicBot.__new__(MapMusicBot)
    bot._playback_generation_lock = threading.Lock()
    bot._playback_generation = 0
    bot.enabled = True
    bot.playing = True
    bot.paused = False
    bot.is_loading_stream = False
    bot.current_title = "Test Track"
    bot.current_target = os.path.abspath(os.path.join(
        os.path.dirname(__file__), "..", "libs", "music_bot", "controller.py"))
    bot.current_source = "local"
    bot.searching = False
    bot.stream_source = None
    bot.streamer = FakeStreamer()
    bot.current_local_sound = None
    bot.mode = "youtube"
    bot._stream_announced = False
    bot._current_reverb_slot = None
    bot.feed_tracks = []
    bot.play_queue = []
    bot.stop_calls = []
    bot.starts = []
    bot.game = SimpleNamespace(put=lambda fn: None)

    def fake_stop(clear_queue=True, clear_feed=True, invalidate_pending=True, fade=False):
        bot.stop_calls.append(
            (clear_queue, clear_feed, invalidate_pending, fade))

    def fake_start(audio_url, title, playback_generation=None,
                   http_headers=None, canonical_url=None,
                   start_offset=0.0, start_paused=False):
        bot.starts.append({
            "url": audio_url,
            "title": title,
            "canonical_url": canonical_url,
            "start_offset": start_offset,
            "start_paused": start_paused,
        })
        bot.is_loading_stream = False

    bot.stop = fake_stop
    bot._start_youtube_stream = fake_start
    for key, value in overrides.items():
        setattr(bot, key, value)
    return bot


class TestMapMusicBotSeekGuards(unittest.TestCase):
    def _seek(self, bot, delta):
        with mock.patch("libs.music_bot.controller.speak") as speak:
            bot.seek_by(delta)
        return speak

    def test_backward_seek_restarts_at_target(self):
        bot = make_bot()
        speak = self._seek(bot, -10)
        self.assertEqual(bot.starts[0]["start_offset"], 90.0)
        self.assertFalse(bot.starts[0]["start_paused"])
        self.assertEqual(bot.stop_calls[0], (False, False, False, False))
        speak.assert_any_call("Seeking to 1:30.")

    def test_forward_seek(self):
        bot = make_bot()
        speak = self._seek(bot, 10)
        self.assertEqual(bot.starts[0]["start_offset"], 110.0)
        speak.assert_any_call("Seeking to 1:50.")

    def test_seek_while_paused_keeps_new_stream_paused(self):
        bot = make_bot(paused=True)
        self._seek(bot, 10)
        self.assertTrue(bot.starts[0]["start_paused"])

    def test_not_playing_does_nothing(self):
        bot = make_bot(playing=False)
        speak = self._seek(bot, 10)
        self.assertEqual(bot.starts, [])
        speak.assert_any_call("No active Music Bot track to seek.")

    def test_dead_streamer_does_nothing(self):
        bot = make_bot()
        bot.streamer = FakeStreamer(alive=False)
        speak = self._seek(bot, 10)
        self.assertEqual(bot.starts, [])
        speak.assert_any_call("No active Music Bot track to seek.")

    def test_at_start_backward_does_nothing(self):
        bot = make_bot()
        bot.streamer = FakeStreamer(position=0.0)
        speak = self._seek(bot, -10)
        self.assertEqual(bot.starts, [])
        speak.assert_any_call("Already at the start of the track.")

    def test_loading_stream_does_nothing(self):
        bot = make_bot(is_loading_stream=True)
        speak = self._seek(bot, 10)
        self.assertEqual(bot.starts, [])
        speak.assert_any_call("Please wait, the track is still loading.")

    def test_missing_position_does_nothing(self):
        bot = make_bot()
        bot.streamer = FakeStreamer()

        class BoomStreamer(FakeStreamer):
            def content_position(self):
                raise RuntimeError("no position")

        bot.streamer = BoomStreamer()
        speak = self._seek(bot, 10)
        self.assertEqual(bot.starts, [])
        speak.assert_any_call("No active Music Bot track to seek.")


class TestMapMusicBotLocalSeek(unittest.TestCase):
    def test_local_seek_starts_file_at_offset(self):
        fd, path = tempfile.mkstemp(suffix=".mp4")
        os.close(fd)
        try:
            bot = make_bot(
                current_title="Movie",
                current_target=path,
                current_source="local",
            )
            speak = mock.patch("libs.music_bot.controller.speak")
            with speak as sp:
                bot.seek_by(25)
            start = bot.starts[0]
            self.assertEqual(start["url"], path)
            self.assertEqual(start["title"], "Movie")
            self.assertIsNone(start["canonical_url"])
            self.assertEqual(start["start_offset"], 125.0)
            self.assertFalse(start["start_paused"])
            sp.assert_any_call("Seeking to 2:05.")
        finally:
            os.unlink(path)

    def test_local_seek_missing_file_does_nothing(self):
        bot = make_bot(
            current_target=os.path.join(tempfile.gettempdir(),
                                        "definitely_missing_xyz.mp4"),
            current_source="local",
        )
        with mock.patch("libs.music_bot.controller.speak") as speak:
            bot.seek_by(10)
        self.assertEqual(bot.starts, [])
        speak.assert_any_call("File not found.")


class TestMapMusicBotRemoteSeek(unittest.TestCase):
    def _remote_bot(self):
        bot = make_bot(
            current_title="Remote Track",
            current_target="https://www.youtube.com/watch?v=abc",
            current_source="youtube",
        )
        return bot

    def test_remote_seek_re_resolves_and_restarts(self):
        bot = self._remote_bot()
        results = []

        def fake_start(audio_url, title, playback_generation=None,
                       http_headers=None, canonical_url=None,
                       start_offset=0.0, start_paused=False):
            results.append({
                "url": audio_url,
                "title": title,
                "canonical_url": canonical_url,
                "start_offset": start_offset,
                "start_paused": start_paused,
                "http_headers": http_headers,
            })

        bot._start_youtube_stream = fake_start
        bot.game = SimpleNamespace(put=lambda fn: fn())  # run on caller thread

        info = {"url": "https://googlevideo.com/signed?x=1",
                "http_headers": {"User-Agent": "agent"}}
        with mock.patch("libs.music_bot.controller.speak"):
            with mock.patch.object(
                    music_bot.YouTubeSearcher, "get_stream_info",
                    return_value=info):
                bot.seek_by(-10)
        # The restart happens on the spawned resolver thread; wait for it.
        for _ in range(500):
            if results:
                break
            time.sleep(0.01)

        self.assertEqual(len(results), 1)
        result = results[0]
        self.assertEqual(result["url"], info["url"])
        self.assertEqual(result["canonical_url"],
                         "https://www.youtube.com/watch?v=abc")
        self.assertEqual(result["start_offset"], 90.0)
        self.assertFalse(result["start_paused"])
        self.assertEqual(result["http_headers"], {"User-Agent": "agent"})

    def test_remote_seek_resolution_failure_clears_loading(self):
        bot = self._remote_bot()
        with mock.patch("libs.music_bot.controller.speak"):
            with mock.patch.object(
                    music_bot.YouTubeSearcher, "get_stream_info",
                    return_value=None):
                bot.seek_by(10)
        # Worker thread runs asynchronously; wait for it to finish.
        for _ in range(500):
            if bot.is_loading_stream is False:
                break
            time.sleep(0.01)
        self.assertFalse(bot.is_loading_stream)

    def test_remote_seek_reuses_live_signed_url_without_resolution(self):
        # A seek while the streamer holds a direct signed URL + headers must
        # restart straight from them - no yt-dlp worker round trip - so the
        # seek is fast. Resolution never runs and no worker thread is spawned.
        bot = self._remote_bot()
        bot.streamer = FakeStreamer(
            audio_url="https://rr1.googlevideo.com/videoplayback?expire=999",
            http_headers={"Authorization": "Bearer live"},
        )
        results = []

        def fake_start(audio_url, title, playback_generation=None,
                       http_headers=None, canonical_url=None,
                       start_offset=0.0, start_paused=False):
            results.append({
                "url": audio_url,
                "title": title,
                "canonical_url": canonical_url,
                "start_offset": start_offset,
                "http_headers": http_headers,
            })

        bot._start_youtube_stream = fake_start
        bot.game = SimpleNamespace(put=lambda fn: fn())
        with mock.patch("libs.music_bot.controller.speak"):
            with mock.patch("libs.music_bot.controller.threading.Thread",
                            side_effect=AssertionError("worker must not spawn")):
                bot.seek_by(-10)
        # Fast path is synchronous - results are already there, no thread wait.
        self.assertEqual(len(results), 1)
        result = results[0]
        self.assertEqual(result["url"],
                         "https://rr1.googlevideo.com/videoplayback?expire=999")
        self.assertEqual(result["http_headers"], {"Authorization": "Bearer live"})
        self.assertEqual(result["canonical_url"],
                         "https://www.youtube.com/watch?v=abc")
        self.assertEqual(result["start_offset"], 90.0)


class FakeMusicBot:
    def __init__(self):
        self.volume = 50
        self.calls = []

    def set_volume(self, vol):
        self.calls.append(("set_volume", vol))

    def seek_by(self, delta):
        self.calls.append(("seek_by", delta))


def make_gp(**role_flags):
    gp = Gameplay.__new__(Gameplay)
    gp.music_bot = FakeMusicBot()
    for key, val in role_flags.items():
        setattr(gp, key, val)
    return gp


class TestMusicBotSeekKeys(unittest.TestCase):
    def test_ctrl_f9_rewinds_staff(self):
        gp = make_gp(is_staff=True)
        gp.music_bot_volume_key(-1, pygame.KMOD_CTRL)
        self.assertIn(("seek_by", -10), gp.music_bot.calls)

    def test_ctrl_f10_forwards_staff(self):
        gp = make_gp(is_staff=True)
        gp.music_bot_volume_key(1, pygame.KMOD_CTRL)
        self.assertIn(("seek_by", 10), gp.music_bot.calls)

    def test_ctrl_shift_jump_is_60_seconds(self):
        gp = make_gp(is_staff=True)
        gp.music_bot_volume_key(-1, pygame.KMOD_CTRL | pygame.KMOD_SHIFT)
        gp.music_bot_volume_key(1, pygame.KMOD_CTRL | pygame.KMOD_SHIFT)
        self.assertIn(("seek_by", -60), gp.music_bot.calls)
        self.assertIn(("seek_by", 60), gp.music_bot.calls)

    def test_plain_press_changes_volume(self):
        gp = make_gp(is_staff=True)
        with mock.patch("libs.gameplay.speak"):
            gp.music_bot_volume_key(1, 0)
        self.assertIn(("set_volume", 60), gp.music_bot.calls)
        self.assertNotIn(("seek_by", 10), gp.music_bot.calls)

    def test_non_staff_seek_keys_are_silent(self):
        gp = make_gp()  # no staff flags at all
        gp.music_bot_volume_key(-1, pygame.KMOD_CTRL)
        gp.music_bot_volume_key(1, pygame.KMOD_CTRL)
        self.assertEqual(gp.music_bot.calls, [])

    def test_no_music_bot_is_safe(self):
        gp = Gameplay.__new__(Gameplay)  # no music_bot attribute at all
        gp.music_bot_volume_key(-1, pygame.KMOD_CTRL)


if __name__ == "__main__":
    unittest.main()
