import queue
import unittest
from types import SimpleNamespace
from unittest import mock

import cyal

from libs import music_bot, voice_chat


class _FakeBuffer:
    def __init__(self):
        self.data = None

    def set_data(self, data, **_kwargs):
        self.data = bytes(data)


class _FakeSource:
    def __init__(self):
        self.state = cyal.SourceState.INITIAL
        self.gain = 1.0
        self.queued = []
        self.play_calls = 0
        self.rewind_calls = 0
        self.processed = 0

    @property
    def buffers_queued(self):
        return len(self.queued)

    @property
    def buffers_processed(self):
        # OpenAL marks even newly queued buffers as processed while STOPPED.
        # INITIAL, unlike STOPPED, can accumulate a real-frame prebuffer.
        if self.state == cyal.SourceState.STOPPED:
            return len(self.queued)
        return self.processed

    def unqueue_buffers(self):
        count = self.buffers_processed
        buffers = self.queued[:count]
        del self.queued[:count]
        self.processed = 0
        return buffers

    def queue_buffers(self, buf):
        self.queued.append(buf)

    def play(self):
        self.play_calls += 1
        self.state = cyal.SourceState.PLAYING

    def stop(self):
        self.state = cyal.SourceState.STOPPED

    def rewind(self):
        self.rewind_calls += 1
        self.state = cyal.SourceState.INITIAL
        self.processed = 0


class TestLocalMegaphoneMonitor(unittest.TestCase):
    def test_worker_handoff_copies_pcm_and_defers_openal(self):
        deferred = []
        audio = SimpleNamespace(defer_audio=deferred.append)
        gameplay = SimpleNamespace(game=SimpleNamespace(audio_mngr=audio))
        pcm = bytearray(b"original")
        worker = object()
        main = object()

        with mock.patch.object(voice_chat.threading, "current_thread", return_value=worker), \
                mock.patch.object(voice_chat.threading, "main_thread", return_value=main), \
                mock.patch.object(voice_chat, "soft_limit_audio", side_effect=lambda data, **_kwargs: data), \
                mock.patch.object(voice_chat, "_feed_local_megaphone_main") as feed_main:
            voice_chat._feed_local_megaphone_direct(gameplay, pcm, producer="music")
            pcm[:] = b"changed!"
            self.assertEqual(len(deferred), 1)
            feed_main.assert_not_called()
            deferred[0]()

        feed_main.assert_called_once_with(gameplay, b"original", "music")

    def test_music_monitor_requests_three_real_frames(self):
        src = _FakeSource()
        local_key = "player-1:music"
        megaphone = SimpleNamespace(
            get_megaphone_player_sources=lambda _key: [src],
            player_sources={
                local_key: {
                    "currents_vol": [1.0],
                    "targets_vol": [1.0],
                }
            },
        )
        gameplay = SimpleNamespace(
            game=SimpleNamespace(audio_mngr=object()),
            megaphone=megaphone,
            player=SimpleNamespace(id="player-1", name="player-1"),
        )

        with mock.patch.object(voice_chat, "_queue_packet_to_source") as queue_frame:
            voice_chat._feed_local_megaphone_main(
                gameplay, b"\x01\x00" * 960, producer="music"
            )

        self.assertEqual(queue_frame.call_count, 1)
        self.assertEqual(queue_frame.call_args.kwargs["real_prebuffer_frames"], 3)

    def test_real_frame_prebuffer_starts_on_third_frame_without_silence(self):
        src = _FakeSource()
        gameplay = SimpleNamespace(
            concert_spectator_mode=True,
            game=SimpleNamespace(audio_mngr=object()),
        )
        frames = [bytes([value, 0]) * 960 for value in (1, 2, 3)]

        with mock.patch.object(voice_chat, "_get_buffer_from_pool", side_effect=lambda _mngr: _FakeBuffer()), \
                mock.patch.object(voice_chat, "_fade_in_packet", side_effect=lambda data: data):
            voice_chat._queue_packet_to_source(
                gameplay, 0, src, frames[0], real_prebuffer_frames=3
            )
            self.assertEqual(src.play_calls, 0)
            voice_chat._queue_packet_to_source(
                gameplay, 0, src, frames[1], real_prebuffer_frames=3
            )
            self.assertEqual(src.play_calls, 0)
            voice_chat._queue_packet_to_source(
                gameplay, 0, src, frames[2], real_prebuffer_frames=3
            )

        self.assertEqual(src.play_calls, 1)
        self.assertEqual([buf.data for buf in src.queued], frames)
        self.assertEqual(src.rewind_calls, 0)

    def test_legacy_path_keeps_existing_silence_cushion(self):
        src = _FakeSource()
        gameplay = SimpleNamespace(
            concert_spectator_mode=True,
            game=SimpleNamespace(audio_mngr=object()),
        )
        frame = b"\x01\x00" * 960

        with mock.patch.object(voice_chat, "_reclaim_source_buffers"), \
                mock.patch.object(voice_chat, "_get_buffer_from_pool", side_effect=lambda _mngr: _FakeBuffer()), \
                mock.patch.object(voice_chat, "_fade_in_packet", side_effect=lambda data: data):
            voice_chat._queue_packet_to_source(gameplay, 0, src, frame)

        self.assertEqual(src.play_calls, 1)
        self.assertEqual(src.queued[0].data, frame)
        self.assertEqual(src.queued[1].data, bytes(len(frame)))


class TestLocalMegaphoneResume(unittest.TestCase):
    def setUp(self):
        self.pool = []
        self.pool_patch = mock.patch.object(voice_chat, "_shared_buffer_pool", self.pool)
        self.pool_patch.start()
        self.addCleanup(self.pool_patch.stop)
        self.gen_buffer = mock.Mock(side_effect=_FakeBuffer)
        self.gameplay = SimpleNamespace(
            concert_spectator_mode=True,
            game=SimpleNamespace(audio_mngr=SimpleNamespace(
                context=SimpleNamespace(gen_buffer=self.gen_buffer),
            )),
        )
        self.src = _FakeSource()
        self.frame = b"\x01\x00" * 960

    def queue_frame(self, frame=None, *, real_prebuffer_frames=3):
        voice_chat._queue_packet_to_source(
            self.gameplay, 0, self.src,
            self.frame if frame is None else frame,
            real_prebuffer_frames=real_prebuffer_frames,
        )

    def start_stream(self):
        for _ in range(3):
            self.queue_frame()
        self.assertEqual(self.src.state, cyal.SourceState.PLAYING)

    def test_repeated_stop_resume_preserves_new_frames_and_reuses_buffers(self):
        self.start_stream()
        for cycle in range(10):
            self.src.stop()
            frames = [bytes([cycle * 3 + index + 2, 0]) * 960 for index in range(3)]
            with mock.patch.object(voice_chat, "_fade_in_packet", side_effect=lambda data: data) as fade:
                for index, frame in enumerate(frames):
                    self.queue_frame(frame)
                    self.assertEqual(self.src.buffers_queued, index + 1)
                    self.assertEqual(self.src.buffers_processed, 0)
                    self.assertEqual(self.src.play_calls, cycle + 1 + (index == 2))
                fade.assert_called_once_with(frames[0])
            self.assertEqual([buf.data for buf in self.src.queued], frames)
            self.assertEqual(self.src.rewind_calls, cycle + 1)
            self.assertEqual(self.src.state, cyal.SourceState.PLAYING)
        self.assertEqual(self.gen_buffer.call_count, 3)
        self.assertEqual(self.pool, [])

    def test_playing_stream_recycles_processed_audio_without_rewind(self):
        self.start_stream()
        self.src.processed = 1
        self.queue_frame()
        self.assertEqual(self.src.play_calls, 1)
        self.assertEqual(self.src.rewind_calls, 0)
        self.assertEqual(self.src.buffers_queued, 3)
        self.assertEqual(self.gen_buffer.call_count, 3)

    def test_underrun_during_reclaim_drains_old_audio_before_rewind(self):
        self.start_stream()
        reclaim = voice_chat._reclaim_source_buffers

        def drain_then_expire(src):
            reclaim(src)
            if src.state == cyal.SourceState.PLAYING:
                src.stop()

        with mock.patch.object(voice_chat, "_reclaim_source_buffers", side_effect=drain_then_expire):
            self.queue_frame()
        self.assertEqual(self.src.state, cyal.SourceState.INITIAL)
        self.assertEqual(self.src.buffers_queued, 1)
        self.assertEqual(self.src.buffers_processed, 0)
        self.assertEqual(self.src.rewind_calls, 1)
        self.assertEqual(self.src.play_calls, 1)

    def test_stopped_legacy_voice_still_starts_immediately_with_cushion(self):
        self.start_stream()
        self.src.stop()
        self.queue_frame(real_prebuffer_frames=None)
        self.assertEqual(self.src.state, cyal.SourceState.PLAYING)
        self.assertEqual(self.src.play_calls, 2)
        self.assertEqual(self.src.rewind_calls, 0)
        self.assertEqual(self.src.buffers_queued, 2)
        self.assertEqual(self.src.queued[1].data, bytes(len(self.frame)))

    def test_paused_source_is_not_rewound_or_forced_to_play(self):
        self.start_stream()
        self.src.state = cyal.SourceState.PAUSED
        self.queue_frame()
        self.assertEqual(self.src.state, cyal.SourceState.PAUSED)
        self.assertEqual(self.src.play_calls, 1)
        self.assertEqual(self.src.rewind_calls, 0)

    def test_rewind_failure_does_not_queue_into_stopped_source_and_can_retry(self):
        self.start_stream()
        self.src.stop()
        with mock.patch.object(self.src, "rewind", side_effect=RuntimeError("device unavailable")):
            self.queue_frame()
        self.assertEqual(self.src.buffers_queued, 0)
        self.assertEqual(len(self.pool), 3)
        self.assertEqual(self.src.play_calls, 1)
        self.start_stream()
        self.assertEqual(self.src.play_calls, 2)
        self.assertEqual(self.gen_buffer.call_count, 3)

    def test_failed_drain_does_not_rewind_or_replay_stale_buffers(self):
        self.start_stream()
        self.src.stop()
        with mock.patch.object(self.src, "unqueue_buffers", side_effect=RuntimeError("device unavailable")):
            self.queue_frame()
        self.assertEqual(self.src.state, cyal.SourceState.STOPPED)
        self.assertEqual(self.src.buffers_queued, 3)
        self.assertEqual(self.src.rewind_calls, 0)
        self.assertEqual(self.src.play_calls, 1)
        self.start_stream()
        self.assertEqual(self.src.play_calls, 2)
        self.assertEqual(self.gen_buffer.call_count, 3)


class TestMusicBotDeadlinePacing(unittest.TestCase):
    def test_small_scheduler_overshoot_does_not_accumulate(self):
        streamer = music_bot.AudioStreamer.__new__(music_bot.AudioStreamer)
        streamer.running = True
        streamer.paused = False
        streamer.bot = None
        streamer.last_send_time = None
        streamer.network_queue = queue.Queue()
        for value in (b"one", b"two", b"three"):
            streamer.network_queue.put((value, None, None))

        sent = []

        def capture(data, _epoch, _seq):
            sent.append(data)
            if len(sent) == 3:
                streamer.running = False

        streamer._send_to_network_actual = capture
        with mock.patch.object(
            music_bot.streaming.time, "perf_counter", side_effect=(1.000, 1.021, 1.041)
        ):
            streamer._network_sender_loop()

        self.assertEqual(sent, [b"one", b"two", b"three"])
        self.assertAlmostEqual(streamer.last_send_time, 1.040, places=6)


if __name__ == "__main__":
    unittest.main()
