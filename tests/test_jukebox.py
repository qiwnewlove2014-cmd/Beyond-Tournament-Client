"""Unit tests for the client-side jukebox module (menu state helpers)."""

import sys
import os
import unittest
import struct
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from libs import jukebox
from libs.music_bot import streaming as mb_stream
from libs.music_bot import controller as mb_ctrl


class FakePlayer:
    def __init__(self, x, y, z, name="me"):
        self.x = x
        self.y = y
        self.z = z
        self.name = name


class FakeGameplay:
    def __init__(self, state=None):
        self.jukebox_state = state
        self.player = FakePlayer(0, 0, 0)
        self.pop_count = 0

    def pop_last_substate(self):
        self.pop_count += 1


class TestClosestJukebox(unittest.TestCase):
    def test_no_state_returns_none(self):
        gp = FakeGameplay()
        self.assertIsNone(jukebox._closest_jukebox(gp))

    def test_empty_jukeboxes_returns_none(self):
        gp = FakeGameplay({"jukeboxes": {}})
        self.assertIsNone(jukebox._closest_jukebox(gp))

    def test_picks_nearest(self):
        state = {
            "jukeboxes": {
                "far": {"id": "far", "x": 100, "y": 100, "z": 0},
                "near": {"id": "near", "x": 3, "y": 4, "z": 0},
            }
        }
        gp = FakeGameplay(state)
        jb = jukebox._closest_jukebox(gp)
        self.assertEqual(jb.get("id"), "near")

    def test_uses_player_position(self):
        state = {
            "jukeboxes": {
                "a": {"id": "a", "x": 10, "y": 0, "z": 0},
                "b": {"id": "b", "x": 5, "y": 0, "z": 0},
            }
        }
        gp = FakeGameplay(state)
        gp.player.x = 5
        jb = jukebox._closest_jukebox(gp)
        self.assertEqual(jb.get("id"), "b")


class TestCurrentState(unittest.TestCase):
    def test_missing_state_defaults(self):
        gp = FakeGameplay()
        self.assertEqual(jukebox._current_state(gp), {"jukeboxes": {}})

    def test_passes_through_valid_state(self):
        state = {"jukeboxes": {"x": {"id": "x"}}}
        gp = FakeGameplay(state)
        self.assertIs(jukebox._current_state(gp), state)

    def test_non_dict_state_defaults(self):
        gp = FakeGameplay("garbage")
        self.assertEqual(jukebox._current_state(gp), {"jukeboxes": {}})

    def test_pause_label_follows_cached_server_state(self):
        gp = FakeGameplay({"jukeboxes": {"box": {"id": "box", "paused": False}}})
        self.assertEqual(jukebox._pause_menu_label(gp, "box"), "Pause playback")
        gp.jukebox_state["jukeboxes"]["box"]["paused"] = True
        self.assertEqual(jukebox._pause_menu_label(gp, "box"), "Resume playback")

    def test_pause_button_sends_authoritative_toggle(self):
        game = FakeGameNetwork()
        gp = FakeGameplay({"jukeboxes": {"box": {"id": "box", "paused": True}}})
        jukebox._toggle_pause(game, gp, "box")
        args, _ = game.network.sent[-1]
        self.assertEqual(args[1], "jukebox_toggle_pause")
        self.assertEqual(args[2], {"id": "box"})


class _FakeJukeboxAudioPlayer:
    def __init__(self):
        self.eq_calls = []
        self.volume_calls = []

    def set_eq_profile(self, jukebox_id, profile, values=None):
        self.eq_calls.append((jukebox_id, profile, values))

    def set_cabinet_volume(self, jukebox_id, volume):
        self.volume_calls.append((jukebox_id, volume))


class TestAccessibleJukeboxSliders(unittest.TestCase):
    def _event(self, key, mod=0):
        return SimpleNamespace(type=jukebox.pygame.KEYDOWN, key=key, mod=mod)

    def _make(self, box):
        gp = FakeGameplay({"jukeboxes": {"box-1": {"id": "box-1", **box}}})
        gp.jukebox_player = _FakeJukeboxAudioPlayer()
        game = FakeGameNetwork()
        return game, gp

    def test_eq_slider_starts_from_current_values_and_tabs_between_bands(self):
        from unittest import mock

        game, gp = self._make({
            "eq_profile": "custom",
            "eq_values": {"bass": 61, "mid": 42, "treble": 73},
        })
        slider = jukebox._JukeboxEqSlider(game, gp, "box-1")
        self.assertEqual(slider.values, {"bass": 61, "mid": 42, "treble": 73})

        with mock.patch("libs.jukebox.speak") as spoken:
            slider.enter()
            slider.update([self._event(jukebox.pygame.K_TAB)])
        self.assertEqual(slider.current_index, 1)
        self.assertIn("Mid. Slider: 42%", [call.args[0] for call in spoken.call_args_list])

    def test_eq_slider_previews_locally_and_commits_final_values(self):
        from unittest import mock

        game, gp = self._make({"eq_profile": "normal"})
        slider = jukebox._JukeboxEqSlider(game, gp, "box-1")
        with mock.patch("libs.jukebox.speak"), \
                mock.patch("libs.jukebox.open_jukebox_menu"):
            slider.update([self._event(jukebox.pygame.K_UP)])
            slider.update([self._event(jukebox.pygame.K_RETURN)])

        self.assertEqual(gp.jukebox_player.eq_calls[0], (
            "box-1", "custom", {"bass": 51, "mid": 50, "treble": 50},
        ))
        packets = [args[2] for args, _ in game.network.sent]
        self.assertTrue(packets[0]["preview"])
        self.assertTrue(packets[-1]["commit"])
        self.assertEqual(packets[-1]["eq_values"]["bass"], 51)

    def test_volume_slider_focuses_cached_value_previews_and_commits(self):
        from unittest import mock

        game, gp = self._make({"volume": 73})
        slider = jukebox._JukeboxVolumeSlider(game, gp, "box-1")
        self.assertEqual(slider.value, 73)

        with mock.patch("libs.jukebox.speak") as spoken, \
                mock.patch("libs.jukebox.open_jukebox_menu"):
            slider.enter()
            slider.update([self._event(jukebox.pygame.K_DOWN)])
            slider.update([self._event(jukebox.pygame.K_RETURN)])

        self.assertIn("Volume. Slider: 73%", [call.args[0] for call in spoken.call_args_list])
        self.assertEqual(gp.jukebox_player.volume_calls[0], ("box-1", 72))
        packets = [args[2] for args, _ in game.network.sent]
        self.assertEqual(packets[0]["volume"], 72)
        self.assertTrue(packets[0]["preview"])
        self.assertTrue(packets[-1]["commit"])

    def test_eq_gain_curve_is_flat_at_50_and_bounded_at_extremes(self):
        params = dict(jukebox.JukeboxPlayer._custom_eq_parameters({
            "bass": 50, "mid": 0, "treble": 100,
        }))
        self.assertAlmostEqual(params["low_gain"], 1.0)
        self.assertGreaterEqual(params["mid1_gain"], 0.126)
        self.assertLessEqual(params["high_gain"], 7.943)

    def test_custom_eq_updates_one_effect_slot_in_place(self):
        class Effect:
            def __init__(self):
                self.updates = []

            def set(self, *param):
                self.updates.append(param)

        class Slot:
            def __init__(self):
                self.effect = Effect()

        class Audio:
            def __init__(self):
                self.efx = object()
                self.created = []

            def gen_effect(self, *_args):
                slot = Slot()
                self.created.append(slot)
                return slot

        game = SimpleNamespace(audio_mngr=Audio())
        player = jukebox.JukeboxPlayer(game)
        player.set_eq_profile("box-1", "custom", {
            "bass": 50, "mid": 50, "treble": 50,
        })
        player.set_eq_profile("box-1", "custom", {
            "bass": 60, "mid": 40, "treble": 70,
        })
        self.assertEqual(len(game.audio_mngr.created), 1)
        self.assertEqual(len(game.audio_mngr.created[0].effect.updates), 10)

class FakeNetwork:
    def __init__(self):
        self.sent = []

    def send(self, *args, **kwargs):
        self.sent.append((args, kwargs))


class FakeGameNetwork:
    def __init__(self):
        self.network = FakeNetwork()


class TestStableQueuedUrl(unittest.TestCase):
    def test_search_result_queues_canonical_webpage_url(self):
        """A returning listener must never receive an expired googlevideo URL."""
        from unittest import mock

        game = FakeGameNetwork()
        gp = FakeGameplay()
        result = {
            "title": "Persistent song",
            "duration": 180,
            "url": "https://rr.example.googlevideo.com/expired-signed-stream",
            "webpage_url": "https://www.youtube.com/watch?v=stable123",
        }
        with mock.patch("libs.jukebox.speak"):
            jukebox._pick_song(game, gp, "box-1", result)

        self.assertEqual(gp.pop_count, 1)
        args, _ = game.network.sent[-1]
        self.assertEqual(args[1], "jukebox_queue_add")
        self.assertEqual(
            args[2]["url"],
            "https://www.youtube.com/watch?v=stable123",
        )


class FakeBot:
    broadcast_enabled = False
    broadcast_to_megaphone = False


class TestAudioStreamerNetworkGuard(unittest.TestCase):
    """Jukebox streams (bot=None) must never re-broadcast as the player's own
    music bot stream, and mono streams must be sized for one channel."""

    def _make(self, game, bot=None, channels=2):
        from libs import music_bot as mb
        return mb.AudioStreamer(game, "http://example.com/a.mp3", object(), volume=50, bot=bot, channels=channels)

    def test_no_bot_never_sends(self):
        game = FakeGameNetwork()
        streamer = self._make(game, bot=None)
        streamer._send_to_network_actual(b"\x00" * 3840)
        self.assertEqual(game.network.sent, [])

    def test_bot_without_broadcast_never_sends(self):
        game = FakeGameNetwork()
        streamer = self._make(game, bot=FakeBot())
        streamer._send_to_network_actual(b"\x00" * 3840)
        self.assertEqual(game.network.sent, [])

    def test_bot_with_broadcast_still_sends(self):
        from libs import consts
        game = FakeGameNetwork()
        bot = FakeBot()
        bot.broadcast_enabled = True
        streamer = self._make(game, bot=bot)
        streamer._send_to_network_actual(b"\x00" * 3840)
        self.assertEqual(len(game.network.sent), 1)
        self.assertEqual(game.network.sent[0][0][0], consts.CHANNEL_MUSICBOT)

    def test_mono_buffer_size(self):
        game = FakeGameNetwork()
        mono = self._make(game, channels=1)
        self.assertEqual(mono.BUFFER_SIZE, 960 * 1 * 2)
        stereo = self._make(game, channels=2)
        self.assertEqual(stereo.BUFFER_SIZE, 960 * 2 * 2)

    def test_empty_pool_applies_backpressure_instead_of_allocating(self):
        from unittest import mock

        streamer = self._make(FakeGameNetwork())
        streamer._buffer_pool = []
        with mock.patch.object(streamer, "_reclaim_processed"):
            self.assertIsNone(streamer._get_buffer())

    def test_intentional_startup_cancel_does_not_announce_load_failure(self):
        from unittest import mock
        from libs import music_bot as mb

        class Source:
            buffers_processed = 0
            def stop(self): pass
        class Process:
            stderr = None
            def poll(self): return None
            def kill(self): pass
            def wait(self, timeout=None): pass

        streamer = mb.AudioStreamer(
            FakeGameNetwork(), "https://example.com/audio", Source(),
            bot=None, channels=2,
        )

        def cancel_during_prebuffer():
            streamer.running = False
            return 0, b""

        with mock.patch.object(streamer, "_init_buffer_pool"), \
                mock.patch.object(streamer, "_read_prebuffer", side_effect=cancel_during_prebuffer), \
                mock.patch("libs.music_bot.streaming.subprocess.Popen", return_value=Process()), \
                mock.patch("libs.music_bot.streaming.speak") as speak_mock:
            streamer.run()
        speak_mock.assert_not_called()
        self.assertIsNone(streamer.failure_reason)

    def test_fresh_song_never_seeks_but_resume_does(self):
        """A fresh jukebox song (start_offset=0) must start from the beginning
        even after the client spent seconds resolving; a mid-song resume still
        seeks to the current position (+ the resolve delay)."""
        from unittest import mock
        from libs import music_bot as mb

        class Source:
            buffers_processed = 0
            def stop(self): pass
        class Process:
            stderr = None
            def poll(self): return 1
            def kill(self): pass
            def wait(self, timeout=None): pass
            class _EOFStream:
                def read(self, n): return b""
            stdout = _EOFStream()

        captured_cmds = []

        def fake_popen(cmd, **kwargs):
            captured_cmds.append(list(cmd))
            return Process()

        def fake_prebuffer(streamer):
            def _read():
                return 1, b""
            return _read()

        # Fresh song: start_offset=0 -> no -ss at all.
        fresh = mb.AudioStreamer(
            FakeGameNetwork(), "https://example.com/audio.mp3", Source(),
            bot=None, channels=2, start_offset=0.0,
            start_offset_received_at=__import__("time").monotonic() - 5.0,
        )
        with mock.patch.object(fresh, "_init_buffer_pool"), \
                mock.patch.object(fresh, "_read_prebuffer", side_effect=lambda: fake_prebuffer(fresh)), \
                mock.patch("libs.music_bot.streaming.subprocess.Popen", side_effect=fake_popen):
            fresh.run()
        self.assertFalse(any("-ss" in c for c in captured_cmds),
                         "fresh song must not seek: %s" % captured_cmds)

        # Resume mid-song: start_offset=5 -> -ss present (5 + resolve delay).
        captured_cmds.clear()
        resume = mb.AudioStreamer(
            FakeGameNetwork(), "https://example.com/audio.mp3", Source(),
            bot=None, channels=2, start_offset=5.0,
            start_offset_received_at=__import__("time").monotonic() - 1.0,
        )
        with mock.patch.object(resume, "_init_buffer_pool"), \
                mock.patch.object(resume, "_read_prebuffer", side_effect=lambda: fake_prebuffer(resume)), \
                mock.patch("libs.music_bot.streaming.subprocess.Popen", side_effect=fake_popen):
            resume.run()
        self.assertTrue(any("-ss" in c for c in captured_cmds),
                        "resume must seek: %s" % captured_cmds)


class TestJukeboxRelayProtocol(unittest.TestCase):
    def test_packet_routes_only_matching_epoch(self):
        class Receiver:
            def __init__(self): self.received = []
            def receive(self, *args): self.received.append(args)
        player = jukebox.JukeboxPlayer(object())
        receiver = Receiver()
        player.relay_routes[(7, 9)] = receiver
        packet = bytes([1]) + struct.pack(">IIHB", 7, 9, 65535, 0) + b"opus"
        self.assertTrue(player.receive_relay_packet(packet))
        self.assertEqual(receiver.received, [(65535, b"opus", 0)])
        stale = bytes([1]) + struct.pack(">IIHB", 7, 8, 1, 0) + b"old"
        self.assertFalse(player.receive_relay_packet(stale))
        self.assertEqual(len(receiver.received), 1)

    def test_packet_rejects_bad_version_flags_and_size(self):
        player = jukebox.JukeboxPlayer(object())
        self.assertFalse(player.receive_relay_packet(b"short"))
        self.assertFalse(player.receive_relay_packet(bytes([2]) + b"\0" * 20))
        bad_flags = bytes([1]) + struct.pack(">IIHB", 1, 2, 3, 0x80) + b"x"
        self.assertFalse(player.receive_relay_packet(bad_flags))

    def test_relay_identity_change_recreates_receiver(self):
        """A server relay restart (new relay_id/epoch, same song + playback_id)
        must tear down the old receiver and register the new route — otherwise
        frames from the new worker are dropped and connected clients go silent
        (fresh clients register the new route on join, which is why only
        "old" clients lose audio after repeated reloads)."""
        from unittest import mock

        class Source:
            position = None
        class Context:
            def gen_source(self): return Source()
        class Audio:
            context = Context()
        class Game:
            audio_mngr = Audio()

        game = Game()
        with mock.patch("libs.jukebox.JukeboxRelayReceiver") as recv_cls:
            recv_cls.return_value.start = lambda: None
            recv_cls.return_value.stop = lambda: None
            recv_cls.return_value.join = lambda timeout: None
            player = jukebox.JukeboxPlayer(game)

            kwargs = dict(
                jukebox_id="box", x=1, y=2, z=3, title="Song",
                url="https://youtube.com/watch?v=x", duration=60,
                playback_id=44, transport="relay",
            )
            # Identity A.
            player.play(**kwargs, relay_id=1001, stream_epoch=2001)
            self.assertIn((1001, 2001), player.relay_routes)

            # Identity B: same song/playback, new relay worker (e.g. a relay
            # that died and was restarted during a map reload).
            player.play(**kwargs, relay_id=1002, stream_epoch=2002)
            self.assertNotIn((1001, 2001), player.relay_routes)
            self.assertIn((1002, 2002), player.relay_routes)
            self.assertEqual(player.players["box"]["relay_key"], (1002, 2002))
            # Two distinct receivers were created (one per relay identity).
            self.assertEqual(recv_cls.call_count, 2)

    def test_relay_same_identity_stays_seamless(self):
        """Same relay identity (reload re-broadcast) must NOT rebuild the
        receiver — playback continues without interruption."""
        from unittest import mock

        class Source:
            position = None
        class Context:
            def gen_source(self): return Source()
        class Audio:
            context = Context()
        class Game:
            audio_mngr = Audio()

        game = Game()
        with mock.patch("libs.jukebox.JukeboxRelayReceiver") as recv_cls:
            recv_cls.return_value.start = lambda: None
            recv_cls.return_value.stop = lambda: None
            recv_cls.return_value.join = lambda timeout: None
            player = jukebox.JukeboxPlayer(game)
            kwargs = dict(
                jukebox_id="box", x=1, y=2, z=3, title="Song",
                url="https://youtube.com/watch?v=x", duration=60,
                playback_id=44, transport="relay",
            )
            player.play(**kwargs, relay_id=1001, stream_epoch=2001)
            receiver_a = player.players["box"]["streamer"]
            player.play(**kwargs, relay_id=1001, stream_epoch=2001)
            self.assertIs(player.players["box"]["streamer"], receiver_a)
            self.assertIn((1001, 2001), player.relay_routes)
            self.assertEqual(recv_cls.call_count, 1)

    def test_duplicate_playback_id_is_idempotent_during_resolve(self):
        from unittest import mock

        class Source:
            position = None
        class Context:
            def gen_source(self): return Source()
        class Audio:
            context = Context()
        class Game:
            audio_mngr = Audio()

        game = Game()
        with mock.patch("libs.music_bot.AudioStreamer") as streamer_cls:
            player = jukebox.JukeboxPlayer(game)
            args = ("box", 1, 2, 3, "Song", "https://youtube.com/watch?v=x", 60)
            player.play(*args, playback_id=44)
            player.play(*args, playback_id=44)
            self.assertEqual(streamer_cls.call_count, 1)

    def test_spatial_gain_uses_dedicated_jukebox_category(self):
        """Jukebox songs must follow their OWN mixer slider ("jukebox"), not
        the music bot / map music category ("music")."""
        from libs import music_bot

        class Source:
            position = (0.0, 0.0, 0.0)
            gain = 0.0
        class Audio:
            position = (0.0, 0.0, 0.0)
            volume_categories = {"jukebox": [50], "music": [100]}
        class Game:
            audio_mngr = Audio()

        left, right = Source(), Source()
        streamer = music_bot.AudioStreamer(
            Game(), "https://example.com/audio", left, volume=40,
            spatial_pair=(left, right, 8, 40),
        )
        streamer._update_spatial_gain()
        # 40% jukebox volume x 50% jukebox category = 0.2
        self.assertAlmostEqual(left.gain, 0.2)
        self.assertAlmostEqual(right.gain, 0.2)

        # Lowering the music slider must NOT touch the jukebox.
        class Audio2:
            position = (0.0, 0.0, 0.0)
            volume_categories = {"jukebox": [50], "music": [10]}
        class Game2:
            audio_mngr = Audio2()
        left2, right2 = Source(), Source()
        streamer2 = music_bot.AudioStreamer(
            Game2(), "https://example.com/audio", left2, volume=40,
            spatial_pair=(left2, right2, 8, 40),
        )
        streamer2._update_spatial_gain()
        self.assertAlmostEqual(left2.gain, 0.2)

    def test_jukebox_volume_defaults_to_100(self):
        """The dedicated jukebox mixer category defaults to 100% so existing
        players are not quieter after the change."""
        from libs import options
        self.assertEqual(options.get("volume_jukebox", 100), 100)


class _SyncPutGame:
    """Fake game that executes main-thread lambdas synchronously."""

    class _Audio:
        class _Ctx:
            def gen_source(self):
                return type("Src", (), {"position": None})()
        context = _Ctx()

    audio_mngr = _Audio()

    def put(self, fn):
        fn()


class TestReloadSurvival(unittest.TestCase):
    """Repeated map reloads must NOT tear down a still-playing song:
    play() re-confirms the song and clears the pending-stop mark; only songs
    that are never re-confirmed get stopped after the grace period."""

    def _play(self, player, jid="box", playback=1):
        player.play(jid, 1, 2, 3, "Song", "https://youtube.com/watch?v=x", 60,
                    playback_id=playback, transport="direct")

    def test_confirmed_song_survives_reload_sweep(self):
        from unittest import mock
        import time
        with mock.patch("libs.music_bot.AudioStreamer"):
            player = jukebox.JukeboxPlayer(_SyncPutGame())
            self._play(player)
            # Map reload: mark for confirmation.
            player.mark_pending_map_change(player.control_serial)
            # Server re-broadcasts the same song -> play() clears the mark.
            self._play(player)
            time.sleep(player.MAP_RELOAD_CONFIRM_TIMEOUT + 0.3)  # let the grace-period sweep run
            self.assertIn("box", player.players)
            self.assertEqual(player._pending_map_change, set())

    def test_stale_reload_mark_cannot_claim_newer_play(self):
        """Cross-channel ordering may deliver play before the queued map mark."""
        from unittest import mock
        with mock.patch("libs.music_bot.AudioStreamer"):
            player = jukebox.JukeboxPlayer(_SyncPutGame())
            self._play(player)
            stale_serial = player.control_serial
            self._play(player)  # newer play control wins before stale mark runs
            self.assertFalse(player.mark_pending_map_change(stale_serial))
            self.assertEqual(player._pending_map_change, set())
            self.assertIn("box", player.players)

    def test_stale_stop_cannot_stop_newer_playback_generation(self):
        from unittest import mock
        with mock.patch("libs.music_bot.AudioStreamer"):
            player = jukebox.JukeboxPlayer(_SyncPutGame())
            self._play(player, playback=22)
            self.assertFalse(player.stop("box", playback_id=21))
            self.assertIn("box", player.players)
            self.assertTrue(player.stop("box", playback_id=22))
            self.assertNotIn("box", player.players)

    def test_unconfirmed_song_is_stopped_after_reload(self):
        from unittest import mock
        import time
        with mock.patch("libs.music_bot.AudioStreamer"):
            player = jukebox.JukeboxPlayer(_SyncPutGame())
            self._play(player)
            player.mark_pending_map_change(player.control_serial)
            time.sleep(player.MAP_RELOAD_CONFIRM_TIMEOUT + 0.3)  # no play() ever re-confirms it -> swept
            self.assertNotIn("box", player.players)

    def test_newer_reload_supersedes_older_sweep(self):
        from unittest import mock
        import time
        with mock.patch("libs.music_bot.AudioStreamer"):
            player = jukebox.JukeboxPlayer(_SyncPutGame())
            self._play(player, "box")
            self._play(player, "box2", playback=2)
            player.mark_pending_map_change(1)   # first reload
            self._play(player, "box")           # re-confirm box
            player.mark_pending_map_change(2)   # second (newer) reload
            self._play(player, "box")
            self._play(player, "box2", playback=2)
            time.sleep(player.MAP_RELOAD_CONFIRM_TIMEOUT + 0.3)
            # Both were re-confirmed under the newest mark -> still playing.
            self.assertIn("box", player.players)
            self.assertIn("box2", player.players)

    def test_play_forwards_http_headers_to_streamer(self):
        """Direct fallback must pass the server's paired auth headers so a
        signed googlevideo URL does not 403."""
        from unittest import mock

        class Source:
            position = None
        class Context:
            def gen_source(self): return Source()
        class Audio:
            context = Context()
        class Game:
            audio_mngr = Audio()

        game = Game()
        headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://youtube.com"}
        with mock.patch("libs.music_bot.AudioStreamer") as streamer_cls:
            player = jukebox.JukeboxPlayer(game)
            player.play(
                "box", 1, 2, 3, "Song", "https://rr.googlevideo.com/stream", 60,
                playback_id=44, transport="direct", http_headers=headers,
            )
        _, kwargs = streamer_cls.call_args
        self.assertEqual(kwargs.get("http_headers"), headers)

    def test_dead_relay_requests_authoritative_resync(self):
        """A receiver that died silently is removed before state re-sync."""
        import time
        from types import SimpleNamespace

        sent = []
        game = SimpleNamespace(
            network=SimpleNamespace(send=lambda *args: sent.append(args)),
            audio_mngr=None,
        )
        player = jukebox.JukeboxPlayer(game)
        player.players["box"] = {
            "source": None,
            "secondary_source": None,
            "streamer": SimpleNamespace(is_alive=lambda: False),
            "transport": "relay",
            "created_at": time.monotonic() - 20,
        }

        player.update()

        self.assertNotIn("box", player.players)
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][1], "jukebox_resync")

    def test_repeated_relay_failures_switch_to_direct_playback(self):
        """Three consecutive relay recovery failures on one connection switch
        that jukebox to local direct playback — chronic unreliable-channel
        loss used to leave a player silent until a full relogin."""
        import time
        from types import SimpleNamespace
        from unittest import mock

        sent = []
        game = SimpleNamespace(
            network=SimpleNamespace(send=lambda *args: sent.append(args)),
            audio_mngr=None,
        )
        player = jukebox.JukeboxPlayer(game)
        params = {
            "x": 1.0, "y": 2.0, "z": 3.0, "title": "Song",
            "url": "https://youtu.be/x", "duration": 120,
            "start_offset": 10.0, "http_headers": None,
            "received_at": time.monotonic() - 30,
        }
        plays = []
        player.play = lambda *a, **k: plays.append((a, k))

        with mock.patch("libs.jukebox.speak"):
            for _ in range(3):
                player.players["box"] = {
                    "source": None,
                    "secondary_source": None,
                    "streamer": SimpleNamespace(is_alive=lambda: False),
                    "transport": "relay",
                    "created_at": time.monotonic() - 20,
                    "play_params": dict(params),
                }
                # Each iteration represents one executed recovery attempt
                # (~5s apart in real time): let the resync cooldown expire.
                player._last_recovery_request_at = 0.0
                player.update()

        self.assertEqual(player._relay_fail_counts.get("box"), 3)
        # The switch fired exactly once, with direct transport and an offset
        # advanced past the original start by wall-clock time.
        self.assertEqual(len(plays), 1)
        args, kwargs = plays[0]
        self.assertEqual(args[0], "box")
        self.assertEqual(kwargs.get("transport"), "direct")
        self.assertGreaterEqual(kwargs.get("start_offset", 0), 40.0)
        # Sticky: later relay play events stay direct until the map changes.
        self.assertIn("box", player._direct_fallback_until)

    def test_healthy_resets_strike_count(self):
        """A relay that demonstrably delivers frames clears its strike count,
        so an earlier blip never escalates to a direct switch."""
        import time
        from types import SimpleNamespace

        sent = []
        game = SimpleNamespace(
            network=SimpleNamespace(send=lambda *args: sent.append(args)),
            audio_mngr=None,
        )
        player = jukebox.JukeboxPlayer(game)
        player._relay_fail_counts["box"] = 2
        now = time.monotonic()
        player.players["box"] = {
            "source": None,
            "secondary_source": None,
            "streamer": SimpleNamespace(
                is_alive=lambda: True,
                last_packet_at=now,
                last_audio_activity=now,
                _play_started=True,
            ),
            "transport": "relay",
            "created_at": now,
        }

        player.update()

        self.assertEqual(player._relay_fail_counts.get("box"), 0)
        self.assertIn("box", player.players)
        self.assertEqual(sent, [])

    def test_sticky_direct_overrides_relay_play(self):
        """While the direct fallback is sticky, an incoming relay play event
        plays directly instead of building another receiver for frames that
        never arrive on this connection."""
        import time
        from unittest import mock

        class Source:
            position = None
        class Context:
            def gen_source(self): return Source()
        class Audio:
            context = Context()
        class Game:
            audio_mngr = Audio()

        with mock.patch("libs.music_bot.AudioStreamer") as streamer_cls:
            player = jukebox.JukeboxPlayer(Game())
            player._direct_fallback_until["box"] = time.monotonic() + 60
            player._relay_pending[(7, 8)] = {"frames": [], "at": time.monotonic()}
            player.play(
                "box", 1, 2, 3, "Song", "https://youtu.be/x", 60,
                playback_id=9, transport="relay", relay_id=7, stream_epoch=8,
            )

        self.assertTrue(streamer_cls.called)
        entry = player.players["box"]
        self.assertEqual(entry["transport"], "direct")
        # No relay route was registered and the pending buffer was dropped.
        self.assertNotIn((7, 8), player.relay_routes)
        self.assertNotIn((7, 8), player._relay_pending)

    def test_map_change_clears_direct_sticky(self):
        """A fresh map is a fresh chance for the relay channel."""
        import time
        from types import SimpleNamespace

        sent = []
        game = SimpleNamespace(
            network=SimpleNamespace(send=lambda *args: sent.append(args)),
            audio_mngr=None,
        )
        player = jukebox.JukeboxPlayer(game)
        player._direct_fallback_until["box"] = time.monotonic() + 60
        player._relay_fail_counts["box"] = 3

        self.assertTrue(player.mark_pending_map_change(player.control_serial))

        self.assertEqual(player._direct_fallback_until, {})
        self.assertEqual(player._relay_fail_counts, {})

    def test_repeated_warmup_unsticks_rebuild_early(self):
        """A stall that keeps surviving warm-up resyncs rebuilds immediately
        after RELAY_UNSTICK_TRIES attempts, instead of waiting out the full
        hard-stall timeout (the post-map-reload pattern from the field)."""
        import time
        from types import SimpleNamespace

        sent = []
        game = SimpleNamespace(
            network=SimpleNamespace(send=lambda *args: sent.append(args)),
            audio_mngr=None,
        )
        player = jukebox.JukeboxPlayer(game)

        def stall_box():
            player.players["box"] = {
                "source": None,
                "secondary_source": None,
                # Alive receiver whose last packet is already >5s old.
                "streamer": SimpleNamespace(
                    is_alive=lambda: True,
                    last_packet_at=time.monotonic() - 6,
                    last_audio_activity=None,
                    _play_started=False,
                ),
                "transport": "relay",
                "created_at": time.monotonic(),
                "play_params": {},
            }

        # First stalled update: resync sent, un-stick counted, entry kept.
        stall_box()
        player._last_recovery_request_at = 0.0
        player.update()
        self.assertEqual(player._stall_unsticks.get("box"), 1)
        self.assertIn("box", player.players)

        # Second stalled update: the un-stick did not hold — rebuild now.
        stall_box()
        player._last_recovery_request_at = 0.0
        player.update()
        self.assertEqual(player._stall_unsticks.get("box"), 2)
        self.assertNotIn("box", player.players)

    def test_underrunning_speaker_triggers_rebuild(self):
        """A stream whose frames arrive and buffers queue, but whose speakers
        stopped CONSUMING (slow starvation / stutter loop), is invisible to
        every packet-based check — only output consumption catches it."""
        import time
        from types import SimpleNamespace

        sent = []
        game = SimpleNamespace(
            network=SimpleNamespace(send=lambda *args: sent.append(args)),
            audio_mngr=None,
        )
        player = jukebox.JukeboxPlayer(game)
        now = time.monotonic()
        # Everything looks "healthy": alive receiver, fresh packets, fresh
        # buffer queueing — but OpenAL consumed nothing for 10 seconds.
        player.players["box"] = {
            "source": None,
            "secondary_source": None,
            "streamer": SimpleNamespace(
                is_alive=lambda: True,
                last_packet_at=now,
                last_audio_activity=now,
                last_output_at=now - 10,
                _play_started=True,
            ),
            "transport": "relay",
            "created_at": now,
            "play_params": {},
        }

        player.update()

        self.assertNotIn("box", player.players)

    def test_trickling_frame_rate_escalates_to_rebuild(self):
        """A trickling channel (a few frames every few seconds) passes every
        liveness check — packets fresh, buffers queueing, speakers consuming
        the bursts — while the listener hears a sped-up stutter fading to
        silence. Only the arrival RATE over a window exposes it; the rebuild
        ladder then escalates to direct playback mid-song."""
        import time
        from types import SimpleNamespace
        from unittest import mock

        sent = []
        game = SimpleNamespace(
            network=SimpleNamespace(send=lambda *args: sent.append(args)),
            audio_mngr=None,
        )
        player = jukebox.JukeboxPlayer(game)
        plays = []
        player.play = lambda *a, **k: plays.append((a, k))
        real_params = {
            "x": 1.0, "y": 2.0, "z": 3.0, "title": "Song",
            "url": "https://youtu.be/x", "duration": 120,
            "start_offset": 10.0, "http_headers": None,
            "received_at": time.monotonic() - 30,
        }

        def trickling_box():
            now = time.monotonic()
            player.players["box"] = {
                "source": None,
                "secondary_source": None,
                "streamer": SimpleNamespace(
                    is_alive=lambda: True,
                    last_packet_at=now,
                    last_audio_activity=now,
                    last_output_at=now,
                    _play_started=True,
                    received_frames=1000,
                ),
                "transport": "relay",
                "created_at": now,
                "play_params": dict(real_params),
                # Snapshot taken 10s ago with only 4 more frames back then:
                # 4 frames / 10s = 0.4 fps, far below the 12.5 fps floor.
                "frame_rate_check": [now - 10, 996],
            }

        with mock.patch("libs.jukebox.speak"):
            for _ in range(3):
                trickling_box()
                player._last_recovery_request_at = 0.0
                player.update()

        # Three starvation rebuilds escalate to the direct fallback.
        self.assertEqual(player._relay_fail_counts.get("box"), 3)
        self.assertEqual(len(plays), 1)
        self.assertEqual(plays[0][1].get("transport"), "direct")

    def test_healthy_frame_rate_does_not_rebuild(self):
        """A full-rate stream (25 fps) with all-green metrics never trips the
        starvation watchdog."""
        import time
        from types import SimpleNamespace

        sent = []
        game = SimpleNamespace(
            network=SimpleNamespace(send=lambda *args: sent.append(args)),
            audio_mngr=None,
        )
        player = jukebox.JukeboxPlayer(game)
        now = time.monotonic()
        player.players["box"] = {
            "source": None,
            "secondary_source": None,
            "streamer": SimpleNamespace(
                is_alive=lambda: True,
                last_packet_at=now,
                last_audio_activity=now,
                last_output_at=now,
                _play_started=True,
                received_frames=1250,
            ),
            "transport": "relay",
            "created_at": now,
            "play_params": {},
            # 250 frames over 10s = 25 fps — exactly a healthy stream.
            "frame_rate_check": [now - 10, 1000],
        }

        player.update()

        self.assertIn("box", player.players)
        self.assertEqual(sent, [])

    def test_relay_pending_keeps_working_direct_stream(self):
        """Make-before-break: a relay_pending event for the song already
        playing over a healthy DIRECT stream must not stop it (map reloads
        used to mute a working direct stream for ~10s while the relay
        readied)."""
        import time
        from types import SimpleNamespace

        game = SimpleNamespace(
            network=SimpleNamespace(send=lambda *a: None),
            audio_mngr=None,
        )
        player = jukebox.JukeboxPlayer(game)
        player.players["box"] = {
            "source": None,
            "secondary_source": None,
            "streamer": SimpleNamespace(is_alive=lambda: True),
            "title": "Song", "url": "https://youtu.be/x",
            "transport": "direct",
            "playback_key": ("id", 4),
            "created_at": time.monotonic(),
            "play_params": {},
        }

        player.play(
            "box", 1, 2, 3, "Song", "https://youtu.be/x", 120,
            playback_id=4, transport="relay_pending",
        )

        entry = player.players["box"]
        self.assertEqual(entry["transport"], "direct")
        self.assertIsNotNone(entry.get("streamer"))

    def test_relay_receiver_thread_starts_and_stops_cleanly(self):
        """Regression: JukeboxRelayReceiver used to set `self._started = False`,
        shadowing threading.Thread._started (an Event). Thread.start() then
        raised AttributeError -> 'failed to start streamer' -> relay audio never
        played. The receiver must be able to start() and join() cleanly."""
        import threading

        class FakeBuffer:
            def set_data(self, *a, **k): pass
        class FakeContext:
            def gen_buffer(self): return FakeBuffer()
            def batch(self):
                return _NullContext()
        class FakeSource:
            buffers_processed = 0
            buffers_queued = 0
            state = None
            position = (0.0, 0.0, 0.0)
            def unqueue_buffers(self): return None
            def queue_buffers(self, b): self.buffers_queued += 1
            def play(self): pass
            def stop(self): pass
        class FakeAudio:
            position = (0.0, 0.0, 0.0)
            volume_categories = {"music": [100]}
            context = FakeContext()
        class FakeGame:
            audio_mngr = FakeAudio()

        game = FakeGame()
        receiver = jukebox.JukeboxRelayReceiver(
            game, FakeSource(), FakeSource(), 65, 123, 456, 8.0, 40.0,
        )
        # The exact call that used to crash inside JukeboxPlayer.play():
        receiver.start()
        self.assertTrue(receiver.is_alive())
        self.assertTrue(hasattr(receiver, "_play_started"))
        # `_started` must remain threading.Thread's own Event (start()/join()
        # depend on it) — the bug was replacing it with a plain bool.
        self.assertIsInstance(receiver._started, threading.Event)
        receiver.stop()
        receiver.join(timeout=1.0)  # Test cleanup only; stop must not join on the game thread.
        self.assertFalse(receiver.is_alive())

    def test_pick_song_with_only_googlevideo_url_resolves_in_background(self):
        """A bare signed googlevideo URL must never be queued directly (it
        expires -> 403). It is resolved to a canonical URL first."""
        from unittest import mock

        game = FakeGameNetwork()
        gp = FakeGameplay()
        result = {
            "title": "Expiring stream",
            "duration": 180,
            "url": "https://rr.example.googlevideo.com/signed-stream",
        }
        with mock.patch("libs.jukebox.speak") as speak_mock, \
                mock.patch("libs.jukebox.threading.Thread",
                           side_effect=lambda *a, **k: (_ for _ in ()).throw(
                               AssertionError("background resolve started"))):
            with self.assertRaises(AssertionError):
                jukebox._pick_song(game, gp, "box-1", result)
        # Nothing queued synchronously with the expiring URL.
        self.assertEqual(game.network.sent, [])
        self.assertEqual(gp.pop_count, 1)

    def test_pick_song_builds_canonical_from_video_id(self):
        from unittest import mock

        game = FakeGameNetwork()
        gp = FakeGameplay()
        result = {
            "title": "Id-only",
            "duration": 120,
            "url": "https://rr.example.googlevideo.com/signed",
            "id": "abc123XYZ",
        }
        with mock.patch("libs.jukebox.speak"):
            jukebox._pick_song(game, gp, "box-1", result)
        args, _ = game.network.sent[-1]
        self.assertEqual(args[2]["url"], "https://www.youtube.com/watch?v=abc123XYZ")


class _NullContext:
    def __enter__(self): return None
    def __exit__(self, *exc): return False

    def test_delayed_map_cleanup_cannot_stop_new_playback_control(self):
        player = jukebox.JukeboxPlayer(object())
        old_serial = player.control_serial
        player.play(
            "box", 0, 0, 0, "Song", "https://youtube.com/watch?v=x", 60,
            playback_id=12, transport="relay_pending",
        )
        self.assertFalse(player.stop_all_if_serial(old_serial))
        self.assertIn("box", player.players)
        self.assertTrue(player.stop_all_if_serial(player.control_serial))
        self.assertNotIn("box", player.players)


class TestJukeboxPlayerSpatial(unittest.TestCase):
    """The jukebox player must create a STEREO spatial pair (two positioned
    mono sources, L/R split) with no network bot — like piano/drums."""

    def test_play_uses_stereo_spatial_pair(self):
        from unittest import mock
        from libs import jukebox

        class FakeSource:
            def __init__(self):
                self.position = None
                self.rolloff_factor = None
                self.reference_distance = None
                self.max_distance = None
                self.spatialize = None
                self.direct_channels = None
                self.gain = None

        class FakeContext:
            def __init__(self):
                self.created = []

            def gen_source(self):
                s = FakeSource()
                self.created.append(s)
                return s

        class FakeAudio:
            def __init__(self):
                self.context = FakeContext()

        class FakeGame:
            def __init__(self):
                self.audio_mngr = FakeAudio()

        game = FakeGame()
        with mock.patch("libs.music_bot.AudioStreamer") as AS:
            player = jukebox.JukeboxPlayer(game)
            player.play("j1", 10, 20, 0, "Song", "http://example.com/a.mp3", 60)
            args, kwargs = AS.call_args
            # STEREO decode split into L/R, no bot, spatial_pair passed.
            self.assertEqual(kwargs.get("channels"), 2)
            self.assertIsNone(kwargs.get("bot"))
            src_l, src_r, ref, maxd = kwargs["spatial_pair"]
            self.assertEqual(src_l.position, (7.5, 20, 0))   # x - 2.5
            self.assertEqual(src_r.position, (12.5, 20, 0))  # x + 2.5
            self.assertLess(ref, maxd)
            self.assertTrue(src_l.spatialize)
            self.assertEqual(src_l.rolloff_factor, 0.0)
            created = game.audio_mngr.context.created
            self.assertEqual(len(created), 2)

    def test_split_stereo_16(self):
        from libs import music_bot as mb
        # Interleaved s16le: L=0x0102, R=0x0304, L=0x0506, R=0x0708 ...
        data = bytes([
            0x02, 0x01, 0x04, 0x03,
            0x06, 0x05, 0x08, 0x07,
            0x0a, 0x09, 0x0c, 0x0b,
        ])
        left, right = mb.AudioStreamer._split_stereo_16(data)
        self.assertEqual(left, bytes([0x02, 0x01, 0x06, 0x05, 0x0a, 0x09]))
        self.assertEqual(right, bytes([0x04, 0x03, 0x08, 0x07, 0x0c, 0x0b]))
        self.assertEqual(len(left), len(right))

    def test_play_applies_room_reverb_to_both_sources(self):
        """The song picks up the reverb zone at the jukebox's position, like
        piano/drums: efx.send is applied to BOTH the L and R sources."""
        from unittest import mock
        from libs import jukebox

        class FakeSource:
            def __init__(self):
                self.position = None
                self.rolloff_factor = None
                self.reference_distance = None
                self.max_distance = None
                self.spatialize = None
                self.direct_channels = None
                self.gain = None

        class FakeContext:
            def gen_source(self):
                return FakeSource()

        class FakeEfx:
            def __init__(self):
                self.sends = []

            def send(self, source, index, slot, **kw):
                self.sends.append((source, index, slot))

        class FakeAudio:
            def __init__(self):
                self.context = FakeContext()
                self.efx = FakeEfx()

        class FakeReverbZone:
            reverb = "reverb-slot-1"

        class FakeMap:
            def get_reverb_at(self, x, y, z):
                return FakeReverbZone()

        class FakeGameplay:
            def __init__(self):
                self.map = FakeMap()

        class FakeGame:
            def __init__(self):
                self.audio_mngr = FakeAudio()
                self.gameplay = FakeGameplay()

        game = FakeGame()
        with mock.patch("libs.music_bot.AudioStreamer") as AS:
            player = jukebox.JukeboxPlayer(game)
            player.play("j1", 10, 10, 0, "Song", "http://example.com/a.mp3", 60)
            sends = game.audio_mngr.efx.sends
            self.assertEqual(len(sends), 2)
            self.assertEqual(sends[0][1], 0)
            self.assertEqual(sends[0][2], "reverb-slot-1")
            self.assertEqual(sends[1][2], "reverb-slot-1")

    def test_play_max_distance_is_40(self):
        from unittest import mock
        from libs import jukebox

        class FakeSource:
            def __init__(self):
                self.position = None
                self.rolloff_factor = None
                self.reference_distance = None
                self.max_distance = None
                self.spatialize = None
                self.direct_channels = None
                self.gain = None

        class FakeContext:
            def gen_source(self):
                return FakeSource()

        class FakeAudio:
            def __init__(self):
                self.context = FakeContext()

        class FakeGame:
            def __init__(self):
                self.audio_mngr = FakeAudio()

        game = FakeGame()
        with mock.patch("libs.music_bot.AudioStreamer") as AS:
            player = jukebox.JukeboxPlayer(game)
            player.play("j1", 10, 10, 0, "Song", "http://example.com/a.mp3", 60)
            _, _, ref, maxd = AS.call_args.kwargs["spatial_pair"]
            self.assertEqual(ref, 8.0)
            self.assertEqual(maxd, 40.0)


class TestAudioStreamerFailureSurfacing(unittest.TestCase):
    """A stream that produces no audio (ffmpeg exits / bad URL) must not fail
    silently: jukebox streams speak a clear error and the process is killed."""

    def _run_streamer(self, bot, speak_calls, start_offset=0.0,
                      http_headers=None, audio_url="http://example.com/a.mp3"):
        from unittest import mock
        from libs import music_bot as mb

        class FakeStdout:
            def read(self, n):
                return b""  # immediate EOF: ffmpeg died before producing audio

        class FakeStderr:
            def read(self, n):
                return b"tls error"

        class FakeProc:
            def __init__(self):
                self.stdout = FakeStdout()
                self.stderr = FakeStderr()
                self.killed = False

            def poll(self):
                return 1  # exited

            def kill(self):
                self.killed = True

        proc = FakeProc()
        streamer = mb.AudioStreamer(
            None,
            audio_url,
            object(),
            volume=50,
            bot=bot,
            start_offset=start_offset,
            http_headers=http_headers,
        )
        with mock.patch.object(mb_stream, "subprocess") as sp, \
                mock.patch.object(streamer, "_init_buffer_pool") as init_pool, \
                mock.patch.object(mb_stream, "speak") as speak, \
                mock.patch.object(mb_stream.logger, "log") as log_line:
            sp.Popen = mock.Mock(return_value=proc)
            streamer.run()
        command = sp.Popen.call_args.args[0]
        return proc, speak, log_line, command

    def test_jukebox_stream_failure_speaks_and_kills(self):
        proc, speak, log_line, command = self._run_streamer(
            None, [], start_offset=12.5
        )
        self.assertTrue(proc.killed)
        self.assertTrue(speak.called)
        self.assertTrue(log_line.called)
        msg = speak.call_args[0][0]
        self.assertIn("jukebox", msg.lower())
        self.assertIn("-re", command)
        self.assertLess(command.index("-re"), command.index("-i"))
        self.assertEqual(command[command.index("-ss") + 1], "12.50")
        self.assertLess(command.index("-ss"), command.index("-i"))

    def test_personal_bot_failure_logs_but_does_not_speak(self):
        from libs import music_bot as mb
        proc, speak, log_line, command = self._run_streamer(bot=FakeBot(), speak_calls=[])
        self.assertTrue(proc.killed)
        self.assertFalse(speak.called)  # menu layer gives its own feedback
        self.assertTrue(log_line.called)
        self.assertIn("-re", command)

    def test_googlevideo_streams_get_reconnect_flags(self):
        # A CDN drop near the end of a song used to kill ffmpeg outright
        # (no reconnect flags on googlevideo) and cut the ending off.
        # Verified: with reconnect, ffmpeg resumes at the last byte offset
        # (range requests) and the song plays out in full.
        proc, speak, log_line, command = self._run_streamer(
            None, [], start_offset=0.0,
            audio_url="https://rr3---sn-a5mekn7e.googlevideo.com/videoplayback?foo=1",
        )
        self.assertIn("-reconnect", command)
        self.assertIn("-reconnect_streamed", command)
        # googlevideo keeps a SHORT reconnect budget so the startup-403 path
        # (stale signed URL) re-resolves a fresh URL quickly instead of
        # burning the full backoff window.
        self.assertEqual(
            command[command.index("-reconnect_delay_total_max") + 1], "6"
        )

    def test_regular_http_streams_keep_full_reconnect_budget(self):
        _, _, _, command = self._run_streamer(
            None, [], start_offset=0.0,
            audio_url="https://cdn.example.com/song.mp3",
        )
        self.assertIn("-reconnect", command)
        self.assertEqual(
            command[command.index("-reconnect_delay_total_max") + 1], "12"
        )

    def test_ffmpeg_receives_paired_ytdlp_headers_before_input(self):
        _, _, _, command = self._run_streamer(
            bot=FakeBot(),
            speak_calls=[],
            http_headers={
                "User-Agent": "Exact yt-dlp Agent/139",
                "Accept-Language": "en-US,en;q=0.5",
                "Injected": "safe\r\nX-Evil: true",
            },
        )
        self.assertIn("-user_agent", command)
        self.assertEqual(
            command[command.index("-user_agent") + 1],
            "Exact yt-dlp Agent/139",
        )
        self.assertIn("-headers", command)
        header_block = command[command.index("-headers") + 1]
        self.assertIn("Accept-Language: en-US,en;q=0.5\r\n", header_block)
        self.assertNotIn("User-Agent", header_block)
        self.assertNotIn("Injected", header_block)
        self.assertNotIn("X-Evil", header_block)
        self.assertIn("-reconnect_on_http_error", command)
        self.assertLess(command.index("-headers"), command.index("-i"))

    def test_transient_googlevideo_403_retries_and_reaches_ready_state(self):
        from unittest import mock
        import cyal
        from libs import music_bot as mb

        class FakeStdout:
            def __init__(self, data=b""):
                self.data = bytearray(data)

            def read(self, size):
                if not self.data:
                    return b""
                chunk = bytes(self.data[:size])
                del self.data[:size]
                return chunk

        class FakeStderr:
            def __init__(self, data=b""):
                self.data = data

            def read(self, size):
                return self.data[:size]

        class FakeProc:
            def __init__(self, pcm=b"", stderr=b"", exit_code=1):
                self.stdout = FakeStdout(pcm)
                self.stderr = FakeStderr(stderr)
                self.killed = False
                self._exit_code = exit_code

            def poll(self):
                return None if self.stdout.data else self._exit_code

            def kill(self):
                self.killed = True

            def wait(self, timeout=None):
                return self._exit_code

        class FakeBuffer:
            def set_data(self, data, sample_rate=None, format=None):
                self.data = data

        class FakeContext:
            def gen_buffer(self):
                return FakeBuffer()

        class FakeAudio:
            def __init__(self):
                self.context = FakeContext()

        class FakeGame:
            def __init__(self):
                self.audio_mngr = FakeAudio()

        class FakeSource:
            def __init__(self):
                self.items = []
                self.state = cyal.SourceState.STOPPED

            @property
            def buffers_queued(self):
                return len(self.items)

            @property
            def buffers_processed(self):
                return len(self.items)

            def queue_buffers(self, buffer):
                self.items.append(buffer)

            def unqueue_buffers(self):
                return self.items.pop(0) if self.items else None

            def play(self):
                self.state = cyal.SourceState.PLAYING

            def stop(self):
                self.state = cyal.SourceState.STOPPED

        denied = FakeProc(stderr=b"Server returned 403 Forbidden")
        # The retried process produces audio and then exits 0 (clean EOF) — a
        # genuine song end must stay a normal completion, not a failure.
        playable = FakeProc(pcm=b"\0" * (3840 * 10), exit_code=0)
        streamer = mb.AudioStreamer(
            FakeGame(),
            "https://rr.example.googlevideo.com/audio",
            FakeSource(),
            bot=None,
            http_headers={"User-Agent": "Exact Agent"},
        )
        with mock.patch.object(mb_stream.subprocess, "Popen", side_effect=[denied, playable]) as popen, \
                mock.patch.object(mb_stream.time, "sleep"), \
                mock.patch.object(mb_stream, "speak"), \
                mock.patch.object(mb_stream.logger, "log"):
            streamer.run()

        self.assertEqual(popen.call_count, 2)
        self.assertTrue(denied.killed)
        self.assertTrue(streamer.ready_event.is_set())
        self.assertIsNone(streamer.failure_reason)
        self.assertTrue(streamer.completed_normally)


class TestMusicBotStreamMetadata(unittest.TestCase):
    def test_search_uses_flat_extraction_and_builds_canonical_urls(self):
        """Search must use extract_flat (one-request metadata, ~5x faster) and
        still return canonical watch URLs — never expired signed streams."""
        from unittest import mock
        import yt_dlp
        from libs import music_bot as mb

        class FakeYDL:
            def __init__(self, options):
                self.options = options

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def extract_info(self, target, download=False):
                self.target = target
                return {
                    "entries": [
                        {
                            "id": "abc123",
                            "title": "Flat Song",
                            # Flat search returns durations as FLOAT — the
                            # results menus format with '% 60:02d' (int-only),
                            # so search() must normalize to int or the menu
                            # crashes before it opens.
                            "duration": 210.0,
                            # Flat entries have no webpage_url; the watch URL
                            # sits in `url` instead.
                            "url": "https://www.youtube.com/watch?v=abc123",
                        },
                        {
                            "id": "def456",
                            "title": "Broken result",
                        },
                        None,  # poisoned entry must not kill the search
                    ]
                }

        instances = []

        def factory(opts):
            inst = FakeYDL(opts)
            instances.append(inst)
            return inst

        with mock.patch.object(yt_dlp, "YoutubeDL", side_effect=factory):
            results = mb.YouTubeSearcher.search("query", count=5)
        fake = instances[0]

        self.assertEqual(fake.options.get("extract_flat"), "in_playlist")
        self.assertTrue(fake.options.get("skip_download"))
        self.assertEqual(fake.target, "ytsearch5:query")
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0]["webpage_url"], "https://www.youtube.com/watch?v=abc123")
        # No signed googlevideo stream leaks out of search.
        self.assertNotIn("googlevideo", results[0]["url"])
        # Float duration must be normalized to int (menu crash regression).
        self.assertEqual(results[0]["duration"], 210)
        self.assertIsInstance(results[0]["duration"], int)
        self.assertEqual(
            results[1]["webpage_url"],
            "https://www.youtube.com/watch?v=def456",
        )

    def test_get_stream_info_keeps_url_and_authorization_headers(self):
        from unittest import mock
        from libs import music_bot as mb
        resolved = {
            "url": "https://rr.example.googlevideo.com/audio",
            "http_headers": {"User-Agent": "Exact Agent"},
        }
        with mock.patch("libs.youtube_resolver.resolve_stream_info", return_value=resolved) as resolve:
            info = mb.YouTubeSearcher.get_stream_info(
                "https://www.youtube.com/watch?v=abc"
            )
        resolve.assert_called_once_with("https://www.youtube.com/watch?v=abc", cancelled=None)
        self.assertEqual(info["url"], "https://rr.example.googlevideo.com/audio")
        self.assertEqual(info["http_headers"]["User-Agent"], "Exact Agent")

    def test_failed_start_is_not_announced_as_finished(self):
        from unittest import mock
        from libs import music_bot as mb

        class DeadStreamer:
            failure_reason = "stream produced no audio"

            def __init__(self):
                import threading
                self.ready_event = threading.Event()

            def is_alive(self):
                return False

        bot = mb.MapMusicBot.__new__(mb.MapMusicBot)
        bot.enabled = True
        bot.broadcast_to_megaphone = False
        bot.duck_multiplier = 1.0
        bot.stream_source = None
        bot.playing = True
        bot.paused = False
        bot.mode = "youtube"
        bot.current_local_sound = None
        bot.current_title = "Unavailable track"
        bot.streamer = DeadStreamer()
        bot._stream_announced = False
        bot._find_gameplay = lambda: None
        bot._ensure_live_relay_streamer = lambda: None
        bot._advance_track_queue = lambda: False

        with mock.patch.object(mb_ctrl, "speak") as speak:
            bot.loop()
        speak.assert_called_once_with("Could not load track.")


class TestRelayPendingMakeBeforeBreak(unittest.TestCase):
    """A relay_pending re-offer must never tear down a stream that is still
    audibly working for the same song — the server's retry notice can race in
    while the current relay (or direct) stream is perfectly healthy, and
    stopping it made the jukebox go silent for no reason (rapid map
    round-trips)."""

    def _player_with_relay(self, last_packet_ago):
        import time
        from types import SimpleNamespace

        game = SimpleNamespace(
            network=SimpleNamespace(send=lambda *a: None),
            audio_mngr=None,
        )
        player = jukebox.JukeboxPlayer(game)

        class FakeRelayReceiver:
            def __init__(self):
                self.last_packet_at = time.monotonic() - last_packet_ago

            def is_alive(self):
                return True

            def stop(self):
                pass

        receiver = FakeRelayReceiver()
        player.players["box"] = {
            "source": None,
            "secondary_source": None,
            "streamer": receiver,
            "title": "Song", "url": "https://youtu.be/x",
            "transport": "relay",
            "playback_key": ("id", 4),
            "relay_key": (1001, 2001),
            "created_at": time.monotonic(),
            "play_params": {},
        }
        player.relay_routes[(1001, 2001)] = receiver
        return player, receiver

    def test_relay_pending_keeps_live_relay_receiver(self):
        """Same song, receiver still receiving frames (0.5s ago): the pending
        re-offer is a stale/racing notice — keep the receiver playing."""
        player, receiver = self._player_with_relay(last_packet_ago=0.5)
        player.play(
            "box", 1, 2, 3, "Song", "https://youtu.be/x", 120,
            playback_id=4, transport="relay_pending",
        )
        entry = player.players["box"]
        self.assertEqual(entry["transport"], "relay")
        self.assertIs(entry.get("streamer"), receiver)
        self.assertIn((1001, 2001), player.relay_routes)

    def test_relay_pending_replaces_stale_relay_receiver(self):
        """Receiver starved for 30s: the relay is truly gone — install the
        pending placeholder so the ready relay event can rebuild it."""
        player, receiver = self._player_with_relay(last_packet_ago=30.0)
        player.play(
            "box", 1, 2, 3, "Song", "https://youtu.be/x", 120,
            playback_id=4, transport="relay_pending",
        )
        entry = player.players["box"]
        self.assertEqual(entry["transport"], "relay_pending")
        self.assertIsNone(entry.get("streamer"))

    def test_stop_all_clears_pending_map_change_marks(self):
        """A synchronous map-change teardown must clear any pending sweep marks
        so a stale sweep can never act on a later jukebox instance."""
        import time
        from types import SimpleNamespace

        game = SimpleNamespace(
            network=SimpleNamespace(send=lambda *a: None),
            audio_mngr=None,
        )
        player = jukebox.JukeboxPlayer(game)
        player.players["box"] = {
            "source": None,
            "secondary_source": None,
            "streamer": None,
            "title": "Song", "url": "https://youtu.be/x",
            "transport": "direct",
            "playback_key": ("id", 4),
            "created_at": time.monotonic(),
            "play_params": {},
        }
        player.mark_pending_map_change(player.control_serial)
        self.assertEqual(player._pending_map_change, {"box"})
        player.stop_all()
        self.assertEqual(player._pending_map_change, set())
        self.assertIsNone(player._pending_map_change_serial)
        self.assertEqual(player.players, {})


class TestAudioStreamerMidSongDeath(unittest.TestCase):
    """An ffmpeg process that dies AFTER audio started (403 on a CDN
    reconnect, connection reset, ...) must be reported as a FAILURE so the
    jukebox recovery watchdog rebuilds it — not as a naturally finished song
    (which left the cabinet silent until the next track)."""

    def _run_streamer(self, pcm, exit_code):
        from unittest import mock
        import cyal
        from libs import music_bot as mb

        class FakeStdout:
            def __init__(self, data=b""):
                self.data = bytearray(data)

            def read(self, size):
                if not self.data:
                    return b""
                chunk = bytes(self.data[:size])
                del self.data[:size]
                return chunk

        class FakeStderr:
            def read(self, size):
                return b""

        class FakeProc:
            def __init__(self, pcm=b"", exit_code=0):
                self.stdout = FakeStdout(pcm)
                self.stderr = FakeStderr()
                self._exit_code = exit_code
                self.killed = False

            def poll(self):
                # The process is alive while it still has output to give;
                # once the stream is exhausted it has exited.
                return None if self.stdout.data else self._exit_code

            def kill(self):
                self.killed = True

            def wait(self, timeout=None):
                return self._exit_code

        class FakeBuffer:
            def set_data(self, data, sample_rate=None, format=None):
                self.data = data

        class FakeContext:
            def gen_buffer(self):
                return FakeBuffer()

        class FakeAudio:
            def __init__(self):
                self.context = FakeContext()

        class FakeGame:
            def __init__(self):
                self.audio_mngr = FakeAudio()

        class FakeSource:
            def __init__(self):
                self.items = []
                self.state = cyal.SourceState.STOPPED

            @property
            def buffers_queued(self):
                return len(self.items)

            @property
            def buffers_processed(self):
                return len(self.items)

            def queue_buffers(self, buffer):
                self.items.append(buffer)

            def unqueue_buffers(self):
                return self.items.pop(0) if self.items else None

            def play(self):
                self.state = cyal.SourceState.PLAYING

            def stop(self):
                self.state = cyal.SourceState.STOPPED

        proc = FakeProc(pcm=pcm, exit_code=exit_code)
        streamer = mb.AudioStreamer(
            FakeGame(),
            "https://example.com/audio.mp3",
            FakeSource(),
            bot=None,
        )
        with mock.patch.object(mb_stream.subprocess, "Popen", return_value=proc) as popen, \
                mock.patch.object(mb_stream.time, "sleep"), \
                mock.patch.object(mb_stream, "speak"), \
                mock.patch.object(mb_stream.logger, "log"):
            streamer.run()
        return streamer

    def test_mid_song_ffmpeg_death_is_a_failure(self):
        # 12 frames: 10 pre-buffered, 2 played, then the pipe dies with exit 1.
        streamer = self._run_streamer(
            pcm=b"\0" * (3840 * 12), exit_code=1
        )
        self.assertTrue(streamer.ready_event.is_set())
        self.assertFalse(streamer.completed_normally)
        self.assertIsNotNone(streamer.failure_reason)
        self.assertIn("ffmpeg exited early", streamer.failure_reason)

    def test_clean_eof_stays_normal_completion(self):
        # Natural song end: ffmpeg exits 0 -> still a normal completion.
        streamer = self._run_streamer(
            pcm=b"\0" * (3840 * 12), exit_code=0
        )
        self.assertTrue(streamer.ready_event.is_set())
        self.assertTrue(streamer.completed_normally)
        self.assertIsNone(streamer.failure_reason)


class TestDirectOutputStallWatchdog(unittest.TestCase):
    """A direct (no-relay) jukebox stream whose speakers stop consuming
    buffers must be rebuilt even while its thread is alive — ffmpeg can
    hang without exiting and OpenAL can lose its sink, and the old
    thread-death check could never see either (the cabinet went silent
    for that listener until the next song)."""

    def _player(self):
        import time
        from types import SimpleNamespace
        sent = []
        game = SimpleNamespace(
            network=SimpleNamespace(send=lambda *args: sent.append(args)),
            audio_mngr=None,
        )
        player = jukebox.JukeboxPlayer(game)
        # Fresh enough that created_at never trips other watchdogs.
        player._last_recovery_request_at = 0.0
        return player, sent, time

    def _direct_entry(self, time, *, alive=True, ready=True, running=True,
                      output_age=None, failure=None):
        import threading
        from types import SimpleNamespace
        ready_ev = threading.Event()
        if ready:
            ready_ev.set()
        streamer = SimpleNamespace(
            is_alive=lambda: alive,
            ready_event=ready_ev,
            running=running,
            failure_reason=failure,
        )
        if output_age is None:
            streamer.last_output_at = None
        else:
            streamer.last_output_at = time.monotonic() - output_age
        return {
            "source": None, "secondary_source": None, "streamer": streamer,
            "transport": "direct",
            # Old entry so created_at never affects the direct checks.
            "created_at": time.monotonic() - 60,
        }

    def test_stalled_output_rebuilds_despite_alive_thread(self):
        player, sent, time = self._player()
        entry = self._direct_entry(
            time, output_age=jukebox.JukeboxPlayer.DIRECT_OUTPUT_STALL_TIMEOUT + 1)
        player.players["box"] = entry
        player.update()
        # The stalled route was torn down and a recovery resync went out so
        # the server re-offers the song at its current position.
        self.assertNotIn("box", player.players)
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][1], "jukebox_resync")

    def test_healthy_output_does_not_rebuild(self):
        player, sent, time = self._player()
        player.players["box"] = self._direct_entry(time, output_age=0.2)
        player.update()
        self.assertIn("box", player.players)
        self.assertEqual(sent, [])

    def test_stream_still_starting_is_not_rebuilt(self):
        # Still resolving / holding the lead-in: ready_event is unset and
        # nothing has played yet, so no output stamp exists. A slow resolve
        # legitimately takes many seconds; never rebuild a stream that has
        # not begun.
        player, sent, time = self._player()
        player.players["box"] = self._direct_entry(time, ready=False)
        player.update()
        self.assertIn("box", player.players)
        self.assertEqual(sent, [])

    def test_dead_direct_stream_with_failure_still_rebuilds(self):
        # Existing behavior preserved: an explicit mid-song failure (ffmpeg
        # exited early, resolve failed) rebuilds even without the stall path.
        player, sent, time = self._player()
        player.players["box"] = self._direct_entry(
            time, alive=False, failure="ffmpeg exited early (code 1)")
        player.update()
        self.assertNotIn("box", player.players)
        self.assertEqual(len(sent), 1)

    def test_finished_direct_stream_is_left_alone(self):
        # Natural end: thread finished with no failure_reason — await the
        # server's next-song timer, exactly as before the stall watchdog.
        player, sent, time = self._player()
        player.players["box"] = self._direct_entry(
            time, alive=False, ready=True, output_age=0.0)
        player.update()
        self.assertIn("box", player.players)
        self.assertEqual(sent, [])

    def test_stream_in_cleanup_is_not_rebuilt_as_stall(self):
        # A streamer whose thread finished sets running=False in _cleanup;
        # even with a stale stamp it must not be misread as an output stall.
        player, sent, time = self._player()
        player.players["box"] = self._direct_entry(
            time, alive=False, running=False,
            output_age=jukebox.JukeboxPlayer.DIRECT_OUTPUT_STALL_TIMEOUT + 5)
        player.update()
        self.assertIn("box", player.players)
        self.assertEqual(sent, [])


class TestDirectReclaimOutputStamp(unittest.TestCase):
    """AudioStreamer._reclaim_processed stamps audible progress only for
    jukebox direct streams (jukebox_player set) and only when the speakers
    actually consumed a buffer."""

    class _Buf:
        def set_data(self, data, sample_rate=None, format=None):
            self.data = data

    class _Ctx:
        def gen_buffer(self):
            return TestDirectReclaimOutputStamp._Buf()

    class _Src:
        def __init__(self, processed):
            self._processed = processed
            self.state = None

        @property
        def buffers_queued(self):
            return self._processed

        @property
        def buffers_processed(self):
            return self._processed

        def unqueue_buffers(self):
            if self._processed:
                self._processed -= 1
                return TestDirectReclaimOutputStamp._Buf()
            return None

        def play(self):
            pass

        def stop(self):
            pass

    @staticmethod
    def _streamer(processed):
        from types import SimpleNamespace
        from libs import music_bot as mb
        game = SimpleNamespace(
            audio_mngr=SimpleNamespace(context=TestDirectReclaimOutputStamp._Ctx())
        )
        return mb.AudioStreamer(
            game, "https://example.com/audio.mp3",
            TestDirectReclaimOutputStamp._Src(processed), bot=None,
        )

    def test_consumed_buffer_stamps_output_for_jukebox_direct(self):
        import time
        streamer = self._streamer(processed=3)
        streamer.jukebox_player = object()
        before = time.monotonic()
        streamer._reclaim_processed()
        self.assertIsNotNone(streamer.last_output_at)
        self.assertGreaterEqual(streamer.last_output_at, before)

    def test_no_consumption_keeps_stamp_none(self):
        streamer = self._streamer(processed=0)
        streamer.jukebox_player = object()
        streamer._reclaim_processed()
        self.assertIsNone(streamer.last_output_at)

    def test_non_jukebox_stream_is_not_stamped(self):
        # Personal Music Bot direct streams never set jukebox_player; their
        # output is not watched by the jukebox watchdog, so no stamp.
        streamer = self._streamer(processed=2)
        streamer._reclaim_processed()
        self.assertIsNone(streamer.last_output_at)


if __name__ == "__main__":
    unittest.main()
