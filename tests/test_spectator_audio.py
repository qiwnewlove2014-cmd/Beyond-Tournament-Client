"""Spectator audio and view: the ears, not the parked body.

A spectator's own character is parked where they joined the match and never
moves again, while the client's listener follows whoever they are watching.
Three things broke on that split, and each has a test here:

* The watched player's *facing* never reached the client -- the snapshot sent
  a field no server-side Player had, and a movement packet without an angle
  was read as facing 0 -- so the listener's head was stamped north on every
  step and a forward walk read as a sideways shuffle. A rotation on the spot
  sent nothing at all, which also left the shield's block arc stale.
* Relayed sounds were judged from the parked body (occlusion tier and the
  stereo path's distance cull), so the sounds that did arrive were muffled by
  walls nowhere near the listener and dropped outright past a distance the
  listener was not standing at.
* A map change moved the character (whose on_move drives the camera) rather
  than the ears, so the new map's ambience bed, zone and reverb were never
  entered until the followed player happened to take a step.
"""

import unittest
from types import SimpleNamespace

from libs.camera import Camera
from libs.event_handeler import EventHandeler
from libs.gameplay import Gameplay
from libs.objects.player import Player


class FakeSoundgroup:
    def __init__(self):
        self.effects = []
        self.filters = []

    def apply_effect(self, effect, slot):
        self.effects.append((effect, slot))

    def apply_filter(self, f, replace=True, clear=False):
        self.filters.append((f, replace, clear))


class FakeAmbience:
    def __init__(self):
        self.playing = False
        self.entered = 0
        self.left = 0

    def enter(self):
        self.entered += 1
        self.playing = True

    def leave(self):
        self.left += 1
        self.playing = False


class FakeMap:
    """A map that answers the listener's questions, by position."""

    def __init__(self, regions=(), reverb=None, zone=None):
        # regions: list of (min_x, max_x, ambience)
        self.regions = list(regions)
        self.reverb = reverb
        self.zone = zone
        self.asked = []

    def get_ambiences_at(self, x, y, z):
        self.asked.append((x, y, z))
        return [amb for lo, hi, amb in self.regions if lo <= x <= hi]

    def get_musics_at(self, x, y, z):
        return []

    def get_tile_at(self, x, y, z):
        return "air"

    def get_reverb_at(self, x, y, z):
        return self.reverb

    def get_zone_at(self, x, y, z):
        return self.zone


class FakeAudioMngr:
    def __init__(self):
        self.position = (0.0, 0.0, 0.0)
        self.orientation = (0.0, 0.0, 0.0)
        self.unbound = []
        self.stereo = []
        self.filters = []
        self.efx = SimpleNamespace(send=lambda *args, **kwargs: None)

    def gen_filter(self, type="LOWPASS"):
        flt = SimpleNamespace(set=lambda *args: None)
        self.filters.append(flt)
        return flt

    def release_filter(self, flt):
        pass

    def apply_filter(self, flt, exclude=None, replace=True, clear=False):
        pass

    def get_unbound_occlusion_filter(self):
        return FakeSoundgroup()  # any opaque object

    def get_light_unbound_occlusion_filter(self):
        return FakeSoundgroup()

    def play_unbound(self, path, x, y, z, *args, **kwargs):
        self.unbound.append((path, x, y, z, kwargs))

    def play_unbound_stereo_spatial(self, *args, **kwargs):
        self.stereo.append((args, kwargs))


class FakeClock:
    def __init__(self):
        self.elapsed = 0.0

    def restart(self):
        self.elapsed = 0.0


def make_focus(map_obj, **overrides):
    focus = SimpleNamespace(
        map=map_obj,
        x=0.0,
        y=0.0,
        z=0.0,
        dead=False,
        in_water=False,
        depth=1.0,
        recorded_depth=1.0,
        drownable=True,
        drown_clock=SimpleNamespace(restart=lambda: None),
        soundgroup=FakeSoundgroup(),
        vc_source=None,
        play_sound=lambda *args, **kwargs: None,
    )
    for key, value in overrides.items():
        setattr(focus, key, value)
    return focus


class TestListenerMap(unittest.TestCase):
    """The ears read the map they are standing on, not a stale focus object."""

    def make_camera(self, current_map, focus):
        cam = Camera.__new__(Camera)
        cam.game = SimpleNamespace(
            audio_mngr=FakeAudioMngr(),
            gameplay=SimpleNamespace(map=current_map),
            exclude_water=set(),
            automations=[],
        )
        cam.soundgroup = FakeSoundgroup()
        cam.x = cam.y = cam.z = 0.0
        cam.reverb = None
        cam.currentzone = ""
        cam.sonar = False
        cam._water_automation = None
        cam._water_gainhf = 1.0
        cam.focus_object = focus
        return cam

    def test_the_current_map_answers_the_listener_not_the_focus_objects_map(self):
        stale = FakeMap()
        current = FakeMap(regions=[(0, 10, FakeAmbience())])
        cam = self.make_camera(current, make_focus(stale))
        cam.apply_listener_state(1.0, 2.0, 0.0)
        self.assertEqual(stale.asked, [])
        self.assertEqual(len(current.asked), 1)
        self.assertEqual(current.regions[0][2].entered, 1)

    def test_refresh_listener_enters_the_room_it_now_stands_in(self):
        ambience = FakeAmbience()
        current = FakeMap(regions=[(0, 10, ambience)], zone="Main Hall")
        cam = self.make_camera(current, make_focus(FakeMap()))
        cam.x, cam.y, cam.z = 40.0, 40.0, 0.0
        cam.refresh_listener(4.0, 5.0, 0.0)
        self.assertEqual((cam.x, cam.y, cam.z), (4.0, 5.0, 0.0))
        self.assertEqual(cam.game.audio_mngr.position, (4.0, 5.0, 0.0))
        self.assertEqual(cam.soundgroup.position, (4.0, 5.0, 0.0))
        self.assertEqual(ambience.entered, 1)
        self.assertEqual(ambience.left, 0)

    def test_moving_still_leaves_the_ambience_it_walked_out_of(self):
        ambience = FakeAmbience()
        map_obj = FakeMap(regions=[(0, 4, ambience)])
        cam = self.make_camera(map_obj, make_focus(map_obj))
        cam.move(2.0, 0.0, 0.0)
        self.assertEqual(ambience.entered, 1)
        cam.move(9.0, 0.0, 0.0)
        self.assertEqual(ambience.left, 1)


class TestListenerObject(unittest.TestCase):
    def test_where_a_sound_is_judged_from_is_where_it_is_heard(self):
        gp = Gameplay.__new__(Gameplay)
        focus = SimpleNamespace(x=99.0)
        gp.player = SimpleNamespace(x=1.0)
        gp.camera = SimpleNamespace(focus_object=focus)
        self.assertIs(gp.listener_object(), focus)

    def test_a_game_with_no_camera_still_answers_with_the_player(self):
        gp = Gameplay.__new__(Gameplay)
        gp.player = SimpleNamespace(x=1.0)
        gp.camera = SimpleNamespace(focus_object=None)
        self.assertIs(gp.listener_object(), gp.player)


class TestRelayedSoundIsJudgedFromTheEars(unittest.TestCase):
    """Occlusion and stereo distance ask the listener, never the parked body."""

    PARKED = (0.0, 0.0, 0.0)
    EARS = (50.0, 50.0, 0.0)

    def make_handler(self):
        asked = []

        class TierMap(FakeMap):
            def occlusion_tier(self, source, listener):
                asked.append((tuple(source), tuple(listener)))
                return 0

        gameplay = Gameplay.__new__(Gameplay)
        gameplay.player = SimpleNamespace(x=self.PARKED[0], y=self.PARKED[1], z=self.PARKED[2])
        gameplay.camera = SimpleNamespace(
            focus_object=SimpleNamespace(x=self.EARS[0], y=self.EARS[1], z=self.EARS[2])
        )
        gameplay.map = TierMap()
        audio = FakeAudioMngr()
        handler = EventHandeler.__new__(EventHandeler)
        handler.gameplay = gameplay
        handler.game = SimpleNamespace(gameplay=gameplay, audio_mngr=audio, pong_mode=False)
        return handler, audio, asked

    def test_a_relayed_3d_sound_is_occluded_from_the_ears(self):
        handler, audio, asked = self.make_handler()
        handler.play_unbound({
            "_main_thread": True, "sound": "entities/zomby/amb/1.ogg",
            "x": 30.0, "y": 30.0, "z": 0.0, "volume": 100,
        })
        self.assertEqual(asked, [((30.0, 30.0, 0.0), self.EARS)])
        self.assertEqual(len(audio.unbound), 1)

    def test_the_stereo_path_drops_a_sound_by_the_ears_distance(self):
        handler, audio, asked = self.make_handler()
        handler.play_unbound({
            "_main_thread": True, "is_stereo_spatial": True,
            "sound": "piano/Piano.mf.C4.ogg", "x": 60.0, "y": 60.0, "z": 0.0,
        })
        # play_unbound_stereo_spatial(path, x, y, z, listener_x, listener_y,
        # listener_z, ...) -- the listener triple is what the distance cull and
        # the stereo placement use inside.
        args, _kwargs = audio.stereo[0]
        self.assertEqual(args[4:7], self.EARS)


class TestWatchedPlayerFacing(unittest.TestCase):
    def make_handler(self):
        entity = SimpleNamespace(faced=[], moved=[], vfacing=0, bfacing=0)
        entity.move = lambda *args: entity.moved.append(args)
        entity.face = lambda *args, **kwargs: entity.faced.append((args, kwargs))
        handler = EventHandeler.__new__(EventHandeler)
        handler.gameplay = SimpleNamespace(
            map=SimpleNamespace(entities={"bob": entity}),
            player=SimpleNamespace(name="me"),
        )
        return handler, entity

    def test_a_move_without_a_facing_leaves_the_watched_head_alone(self):
        handler, entity = self.make_handler()
        handler._apply_move({"name": "bob", "x": 1, "y": 2, "z": 3})
        self.assertEqual(len(entity.moved), 1)
        self.assertEqual(entity.faced, [])

    def test_a_move_with_a_facing_turns_the_entity_the_listener_follows(self):
        handler, entity = self.make_handler()
        handler._apply_move({"name": "bob", "x": 1, "y": 2, "z": 3, "angle": 90})
        (args, kwargs) = entity.faced[0]
        self.assertEqual(args[0], 90)
        self.assertTrue(kwargs["force"])

    def test_the_snapshot_turns_the_watched_player(self):
        entity = SimpleNamespace(faced=[], positions=[])
        entity.face = lambda h, v, b: entity.faced.append((h, v, b))
        entity.sync_network_position = lambda x, y, z: entity.positions.append((x, y, z))
        handler = EventHandeler.__new__(EventHandeler)
        handler.gameplay = SimpleNamespace(
            spectator_mode=True,
            player=SimpleNamespace(name="me"),
            map=SimpleNamespace(entities={"bob": entity}),
        )
        handler.spectator_update({
            "players": [{"name": "bob", "x": 1, "y": 2, "z": 3, "hfacing": 180}],
        })
        self.assertEqual(entity.positions, [(1, 2, 3)])
        self.assertEqual(entity.faced, [(180, 0, 0)])


class TestFacingReport(unittest.TestCase):
    def make_gameplay(self, pong=False, network=True):
        gp = Gameplay.__new__(Gameplay)
        sent = []
        gp.game = SimpleNamespace(
            pong_mode=pong,
            network=SimpleNamespace(send=lambda *args: sent.append(args)) if network else None,
        )
        gp.player = SimpleNamespace(hfacing=90.0)
        gp._facing_reported = None
        gp._facing_report_clock = FakeClock()
        return gp, sent

    def test_movement_packets_carry_the_facing(self):
        player = Player.__new__(Player)
        player.x, player.y, player.z, player.hfacing = 1.0, 2.0, 3.0, 90.0
        sent = []
        player.game = SimpleNamespace(network=SimpleNamespace(send=lambda *args: sent.append(args)))
        Player.send_movement(player, "walk")
        self.assertEqual(sent[0][2]["angle"], 90.0)
        self.assertEqual(sent[0][2]["mode"], "walk")

    def test_the_first_report_always_goes_out(self):
        gp, sent = self.make_gameplay()
        gp._report_facing()
        self.assertEqual(sent[0][2], {"angle": 90.0})

    def test_a_turn_inside_the_throttle_is_not_reported(self):
        gp, sent = self.make_gameplay()
        gp._report_facing()
        gp.player.hfacing = 89.0  # moved, but not yet and not far enough
        gp._report_facing()
        self.assertEqual(len(sent), 1)

    def test_a_real_turn_after_the_interval_is_reported(self):
        gp, sent = self.make_gameplay()
        gp._report_facing()
        gp._facing_report_clock.elapsed = Gameplay.FACING_REPORT_INTERVAL_MS + 1
        gp.player.hfacing = 180.0
        gp._report_facing()
        self.assertEqual(sent[-1][2], {"angle": 180.0})

    def test_a_tiny_turn_after_the_interval_is_still_not_reported(self):
        gp, sent = self.make_gameplay()
        gp._report_facing()
        gp._facing_report_clock.elapsed = Gameplay.FACING_REPORT_INTERVAL_MS + 1
        gp.player.hfacing = 90.5
        gp._report_facing()
        self.assertEqual(len(sent), 1)

    def test_the_arena_owns_the_facing_in_pong(self):
        gp, sent = self.make_gameplay(pong=True)
        gp._report_facing()
        self.assertEqual(sent, [])

    def test_a_client_with_no_network_reports_nothing(self):
        gp, sent = self.make_gameplay(network=False)
        gp._report_facing()
        self.assertEqual(sent, [])


class TestSpectatorMapChange(unittest.TestCase):
    """A map packet moves the character; a spectator's ears must move too."""

    def make_handler(self):
        refresh = []

        class Sink:
            def clear(self):
                pass

        gameplay = SimpleNamespace(
            spectator_mode=True,
            voice_channels={},
            map_name="oldmap",
            party_sync=None,
            megaphone=None,
            music_bot=None,
            camera=SimpleNamespace(
                refresh_listener=lambda x, y, z: refresh.append((x, y, z))
            ),
            player=SimpleNamespace(move=lambda *args, **kwargs: None),
            parser=SimpleNamespace(load=lambda *args, **kwargs: None),
        )
        handler = EventHandeler.__new__(EventHandeler)
        handler.gameplay = gameplay
        handler.game = SimpleNamespace(
            automations=[],
            exclude_water=set(),
            audio_mngr=SimpleNamespace(apply_filter=lambda *a, **k: None, instrument_samples=Sink()),
            network=SimpleNamespace(send=lambda *args: None),
        )
        handler._begin_map_audio_reload = lambda: None
        handler._finish_map_audio_reload = lambda: None
        handler._reset_instruments_for_map_change = lambda: None
        handler._stop_jukebox_players_for_map_change = lambda same_map=False: None
        return handler, refresh

    def test_the_new_maps_room_is_entered_at_the_new_position(self):
        handler, refresh = self.make_handler()
        handler._apply_parse_map({"name": "arena", "data": {}, "x": 4.0, "y": 5.0, "z": 0.0})
        self.assertEqual(refresh, [(4.0, 5.0, 0.0)])

    def test_a_playing_client_leaves_the_listener_alone(self):
        handler, refresh = self.make_handler()
        handler.gameplay.spectator_mode = False
        handler._apply_parse_map({"name": "arena", "data": {}, "x": 4.0, "y": 5.0, "z": 0.0})
        self.assertEqual(refresh, [])


if __name__ == "__main__":
    unittest.main()
