"""Offline direct-playback cache regressions; no native audio or remote I/O."""

import io
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from libs.jukebox_media_cache import JukeboxMediaCache
from libs.music_bot import AudioStreamer, YouTubeSearcher


PAGE = "https://www.youtube.com/watch?v=fixture"


def media(label="first"):
    return {"url": "https://rr.example.googlevideo.com/videoplayback?expire=9000&sig=" + label,
            "http_headers": {"User-Agent": "Exact Agent " + label, "Referer": PAGE}}


class JukeboxMediaIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.now = 100.0
        self.wall = 2000.0
        self.cache = JukeboxMediaCache(monotonic=lambda: self.now, wall_clock=lambda: self.wall)

    def stream(self, *, cache=True, offset=0):
        stream = AudioStreamer(SimpleNamespace(), PAGE, object(),
                               media_cache=self.cache if cache else None,
                               start_offset=offset, start_offset_received_at=100.0)
        stream._init_buffer_pool = Mock()
        stream._read_prebuffer = Mock(return_value=(1, b""))
        stream._reclaim_processed = Mock()
        stream._buffers_queued = Mock(return_value=0)
        stream._play_all = Mock()
        stream._route_aligned_network_frame = Mock()
        return stream

    def process(self, error=b"", exit_code=0):
        return SimpleNamespace(stdout=io.BytesIO(b""), stderr=io.BytesIO(error),
                               poll=lambda: exit_code, kill=Mock(), wait=Mock())

    def run_stream(self, stream, *, resolve=None, processes=None):
        with patch("libs.music_bot.streaming.FFMPEG_PATH", "never-executed"), \
             patch.object(YouTubeSearcher, "get_stream_info", side_effect=resolve or (lambda *a, **kw: media())) as resolver, \
             patch("libs.music_bot.streaming.subprocess.Popen", side_effect=processes or [self.process()]) as launch, \
             patch("libs.music_bot.streaming.time.monotonic", side_effect=lambda: self.now), \
             patch("libs.music_bot.streaming.time.sleep") as sleep, \
             patch("libs.music_bot.streaming.logger.log"), patch("libs.music_bot.streaming.speak") as speak:
            stream.run()
        return resolver, launch, sleep, speak

    def test_only_successful_prebuffer_populates_cache_and_return_skips_resolve(self):
        first = self.stream()
        def prebuffer():
            self.assertIsNone(self.cache.get(PAGE))
            return 1, b""
        first._read_prebuffer.side_effect = prebuffer
        resolver, _, _, _ = self.run_stream(first)
        self.assertEqual(resolver.call_count, 1)
        self.assertTrue(first.ready_event.is_set())
        self.assertIsNotNone(self.cache.get(PAGE))
        second = self.stream()
        resolver, launch, _, _ = self.run_stream(second)
        resolver.assert_not_called()
        command = launch.call_args.args[0]
        self.assertEqual(command[command.index("-i") + 1], media()["url"])
        self.assertEqual(command[command.index("-user_agent") + 1], "Exact Agent first")
        self.assertNotIn("-ss", command)
        self.assertTrue(second.completed_normally)

    def test_cached_return_uses_current_server_offset_not_old_play_position(self):
        self.cache.put(PAGE, media())
        self.now = 103.0
        stream = self.stream(offset=45)
        resolver, launch, _, _ = self.run_stream(stream)
        resolver.assert_not_called()
        command = launch.call_args.args[0]
        # Elapsed compensation (45 + 3) plus the anchored aim-ahead
        # (DIRECT_STARTUP_EST_S): the prebuffer-complete hold trims the
        # residual, so the audible head lands on the shared timeline.
        expected = "%.2f" % (48.0 + AudioStreamer.DIRECT_STARTUP_EST_S)
        self.assertEqual(command[command.index("-ss") + 1], expected)

    def test_expired_entry_resolves_again_without_resetting_server_offset(self):
        self.cache.put(PAGE, media())
        self.now += 301
        stream = self.stream(offset=45)
        stream.start_offset_received_at = self.now
        def resolve(*args, **kwargs):
            self.now += 3
            return media("fresh")
        resolver, launch, _, _ = self.run_stream(stream, resolve=resolve)
        self.assertEqual(resolver.call_count, 1)
        command = launch.call_args.args[0]
        # Same anchored aim-ahead rule: 45 + resolve-elapsed (3) + estimate.
        expected = "%.2f" % (48.0 + AudioStreamer.DIRECT_STARTUP_EST_S)
        self.assertEqual(command[command.index("-ss") + 1], expected)
        self.assertEqual(self.cache.get(PAGE).info(), media("fresh"))

    def test_bad_cached_link_retries_fresh_immediately_with_exact_new_headers(self):
        old = self.cache.put(PAGE, media())
        stream = self.stream()
        stream._read_prebuffer.side_effect = [(0, b""), (1, b"")]
        resolver, launch, sleep, speak = self.run_stream(stream,
            resolve=lambda *a, **kw: media("fresh"),
            processes=[self.process(b"HTTP 403", 1), self.process()])
        self.assertEqual(resolver.call_count, 1)
        self.assertEqual(launch.call_count, 2)
        sleep.assert_not_called()
        speak.assert_not_called()
        commands = [call.args[0] for call in launch.call_args_list]
        self.assertEqual(commands[0][commands[0].index("-i") + 1], media()["url"])
        self.assertEqual(commands[1][commands[1].index("-i") + 1], media("fresh")["url"])
        self.assertEqual(commands[1][commands[1].index("-user_agent") + 1], "Exact Agent fresh")
        self.assertIsNot(self.cache.get(PAGE), old)
        self.assertIsNone(stream.failure_reason)

    def test_failed_fresh_resolution_never_relaunches_bad_cached_link(self):
        self.cache.put(PAGE, media())
        stream = self.stream()
        stream._read_prebuffer.return_value = (0, b"")
        _, launch, sleep, _ = self.run_stream(stream, resolve=lambda *a, **kw: None,
                                             processes=[self.process(b"HTTP 403", 1)])
        self.assertEqual(launch.call_count, 1)
        sleep.assert_not_called()
        self.assertIsNone(self.cache.get(PAGE))
        self.assertEqual(stream.failure_reason, "audio link resolution failed")

    def test_cancelled_cache_hit_does_not_launch_or_evict_healthy_entry(self):
        old = self.cache.put(PAGE, media())
        stream = self.stream()
        def get(url):
            stream.running = False
            return old
        with patch.object(self.cache, "get", side_effect=get):
            resolver, launch, _, speak = self.run_stream(stream)
        launch.assert_not_called()
        resolver.assert_not_called()
        speak.assert_not_called()
        self.assertIs(self.cache.get(PAGE), old)
        self.assertFalse(stream.ready_event.is_set())

    def test_cancellation_during_fresh_retry_drops_late_result(self):
        self.cache.put(PAGE, media())
        stream = self.stream()
        stream._read_prebuffer.return_value = (0, b"")
        def resolve(url, *, cancelled):
            stream.running = False
            self.assertTrue(cancelled())
            return media("late")
        _, launch, _, speak = self.run_stream(stream, resolve=resolve,
                                             processes=[self.process(b"HTTP 403", 1)])
        self.assertEqual(launch.call_count, 1)
        self.assertIsNone(self.cache.get(PAGE))
        self.assertFalse(stream.ready_event.is_set())
        speak.assert_not_called()

    def test_cancelled_prebuffer_never_promotes_or_plays_late_song(self):
        stream = self.stream()
        def prebuffer():
            stream.running = False
            return 1, b""
        stream._read_prebuffer.side_effect = prebuffer
        self.run_stream(stream)
        self.assertIsNone(self.cache.get(PAGE))
        stream._play_all.assert_not_called()
        self.assertFalse(stream.ready_event.is_set())

    def test_failed_initial_prebuffer_is_never_cached(self):
        stream = self.stream()
        stream._read_prebuffer.return_value = (0, b"")
        self.run_stream(stream, processes=[self.process(b"unsupported media", 1)])
        self.assertIsNone(self.cache.get(PAGE))

    def test_cancelled_publication_never_plays_or_discards_a_newer_entry(self):
        for replace in (False, True):
            with self.subTest(newer_entry=replace):
                stream = self.stream()
                put = self.cache.put
                newer = []
                def cancelled_put(key, info):
                    entry = put(key, info)
                    stream.running = False
                    if replace:
                        newer.append(put(key, media("newer")))
                    return entry
                with patch.object(self.cache, "put", side_effect=cancelled_put):
                    self.run_stream(stream)
                self.assertIs(self.cache.get(PAGE), newer[0] if replace else None)
                stream._play_all.assert_not_called()
                self.assertFalse(stream.ready_event.is_set())

    def test_cancel_during_reap_preserves_existing_entry_without_retry(self):
        entry = self.cache.put(PAGE, media())
        stream = self.stream()
        stream._read_prebuffer.return_value = (0, b"")
        process = self.process(b"connection closed", 1)
        process.wait.side_effect = lambda **kw: setattr(stream, "running", False)
        resolver, launch, _, speak = self.run_stream(stream, processes=[process])
        self.assertIs(self.cache.get(PAGE), entry)
        resolver.assert_not_called()
        self.assertEqual(launch.call_count, 1)
        speak.assert_not_called()

    def test_fresh_same_link_retry_success_can_be_cached(self):
        stream = self.stream()
        stream._read_prebuffer.side_effect = [(0, b""), (1, b"")]
        self.run_stream(stream, processes=[self.process(b"HTTP 403", 1), self.process()])
        self.assertEqual(self.cache.get(PAGE).info(), media())

    def test_early_exit_invalidates_only_that_attempts_entry(self):
        for replace in (False, True):
            with self.subTest(newer_entry=replace):
                stream = self.stream()
                replacement = []
                if replace:
                    stream._play_all.side_effect = lambda: replacement.append(self.cache.put(PAGE, media("newer")))
                self.run_stream(stream, processes=[self.process(exit_code=1)])
                self.assertIn("ffmpeg exited early", stream.failure_reason)
                self.assertIs(self.cache.get(PAGE), replacement[0] if replace else None)

    def test_personal_bot_cannot_use_jukebox_cache_even_if_passed(self):
        self.cache.put(PAGE, media())
        with patch("pyogg.OpusEncoder"):
            stream = AudioStreamer(SimpleNamespace(), PAGE, object(), bot=object(), media_cache=self.cache)
        with patch.object(YouTubeSearcher, "get_stream_info", return_value=media("personal")) as resolve:
            self.assertEqual(stream._resolve_playback_info(PAGE, use_cache=True), media("personal"))
        self.assertIsNone(stream._media_cache)
        self.assertEqual(resolve.call_count, 1)
        self.assertEqual(self.cache.get(PAGE).info(), media())


class JukeboxMediaOwnerTests(unittest.TestCase):
    def test_map_changes_drop_sources_but_reuse_metadata_only_for_fixed_songs(self):
        from libs.jukebox import JukeboxPlayer
        from libs.event_handeler import EventHandeler
        class Source:
            position = None
            buffers_processed = 0
            def stop(self): pass
            def delete(self): pass
        game = SimpleNamespace(audio_mngr=SimpleNamespace(context=SimpleNamespace(gen_source=Source)))
        player = JukeboxPlayer(game)
        player._media_cache = JukeboxMediaCache(monotonic=lambda: 100, wall_clock=lambda: 2000)
        entry = player._media_cache.put(PAGE, media())
        handler = EventHandeler.__new__(EventHandeler)
        handler.gameplay = SimpleNamespace(jukebox_player=player)
        with patch("libs.music_bot.AudioStreamer") as stream, patch("libs.jukebox.log_line"):
            player.play("box", 0, 0, 0, "Song", PAGE, 60, playback_id=1)
            first = player.players["box"]
            self.assertIs(stream.call_args.kwargs["media_cache"], player._media_cache)
            handler._stop_jukebox_players_for_map_change(same_map=False)
            self.assertEqual(player.players, {})
            self.assertEqual(player.relay_routes, {})
            self.assertIs(player._media_cache.get(PAGE), entry)
            player.play("box", 0, 0, 0, "Song", PAGE, 60, playback_id=1, start_offset=25)
            self.assertIsNot(player.players["box"]["source"], first["source"])
            self.assertIs(stream.call_args.kwargs["media_cache"], player._media_cache)
            self.assertEqual(stream.call_args.kwargs["start_offset"], 25)
            player.play("live", 1, 1, 0, "Live", PAGE, 0, playback_id=2)
            self.assertIsNone(stream.call_args.kwargs["media_cache"])
            player.stop_all()
        self.assertIsNone(JukeboxPlayer(game)._media_cache.get(PAGE))


if __name__ == "__main__":
    unittest.main()
