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

# The crossfade machine lives in crossfade_handler.py (extracted from the
# controller), and that is where its candidate streamer is built -- so the fake
# below is patched there, not on the module the machine used to live in.
from libs.music_bot import controller as controller_module
from libs.music_bot import crossfade_handler as crossfade_module
from libs.music_bot import menus as menus_module
from libs.music_bot import subtitles
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
                 join_playing_room=False, cinema=None, remote=None,
                 remote_slot=0, remote_category=1.0):
        self.game = game
        # The player's other device (music_bot/output_device.py); None is the
        # game's own, which is what this state machine is tested against.
        self.remote = remote
        self.remote_slot = remote_slot
        self.remote_category = remote_category
        self.output_gains = []
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

    def set_output_gain(self, gain):
        # The real one writes the gain to its own source (or to the child on
        # another device); the state machine must reach the source either way.
        self.output_gains.append(gain)
        if self.source is not None:
            self.source.gain = gain

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

    def _gen_effect(self, name, *params, hold=None):
        # ``hold`` is the pool's bookkeeping (which job the slot is for); the
        # real manager records it and the EQ path passes it through.
        eff = FakeEffect()
        eff.params = params
        eff.hold = hold
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


class TestTheCustomEqSlider(unittest.TestCase):
    """The Bass/Mid/Treble slider -- the one Music Bot menu item that is a State.

    It moved to music_bot/menus.py with the menus on 2026-10-18. The working copy
    of the three bands is the slider's own (`values`, `current_index`, `_closed`)
    and only the committed profile reaches the bot, so a mechanical
    `self.` -> `self._bot.` rewrite of the moved code (which turns those into the
    bot's attributes) breaks Custom EQ with nothing else in the suite opening it.
    """

    def _open(self, eq_values=None):
        from libs.music_bot import menus as menus_module

        bot = make_bot(eq_values=dict(eq_values or {
            "bass": 61, "mid": 42, "treble": 73}))
        audio = FakeAudio()
        gp = SimpleNamespace(substates=[])
        gp.add_substate = lambda substate: gp.substates.append(substate)
        gp.pop_last_substate = lambda: gp.substates.pop()
        bot.game = SimpleNamespace(audio_mngr=audio, keyconfig={})
        bot._find_gameplay = lambda: gp
        # The commit reopens the profile menu; that menu is pinned elsewhere.
        bot._open_eq_menu = lambda: None
        persisting = mock.patch("libs.music_bot.controller.options.set")
        persisting.start()
        self.addCleanup(persisting.stop)
        with mock.patch.object(menus_module, "speak"):
            bot._open_custom_eq_sliders()
        return bot, gp, menus_module, audio

    def _press(self, menus_module, key, mod=0):
        return SimpleNamespace(type=menus_module.pygame.KEYDOWN, key=key, mod=mod)

    def test_the_slider_holds_its_own_three_bands(self):
        bot, gp, menus_module, _audio = self._open()
        slider = gp.substates[-1]
        self.assertIsInstance(slider, menus_module._MusicBotEqSlider)
        self.assertEqual(slider.values,
                         {"bass": 61, "mid": 42, "treble": 73})
        self.assertEqual(slider.current_index, 0)
        self.assertFalse(hasattr(slider, "_bot"))
        # The bot keeps its own copy, untouched until Enter.
        self.assertEqual(bot.eq_values,
                         {"bass": 61, "mid": 42, "treble": 73})

    def test_a_band_moves_on_the_slider_and_only_enter_closes_it(self):
        bot, gp, menus_module, audio = self._open()
        slider = gp.substates[-1]
        applied = len(audio.sends)
        with mock.patch.object(menus_module, "speak"):
            slider.enter()
            slider.update([self._press(menus_module, menus_module.pygame.K_TAB)])
            slider.update([self._press(menus_module, menus_module.pygame.K_UP)])
            self.assertEqual(slider.current_index, 1)
            self.assertEqual(slider.values["mid"], 43)
            # A tick is heard at once (the custom slot is mutated in place),
            # while the slider is still open.
            self.assertEqual(bot.eq_profile, "custom")
            self.assertEqual(bot.eq_values["mid"], 43)
            self.assertFalse(slider._closed)
            self.assertGreater(len(audio.sends), applied)
            applied = len(audio.sends)
            slider.update([self._press(menus_module,
                                       menus_module.pygame.K_RETURN)])
        self.assertTrue(slider._closed)
        self.assertEqual(bot.eq_values["mid"], 43)
        self.assertEqual(gp.substates, [])
        self.assertGreater(len(audio.sends), applied)


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



class TheCustomHalvesTests(unittest.TestCase):
    """Crossfade Fade In / Fade Out: the two halves, kept apart.

    A blend used to be one window, symmetric by construction. The page lets a
    player set the incoming half and the outgoing half separately -- a long tail
    under a quick entrance, say -- and the overlap is whichever half is longer.
    Equal halves are exactly the old window, which is what the defaults are.
    """

    def _bot(self, fades=None):
        bot = make_bot()
        store = dict(fades or {})
        written = []
        def set_value(key, value, *args, **kwargs):
            store[key] = value
            written.append((key, value))

        stub = SimpleNamespace(
            get=lambda key, default=None: store.get(key, default),
            set=set_value,
        )
        patcher = mock.patch.object(controller_module, "options", stub)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.store = store
        self.written = written
        return bot

    def test_both_halves_default_to_the_single_window_this_had(self):
        bot = self._bot()
        self.assertEqual(bot.crossfade_fade_in(), MapMusicBot.CROSSFADE_SECONDS)
        self.assertEqual(bot.crossfade_fade_out(), MapMusicBot.CROSSFADE_SECONDS)
        self.assertEqual(bot.crossfade_overlap_seconds(),
                         MapMusicBot.CROSSFADE_SECONDS)
        self.assertEqual(bot.crossfade_fade_label("in"),
                         "Crossfade Fade In: 3 seconds")
        self.assertEqual(bot.crossfade_fade_label("out"),
                         "Crossfade Fade Out: 3 seconds")

    def test_each_half_is_kept_on_its_own_option(self):
        bot = self._bot()
        bot.set_crossfade_fade("in", 1)
        bot.set_crossfade_fade("out", 5.5)
        self.assertEqual(bot.crossfade_fade_in(), 1.0)
        self.assertEqual(bot.crossfade_fade_out(), 5.5)
        self.assertEqual(bot.crossfade_overlap_seconds(), 5.5)
        self.assertEqual(bot.crossfade_fade_label("in"),
                         "Crossfade Fade In: 1 second")
        self.assertEqual(bot.crossfade_fade_label("out"),
                         "Crossfade Fade Out: 5.5 seconds")
        self.assertIn(("music_bot_crossfade_fade_in", 1.0), self.written)
        self.assertIn(("music_bot_crossfade_fade_out", 5.5), self.written)

    def test_a_value_out_of_range_or_garbage_is_clamped_not_stored(self):
        bot = self._bot()
        self.assertEqual(bot.set_crossfade_fade("out", 999),
                         MapMusicBot.MAX_CROSSFADE_FADE_SECONDS)
        self.assertEqual(bot.set_crossfade_fade("in", -4), 0.0)
        self.assertEqual(bot.set_crossfade_fade("in", "not a number"), 0.0)
        self.assertEqual(bot.set_crossfade_fade("in", None), 0.0)

    def test_a_stored_value_this_build_cannot_read_reads_as_the_default(self):
        bot = self._bot({"music_bot_crossfade_fade_in": "half"})
        self.assertEqual(bot.crossfade_fade_in(), MapMusicBot.CROSSFADE_SECONDS)
        bot = self._bot({"music_bot_crossfade_fade_out": 900})
        self.assertEqual(bot.crossfade_fade_out(),
                         MapMusicBot.MAX_CROSSFADE_FADE_SECONDS)

    def test_restore_puts_both_halves_back_to_the_shipped_blend(self):
        bot = self._bot({"music_bot_crossfade_fade_in": 1.0,
                         "music_bot_crossfade_fade_out": 8.0})
        self.assertEqual(bot.restore_crossfade_defaults(),
                         (MapMusicBot.CROSSFADE_SECONDS,
                          MapMusicBot.CROSSFADE_SECONDS))
        self.assertEqual(bot.crossfade_fade_in(), MapMusicBot.CROSSFADE_SECONDS)
        self.assertEqual(bot.crossfade_fade_out(), MapMusicBot.CROSSFADE_SECONDS)

    def test_the_unknown_half_is_refused_rather_than_stored(self):
        bot = self._bot()
        with self.assertRaises(ValueError):
            bot.set_crossfade_fade("sideways", 2)


class TestTheCustomWindowInTheMachine(unittest.TestCase):
    """The machine reads the two halves: overlap, commit point and both ramps."""

    def _bot(self, fade_in, fade_out, position=5.0, duration=10.0):
        bot = SimpleTestCase_bot(position=position, duration=duration)
        store = {
            "music_bot_crossfade_fade_in": fade_in,
            "music_bot_crossfade_fade_out": fade_out,
        }
        stub = SimpleNamespace(
            get=lambda key, default=None: store.get(key, default),
            set=lambda key, value, *args, **kwargs: None,
        )
        patcher = mock.patch.object(controller_module, "options", stub)
        patcher.start()
        self.addCleanup(patcher.stop)
        return bot

    def test_both_halves_off_starts_no_roll_at_all(self):
        bot = self._bot(0.0, 0.0)
        bot.next_up_queue = [{"title": "Next", "target": "C:\\next.mp3",
                              "source": "local"}]
        with mock.patch.object(crossfade_module, "AudioStreamer", FakeStreamer):
            bot._update_crossfade()
        self.assertIsNone(bot._crossfade)

    def test_the_longer_half_decides_when_the_blend_begins(self):
        # Fade In 1, Fade Out 4: both songs are together for four seconds, and
        # the incoming one is already at full level after the first of them.
        bot = self._bot(1.0, 4.0)
        bot.next_up_queue = [{"title": "Next Song",
                              "target": "C:\\next.mp3", "source": "local"}]
        old_streamer = bot.streamer
        with mock.patch.object(crossfade_module, "AudioStreamer",
                               FakeStreamer) as streamer_cls:
            bot._update_crossfade()
            self.assertIsNotNone(bot._crossfade)
            self.assertEqual(bot._crossfade["overlap"], 4.0)
            # Remaining 4.5: the candidate is launched (inside overlap + launch)
            # but the hand-over has not happened yet.
            bot._position[0] = 5.5
            bot._update_crossfade()
            cand = bot._crossfade["candidate"]
            self.assertIsNotNone(cand)
            self.assertEqual(bot._crossfade["phase"], "waiting")
            cand.prebuffer_event.set()
            # Remaining 4.5 is still outside the window by half a second.
            bot._update_crossfade()
            self.assertEqual(bot._crossfade["phase"], "waiting")
            # Remaining 4.0: the blend starts.
            bot._position[0] = 6.0
            bot._update_crossfade()
        self.assertEqual(bot._crossfade["phase"], "fading")
        self.assertEqual(bot._crossfade["fade_in"], 1.0)
        self.assertEqual(bot._crossfade["fade_out"], 4.0)
        self.assertEqual(bot._crossfade["overlap"], 4.0)
        # The room gets the same window the performer hears.
        self.assertEqual(old_streamer.crossfade_mix[1], 4.0)

    def test_each_half_ramps_over_its_own_length(self):
        bot = self._bot(1.0, 4.0, position=6.0)
        bot.next_up_queue = [{"title": "Next Song",
                              "target": "C:\\next.mp3", "source": "local"}]
        old_streamer = bot.streamer
        with mock.patch.object(crossfade_module, "AudioStreamer", FakeStreamer):
            bot._update_crossfade()
            cand = bot._crossfade["candidate"]
            cand.prebuffer_event.set()
            bot._update_crossfade()
            self.assertEqual(bot._crossfade["phase"], "fading")
            # One second in: the incoming half is done, the outgoing one is a
            # quarter of the way down.
            bot._crossfade["fade_started_at"] = time.monotonic() - 1.0
            bot._update_fade_gains(0.8)
            self.assertAlmostEqual(bot.stream_source.gain, 0.8, places=6)
            self.assertAlmostEqual(old_streamer.output_gains[-1], 0.5 * 0.75, places=6)
            self.assertIsNotNone(bot._crossfade)
            # Three seconds in: still fading, and only the outgoing side moves.
            bot._crossfade["fade_started_at"] = time.monotonic() - 3.0
            bot._update_fade_gains(0.8)
            self.assertAlmostEqual(bot.stream_source.gain, 0.8, places=6)
            self.assertAlmostEqual(old_streamer.output_gains[-1], 0.5 * 0.25, places=6)
            self.assertIsNotNone(bot._crossfade)
            # Past the window: the blend is over and the old stream is stopped.
            bot._crossfade["fade_started_at"] = time.monotonic() - 4.5
            bot._update_fade_gains(0.8)
            self.assertIsNone(bot._crossfade)
            self.assertTrue(old_streamer.stopped)

    def test_a_change_the_player_asks_for_uses_the_same_two_halves(self):
        bot = self._bot(2.0, 5.0)
        window = bot._begin_track_change_fade()
        self.assertEqual(window, 5.0)
        self.assertEqual(bot._incoming_fade["seconds"], 2.0)


def SimpleTestCase_bot(position=5.0, duration=10.0):
    """A playing bot with a known duration, as the machine tests build one."""
    bot = make_bot(playing=True, paused=False, is_loading_stream=False)
    bot.crossfade_enabled = True
    bot.current_duration = duration
    bot.streamer = FakeStreamer(None, "u", bot.stream_source, bot=bot)
    pos = [position]
    bot.track_position = lambda: pos[0]
    bot._position = pos
    bot._new_bot_source = lambda: FakeSrc()
    return bot


class FakeMenu:
    """The menu builder's own record of the page it would have shown."""

    built = []

    def __init__(self, game, title, **kwargs):
        self.game = game
        self.title = title
        self.items = []
        self.pos = -1
        FakeMenu.built.append(self)

    def add_items(self, items):
        self.items.extend(items)

    def speak_current_item(self):
        return None


class TheCrossfadeFadePagesTests(unittest.TestCase):
    """The Settings lines, the two fade pages and the restore confirmation."""

    def _page_bot(self):
        bot = make_bot()
        store = {"music_bot_crossfade_fade_in": 3.0,
                 "music_bot_crossfade_fade_out": 3.0}
        written = []

        def get(key, default=None):
            return store.get(key, default)

        def set_value(key, value, *args, **kwargs):
            store[key] = value
            written.append((key, value))

        stub = SimpleNamespace(get=get, set=set_value)
        patcher = mock.patch.object(controller_module, "options", stub)
        patcher.start()
        self.addCleanup(patcher.stop)
        bot.game = FakeMenuGame()
        self.store = store
        self.written = written
        return bot

    def _press(self, bot, action):
        """Press a line whose action reopens the Settings page it came from."""
        with mock.patch("libs.menu.Menu", FakeMenu),                 mock.patch("libs.menus.set_default_sounds"),                 mock.patch.object(menus_module, "speak"):
            action()

    def _open(self, bot, method, *args):
        built = []
        gp = SimpleNamespace(substates=built, add_substate=built.append,
                             pop_last_substate=lambda: built.pop())
        bot._find_gameplay = lambda: gp
        handler = menus_module.MusicBotMenus(bot)
        with mock.patch("libs.menu.Menu", FakeMenu), \
                mock.patch("libs.menus.set_default_sounds"), \
                mock.patch.object(menus_module, "speak") as spoke:
            getattr(handler, method)(*args)
        return built[-1], spoke

    def _labels(self, menu):
        return [label() if callable(label) else label for label, *_rest in menu.items]

    def test_settings_offers_both_halves_while_the_switch_is_on(self):
        bot = self._page_bot()
        bot.crossfade_enabled = True
        menu, _spoke = self._open(bot, "_open_settings_menu")
        labels = self._labels(menu)
        self.assertIn("Crossfade Fade In: 3 seconds", labels)
        self.assertIn("Crossfade Fade Out: 3 seconds", labels)
        self.assertIn("Restore Crossfade Defaults", labels)
        # Every one of the three carries its own description for Tab.
        def resolved(item):
            label = item[0]
            return label() if callable(label) else label
        described = [item for item in menu.items
                     if isinstance(resolved(item), str)
                     and resolved(item).startswith(
                         ("Crossfade Fade", "Restore Crossfade"))]
        self.assertEqual(len(described), 3)
        for item in described:
            self.assertTrue(len(item) >= 4 and item[3], item)

    def test_both_fade_lines_are_hidden_while_the_switch_is_off(self):
        bot = self._page_bot()
        bot.crossfade_enabled = False
        menu, _spoke = self._open(bot, "_open_settings_menu")
        labels = self._labels(menu)
        self.assertNotIn("Crossfade Fade In: 3 seconds", labels)
        self.assertNotIn("Crossfade Fade Out: 3 seconds", labels)
        # The switch itself and the way back to the defaults stay in reach: a
        # player who turned it off must not need to turn it on to undo it.
        self.assertIn("Crossfade Between Songs: OFF", labels)
        self.assertIn("Restore Crossfade Defaults", labels)

    def test_the_switch_brings_the_two_lines_back_and_keeps_their_values(self):
        bot = self._page_bot()
        bot.crossfade_enabled = False
        self.store["music_bot_crossfade_fade_in"] = 5.0
        self.store["music_bot_crossfade_fade_out"] = 5.0
        menu, _spoke = self._open(bot, "_open_settings_menu")
        toggle = [item for item in menu.items
                  if isinstance(self._labels(menu)[menu.items.index(item)], str)
                  and self._labels(menu)[menu.items.index(item)].startswith(
                      "Crossfade Between Songs:")][0]
        self._press(bot, toggle[1])
        self.assertTrue(bot.crossfade_enabled)
        rebuilt = FakeMenu.built[-1]
        labels = self._labels(rebuilt)
        self.assertIn("Crossfade Fade In: 5 seconds", labels)
        self.assertIn("Crossfade Fade Out: 5 seconds", labels)
        # The cursor is put back on the switch that was just pressed.
        self.assertTrue(labels[rebuilt.pos].startswith("Crossfade Between Songs: ON"))

    def test_the_fade_page_marks_the_time_in_force_and_offers_any_value(self):
        bot = self._page_bot()
        self.store["music_bot_crossfade_fade_in"] = 2.0
        menu, _spoke = self._open(bot, "_open_crossfade_fade_menu", "in")
        self.assertEqual(menu.title, "Crossfade Fade In: 2 seconds")
        labels = [label() if callable(label) else label
                  for label, *_rest in menu.items]
        self.assertIn("3 seconds", labels)
        self.assertIn("2 seconds (active)", labels)
        self.assertIn("Custom Seconds", labels)
        self.assertIn("Back", labels)
        for item in menu.items:
            self.assertTrue(len(item) >= 4 and item[3], item)

    def test_picking_a_time_sets_that_half_only(self):
        bot = self._page_bot()
        menu, _spoke = self._open(bot, "_open_crossfade_fade_menu", "out")
        items = [item for item in menu.items
                 if callable(item[0]) and item[0]() == "5 seconds"]
        self.assertEqual(len(items), 1)
        self._press(bot, items[0][1])
        self.assertEqual(self.store["music_bot_crossfade_fade_out"], 5.0)
        self.assertEqual(self.store["music_bot_crossfade_fade_in"], 3.0)

    def test_the_restore_page_puts_both_halves_back(self):
        bot = self._page_bot()
        self.store["music_bot_crossfade_fade_in"] = 1.0
        self.store["music_bot_crossfade_fade_out"] = 8.0
        menu, _spoke = self._open(bot, "_open_crossfade_reset_menu")
        self.assertIn("Restore", menu.title)
        self._press(bot, menu.items[0][1])
        self.assertEqual(self.store["music_bot_crossfade_fade_in"], 3.0)
        self.assertEqual(self.store["music_bot_crossfade_fade_out"], 3.0)

    def test_cancel_leaves_both_halves_alone(self):
        bot = self._page_bot()
        self.store["music_bot_crossfade_fade_out"] = 8.0
        menu, _spoke = self._open(bot, "_open_crossfade_reset_menu")
        self._press(bot, menu.items[1][1])
        self.assertEqual(self.store["music_bot_crossfade_fade_out"], 8.0)


class FakeMenuGame:
    """Enough of a game for the fade page: its open sound and its input field."""

    def __init__(self):
        self.soundgroup = SimpleNamespace(play=lambda *a, **k: None)
        self.input = SimpleNamespace(
            run=lambda prompt, **kwargs: SimpleNamespace(ck=lambda: False))




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
        with mock.patch.object(crossfade_module, "AudioStreamer",
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

    def test_a_real_crossfade_does_not_ramp_the_new_song_twice(self):
        """A skip's pending fade-in is dropped when the machine fades instead.

        Both halves would touch the same source gain: the machine ramps both
        streams against each other, and a fade-in left over from a manual
        change would ramp the incoming one a second time.
        """
        bot = self._playing_bot(position=4.0, duration=10.0)
        bot.next_up_queue = [{"title": "Next", "target": "C:\\next.mp3",
                              "source": "local"}]
        bot._incoming_fade = {"seconds": 3.0, "at": None}
        with mock.patch.object(crossfade_module, "AudioStreamer", FakeStreamer):
            bot._update_crossfade()
            bot._position[0] = 7.5
            bot._update_crossfade()
            bot._crossfade["candidate"].prebuffer_event.set()
            bot._position[0] = 8.0
            bot._update_crossfade()
        self.assertEqual(bot._crossfade["phase"], "fading")
        self.assertIsNone(bot._incoming_fade)

    def test_cancel_tears_down_candidate(self):
        bot = self._playing_bot(position=4.0, duration=10.0)
        bot.next_up_queue = [{"title": "Next", "target": "C:\\next.mp3",
                              "source": "local"}]
        with mock.patch.object(crossfade_module, "AudioStreamer", FakeStreamer):
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
        with mock.patch.object(crossfade_module, "AudioStreamer", FakeStreamer):
            bot._update_crossfade()
            bot.streamer.alive = False  # current song died mid-roll
            bot._update_crossfade()
        self.assertIsNone(bot._crossfade)
        # The queue was never consumed: the normal advance still owns it.
        self.assertEqual(len(bot.next_up_queue), 1)


class AManualTrackChangeFadesLikeTheQueue(unittest.TestCase):
    """Ctrl+[ / ], the queue's own lines and a local batch fade too.

    Crossfade Between Songs used to own only the end-of-song crossfade, so a
    skip cut the old song off in half a second and the new one arrived at full
    gain. A change the player asks for now takes the same window: the outgoing
    song ramps down while the incoming one ramps up underneath it, and a local
    file (which needs no resolve) is a real overlap.
    """

    def playing_bot(self, **overrides):
        bot = make_bot(**overrides)
        bot.game = SimpleNamespace(
            put=lambda fn: None,
            audio_mngr=SimpleNamespace(volume_categories={"music": [100]}),
        )
        bot.stream_source = FakeSrc(gain=0.5)
        bot.streamer = FakeStreamer(SimpleNamespace(), "http://example.com/a.mp3",
                                    bot.stream_source)
        bot._incoming_fade = None
        bot.cinema_target = None
        bot._ensure_live_relay_streamer = lambda: None
        bot._update_crossfade = lambda: None
        bot._pump_subtitles = lambda: None
        bot.announce_party_queue = lambda: None
        return bot

    def test_a_skip_takes_the_crossfade_window(self):
        bot = self.playing_bot(crossfade_enabled=True)
        self.assertEqual(bot._begin_track_change_fade(), bot.CROSSFADE_SECONDS)
        self.assertEqual(bot._incoming_fade["seconds"], bot.CROSSFADE_SECONDS)
        # Armed, not started: a YouTube change spends the resolve in between.
        self.assertIsNone(bot._incoming_fade["at"])

    def test_crossfade_off_keeps_the_half_second_cut(self):
        bot = self.playing_bot(crossfade_enabled=False)
        self.assertEqual(bot._begin_track_change_fade(), 0.5)
        self.assertIsNone(bot._incoming_fade)

    def test_a_room_is_not_crossfaded(self):
        """A cabinet's room plays one stream at a time, so nothing to overlap."""
        bot = self.playing_bot(crossfade_enabled=True)
        bot.cinema_target = "j1"
        bot._find_gameplay = lambda: SimpleNamespace(can_use_cinema_speakers=True)
        self.assertEqual(bot._begin_track_change_fade(), 0.5)
        self.assertIsNone(bot._incoming_fade)

    def test_nothing_playing_has_nothing_to_fade(self):
        bot = self.playing_bot(crossfade_enabled=True)
        bot.playing = False
        self.assertEqual(bot._begin_track_change_fade(), 0.5)
        self.assertIsNone(bot._incoming_fade)

    def test_stop_fades_the_old_song_over_that_window(self):
        bot = self.playing_bot(crossfade_enabled=True)
        windows = []
        bot._fade_out_source = (
            lambda src, streamer=None, duration=0.5: windows.append(duration))
        bot.stop(clear_queue=False, fade=True, invalidate_pending=False)
        self.assertEqual(windows, [bot.CROSSFADE_SECONDS])

    def test_a_plain_stop_arms_nothing(self):
        bot = self.playing_bot(crossfade_enabled=True)
        bot._incoming_fade = {"seconds": 3.0, "at": None}
        bot._fade_out_source = lambda *a, **k: None
        bot.stop(clear_queue=False, invalidate_pending=False)
        self.assertIsNone(bot._incoming_fade)

    def test_the_new_song_ramps_up_and_then_plays_at_full_gain(self):
        """The fade-in starts with the new stream's first audible frame."""
        bot = self.playing_bot(crossfade_enabled=True)
        bot._begin_track_change_fade()
        source = bot.stream_source
        clock = [100.0]
        with mock.patch("libs.music_bot.controller.time.monotonic",
                        lambda: clock[0]):
            bot.loop()
            self.assertAlmostEqual(source.gain, 0.0, places=6)
            clock[0] += bot.CROSSFADE_SECONDS / 2.0
            bot.loop()
            # Half the window in: half of the base gain (50% volume).
            self.assertAlmostEqual(source.gain, 0.25, places=3)
            clock[0] += bot.CROSSFADE_SECONDS
            bot.loop()
            self.assertAlmostEqual(source.gain, 0.5, places=6)
        self.assertIsNone(bot._incoming_fade)

    def test_a_local_file_change_fades_too(self):
        """Choose Local File and a local batch take the same path as a skip."""
        bot = self.playing_bot(crossfade_enabled=True)
        path = os.path.abspath(os.path.join(
            os.path.dirname(__file__), "..", "libs", "music_bot", "controller.py"))
        calls = []
        bot.stop = lambda **kwargs: calls.append(kwargs)
        with mock.patch.object(MapMusicBot, "_start_youtube_stream",
                               lambda *a, **k: None), \
                mock.patch("libs.music_bot.track_source.speak"):
            bot._start_local_file_stream(path, "Track")
        self.assertTrue(calls and calls[0].get("fade") is True)


class TestWideLiveInputMix(unittest.TestCase):
    """Live music + guitar/mic frames are summed before the one limiter."""

    @staticmethod
    def _mono_frame(value, samples=960):
        return struct.pack("<h", value) * samples

    def test_wide_mix_limits_without_intermediate_int16_saturation(self):
        frame = self._mono_frame(20000)
        mixed = stream_mod.voice_chat.mix_audio_frames(
            [(frame, 0.85), (frame, 0.85)],
            state_key=None,
        )
        samples = struct.unpack("<960h", mixed)
        self.assertEqual(len(mixed), len(frame))
        self.assertLess(max(abs(value) for value in samples), 32767)
        # Two 20k sources would have saturated at 32767 if audioop.add ran
        # before the limiter. The true wide sum is limited below that ceiling.
        self.assertGreater(samples[0], 27000)
        self.assertLess(samples[0], 30000)

    def test_guitar_encoder_queue_stays_bounded_and_keeps_the_latest_frames(self):
        encoder = stream_mod.voice_chat.voice_chat_compression.__new__(
            stream_mod.voice_chat.voice_chat_compression)
        encoder.queue = queue.Queue(maxsize=2)
        encoder._max_pending_frames = 2
        encoder.dropped_frames = 0
        encoder.running = True
        encoder.put(b"old")
        encoder.put(b"middle")
        encoder.put(b"new")
        self.assertEqual(encoder.queue.qsize(), 2)
        self.assertEqual(encoder.dropped_frames, 1)
        self.assertEqual(encoder.queue.get_nowait(), b"middle")
        self.assertEqual(encoder.queue.get_nowait(), b"new")
        encoder.close()
        self.assertIsNone(encoder.queue.get_nowait())


class TestUploadGate(unittest.TestCase):
    """Who may upload: the sender's own switches, never the listener's.

    A regression pin for the cinema-era edit that ORed the *listener* switches
    (`Instruments:`, `Cinema rooms:`) into `_send_to_network_actual`'s gate.
    Those are on by default and say how this client HEARS a cabinet, so reading
    one of them here turned every private Music Bot into a map-wide broadcast.
    """

    @staticmethod
    def _streamer(sent, **bot_flags):
        streamer = stream_mod.AudioStreamer.__new__(stream_mod.AudioStreamer)
        streamer.game = SimpleNamespace(
            network=SimpleNamespace(
                send=lambda *args, **kwargs: sent.append(args)),
        )
        flags = {
            "broadcast_enabled": False,
            "broadcast_to_megaphone": False,
            "duck_multiplier": 1.0,
            "mic_pcm_queue": None,
            "guitar_pcm_queue": None,
        }
        flags.update(bot_flags)
        streamer.bot = SimpleNamespace(**flags)
        streamer.encoder = SimpleNamespace(encode=lambda _pcm: b"mono-opus")
        # A bot-backed streamer always has the stereo encoder too (streaming.AudioStreamer
        # builds both), and a party/cinema leg picks it.
        streamer.encoder_stereo = SimpleNamespace(encode=lambda _pcm: b"stereo-opus")
        streamer.volume = 100
        return streamer

    def test_a_listener_switch_never_makes_a_private_bot_upload(self):
        sent = []
        streamer = self._streamer(
            sent,
            instruments_cinema_active=True,
            instrument_cinema_active=True,
            cinema_speakers=True,
            cinema_force_upload=False,
        )
        streamer._send_to_network_actual(b"\x00" * 3840)
        self.assertEqual(sent, [])

    def test_the_sender_own_switches_still_upload(self):
        for flags in ({"broadcast_enabled": True},
                      {"broadcast_to_megaphone": True},
                      {"cinema_force_upload": True}):
            sent = []
            streamer = self._streamer(sent, **flags)
            streamer._send_to_network_actual(b"\x00" * 3840)
            self.assertEqual(len(sent), 1, flags)

    def test_a_streamer_without_a_stereo_encoder_still_uploads_mono(self):
        """The stereo branch must never go silent on a streamer that lacks one.

        The frame is still owed to the room; falling through to the mono
        encoder keeps the song playing (one centre, as before this change)
        instead of dropping every frame of it.
        """
        sent = []
        streamer = self._streamer(sent, cinema_force_upload=True)
        del streamer.encoder_stereo
        streamer._send_to_network_actual(b"\x00" * 3840)
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][2], b"mono-opus")


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
