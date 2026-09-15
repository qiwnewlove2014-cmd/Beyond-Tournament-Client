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
from libs.audio.cinema import plugin as cinema_plugin
from libs.world_map import Map

ANCHOR = (10.0, 20.0, 0.0)

# Every timer the game was asked to run, so a test can let them fire.
LATER = []


class FakeSource:
    def __init__(self):
        self.position = None
        self.pitch = None
        self.gain = 1.0
        self.buffer = None
        self.direct_filter = None
        self.reference_distance = None
        self.max_distance = None
        self.rolloff_factor = None
        self.spatialize = None
        self.direct_channels = None
        self.buffers_queued = 0
        self.buffers_processed = 0

    def set(self, name, value):
        setattr(self, name, value)

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

    def play_unbound_stereo_spatial(self, path, x, y, z, listener_x=None,
                                    listener_y=None, listener_z=None, **kwargs):
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
# The jukebox's own switch ("Cinema rooms:"). It belongs to the *song*: one
# listening choice per shape of sound, so it never silences a band (see
# libs/audio/cinema/plugin.py::listening_summary).
ROOMS_KEY = cinema_plugin.OPTION_ENABLED
_SNAPSHOT = None


def setUpModule():
    global _SNAPSHOT
    from libs import options
    _SNAPSHOT = (options.prefs.get(LIVE_KEY), options.prefs.get(ROOMS_KEY))


def tearDownModule():
    from libs import options
    live, rooms = _SNAPSHOT or (None, None)
    if live is None:
        options.prefs.pop(LIVE_KEY, None)
    else:
        options.prefs[LIVE_KEY] = live
    if rooms is None:
        options.prefs.pop(ROOMS_KEY, None)
    else:
        options.prefs[ROOMS_KEY] = rooms


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
              bot=None, modes=None, trims=None, live=True, rooms=True):
    """A map with cabinets and cinema speakers, and one performer standing on it.

    The speakers are placed with the labels the room expects, so a real
    resolver runs; nothing here hand-builds a placement. ``live`` arms the
    listener's option -- on is the shipped default, and off is a listener who
    asked for a plain concert.
    """
    from libs import options
    options.prefs[LIVE_KEY] = bool(live)
    options.prefs[ROOMS_KEY] = bool(rooms)
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

    def test_a_switch_that_is_off_says_so_instead_of_looking_like_a_dead_room(self):
        """The one state that silences a band with no other trace: a listener
        whose own switch is off hears the plain instrument and nothing says
        why, which is indistinguishable from a room that would not resolve."""
        from libs import options
        options.prefs[LIVE_KEY] = False
        with mock.patch.object(cinema_live, "_gate_reported", None), \
                mock.patch.object(cinema_live, "log_line") as log:
            self.assertFalse(cinema_live.note_reaches_a_room())
            self.assertFalse(cinema_live.note_reaches_a_room())   # a second note
            self.assertFalse(cinema_live.note_reaches_a_room())
        self.assertEqual([call.args[0] for call in log.call_args_list],
                         ["[Cinema] live instruments: this client's Instruments switch is off"])
        # Back on: notes play, and switching it off again is news again.
        options.prefs[LIVE_KEY] = True
        self.assertTrue(cinema_live.note_reaches_a_room())
        options.prefs[LIVE_KEY] = False
        with mock.patch.object(cinema_live, "log_line") as log:
            self.assertFalse(cinema_live.note_reaches_a_room())
        self.assertEqual(len(log.call_args_list), 1)

    def test_the_band_has_one_switch_and_it_is_its_own(self):
        """``Cinema rooms:`` off must not take the band with it.

        It is the jukebox's line; the band's is ``Instruments:``. A band that
        goes quiet because somebody switched a line about *songs* looks exactly
        like a room that would not resolve, which is the report this rules out.
        """
        from libs import options
        options.prefs[LIVE_KEY] = True
        options.prefs[ROOMS_KEY] = False
        with mock.patch.object(cinema_live, "_gate_reported", None), \
                mock.patch.object(cinema_live, "log_line") as log:
            self.assertTrue(cinema_live.note_reaches_a_room())
        self.assertEqual(log.call_args_list, [])
        # The band's own line is still the whole answer, and it is named as
        # itself -- one line per change of reason, which is what makes a
        # listener's log readable.
        options.prefs[LIVE_KEY] = False
        with mock.patch.object(cinema_live, "_gate_reported", None), \
                mock.patch.object(cinema_live, "log_line") as log:
            self.assertFalse(cinema_live.note_reaches_a_room())
        self.assertEqual(
            [call.args[0] for call in log.call_args_list],
            ["[Cinema] live instruments: this client's Instruments switch is off"])
        options.prefs[ROOMS_KEY] = True
        options.prefs[LIVE_KEY] = True


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
        terms = dict((slot, gain)
                     for slot, _pos, gain, _delay, _tier, _channel
                     in router.speaker_terms((10.0, 25.0, 0.0), (10.0, 25.0, 0.0)))
        self.assertEqual(set(terms), {"front_l", "front_r"})
        for gain in terms.values():
            self.assertAlmostEqual(gain, 1.0, places=6)
        # Far past the room's own reach: nothing, without any special case.
        away = (10.0 + ROOM_MAX_DISTANCE + 40.0, 25.0, 0.0)
        self.assertEqual(router.speaker_terms((10.0, 25.0, 0.0), away), [])

    def test_the_rooms_own_level_and_trim_reach_the_note(self):
        game = make_game(trims={"front_l": (50.0, 40.0)})
        terms = dict((slot, (gain, delay))
                     for slot, _pos, gain, delay, _tier, _channel
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
        self.assertTrue(all(tier == 2
                            for _s, _p, _g, _d, tier, _c in terms))

    def test_a_burst_of_notes_measures_the_room_once(self):
        """A drum roll is twenty notes a second, and every note needs a
        distance, an aim and a wall ray per speaker -- all of it on the main
        thread before the sample is spawned, which is exactly the tail a band
        feels. Both the listener and the performer move at walking speed."""
        calls = []

        def provider(position, listener, max_distance):
            calls.append((position, listener))
            return 0

        router = LiveRoomRouter(make_game())
        for _ in range(8):
            terms = router.speaker_terms((10.0, 25.0, 0.0), (10.0, 25.0, 0.0),
                                        occlusion_provider=provider)
        self.assertEqual(len(terms), 2)
        self.assertEqual(len(calls), 2)          # one measurement, per speaker
        # A listener who has walked is measured again at once, so stepping
        # behind a wall muffles the band when it happens.
        router.speaker_terms((10.0, 25.0, 0.0), (30.0, 25.0, 0.0),
                             occlusion_provider=provider)
        self.assertEqual(len(calls), 4)

    def test_a_cached_answer_is_never_served_for_another_maps_walls(self):
        """The measurement is per provider: a stale answer would muffle a note
        by a wall from somewhere else."""
        router = LiveRoomRouter(make_game())
        blocked = lambda *args: 2
        clear = lambda *args: 0
        through_a_wall = router.speaker_terms((10.0, 25.0, 0.0),
                                              (10.0, 25.0, 0.0),
                                              occlusion_provider=blocked)
        self.assertTrue(all(tier == 2
                            for _s, _p, _g, _d, tier, _c in through_a_wall))
        open_air = router.speaker_terms((10.0, 25.0, 0.0), (10.0, 25.0, 0.0),
                                        occlusion_provider=clear)
        self.assertTrue(all(tier == 0
                            for _s, _p, _g, _d, tier, _c in open_air))

    def test_which_room_the_band_comes_out_of_is_said_once_per_change(self):
        """A listener hearing the band from the instrument itself has no other
        way to learn which room their own client resolved - the performer's
        menu only ever describes the performer's client."""
        game = make_game(cabinets=(("j1", (10.0, 20.0, 0.0)),))
        router = LiveRoomRouter(game)
        with mock.patch.object(cinema_live, "log_line") as log:
            router.route_for((10.0, 25.0, 0.0))
            router.route_for((10.2, 25.0, 0.0))      # still standing still
        self.assertEqual([call.args[0] for call in log.call_args_list],
                         ["[Cinema] live instruments -> jukebox j1 (front_only)"])

    def test_a_map_with_no_room_says_why(self):
        with mock.patch.object(cinema_live, "log_line") as log:
            self.assertIsNone(LiveRoomRouter(make_game(cabinets=()))
                              .route_for((10.0, 25.0, 0.0)))
        self.assertTrue(any("not in a room (this map has no jukebox)" in call.args[0]
                            for call in log.call_args_list),
                        [call.args[0] for call in log.call_args_list])

        with mock.patch.object(cinema_live, "log_line") as log:
            self.assertIsNone(LiveRoomRouter(make_game(modes={"j1": "off"}))
                              .route_for((10.0, 25.0, 0.0)))
        self.assertTrue(any("is set to play its own stereo (off)" in call.args[0]
                            for call in log.call_args_list),
                        [call.args[0] for call in log.call_args_list])

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
            lambda x, y, z, gain, tier, delay, channel=None:
                played.append((x, y, z, gain)))
        self.assertEqual(spoken, 2)
        self.assertEqual(len(played), 2)
        self.assertTrue(all(gain > 0.0 for _x, _y, _z, gain in played))

    def test_a_trimmed_speaker_is_scheduled_rather_than_struck_now(self):
        """The trim is latency the room holds, so the note waits it out."""
        game = make_game(trims={"front_l": (100.0, 40.0)})
        played = []
        count = cinema_live.route_to_room(
            game, (10.0, 25.0, 0.0),
            lambda x, y, z, gain, tier, delay, channel=None: played.append(delay),
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
            lambda x, y, z, gain, tier, delay, channel=None: played.append(delay),
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
            lambda x, y, z, gain, tier, delay, channel=None: played.append(delay),
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


class SpeakerImageTests(unittest.TestCase):
    """The band keeps the room's stereo image instead of a second copy of itself.

    The reported question: a kit pans a tom left in its own sample, but at a
    cabinet every speaker played the whole note, so the room had no left or
    right to hear it in. The room's profile already divides the *song* between
    its speakers -- front_l is the left channel and nothing else, front_r the
    right, a side wall carries the difference, the rears lean across -- so a
    live note is divided the same way at the speakers that carry a whole
    channel, and only there: the rest are a mix no sample half stands in for,
    and inventing one would make the room sound like it has two left toms.
    """

    THEATRE = (("front_l", 6.5, 26.5), ("front_r", 13.5, 26.5),
               ("rear_l", 6.5, 17.0), ("rear_r", 13.5, 17.0))

    def channels(self, game):
        return {slot: channel for slot, _pos, _gain, _delay, _tier, channel
                in LiveRoomRouter(game).speaker_terms((10.0, 25.0, 0.0),
                                                      (10.0, 25.0, 0.0))}

    def test_the_screen_wall_splits_the_note_the_way_it_splits_the_song(self):
        self.assertEqual(self.channels(make_game()),
                         {"front_l": "l", "front_r": "r"})

    def test_the_rest_of_the_room_keeps_the_whole_note(self):
        """The rears lean across to the opposite channel: a mix, not a half."""
        game = make_game(speakers=self.THEATRE)
        _cid, plan = LiveRoomRouter(game).route_for((10.0, 25.0, 0.0))
        self.assertEqual(plan.profile_name, "theatre")
        self.assertEqual(self.channels(game),
                         {"front_l": "l", "front_r": "r",
                          "rear_l": None, "rear_r": None})

    def test_only_a_whole_channel_counts_as_one_side(self):
        from libs.audio.cinema.live import slot_channel
        self.assertEqual(slot_channel((1.0, 0.0)), "l")
        self.assertEqual(slot_channel((0.0, 1.0)), "r")
        # The centre speaker, the difference signal on the walls and the
        # rears' lean across to the opposite channel are all a mix.
        for weights in ((0.5, 0.5), (0.5, -0.5), (-0.5, 0.5),
                        (0.15, 0.35), (0.35, 0.15), (0.707, 0.0)):
            self.assertIsNone(slot_channel(weights), weights)
        for weights in (None, (), (1.0,), ("x", "y"), "l"):
            self.assertIsNone(slot_channel(weights), weights)

    def test_a_room_with_no_image_splits_nothing(self):
        """A mono programme has no left or right to put a tom in.

        ``mono_spread`` gives every speaker the same mid, so its front pair is
        not a channel either -- the one profile this rule must NOT read a
        stereo image out of, however the slot is named.
        """
        from libs.audio.cinema.live import plan_channels
        self.assertEqual(plan_channels(SimpleNamespace(profile="mono_spread")), {})
        self.assertEqual(set(plan_channels(SimpleNamespace(profile="theatre"))),
                         {"front_l", "front_r"})

    def test_the_note_reaches_the_speaker_carrying_its_side(self):
        game = make_game()
        heard = {}

        def play_one(x, y, z, gain, tier, delay, channel=None):
            heard[x] = channel

        cinema_live.route_to_room(game, (10.0, 25.0, 0.0), play_one)
        self.assertEqual(heard, {6.5: "l", 13.5: "r"})


class SpeakerChannelSampleTests(unittest.TestCase):
    """Playing one half of a sample, and never playing silence instead."""

    def test_one_channel_or_the_whole_file(self):
        from libs.audio_manager import split_channel_buffer

        class Provider:
            def load_stereo_split_buffers(self, path):
                return ("L", "R") if path == "stereo" else (None, None)

        provider = Provider()
        self.assertEqual(split_channel_buffer(provider, "stereo", "l"), "L")
        self.assertEqual(split_channel_buffer(provider, "stereo", "r"), "R")
        # A sample the cache has not prepared yet, a provider that cannot
        # split at all, and a channel nobody asked for all mean "whole file".
        self.assertIsNone(split_channel_buffer(provider, "preparing", "l"))
        self.assertIsNone(split_channel_buffer(provider, "stereo", None))
        self.assertIsNone(split_channel_buffer(None, "stereo", "l"))
        self.assertIsNone(split_channel_buffer(object(), "stereo", "l"))

        class Broken:
            def load_stereo_split_buffers(self, path):
                raise RuntimeError("boom")

        self.assertIsNone(split_channel_buffer(Broken(), "stereo", "l"))

    def test_a_mono_sample_plays_the_same_buffer_at_every_speaker(self):
        """Mono files are handed back twice; the note is still audible."""
        from libs.audio_manager import split_channel_buffer
        provider = SimpleNamespace(load_stereo_split_buffers=lambda path: ("M", "M"))
        self.assertEqual(split_channel_buffer(provider, "mono", "l"), "M")
        self.assertEqual(split_channel_buffer(provider, "mono", "r"), "M")

    def test_play_unbound_takes_the_half_it_was_asked_for(self):
        """The wiring itself: the channel reaches OpenAL's buffer."""
        from libs.audio_manager import AudioManager
        audio = AudioManager.__new__(AudioManager)
        audio.muted = False
        audio.volume_categories = {"miscelaneous": [100, set()],
                                   "master": [100, set()]}
        audio.filter = []
        audio.sends = []
        audio.unbound_sources = []
        audio.context = FakeContext()
        audio.efx = FakeEfx()
        audio.make_orientation = lambda *direction: (0.0, 0.0, 0.0)
        audio.load_buffer = lambda path, instrument=True: "whole"
        provider = SimpleNamespace(load_stereo_split_buffers=lambda path: ("L", "R"))

        played = audio.play_unbound("piano/x.ogg", 1, 2, 3, channel="r",
                                    stereo_provider=provider)
        source = audio.context.created[0]
        self.assertEqual(source.buffer, "R")
        self.assertEqual(played.volume, 100)

        audio.context.created.clear()
        audio.play_unbound("piano/x.ogg", 1, 2, 3)
        self.assertEqual(audio.context.created[0].buffer, "whole")

        # A provider that cannot split: the whole file, never a missing note.
        audio.context.created.clear()
        audio.play_unbound("piano/x.ogg", 1, 2, 3, channel="l",
                           stereo_provider=SimpleNamespace())
        self.assertEqual(audio.context.created[0].buffer, "whole")

    def test_the_instruments_ask_for_the_side_their_speaker_carries(self):
        game = make_game()
        from libs.piano import PianoAudio
        piano = PianoAudio(game.audio_mngr)
        piano.gameplay = game.gameplay
        self.assertEqual(piano.route_to_cinema_room(
            "peer", "C4", 10.0, 25.0, 0.0, 300), 2)
        by_side = {spot[0]: kwargs
                   for _path, spot, kwargs in game.audio_mngr.played}
        self.assertEqual(by_side[6.5]["channel"], "l")
        self.assertEqual(by_side[13.5]["channel"], "r")
        self.assertIs(by_side[6.5]["stereo_provider"], piano)

        game = make_game()
        from libs.drums import DrumAudio
        drums = DrumAudio(game.audio_mngr)
        drums.gameplay = game.gameplay
        drums.route_to_cinema_room("peer", 0, 10.0, 25.0, 0.0, 100.0)
        by_side = {spot[0]: kwargs
                   for _path, spot, kwargs in game.audio_mngr.played}
        self.assertEqual(by_side[6.5]["channel"], "l")
        self.assertEqual(by_side[13.5]["channel"], "r")


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
            live = dict((slot, gain)
                        for slot, _pos, gain, _delay, _tier, _channel
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
        # The record owns both room copies -- and nothing else, because the
        # kit itself is not heard on a listener's machine once the room carries
        # the hit (see NoteHeardFromTheVenueTests). A choke still reaches every
        # speaker, which is what this record exists for.
        self.assertEqual(game.audio_mngr.direct, [])
        self.assertEqual(len(record["sounds"]), 2)

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


class NoteHeardFromTheVenueTests(unittest.TestCase):
    """A note that comes out of a room is not *also* heard at the instrument.

    The listener hears the band from the room it is playing in: one sound, in
    one place. The instrument's own copy is a second version of the same note
    in the wrong place -- it is what made a hall sound like a piano standing in
    a hall *plus* a pair of speakers, and it is the copy an installer's trims,
    the room's ramp and its walls never shaped.

    One exemption, deliberate: the performer (their own ears are at the
    instrument and in the room at once, and this is the sound they play
    against). Everywhere else the room *replaces* the instrument, and which
    room that is does not depend on where the listener stands -- a room the
    listener is out of earshot of simply carries nothing to those ears, and
    says so once, because a silent instrument is otherwise indistinguishable
    from a broken one.
    """

    def setUp(self):
        LATER.clear()

    def piano(self, game):
        from libs.piano import PianoAudio
        piano = PianoAudio(game.audio_mngr)
        piano.gameplay = game.gameplay
        return piano

    def drums(self, game):
        from libs.drums import DrumAudio
        drums = DrumAudio(game.audio_mngr)
        drums.gameplay = game.gameplay
        return drums

    def test_a_remote_note_is_heard_from_the_room_not_from_the_piano(self):
        game = make_game()
        self.piano(game).play_note("peer", "C4", 10.0, 25.0, 0.0,
                                   10.0, 25.0, 0.0, volume=300)
        self.assertEqual(game.audio_mngr.direct, [])       # not at the piano
        self.assertEqual(len(game.audio_mngr.played), 2)   # at the room

    def test_a_remote_hit_is_heard_from_the_room_not_from_the_kit(self):
        game = make_game()
        self.drums(game).play_hit("peer", 0, 10.0, 25.0, 0.0,
                                  10.0, 25.0, 0.0, 100.0, kit="default")
        self.assertEqual(game.audio_mngr.direct, [])
        self.assertEqual(len(game.audio_mngr.played), 2)

    def test_a_note_is_not_put_back_at_the_instrument_for_a_distant_listener(self):
        # The listener is 200 m away, so the room's own ramp answers zero -- and
        # the note still belongs to the room. It is heard by nobody here rather
        # than at the piano: a venue replaces the instrument, it does not join
        # it, or "clear the pan" would mean one thing per listener (which is
        # exactly what made the instrument appear to walk back to its stand).
        game = make_game(position=(10.0, 220.0, 0.0))
        self.piano(game).play_note("peer", "C4", 10.0, 25.0, 0.0,
                                   10.0, 220.0, 0.0, volume=300)
        self.assertEqual(game.audio_mngr.direct, [])
        self.assertEqual(game.audio_mngr.played, [])

    def test_a_hit_is_not_put_back_at_the_kit_for_a_distant_listener(self):
        game = make_game(position=(10.0, 220.0, 0.0))
        self.drums(game).play_hit("peer", 0, 10.0, 25.0, 0.0,
                                  10.0, 220.0, 0.0, 100.0, kit="default")
        self.assertEqual(game.audio_mngr.direct, [])
        self.assertEqual(game.audio_mngr.played, [])

    def test_the_performer_still_hears_the_instrument_and_the_room(self):
        # At the instrument and in the room at once: both, as before.
        game = make_game()
        self.piano(game).play_note("local", "C4", 10.0, 25.0, 0.0,
                                   10.0, 25.0, 0.0, volume=300)
        self.assertEqual(len(game.audio_mngr.direct), 1)
        self.assertEqual(len(game.audio_mngr.played), 2)

        game = make_game()
        self.drums(game).play_hit("local", 0, 10.0, 25.0, 0.0,
                                  10.0, 25.0, 0.0, 100.0, kit="default")
        self.assertEqual(len(game.audio_mngr.direct), 1)
        self.assertEqual(len(game.audio_mngr.played), 2)

    def test_a_listener_who_switched_the_rooms_off_hears_the_instrument(self):
        game = make_game(live=False)
        self.piano(game).play_note("peer", "C4", 10.0, 25.0, 0.0,
                                   10.0, 25.0, 0.0, volume=300)
        self.assertEqual(len(game.audio_mngr.direct), 1)
        self.assertEqual(game.audio_mngr.played, [])

    def test_a_panned_band_is_still_heard_at_the_instrument_when_this_client_says_so(self):
        """A pan chooses *which* room, never whether this client hears one.

        The staff decision is where the band is, not a way past somebody's own
        listening switch: a listener who asked for instruments where they stand
        keeps them there, exactly as the menu line promises. That switch is the
        band's own line -- ``Cinema rooms:`` is the jukebox's songs and has no
        say here, which is what keeps one menu line per shape of sound true.
        """
        from libs.audio.cinema import pan as cinema_pan
        options = __import__("libs.options", fromlist=["prefs"])
        game = make_game(live=False)
        cinema_pan.table_for(game.gameplay).set("peer", 7, "j1", "left")
        self.piano(game).play_note("peer", "C4", 10.0, 25.0, 0.0,
                                   10.0, 25.0, 0.0, volume=300)
        self.assertEqual(len(game.audio_mngr.direct), 1)
        self.assertEqual(game.audio_mngr.played, [])
        # ...and the same kit, whose hit is the other half of the gate.
        game = make_game(live=False)
        cinema_pan.table_for(game.gameplay).set("peer", 7, "j1", "left")
        self.drums(game).play_hit("peer", 0, 10.0, 25.0, 0.0,
                                  10.0, 25.0, 0.0, 100.0, kit="default")
        self.assertEqual(len(game.audio_mngr.direct), 1)
        self.assertEqual(game.audio_mngr.played, [])
        # The jukebox's own line touches none of it: the band still comes out
        # of the room staff named.
        game = make_game(rooms=False)
        cinema_pan.table_for(game.gameplay).set("peer", 7, "j1", "left")
        self.piano(game).play_note("peer", "C4", 10.0, 25.0, 0.0,
                                   10.0, 25.0, 0.0, volume=300)
        self.assertEqual(game.audio_mngr.direct, [])
        self.assertEqual(len(game.audio_mngr.played), 2)
        options.prefs[ROOMS_KEY] = True

    def test_a_pan_whose_room_is_gone_leaves_the_note_at_the_instrument(self):
        # The panned cabinet has no room of its own, so there is nothing to
        # play into: the note is heard at the instrument rather than nowhere.
        from libs.audio.cinema import pan as cinema_pan
        game = make_game(cabinets=(("j1", (10.0, 20.0, 0.0)),
                                   ("j2", (200.0, 20.0, 0.0))))
        cinema_pan.table_for(game.gameplay).set("peer", 7, "j2", "left")
        self.piano(game).play_note("peer", "C4", 10.0, 25.0, 0.0,
                                   10.0, 25.0, 0.0, volume=300)
        self.assertEqual(len(game.audio_mngr.direct), 1)
        self.assertEqual(game.audio_mngr.played, [])

    def test_a_venue_copy_carries_the_zones_own_reverb(self):
        """The copy that replaced the positional one is not a drier note."""
        game = make_game()
        game.gameplay.map.get_reverb_at = \
            lambda x, y, z: SimpleNamespace(reverb="hall")
        self.piano(game).route_to_cinema_room("peer", "C4", 10.0, 25.0, 0.0, 300)
        self.assertEqual(len(game.audio_mngr.efx.sends), 2)

        game = make_game()
        game.gameplay.map.get_reverb_at = lambda *args: None
        self.drums(game).route_to_cinema_room("peer", 0, 10.0, 25.0, 0.0, 100.0,
                                              kit="default")
        self.assertEqual(game.audio_mngr.efx.sends, [])

    def test_the_venue_decision_is_the_rooms_own_answer(self):
        """The instruments ask live.room_for; nothing guesses twice."""
        game = make_game()
        self.assertTrue(cinema_live.note_goes_to_a_room(
            game, (10.0, 25.0, 0.0)))
        self.assertEqual(cinema_live.room_for(game, (10.0, 25.0, 0.0))[0], "j1")
        self.assertEqual(len(cinema_live.room_terms_for(
            game, (10.0, 25.0, 0.0))), 2)
        # A listener out of the room's reach: the note still belongs to the
        # room (that half of the answer is the performer's, and the same on
        # every machine) -- it simply has no speakers at these ears.
        far = make_game(position=(10.0, 220.0, 0.0))
        self.assertTrue(cinema_live.note_goes_to_a_room(far, (10.0, 25.0, 0.0)))
        self.assertEqual(cinema_live.room_terms_for(far, (10.0, 25.0, 0.0)), ())
        # A map with no cabinet at all, and no game to ask: never an error.
        self.assertFalse(cinema_live.note_goes_to_a_room(
            make_game(cabinets=()), (10.0, 25.0, 0.0)))
        self.assertIsNone(cinema_live.room_for(make_game(cabinets=()),
                                               (10.0, 25.0, 0.0)))
        self.assertFalse(cinema_live.note_goes_to_a_room(
            SimpleNamespace(), (10.0, 25.0, 0.0)))
        self.assertEqual(cinema_live.room_terms_for(None, (1.0, 2.0, 3.0)), ())
        self.assertIsNone(cinema_live.room_for(None, (1.0, 2.0, 3.0)))

    def test_every_client_agrees_on_which_room_the_band_is_in(self):
        """The room is the performer's, and only how loud it is is the listener's.

        Two listeners, one among the speakers and one 200 m outside the room:
        both answer "jukebox j1". Before this the distant one answered "no
        room" and was handed the instrument instead, so the *same* delivery
        sounded like two different systems depending on where you stood.
        """
        near = make_game()
        far = make_game(position=(10.0, 220.0, 0.0))
        for game in (near, far):
            self.assertTrue(cinema_live.note_goes_to_a_room(
                game, (10.0, 25.0, 0.0)))
            self.assertEqual(cinema_live.room_for(game, (10.0, 25.0, 0.0))[0],
                             "j1")
        self.assertEqual(len(cinema_live.room_terms_for(
            near, (10.0, 25.0, 0.0))), 2)
        self.assertEqual(cinema_live.room_terms_for(far, (10.0, 25.0, 0.0)), ())

    def test_a_listener_out_of_reach_is_told_once_rather_than_left_wondering(self):
        """A silent instrument has no other trace, so the client says why."""
        game = make_game(position=(10.0, 220.0, 0.0))
        piano = self.piano(game)

        def lines():
            with mock.patch.object(cinema_live, "log_line") as log:
                piano.play_note("peer", "C4", 10.0, 25.0, 0.0,
                                10.0, game.audio_mngr.position[1], 0.0,
                                volume=300)
            return [call.args[0] for call in log.call_args_list
                    if "out of reach here" in call.args[0]]

        said = lines()
        self.assertEqual(len(said), 1)          # said once, naming the cabinet
        self.assertIn("jukebox j1", said[0])
        self.assertEqual(lines(), [])           # ...and not once per note
        # Walking back into the room is not news (the sound is simply back),
        # and it moves the state on so walking out again can be; a listener
        # standing on the ramp edge is not a line per step.
        game.audio_mngr.position = (10.0, 25.0, 0.0)
        self.assertEqual(lines(), [])
        game.audio_mngr.position = (10.0, 220.0, 0.0)
        self.assertEqual(lines(), [])
        with mock.patch.object(cinema_live, "REACH_REPORT_INTERVAL", 0.0):
            self.assertEqual(len(lines()), 1)


if __name__ == "__main__":
    unittest.main()
