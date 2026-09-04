"""Offline tests for private Music Bot download configuration and workers."""

import os
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import tempfile
import threading

from libs.music_bot.music_downloader import (
    MusicDownloadManager,
    SOURCE_FORMATS,
    filter_download_tracks,
    is_supported_music_url,
)


class FakeYoutubeDL:
    instances = []
    result = 0

    def __init__(self, options):
        self.options = options
        self.urls = []
        self.__class__.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def download(self, urls):
        self.urls = list(urls)
        for hook in self.options.get("progress_hooks", []):
            hook({"status": "finished"})
        return self.result


class MusicDownloaderTests(unittest.TestCase):
    def setUp(self):
        FakeYoutubeDL.instances.clear()
        self.sound = Mock()
        self.game = SimpleNamespace(
            put=lambda callback: callback(),
            direct_soundgroup=SimpleNamespace(play=self.sound),
        )
        self.manager = MusicDownloadManager(self.game, lambda: None, "C:/game/ffmpeg.exe")
        self.manager._progress_bar = Mock()

    def test_only_https_youtube_pages_are_accepted(self):
        self.assertTrue(is_supported_music_url("https://youtu.be/abc"))
        self.assertTrue(is_supported_music_url("https://music.youtube.com/playlist?list=abc"))
        for value in (
            "http://youtube.com/watch?v=abc", "file:///song.mp3",
            "https://example.com/song", "https://youtube.com@example.com/song",
        ):
            self.assertFalse(is_supported_music_url(value), value)

    def test_saved_track_filter_skips_local_and_malformed_entries(self):
        tracks = filter_download_tracks([
            {"title": "One", "target": "https://youtube.com/watch?v=one"},
            {"title": "Local", "target": "C:/music/local.mp3", "source": "local"},
            {"title": "Wrong", "target": "https://example.com/song"},
            "not a track",
        ])
        self.assertEqual(tracks, [{
            "title": "One", "target": "https://youtube.com/watch?v=one",
        }])

    def test_output_template_stays_inside_selected_folder(self):
        folder = os.path.abspath("chosen music")
        options = self.manager._ydl_options(folder, "mp3", "320", False)
        self.assertEqual(
            options["format"],
            SOURCE_FORMATS[0],
        )
        self.assertEqual(options["noplaylist"], True)
        self.assertEqual(
            options["extractor_args"],
            {"youtube": {"player_client": ["android", "web"]}},
        )
        self.assertEqual(options["ffmpeg_location"], "C:/game/ffmpeg.exe")
        self.assertEqual(options["postprocessors"][0]["preferredcodec"], "mp3")
        self.assertEqual(options["postprocessors"][0]["preferredquality"], "320")
        self.assertEqual(os.path.commonpath((folder, options["outtmpl"])), folder)
        self.assertEqual(
            os.path.basename(options["outtmpl"]),
            "%(title).180B.%(ext)s",
        )
        self.assertNotIn("%(id)", options["outtmpl"])

    def test_saved_download_folder_is_used_without_opening_selector(self):
        request = {"tracks": [], "label": "Song", "output_format": "mp3", "quality": "192"}
        with tempfile.TemporaryDirectory() as temp, \
                patch(
                    "libs.music_bot.music_downloader.options.get",
                    side_effect=lambda key, default=None: (
                        temp if key == "music_bot_download_folder" else default
                    ),
                ), patch.object(self.manager, "_start") as start, \
                patch.object(self.manager, "_choose_folder") as choose_folder:
            self.manager._use_saved_folder_or_choose(request)
        start.assert_called_once_with(request, os.path.abspath(temp))
        choose_folder.assert_not_called()

    def test_missing_saved_folder_is_cleared_then_selector_opens(self):
        missing = os.path.abspath("missing download folder")
        request = {"tracks": [], "label": "Song"}
        with patch("libs.music_bot.music_downloader.options.get", return_value=missing), \
                patch("libs.music_bot.music_downloader.options.set") as save_option, \
                patch.object(self.manager, "_choose_folder") as choose_folder, \
                patch("libs.music_bot.music_downloader.speak") as spoken:
            self.manager._use_saved_folder_or_choose(request)
        save_option.assert_called_once_with("music_bot_download_folder", "")
        choose_folder.assert_called_once_with(request)
        self.assertIn("no longer available", spoken.call_args.args[0].lower())

    def test_no_saved_folder_preserves_per_download_selector_fallback(self):
        request = {"tracks": [], "label": "Song"}
        with patch("libs.music_bot.music_downloader.options.get", return_value=""), \
                patch("libs.music_bot.music_downloader.options.set") as save_option, \
                patch.object(self.manager, "_choose_folder") as choose_folder:
            self.manager._use_saved_folder_or_choose(request)
        save_option.assert_not_called()
        choose_folder.assert_called_once_with(request)

    def test_explicit_folder_choice_is_persisted_and_can_be_cleared(self):
        with tempfile.TemporaryDirectory() as temp, \
                patch("libs.music_bot.music_downloader.options.set") as save_option, \
                patch("libs.music_bot.music_downloader.speak"):
            self.manager._accept_default_folder(temp)
            save_option.assert_called_once_with(
                "music_bot_download_folder",
                os.path.abspath(temp),
            )
            save_option.reset_mock()
            self.manager.clear_default_folder()
            save_option.assert_called_once_with("music_bot_download_folder", "")

    def test_active_download_blocks_default_folder_changes(self):
        with patch.object(self.manager, "is_active", return_value=True), \
                patch("libs.music_bot.music_downloader.options.set") as save_option, \
                patch.object(self.manager, "_open_folder_selector") as open_selector, \
                patch("libs.music_bot.music_downloader.speak"):
            self.manager.choose_default_folder()
            self.manager.clear_default_folder()
        save_option.assert_not_called()
        open_selector.assert_not_called()

    def test_unsaved_search_result_downloads_as_one_song(self):
        request = {
            "tracks": [{"title": "Search Result", "target": "https://youtube.com/watch?v=abc"}],
            "label": "Search Result",
            "output_format": "m4a", "quality": "192",
        }
        fake_module = SimpleNamespace(YoutubeDL=FakeYoutubeDL)
        with patch.dict(sys.modules, {"yt_dlp": fake_module}), patch("libs.music_bot.music_downloader.speak") as spoken:
            self.manager._run_download(request, os.path.abspath("music"))
        self.assertEqual(len(FakeYoutubeDL.instances), 1)
        self.assertTrue(FakeYoutubeDL.instances[0].options["noplaylist"])
        self.assertEqual(FakeYoutubeDL.instances[0].urls, [request["tracks"][0]["target"]])
        self.sound.assert_called_once_with("ui/unread.ogg", cat="ui")
        self.manager._progress_bar.destroy.assert_called_once_with()
        self.assertIn("download complete", spoken.call_args.args[0].lower())

    def test_saved_tracks_download_individually_without_expanding_embedded_lists(self):
        request = {
            "tracks": [
                {"title": "One", "target": "https://youtu.be/one?list=mix"},
                {"title": "Two", "target": "https://youtu.be/two"},
            ],
            "label": "Favorites",
            "output_format": "ogg", "quality": "128",
        }
        fake_module = SimpleNamespace(YoutubeDL=FakeYoutubeDL)
        with patch.dict(sys.modules, {"yt_dlp": fake_module}), patch("libs.music_bot.music_downloader.speak"):
            self.manager._run_download(request, os.path.abspath("music"))
        self.assertEqual(len(FakeYoutubeDL.instances), 2)
        self.assertTrue(all(instance.options["noplaylist"] for instance in FakeYoutubeDL.instances))
        self.assertTrue(all(instance.options["postprocessors"][0]["preferredcodec"] == "vorbis"
                            for instance in FakeYoutubeDL.instances))

    def test_403_retries_alternate_youtube_media_route(self):
        class RouteFallbackYoutubeDL(FakeYoutubeDL):
            def download(self, urls):
                self.urls = list(urls)
                if self.options["format"] == SOURCE_FORMATS[0]:
                    raise RuntimeError("HTTP Error 403: Forbidden")
                return 0

        fake_module = SimpleNamespace(YoutubeDL=RouteFallbackYoutubeDL)
        with patch.dict(sys.modules, {"yt_dlp": fake_module}):
            result = self.manager._download_track(
                "https://youtu.be/abc", os.path.abspath("music"), "mp3", "192"
            )
            self.assertEqual(result, 0)
            self.assertEqual(
                [instance.options["format"] for instance in RouteFallbackYoutubeDL.instances],
                list(SOURCE_FORMATS),
            )
            self.assertEqual(self.manager._preferred_source_format, SOURCE_FORMATS[1])

            RouteFallbackYoutubeDL.instances.clear()
            result = self.manager._download_track(
                "https://youtu.be/second", os.path.abspath("music"), "mp3", "192"
            )
            self.assertEqual(result, 0)
            self.assertEqual(
                [instance.options["format"] for instance in RouteFallbackYoutubeDL.instances],
                [SOURCE_FORMATS[1]],
            )

    def test_403_can_switch_back_from_audio_only_to_progressive(self):
        class AudioFallbackYoutubeDL(FakeYoutubeDL):
            def download(self, urls):
                self.urls = list(urls)
                if self.options["format"] == SOURCE_FORMATS[1]:
                    raise RuntimeError("HTTP Error 403: Forbidden")
                return 0

        self.manager._preferred_source_format = SOURCE_FORMATS[1]
        fake_module = SimpleNamespace(YoutubeDL=AudioFallbackYoutubeDL)
        with patch.dict(sys.modules, {"yt_dlp": fake_module}):
            result = self.manager._download_track(
                "https://youtu.be/abc", os.path.abspath("music"), "mp3", "192"
            )
        self.assertEqual(result, 0)
        self.assertEqual(
            [instance.options["format"] for instance in AudioFallbackYoutubeDL.instances],
            [SOURCE_FORMATS[1], SOURCE_FORMATS[0]],
        )
        self.assertEqual(self.manager._preferred_source_format, SOURCE_FORMATS[0])

    def test_cancel_prevents_later_tracks_and_uses_warning_notification(self):
        request = {
            "tracks": [{"title": "One", "target": "https://youtu.be/one"}],
            "label": "Favorites",
            "output_format": "mp3", "quality": "192",
        }
        self.manager._cancel_event.set()
        with patch("libs.music_bot.music_downloader.speak") as spoken:
            self.manager._run_download(request, os.path.abspath("music"))
        self.sound.assert_called_once_with("ui/warn.ogg", cat="ui")
        self.assertIn("cancelled", spoken.call_args.args[0].lower())

    def test_start_uses_broadcast_sound(self):
        request = {
            "tracks": [{"title": "One", "target": "https://youtu.be/one"}],
            "label": "One", "output_format": "mp3", "quality": "192",
        }
        fake_thread = Mock()
        fake_thread.is_alive.return_value = False
        with tempfile.TemporaryDirectory() as folder, \
                patch("libs.music_bot.music_downloader.threading.Thread", return_value=fake_thread), \
                patch("libs.music_bot.music_downloader.speak"):
            self.manager._start(request, folder)
        fake_thread.start.assert_called_once_with()
        self.manager._progress_bar.create.assert_not_called()
        self.manager._progress_bar.set_value.assert_not_called()
        self.sound.assert_called_once_with("ui/broadcast.ogg", cat="ui")

    def test_progress_control_exists_only_while_download_menu_is_open(self):
        worker = Mock()
        worker.is_alive.return_value = True
        with self.manager._lock:
            self.manager._worker = worker
            self.manager._progress_percent = 46
        self.manager.show_progress_bar()
        self.manager._progress_bar.create.assert_called_once_with()
        self.manager._progress_bar.set_value.assert_called_once_with(46)
        self.manager.hide_progress_bar()
        self.manager._progress_bar.destroy.assert_called_once_with()

    def test_progress_hook_reports_overall_playlist_percentage(self):
        worker = Mock()
        worker.is_alive.return_value = True
        with self.manager._lock:
            self.manager._worker = worker
            self.manager._progress_track_index = 2
            self.manager._progress_track_count = 4
            self.manager._track_progress = {1: 1.0}
            self.manager._progress_completed = 1
        with patch("libs.music_bot.music_downloader.time.monotonic", return_value=1.0):
            self.manager._progress_hook({
                "status": "downloading",
                "downloaded_bytes": 50,
                "total_bytes": 100,
            })
        self.assertEqual(self.manager._progress_percent, 37)
        self.manager._progress_bar.set_value.assert_called_once_with(37)
        self.assertEqual(
            self.manager.progress_menu_label(),
            "Download progress: 37 percent, 1 of 4 files complete",
        )

    def test_saved_batch_uses_bounded_parallel_slots_and_notifies_each_file(self):
        class ConcurrentYoutubeDL(FakeYoutubeDL):
            active = 0
            maximum = 0
            started = 0
            guard = threading.Lock()
            first_pair = threading.Barrier(2)

            def download(self, urls):
                self.urls = list(urls)
                with self.guard:
                    self.__class__.active += 1
                    self.__class__.started += 1
                    self.__class__.maximum = max(
                        self.__class__.maximum, self.__class__.active
                    )
                    started = self.__class__.started
                try:
                    if started <= 2:
                        self.__class__.first_pair.wait(timeout=1.0)
                    for hook in self.options.get("progress_hooks", []):
                        hook({"status": "finished"})
                    return 0
                finally:
                    with self.guard:
                        self.__class__.active -= 1

        request = {
            "tracks": [
                {"title": f"Song {index}", "target": f"https://youtu.be/{index}"}
                for index in range(1, 4)
            ],
            "label": "Favorites", "output_format": "mp3", "quality": "192",
            "parallel_downloads": 2, "notify_each_file": True,
        }
        fake_module = SimpleNamespace(YoutubeDL=ConcurrentYoutubeDL)
        with patch.dict(sys.modules, {"yt_dlp": fake_module}), \
                patch("libs.music_bot.music_downloader.speak") as spoken:
            self.manager._run_download(request, os.path.abspath("music"))
        self.assertEqual(ConcurrentYoutubeDL.maximum, 2)
        self.assertEqual(self.sound.call_count, 3)
        self.assertTrue(all(
            call.args == ("ui/unread.ogg",) and call.kwargs == {"cat": "ui"}
            for call in self.sound.call_args_list
        ))
        per_file_messages = [
            call.args[0] for call in spoken.call_args_list
            if call.args and call.args[0].startswith("Downloaded ")
        ]
        self.assertEqual(len(per_file_messages), 3)

    def test_progress_bar_updates_are_throttled_off_the_worker(self):
        with self.manager._lock:
            self.manager._progress_track_index = 1
            self.manager._progress_track_count = 1
        with patch("libs.music_bot.music_downloader.time.monotonic", side_effect=(1.0, 1.01, 1.2)):
            for downloaded in (10, 11, 12):
                self.manager._progress_hook({
                    "status": "downloading",
                    "downloaded_bytes": downloaded,
                    "total_bytes": 100,
                })
        self.assertEqual(
            [call.args[0] for call in self.manager._progress_bar.set_value.call_args_list],
            [10, 12],
        )

    def test_close_cancels_worker_and_destroys_progress_control(self):
        self.manager.close()
        self.assertTrue(self.manager._cancel_event.is_set())
        self.assertTrue(self.manager._closed)
        self.manager._progress_bar.destroy.assert_called_once_with()

    def test_music_bot_exposes_every_requested_download_source(self):
        source = (Path(__file__).resolve().parents[1] / "libs" / "music_bot" / "controller.py").read_text(encoding="utf-8")
        for label in (
            "Music Download Center", "Download Current Song", "Download Song",
            "Download All Favorites", "Download a Saved Playlist", "Download This Track",
            "Set Download Folder", "Clear Saved Download Folder",
        ):
            self.assertIn(label, source)
        self.assertIn("class DownloadMusicMenu", source)
        self.assertIn("download_mgr.hide_progress_bar()", source)


if __name__ == "__main__":
    unittest.main()
