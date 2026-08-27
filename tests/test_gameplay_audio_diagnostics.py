"""Granular diagnostics exercise real methods with fake audio and clocks only."""

from collections import defaultdict
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from libs.audio_diagnostics import AudioDiagnostics, probe


class GameplayAudioDiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.now = 100.0
        self.lines = []
        self.order = []
        self.labels = {}
        fresh = AudioDiagnostics(wall_clock=lambda: self.now, cpu_clock=lambda: 0.0,
                                 sink=self.lines.append, enabled=True)
        state_patch = patch.dict(probe.__dict__, fresh.__dict__, clear=True)
        state_patch.start()
        self.addCleanup(state_patch.stop)

    def timed(self, name, duration=0.001, result=None):
        def action(*args, **kwargs):
            self.order.append(name)
            self.now += duration
            return result
        return Mock(side_effect=action)

    def capture(self, function, *args, **kwargs):
        @probe.frame
        def frame():
            probe.event("map.parse")
            try:
                return function(*args, **kwargs)
            finally:
                self.labels = {key: list(value) for key, value in probe._active().labels.items()}
        return frame()

    def gameplay(self, block=True):
        from libs.gameplay import Gameplay
        gp = Gameplay.__new__(Gameplay)
        gp._update_horse_wind = self.timed("wind")
        gp.megaphone = SimpleNamespace(update_megaphone_audio=self.timed("pa", .2))
        gp.wall_tone = SimpleNamespace(update=self.timed("wall"))
        gp.compass_turn_cue = SimpleNamespace(update=self.timed("compass"))
        gp.guitar = SimpleNamespace(active=False)
        gp.spectator_mode = False
        gp.player = SimpleNamespace(loop=self.timed("player"), dead=False,
                                    drownable=True, in_water=False)
        gp.source = SimpleNamespace(loop=self.timed("source"))
        gp.entity = SimpleNamespace()
        gp.map = SimpleNamespace(loop=self.timed("map"), entities={"entity": gp.entity},
                                 source_list=[gp.source])
        gp.camera = SimpleNamespace(focus_object=SimpleNamespace(x=1, y=2, z=3))
        gp.music_bot = SimpleNamespace(loop=self.timed("music"))
        gp.jukebox_player = SimpleNamespace(update=self.timed("jukebox"))
        gp.tracking_target = None
        gp.substates = [self.timed("substate", result=block)]
        gp.vehicle = SimpleNamespace(active=False)
        gp.keys_held = {}
        gp.keys_pressed = {}
        gp.keys_released = {}
        gp._dispatch_configurable_key_actions = self.timed("dispatch")
        gp.game = SimpleNamespace(mouse_buttons={"left": False, "middle": False, "right": False})
        return gp

    def test_gameplay_audio_order_arguments_and_blocking_substate(self):
        gp = self.gameplay()
        with patch("libs.gameplay.pygame.key.get_pressed") as keys:
            self.assertIsNone(self.capture(gp.update, []))
        keys.assert_not_called()
        self.assertEqual(self.order, ["wind", "pa", "wall", "compass", "player", "map",
                                      "source", "music", "jukebox", "substate"])
        gp.megaphone.update_megaphone_audio.assert_called_once_with(0, None)
        gp.source.loop.assert_called_once_with(1, 2, 3)
        self.assertFalse(gp.entity.player_dead)
        self.assertAlmostEqual(self.labels["gp.megaphone"][0], .2)
        for label in ("gp.player", "gp.map", "gp.sources", "gp.substate", "gp.jukebox"):
            self.assertEqual(self.labels[label][2], 1)
        self.assertFalse(any(label.startswith("gp.input") for label in self.labels))
        self.assertIn("cpu_ms=0.00", self.lines[0])

    def test_gameplay_held_and_discrete_input_keep_order_and_binding(self):
        import pygame
        gp = self.gameplay(block=False)
        held = self.timed("held")
        pressed = self.timed("pressed")
        released = self.timed("released")
        gp.keys_held = {7: held}
        gp.keys_pressed = {pygame.K_x: pressed}
        gp.keys_released = {pygame.K_x: released}
        events = [SimpleNamespace(type=pygame.KEYDOWN, key=pygame.K_x, mod=11),
                  SimpleNamespace(type=pygame.KEYUP, key=pygame.K_x, mod=12)]
        with patch("libs.gameplay.pygame.key.get_pressed", return_value=defaultdict(bool, {7: True})), \
             patch("libs.gameplay.pygame.key.get_mods", return_value=13), \
             patch("libs.gameplay.pygame.event.get_grab", return_value=True):
            self.assertIsNone(self.capture(gp.update, events))
        self.assertEqual(self.order[-4:], ["held", "dispatch", "pressed", "released"])
        held.assert_called_once_with(13)
        pressed.assert_called_once_with(11)
        released.assert_called_once_with(12)
        gp._dispatch_configurable_key_actions.assert_called_once_with(events[0])
        input_labels = {name: value[2] for name, value in self.labels.items()
                        if name.startswith("gp.input")}
        self.assertEqual(input_labels, {"gp.input_poll": 1, "gp.input_held": 1,
            "gp.input_dispatch": 1, "gp.input_pressed": 1, "gp.input_released": 1})

    def test_slow_poll_and_held_handlers_have_independent_static_labels(self):
        gp = self.gameplay(block=False)
        held = self.timed("private-handler", .205)
        gp.keys_held = {7: held}
        poll = self.timed("poll", .021, defaultdict(bool, {7: True}))
        with patch("libs.gameplay.pygame.key.get_pressed", poll), \
             patch("libs.gameplay.pygame.key.get_mods", return_value=13):
            self.capture(gp.update, [])
        poll.assert_called_once_with()
        held.assert_called_once_with(13)
        self.assertAlmostEqual(self.labels["gp.input_poll"][0], .021)
        self.assertAlmostEqual(self.labels["gp.input_held"][0], .205)
        self.assertEqual(self.labels["gp.input_held"][2], 1)
        self.assertNotIn("private-handler", self.lines[0])

    def test_instrument_consumed_event_keeps_identity_and_skips_general_input(self):
        import pygame
        gp = self.gameplay(block=False)
        gp.piano_mode = True
        gp._poll_piano_midi = self.timed("midi")
        gp.piano = SimpleNamespace(handle_event=self.timed("piano", result=True))
        event = SimpleNamespace(type=pygame.KEYDOWN, key=pygame.K_x, mod=11, unicode="ฝ")
        pressed = self.timed("pressed")
        gp.keys_pressed = {pygame.K_x: pressed}
        with patch("libs.gameplay.pygame.key.get_pressed", return_value=defaultdict(bool)), \
             patch("libs.gameplay.pygame.event.get_grab") as grab:
            self.capture(gp.update, [event])
        gp._poll_piano_midi.assert_called_once_with()
        gp.piano.handle_event.assert_called_once_with(event)
        self.assertIs(gp.piano.handle_event.call_args.args[0], event)
        self.assertEqual(event.unicode, "ฝ")
        pressed.assert_not_called()
        gp._dispatch_configurable_key_actions.assert_not_called()
        grab.assert_not_called()
        self.assertEqual(self.labels["gp.input_instrument"][2], 2)
        self.assertNotIn("gp.input_pressed", self.labels)

    def test_mouse_action_is_separate_and_preserves_relative_coordinates(self):
        import pygame
        gp = self.gameplay(block=False)
        gp.player.hfacing, gp.player.vfacing = 20, 5
        gp.player.face = self.timed("face", .01)
        event = SimpleNamespace(type=pygame.MOUSEMOTION, rel=(6, 0))
        with patch("libs.gameplay.pygame.key.get_pressed", return_value=defaultdict(bool)), \
             patch("libs.gameplay.pygame.event.get_grab", return_value=True):
            self.capture(gp.update, [event])
        gp.player.face.assert_called_once_with(23.0, 5)
        self.assertEqual(self.labels["gp.input_mouse"][2], 1)
        self.assertAlmostEqual(self.labels["gp.input_mouse"][0], .01)
        self.assertNotIn("gp.input_held", self.labels)

    def test_gameplay_exception_is_not_swallowed_or_replayed(self):
        gp = self.gameplay()
        failure = RuntimeError("test gameplay failure")
        gp.player.loop = Mock(side_effect=failure)
        with self.assertRaises(RuntimeError) as caught:
            self.capture(gp.update, [])
        self.assertIs(caught.exception, failure)
        gp.player.loop.assert_called_once_with()
        gp.map.loop.assert_not_called()
        self.assertEqual(self.labels["gp.player"][2], 1)
        self.assertIsNone(probe._active())

    def test_tracking_timing_keeps_beacon_and_reverb_calls(self):
        gp = self.gameplay()
        obj = SimpleNamespace(x=4, y=5, z=6)
        gp.tracking_target = ("entity", obj, (1, 2, 3))
        gp._validate_tracking_target = self.timed("validate", result=True)
        gp._beacon_pitch = self.timed("pitch", result=1.25)
        gp.tracking_clock = SimpleNamespace(elapsed=1200, restart=self.timed("restart"))
        source = SimpleNamespace()
        play = self.timed("beacon", .05, SimpleNamespace(source=source))
        send = self.timed("send")
        gp.game.audio_mngr = SimpleNamespace(play_unbound=play, efx=SimpleNamespace(send=send))
        gp.current_player_reverb_slot = object()
        self.capture(gp.update, [])
        self.assertEqual(gp.tracking_target, ("entity", obj, (4, 5, 6)))
        play.assert_called_once_with("ui/facing.ogg", 4, 5, 6, looping=False,
                                     volume=35, cat="miscelaneous", pitch=1.25)
        send.assert_called_once_with(source, 3, gp.current_player_reverb_slot)
        self.assertEqual(source.reference_distance, 15.0)
        self.assertEqual(source.rolloff_factor, .5)
        self.assertEqual(self.labels["gp.tracking"][2], 4)

    def test_map_loop_preserves_per_entity_order_and_occlusion_arguments(self):
        from libs.world_map import Map
        world = Map.__new__(Map)
        world.player = SimpleNamespace(dead=False)
        # Do not instantiate entities, contexts or native sources in this test.
        entities = []
        for name in ("one", "two"):
            entities.append(SimpleNamespace(loop=self.timed(name + ".loop"),
                water_check=self.timed(name + ".water"),
                soundgroup=SimpleNamespace()))
        world.entities = dict(zip(("one", "two"), entities))
        for name, item in world.entities.items():
            item.soundgroup.aclude_check = self.timed(name + ".occlusion")
        source = SimpleNamespace(soundgroup=SimpleNamespace(aclude_check=self.timed("source.occlusion")))
        pannable = SimpleNamespace(soundgroup=SimpleNamespace(aclude_check=self.timed("pannable.occlusion")))
        world.source_list, world.pannable_list = [source], [pannable]
        self.capture(world.loop)
        self.assertEqual(self.order, ["one.loop", "one.water", "one.occlusion", "two.loop",
            "two.water", "two.occlusion", "source.occlusion", "pannable.occlusion"])
        self.assertEqual(self.labels["map.entity_update"][2], 2)
        self.assertEqual(self.labels["map.entity_water"][2], 2)
        self.assertEqual(self.labels["map.occlusion"][2], 4)
        source.soundgroup.aclude_check.assert_called_once_with(world)
        self.order.clear()
        world.player.dead = True
        self.capture(world.loop)
        self.assertEqual(self.order, ["one.loop", "one.water", "two.loop", "two.water"])
        self.assertNotIn("map.occlusion", self.labels)

    def test_parser_static_buckets_preserve_kwargs_and_unknown_dispatch(self):
        from libs.map import Map_parser
        world = SimpleNamespace(destroy=self.timed("destroy"),
            spawn_reverb=self.timed("reverb", .05), spawn_platform=self.timed("platform"),
            spawn_private_type=self.timed("other"))
        parser = Map_parser(None, world)
        payload = {"minx": 1, "miny": 2, "minz": 3, "maxx": 4, "maxy": 5, "maxz": 6,
            "elements": [{"type": "reverb", "data": {"id": "private-id"}},
                         {"type": "platform", "data": {"sound": "private-path"}},
                         {"type": "private_type", "data": {"label": 7, "function": 8}},
                         {"type": "unsupported", "data": {}}]}
        self.capture(parser.load, payload, False)
        self.assertEqual(self.order, ["destroy", "reverb", "platform", "other"])
        world.destroy.assert_called_once_with(False)
        world.spawn_private_type.assert_called_once_with(label=7, function=8)
        self.assertIs(parser.map_data, payload)
        self.assertEqual((world.minx, world.maxz), (1, 6))
        self.assertEqual(set(self.labels), {"map.destroy", "map.spawn.reverb",
                                          "map.spawn.geometry", "map.spawn.other"})
        self.assertNotIn("private", self.lines[0])

    def test_parser_existing_error_recovery_continues_to_next_element(self):
        from libs.map import Map_parser
        world = SimpleNamespace(destroy=Mock(side_effect=ValueError("destroy test")),
            spawn_reverb=Mock(side_effect=ValueError("spawn test")),
            spawn_platform=self.timed("platform"))
        parser = Map_parser(None, world)
        payload = dict(minx=0, miny=0, minz=0, maxx=1, maxy=1, maxz=1,
            elements=[{"type": "reverb", "data": {}}, {"type": "platform", "data": {}}])
        with patch("libs.map.speak") as speech, patch("builtins.print"):
            self.assertIsNone(self.capture(parser.load, payload))
        world.spawn_platform.assert_called_once_with()
        speech.assert_called_once_with("Map Error: spawn test")
        self.assertEqual(self.labels["map.destroy"][2], 1)
        self.assertEqual(self.labels["map.spawn.reverb"][2], 1)

    def test_sound_source_start_and_resume_are_single_calls(self):
        from libs.world_map import SoundSource
        native = SimpleNamespace(play=self.timed("resume"), pause=Mock(), gain=0)
        sound = SimpleNamespace(source=native)
        group = SimpleNamespace(play=self.timed("load", .1, sound))
        world = SimpleNamespace(game=SimpleNamespace(audio_mngr=SimpleNamespace(
            volume_categories={"sound_source": [100]})), get_reverb_at=Mock(return_value=None))
        source = SoundSource.__new__(SoundSource)
        source.map, source.soundgroup, source.sound = world, group, None
        source.minx = source.miny = source.minz = 0
        source.maxx = source.maxy = source.maxz = 1
        source.path, source.volume = "private-sound.ogg", 75
        source.playing, source.fade_range, source.current_gain = False, 25.0, 0.0
        self.capture(source.loop, 0, 0, 0)
        group.play.assert_called_once_with("private-sound.ogg", True, cat="sound_source", volume=75)
        self.assertIs(source.sound, sound)
        self.assertAlmostEqual(self.labels["map.source_start"][0], .1)
        self.assertAlmostEqual(source.current_gain, .05)
        self.capture(source.loop, 0, 0, 0)
        self.assertNotIn("map.source_start", self.labels)
        self.assertEqual(group.play.call_count, 1)
        source.playing = False
        self.capture(source.loop, 0, 0, 0)
        native.play.assert_called_once_with()
        self.assertEqual(self.labels["map.source_start"][2], 1)
        self.assertNotIn("private", "\n".join(self.lines))


if __name__ == "__main__":
    unittest.main()
