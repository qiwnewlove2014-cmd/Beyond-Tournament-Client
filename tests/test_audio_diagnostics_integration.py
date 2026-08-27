"""Exercise real timing call sites with fake clocks/devices, never live audio."""

from collections import deque
from contextlib import contextmanager
import queue
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from libs.audio_diagnostics import AudioDiagnostics, probe


class AudioDiagnosticIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.now = 100.0
        self.lines = []
        fresh = AudioDiagnostics(wall_clock=lambda: self.now, cpu_clock=lambda: self.now,
                                 sink=self.lines.append, enabled=True)
        # Decorators already hold this singleton: replace its state, not its
        # identity, and restore it after each test. No writer thread is started.
        state_patch = patch.dict(probe.__dict__, fresh.__dict__, clear=True)
        state_patch.start()
        self.addCleanup(state_patch.stop)

    def advance(self, seconds):
        self.now += seconds

    def test_game_loop_keeps_callback_state_order_and_tick_result(self):
        from libs.game import Game
        game = Game.__new__(Game)
        order = []
        game.queue = queue.SimpleQueue()
        game.get = game.queue.get_nowait
        def callback():
            probe.event("jukebox.start")
            order.append("callback")
            self.advance(.025)
        game.queue.put(callback)
        game.queue.put(("test_value", 12))
        game.watchdog = SimpleNamespace(heartbeat=lambda: order.append("heartbeat"))
        game.lock = threading.RLock()
        game.delta = 16
        game.update = lambda delta: order.append("display")
        game.audio_mngr = SimpleNamespace(loop=lambda: order.append("audio"))
        game.automations = []
        game.title_clock = game.device_clock = SimpleNamespace(elapsed=0)
        game.stack = [lambda: order.append("state")]
        game.delayed_functions = {}
        game.framerate = 60
        game.clock = SimpleNamespace(get_fps=lambda: 60, tick=Mock(return_value=17))
        with patch("libs.game.pygame.event.get", return_value=[]), \
             patch("libs.game.keyboard_layout.normalize_events", return_value=[]), \
             patch("libs.game.options.get", side_effect=lambda name, default=None: default):
            self.assertIsNone(game.loop_function())
        self.assertEqual(order, ["callback", "heartbeat", "display", "audio", "state"])
        game.clock.tick.assert_called_once_with(60)
        self.assertEqual(game.delta, 17)
        self.assertEqual(game.test_value, 12)
        self.assertIn("game.callback", self.lines[0])
        self.assertIn("25.0", self.lines[0])

    def test_game_queue_termination_does_not_start_audio_or_tick(self):
        from libs.game import Game
        game = Game.__new__(Game)
        game.queue = queue.SimpleQueue()
        game.get = game.queue.get_nowait
        game.queue.put(None)
        self.assertIs(game.loop_function(), False)
        self.assertEqual(self.lines, [])

    def test_native_batch_commit_is_in_total_but_not_body(self):
        from libs.audio_manager import AudioManager
        audio = AudioManager.__new__(AudioManager)
        @contextmanager
        def batch():
            self.advance(.005)
            try:
                yield
            finally:
                self.advance(.025)
        audio.context = SimpleNamespace(batch=batch)
        audio._drain_audio_inbox = lambda: None
        audio._jukebox_receivers = deque()
        audio.instrument_samples = SimpleNamespace(pump=lambda **kw: self.advance(.002))
        audio.piano = audio.drums = SimpleNamespace(update=lambda: None)
        audio.unbound_sources = []
        audio.soundgroups = []
        @probe.frame
        def frame():
            probe.event("jukebox.start")
            audio.loop()
        frame()
        self.assertIn("audio.total", self.lines[0])
        self.assertIn("32.0", self.lines[0])
        self.assertIn("audio.body", self.lines[0])

    def test_first_relay_play_is_measured_without_extra_native_play_calls(self):
        from libs.jukebox_relay import JukeboxRelayReceiver
        from test_jukebox_relay_audio import Source, Buffer
        owner = threading.get_ident()
        left, right = Source(owner, (-2.5, 0, 0)), Source(owner, (2.5, 0, 0))
        disposed = []
        game = SimpleNamespace(audio_mngr=SimpleNamespace(
            context=SimpleNamespace(gen_buffer=lambda: Buffer(owner, disposed)),
            volume_categories={"jukebox": [100]}, position=None))
        receiver = JukeboxRelayReceiver(game, left, right, 100, 1, 1, 8, 40,
                                       clock=lambda: self.now)
        self.addCleanup(receiver.stop)
        for _ in range(4):
            receiver._pcm_frames.put((0, b"\0\0", b"\0\0"))
        play_left = left.play
        def slow_play():
            self.advance(.03)
            play_left()
        left.play = slow_play
        @probe.frame
        def frame():
            receiver.pump_audio(max_new_buffers=8)
        frame()
        self.assertEqual((left.play_calls, right.play_calls), (1, 1))
        self.assertIn("relay.first_play", self.lines[0])
        self.assertIn("relay.play", self.lines[0])
        self.assertIn("30.0", self.lines[0])

    def test_map_parse_times_loader_without_logging_packet_contents(self):
        from libs.event_handeler import EventHandeler
        handler = EventHandeler.__new__(EventHandeler)
        packet = {"name": "private-map", "data": "private-map-data", "x": 1, "y": 2, "z": 3}
        loader = Mock(side_effect=lambda data: self.advance(.04))
        mover = Mock()
        handler.gameplay = SimpleNamespace(voice_channels={}, map_name="private-map",
            parser=SimpleNamespace(load=loader), player=SimpleNamespace(move=mover))
        handler.game = SimpleNamespace(automations=[], exclude_water=[],
            audio_mngr=SimpleNamespace(apply_filter=Mock()), network=SimpleNamespace(send=Mock()))
        handler._begin_map_audio_reload = Mock()
        handler._reset_instruments_for_map_change = Mock()
        handler._stop_jukebox_players_for_map_change = Mock()
        handler._finish_map_audio_reload = Mock()
        with patch("libs.logger.log"):
            probe.frame(lambda: handler._apply_parse_map(packet))()
        loader.assert_called_once_with(packet["data"])
        mover.assert_called_once_with(1.0, 2.0, 3.0, play_sound=False)
        handler._stop_jukebox_players_for_map_change.assert_called_once_with(same_map=True)
        self.assertIn("map.parse", self.lines[0])
        self.assertIn("map.parser_load", self.lines[0])
        self.assertNotIn("private-map", self.lines[0])

    def test_disabled_map_parse_keeps_load_and_resync_without_transition_logs(self):
        from libs.event_handeler import EventHandeler
        handler = EventHandeler.__new__(EventHandeler)
        packet = {"name": "map", "data": "map-data", "x": 1, "y": 2, "z": 3}
        loader, mover, send = Mock(), Mock(), Mock()
        handler.gameplay = SimpleNamespace(voice_channels={}, map_name="map",
            parser=SimpleNamespace(load=loader), player=SimpleNamespace(move=mover))
        handler.game = SimpleNamespace(automations=[], exclude_water=[],
            audio_mngr=SimpleNamespace(apply_filter=Mock()), network=SimpleNamespace(send=send))
        handler._begin_map_audio_reload = Mock()
        handler._reset_instruments_for_map_change = Mock()
        handler._stop_jukebox_players_for_map_change = Mock()
        handler._finish_map_audio_reload = Mock()
        with patch.object(probe, "enabled", False), patch("libs.logger.log") as log:
            probe.frame(lambda: handler._apply_parse_map(packet))()
        log.assert_not_called()
        loader.assert_called_once_with("map-data")
        mover.assert_called_once_with(1.0, 2.0, 3.0, play_sound=False)
        self.assertEqual(send.call_args.args[1], "jukebox_resync")
        handler._finish_map_audio_reload.assert_called_once()
        self.assertEqual(self.lines, [])


if __name__ == "__main__":
    unittest.main()
