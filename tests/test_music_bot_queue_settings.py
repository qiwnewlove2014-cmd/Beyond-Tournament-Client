"""Unit tests for the Music Bot play-next queue (Queue Mode) and the Music Bot
settings menu toggles.

Covers:
- Queue Mode flag defaults + persistence keys.
- _enqueue_track: plays immediately when idle, appends while playing/loading,
  rejects empty targets.
- _advance_track_queue: queued songs play BEFORE the favorites/playlist queue,
  which resumes afterwards.
- _play_queued_next preserves the favorites/playlist queue.
- stop(clear_queue=True) clears the play-next queue; stop(clear_queue=False)
  preserves it.
- _on_result_selected: Queue Mode Enter queues directly (no options menu);
  otherwise the options menu offers "Play Next (Add to Queue)".
- Settings: _reapply_bot_water_filter attach/detach, _sync_map_reverb gated by
  reverb_enabled, settings menu builds with all three toggles, options.set is
  called on toggle.
- camera._apply_music_water_filter skips the bot source when the player
  disabled the underwater muffle but still muffles jukebox sources.

No game, OpenAL, ffmpeg, or network is required.
"""

import os
import sys
import threading
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from libs import music_bot
from libs.camera import _apply_music_water_filter
from libs.gameplay import Gameplay
from libs.music_bot import MapMusicBot


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
    bot.feed_tracks = []
    bot.play_queue = []
    bot.play_queue_index = -1
    bot.play_queue_label = ""
    bot.next_up_queue = []
    bot.queue_mode = False
    bot.water_muffle_enabled = True
    bot.reverb_enabled = True
    bot.broadcast_enabled = False
    bot.broadcast_to_megaphone = False
    bot.game = SimpleNamespace(put=lambda fn: None)
    bot._stop_local = lambda: None
    bot._destroy_stream_source = lambda: None
    bot._clear_personal_feed = lambda: None
    for key, value in overrides.items():
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
        labels = [label for label, _ in items]
        self.assertIn("Play Now", labels)
        self.assertIn("Play Next (Add to Queue)", labels)
        for label, cb in items:
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
        labels = [norm(label) for label, _ in last_menu().items]
        self.assertEqual(labels, [
            "Play Queue (2 waiting)",
            "1. Song A",
            "2. Song B",
            "Clear Queue",
            "Back",
        ])

    def test_empty_queue_has_no_song_items(self):
        bot = make_bot()
        self._open(bot)
        labels = [norm(label) for label, _ in last_menu().items]
        self.assertEqual(labels, [
            "Play Queue (empty)", "Clear Queue", "Back",
        ])

    def test_song_item_enter_restates_title(self):
        bot = make_bot()
        bot.next_up_queue = [{"title": "Song A", "target": "t1"}]
        self._open(bot)
        with mock.patch("libs.music_bot.controller.speak") as speak:
            for label, cb in last_menu().items:
                if norm(label) == "1. Song A":
                    cb()
        speak.assert_called_once_with("1. Song A")

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
            with mock.patch("libs.music_bot.controller.speak") as speak:
                for label, cb in last_menu().items:
                    if norm(label) == "Clear Queue":
                        cb()
        self.assertEqual(bot.next_up_queue, [])
        speak.assert_called_once_with("Queue cleared.")
        # The menu was rebuilt without the removed song item.
        self.assertEqual(len(FakeMenu.instances), 2)
        labels = [norm(label) for label, _ in last_menu().items]
        self.assertEqual(labels, [
            "Play Queue (empty)", "Clear Queue", "Back",
        ])


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
        labels = [norm(label) for label, _ in items]
        # Broadcast to Others is intentionally absent — it has its own
        # keyboard shortcut and must not be duplicated in the settings menu.
        self.assertNotIn("Broadcast to Others: OFF", labels)
        self.assertIn("Underwater Muffle: ON", labels)
        self.assertIn("Room Reverb (Realism): ON", labels)

    def test_water_toggle_persists_and_applies(self):
        gp = mock.MagicMock(spec=Gameplay)
        bot = make_bot()
        bot.stream_source = SimpleNamespace()
        bot.game = SimpleNamespace(stack=[gp], audio_mngr=SimpleNamespace(
            filter=[None, object()]))
        with patch_menu()[0], patch_menu()[1]:
            bot._open_settings_menu()
        with mock.patch("libs.music_bot.controller.options.set") as opt_set, \
                mock.patch("libs.music_bot.controller.speak") as speak:
            for label, cb in last_menu().items:
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
        with mock.patch("libs.music_bot.controller.options.set") as opt_set, \
                mock.patch("libs.music_bot.controller.speak") as speak:
            for label, cb in last_menu().items:
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


if __name__ == "__main__":
    unittest.main()