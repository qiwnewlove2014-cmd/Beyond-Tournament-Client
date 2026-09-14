"""Live instruments played out of a cabinet's room.

A room is built to play a song; these cover the other thing that happens in a
hall -- someone picks up a drumstick. A live note is not a stream, so it is
played *at* the room's speakers rather than pushed through the room's queue,
and the point of these tests is that it is shaped by the room's own numbers
(its distance ramp, the map's per-speaker level and trim, the wall between)
instead of by a second, unshaped copy of the note.
"""

import os
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from libs.audio.cinema import ROOM_MAX_DISTANCE, LiveRoomRouter
from libs.audio.cinema import live as cinema_live
from libs.world_map import Map

ANCHOR = (10.0, 20.0, 0.0)

# Every timer the game was asked to run, so a test can let them fire.
LATER = []


class FakeSource:
    def __init__(self):
        self.position = None
        self.pitch = None
        self.gain = 1.0
        self.direct_filter = None
        self.reference_distance = None
        self.max_distance = None
        self.rolloff_factor = None
        self.spatialize = None
        self.direct_channels = None
        self.buffers_queued = 0
        self.buffers_processed = 0

    def play(self):
        return None

    def stop(self):
        return None

    def delete(self):
        return None

    def queue_buffers(self, buffer):
        self.buffers_queued += 1

    def unqueue_buffers(self):
        if self.buffers_processed <= 0:
            return None
        self.buffers_processed -= 1
        return []


class FakeContext:
    def __init__(self):
        self.created = []

    def gen_source(self, **kwargs):
        source = FakeSource()
        self.created.append(source)
        return source

    def gen_buffer(self):
        return SimpleNamespace(set_data=lambda *a, **k: None)


class FakeEfx:
    def __init__(self):
        self.sends = []

    def send(self, source, index, slot, filter=None):
        self.sends.append((source, index, slot))


class FakeAudio:
    def __init__(self, position=(10.0, 25.0, 0.0)):
        self.position = position
        self.volume_categories = {"jukebox": [100], "music": [100],
                                  "miscelaneous": [100], "master": [100]}
        self.played = []          # room copies (one sample per speaker)
        self.direct = []          # the instrument's own hit, at its own spot
        self.filter = []
        self.sends = []
        self.context = FakeContext()
        self.efx = FakeEfx()

    def play_unbound(self, path, x, y, z, **kwargs):
        self.played.append((path, (x, y, z), kwargs))
        return SimpleNamespace(source=FakeSource())

    def play_unbound_stereo_spatial(self, path, x, y, z, lx, ly, lz, **kwargs):
        self.direct.append((path, (x, y, z), kwargs))
        return SimpleNamespace(source=FakeSource())

    def gen_filter(self, kind, *params):
        return ("filter", kind, params)


# The listener's own option (libs/audio/cinema/live.py): whether this client
# also plays live instruments at the speakers of the room around a cabinet.
# Set straight into ``options.prefs`` so a test never touches the player's
# settings file, and put back by the module so a test cannot decide what the
# next module hears.
LIVE_KEY = "cinema_live_instruments"
_SNAPSHOT = None


def setUpModule():
    global _SNAPSHOT
    from libs import options
    _SNAPSHOT = options.prefs.get(LIVE_KEY)


def tearDownModule():
    from libs import options
    if _SNAPSHOT is None:
        options.prefs.pop(LIVE_KEY, None)
    else:
        options.prefs[LIVE_KEY] = _SNAPSHOT


class FakeBot:
    """The bits of the Music Bot an instrument asks for (its volume only).

    It used to hold the live-instrument gate as well; that moved to the
    listener's own option, which is what both instruments ask now, so there is
    no bot-side switch left to fake.
    """

    def __init__(self, volume=50):
        self.volume = volume


def make_game(speakers=(("front_l", 6.5, 26.5), ("front_r", 13.5, 26.5)),
              cabinets=(("j1", ANCHOR),), position=(10.0, 25.0, 0.0),
              bot=None, modes=None, trims=None, live=True):
    """A map with cabinets and cinema speakers, and one performer standing on it.

    The speakers are placed with the labels the room expects, so a real
    resolver runs; nothing here hand-builds a placement. ``live`` arms the
    listener's option -- on is the shipped default, and off is a listener who
    asked for a plain concert.
    """
    from libs import options
    options.prefs[LIVE_KEY] = bool(live)
    game = SimpleNamespace()
    game.audio_mngr = FakeAudio(position)
    map_obj = Map(game)
    trims = dict(trims or {})
    for name, x, y in speakers:
        level, delay = trims.get(name, (100.0, 0.0))
        map_obj.spawn_cinemaSpeaker(minx=x - 0.5, maxx=x + 0.5, miny=y - 0.5,
                                    maxy=y + 0.5, minz=0, maxz=1, id=name,
                                    channel=name, level=level, delay=delay)
    map_obj.jukebox_list = [SimpleNamespace(id=cabinet_id, center=center)
                            for cabinet_id, center in cabinets]
    modes = dict(modes or {})
    player = SimpleNamespace(cinema_mode=lambda cabinet_id: modes.get(cabinet_id, "auto"),
                             occlusion_tier=lambda *args: 0)
    game.gameplay = SimpleNamespace(map=map_obj, music_bot=bot or FakeBot(),
                                    jukebox_player=player,
                                    game=game, megaphone=None)
    game.call_after = lambda ms, fn: LATER.append((ms, fn))
    return game


class LiveInstrumentSwitchTests(unittest.TestCase):
    """The option itself: on for a new listener, and one answer for both parts."""

    def setUp(self):
        from libs import options
        self._previous = options.prefs.get(LIVE_KEY)

    def tearDown(self):
        from libs import options
        if self._previous is None:
            options.prefs.pop(LIVE_KEY, None)
        else:
            options.prefs[LIVE_KEY] = self._previous

    def test_it_is_on_for_a_listener_that_has_never_chosen(self):
        from libs import options
        options.prefs.pop(LIVE_KEY, None)
        options.prefs.pop(cinema_live.LEGACY_OPTION, None)
        self.assertTrue(cinema_live.live_instruments_enabled())

    def test_a_choice_made_under_the_old_name_still_counts(self):
        from libs import options
        options.prefs.pop(LIVE_KEY, None)
        options.prefs[cinema_live.LEGACY_OPTION] = False
        self.assertFalse(cinema_live.live_instruments_enabled())
        options.prefs[LIVE_KEY] = True
        self.assertTrue(cinema_live.live_instruments_enabled())

    def test_the_saved_choice_is_the_one_the_instruments_read(self):
        from libs import options
        with mock.patch.object(options, "set") as setter:
            self.assertTrue(cinema_live.set_live_instruments(False))
        setter.assert_called_once_with(LIVE_KEY, False)
        # ...and that key is the one the instruments read.
        options.prefs[LIVE_KEY] = False
        self.assertFalse(cinema_live.live_instruments_enabled())


class LiveRoomRoutingTests(unittest.TestCase):
    """Which room a performance comes out of."""

    def setUp(self):
        LATER.clear()

    def test_the_note_goes_to_the_cabinet_the_performer_stands_at(self):
        game = make_game(cabinets=(("j1", (10.0, 20.0, 0.0)),
                                   ("j2", (200.0, 20.0, 0.0))))
        router = LiveRoomRouter(game)
        cabinet_id, plan = router.route_for((10.0, 25.0, 0.0))
        self.assertEqual(cabinet_id, "j1")
        self.assertEqual(plan.profile_name, "front_only")

    def test_a_cabinet_the_map_set_to_off_is_not_a_target(self):
        """The map's decision about a cabinet outranks a performer standing in it."""
        game = make_game(modes={"j1": "off"})
        self.assertIsNone(LiveRoomRouter(game).route_for((10.0, 25.0, 0.0)))

    def test_a_map_with_no_cabinet_has_nowhere_to_play(self):
        game = make_game(cabinets=())
        self.assertIsNone(LiveRoomRouter(game).route_for((10.0, 25.0, 0.0)))
        self.assertEqual(LiveRoomRouter(game).speaker_terms((10.0, 25.0, 0.0)), [])

    def test_gain_follows_the_rooms_own_distance_ramp(self):
        game = make_game()
        router = LiveRoomRouter(game)
        # A listener standing among the speakers: everything the room has, full.
        terms = dict((slot, gain) for slot, _pos, gain, _delay, _tier
                     in router.speaker_terms((10.0, 25.0, 0.0), (10.0, 25.0, 0.0)))
        self.assertEqual(set(terms), {"front_l", "front_r"})
        for gain in terms.values():
            self.assertAlmostEqual(gain, 1.0, places=6)
        # Far past the room's own reach: nothing, without any special case.
        away = (10.0 + ROOM_MAX_DISTANCE + 40.0, 25.0, 0.0)
        self.assertEqual(router.speaker_terms((10.0, 25.0, 0.0), away), [])

    def test_the_rooms_own_level_and_trim_reach_the_note(self):
        game = make_game(trims={"front_l": (50.0, 40.0)})
        terms = dict((slot, (gain, delay)) for slot, _pos, gain, delay, _tier
                     in LiveRoomRouter(game).speaker_terms((10.0, 25.0, 0.0),
                                                           (10.0, 25.0, 0.0)))
        self.assertAlmostEqual(terms["front_l"][0], 0.5, places=6)
        self.assertAlmostEqual(terms["front_l"][1], 40.0, places=6)
        self.assertAlmostEqual(terms["front_r"][1], 0.0, places=6)

    def test_a_speaker_behind_a_wall_is_muffled_not_dropped(self):
        """The room muffles a wall on the song; a live note must match it."""
        game = make_game()
        provider = lambda position, listener, max_distance: 2
        terms = LiveRoomRouter(game).speaker_terms(
            (10.0, 25.0, 0.0), (10.0, 25.0, 0.0), occlusion_provider=provider)
        self.assertEqual(len(terms), 2)
        self.assertTrue(all(tier == 2 for _s, _p, _g, _d, tier in terms))

    def test_the_room_is_resolved_at_most_once_a_second(self):
        game = make_game()
        router = LiveRoomRouter(game)
        with mock.patch.object(cinema_live, "preview_room",
                               side_effect=cinema_live.preview_room) as preview:
            router.route_for((10.0, 25.0, 0.0))
            router.route_for((10.0, 25.1, 0.0))     # still standing still
            self.assertEqual(preview.call_count, 1)
            router.route_for((60.0, 25.0, 0.0))     # walked away: re-resolve
            self.assertEqual(preview.call_count, 2)


class LiveNotePlaybackTests(unittest.TestCase):
    """Playing one note at the room's speakers."""

    def setUp(self):
        LATER.clear()

    def test_one_sample_per_speaker(self):
        game = make_game()
        played = []
        spoken = cinema_live.route_to_room(
            game, (10.0, 25.0, 0.0),
            lambda x, y, z, gain, tier, delay: played.append((x, y, z, gain)))
        self.assertEqual(spoken, 2)
        self.assertEqual(len(played), 2)
        self.assertTrue(all(gain > 0.0 for _x, _y, _z, gain in played))

    def test_a_trimmed_speaker_is_scheduled_rather_than_struck_now(self):
        """The trim is latency the room holds, so the note waits it out."""
        game = make_game(trims={"front_l": (100.0, 40.0)})
        played = []
        count = cinema_live.route_to_room(
            game, (10.0, 25.0, 0.0),
            lambda x, y, z, gain, tier, delay: played.append(delay),
            schedule=game.call_after)
        self.assertEqual(count, 2)
        self.assertEqual(len(played), 1)              # the un-trimmed speaker
        self.assertEqual([ms for ms, _fn in LATER], [40.0])
        LATER[0][1]()                                  # ...and it does play
        self.assertEqual(len(played), 2)
        self.assertEqual(played[1], 40.0)

    def test_a_note_that_is_no_longer_wanted_is_not_played(self):
        """A key released while a trimmed speaker waits must stay released.

        The trim is a timer, and a timer can outlive the note that armed it:
        without this the room copy plays with no note under it and nothing
        left that could ever stop it -- a piano key heard forever.
        """
        game = make_game(trims={"front_l": (100.0, 40.0), "front_r": (100.0, 40.0)})
        played = []
        cinema_live.route_to_room(
            game, (10.0, 25.0, 0.0),
            lambda x, y, z, gain, tier, delay: played.append(delay),
            schedule=game.call_after, wanted=lambda: True)
        self.assertEqual(played, [])            # both waited for their trim
        self.assertEqual(len(LATER), 2)
        for _ms, fire in LATER:                 # ...and both still wanted
            fire()
        self.assertEqual(played, [40.0, 40.0])

        played.clear()
        LATER.clear()
        cinema_live.route_to_room(
            game, (10.0, 25.0, 0.0),
            lambda x, y, z, gain, tier, delay: played.append(delay),
            schedule=game.call_after, wanted=lambda: False)
        for _ms, fire in LATER:                 # released in between: silent
            fire()
        self.assertEqual(played, [])

    def test_a_note_that_reaches_nobody_costs_nothing(self):
        game = make_game(cabinets=())
        played = []
        self.assertEqual(cinema_live.route_to_room(
            game, (10.0, 25.0, 0.0),
            lambda *args: played.append(args)), 0)
        self.assertEqual(played, [])


class LiveGainAgreesWithTheRoomTests(unittest.TestCase):
    """One curve, not two: a speaker is as loud for the band as for the song."""

    def test_the_live_gain_equals_the_banks_gain(self):
        from libs.audio.cinema import CinemaRenderer, CinemaSpeakerBank
        game = make_game()
        router = LiveRoomRouter(game)
        _cid, plan = router.route_for((10.0, 25.0, 0.0))
        renderer = CinemaRenderer(ANCHOR, plan.profile_name, specs=plan.specs)
        bank = CinemaSpeakerBank(game, renderer, occlusion_provider=lambda *a: 0)
        for listener in ((10.0, 25.0, 0.0), (10.0, 26.5, 0.0), (60.0, 25.0, 0.0)):
            live = dict((slot, gain) for slot, _pos, gain, _delay, _tier
                        in router.speaker_terms((10.0, 25.0, 0.0), listener))
            for slot in bank.slot_sources:
                self.assertAlmostEqual(live.get(slot, 0.0),
                                       bank.distance_gain(slot, listener),
                                       places=6,
                                       msg=f"{slot} at {listener}")


class PianoThroughTheRoomTests(unittest.TestCase):
    """The instrument hook itself: off means off, on means at the speakers."""

    def setUp(self):
        LATER.clear()

    def piano(self, game):
        from libs.piano import PianoAudio
        piano = PianoAudio(game.audio_mngr)
        piano.gameplay = game.gameplay
        return piano

    def test_off_plays_nowhere_near_a_cabinet(self):
        game = make_game(live=False)
        piano = self.piano(game)
        self.assertEqual(piano.route_to_cinema_room("peer", "C4", 10.0, 25.0, 0.0, 300), 0)
        self.assertEqual(game.audio_mngr.played, [])

    def test_on_plays_the_note_at_every_speaker(self):
        game = make_game()
        piano = self.piano(game)
        spoken = piano.route_to_cinema_room("peer", "C4", 10.0, 25.0, 0.0, 300)
        self.assertEqual(spoken, 2)
        self.assertEqual(len(game.audio_mngr.played), 2)
        _path, _spot, kwargs = game.audio_mngr.played[0]
        # Flat at the source: the room's own ramp already shaped this note.
        self.assertEqual(kwargs["rolloff"], 0.0)
        # ...and the note is tracked so its note-off fades the room with it.
        self.assertIn("cin-peer-C4", piano.active_piano_notes)

    def test_a_regular_player_hears_the_band_through_the_room(self):
        """No rank in this path at all: the switch is the listener's own.

        Feeding a room with a *song* is Developer/Contributor because it takes
        a cabinet over; a note played at a room's speakers by this client
        takes nothing, so a player with no staff flags is routed like anyone
        else once their own option says so.
        """
        game = make_game()
        del game.gameplay.music_bot
        piano = self.piano(game)
        self.assertEqual(piano.route_to_cinema_room("peer", "C4", 10.0, 25.0, 0.0, 300), 2)
        self.assertEqual(len(game.audio_mngr.played), 2)

    def test_note_off_fades_the_rooms_copy_too(self):
        game = make_game()
        piano = self.piano(game)
        piano.route_to_cinema_room("peer", "C4", 10.0, 25.0, 0.0, 300)
        with mock.patch.object(piano, "_schedule_filter_cleanup"):
            piano.stop_note("peer", "C4")
        self.assertNotIn("cin-peer-C4", piano.active_piano_notes)

    def test_a_key_released_inside_the_trim_never_comes_out_of_the_room(self):
        """The reported bug: a quick tap left a note ringing at the cabinet.

        Every speaker of this room carries a trim, so every copy is armed as a
        timer; releasing the key before the timer fires used to leave a note
        with no note-off left to reach it.
        """
        game = make_game(trims={"front_l": (100.0, 40.0), "front_r": (100.0, 40.0)})
        piano = self.piano(game)
        piano.route_to_cinema_room("peer", "C4", 10.0, 25.0, 0.0, 300)
        self.assertEqual(game.audio_mngr.played, [])      # still waiting
        with mock.patch.object(piano, "_schedule_filter_cleanup"):
            piano.stop_note("peer", "C4")                  # the quick release
        for _ms, fire in LATER:
            fire()
        self.assertEqual(game.audio_mngr.played, [])

    def test_a_held_key_still_reaches_a_trimmed_speaker(self):
        game = make_game(trims={"front_l": (100.0, 40.0)})
        piano = self.piano(game)
        piano.route_to_cinema_room("peer", "C4", 10.0, 25.0, 0.0, 300)
        self.assertEqual(len(game.audio_mngr.played), 1)   # the untrimmed one
        for _ms, fire in LATER:
            fire()
        self.assertEqual(len(game.audio_mngr.played), 2)   # the trimmed one too


class DrumsThroughTheRoomTests(unittest.TestCase):
    """A kit in a hall is heard through the hall."""

    def setUp(self):
        LATER.clear()

    def drums(self, game):
        from libs.drums import DrumAudio
        drums = DrumAudio(game.audio_mngr)
        drums.gameplay = game.gameplay
        return drums

    def test_a_choked_hit_is_marked_retired(self):
        """The mark a pending room copy reads to drop itself."""
        import collections
        game = make_game()
        drums = self.drums(game)
        record = {"created": 0.0, "sounds": [], "retired": False}
        drums.active_voices[("peer", 4)] = collections.deque([record])
        with mock.patch.object(drums, "_schedule_fade"):
            drums._remove_record(("peer", 4), record, 0.05)
        self.assertTrue(record["retired"])

    def test_the_room_copies_are_owned_by_the_hit_that_spawned_them(self):
        """A choke has to reach every speaker, including the ones that waited.

        The reported bug: the copies for trimmed speakers are spawned after
        the route returns, so a list nobody kept left them outside the record
        a choke fades -- the open hat went on ringing at two or three of the
        speakers while the rest of the hit was silenced.
        """
        game = make_game(trims={"front_l": (100.0, 40.0), "front_r": (100.0, 40.0)})
        drums = self.drums(game)
        drums.play_hit("peer", 4, 10.0, 25.0, 0.0, 10.0, 25.0, 0.0, 100.0,
                       kit="default")
        self.assertEqual(game.audio_mngr.played, [])      # waiting on the trim
        record = list(drums.active_voices[("peer", 4)])[0]
        for _ms, fire in LATER:
            fire()
        self.assertEqual(len(game.audio_mngr.played), 2)
        # The record owns the hit and both room copies.
        self.assertEqual(len(record["sounds"]), 3)

        # The pedal: the open hat is choked, and every copy goes with it.
        drums.play_hit("peer", 5, 10.0, 25.0, 0.0, 10.0, 25.0, 0.0, 100.0,
                       kit="default")
        self.assertTrue(record["retired"])
        faded = {id(entry["sound"]) for entry in drums._fades}
        for sound in record["sounds"]:
            self.assertIn(id(sound), faded, "a speaker was left out of the choke")

    def test_an_open_hat_choked_inside_the_trim_never_reaches_the_room(self):
        """Fast hats: the choke lands while a trimmed speaker is still waiting."""
        import collections
        game = make_game(trims={"front_l": (100.0, 40.0), "front_r": (100.0, 40.0)})
        drums = self.drums(game)
        record = {"created": 0.0, "sounds": [], "retired": False}
        drums.active_voices[("peer", 4)] = collections.deque([record])
        drums.route_to_cinema_room("peer", 4, 10.0, 25.0, 0.0, 100.0,
                                   wanted=lambda: not record["retired"])
        self.assertEqual(game.audio_mngr.played, [])
        with mock.patch.object(drums, "_schedule_fade"):
            drums._choke_open_hat("peer")                     # the closed hat
        for _ms, fire in LATER:
            fire()
        self.assertEqual(game.audio_mngr.played, [])

    def test_off_plays_nowhere_near_a_cabinet(self):
        game = make_game(live=False)
        self.assertEqual(self.drums(game).route_to_cinema_room(
            "peer", 0, 10.0, 25.0, 0.0, 100.0), [])
        self.assertEqual(game.audio_mngr.played, [])

    def test_on_spawns_the_hit_at_every_speaker(self):
        game = make_game()
        sounds = self.drums(game).route_to_cinema_room(
            "peer", 0, 10.0, 25.0, 0.0, 100.0)
        self.assertEqual(len(sounds), 2)
        self.assertEqual(len(game.audio_mngr.played), 2)

    def test_a_silent_pad_stays_silent(self):
        """Salamander has no Tom 3/4: routing must not invent a sound."""
        game = make_game()
        drums = self.drums(game)
        with mock.patch.object(drums, "pad_defs",
                               return_value=[(None, None, 1.0, 1)] * 17):
            self.assertEqual(drums.route_to_cinema_room(
                "peer", 8, 10.0, 25.0, 0.0, 100.0), [])
        self.assertEqual(game.audio_mngr.played, [])


if __name__ == "__main__":
    unittest.main()
