"""The cabinet's machinery: a placed region that starts, runs, and winds down.

The clips themselves are the owner's to record; what these tests pin is the
contract around them:

* power on starts the spin-up once and loops the rotation **and the fan**, at the
  part of the cabinet's placed box nearest the listener, on the cabinet's own
  category with the engine's own sound-source distance model;
* the spin-up is heard first and whole, and the rotation and the fan join it only
  when its own **measured** length has run out -- silent underneath it, handed
  over over the last ``CROSSFADE`` seconds, never both at once; a clip whose
  length cannot be read is not waited for, and a cabinet switched off during its
  spin-up is not left catching;
* the level is a placed Sound Source's level -- smoothstep over 25 m measured to
  the nearest point of the box, height weighted 3.5x -- not a private 8 m/40 m
  curve, so a cabinet sounds like every other placed sound on the map;
* the level follows the listener every frame (`tick`), and a cabinet dragged
  several steps wide is heard along its whole front, from the nearest point;
* power off stops the loops **there and then** and says the wind-down once, on
  its own source, only where it could be heard; a cabinet that was never running
  says nothing, and a map change takes the sources without pretending anything
  was switched off;
* missing files are silence, not an error;
* the listener's ``Cabinet voice`` switch silences the *lines*, never the motor.
"""

import os
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from libs.jukebox import motor, voice


# What the real clips are: start.ogg 3.99 s, loop.ogg 3.34 s, room3.ogg 6.95 s.
DEFAULT_LENGTHS = {
    motor.START: 4.0,
    motor.LOOP: 3.34,
    motor.FAN: 6.95,
    motor.STOP: 2.8,
}


class FakeBuffer:
    def __init__(self, seconds):
        self.sec_length = seconds


class FakeClock:
    """A clock a test moves by hand: the handover is measured in seconds."""

    def __init__(self, now=0.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class FakeSource:
    def __init__(self, gain):
        self.gain = gain
        self.position = None
        self.paused = False
        self.created = 1
        self.reference_distance = None

    def play(self):
        self.paused = False
        self.created += 1

    def pause(self):
        self.paused = True


class FakeSound:
    def __init__(self, path, looping, volume):
        self.path = path
        self.looping = looping
        self.volume = volume
        self.source = FakeSource((volume / 100.0))
        self.destroyed = False

    def destroy(self):
        self.destroyed = True
        self.source = None


class FakeAudio:
    def __init__(self, position=(0.0, 0.0, 0.0), missing=(), category=100, lengths=None):
        self.position = position
        self.missing = set(missing)
        self.played = []
        self.volume_categories = {"jukebox": [category]}
        self.efx = SimpleNamespace(send=mock.Mock())
        self.lengths = dict(DEFAULT_LENGTHS)
        if lengths:
            self.lengths.update(lengths)

    def load_buffer(self, path, instrument=True):
        """What the cabinet measures a clip with: the file's own length."""
        if path in self.missing:
            return None
        return FakeBuffer(self.lengths.get(path, 0.0))

    def play_unbound(self, path, x, y, z, **options):
        if path in self.missing:
            return None
        sound = FakeSound(path, options.get("looping"), options.get("volume", 100))
        self.played.append((path, x, y, z, options, sound))
        return sound


class FakeMap:
    def __init__(self, reverb=None, zone=None):
        self.reverb = reverb
        self.zone = zone
        self.looked_up = []

    def get_reverb_at(self, x, y, z):
        self.looked_up.append((x, y, z))
        return None if self.reverb is None else SimpleNamespace(reverb=self.reverb)

    def get_jukebox(self, jid):
        return self.zone


class FakeGame:
    def __init__(self, audio=None, world=None):
        self.audio_mngr = audio
        self.queued = []
        self.gameplay = SimpleNamespace(map=world or FakeMap())

    def put(self, callback):
        self.queued.append(callback)
        callback()


def make(audio=None, world=None):
    game = FakeGame(audio, world)
    holder = motor.motors_for(game)
    game.clock = FakeClock()
    holder.clock = game.clock
    return game, holder


def zone(minx=0.0, maxx=0.0, miny=0.0, maxy=0.0, minz=0.0, maxz=0.0):
    return SimpleNamespace(minx=minx, maxx=maxx, miny=miny, maxy=maxy,
                           minz=minz, maxz=maxz)


def paths(audio):
    return [entry[0] for entry in audio.played]


def sounds(audio):
    return [entry[5] for entry in audio.played]


class StartTests(unittest.TestCase):
    def test_power_on_spins_up_once_and_loops_the_rotation_and_the_fan(self):
        audio = FakeAudio()
        _game, holder = make(audio, FakeMap(zone=zone()))
        self.assertTrue(holder.power("jb1", True, (0.0, 0.0, 0.0)))

        self.assertEqual(sorted(paths(audio)), sorted([motor.START, motor.LOOP, motor.FAN]))
        played = {entry[0]: entry for entry in audio.played}
        self.assertFalse(played[motor.START][4]["looping"])
        self.assertTrue(played[motor.LOOP][4]["looping"])
        self.assertTrue(played[motor.FAN][4]["looping"])
        # The spin-up is heard at the box's own level, and the loops are already
        # running underneath it -- silent, because it has not finished yet.
        for path in (motor.LOOP, motor.FAN):
            sound = played[path][5]
            self.assertEqual(played[path][4]["volume"], 0)
            self.assertAlmostEqual(sound.source.gain, 0.0, places=3)
        self.assertAlmostEqual(played[motor.START][5].source.gain, 1.0, places=3)
        for _path, x, y, z, options, _sound in audio.played:
            self.assertEqual((x, y, z), (0.0, 0.0, 0.0))
            self.assertEqual(options["cat"], motor.CATEGORY)
            self.assertEqual(options["reference_distance"], motor.REFERENCE_DISTANCE)

    def test_a_cabinet_is_heard_from_the_near_edge_of_the_box_it_was_placed_in(self):
        audio = FakeAudio(position=(8.0, 2.0, 0.0))
        _game, holder = make(audio, FakeMap(zone=zone(0.0, 4.0, 0.0, 2.0)))
        holder.power("jb1", True, (2.0, 1.0, 0.0))
        for _path, x, y, z, _options, _sound in audio.played:
            # The element counts both ends of its bounds, plus half a tile: a
            # cabinet placed 0..4 wide is 4.5 at its eastern edge.
            self.assertEqual((x, y, z), (4.5, 2.0, 0.0))

    def test_the_level_is_a_placed_sound_source_s_level(self):
        audio = FakeAudio(position=(13.0, 0.0, 0.0))
        _game, holder = make(audio, FakeMap(zone=zone()))
        holder.power("jb1", True, (0.0, 0.0, 0.0))
        # The eastern edge of a one-tile cabinet sits at 0.5, so 13.0 is 12.5 m
        # away: half of the 25 m reach, and half on the smoothstep curve.
        self.assertAlmostEqual(holder.motors["jb1"]["gain"], 0.5, places=3)


class HandoverTests(unittest.TestCase):
    """The spin-up first, then the rotation -- timed by the clip, not a constant."""

    def started(self, lengths=None):
        audio = FakeAudio(lengths=lengths)
        game, holder = make(audio, FakeMap(zone=zone()))
        holder.power("jb1", True, (0.0, 0.0, 0.0))
        return audio, game, holder

    def loops(self, audio):
        return [sound for sound in sounds(audio) if sound.looping]

    def test_the_rotation_is_held_silent_for_the_whole_spin_up(self):
        audio, game, holder = self.started()
        game.clock.advance(audio.lengths[motor.START] - motor.CROSSFADE - 0.1)
        holder.tick(0.0, 0.0, 0.0)
        for loop in self.loops(audio):
            self.assertEqual(loop.source.gain, 0.0)

    def test_the_rotation_comes_up_under_the_end_of_the_clip(self):
        audio, game, holder = self.started()
        clip = audio.lengths[motor.START]
        # Halfway through the handover the rotation and the fan are both up, and
        # neither of them has jumped to full volume.
        game.clock.advance(clip - motor.CROSSFADE / 2.0)
        holder.tick(0.0, 0.0, 0.0)
        for loop in self.loops(audio):
            self.assertAlmostEqual(loop.source.gain, 0.5, places=3)
        # At the clip's last sample the handover is complete, and the entry is
        # tidied away so the per-frame path is the plain region level again.
        game.clock.advance(motor.CROSSFADE / 2.0)
        holder.tick(0.0, 0.0, 0.0)
        for loop in self.loops(audio):
            self.assertAlmostEqual(loop.source.gain, 1.0, places=3)
        self.assertEqual(holder.motors["jb1"]["intro_until"], 0.0)

    def test_a_shorter_spin_up_hands_over_sooner(self):
        audio, game, holder = self.started(lengths={motor.START: 0.6})
        # A clip shorter than ``CROSSFADE`` fades across its whole length: half
        # way through it the rotation is half up, not still waiting.
        game.clock.advance(0.3)
        holder.tick(0.0, 0.0, 0.0)
        self.assertAlmostEqual(self.loops(audio)[0].source.gain, 0.5, places=3)
        game.clock.advance(0.3)
        holder.tick(0.0, 0.0, 0.0)
        self.assertAlmostEqual(self.loops(audio)[0].source.gain, 1.0, places=3)

    def test_a_length_that_cannot_be_read_is_not_waited_for(self):
        audio, _game, _holder = self.started(lengths={motor.START: 0.0})
        self.assertAlmostEqual(self.loops(audio)[0].source.gain, 1.0, places=3)

    def test_with_no_spin_up_clip_the_rotation_starts_at_once(self):
        audio = FakeAudio(missing={motor.START})
        _game, holder = make(audio, FakeMap(zone=zone()))
        holder.power("jb1", True, (0.0, 0.0, 0.0))
        self.assertAlmostEqual(self.loops(audio)[0].source.gain, 1.0, places=3)

    def test_the_spin_up_follows_the_listener_like_the_rotation(self):
        audio, _game, holder = self.started()
        spin_up = [sound for sound in sounds(audio) if sound.path == motor.START][0]
        holder.tick(13.0, 0.0, 0.0)
        # The engine's stepped ramp, from the near edge of the box: the spin-up is
        # a source of the cabinet's own while it sounds, not a fixed one-shot.
        self.assertAlmostEqual(spin_up.source.gain, 0.95, places=3)
        self.assertEqual(spin_up.source.position, (0.5, 0.0, 0.0))
        self.assertAlmostEqual(self.loops(audio)[0].source.gain, 0.0, places=3)

    def test_switching_off_during_the_spin_up_takes_it_with_the_machine(self):
        audio, _game, holder = self.started()
        spin_up = [sound for sound in sounds(audio) if sound.path == motor.START][0]
        self.assertTrue(holder.power("jb1", False, (0.0, 0.0, 0.0)))
        # A cabinet switched off while it is still catching is dead: only the
        # wind-down is left sounding, never a motor spinning up from a dead box.
        self.assertTrue(spin_up.destroyed)
        self.assertEqual(paths(audio).count(motor.STOP), 1)


class TickTests(unittest.TestCase):
    def test_the_level_follows_the_listener_every_frame(self):
        audio = FakeAudio()
        game, holder = make(audio, FakeMap(zone=zone()))
        holder.power("jb1", True, (0.0, 0.0, 0.0))
        game.clock.advance(audio.lengths[motor.START] + 0.01)
        holder.tick(0.0, 0.0, 0.0)
        loop = [sound for sound in sounds(audio) if sound.looping][0]
        self.assertAlmostEqual(loop.source.gain, 1.0, places=3)

        holder.tick(13.0, 0.0, 0.0)
        # One frame of the engine's stepped ramp, not a jump across the box.
        self.assertAlmostEqual(loop.source.gain, 0.95, places=3)

    def test_a_wide_cabinet_is_heard_along_its_whole_front(self):
        audio = FakeAudio()
        _game, holder = make(audio, FakeMap(zone=zone(0.0, 4.0, 0.0, 2.0)))
        holder.power("jb1", True, (2.0, 1.0, 0.0))
        loop = [sound for sound in sounds(audio) if sound.looping][0]
        holder.tick(-6.0, 1.0, 0.0)
        self.assertEqual(loop.source.position, (-0.5, 1.0, 0.0))
        holder.tick(9.0, 1.0, 0.0)
        self.assertEqual(loop.source.position, (4.5, 1.0, 0.0))

    def test_standing_on_the_box_hears_it_from_the_middle(self):
        audio = FakeAudio()
        _game, holder = make(audio, FakeMap(zone=zone(0.0, 4.0, 0.0, 2.0)))
        holder.power("jb1", True, (2.0, 1.0, 0.0))
        loop = [sound for sound in sounds(audio) if sound.looping][0]
        holder.tick(2.0, 1.0, 0.0)
        self.assertEqual(loop.source.position, (2.0, 1.0, 0.0))

    def test_out_of_earshot_pauses_and_walking_back_resumes(self):
        audio = FakeAudio()
        _game, holder = make(audio, FakeMap(zone=zone()))
        holder.power("jb1", True, (0.0, 0.0, 0.0))
        loops = [sound for sound in sounds(audio) if sound.looping]
        for _ in range(40):
            holder.tick(1000.0, 1000.0, 0.0)
        self.assertTrue(all(sound.source.paused for sound in loops))
        self.assertEqual(holder.motors["jb1"]["gain"], 0.0)

        holder.tick(0.0, 0.0, 0.0)
        self.assertFalse(any(sound.source.paused for sound in loops))

    def test_height_is_weighted_harder_than_ground_distance(self):
        audio = FakeAudio()
        _game, holder = make(audio, FakeMap(zone=zone()))
        holder.power("jb1", True, (0.0, 0.0, 0.0))
        # 7 steps up is 24.5 m of weighted distance: still just audible.
        holder.tick(0.0, 0.0, 7.0)
        self.assertGreater(holder.motors["jb1"]["gain"], 0.0)
        for _ in range(40):
            holder.tick(0.0, 0.0, 8.0)
        self.assertEqual(holder.motors["jb1"]["gain"], 0.0)

    def test_the_room_is_read_where_the_listener_hears_the_box(self):
        audio = FakeAudio()
        world = FakeMap(reverb="zone-slot", zone=zone(0.0, 4.0, 0.0, 0.0))
        _game, holder = make(audio, world)
        holder.power("jb1", True, (2.0, 0.0, 0.0))
        audio.efx.send.reset_mock()
        holder.tick(9.0, 0.0, 0.0)
        for call in audio.efx.send.call_args_list:
            self.assertEqual(call.args[1:], (motor.REVERB_SEND, "zone-slot"))
        self.assertEqual(world.looked_up[-1], (4.5, 0.0, 0.0))

    def test_power_on_twice_does_not_restart_a_running_motor(self):
        audio = FakeAudio()
        _game, holder = make(audio, FakeMap(zone=zone()))
        self.assertTrue(holder.power("jb1", True, (0.0, 0.0, 0.0)))
        self.assertFalse(holder.power("jb1", True, (0.0, 0.0, 0.0)))
        self.assertEqual(len(audio.played), 3)


class StopTests(unittest.TestCase):
    def test_power_off_stops_the_loops_there_and_then_and_winds_down(self):
        audio = FakeAudio()
        _game, holder = make(audio, FakeMap(zone=zone()))
        holder.power("jb1", True, (0.0, 0.0, 0.0))
        running = [sound for sound in sounds(audio) if sound.looping]
        self.assertTrue(holder.power("jb1", False, (0.0, 0.0, 0.0)))

        self.assertTrue(all(sound.destroyed for sound in running))
        self.assertEqual(holder.motors, {})
        self.assertEqual(paths(audio).count(motor.STOP), 1)
        stop = [sound for sound in sounds(audio) if sound.path == motor.STOP][0]
        self.assertFalse(stop.destroyed)

    def test_power_off_twice_is_quiet(self):
        audio = FakeAudio()
        _game, holder = make(audio, FakeMap(zone=zone()))
        holder.power("jb1", True, (0.0, 0.0, 0.0))
        self.assertTrue(holder.power("jb1", False, (0.0, 0.0, 0.0)))
        self.assertFalse(holder.power("jb1", False, (0.0, 0.0, 0.0)))
        self.assertEqual(paths(audio).count(motor.STOP), 1)

    def test_a_cabinet_that_was_never_running_says_nothing(self):
        audio = FakeAudio()
        _game, holder = make(audio, FakeMap(zone=zone()))
        self.assertFalse(holder.power("jb1", False, (0.0, 0.0, 0.0)))
        self.assertEqual(audio.played, [])

    def test_a_cabinet_out_of_earshot_does_not_wind_down(self):
        audio = FakeAudio(position=(1000.0, 1000.0, 0.0))
        _game, holder = make(audio, FakeMap(zone=zone()))
        holder.power("jb1", True, (0.0, 0.0, 0.0))
        holder.power("jb1", False, (0.0, 0.0, 0.0))
        self.assertNotIn(motor.STOP, paths(audio))

    def test_the_wind_down_comes_from_where_the_machine_stood(self):
        audio = FakeAudio(position=(6.0, 2.0, 0.0))
        _game, holder = make(audio, FakeMap(zone=zone(0.0, 4.0, 0.0, 2.0)))
        holder.power("jb1", True, (2.0, 1.0, 0.0))
        holder.power("jb1", False, None)
        entry = [entry for entry in audio.played if entry[0] == motor.STOP][0]
        self.assertEqual(entry[1:4], (4.5, 2.0, 0.0))

    def test_a_missing_wind_down_clip_is_silence_not_an_error(self):
        audio = FakeAudio(missing={motor.STOP})
        _game, holder = make(audio, FakeMap(zone=zone()))
        holder.power("jb1", True, (0.0, 0.0, 0.0))
        self.assertTrue(holder.power("jb1", False, (0.0, 0.0, 0.0)))
        self.assertNotIn(motor.STOP, paths(audio))

    def test_a_new_spin_up_takes_back_a_wind_down_still_sounding(self):
        audio = FakeAudio()
        _game, holder = make(audio, FakeMap(zone=zone()))
        holder.power("jb1", True, (0.0, 0.0, 0.0))
        holder.power("jb1", False, (0.0, 0.0, 0.0))
        windup = holder.windups["jb1"]
        self.assertTrue(holder.power("jb1", True, (0.0, 0.0, 0.0)))
        self.assertTrue(windup.destroyed)
        self.assertEqual(holder.windups, {})

    def test_release_hands_every_motor_and_wind_down_back(self):
        audio = FakeAudio()
        game, holder = make(audio, FakeMap(zone=zone()))
        holder.power("jb1", True, (0.0, 0.0, 0.0))
        holder.power("jb2", True, (1.0, 0.0, 0.0))
        running = [sound for sound in sounds(audio) if sound.looping]
        holder.power("jb1", False, (0.0, 0.0, 0.0))
        windup = holder.windups["jb1"]
        motor.release(game)
        self.assertTrue(all(sound.destroyed for sound in running))
        self.assertTrue(windup.destroyed)
        self.assertEqual(holder.motors, {})
        self.assertEqual(holder.windups, {})

    def test_a_map_change_never_pretends_a_machine_was_switched_off(self):
        audio = FakeAudio()
        game, holder = make(audio, FakeMap(zone=zone()))
        holder.power("jb1", True, (0.0, 0.0, 0.0))
        motor.release(game)
        self.assertNotIn(motor.STOP, paths(audio))


class FanTests(unittest.TestCase):
    def test_the_fan_rides_with_the_rotation(self):
        audio = FakeAudio()
        game, holder = make(audio, FakeMap(zone=zone()))
        holder.power("jb1", True, (0.0, 0.0, 0.0))
        game.clock.advance(audio.lengths[motor.START] + 0.01)
        fan = [sound for sound in sounds(audio) if sound.path == motor.FAN][0]
        self.assertTrue(fan.looping)
        holder.tick(13.0, 0.0, 0.0)
        holder.tick(13.0, 0.0, 0.0)
        # The fan is a part of the machine, not a sound of its own: the level,
        # the box and the room are the rotation's.
        self.assertLess(fan.source.gain, 1.0)
        self.assertGreater(fan.source.gain, 0.0)

    def test_the_fan_dies_with_the_power(self):
        audio = FakeAudio()
        _game, holder = make(audio, FakeMap(zone=zone()))
        holder.power("jb1", True, (0.0, 0.0, 0.0))
        fan = [sound for sound in sounds(audio) if sound.path == motor.FAN][0]
        holder.power("jb1", False, (0.0, 0.0, 0.0))
        self.assertTrue(fan.destroyed)
        self.assertEqual(holder.motors, {})

    def test_a_missing_fan_leaves_the_motor_running(self):
        audio = FakeAudio(missing={motor.FAN})
        _game, holder = make(audio, FakeMap(zone=zone()))
        self.assertTrue(holder.power("jb1", True, (0.0, 0.0, 0.0)))
        self.assertNotIn(motor.FAN, paths(audio))
        self.assertIn(motor.LOOP, paths(audio))

    def test_the_cabinet_slider_turns_the_machinery_down(self):
        audio = FakeAudio(category=50)
        _game, holder = make(audio, FakeMap(zone=zone()))
        holder.power("jb1", True, (0.0, 0.0, 0.0))
        self.assertAlmostEqual(holder.motors["jb1"]["gain"], 0.5, places=3)


class MissingFilesTests(unittest.TestCase):
    def test_a_cabinet_with_no_clips_is_simply_silent(self):
        audio = FakeAudio(missing={motor.START, motor.LOOP, motor.FAN})
        _game, holder = make(audio, FakeMap(zone=zone()))
        self.assertFalse(holder.power("jb1", True, (0.0, 0.0, 0.0)))
        self.assertEqual(holder.motors, {})

    def test_one_loop_is_enough_to_run(self):
        audio = FakeAudio(missing={motor.START, motor.FAN})
        _game, holder = make(audio, FakeMap(zone=zone()))
        self.assertTrue(holder.power("jb1", True, (0.0, 0.0, 0.0)))
        self.assertEqual(paths(audio), [motor.LOOP])

    def test_the_listener_s_voice_switch_does_not_silence_the_motor(self):
        audio = FakeAudio()
        _game, holder = make(audio, FakeMap(zone=zone()))
        with mock.patch("libs.options.get", return_value=False):
            self.assertFalse(voice.voice_enabled())
            self.assertTrue(holder.power("jb1", True, (0.0, 0.0, 0.0)))
        self.assertEqual(len(audio.played), 3)


class PacketTests(unittest.TestCase):
    def _handler(self, game):
        from libs.event_handeler import EventHandeler
        handler = EventHandeler.__new__(EventHandeler)
        handler.game = game
        handler.gameplay = game.gameplay
        return handler

    def test_the_power_packet_starts_and_stops_one_cabinet(self):
        audio = FakeAudio()
        game = FakeGame(audio)
        handler = self._handler(game)
        handler.jukebox_power({"id": "jb1", "on": True, "x": 7, "y": 8, "z": 1})
        self.assertEqual(audio.played[0][1:4], (7.0, 8.0, 1.0))
        running = [sound for sound in sounds(audio) if sound.looping]
        handler.jukebox_power({"id": "jb1", "on": False})
        self.assertTrue(all(sound.destroyed for sound in running))
        # The packet carried no coordinates, so the wind-down is placed where the
        # machine stood when it started, not at the origin.
        stop_entry = [entry for entry in audio.played if entry[0] == motor.STOP][0]
        self.assertEqual(stop_entry[1:4], (7.0, 8.0, 1.0))

    def test_a_join_starts_the_machinery_of_a_cabinet_already_on(self):
        audio = FakeAudio()
        game = FakeGame(audio)
        handler = self._handler(game)
        handler.jukebox_state({"jukeboxes": {
            "jb1": {"id": "jb1", "x": 3.0, "y": 4.0, "z": 0.0, "powered": True,
                    "current": None, "queue": []},
        }})
        self.assertEqual(len(audio.played), 3)
        self.assertEqual(audio.played[0][1:4], (3.0, 4.0, 0.0))

    def test_a_join_with_the_power_out_leaves_it_stopped(self):
        audio = FakeAudio()
        game = FakeGame(audio)
        handler = self._handler(game)
        holder = motor.motors_for(game)
        holder.power("jb1", True, (0.0, 0.0, 0.0))
        running = [sound for sound in sounds(audio) if sound.looping]
        handler.jukebox_state({"jukeboxes": {
            "jb1": {"id": "jb1", "x": 0.0, "y": 0.0, "z": 0.0, "powered": False,
                    "current": None, "queue": []},
        }})
        self.assertTrue(all(sound.destroyed for sound in running))

    def test_a_packet_with_no_cabinet_is_ignored(self):
        audio = FakeAudio()
        game = FakeGame(audio)
        handler = self._handler(game)
        handler.jukebox_power(None)
        handler.jukebox_power({"on": True})
        self.assertEqual(audio.played, [])

    def test_a_map_change_releases_the_machinery(self):
        audio = FakeAudio()
        game = FakeGame(audio)
        holder = motor.motors_for(game)
        game.gameplay.jukebox_player = None
        holder.power("jb1", True, (0.0, 0.0, 0.0))
        running = [sound for sound in sounds(audio) if sound.looping]
        from libs.event_handeler import EventHandeler
        EventHandeler._stop_jukebox_players_for_map_change(self._handler(game))
        self.assertTrue(all(sound.destroyed for sound in running))


class CabinetSwitchMenuTests(unittest.TestCase):
    """The interact screen: floor off a dark cabinet, staff-only way off."""

    class FakeMenu:
        def __init__(self, *args, **kwargs):
            self.items = []

        def add_items(self, items):
            self.items = list(items)

    def make(self, staff=False):
        box = {"id": "cab1", "x": 0.0, "y": 0.0, "z": 0.0,
               "cinema_mode": "auto", "current": None, "queue": [],
               "powered": False}
        gp = SimpleNamespace(
            jukebox_state={"jukeboxes": {"cab1": box}},
            player=SimpleNamespace(name="Kanya", x=0.0, y=0.0, z=0.0),
            substates=[], pop_last_substate=lambda: None,
            is_staff=staff, is_builder=False,
        )
        gp.add_substate = gp.substates.append
        sent = []
        game = SimpleNamespace(
            gameplay=gp, audio_mngr=FakeAudio(),
            network=SimpleNamespace(send=lambda channel, event, data: sent.append((event, data))),
        )
        gp.game = game
        return game, gp, sent

    def menu(self, game, items):
        with mock.patch("libs.menu.Menu", self.FakeMenu), \
                mock.patch("libs.menus.set_default_sounds"), \
                mock.patch("libs.jukebox.menu.neighbour_line", return_value=None), \
                mock.patch("libs.jukebox.menu.neighbour_note", return_value=None):
            items(game)
        return game.gameplay.substates[-1]

    def labels(self, menu):
        return [label() if callable(label) else label for label, _action in menu.items]

    def test_a_dark_cabinet_offers_only_its_switch(self):
        from libs import jukebox
        game, gp, sent = self.make()
        menu = self.menu(game, lambda g: jukebox.open_jukebox_switch_menu(g, gp, "cab1"))
        self.assertEqual(self.labels(menu), ["Turn on the cabinet", "Cancel"])
        with mock.patch("libs.jukebox.menu.speak"):
            dict((label, action) for label, action in menu.items)["Turn on the cabinet"]()
        self.assertEqual(sent, [("jukebox_switch", {"id": "cab1", "on": True})])

    def test_staff_are_offered_the_way_off_at_the_end(self):
        from libs import jukebox
        game, gp, sent = self.make(staff=True)
        menu = self.menu(game, lambda g: jukebox.open_jukebox_menu(g, gp))
        labels = self.labels(menu)
        self.assertEqual(labels[-1], "Cancel")
        self.assertEqual(labels[-2], "Turn off the cabinet (Staff only)")
        with mock.patch("libs.jukebox.menu.speak"):
            dict((label, action) for label, action in menu.items)[labels[-2]]()
        self.assertEqual(sent, [("jukebox_switch", {"id": "cab1", "on": False})])

    def test_a_player_is_never_offered_the_way_off(self):
        from libs import jukebox
        game, gp, _sent = self.make()
        menu = self.menu(game, lambda g: jukebox.open_jukebox_menu(g, gp))
        self.assertNotIn("Turn off the cabinet (Staff only)", self.labels(menu))

    def test_the_switch_packet_opens_the_dark_cabinet_screen(self):
        from libs.event_handeler import EventHandeler
        audio = FakeAudio()
        game = FakeGame(audio)
        game.gameplay.jukebox_state = {"jukeboxes": {"cab1": {"id": "cab1"}}}
        handler = EventHandeler.__new__(EventHandeler)
        handler.game = game
        handler.gameplay = game.gameplay
        with mock.patch("libs.jukebox.open_jukebox_switch_menu") as opened:
            handler.jukebox_switch_open({"id": "cab1"})
        opened.assert_called_once_with(game, game.gameplay, "cab1")


class TickWiringTests(unittest.TestCase):
    def test_the_module_tick_reaches_the_holder(self):
        audio = FakeAudio()
        game, holder = make(audio)
        holder.power("jb1", True, (0.0, 0.0, 0.0))
        game.clock.advance(audio.lengths[motor.START] + 0.01)
        loop = [sound for sound in sounds(audio) if sound.looping][0]
        motor.tick(game, 13.0, 0.0, 0.0)
        self.assertAlmostEqual(loop.source.gain, 0.95, places=3)

    def test_a_client_with_no_audio_has_nothing_to_tick(self):
        motor.tick(FakeGame(None), 0.0, 0.0, 0.0)


if __name__ == "__main__":
    unittest.main()
