import queue
import unittest
from types import SimpleNamespace
from unittest import mock

import cyal

import pathlib

from libs import music_bot, voice_chat
from libs.music_bot import streaming


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

    def test_music_monitor_uses_spread_stagger_with_three_real_frames(self):
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

        with mock.patch.object(voice_chat, "queue_and_delay_frame") as stagger:
            voice_chat._feed_local_megaphone_main(
                gameplay, b"\x01\x00" * 960, producer="music"
            )

        # The local song monitor must ride the same per-speaker propagation
        # stagger as remote listeners (no jitter margin, music prebuffer kept),
        # while dropping the installer's per-speaker delay: the owner is not
        # listening to their own broadcast from across the room.
        stagger.assert_called_once_with(
            gameplay, local_key, [src], b"\x01\x00" * 960,
            margin_frames=0,
            real_prebuffer_frames=3,
            ignore_speaker_delay=True,
        )

    def test_mic_monitor_uses_spread_stagger_without_prebuffer(self):
        src = _FakeSource()
        local_key = "player-1:mic"
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

        with mock.patch.object(voice_chat, "queue_and_delay_frame") as stagger:
            voice_chat._feed_local_megaphone_main(
                gameplay, b"\x01\x00" * 960, producer="mic"
            )

        stagger.assert_called_once_with(
            gameplay, local_key, [src], b"\x01\x00" * 960,
            margin_frames=0,
            real_prebuffer_frames=None,
            ignore_speaker_delay=True,
        )

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


class TestTheOwnersVoiceFromTheCabinets(unittest.TestCase):
    """The switch a singer asked for: the cabinets' copy of their own voice.

    The one copy of a broadcast that reaches its sender is the local monitor --
    the Server never echoes a frame back -- so this is the switch that decides
    whether a talker hears themselves out of the walls while they sing.
    """

    def monitor(self, producer, enabled):
        gameplay = SimpleNamespace(game=SimpleNamespace(audio_mngr=object()))
        with mock.patch.object(voice_chat.options, "get", return_value=enabled), \
                mock.patch.object(voice_chat, "soft_limit_audio",
                                  side_effect=lambda data, **_kw: data), \
                mock.patch.object(voice_chat, "_feed_local_megaphone_main") as feed_main:
            voice_chat._feed_local_megaphone_direct(
                gameplay, b"\x01\x00" * 480, producer=producer
            )
        return feed_main

    def test_the_voice_is_not_played_by_the_cabinets_when_it_is_off(self):
        feed_main = self.monitor("mic", False)
        feed_main.assert_not_called()

    def test_the_song_and_a_guitar_keep_their_own_monitor(self):
        for producer in ("music", "guitar"):
            feed_main = self.monitor(producer, False)
            self.assertEqual(feed_main.call_count, 1, producer)

    def test_on_still_plays_the_voice(self):
        feed_main = self.monitor("mic", True)
        self.assertEqual(feed_main.call_count, 1)

    def test_the_choice_is_saved_on_this_machine(self):
        with mock.patch.object(voice_chat.options, "set") as setter:
            voice_chat.set_own_voice_from_cabinets(False)
        setter.assert_called_once_with(voice_chat.OWN_VOICE_CABINET_OPTION, False)

    def test_the_switch_defaults_to_on(self):
        with mock.patch.object(voice_chat.options, "get",
                               side_effect=lambda key, default=None: default):
            self.assertTrue(voice_chat.own_voice_from_cabinets())

    def test_an_unreadable_preference_fails_open(self):
        # A preference that cannot be read must never silence a real player's
        # own voice out of nowhere.
        with mock.patch.object(voice_chat.options, "get",
                               side_effect=RuntimeError("unreadable")):
            self.assertTrue(voice_chat.own_voice_from_cabinets())

    def test_the_recording_keeps_the_voice_when_the_cabinets_are_silenced(self):
        # The game recorder captures what this Client renders, so a voice the
        # PA monitor played is already in the take. With the monitor silenced
        # nothing rendered it, and the recorder must add the transmitted
        # frames itself -- otherwise the switch would empty the file.
        with mock.patch.object(voice_chat, "own_voice_from_cabinets",
                               return_value=True):
            self.assertTrue(voice_chat.voice_is_rendered_locally(True))
        with mock.patch.object(voice_chat, "own_voice_from_cabinets",
                               return_value=False):
            self.assertFalse(voice_chat.voice_is_rendered_locally(True))
        with mock.patch.object(voice_chat, "own_voice_from_cabinets",
                               return_value=True):
            self.assertFalse(voice_chat.voice_is_rendered_locally(False))

    def test_a_running_take_in_pa_test_mode_is_the_one_exception(self):
        # A recording is this game's own output (process loopback), so the file
        # can only hold the voice the speakers played: an audition take in PA
        # Test Mode plays the silenced copy, and nothing else does.
        recorder = mock.Mock()
        recorder.state.return_value = "recording"
        gp = SimpleNamespace(pa_test_mode=True,
                             music_bot=SimpleNamespace(audio_recorder=recorder))
        self.assertTrue(voice_chat.cabinets_render_for_recording(gp))
        # Found through the bot already in hand when the caller has it.
        self.assertTrue(voice_chat.cabinets_render_for_recording(
            SimpleNamespace(pa_test_mode=True), gp.music_bot))
        recorder.state.return_value = "idle"
        self.assertFalse(voice_chat.cabinets_render_for_recording(gp))
        recorder.state.return_value = "recording"
        gp.pa_test_mode = False
        self.assertFalse(voice_chat.cabinets_render_for_recording(gp))
        self.assertFalse(voice_chat.cabinets_render_for_recording(None))
        self.assertFalse(voice_chat.cabinets_render_for_recording(
            SimpleNamespace(pa_test_mode=True)))

    def test_and_the_recorder_is_told_the_take_already_has_it(self):
        # Played again means played once: the loopback holds it.
        with mock.patch.object(voice_chat, "own_voice_from_cabinets",
                               return_value=False):
            self.assertTrue(voice_chat.voice_is_rendered_locally(True, forced=True))
            self.assertFalse(voice_chat.voice_is_rendered_locally(False, forced=True))

    def test_a_forced_take_plays_the_voice_the_switch_silenced(self):
        recorder = mock.Mock()
        recorder.state.return_value = "recording"
        gameplay = SimpleNamespace(
            game=SimpleNamespace(audio_mngr=object()),
            pa_test_mode=True,
            music_bot=SimpleNamespace(audio_recorder=recorder),
        )
        with mock.patch.object(voice_chat.options, "get", return_value=False), \
                mock.patch.object(voice_chat, "soft_limit_audio",
                                  side_effect=lambda data, **_kw: data), \
                mock.patch.object(voice_chat, "_feed_local_megaphone_main") as feed_main:
            voice_chat._feed_local_megaphone_direct(
                gameplay, b"\x01\x00" * 480, producer="mic")
        self.assertEqual(feed_main.call_count, 1)

    def test_the_menu_line_says_why_the_cabinets_are_playing(self):
        # The one place a player would look: a voice rising under a switch that
        # says OFF needs an answer on the line itself.
        from libs.music_bot.controller import MapMusicBot
        recorder = mock.Mock()
        recorder.state.return_value = "recording"
        gp = SimpleNamespace(pa_test_mode=True,
                             music_bot=SimpleNamespace(audio_recorder=recorder))
        with mock.patch.object(voice_chat, "own_voice_from_cabinets",
                               return_value=False):
            bot = SimpleNamespace(_find_gameplay=lambda: gp,
                                  audio_recorder=recorder)
            label = MapMusicBot.own_voice_from_cabinets_label(bot)
            recorder.state.return_value = "idle"
            idle = MapMusicBot.own_voice_from_cabinets_label(bot)
        self.assertEqual(
            label,
            "My own voice from the cabinets: OFF "
            "(this take plays it from the cabinets)")
        self.assertEqual(idle, "My own voice from the cabinets: OFF")

    def test_a_silenced_press_is_the_one_the_take_writes_itself(self):
        # Recording never plays the silenced copy again -- a voice starting up
        # under a player who just muted it reads as a bug (the owner's call,
        # 2026-11-16). The switch is the ears'; the file gets the voice from the
        # microphone, so the recorder marks this press as one it must write.
        with mock.patch.object(voice_chat, "own_voice_from_cabinets",
                               return_value=False):
            self.assertTrue(voice_chat.voice_is_silenced_from_cabinets(True))
            # A plain voice-chat press has no cabinet copy to stand in for.
            self.assertFalse(voice_chat.voice_is_silenced_from_cabinets(False))
        with mock.patch.object(voice_chat, "own_voice_from_cabinets",
                               return_value=True):
            self.assertFalse(voice_chat.voice_is_silenced_from_cabinets(True))

    def test_the_music_bot_line_flips_this_clients_switch(self):
        # The line lives in the Music Bot menu and the whole decision is the
        # Client's: no packet, no map attribute, no Server state.
        from libs.music_bot.controller import MapMusicBot
        with mock.patch.object(voice_chat, "own_voice_from_cabinets",
                               return_value=True), \
                mock.patch.object(voice_chat, "set_own_voice_from_cabinets",
                                  side_effect=lambda value: value) as setter, \
                mock.patch("libs.speech.speak") as said:
            MapMusicBot.toggle_own_voice_from_cabinets(object())
        setter.assert_called_once_with(False)
        self.assertIn("silent from the cabinets", said.call_args[0][0])

    def test_a_press_when_it_is_off_turns_it_back_on(self):
        from libs.music_bot.controller import MapMusicBot
        with mock.patch.object(voice_chat, "own_voice_from_cabinets",
                               return_value=False), \
                mock.patch.object(voice_chat, "set_own_voice_from_cabinets",
                                  side_effect=lambda value: value) as setter, \
                mock.patch("libs.speech.speak") as said:
            MapMusicBot.toggle_own_voice_from_cabinets(object())
        setter.assert_called_once_with(True)
        self.assertIn("comes from the cabinets again", said.call_args[0][0])

    def test_both_pa_feeds_ask_the_switch(self):
        # The song's own monitor carries a mic it mixed in (a bot broadcasting
        # to the megaphone), so it has to offer the owner a mix without that
        # voice; the live relay leg has the same shape.
        source = "".join(
            pathlib.Path(name).read_text(encoding="utf-8")
            for name in (streaming.__file__,
                         pathlib.Path(streaming.__file__).with_name("network.py"),
                         pathlib.Path(streaming.__file__).with_name("live_relay.py"))
        )
        self.assertEqual(
            source.count("not voice_chat.own_voice_from_cabinets()"), 2)
        self.assertEqual(source.count("if part is not mic_part"), 2)


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


class TestMegaphoneStaggerPadding(unittest.TestCase):
    """How much silence `queue_and_delay_frame` pads ahead of a PA frame.

    Since 2026-11-14 the answer is: the installer's trim only. Distance used
    to be padded too (listener->speaker / 343), which paid every cabinet
    twice for the same metre and turned a map's PA into an echo -- so a
    cabinet in this file's geometry now starts on arrival, and the only
    number that staggers a room is the builder's per-speaker `delay`. See
    `.agents/skills/megaphone_system/references/cabinet-alignment.md`.
    """

    def setUp(self):
        voice_chat._speaker_last_calc_time = {}
        voice_chat._speaker_current_delays = {}
        voice_chat._speaker_initial_delays = {}
        voice_chat._speaker_delay_cache_expires = {}
        voice_chat._last_tail_sample = {}
        voice_chat._just_padded = {}
        self.pool = []
        self.pool_patch = mock.patch.object(voice_chat, "_shared_buffer_pool", self.pool)
        self.pool_patch.start()
        self.addCleanup(self.pool_patch.stop)
        self.gen_buffer = mock.Mock(side_effect=_FakeBuffer)
        self.frame = b"\x01\x00" * 960

    def make_gameplay(self, speaker_pos, delay=0.0):
        return SimpleNamespace(
            concert_spectator_mode=False,
            camera=SimpleNamespace(
                focus_object=SimpleNamespace(x=0.0, y=0.0, z=0.0)
            ),
            map=SimpleNamespace(minz=0.0),
            game=SimpleNamespace(audio_mngr=SimpleNamespace(
                context=SimpleNamespace(gen_buffer=self.gen_buffer),
            )),
            megaphone=SimpleNamespace(
                fading_sources=[],
                speaker_data=[{"position": speaker_pos, "delay": delay}],
            ),
        )

    def count_frames(self, src):
        silence = sum(1 for b in src.queued if b.data == bytes(len(self.frame)))
        real = sum(1 for b in src.queued if b.data == self.frame)
        return silence, real

    def test_remote_listener_keeps_six_frame_reserve_by_default(self):
        src = _FakeSource()
        gameplay = self.make_gameplay((0.0, 0.0, 0.0))

        with mock.patch.object(voice_chat, "_reclaim_source_buffers"), \
                mock.patch.object(voice_chat, "_fade_in_packet", side_effect=lambda data: data), \
                mock.patch.object(voice_chat, "_fade_out_from_tail", side_effect=lambda data, _tail: data):
            voice_chat.queue_and_delay_frame(gameplay, "remote-1", [src], self.frame)

        # Speaker at the listener: propagation 0 + static 0, but the stable
        # v1.6 PA reserve defaults to 6 silence frames (120ms cushion).
        silence, real = self.count_frames(src)
        self.assertEqual(silence, 7)  # 6 reserve + 1 legacy cushion
        self.assertEqual(real, 1)
        self.assertEqual(src.play_calls, 1)

    def test_local_monitor_gets_no_jitter_margin(self):
        src = _FakeSource()
        gameplay = self.make_gameplay((0.0, 0.0, 0.0))

        with mock.patch.object(voice_chat, "_reclaim_source_buffers"), \
                mock.patch.object(voice_chat, "_fade_in_packet", side_effect=lambda data: data), \
                mock.patch.object(voice_chat, "_fade_out_from_tail", side_effect=lambda data, _tail: data):
            voice_chat.queue_and_delay_frame(
                gameplay, "local:mic", [src], self.frame, margin_frames=0
            )

        # margin_frames=0: no reserve padding, only the legacy cushion and the
        # real frame — the owner's monitor stays as close to zero-latency as
        # the direct queue path it replaces.
        silence, real = self.count_frames(src)
        self.assertEqual(silence, 1)
        self.assertEqual(real, 1)
        self.assertEqual(src.play_calls, 1)

    def test_local_monitor_does_not_stagger_by_distance(self):
        src = _FakeSource()
        # 6.86 m is exactly one 20 ms frame of travel time -- and it is the air
        # that pays it, not the source's silence padding.
        gameplay = self.make_gameplay((6.86, 0.0, 0.0))

        with mock.patch.object(voice_chat, "_reclaim_source_buffers"), \
                mock.patch.object(voice_chat, "_fade_in_packet", side_effect=lambda data: data), \
                mock.patch.object(voice_chat, "_fade_out_from_tail", side_effect=lambda data, _tail: data):
            voice_chat.queue_and_delay_frame(
                gameplay, "local:mic", [src], self.frame, margin_frames=0
            )

        # The legacy cushion and nothing else: no propagation stagger is
        # padded, so the owner hears their own line on the frame it arrives.
        silence, real = self.count_frames(src)
        self.assertEqual(silence, 1)  # 1 legacy cushion, 0 propagation
        self.assertEqual(real, 1)
        self.assertEqual(src.play_calls, 1)

    def test_owner_monitor_drops_the_installer_delay(self):
        src = _FakeSource()
        # Half a second of installer trim on a speaker 30 m away.
        gameplay = self.make_gameplay((30.0, 0.0, 0.0), delay=0.5)

        with mock.patch.object(voice_chat, "_reclaim_source_buffers"), \
                mock.patch.object(voice_chat, "_fade_in_packet", side_effect=lambda data: data), \
                mock.patch.object(voice_chat, "_fade_out_from_tail", side_effect=lambda data, _tail: data):
            voice_chat.queue_and_delay_frame(
                gameplay, "local:mic", [src], self.frame,
                margin_frames=0, ignore_speaker_delay=True,
            )

        # NONE of the 25 trim frames (and no propagation): the owner hears
        # their own line immediately while the room still gets the offset.
        silence, real = self.count_frames(src)
        self.assertEqual(silence, 1)
        self.assertEqual(real, 1)
        self.assertEqual(src.play_calls, 1)

    def test_remote_listener_still_gets_the_installer_delay(self):
        src = _FakeSource()
        gameplay = self.make_gameplay((30.0, 0.0, 0.0), delay=0.5)

        with mock.patch.object(voice_chat, "_reclaim_source_buffers"), \
                mock.patch.object(voice_chat, "_fade_in_packet", side_effect=lambda data: data), \
                mock.patch.object(voice_chat, "_fade_out_from_tail", side_effect=lambda data, _tail: data):
            voice_chat.queue_and_delay_frame(gameplay, "remote-1", [src], self.frame)

        # 25 trim frames + 6 jitter reserve + 1 legacy cushion, and no
        # propagation: only the owner's own monitor skips the offset.
        silence, real = self.count_frames(src)
        self.assertEqual(silence, 32)
        self.assertEqual(real, 1)


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
