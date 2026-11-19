"""Unit tests for the Music Bot play-next queue (Queue Mode) and the Music Bot
settings menu toggles.

Covers:
- Queue Mode flag defaults + persistence keys.
- _enqueue_track: plays immediately when idle, appends while playing/loading,
  rejects empty targets.
- _advance_track_queue: queued songs play BEFORE the favorites/playlist queue,
  which resumes afterwards.
- _play_queued_next preserves the favorites/playlist queue.
- skip_track: next drains the play-next queue before walking the
  playlist/local batch; previous walks that batch.
- Local-file selection: several files become one local batch; one file keeps
  the single-file path.
- stop(clear_queue=True) clears the play-next queue; stop(clear_queue=False)
  preserves it.
- _open_queue_menu: one item per queued song, and every line (count, now
  playing, tracks, empty sentence) composed by libs.music_bot.song_requests so
  the host reads the same strings a Party Sync listener does.
- Settings: _reapply_bot_water_filter attach/detach, _sync_map_reverb gated by
  reverb_enabled, settings menu builds with all three toggles, options.set is
  called on toggle.
- camera._apply_music_water_filter skips the bot source when the player
  disabled the underwater muffle but still muffles jukebox sources.

No game, OpenAL, ffmpeg, or network is required.
"""

import ast
import os
import sys
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from libs import music_bot
from libs.camera import _apply_music_water_filter
from libs.gameplay import Gameplay
from libs.music_bot import MapMusicBot
from libs.music_bot import repeat
from libs.music_bot import subtitles
from libs.music_bot.track_source import TrackSourceHandler


def make_bot(**overrides):
    """Hermetic MapMusicBot built with __new__ (no game/OpenAL needed)."""
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
    bot.streamer = None
    bot.current_local_sound = None
    bot.mode = "youtube"
    bot._stream_announced = False
    bot._current_reverb_slot = None
    bot.subtitles_enabled = False
    bot.subtitle_language = subtitles.DEFAULT_LANGUAGE_PREFERENCE
    bot.subtitle_offset = 0
    bot.subtitle_reader = subtitles.SubtitleReader()
    bot._caption_fetcher = subtitles.CaptionFetcher()
    bot._subtitle_generation = 0
    bot._subtitle_page = ""
    bot.feed_tracks = []
    bot.play_queue = []
    bot.play_queue_index = -1
    bot.play_queue_label = ""
    bot.next_up_queue = []
    bot.queue_mode = False
    bot.repeat_mode = repeat.REPEAT_OFF
    bot._repeat_cycle = []
    bot.water_muffle_enabled = True
    bot.reverb_enabled = True
    # Where this bot's own stream is heard: empty is the game's own device.
    bot.output_device = ""
    bot._output_process = None
    bot.broadcast_enabled = False
    bot.broadcast_to_megaphone = False
    bot.eq_profile = "normal"
    bot.eq_values = {"bass": 50, "mid": 50, "treble": 50}
    bot._eq_slots = {}
    bot._custom_eq_slot = None
    bot.crossfade_enabled = False
    bot.current_duration = None
    bot._known_durations = {}
    bot._crossfade = None
    bot.game = SimpleNamespace(put=lambda fn: None)
    bot._stop_local = lambda: None
    bot._destroy_stream_source = lambda: None
    bot._clear_personal_feed = lambda: None
    for key, value, *_extra in overrides.items():
        setattr(bot, key, value)
    return bot


class FakeMenu:
    """Drop-in for libs.menu.Menu capturing the items it was built with."""

    instances = []

    def __init__(self, game, title, parrent=None):
        self.game = game
        self.title = title
        self.parrent = parrent
        self.items = []
        self.speak_calls = 0
        FakeMenu.instances.append(self)

    def add_items(self, items):
        self.items = list(items)

    def speak_current_item(self):
        self.speak_calls += 1


def patch_menu():
    FakeMenu.instances = []
    return (
        mock.patch("libs.menu.Menu", FakeMenu),
        mock.patch("libs.menus.set_default_sounds"),
    )


def last_menu():
    return FakeMenu.instances[-1]


def norm(label):
    """Menu labels may be dynamic callables; resolve them for assertions."""
    return label() if callable(label) else label


class TestQueueModeDefaults(unittest.TestCase):
    def test_default_flags(self):
        bot = make_bot()
        self.assertFalse(bot.queue_mode)
        self.assertTrue(bot.water_muffle_enabled)
        self.assertTrue(bot.reverb_enabled)
        self.assertEqual(bot.next_up_queue, [])


class TestEnqueueTrack(unittest.TestCase):
    def test_enqueue_while_playing_appends_and_speaks(self):
        bot = make_bot()
        with mock.patch("libs.music_bot.controller.speak") as speak:
            waiting = bot._enqueue_track(
                "Song A", "https://www.youtube.com/watch?v=abc",
                webpage_url="https://www.youtube.com/watch?v=abc",
            )
        self.assertEqual(waiting, 1)
        self.assertEqual(len(bot.next_up_queue), 1)
        speak.assert_called_once_with("Added Song A to the queue. 1 waiting.")

    def test_enqueue_while_loading_appends(self):
        bot = make_bot(playing=True, is_loading_stream=True)
        with mock.patch("libs.music_bot.controller.speak") as speak:
            waiting = bot._enqueue_track("Song A", "https://target")
        self.assertEqual(waiting, 1)
        self.assertEqual(len(bot.next_up_queue), 1)
        speak.assert_called_once()

    def test_enqueue_when_idle_plays_immediately(self):
        bot = make_bot(playing=False, is_loading_stream=False)
        calls = []

        def fake_start(title, webpage_url, direct_url,
                       http_headers=None, preserve_queue=False):
            calls.append((title, webpage_url, direct_url, http_headers,
                          preserve_queue))

        bot._start_youtube_stream_from_search = fake_start
        with mock.patch("libs.music_bot.controller.speak") as speak:
            waiting = bot._enqueue_track(
                "Song A", "https://www.youtube.com/watch?v=abc",
                http_headers={"k": "v"},
                webpage_url="https://www.youtube.com/watch?v=abc",
                direct_url="https://direct",
            )
        self.assertEqual(waiting, 0)
        self.assertEqual(bot.next_up_queue, [])
        self.assertEqual(calls, [(
            "Song A", "https://www.youtube.com/watch?v=abc",
            "https://direct", {"k": "v"}, True,
        )])
        speak.assert_not_called()

    def test_enqueue_empty_target_rejected(self):
        bot = make_bot()
        with mock.patch("libs.music_bot.controller.speak") as speak:
            waiting = bot._enqueue_track("No Target", "")
        self.assertEqual(waiting, 0)
        self.assertEqual(bot.next_up_queue, [])
        speak.assert_called_once_with("Cannot queue this track.")

    def test_queue_older_items_play_before_newer_after_idle_restart(self):
        # Leftover queue (e.g. a load failure) must be drained oldest-first
        # when playback restarts idle.
        bot = make_bot(playing=False, is_loading_stream=False)
        bot.next_up_queue = [{"title": "Old A", "target": "t1"},
                             {"title": "Old B", "target": "t2"}]
        starts = []

        def fake_start(title, webpage_url, direct_url,
                       http_headers=None, preserve_queue=False):
            starts.append(title)

        bot._start_youtube_stream_from_search = fake_start
        with mock.patch("libs.music_bot.controller.speak"):
            bot._enqueue_track("New C", "https://target")
        self.assertEqual(starts, ["Old A"])
        self.assertEqual([t["title"] for t in bot.next_up_queue],
                         ["Old B", "New C"])


class TestAdvanceTrackQueue(unittest.TestCase):
    def track(self, title, target="https://target"):
        return {"title": title, "target": target, "source": "youtube"}

    def test_next_up_plays_before_playlist(self):
        bot = make_bot()
        bot.play_queue = [self.track("P1"), self.track("P2")]
        bot.play_queue_index = 0
        bot.play_queue_label = "Favorites"
        bot.next_up_queue = [self.track("Q1")]
        started = []

        def fake_start(title, webpage_url, direct_url,
                       http_headers=None, preserve_queue=False):
            started.append((title, preserve_queue))

        bot._start_youtube_stream_from_search = fake_start
        with mock.patch("libs.music_bot.controller.speak"):
            self.assertTrue(bot._advance_track_queue())
        self.assertEqual(started, [("Q1", True)])
        # Playlist untouched — it resumes after the queued song ends.
        self.assertEqual(bot.next_up_queue, [])
        self.assertEqual(bot.play_queue_index, 0)

    def test_playlist_resumes_after_next_up_drained(self):
        bot = make_bot()
        bot.play_queue = [self.track("P1"), self.track("P2")]
        bot.play_queue_index = 0
        bot.play_queue_label = "Favorites"
        bot.next_up_queue = [self.track("Q1")]
        started = []
        bot._start_youtube_stream_from_search = (
            lambda title, webpage_url, direct_url,
                   http_headers=None, preserve_queue=False:
            started.append(title))

        def fake_play_queued():
            started.append(bot.play_queue[bot.play_queue_index]["title"])

        bot._play_queued_track = fake_play_queued
        with mock.patch("libs.music_bot.controller.speak"):
            # Queued song plays first...
            self.assertTrue(bot._advance_track_queue())
            # ...then the playlist continues.
            self.assertTrue(bot._advance_track_queue())
        self.assertEqual(started, ["Q1", "P2"])
        self.assertEqual(bot.next_up_queue, [])
        self.assertEqual(bot.play_queue_index, 1)

    def test_falls_back_to_playlist_when_next_up_empty(self):
        bot = make_bot()
        bot.play_queue = [self.track("P1"), self.track("P2")]
        bot.play_queue_index = 0
        bot.play_queue_label = "Favorites"
        started = []
        bot._play_queued_track = (
            lambda: started.append(bot.play_queue[bot.play_queue_index]["title"]))
        with mock.patch("libs.music_bot.controller.speak"):
            self.assertTrue(bot._advance_track_queue())
        self.assertEqual(started, ["P2"])
        self.assertEqual(bot.play_queue_index, 1)

    def test_both_empty_returns_false(self):
        bot = make_bot()
        with mock.patch("libs.music_bot.controller.speak"):
            self.assertFalse(bot._advance_track_queue())

    def test_play_queued_next_preserves_playlist(self):
        bot = make_bot()
        bot.play_queue = [self.track("P1"), self.track("P2")]
        bot.play_queue_index = 0
        bot.play_queue_label = "Favorites"
        bot.next_up_queue = [self.track("Q1")]
        started = []
        bot._start_youtube_stream_from_search = (
            lambda title, webpage_url, direct_url,
                   http_headers=None, preserve_queue=False:
            started.append((title, preserve_queue)))
        with mock.patch("libs.music_bot.controller.speak"):
            self.assertTrue(bot._play_queued_next())
        self.assertEqual(started, [("Q1", True)])
        self.assertEqual(bot.next_up_queue, [])
        self.assertEqual(len(bot.play_queue), 2)
        self.assertEqual(bot.play_queue_index, 0)


class TestSkipTrack(unittest.TestCase):
    """Ctrl+[ / ] and Queue Mode's lines share one move.

    Next drains the play-next queue first (the order an end-of-song advance
    uses), then walks the playlist/local batch forward. Previous only walks
    that batch: a play-next entry is removed when it starts.
    """

    def track(self, title):
        return {"title": title, "target": title, "source": "youtube"}

    def bot_with_queue(self, count=3, index=0):
        bot = make_bot()
        bot.play_queue = [self.track(f"P{i}") for i in range(count)]
        bot.play_queue_index = index
        bot.play_queue_label = "Favorites"
        return bot

    def test_next_drains_the_play_next_queue_first(self):
        bot = self.bot_with_queue()
        bot.next_up_queue = [self.track("Q1")]
        played = []
        bot._play_queued_next = lambda: played.append("Q1")
        with mock.patch("libs.music_bot.controller.speak") as speak:
            self.assertTrue(bot.skip_track(1))
        self.assertEqual(played, ["Q1"])
        self.assertEqual(bot.play_queue_index, 0)
        speak.assert_not_called()

    def test_next_cancels_a_track_that_is_still_loading(self):
        bot = self.bot_with_queue()
        bot.is_loading_stream = True
        bot.next_up_queue = [self.track("Q1")]
        seen = []
        bot._play_queued_next = lambda: seen.append(bot.is_loading_stream)
        with mock.patch("libs.music_bot.controller.speak"):
            self.assertTrue(bot.skip_track(1))
        self.assertEqual(seen, [False])

    def test_next_walks_forward_in_the_local_batch(self):
        bot = self.bot_with_queue()
        played = []
        bot._play_queued_track = lambda: played.append(bot.play_queue_index)
        with mock.patch("libs.music_bot.controller.speak") as speak:
            self.assertTrue(bot.skip_track(1))
        self.assertEqual(played, [1])
        self.assertEqual(bot.play_queue_index, 1)
        speak.assert_not_called()

    def test_next_at_the_end_keeps_the_last_song_playing(self):
        bot = self.bot_with_queue(index=2)
        with mock.patch("libs.music_bot.controller.speak") as speak:
            self.assertFalse(bot.skip_track(1))
        self.assertEqual(bot.play_queue_index, 2)
        self.assertEqual(len(bot.play_queue), 3)
        speak.assert_called_once_with("Nothing to skip to.")

    def test_previous_walks_back_in_the_local_batch(self):
        bot = self.bot_with_queue(index=2)
        played = []
        bot._play_queued_track = lambda: played.append(bot.play_queue_index)
        with mock.patch("libs.music_bot.controller.speak") as speak:
            self.assertTrue(bot.skip_track(-1))
        self.assertEqual(played, [1])
        self.assertEqual(bot.play_queue_index, 1)
        speak.assert_not_called()

    def test_previous_at_the_start_says_there_is_none(self):
        bot = self.bot_with_queue(index=0)
        with mock.patch("libs.music_bot.controller.speak") as speak:
            self.assertFalse(bot.skip_track(-1))
        self.assertEqual(bot.play_queue_index, 0)
        speak.assert_called_once_with("No previous song in this queue.")


class TestRepeatMode(unittest.TestCase):
    """One line, three modes, and every way a song can advance."""

    def track(self, title):
        return {"title": title, "target": title, "source": "youtube"}

    def bot_with_playlist(self, count=3, index=0):
        bot = make_bot()
        bot.play_queue = [self.track(f"P{i}") for i in range(count)]
        bot.play_queue_index = index
        bot.play_queue_label = "Favorites"
        return bot

    def test_the_line_cycles_off_all_one_and_persists(self):
        bot = make_bot()
        self.assertEqual(bot.repeat_label(), "Repeat: Off")
        with mock.patch("libs.music_bot.controller.options.set") as opt_set, \
                mock.patch("libs.music_bot.controller.speak") as speak, \
                mock.patch.object(bot, "_cancel_crossfade"):
            self.assertEqual(bot.toggle_repeat_mode(), repeat.REPEAT_ALL)
            self.assertEqual(bot.repeat_label(), "Repeat: All songs")
            self.assertEqual(bot.toggle_repeat_mode(), repeat.REPEAT_ONE)
            self.assertEqual(bot.repeat_label(), "Repeat: This song")
            self.assertEqual(bot.toggle_repeat_mode(), repeat.REPEAT_OFF)
        self.assertEqual([call.args for call in opt_set.call_args_list], [
            ("music_bot_repeat_mode", repeat.REPEAT_ALL),
            ("music_bot_repeat_mode", repeat.REPEAT_ONE),
            ("music_bot_repeat_mode", repeat.REPEAT_OFF),
        ])
        self.assertIn("Repeat off", speak.call_args[0][0])

    def test_toggling_drops_a_pre_roll_but_not_a_fade_already_underway(self):
        bot = make_bot()
        with mock.patch.object(bot, "_cancel_crossfade") as cancel, \
                mock.patch("libs.music_bot.controller.options.set"), \
                mock.patch("libs.music_bot.controller.speak"):
            bot._crossfade = {"phase": "resolving"}
            bot.toggle_repeat_mode()
            cancel.assert_called_once()

            bot._crossfade = {"phase": "fading"}
            cancel.reset_mock()
            bot.toggle_repeat_mode()
            cancel.assert_not_called()

    def test_an_unknown_stored_mode_reads_off(self):
        bot = make_bot(repeat_mode="sometimes")
        self.assertEqual(bot.repeat_label(), "Repeat: Off")

    def test_repeat_all_wraps_the_playlist_at_the_end(self):
        bot = self.bot_with_playlist(index=2)
        bot.repeat_mode = repeat.REPEAT_ALL
        played = []
        bot._play_queued_track = lambda: played.append(bot.play_queue_index)
        with mock.patch("libs.music_bot.controller.speak") as speak:
            self.assertTrue(bot._advance_track_queue())
        self.assertEqual(played, [0])
        speak.assert_not_called()

    def test_repeat_off_still_finishes_the_playlist(self):
        bot = self.bot_with_playlist(index=2)
        with mock.patch("libs.music_bot.controller.speak") as speak:
            self.assertFalse(bot._advance_track_queue())
        speak.assert_called_once_with("Favorites finished.")
        self.assertEqual(bot.play_queue, [])

    def test_repeat_one_replays_the_song_at_the_end(self):
        bot = make_bot(repeat_mode=repeat.REPEAT_ONE)
        bot._seek_restart = mock.MagicMock()
        with mock.patch("libs.music_bot.controller.speak") as speak:
            self.assertTrue(bot._advance_track_queue())
        bot._seek_restart.assert_called_once_with(0.0, announce=False)
        speak.assert_not_called()

    def test_a_failed_load_is_not_repeated_forever(self):
        bot = make_bot(repeat_mode=repeat.REPEAT_ONE)
        bot._seek_restart = mock.MagicMock()
        self.assertFalse(bot._advance_track_queue(natural=False))
        bot._seek_restart.assert_not_called()

    def test_repeat_all_loops_a_play_next_queue_with_no_playlist(self):
        bot = make_bot(repeat_mode=repeat.REPEAT_ALL)
        bot._repeat_cycle = [self.track("Q0"), self.track("Q1")]
        started = []
        bot._start_youtube_stream_from_search = (
            lambda title, webpage_url, direct_url,
                   http_headers=None, preserve_queue=False:
            started.append(title))
        with mock.patch("libs.music_bot.controller.speak"):
            self.assertTrue(bot._advance_track_queue())
        self.assertEqual(started, ["Q0"])
        self.assertEqual([t["title"] for t in bot.next_up_queue], ["Q1"])
        self.assertEqual([t["title"] for t in bot._repeat_cycle], ["Q0"])

    def test_next_wraps_the_playlist_in_repeat_all(self):
        bot = self.bot_with_playlist(index=2)
        bot.repeat_mode = repeat.REPEAT_ALL
        played = []
        bot._play_queued_track = lambda: played.append(bot.play_queue_index)
        with mock.patch("libs.music_bot.controller.speak"):
            self.assertTrue(bot.skip_track(1))
        self.assertEqual(played, [0])

    def test_previous_wraps_to_the_last_in_repeat_all(self):
        bot = self.bot_with_playlist(index=0)
        bot.repeat_mode = repeat.REPEAT_ALL
        played = []
        bot._play_queued_track = lambda: played.append(bot.play_queue_index)
        with mock.patch("libs.music_bot.controller.speak"):
            self.assertTrue(bot.skip_track(-1))
        self.assertEqual(played, [2])

    def test_next_and_previous_restart_the_song_in_repeat_one(self):
        bot = make_bot(repeat_mode=repeat.REPEAT_ONE)
        bot._seek_restart = mock.MagicMock()
        with mock.patch("libs.music_bot.controller.speak") as speak:
            self.assertTrue(bot.skip_track(1))
            self.assertTrue(bot.skip_track(-1))
        self.assertEqual(bot._seek_restart.call_count, 2)
        speak.assert_any_call("Repeating Test Track.")

    def test_the_feed_wraps_in_repeat_all_and_stops_off(self):
        bot = make_bot(repeat_mode=repeat.REPEAT_ALL)
        bot.feed_tracks = [self.track("F1"), self.track("F2")]
        bot.feed_index = 1
        played = []
        bot._play_personal_feed_track = lambda: played.append(bot.feed_index)
        self.assertTrue(bot._next_personal_feed())
        self.assertEqual(played, [0])
        # The wrapped-to song is first, so put the cursor on the last one to
        # test what Off does at the end of the feed.
        bot.repeat_mode = repeat.REPEAT_OFF
        bot.feed_index = 1
        cleared = []
        bot._clear_personal_feed = lambda: cleared.append(True)
        with mock.patch("libs.music_bot.controller.speak") as speak:
            self.assertFalse(bot._next_personal_feed())
        speak.assert_called_once_with("Personal Music Feed finished.")
        self.assertEqual(cleared, [True])

    def test_the_crossfade_pre_rolls_the_wrapped_track(self):
        bot = self.bot_with_playlist(index=2)
        bot.repeat_mode = repeat.REPEAT_ALL
        machine = bot._crossfade_machine()
        self.assertEqual(machine._peek_next_track()["title"], "P0")
        self.assertEqual(machine._consume_next_track()["title"], "P0")
        self.assertEqual(bot.play_queue_index, 0)

    def test_the_crossfade_pre_rolls_the_song_itself_in_repeat_one(self):
        bot = make_bot(repeat_mode=repeat.REPEAT_ONE)
        machine = bot._crossfade_machine()
        self.assertEqual(machine._peek_next_track()["title"], "Test Track")
        self.assertEqual(machine._consume_next_track()["title"], "Test Track")

    def test_a_queued_song_plays_before_the_next_feed_song(self):
        bot = make_bot()
        bot.feed_tracks = [self.track("F1"), self.track("F2")]
        bot.feed_index = 0
        bot.next_up_queue = [self.track("Q1")]
        played = []
        bot._play_queued_next = lambda: played.append("Q1")
        bot._next_personal_feed = lambda natural=True: played.append("F2")
        self.assertTrue(bot._advance_track_queue())
        self.assertEqual(played, ["Q1"])

    def test_a_queued_song_takes_the_feed_over(self):
        bot = make_bot()
        bot.feed_tracks = [self.track("F1"), self.track("F2")]
        bot.feed_index = 0
        bot.next_up_queue = [self.track("Q1")]
        bot._clear_personal_feed = mock.MagicMock()
        bot._start_youtube_stream_from_search = lambda *args, **kwargs: None
        self.assertTrue(bot._play_queued_next())
        bot._clear_personal_feed.assert_called_once()
        self.assertEqual(bot.next_up_queue, [])

    def test_a_queued_song_keeps_the_repeat_all_cycle(self):
        bot = make_bot(repeat_mode=repeat.REPEAT_ALL)
        bot._clear_personal_feed = lambda: MapMusicBot._clear_personal_feed(bot)
        bot.next_up_queue = [self.track("Q1")]

        def start(*args, **kwargs):
            # What `_start_youtube_stream_from_search` does on every queued
            # track; it must not erase the queue-only cycle.
            bot._clear_personal_feed()

        bot._start_youtube_stream_from_search = start
        self.assertTrue(bot._play_queued_next())
        self.assertEqual([t["title"] for t in bot._repeat_cycle], ["Q1"])

    def test_off_clears_a_finished_queue_only_cycle(self):
        bot = make_bot()
        bot._repeat_cycle = [self.track("Q0")]
        self.assertFalse(bot._advance_track_queue())
        self.assertEqual(bot._repeat_cycle, [])

    def test_the_crossfade_follows_the_feed_and_wraps_it(self):
        bot = make_bot(repeat_mode=repeat.REPEAT_ALL)
        bot.feed_tracks = [self.track("F1"), self.track("F2")]
        bot.feed_index = 1
        machine = bot._crossfade_machine()
        self.assertEqual(machine._peek_next_track()["title"], "F1")
        self.assertEqual(machine._consume_next_track()["title"], "F1")
        self.assertEqual(bot.feed_index, 0)

    def test_the_crossfade_repeats_the_feed_song_in_repeat_one(self):
        bot = make_bot(repeat_mode=repeat.REPEAT_ONE)
        bot.feed_tracks = [self.track("F1"), self.track("F2")]
        bot.feed_index = 1
        machine = bot._crossfade_machine()
        self.assertEqual(machine._peek_next_track()["title"], "Test Track")
        self.assertEqual(machine._consume_next_track()["title"], "Test Track")
        self.assertEqual(bot.feed_index, 1)

    def test_the_crossfade_has_no_next_at_the_feed_end_in_off(self):
        bot = make_bot()
        bot.feed_tracks = [self.track("F1"), self.track("F2")]
        bot.feed_index = 1
        machine = bot._crossfade_machine()
        self.assertIsNone(machine._peek_next_track())
        self.assertIsNone(machine._consume_next_track())
        self.assertEqual(bot.feed_index, 1)

    def test_the_crossfade_hands_the_feed_to_a_queued_song(self):
        bot = make_bot()
        bot.feed_tracks = [self.track("F1"), self.track("F2")]
        bot.feed_index = 0
        bot.next_up_queue = [self.track("Q1")]
        bot._clear_personal_feed = mock.MagicMock()
        machine = bot._crossfade_machine()
        self.assertEqual(machine._peek_next_track()["title"], "Q1")
        self.assertEqual(machine._consume_next_track()["title"], "Q1")
        bot._clear_personal_feed.assert_called_once()
        self.assertEqual(bot.next_up_queue, [])

    def test_the_repeat_line_sits_under_the_open_queue_line(self):
        import inspect
        from libs.music_bot.menus import MusicBotMenus
        source = inspect.getsource(MusicBotMenus._show_mode_menu)
        self.assertIn("self._bot.repeat_label", source)
        self.assertIn("self._bot.toggle_repeat_mode", source)
        self.assertLess(source.index("get_queue_count_label"),
                        source.index("self._bot.repeat_label"))


class TestStopClearsNextUp(unittest.TestCase):
    def test_stop_clears_next_up_queue(self):
        bot = make_bot()
        bot.next_up_queue = [{"title": "Q1", "target": "t"}]
        bot.stop(clear_queue=True, clear_feed=False)
        self.assertEqual(bot.next_up_queue, [])

    def test_stop_preserves_next_up_queue_when_clear_queue_false(self):
        bot = make_bot()
        bot.next_up_queue = [{"title": "Q1", "target": "t"}]
        bot.stop(clear_queue=False, clear_feed=False)
        self.assertEqual(len(bot.next_up_queue), 1)


class TestResultSelectQueueMode(unittest.TestCase):
    def _bot_with_results(self):
        bot = make_bot()
        bot.search_results = [{
            "title": "Song A",
            "webpage_url": "https://www.youtube.com/watch?v=abc",
            "url": "https://direct",
            "http_headers": {"k": "v"},
        }]
        return bot

    def test_queue_mode_enter_queues_without_options_menu(self):
        bot = self._bot_with_results()
        bot.queue_mode = True
        gp = SimpleNamespace(pop_last_substate=lambda: None)
        enqueued = []

        def fake_enqueue(title, target, source="youtube", http_headers=None,
                         webpage_url="", direct_url=""):
            enqueued.append((title, target, source, http_headers,
                             webpage_url, direct_url))

        bot._enqueue_track = fake_enqueue

        def boom(*args, **kwargs):
            raise AssertionError("options menu must not open in Queue Mode")

        with mock.patch("libs.menu.Menu", boom):
            bot._on_result_selected(0, gp)
        self.assertEqual(enqueued, [(
            "Song A", "https://www.youtube.com/watch?v=abc", "youtube",
            {"k": "v"}, "https://www.youtube.com/watch?v=abc", "https://direct",
        )])

    def test_options_menu_offers_play_next(self):
        bot = self._bot_with_results()
        gp = SimpleNamespace(pop_last_substate=lambda: None,
                             add_substate=lambda m: None)
        enqueued = []
        bot._enqueue_track = (
            lambda title, target, source="youtube", http_headers=None,
                   webpage_url="", direct_url="":
            enqueued.append(title))
        with patch_menu()[0], patch_menu()[1]:
            bot._on_result_selected(0, gp)
        items = last_menu().items
        labels = [label for label, _, *_extra in items]
        self.assertIn("Play Now", labels)
        self.assertIn("Play Next (Add to Queue)", labels)
        for label, cb, *_extra in items:
            if label == "Play Next (Add to Queue)":
                cb()
        self.assertEqual(enqueued, ["Song A"])


class TestQueueMenu(unittest.TestCase):
    def _open(self, bot):
        gp = mock.MagicMock(spec=Gameplay)
        gp.substates = []
        bot.game = SimpleNamespace(stack=[gp])
        with patch_menu()[0], patch_menu()[1]:
            bot._open_queue_menu()
        return gp

    def test_each_queued_song_is_own_menu_item(self):
        bot = make_bot()
        bot.next_up_queue = [
            {"title": "Song A", "target": "t1"},
            {"title": "Song B", "target": "t2"},
        ]
        self._open(bot)
        labels = [norm(label) for label, _, *_extra in last_menu().items]
        # The count is the menu title; the body is the same list a Party Sync
        # listener reads (song_requests.queue_lines).
        self.assertEqual(last_menu().title, "Play Queue (2 waiting)")
        self.assertEqual(labels, [
            "Playing now: Test Track",
            "1. Song A",
            "2. Song B",
            "Clear Queue",
            "Back",
        ])

    def test_empty_queue_has_no_song_items(self):
        bot = make_bot(playing=False, current_title="")
        self._open(bot)
        labels = [norm(label) for label, _, *_extra in last_menu().items]
        self.assertEqual(last_menu().title, "Play Queue (empty)")
        self.assertEqual(labels, [
            "The queue is empty.",
            "Clear Queue", "Back",
        ])

    def test_song_item_enter_restates_title(self):
        bot = make_bot()
        bot.next_up_queue = [{"title": "Song A", "target": "t1"}]
        self._open(bot)
        with mock.patch("libs.music_bot.menus.speak") as speak:
            for label, cb, *_extra in last_menu().items:
                if norm(label) == "1. Song A":
                    cb()
        speak.assert_called_once_with("1. Song A")

    def test_queue_mode_adds_skip_lines(self):
        bot = make_bot()
        bot.queue_mode = True
        bot.next_up_queue = [{"title": "Song A", "target": "t1"}]
        self._open(bot)
        labels = [norm(label) for label, _, *_extra in last_menu().items]
        self.assertEqual(labels[:2], ["Next song", "Previous song"])

    def test_clear_queue_empties_and_rebuilds_menu(self):
        bot = make_bot()
        bot.next_up_queue = [{"title": "Song A", "target": "t1"}]
        gp = mock.MagicMock(spec=Gameplay)
        gp.substates = []
        bot.game = SimpleNamespace(stack=[gp])
        # Keep the menu patches active: Clear Queue rebuilds the menu, which
        # constructs another Menu instance.
        with patch_menu()[0], patch_menu()[1]:
            bot._open_queue_menu()
            with mock.patch("libs.music_bot.menus.speak") as speak:
                for label, cb, *_extra in last_menu().items:
                    if norm(label) == "Clear Queue":
                        cb()
        self.assertEqual(bot.next_up_queue, [])
        speak.assert_called_once_with("Queue cleared.")
        # The menu was rebuilt without the removed song item.
        self.assertEqual(len(FakeMenu.instances), 2)
        labels = [norm(label) for label, _, *_extra in last_menu().items]
        self.assertEqual(last_menu().title, "Play Queue (empty)")
        # The song on air keeps its own line: with nothing waiting the empty
        # sentence would be news only to a bot that is not playing anything.
        self.assertEqual(labels, [
            "Playing now: Test Track",
            "Clear Queue", "Back",
        ])


class _ImmediateThread:
    """A Thread whose start() runs the dialog body now (no game loop here)."""

    def __init__(self, target, daemon=False):
        self._target = target

    def start(self):
        self._target()


class TestLocalFileDialog(unittest.TestCase):
    """The file chooser: one path is a song, several are a local batch."""

    def _run(self, paths):
        bot = make_bot()
        scheduled = []
        bot.game.put = lambda callback: callback()
        bot._start_local_file_stream = (
            lambda path, title: scheduled.append(("file", path, title)))
        bot._start_track_queue = (
            lambda tracks, label: scheduled.append(("queue", tracks, label)))

        asked = []
        # The picker is the dialog helper's child process now (see
        # libs/folder_dialog): this process never builds Tk for it, so the test
        # stands in for the helper rather than for tkinter.
        answer = ("\n".join(paths), "")

        def ask_files(*args, **kwargs):
            asked.append((args, kwargs))
            return answer

        with mock.patch("libs.folder_dialog.ask_files", ask_files), \
                mock.patch("libs.music_bot.track_source.threading.Thread",
                           _ImmediateThread), \
                mock.patch("libs.music_bot.track_source.speak"):
            TrackSourceHandler(bot)._open_file_dialog()
        return scheduled, asked

    def test_several_files_become_one_local_batch(self):
        paths = [os.path.join("songs", "A.mp3"),
                 os.path.join("songs", "B.flac")]
        scheduled, asked = self._run(paths)
        self.assertEqual(scheduled, [("queue", [
            {"title": "A", "target": paths[0], "source": "local"},
            {"title": "B", "target": paths[1], "source": "local"},
        ], "Local files")])
        args, kwargs = asked[0]
        self.assertEqual(args[0], "Choose Local File")
        self.assertIn("*.mp3", kwargs["pattern"])

    def test_one_file_keeps_the_single_file_path(self):
        paths = [os.path.join("songs", "A.mp3")]
        scheduled, _asked = self._run(paths)
        self.assertEqual(scheduled, [("file", paths[0], "A")])

    def test_cancel_selects_nothing(self):
        scheduled, _asked = self._run([])
        self.assertEqual(scheduled, [])


class TestSettings(unittest.TestCase):
    def test_reapply_water_filter_off_detaches(self):
        flt = object()
        bot = make_bot(water_muffle_enabled=False)
        bot.stream_source = SimpleNamespace(direct_filter=flt)
        bot.game = SimpleNamespace(audio_mngr=SimpleNamespace(
            filter=[None, flt]))
        bot._reapply_bot_water_filter()
        self.assertFalse(hasattr(bot.stream_source, "direct_filter"))

    def test_reapply_water_filter_on_reattaches(self):
        flt = object()
        bot = make_bot(water_muffle_enabled=True)
        bot.stream_source = SimpleNamespace()
        bot.game = SimpleNamespace(audio_mngr=SimpleNamespace(
            filter=[None, flt]))
        bot._reapply_bot_water_filter()
        self.assertIs(bot.stream_source.direct_filter, flt)

    def test_reapply_water_filter_no_source_noop(self):
        bot = make_bot(stream_source=None)
        bot._reapply_bot_water_filter()  # must not raise

    def test_sync_map_reverb_disabled_detaches_slot(self):
        slot = object()
        bot = make_bot(reverb_enabled=False, _current_reverb_slot=slot)
        bot.stream_source = object()
        sends = []
        bot.game = SimpleNamespace(audio_mngr=SimpleNamespace(
            efx=SimpleNamespace(send=lambda *a: sends.append(a))))
        bot._sync_map_reverb()
        self.assertEqual(len(sends), 1)
        self.assertEqual(sends[0][2], None)
        self.assertIsNone(bot._current_reverb_slot)

    def test_sync_map_reverb_disabled_no_slot_noop(self):
        bot = make_bot(reverb_enabled=False, _current_reverb_slot=None)
        bot.stream_source = object()
        sends = []
        bot.game = SimpleNamespace(audio_mngr=SimpleNamespace(
            efx=SimpleNamespace(send=lambda *a: sends.append(a))))
        bot._sync_map_reverb()
        self.assertEqual(sends, [])

    def test_sync_map_reverb_enabled_still_runs(self):
        bot = make_bot(reverb_enabled=True)
        bot.stream_source = object()
        bot.game = SimpleNamespace(put=lambda fn: None, stack=[])
        # No gameplay found -> map reverb path returns True without touching EFX.
        self.assertTrue(bot._sync_map_reverb())

    def test_settings_menu_builds_with_all_toggles(self):
        gp = mock.MagicMock(spec=Gameplay)
        bot = make_bot()
        bot.game = SimpleNamespace(stack=[gp])
        with patch_menu()[0], patch_menu()[1]:
            bot._open_settings_menu()
        items = last_menu().items
        labels = [norm(label) for label, _, *_extra in items]
        # Broadcast to Others is intentionally absent — it has its own
        # keyboard shortcut and must not be duplicated in the settings menu.
        self.assertNotIn("Broadcast to Others: OFF", labels)
        self.assertIn("Underwater Muffle: ON", labels)
        self.assertIn("Room Reverb (Realism): ON", labels)
        # The bot's own stream can be sent to another device from here.
        self.assertIn("Music Output Device: Same as the game", labels)

    def test_water_toggle_persists_and_applies(self):
        gp = mock.MagicMock(spec=Gameplay)
        bot = make_bot()
        bot.stream_source = SimpleNamespace()
        bot.game = SimpleNamespace(stack=[gp], audio_mngr=SimpleNamespace(
            filter=[None, object()]))
        with patch_menu()[0], patch_menu()[1]:
            bot._open_settings_menu()
        with mock.patch("libs.music_bot.menus.options.set") as opt_set, \
                mock.patch("libs.music_bot.menus.speak") as speak:
            for label, cb, *_extra in last_menu().items:
                if norm(label).startswith("Underwater Muffle"):
                    cb()
        self.assertFalse(bot.water_muffle_enabled)
        opt_set.assert_called_once_with("music_bot_water_muffle", False)
        speak.assert_called_once_with("Underwater muffle disabled.")
        self.assertEqual(last_menu().speak_calls, 1)
        # Toggling off while underwater detaches the live source filter.
        self.assertFalse(hasattr(bot.stream_source, "direct_filter"))

    def test_reverb_toggle_persists_and_detaches(self):
        gp = mock.MagicMock(spec=Gameplay)
        bot = make_bot(reverb_enabled=True)
        bot._current_reverb_slot = object()
        bot.stream_source = object()
        bot.game = SimpleNamespace(stack=[gp], audio_mngr=SimpleNamespace(
            efx=SimpleNamespace(send=lambda *a: None)))
        with patch_menu()[0], patch_menu()[1]:
            bot._open_settings_menu()
        with mock.patch("libs.music_bot.menus.options.set") as opt_set, \
                mock.patch("libs.music_bot.menus.speak") as speak:
            for label, cb, *_extra in last_menu().items:
                if norm(label).startswith("Room Reverb"):
                    cb()
        self.assertFalse(bot.reverb_enabled)
        opt_set.assert_called_once_with("music_bot_reverb", False)
        speak.assert_called_once_with("Room reverb disabled.")
        self.assertIsNone(bot._current_reverb_slot)


class TestCameraWaterFilterGate(unittest.TestCase):
    def test_bot_source_skipped_when_muffle_disabled(self):
        flt = object()
        bot_src = SimpleNamespace(direct_filter=flt)
        game = SimpleNamespace(gameplay=SimpleNamespace(
            music_bot=SimpleNamespace(water_muffle_enabled=False,
                                      stream_source=bot_src),
            jukebox_player=None))
        _apply_music_water_filter(game, flt)
        self.assertFalse(hasattr(bot_src, "direct_filter"))

    def test_jukebox_still_muffled_when_bot_disabled(self):
        flt = object()
        bot_src = SimpleNamespace(direct_filter=flt)
        jb_src = SimpleNamespace()
        game = SimpleNamespace(gameplay=SimpleNamespace(
            music_bot=SimpleNamespace(water_muffle_enabled=False,
                                      stream_source=bot_src),
            jukebox_player=SimpleNamespace(
                players={"p": {"source": jb_src}})))
        _apply_music_water_filter(game, flt)
        self.assertFalse(hasattr(bot_src, "direct_filter"))
        self.assertIs(jb_src.direct_filter, flt)

    def test_bot_source_filtered_when_muffle_enabled(self):
        flt = object()
        bot_src = SimpleNamespace()
        game = SimpleNamespace(gameplay=SimpleNamespace(
            music_bot=SimpleNamespace(water_muffle_enabled=True,
                                      stream_source=bot_src),
            jukebox_player=None))
        _apply_music_water_filter(game, flt)
        self.assertIs(bot_src.direct_filter, flt)


class TestTheMenuModule(unittest.TestCase):
    """Every menu the bot opens is built in libs/music_bot/menus.py or its pages.

    The menus moved out of controller.py together on 2026-10-18 and MapMusicBot
    kept a one-line shim for each one, because gameplay's key bindings, the event
    handler and cinema_handler's "Back to Music Bot" line all call the bot. The
    page groups moved again on 2026-10-26 into `menu_pages/`, and the shims still
    must match them one for one: a menu that loses its shim -- or a shim
    with nothing behind it -- breaks only when a player presses the key, so the
    sets are pinned equal across both levels here, together with the one rule the
    move hinges on: `self.` inside the handler is the handler and the bot is
    always named `self._bot.`, because a menu that quietly used the handler where
    the bot goes still runs until somebody presses the key.
    """

    #: The page modules beside menus.py; each one holds one group of menus.
    PAGES = ("settings.py", "autotune.py", "subtitles.py", "voice_effect.py",
             "party.py", "picks.py", "recording.py", "library.py",
             "external.py", "converter.py")
    #: Not menus: the constructor and the one speaker the pages share.
    NOT_MENUS = {"__init__", "_say"}

    def _modules(self):
        root = Path(__file__).resolve().parents[1] / "libs" / "music_bot"
        controller = (root / "controller.py").read_text(encoding="utf-8")
        menus = (root / "menus.py").read_text(encoding="utf-8")
        pages = [(root / "menu_pages" / name).read_text(encoding="utf-8")
                 for name in self.PAGES]
        return (controller, menus, "\n".join([menus, *pages]),
                ast.parse(controller), ast.parse(menus),
                [ast.parse(page) for page in pages])

    def _builder_classes(self, mtree, page_trees):
        classes = [n for n in mtree.body
                   if isinstance(n, ast.ClassDef) and n.name == "MusicBotMenus"]
        for tree in page_trees:
            # The page modules also hold `_MusicBotEqSlider`; a menu class is
            # one of the `...Menus` mixins, never the slider or the helper.
            classes.extend(n for n in tree.body
                           if isinstance(n, ast.ClassDef)
                           and n.name.endswith("Menus"))
        return classes

    def test_every_menu_has_a_shim_and_every_shim_a_menu(self):
        controller, _menus, all_menus, ctree, mtree, page_trees = self._modules()
        bot = next(n for n in ctree.body
                   if isinstance(n, ast.ClassDef) and n.name == "MapMusicBot")
        handler = set()
        for builder in self._builder_classes(mtree, page_trees):
            handler.update(n.name for n in builder.body
                           if isinstance(n, ast.FunctionDef))
        handler -= self.NOT_MENUS
        shims = {n.name for n in bot.body
                 if isinstance(n, ast.FunctionDef) and len(n.body) == 1
                 and isinstance(n.body[0], ast.Return)
                 and "self._menus()" in ast.unparse(n.body[0].value)}
        self.assertTrue(handler)
        self.assertEqual(shims, handler)
        self.assertNotIn("menu_mod.Menu(", controller)
        self.assertTrue(all_menus.count("menu_mod.Menu(") >= 20)

    def test_no_menu_line_puts_its_label_in_the_action_slot(self):
        """A page's four-tuple is (label, action, key, description).

        The Party Sync line for the external program was copied from the
        Music Bot menu's `(feature id, label, action, description)` shape, so
        its label sat where the action goes and pressing Enter called a
        string: "'str' object is not callable" in `Menu.select_current_item`.
        Nothing in the source reads wrong -- the tuple is well formed, only
        the meaning of its slots is -- so the slots are read here.

        A page that hands its lines to `menu_prefs.build` writes the other
        shape, `(id, label, action, description)`, with the id lowercase and
        snake_case (or None for a row that is never hidden), so the action is
        the third slot there. Both shapes are checked.
        """
        root = (Path(__file__).resolve().parents[1]
                / "libs" / "music_bot" / "menu_pages")
        for name in self.PAGES:
            tree = ast.parse((root / name).read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not (isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Attribute)
                        and node.func.attr == "append"
                        and node.args
                        and isinstance(node.args[0], ast.Tuple)
                        and len(node.args[0].elts) == 4):
                    continue
                elts = node.args[0].elts
                if self._is_line_id(elts[0]):
                    action = elts[2]
                else:
                    action = elts[1]
                self.assertFalse(
                    isinstance(action, ast.Constant)
                    and isinstance(action.value, str),
                    f"{name}:{node.lineno} puts its label in the action slot")

    @staticmethod
    def _is_line_id(node):
        """The first slot of a `menu_prefs.build` line: an id, or None."""
        if not isinstance(node, ast.Constant):
            return False
        if node.value is None:
            return True
        return (isinstance(node.value, str)
                and node.value == node.value.lower()
                and node.value.replace("_", "").isalnum()
                and node.value[:1].isalpha())


    def test_the_voice_effect_shim_hands_the_world_mode_to_the_page(self):
        """Alt+V reaches the page through the bot's own one-line shim."""
        bot = MapMusicBot.__new__(MapMusicBot)
        seen = {}
        bot._menus_handler = SimpleNamespace(
            _open_voice_effect_menu=lambda group=None, standalone=None:
            seen.update(group=group, standalone=standalone))
        bot._open_voice_effect_menu(standalone=True)
        self.assertEqual(seen, {"group": None, "standalone": True})

    def test_a_menu_never_passes_the_handler_where_the_bot_goes(self):
        _controller, _menus, all_menus, _ctree, mtree, page_trees = self._modules()
        for builder in self._builder_classes(mtree, page_trees):
            for method in builder.body:
                if (not isinstance(method, ast.FunctionDef)
                        or method.name in self.NOT_MENUS):
                    continue
                # `self._bot.game` holds a plain `self`, but only as the owner
                # of an attribute; a `self` used as a value is the handler where
                # the code means the bot (`_MusicBotEqSlider(game, self)` handed
                # the slider the handler, and only opening the slider found it).
                owners = {id(node.value) for node in ast.walk(method)
                          if isinstance(node, ast.Attribute)
                          and isinstance(node.value, ast.Name)}
                bare = [(method.name, node.lineno) for node in ast.walk(method)
                        if isinstance(node, ast.Name) and node.id == "self"
                        and id(node) not in owners]
                self.assertEqual(
                    bare, [], f"plain `self` used as a value in {method.name}")
        # The slider takes the bot as its own argument, so it is handed the bot.
        self.assertIn("_MusicBotEqSlider(self._bot.game, self._bot)", all_menus)


if __name__ == "__main__":
    unittest.main()