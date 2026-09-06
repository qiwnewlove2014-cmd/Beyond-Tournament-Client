"""Unit tests for the Music Bot equalizer (EQ profiles + custom sliders) and
the crossfade between auto-advanced tracks.

Covers:
- EQ: value normalization, set_eq_profile persistence, cached preset slots,
  in-place custom-slot mutation, EFX slot release when leaving Custom.
- Crossfade: remaining-time math, peek/consume ordering vs the normal
  end-of-song advance, pre-roll start gating, the full commit/fade cycle
  (queue consumed, streamer swapped, outgoing network muted, old source
  deleted after the ramp), and cancellation.

No game, OpenAL, ffmpeg, or network is required.
"""

import os
import queue
import struct
import sys
import threading
import time
import unittest
from collections import deque
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from libs.music_bot import controller as bot_module
from libs.music_bot import MapMusicBot
from libs.music_bot import streaming as stream_mod


class FakeSrc:
    """Minimal OpenAL source stand-in with a mutable gain."""

    def __init__(self, gain=0.5):
        self.gain = gain
        self.direct_channels = True
        self.spatialize = False
        self.deleted = False

    def stop(self):
        pass

    def delete(self):
        self.deleted = True

    @property
    def buffers_processed(self):
        return 0

    @property
    def buffers_queued(self):
        return 0

    def unqueue_buffers(self):
        return None


class FakeStreamer:
    """Drop-in AudioStreamer used only by the crossfade state machine."""

    instances = []

    def __init__(self, game, audio_url, source, volume=50, bot=None,
                 channels=2, spatial_pair=None, start_offset=0.0,
                 http_headers=None, start_offset_received_at=None,
                 canonical_url=None, media_cache=None, timeline_anchor=None,
                 start_paused=False, room_lead_in_s=None,
                 join_playing_room=False):
        self.game = game
        self.audio_url = audio_url
        self.source = source
        self.bot = bot
        self.volume = volume
        self.canonical_url = canonical_url
        self.http_headers = dict(http_headers or {})
        self.prebuffer_event = threading.Event()
        self.ready_event = threading.Event()
        self.failure_reason = None
        self.network_muted = False
        self.network_queue = queue.Queue()
        self.crossfade_mix = None
        self.paused = bool(start_paused)
        self.alive = True
        self.stopped = False
        self.set_pause_calls = []
        self.instances.append(self)

    def begin_network_crossfade(self, partner, seconds):
        self.crossfade_mix = (partner, seconds)

    def start(self):
        pass

    def is_alive(self):
        return self.alive

    def set_pause(self, paused):
        self.set_pause_calls.append(bool(paused))
        self.paused = bool(paused)
        if not paused:
            self.ready_event.set()

    def stop(self):
        self.stopped = True
        self.alive = False


def make_bot(**overrides):
    """Hermetic MapMusicBot built with __new__ (no game/OpenAL needed)."""
    bot = MapMusicBot.__new__(MapMusicBot)
    bot._playback_generation_lock = threading.Lock()
    bot._playback_generation = 0
    bot.enabled = True
    bot.playing = True
    bot.paused = False
    bot.is_loading_stream = False
    bot.current_title = "Current"
    bot.current_target = "https://www.youtube.com/watch?v=cur"
    bot.current_source = "youtube"
    bot.current_duration = None
    bot.searching = False
    bot.stream_source = FakeSrc()
    bot.streamer = None
    bot.current_local_sound = None
    bot.mode = "youtube"
    bot.volume = 50
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
    bot.eq_profile = "normal"
    bot.eq_values = {"bass": 50, "mid": 50, "treble": 50}
    bot._eq_slots = {}
    bot._custom_eq_slot = None
    bot.crossfade_enabled = False
    bot._known_durations = {}
    bot._crossfade = None
    bot.duck_multiplier = 1.0
    bot.game = SimpleNamespace(put=lambda fn: None)
    bot._stop_local = lambda: None
    bot._destroy_stream_source = lambda: None
    bot._clear_personal_feed = lambda: None
    bot._find_gameplay = lambda: None
    for key, value in overrides.items():
        setattr(bot, key, value)
    return bot


class FakeEffect:
    """Stand-in for an OpenAL effect slot with in-place parameter editing."""

    def __init__(self):
        self.sets = []
        self.effect = self

    def set(self, *params):
        self.sets.append(params)


class FakeAudio:
    def __init__(self):
        self.sends = []
        self.created = []
        self.released = []
        self.efx = SimpleNamespace(
            send=lambda *args: self.sends.append(args))
        self.gen_effect = self._gen_effect
        self.release_effect_slot = lambda slot: self.released.append(slot)
        self.volume_categories = {"music": [100]}

    def _gen_effect(self, name, *params):
        eff = FakeEffect()
        eff.params = params
        self.created.append((name, params, eff))
        return eff


class TestEqValues(unittest.TestCase):
    def test_normalize_defaults_to_50(self):
        self.assertEqual(MapMusicBot._normalize_eq_values(None),
                         {"bass": 50, "mid": 50, "treble": 50})

    def test_normalize_clamps_and_ignores_garbage(self):
        values = MapMusicBot._normalize_eq_values(
            {"bass": 200, "mid": -5, "treble": "x", "other": 1})
        self.assertEqual(values,
                         {"bass": 100, "mid": 0, "treble": 50})

    def test_custom_parameters_match_jukebox_curve(self):
        params = dict(MapMusicBot._custom_eq_parameters(
            {"bass": 50, "mid": 50, "treble": 50}))
        # Unity (50) maps to a gain of 1.0 on every driven band.
        self.assertEqual(params["low_gain"], 1.0)
        self.assertEqual(params["mid1_gain"], 1.0)
        self.assertEqual(params["mid2_gain"], 1.0)
        self.assertEqual(params["high_gain"], 1.0)
        params = dict(MapMusicBot._custom_eq_parameters(
            {"bass": 100, "mid": 50, "treble": 0}))
        self.assertGreater(params["low_gain"], 1.0)
        self.assertLess(params["high_gain"], 1.0)


class TestEqProfilePersistence(unittest.TestCase):
    def test_set_preset_caches_slot_and_persists(self):
        bot = make_bot()
        audio = FakeAudio()
        bot.game = SimpleNamespace(audio_mngr=audio)
        bot.stream_source = FakeSrc()
        with mock.patch("libs.music_bot.controller.options.set") as opt_set:
            bot.set_eq_profile("bass_boost")
        self.assertEqual(bot.eq_profile, "bass_boost")
        self.assertEqual(opt_set.call_args_list[0][0],
                         ("music_bot_eq_profile", "bass_boost"))
        # Applied to the live source (aux send index 1).
        self.assertEqual(len(audio.sends), 1)
        self.assertEqual(audio.sends[0][0], bot.stream_source)
        self.assertEqual(audio.sends[0][1], 1)
        slot = audio.sends[0][2]
        self.assertIsNotNone(slot)
        self.assertEqual(audio.created[0][0], "EQUALIZER")
        # A second call reuses the cached slot.
        with mock.patch("libs.music_bot.controller.options.set"):
            bot.set_eq_profile("bass_boost")
        self.assertEqual(len(audio.created), 1)

    def test_normal_detaches_send(self):
        bot = make_bot()
        audio = FakeAudio()
        bot.game = SimpleNamespace(audio_mngr=audio)
        bot.stream_source = FakeSrc()
        with mock.patch("libs.music_bot.controller.options.set"):
            bot.set_eq_profile("bass_boost")
            bot.set_eq_profile("normal")
        self.assertEqual(bot.eq_profile, "normal")
        # Leaving a preset does not release it (it stays cached for reuse),
        # but the live send is detached with slot=None.
        last = audio.sends[-1]
        self.assertEqual(last[2], None)

    def test_custom_slot_mutated_in_place_and_released_on_leave(self):
        bot = make_bot()
        audio = FakeAudio()
        bot.game = SimpleNamespace(audio_mngr=audio)
        bot.stream_source = FakeSrc()
        with mock.patch("libs.music_bot.controller.options.set"):
            bot.set_eq_profile("custom", {"bass": 20, "mid": 50, "treble": 80})
            slot1 = audio.sends[-1][2]
            self.assertIsNotNone(slot1)
            # Slider tick: same slot, parameters mutated in place.
            bot.set_eq_profile("custom", {"bass": 30, "mid": 50, "treble": 80})
            slot2 = audio.sends[-1][2]
            self.assertIs(slot1, slot2)
            self.assertGreater(len(slot1.sets), 0)
            # Leaving custom releases the single custom slot.
            bot.set_eq_profile("bass_boost")
        self.assertIn(slot1, audio.released)


class TestRemainingSeconds(unittest.TestCase):
    def test_remaining_uses_duration_and_position(self):
        bot = make_bot(current_duration=10.0)
        bot.track_position = lambda: 4.0
        self.assertEqual(bot._remaining_seconds(), 6.0)

    def test_remaining_none_without_duration(self):
        bot = make_bot(current_duration=None)
        bot.track_position = lambda: 4.0
        self.assertIsNone(bot._remaining_seconds())


class TestPeekConsume(unittest.TestCase):
    def track(self, title, target="https://target"):
        return {"title": title, "target": target, "source": "youtube"}

    def test_peek_does_not_consume(self):
        bot = make_bot()
        bot.next_up_queue = [self.track("Q1")]
        bot.play_queue = [self.track("P1"), self.track("P2")]
        bot.play_queue_index = 0
        self.assertEqual(bot._peek_next_track()["title"], "Q1")
        self.assertEqual(len(bot.next_up_queue), 1)

    def test_next_up_consumed_before_playlist(self):
        bot = make_bot()
        bot.next_up_queue = [self.track("Q1")]
        bot.play_queue = [self.track("P1"), self.track("P2")]
        bot.play_queue_index = 0
        self.assertEqual(bot._consume_next_track()["title"], "Q1")
        self.assertEqual(bot.next_up_queue, [])
        # Playlist resumes afterwards exactly like _advance_track_queue.
        self.assertEqual(bot._consume_next_track()["title"], "P2")
        self.assertEqual(bot.play_queue_index, 1)

    def test_last_playlist_song_has_no_next(self):
        bot = make_bot()
        bot.play_queue = [self.track("P1")]
        bot.play_queue_index = 0
        self.assertIsNone(bot._peek_next_track())
        self.assertIsNone(bot._consume_next_track())


class TestCrossfadeStateMachine(unittest.TestCase):
    def _playing_bot(self, position=5.0, duration=10.0, queue=None):
        bot = make_bot(playing=True, paused=False, is_loading_stream=False)
        bot.crossfade_enabled = True
        bot.current_duration = duration
        bot.streamer = FakeStreamer(None, "u", bot.stream_source, bot=bot)
        pos = [position]

        def track_position():
            return pos[0]
        bot.track_position = track_position
        bot._position = pos
        bot._new_bot_source = lambda: FakeSrc()
        if queue:
            bot.next_up_queue = queue
        return bot

    def test_no_roll_when_disabled(self):
        bot = self._playing_bot()
        bot.crossfade_enabled = False
        bot.next_up_queue = [{"title": "Next", "target": "C:\\next.mp3",
                              "source": "local"}]
        bot._update_crossfade()
        self.assertIsNone(bot._crossfade)

    def test_no_roll_without_duration(self):
        bot = self._playing_bot(duration=None)
        bot.next_up_queue = [{"title": "Next", "target": "C:\\next.mp3",
                              "source": "local"}]
        bot._update_crossfade()
        self.assertIsNone(bot._crossfade)

    def test_no_roll_without_next_track(self):
        bot = self._playing_bot()
        bot._update_crossfade()
        self.assertIsNone(bot._crossfade)

    def test_full_local_commit_and_fade(self):
        bot = self._playing_bot(position=4.0, duration=10.0)
        bot.next_up_queue = [{"title": "Next Song",
                              "target": "C:\\next.mp3", "source": "local"}]
        old_streamer = bot.streamer
        old_source = bot.stream_source
        with mock.patch.object(bot_module, "AudioStreamer",
                               FakeStreamer) as streamer_cls:
            # Far from the end: pre-roll starts but nothing launches yet.
            bot._update_crossfade()
            self.assertIsNotNone(bot._crossfade)
            self.assertIsNone(bot._crossfade["candidate"])
            # Near the end: the paused candidate is launched.
            bot._position[0] = 7.5
            bot._update_crossfade()
            cand = bot._crossfade["candidate"]
            self.assertIsNotNone(cand)
            self.assertTrue(cand.paused)
            self.assertTrue(cand.network_muted)
            # Candidate ready + outro inside the fade window: hand over.
            # A stale pre-roll frame sits in the candidate's queue: commit
            # must drain it so the network blend starts at the live position.
            cand.network_queue.put((b"stale", None, None))
            cand.prebuffer_event.set()
            bot._position[0] = 8.0
            bot._update_crossfade()
        self.assertEqual(len(bot.next_up_queue), 0)
        self.assertEqual(bot.current_title, "Next Song")
        self.assertIs(bot.streamer, cand)
        self.assertIsNot(bot.stream_source, old_source)
        # The outgoing stream blends its network leg into the candidate's
        # (real overlap for the room) instead of hard-muting at commit.
        self.assertIsNotNone(old_streamer.crossfade_mix)
        self.assertIs(old_streamer.crossfade_mix[0], cand)
        self.assertEqual(old_streamer.crossfade_mix[1],
                         MapMusicBot.CROSSFADE_SECONDS)
        self.assertFalse(old_streamer.network_muted)
        self.assertTrue(cand.network_muted)
        self.assertTrue(cand.network_queue.empty())
        self.assertEqual(cand.set_pause_calls, [False])
        self.assertEqual(bot._crossfade["phase"], "fading")
        # Ramp to completion: outgoing stream stopped, old source deleted,
        # and the incoming stream's own network leg takes over the broadcast.
        bot._crossfade["fade_started_at"] = time.monotonic() - 10.0
        bot._update_fade_gains(0.8)
        self.assertTrue(old_streamer.stopped)
        self.assertTrue(old_source.deleted)
        self.assertFalse(cand.network_muted)
        self.assertIsNone(bot._crossfade)
        self.assertAlmostEqual(bot.stream_source.gain, 0.8, places=6)

    def test_cancel_tears_down_candidate(self):
        bot = self._playing_bot(position=4.0, duration=10.0)
        bot.next_up_queue = [{"title": "Next", "target": "C:\\next.mp3",
                              "source": "local"}]
        with mock.patch.object(bot_module, "AudioStreamer", FakeStreamer):
            bot._update_crossfade()
            bot._position[0] = 7.5
            bot._update_crossfade()
            cand = bot._crossfade["candidate"]
            cand_source = bot._crossfade["candidate_source"]
            self.assertTrue(bot._cancel_crossfade())
        self.assertTrue(cand.stopped)
        self.assertTrue(cand_source.deleted)
        self.assertIsNone(bot._crossfade)
        # Cancelling again is a no-op.
        self.assertFalse(bot._cancel_crossfade())

    def test_dead_current_stream_abandons_roll(self):
        bot = self._playing_bot(position=4.0, duration=10.0)
        bot.next_up_queue = [{"title": "Next", "target": "C:\\next.mp3",
                              "source": "local"}]
        with mock.patch.object(bot_module, "AudioStreamer", FakeStreamer):
            bot._update_crossfade()
            bot.streamer.alive = False  # current song died mid-roll
            bot._update_crossfade()
        self.assertIsNone(bot._crossfade)
        # The queue was never consumed: the normal advance still owns it.
        self.assertEqual(len(bot.next_up_queue), 1)


class TestBroadcastMixBlend(unittest.TestCase):
    """PCM blend math behind the party-audible network crossfade."""

    @staticmethod
    def _frame(value, n=3840):
        return struct.pack("<h", value) * (n // 2)

    def test_blend_is_convex_and_never_clips(self):
        streamer = stream_mod.AudioStreamer.__new__(stream_mod.AudioStreamer)
        half = streamer._mix_network_frames(
            self._frame(32767), self._frame(-32767), 0.5, 0.5)
        self.assertEqual(len(half), 3840)
        # audioop truncates each scaled sample, so the null blend lands within
        # one LSB of zero.
        self.assertLessEqual(
            abs(struct.unpack("<h", half[:2])[0]), 1)
        # Full-scale same-sign at 0.5/0.5 stays inside int16 (no clipping).
        both = streamer._mix_network_frames(
            self._frame(32767), self._frame(32767), 0.5, 0.5)
        # audioop truncates each scaled sample: 32767*0.5 -> 16383, doubled.
        self.assertIn(struct.unpack("<h", both[:2])[0], (32766, 32767))
        # Full strength on one side reproduces that input exactly.
        self.assertEqual(
            streamer._mix_network_frames(
                self._frame(12345), self._frame(-999), 1.0, 0.0),
            self._frame(12345))
        self.assertEqual(
            streamer._mix_network_frames(
                self._frame(12345), self._frame(-999), 0.0, 1.0),
            self._frame(-999))

    def test_single_source_falls_back_to_scaled_input(self):
        streamer = stream_mod.AudioStreamer.__new__(stream_mod.AudioStreamer)
        a = self._frame(20000)
        faded = streamer._mix_network_frames(a, None, 0.25, 0.75)
        self.assertEqual(struct.unpack("<h", faded[:2])[0], 5000)
        self.assertEqual(
            streamer._mix_network_frames(None, a, 0.0, 1.0), a)

    def test_uneven_lengths_blend_min_and_keep_partner_tail(self):
        streamer = stream_mod.AudioStreamer.__new__(stream_mod.AudioStreamer)
        out = streamer._mix_network_frames(
            self._frame(1000, 3840), self._frame(2000, 640), 0.5, 0.5)
        # The blend covers the overlap; the partner's extra tail is scaled in.
        self.assertEqual(len(out), 640)
        self.assertEqual(struct.unpack("<h", out[:2])[0], 1500)
        out2 = streamer._mix_network_frames(
            self._frame(1000, 640), self._frame(2000, 3840), 0.5, 0.5)
        self.assertEqual(len(out2), 3840)
        self.assertEqual(struct.unpack("<h", out2[:2])[0], 1500)
        self.assertEqual(
            struct.unpack("<h", out2[640:642])[0], 1000)  # partner tail scaled


class TestBroadcastMixSenderLoop(unittest.TestCase):
    """The network sender loop's crossfade mix branch (in-thread, no device)."""

    def _bare_streamer(self):
        s = stream_mod.AudioStreamer.__new__(stream_mod.AudioStreamer)
        s.running = True
        s.paused = False
        s.bot = None
        s.last_send_time = None
        s.network_queue = queue.Queue()
        s.network_muted = False
        s._network_mix = None
        s._timeline_lock = threading.Lock()
        s._timeline_epoch = 7
        s._timeline_next_seq = 0
        s._timeline_last_sent_seq = None
        s._timeline_delay = deque()
        return s

    @staticmethod
    def _frame(value):
        return struct.pack("<h", value) * 1920

    def test_mix_blends_both_streams_then_retires(self):
        old = self._bare_streamer()
        cand = self._bare_streamer()
        old.network_queue.put((self._frame(20000), None, None))
        old.network_queue.put((self._frame(20000), None, None))
        cand.network_queue.put((self._frame(-20000), None, None))
        cand.network_queue.put((self._frame(-20000), None, None))

        sent = []

        def capture(data, epoch, seq):
            sent.append((data, epoch, seq))

        old._send_to_network_actual = capture
        # monotonic: begin() consumes 0.0; loop iterations see 0.03 (30% in),
        # 0.08 (80% in), 0.11 (past the 0.1s window -> retire). perf_counter
        # paces each send without sleeping (mirrors the deadline-pacing test).
        with mock.patch.object(stream_mod.time, "monotonic",
                               side_effect=(0.0, 0.03, 0.08, 0.11)):
            old.begin_network_crossfade(cand, 0.1)
            with mock.patch.object(stream_mod.time, "perf_counter",
                                  side_effect=(1.000, 1.021, 1.041)), \
                    mock.patch.object(stream_mod.time, "sleep",
                                      side_effect=lambda s: setattr(old, "running", False)):
                old._network_sender_loop()

        self.assertEqual(len(sent), 2)
        first = sent[0]
        self.assertEqual(struct.unpack("<h", first[0][:2])[0], 8000)
        # 20000*0.7 + (-20000)*0.3 = 8000
        self.assertEqual(first[1], 7)  # the outgoing track owns the timeline
        self.assertEqual(first[2], 0)
        second = sent[1]
        # 20000*0.2 + (-20000)*0.8 = -12000
        self.assertEqual(struct.unpack("<h", second[0][:2])[0], -12000)
        self.assertEqual(second[2], 1)
        # The window elapsed: the outgoing leg retired itself and the partner
        # still owns its frames for the hand-over.
        self.assertTrue(old.network_muted)
        self.assertIsNone(old._network_mix)
        self.assertTrue(old.network_queue.empty())
        self.assertTrue(cand.network_queue.empty())

    def test_partner_frames_alone_keep_the_blend_alive(self):
        old = self._bare_streamer()
        cand = self._bare_streamer()
        # The outgoing song already ended: only the incoming stream feeds.
        cand.network_queue.put((self._frame(-20000), None, None))
        cand.network_queue.put((self._frame(-20000), None, None))

        sent = []
        old._send_to_network_actual = lambda data, e, s: sent.append(data)
        with mock.patch.object(stream_mod.time, "monotonic",
                               side_effect=(0.0, 0.03, 0.08, 0.11)):
            old.begin_network_crossfade(cand, 0.1)
            with mock.patch.object(stream_mod.time, "perf_counter",
                                  side_effect=(1.000, 1.021, 1.041)), \
                    mock.patch.object(stream_mod.time, "sleep",
                                      side_effect=lambda s: setattr(old, "running", False)):
                old._network_sender_loop()

        self.assertEqual(len(sent), 2)
        # First iteration: partner at 30% gain -> -6000.
        self.assertEqual(struct.unpack("<h", sent[0][:2])[0], -6000)
        # Second iteration: partner at 80% gain -> -16000.
        self.assertEqual(struct.unpack("<h", sent[1][:2])[0], -16000)
        self.assertTrue(old.network_muted)


if __name__ == "__main__":
    unittest.main()
