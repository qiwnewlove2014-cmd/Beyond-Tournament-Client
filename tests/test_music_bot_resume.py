"""Unit tests for remembering where each Music Bot track stopped (resume.py).

Covers the store (round trip, per-track lookup, the bounded book, a version 1
file, a version 2 map, clearing one track), the near-end rule that starts a
finished song from the top, and the bot's half: a skipped track keeps its
place, coming back to it continues there, the loop writes on its window, and
the last track becomes the replay track after a restart.

No game, OpenAL, ffmpeg or network is required.
"""

import json
import os
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from libs.music_bot import resume  # noqa: E402
from test_music_bot_seek import FakeStreamer, make_bot  # noqa: E402

TARGET = "https://www.youtube.com/watch?v=abc123"
OTHER = "https://www.youtube.com/watch?v=other456"


def make_point(target=TARGET, position=90.0, duration=300.0, **overrides):
    raw = {
        "target": target,
        "title": "Song",
        "source": "youtube",
        "position": position,
        "duration": duration,
    }
    raw.update(overrides)
    return raw


class _ResumeFile(unittest.TestCase):
    """Point the store at a temporary file for the life of one test."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.directory.name, "music_resume.json")
        patcher = mock.patch.object(resume, "path", return_value=self.path)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.directory.cleanup)


class ResumeStoreTests(_ResumeFile):
    def test_two_tracks_keep_their_own_places(self):
        resume.save(make_point(position=100.0))
        resume.save(make_point(target=OTHER, title="Other", position=5.0))
        self.assertEqual(resume.get(TARGET)["position"], 100.0)
        self.assertEqual(resume.get(OTHER)["position"], 5.0)
        self.assertEqual(resume.load()["last"], OTHER)

    def test_a_saved_point_round_trips(self):
        self.assertIsNotNone(resume.save(make_point()))
        point = resume.get(TARGET)
        self.assertEqual(point["title"], "Song")
        self.assertEqual(point["duration"], 300.0)
        self.assertFalse(point["paused"])

    def test_save_leaves_no_temporary_file(self):
        resume.save(make_point(position=1.0))
        resume.save(make_point(position=2.0))
        self.assertEqual(sorted(os.listdir(self.directory.name)),
                         ["music_resume.json"])
        self.assertEqual(resume.get(TARGET)["position"], 2.0)

    def test_an_unreadable_file_is_an_empty_book(self):
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write("{not json")
        book = resume.load()
        self.assertEqual(book["tracks"], {})
        self.assertEqual(book["last"], "")

    def test_a_version_one_single_point_is_read_as_one_track(self):
        with open(self.path, "w", encoding="utf-8") as handle:
            json.dump({"target": TARGET, "title": "Song",
                       "source": "youtube", "position": 33.0,
                       "duration": 300.0}, handle)
        book = resume.load()
        self.assertEqual(book["last"], TARGET)
        self.assertEqual(book["tracks"][TARGET]["position"], 33.0)

    def test_the_book_is_bounded(self):
        for index in range(resume.MAX_TRACKS + 5):
            resume.save(make_point(target=f"{TARGET}/{index}",
                                   position=float(index)))
        book = resume.load()
        self.assertEqual(len(book["tracks"]), resume.MAX_TRACKS)
        self.assertNotIn(f"{TARGET}/0", book["tracks"])
        self.assertIn(f"{TARGET}/{resume.MAX_TRACKS + 4}", book["tracks"])

    def test_forgetting_one_track_keeps_the_others(self):
        resume.save(make_point(position=100.0))
        resume.save(make_point(target=OTHER, title="Other", position=5.0))
        self.assertTrue(resume.forget(TARGET))
        self.assertIsNone(resume.get(TARGET))
        self.assertEqual(resume.get(OTHER)["position"], 5.0)
        self.assertFalse(resume.forget(TARGET))

    def test_a_point_needs_a_target(self):
        self.assertIsNone(resume.normalize_point({"position": 5}))
        self.assertIsNone(resume.normalize_point({"target": "   "}))
        self.assertIsNone(resume.normalize_point("nonsense"))

    def test_a_missing_source_follows_the_target(self):
        self.assertEqual(resume.normalize_point({"target": TARGET})["source"],
                         "youtube")
        self.assertEqual(resume.normalize_point({"target": "C:/song.mp3"})["source"],
                         "local")

    def test_position_is_clamped_and_made_finite(self):
        self.assertEqual(
            resume.normalize_point({"target": TARGET, "position": -20})["position"],
            0.0)
        self.assertEqual(
            resume.normalize_point(
                {"target": TARGET, "position": float("inf")})["position"],
            0.0)
        self.assertEqual(
            resume.normalize_point(
                {"target": TARGET, "position": 10 ** 9})["position"],
            resume.MAX_POSITION_SECONDS)

    def test_a_zero_duration_is_unknown(self):
        self.assertIsNone(
            resume.normalize_point({"target": TARGET, "duration": 0})["duration"])


class OffsetTests(unittest.TestCase):
    def test_no_point_starts_at_the_top(self):
        self.assertEqual(resume.offset_for(None), 0.0)

    def test_a_point_mid_song_resumes_there(self):
        self.assertEqual(resume.offset_for(make_point(position=90.0)), 90.0)

    def test_the_last_seconds_start_over(self):
        self.assertEqual(
            resume.offset_for(make_point(position=297.0, duration=300.0)), 0.0)

    def test_the_end_of_a_track_starts_over(self):
        self.assertEqual(
            resume.offset_for(make_point(position=300.0, duration=300.0)), 0.0)

    def test_an_explicit_duration_wins(self):
        self.assertEqual(
            resume.offset_for(make_point(position=90.0, duration=300.0),
                              duration=95.0),
            0.0)


class MusicBotResumeTests(_ResumeFile):
    def bot(self, **overrides):
        overrides.setdefault("current_target", TARGET)
        overrides.setdefault("current_title", "Song")
        overrides.setdefault("current_source", "youtube")
        bot = make_bot(**overrides)
        bot._resume_enabled = True
        bot._resume_tracks = {}
        bot._resume_last = ""
        bot._resume_saved_at = 0.0
        bot.last_track_title = "Song"
        bot.last_track_target = TARGET
        bot.last_track_source = "youtube"
        bot.last_youtube_url = TARGET
        bot.last_youtube_title = "Song"
        return bot

    def test_a_skipped_track_keeps_its_place(self):
        bot = self.bot()
        bot.streamer = FakeStreamer(position=100.0)
        self.assertTrue(bot._save_resume())
        self.assertEqual(resume.get(TARGET)["position"], 100.0)

    def test_pausing_a_track_then_playing_another_keeps_both_places(self):
        bot = self.bot()
        bot.streamer = FakeStreamer(position=100.0)
        bot.paused = True
        bot._save_resume()
        bot.paused = False
        bot.current_target = OTHER
        bot.current_title = "Other"
        bot.streamer = FakeStreamer(position=5.0)
        bot._save_resume()
        self.assertEqual(bot.resume_offset(TARGET), 100.0)
        self.assertEqual(bot.resume_offset(OTHER), 5.0)

    def test_playing_a_known_track_continues_it(self):
        fd, path = tempfile.mkstemp(suffix=".ogg")
        os.close(fd)
        try:
            bot = self.bot(current_target=path, current_source="local")
            bot._remember_resume(77.0)
            starts = []
            # The bot's own shim is part of the path: patching the method off
            # the bot is what once hid a shim that did not forward the keyword.
            bot._track_source_handler = SimpleNamespace(
                _start_local_file_stream=(
                    lambda *args, **kwargs: starts.append(kwargs)))
            bot.stop = lambda *args, **kwargs: None
            bot._clear_track_queue = lambda: None
            bot._clear_personal_feed = lambda: None
            bot.play_single_track("Song", path, "local")
            self.assertEqual(starts[0]["start_offset"], 77.0)
        finally:
            os.unlink(path)

    def test_a_track_with_no_place_starts_at_the_top(self):
        fd, path = tempfile.mkstemp(suffix=".ogg")
        os.close(fd)
        try:
            bot = self.bot(current_target=path, current_source="local")
            starts = []
            # The bot's own shim is part of the path: patching the method off
            # the bot is what once hid a shim that did not forward the keyword.
            bot._track_source_handler = SimpleNamespace(
                _start_local_file_stream=(
                    lambda *args, **kwargs: starts.append(kwargs)))
            bot.stop = lambda *args, **kwargs: None
            bot._clear_track_queue = lambda: None
            bot._clear_personal_feed = lambda: None
            bot.play_single_track("Song", path, "local")
            self.assertEqual(starts[0]["start_offset"], 0.0)
        finally:
            os.unlink(path)

    def test_the_search_shim_forwards_an_offset(self):
        # The resolution lives in track_source; the bot's shim must carry the
        # keyword through, or a picked search result silently starts at zero.
        bot = self.bot()
        calls = []
        bot._track_source_handler = SimpleNamespace(
            _start_youtube_stream_from_search=(
                lambda *args, **kwargs: calls.append(kwargs)))
        bot._start_youtube_stream_from_search("Song", TARGET, "", start_offset=12.5)
        self.assertEqual(calls[0]["start_offset"], 12.5)

    def test_a_finished_track_starts_from_the_top_next_time(self):
        bot = self.bot()
        bot._remember_resume(100.0)
        bot._forget_resume()
        self.assertIsNone(resume.get(TARGET))
        self.assertEqual(bot.resume_offset(TARGET), 0.0)

    def test_finishing_one_track_leaves_the_other_places_alone(self):
        bot = self.bot()
        bot._remember_resume(100.0)
        bot.current_target = OTHER
        bot._remember_resume(5.0)
        bot._forget_resume(TARGET)
        self.assertIsNone(resume.get(TARGET))
        self.assertEqual(resume.get(OTHER)["position"], 5.0)

    def test_finishing_the_last_track_points_at_what_still_has_a_place(self):
        bot = self.bot()
        bot._remember_resume(100.0)
        bot.current_target = OTHER
        bot._remember_resume(5.0)
        bot._forget_resume(OTHER)
        self.assertEqual(bot._resume_last, TARGET)
        self.assertEqual(resume.load()["last"], TARGET)

    def test_a_swapped_streamer_credits_its_own_track(self):
        # A crossfade promotes its candidate before the bot's current_* names it;
        # the place follows the streamer that produced it, never the old title.
        bot = self.bot()
        first = FakeStreamer(position=100.0)
        first.resume_track = {"target": TARGET, "title": "Song",
                              "source": "youtube", "duration": 300.0}
        bot.streamer = first
        bot._save_resume()
        second = FakeStreamer(position=7.0)
        second.resume_track = {"target": OTHER, "title": "Other",
                               "source": "youtube", "duration": 200.0}
        bot.current_target = OTHER
        bot.streamer = second
        bot._save_resume()
        self.assertEqual(resume.get(TARGET)["position"], 100.0)
        self.assertEqual(resume.get(OTHER)["position"], 7.0)
        self.assertEqual(bot._resume_last, OTHER)

    def test_a_natural_end_forgets_the_track_on_air(self):
        bot = self.bot()
        bot._remember_resume(100.0)
        bot.streamer = None
        bot._forget_resume()
        self.assertIsNone(resume.get(TARGET))

    def test_a_dead_streamer_does_not_erase_a_place(self):
        bot = self.bot()
        bot.streamer = FakeStreamer(position=42.5)
        self.assertTrue(bot._save_resume())
        bot.streamer = FakeStreamer(alive=False)
        self.assertFalse(bot._save_resume())
        self.assertEqual(resume.get(TARGET)["position"], 42.5)

    def test_the_loop_writes_at_most_once_per_window(self):
        bot = self.bot()
        bot.streamer = FakeStreamer(position=10.0)
        bot._resume_saved_at = time.monotonic()
        bot._autosave_resume()
        self.assertEqual(bot._resume_tracks, {})
        bot._resume_saved_at = time.monotonic() - resume.SAVE_INTERVAL - 1
        bot._autosave_resume()
        self.assertEqual(resume.get(TARGET)["position"], 10.0)

    def test_a_restart_makes_the_last_track_the_replay_track(self):
        bot = self.bot()
        bot._remember_resume(61.0)
        fresh = self.bot()
        fresh.last_track_title = ""
        fresh.last_track_target = ""
        fresh.last_youtube_url = ""
        fresh.last_youtube_title = ""
        fresh._resume_tracks = resume.load()["tracks"]
        fresh._resume_last = resume.load()["last"]
        self.assertTrue(fresh._adopt_saved_track())
        self.assertEqual(fresh.last_track_target, TARGET)
        self.assertEqual(fresh.last_track_title, "Song")
        self.assertEqual(fresh.last_youtube_url, TARGET)

    def test_replay_hands_the_saved_place_to_the_player(self):
        bot = self.bot()
        bot._remember_resume(90.0)
        bot.play_single_track = mock.Mock()
        bot._replay_last()
        bot.play_single_track.assert_called_once_with(
            "Song", TARGET, "youtube", start_offset=90.0)

    def test_another_track_is_not_given_someone_elses_place(self):
        bot = self.bot()
        bot._remember_resume(90.0)
        bot.last_track_target = OTHER
        bot.play_single_track = mock.Mock()
        bot._replay_last()
        self.assertEqual(
            bot.play_single_track.call_args.kwargs["start_offset"], 0.0)

    def test_pressing_play_again_says_where_it_continues(self):
        bot = self.bot(playing=False)
        bot._remember_resume(90.0)
        with mock.patch("libs.speech.speak") as speak:
            with mock.patch.object(bot, "_replay_last"):
                bot.toggle_pause()
        speak.assert_any_call("Continuing Song from 1:30.")


if __name__ == "__main__":
    unittest.main()
