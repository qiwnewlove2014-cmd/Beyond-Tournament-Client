"""A voice coming out of a cabinet's room.

The megaphone already gets a voice out of a set of speakers; these cover the
other set of speakers it can use -- the ones a builder placed around one
cabinet. A talker standing inside a room's reach is heard from that room
instead of from the map's PA, and the point of these tests is that the voice is
shaped by the room's own numbers (its distance ramp, the map's per-speaker
level and trim, the wall between) rather than by a second, unshaped copy of
itself -- while a talker with no room around them keeps the PA path exactly as
it shipped, byte for byte.
"""

import array
import math
import os
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

import cyal

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from libs.audio.cinema import ROOM_MAX_DISTANCE, speech_enabled
from libs.audio.cinema import speech as cinema_speech
from libs.audio.cinema.bank import CinemaSpeakerBank
from libs.audio.cinema.crossover import apply as crossover_apply
# The bass cabinet's frame helpers are the crossover tests' own (one DFT and
# one fingerprint, shared rather than re-implemented -- a second copy of the
# measurement is how two files end up disagreeing about the same sound).
from test_cinema_crossover import SAMPLERATE, digests, magnitude
from libs.audio.cinema.listener import (occlusion_filter, speaker_filter,
                                        tone_gainhf, wall_params)
from libs.world_map import Map

ANCHOR = (10.0, 20.0, 0.0)
FRAME = b"\x01\x00" * 960


class FakeBuffer:
    def __init__(self):
        self.data = None

    def set_data(self, data, sample_rate=None, format=None):
        self.data = bytes(data)


class FakeReverb:
    """A reverb zone on the map, as much of one as the lookup touches."""

    def __init__(self, reverb, bounds=(0.0, 100.0, 0.0, 100.0, -10.0, 10.0)):
        self.reverb = reverb
        self.bounds = bounds

    def in_bound(self, x, y, z):
        minx, maxx, miny, maxy, minz, maxz = self.bounds
        return minx <= x <= maxx and miny <= y <= maxy and minz <= z <= maxz


class FakeEfx:
    """Records the room sends a source is given (index, slot, filter)."""

    def __init__(self):
        self.sends = []

    def send(self, source, index, slot, filter=None):
        self.sends.append((source, index, slot, filter))


class FakeSource:
    def __init__(self, context):
        self.context = context
        self.position = None
        self.gain = 1.0
        self.direct_filter = "unset"
        self.rolloff_factor = None
        self.reference_distance = None
        self.max_distance = None
        self.state = cyal.SourceState.INITIAL
        self.queued = []
        self.played = 0
        self.processed = 0
        self.destroyed = False

    @property
    def buffers_queued(self):
        return len(self.queued)

    @property
    def buffers_processed(self):
        return self.processed

    def queue_buffers(self, buffer):
        self.queued.append(buffer)

    def unqueue_buffers(self, *, max=2 ** 31 - 1):
        if self.processed <= 0:
            return []
        count = min(self.processed, int(max))
        taken = self.queued[:count]
        del self.queued[:count]
        self.processed = 0
        return taken

    def play(self):
        self.played += 1
        self.state = cyal.SourceState.PLAYING

    def stop(self):
        self.state = cyal.SourceState.STOPPED

    def destroy(self):
        self.destroyed = True
        self.context.destroyed.append(self)


class DriverFakeSource(FakeSource):
    """A source that reports its queue the way OpenAL really does.

    ``AL_BUFFERS_PROCESSED`` counts *finished* buffers, and a source that is not
    playing has finished everything it holds: a STOPPED or INITIAL source
    reports its whole queue, not zero. That is the behaviour the voice leg has
    to survive -- recycling on that number hands back the frame that was just
    queued, so the queue can never reach ``START_FRAMES`` and a speaker that has
    ever played never starts again. Only a PLAYING source reports honestly.
    """

    @property
    def buffers_processed(self):
        if self.state == cyal.SourceState.PLAYING:
            return self.processed
        return len(self.queued)

    def unqueue_buffers(self, *, max=2 ** 31 - 1):
        # OpenAL hands back exactly what it calls processed, so a stopped
        # source gives up its whole queue -- which is how the frame that was
        # just queued came to be taken straight back.
        count = min(int(self.buffers_processed), int(max))
        if count <= 0:
            return []
        taken = self.queued[:count]
        del self.queued[:count]
        self.processed = 0
        return taken

    def drain_and_stop(self):
        """The device plays everything queued out and the source stops."""
        self.processed = len(self.queued)
        self.state = cyal.SourceState.STOPPED


class FakeContext:
    def __init__(self):
        self.created = []
        self.destroyed = []
        self.buffers = 0

    def gen_source(self, **kwargs):
        source = FakeSource(self)
        self.created.append(source)
        return source

    def gen_buffer(self):
        self.buffers += 1
        return FakeBuffer()


class DriverContext(FakeContext):
    """A context that hands out drivers-honest sources."""

    def gen_source(self, **kwargs):
        source = DriverFakeSource(self)
        self.created.append(source)
        return source


class FakeAudio:
    def __init__(self, position=ANCHOR, jukebox_volume=100):
        self.position = position
        self.volume_categories = {"jukebox": [jukebox_volume], "music": [100]}
        self.filter = []
        self.context = FakeContext()
        self.efx = SimpleNamespace(send=lambda *args, **kwargs: None)

    def gen_filter(self, kind, *params):
        return ("filter", kind, params)


def make_game(speakers=(("front_l", 6.5, 26.5, 100.0, 0.0),
                        ("front_r", 13.5, 26.5, 100.0, 0.0)),
              cabinets=(("j1", ANCHOR),), listener=ANCHOR, modes=None,
              tier=0, talkers=((7, ANCHOR),), jukebox_volume=100,
              reverb=None, cabinet_eq=None, tones=None, crossovers=None):
    """A map with cabinets, cinema speakers and one talker standing on it.

    The speakers are spawned with the labels the room expects, so a real
    resolver runs; nothing here hand-builds a placement. ``reverb`` is the
    effect slot of the reverb zone standing where the cabinets are (None = a
    map with no reverb zone at all), and ``cabinet_eq`` the slot the cabinet
    resolves its EQ to. ``tones`` is the map's own voicing per slot
    (``{"front_l": 40}``), a percentage where 100 is "as placed".
    """
    game = SimpleNamespace()
    audio = FakeAudio(listener, jukebox_volume)
    game.audio_mngr = audio
    map_obj = Map(game)
    if reverb is not None:
        map_obj.reverb_list.append(FakeReverb(reverb,
                                              bounds=(ANCHOR[0] - 20, ANCHOR[0] + 20,
                                                      ANCHOR[1] - 20, ANCHOR[1] + 20,
                                                      -10.0, 10.0)))
    tones = dict(tones or {})
    crossovers = dict(crossovers or {})
    for index, (name, x, y, level, delay) in enumerate(speakers):
        # A unique element id per speaker (the map keys on it), with the slot
        # name as the channel: two cabinets in one map have to be two real
        # rooms, and reusing an id silently dropped the first one's speaker.
        map_obj.spawn_cinemaSpeaker(minx=x - 0.5, maxx=x + 0.5, miny=y - 0.5,
                                    maxy=y + 0.5, minz=0, maxz=1,
                                    id=f"spk{index}", channel=name,
                                    level=level, delay=delay,
                                    tone=tones.get(name, 100),
                                    crossover=crossovers.get(name, 0))
    map_obj.jukebox_list = [SimpleNamespace(id=cabinet_id, center=center)
                            for cabinet_id, center in cabinets]
    modes = dict(modes or {})
    player = SimpleNamespace(
        cinema_mode=lambda cabinet_id: modes.get(cabinet_id, "auto"),
        occlusion_tier=lambda *args: tier,
    )
    if cabinet_eq is not None:
        # The cabinet's own EQ, asked the way the speech path asks for it (the
        # jukebox's resolver, with the cabinet's own profile).
        player.eq_profiles = {cabinet_id: "bass_boost"
                              for cabinet_id, _anchor in cabinets}
        player.eq_values = {}
        player._get_eq_slot = lambda profile, jukebox_id=None, values=None: cabinet_eq
    channels = {}
    for channel_id, position in talkers:
        channels[channel_id] = SimpleNamespace(x=position[0], y=position[1],
                                              z=position[2])
    game.gameplay = SimpleNamespace(
        map=map_obj, voice_channels=channels, jukebox_player=player,
        music_bot=SimpleNamespace(volume=50), game=game,
        player=SimpleNamespace(dead=False),
        megaphone=SimpleNamespace(player_sources={}),
    )
    game.audio_mngr.defer_audio = lambda fn: fn()
    return game


class SpeechSwitchTests(unittest.TestCase):
    """The listener's own switch: on for a new player, and one key for both."""

    def setUp(self):
        from libs import options
        self._saved = options.prefs.pop(cinema_speech.OPTION_ENABLED, None)

    def tearDown(self):
        from libs import options
        options.prefs.pop(cinema_speech.OPTION_ENABLED, None)
        if self._saved is not None:
            options.prefs[cinema_speech.OPTION_ENABLED] = self._saved

    def test_a_voice_comes_through_the_room_for_a_listener_who_never_chose(self):
        from libs import options
        options.prefs.pop(cinema_speech.OPTION_ENABLED, None)
        self.assertTrue(cinema_speech.DEFAULT_ENABLED)
        self.assertTrue(speech_enabled())
        self.assertTrue(cinema_speech.routed(*self.routed_talker()))

    def routed_talker(self):
        game = make_game()
        return game, game.gameplay, 7

    def test_the_saved_choice_is_the_one_the_voice_path_reads(self):
        from libs import options
        with mock.patch.object(options, "set") as setter:
            self.assertTrue(cinema_speech.set_speech_enabled(False))
        setter.assert_called_once_with(cinema_speech.OPTION_ENABLED, False)
        options.prefs[cinema_speech.OPTION_ENABLED] = False
        self.assertFalse(speech_enabled())
        self.assertFalse(cinema_speech.routed(*self.routed_talker()))


class TalkerPositionTests(unittest.TestCase):
    """Whose room: the room the person speaking is standing in."""

    def test_the_position_comes_from_the_senders_voice_channel(self):
        game = make_game(talkers=((7, (44.0, 55.0, 1.0)),))
        self.assertEqual(cinema_speech.talker_position(game.gameplay, 7),
                         (44.0, 55.0, 1.0))

    def test_a_sender_with_no_entity_has_no_position(self):
        game = make_game()
        self.assertIsNone(cinema_speech.talker_position(game.gameplay, 99))


class RoomRoutingTests(unittest.TestCase):
    """Which voices belong to a room, and which stay on the PA."""

    def test_a_talker_in_a_room_is_routed_to_it(self):
        game = make_game()
        cabinet_id, plan = cinema_speech.room_target(game, game.gameplay, 7)
        self.assertEqual(cabinet_id, "j1")
        self.assertEqual(plan.profile_name, "front_only")

    def test_a_cabinet_the_map_set_to_off_is_not_a_room(self):
        game = make_game(modes={"j1": "off"})
        self.assertFalse(cinema_speech.routed(game, game.gameplay, 7))
        self.assertFalse(cinema_speech.feed(game, game.gameplay, 7, FRAME))

    def test_a_map_with_no_cabinet_keeps_the_pa(self):
        game = make_game(cabinets=())
        self.assertFalse(cinema_speech.routed(game, game.gameplay, 7))
        self.assertFalse(cinema_speech.feed(game, game.gameplay, 7, FRAME))

    def test_a_map_with_speakers_but_no_front_pair_keeps_the_pa(self):
        game = make_game(speakers=(("side_l", -8.0, 20.0, 100.0, 0.0),))
        self.assertFalse(cinema_speech.routed(game, game.gameplay, 7))

    def test_a_talker_who_walks_out_of_the_room_gives_it_back(self):
        game = make_game(cabinets=(("j1", ANCHOR),))
        self.assertTrue(cinema_speech.feed(game, game.gameplay, 7, FRAME))
        holder = game.audio_mngr.cinema_speech
        self.assertIn(7, holder.legs)
        # Walk far past the room's own reach: the next frame goes to the PA.
        game.gameplay.voice_channels[7] = SimpleNamespace(
            x=ANCHOR[0] + ROOM_MAX_DISTANCE + 200.0, y=ANCHOR[1], z=0.0)
        self.assertFalse(cinema_speech.feed(game, game.gameplay, 7, FRAME))
        self.assertEqual(holder.legs, {})
        self.assertTrue(game.audio_mngr.context.created[0].destroyed)


class RoomLegTests(unittest.TestCase):
    """What one talker's voice does once it is playing through the room."""

    def leg(self, game, sender_id=7):
        self.assertTrue(cinema_speech.feed(game, game.gameplay, sender_id, FRAME))
        return game.audio_mngr.cinema_speech.legs[sender_id]

    def test_one_flat_source_per_speaker_of_the_room(self):
        game = make_game()
        leg = self.leg(game)
        self.assertEqual(set(leg.sources), {"front_l", "front_r"})
        for source in leg.sources.values():
            # Flat at the source: the room's own ramp already shaped the voice,
            # and OpenAL attenuating it again would fade the same speaker twice.
            self.assertEqual(source.rolloff_factor, 0.0)
        self.assertEqual(leg.sources["front_l"].position, (6.5, 26.5, 0.5))

    def test_a_speaker_starts_on_real_frames_not_on_one_frame_at_a_time(self):
        game = make_game()
        leg = self.leg(game)
        source = leg.sources["front_l"]
        # One frame in hand is not enough to start: the play-out timer runs at
        # the same 20 ms as the frames, so a speaker handed them one at a time
        # goes dry the first time that timer is late (clicks in the voice).
        self.assertEqual(source.state, cyal.SourceState.INITIAL)
        cinema_speech.feed(game, game.gameplay, 7, FRAME)
        self.assertEqual(source.state, cyal.SourceState.INITIAL)
        cinema_speech.feed(game, game.gameplay, 7, FRAME)
        self.assertEqual(source.state, cyal.SourceState.PLAYING)
        self.assertEqual(source.played, 1)

    def test_gain_is_the_rooms_own_level_distance_and_aim(self):
        game = make_game(speakers=(("front_l", 6.5, 26.5, 50.0, 0.0),
                                   ("front_r", 13.5, 26.5, 100.0, 0.0)))
        leg = self.leg(game)
        self.assertAlmostEqual(leg.sources["front_l"].gain, 0.5, places=6)
        self.assertAlmostEqual(leg.sources["front_r"].gain, 1.0, places=6)

    def test_the_cabinet_volume_moves_the_voice_with_the_song(self):
        game = make_game(jukebox_volume=50)
        leg = self.leg(game)
        self.assertAlmostEqual(leg.sources["front_l"].gain, 0.5, places=6)

    def test_a_listener_out_of_reach_is_silenced_not_frozen(self):
        game = make_game()
        leg = self.leg(game)
        self.assertAlmostEqual(leg.sources["front_l"].gain, 1.0, places=6)
        game.audio_mngr.position = (ANCHOR[0] + ROOM_MAX_DISTANCE + 100.0,
                                    ANCHOR[1], 0.0)
        cinema_speech.feed(game, game.gameplay, 7, FRAME)
        self.assertEqual(leg.sources["front_l"].gain, 0.0)
        self.assertEqual(leg.sources["front_r"].gain, 0.0)

    def test_a_speakers_trim_holds_the_voice_back_by_whole_frames(self):
        game = make_game(speakers=(("front_l", 6.5, 26.5, 100.0, 40.0),
                                   ("front_r", 13.5, 26.5, 100.0, 0.0)))
        leg = self.leg(game)
        # Two frames of trim on front_l, none on front_r: the first frames of
        # the room's own voice reach the untrimmed speaker, and nobody waits
        # for audio that has not arrived yet (the hold is derived from the
        # trim, so the FIRST frame is never the one that starves).
        self.assertEqual(len(leg.sources["front_l"].queued), 0)
        self.assertEqual(len(leg.sources["front_r"].queued), 1)
        cinema_speech.feed(game, game.gameplay, 7, FRAME)
        self.assertEqual(len(leg.sources["front_l"].queued), 0)
        cinema_speech.feed(game, game.gameplay, 7, FRAME)
        self.assertEqual(len(leg.sources["front_l"].queued), 1)
        self.assertEqual(len(leg.sources["front_r"].queued), 3)

    def test_a_trim_past_the_limit_is_not_a_lag(self):
        game = make_game(speakers=(("front_l", 6.5, 26.5, 100.0, 500.0),
                                   ("front_r", 13.5, 26.5, 100.0, 0.0)))
        leg = self.leg(game)
        # 500 ms is fine for a song and reads as lag for a voice: the trim is
        # honoured up to TRIM_LIMIT_MS and no further.
        self.assertEqual(leg.holds["front_l"], 3)
        self.assertEqual(leg.holds["front_r"], 0)

    def test_a_wall_muffles_the_voice_and_clearing_it_keeps_the_dive(self):
        game = make_game(tier=1)
        leg = self.leg(game)
        light = leg.sources["front_l"].direct_filter
        self.assertEqual(light, occlusion_filter(game.audio_mngr, 1, {}))
        game.gameplay.jukebox_player.occlusion_tier = lambda *args: 2
        cinema_speech.feed(game, game.gameplay, 7, FRAME)
        self.assertEqual(leg.sources["front_l"].direct_filter,
                         occlusion_filter(game.audio_mngr, 2, {}))
        # Out of the wall with the camera's water muffle active: restoring the
        # active global filter, not deleting it (a raw source that had its
        # filter deleted would surface clear mid-dive).
        game.audio_mngr.filter = ["water"]
        game.gameplay.jukebox_player.occlusion_tier = lambda *args: 0
        cinema_speech.feed(game, game.gameplay, 7, FRAME)
        self.assertEqual(leg.sources["front_l"].direct_filter, "water")

    def test_the_song_and_the_voice_muffle_by_the_same_wall(self):
        """One measurement, one pair of filters -- pinned, not assumed."""
        audio = FakeAudio()
        cached = {}
        for tier, heavy in ((1, False), (2, True)):
            song = CinemaSpeakerBank._occlusion_filter(
                SimpleNamespace(_occlusion_filters=dict(cached)),
                audio, heavy=heavy)
            self.assertEqual(song, occlusion_filter(audio, tier, dict(cached)))

    def test_a_dulled_speaker_dulls_the_voice_too(self):
        """A voice is heard out of the same speaker the song is, so a speaker
        the map made dull must dull the voice by the same amount."""
        game = make_game(tones={"front_l": 40})
        leg = self.leg(game)
        self.assertEqual(leg.sources["front_l"].direct_filter,
                         speaker_filter(game.audio_mngr, 0, 0.4, {}))
        # The speaker nobody dulled keeps the call it always did: no filter on
        # a clear path.
        self.assertIsNone(getattr(leg.sources["front_r"], "direct_filter", None))

    def test_a_dulled_speaker_behind_a_wall_is_both_and_neither_twice(self):
        """One direct filter per source: the voicing and the wall are composed,
        and the wall's own loudness dip survives the composition."""
        game = make_game(tones={"front_l": 40}, tier=2)
        leg = self.leg(game)
        filt = leg.sources["front_l"].direct_filter
        wall_hf, wall_gain = wall_params(2)
        self.assertAlmostEqual(filt[2][0][1], wall_hf * tone_gainhf(0.4),
                               places=6)
        self.assertAlmostEqual(filt[2][1][1], wall_gain, places=6)
        self.assertNotEqual(filt, occlusion_filter(game.audio_mngr, 2, {}))

    def test_a_speaker_placed_mid_sentence_joins_in_place(self):
        game = make_game()
        leg = self.leg(game)
        sources = dict(leg.sources)
        # A builder adds a centre speaker while the talker is talking.
        game.gameplay.map.spawn_cinemaSpeaker(
            minx=9.5, maxx=10.5, miny=26.0, maxy=27.0, minz=0, maxz=1,
            id="front_c", channel="front_c", level=100.0, delay=0.0)
        # The re-resolve is throttled to once a second (a talker standing still
        # costs nothing), so let the next frame be a fresh one.
        game.audio_mngr.cinema_live.interval = 0.0
        cinema_speech.feed(game, game.gameplay, 7, FRAME)
        self.assertEqual(set(leg.sources), {"front_l", "front_r", "front_c"})
        # The speakers already playing are the same objects: rebuilding them
        # would drop a whole queue's worth of voice.
        self.assertIs(leg.sources["front_l"], sources["front_l"])
        self.assertEqual(sources["front_l"].destroyed, False)

    def test_the_same_room_is_not_rebuilt_on_every_frame(self):
        game = make_game()
        leg = self.leg(game)
        before = game.audio_mngr.context.created
        for _ in range(20):
            cinema_speech.feed(game, game.gameplay, 7, FRAME)
        self.assertEqual(len(game.audio_mngr.context.created), len(before))
        self.assertEqual(len(leg.sources["front_l"].queued), 21)

    def test_a_talker_who_moves_to_another_cabinet_takes_the_other_room(self):
        game = make_game(
            speakers=(("front_l", 6.5, 26.5, 100.0, 0.0),
                      ("front_r", 13.5, 26.5, 100.0, 0.0),
                      ("front_l", 196.5, 26.5, 100.0, 0.0),
                      ("front_r", 203.5, 26.5, 100.0, 0.0)),
            cabinets=(("j1", ANCHOR), ("j2", (200.0, 20.0, 0.0))),
            talkers=((7, ANCHOR), (8, (200.0, 25.0, 0.0))))
        leg = self.leg(game, sender_id=8)
        self.assertEqual(leg.cabinet_id, "j2")
        self.assertEqual(set(leg.sources), {"front_l", "front_r"})
        self.assertEqual(leg.sources["front_l"].position, (196.5, 26.5, 0.5))

    def test_releasing_a_voice_returns_its_buffers_and_its_speakers(self):
        game = make_game()
        leg = self.leg(game)
        sources = dict(leg.sources)
        for source in sources.values():
            source.processed = 1   # OpenAL finished the frames it played
        cinema_speech.drop(game, 7)
        self.assertEqual(game.audio_mngr.cinema_speech.legs, {})
        self.assertTrue(all(source.destroyed for source in sources.values()))
        self.assertTrue(game.audio_mngr.cinema_speech.pool)

    def test_the_pool_is_shared_between_speakers_and_talkers(self):
        game = make_game()
        for sender_id in (7, 8, 9):
            game.gameplay.voice_channels[sender_id] = SimpleNamespace(
                x=ANCHOR[0], y=ANCHOR[1], z=0.0)
        for _ in range(25):
            for sender_id in (7, 8, 9):
                cinema_speech.feed(game, game.gameplay, sender_id, FRAME)
            # OpenAL played what was queued, so those buffers are reclaimable.
            for leg in game.audio_mngr.cinema_speech.legs.values():
                for source in leg.sources.values():
                    source.processed = source.buffers_queued
        # 150 frames across six speakers came out of the three frames each
        # speaker keeps to start on, not 150: a voice is ~50 buffers a second
        # per speaker, and every frame after the first fill-up is a buffer a
        # finished one handed back rather than a new allocation.
        self.assertLessEqual(game.audio_mngr.context.buffers,
                             6 * cinema_speech.START_FRAMES)

    def test_a_talker_nobody_has_heard_from_is_let_go(self):
        game = make_game()
        leg = self.leg(game)
        leg.last_feed -= cinema_speech.RELEASE_AFTER_S + 0.5
        holder = game.audio_mngr.cinema_speech
        holder.sweep()
        self.assertEqual(holder.legs, {})
        self.assertTrue(leg.sources == {})

    def test_the_number_of_rooms_lit_at_once_is_capped(self):
        game = make_game()
        for sender_id in range(1, cinema_speech.MAX_TALKERS + 3):
            game.gameplay.voice_channels[sender_id] = SimpleNamespace(
                x=ANCHOR[0], y=ANCHOR[1], z=0.0)
            cinema_speech.feed(game, game.gameplay, sender_id, FRAME)
        holder = game.audio_mngr.cinema_speech
        self.assertEqual(len(holder.legs), cinema_speech.MAX_TALKERS)
        self.assertNotIn(1, holder.legs)

    def test_describe_says_which_cabinet_is_carrying_someone(self):
        game = make_game()
        self.leg(game)
        self.assertEqual(cinema_speech.describe(game),
                         ["7: jukebox j1 (2 speaker(s))"])


class RestartingSpeakerTests(unittest.TestCase):
    """A speaker that has finished one burst has to start again on the next.

    Frames arrive one at a time and a speaker only starts once ``START_FRAMES``
    of them are queued. OpenAL reports a source that is not playing as having
    processed *everything* it holds, so the "recycle what has finished" step in
    front of every frame used to hand back the frame that had just been queued:
    the queue could never reach the start-up depth, and every burst after the
    first one was silent until the leg was swept and its sources rebuilt --
    heard as "the second thing I say does not come out of the room, I have to
    turn PA Test Mode off and on again".
    """

    def game(self):
        game = make_game()
        # The honest driver, not the convenient fake (see DriverFakeSource).
        game.audio_mngr.context = DriverContext()
        return game

    def talk(self, game, frames, sender_id=7):
        for _ in range(frames):
            self.assertTrue(cinema_speech.feed(game, game.gameplay, sender_id, FRAME))

    def speakers(self, game, sender_id=7):
        return game.audio_mngr.cinema_speech.legs[sender_id].sources

    def speaker(self, game, sender_id=7):
        return self.speakers(game, sender_id)["front_l"]

    def test_a_speaker_that_finished_a_burst_starts_again(self):
        game = self.game()
        self.talk(game, 3)                      # enough to start playing
        source = self.speaker(game)
        self.assertEqual(source.state, cyal.SourceState.PLAYING)

        # The device plays the burst out and the speaker stops by itself. Its
        # buffers are finished but still queued until someone unqueues them,
        # which is exactly how the driver leaves a source behind.
        for item in self.speakers(game).values():
            item.drain_and_stop()
        self.assertEqual(source.buffers_processed, source.buffers_queued)

        self.talk(game, 3)
        self.assertEqual(source.state, cyal.SourceState.PLAYING)
        self.assertGreaterEqual(source.buffers_queued, 1)

    def test_the_new_burst_keeps_its_own_frames_while_it_fills_up(self):
        game = self.game()
        self.talk(game, 3)
        source = self.speaker(game)
        for item in self.speakers(game).values():
            item.drain_and_stop()

        # One frame into the next burst the speaker is not playing yet, and the
        # driver already calls that frame processed. It must still be there for
        # the second and third frame to line up behind.
        self.talk(game, 1)
        self.assertEqual(source.buffers_queued, 1)
        self.talk(game, 1)
        self.assertEqual(source.buffers_queued, 2)
        self.talk(game, 1)
        self.assertEqual(source.state, cyal.SourceState.PLAYING)

    def test_the_finished_frames_of_the_last_burst_are_reused(self):
        game = self.game()
        context = game.audio_mngr.context
        self.talk(game, 3)
        source = self.speaker(game)
        for item in self.speakers(game).values():
            item.drain_and_stop()
        made = context.buffers
        self.talk(game, 3)
        # The finished frames go back to the pool, so the new burst reuses them
        # instead of leaving three more buffers attached to a dead queue.
        self.assertEqual(source.buffers_queued, 3)
        self.assertEqual(context.buffers, made)


class RoomAcousticsTests(unittest.TestCase):
    """A voice comes out of *that room*, not out of a dry booth.

    The room's own speakers carry the cabinet's reverb zone and its EQ for the
    song, so a voice played at the same speakers goes through the same two
    sends. A cabinet with no reverb zone around it keeps the dry sound the map
    has always had -- a voice is never given an invented room.
    """

    REVERB = "reverb-slot"
    EQ = "eq-slot"

    def setUp(self):
        # The room's reverb is re-read on a timer in the game; here every frame
        # has to see the change so the send itself is what is being tested.
        self.addCleanup(setattr, cinema_speech.RoomSpeechLeg,
                        "ENVIRONMENT_INTERVAL_S",
                        cinema_speech.RoomSpeechLeg.ENVIRONMENT_INTERVAL_S)
        cinema_speech.RoomSpeechLeg.ENVIRONMENT_INTERVAL_S = 0.0

    def make(self, **kwargs):
        game = make_game(**kwargs)
        efx = FakeEfx()
        game.audio_mngr.efx = efx
        return game, efx

    def feed(self, game, sender_id=7, producer=None):
        if producer is None:
            self.assertTrue(cinema_speech.feed(game, game.gameplay, sender_id, FRAME))
            return
        from libs.voice_chat import _feed_local_megaphone_main
        game.gameplay.player = SimpleNamespace(id=sender_id, name="performer")
        game.gameplay.megaphone = SimpleNamespace(
            get_megaphone_player_sources=mock.Mock(return_value=[object()]),
            player_sources={})
        _feed_local_megaphone_main(game.gameplay, FRAME, producer=producer)

    @staticmethod
    def sends(efx, index):
        return [send for send in efx.sends if send[1] == index]

    def test_the_rooms_reverb_carries_the_voice_like_the_song(self):
        game, efx = self.make(reverb=self.REVERB)
        self.feed(game)
        leg = game.audio_mngr.cinema_speech.legs[7]
        slots = {send[2] for send in self.sends(efx, cinema_speech.REVERB_SEND)}
        self.assertEqual(slots, {self.REVERB})
        # One send per speaker of the room, on that speaker's own source.
        sources = {send[0] for send in self.sends(efx, cinema_speech.REVERB_SEND)}
        self.assertEqual(sources, set(leg.sources.values()))

    def test_a_map_with_no_reverb_zone_plays_the_voice_dry(self):
        game, efx = self.make()
        self.feed(game)
        # The send is explicitly *cleared* (None), never left holding whatever
        # a previous room put there.
        self.assertEqual({send[2] for send in self.sends(efx, cinema_speech.REVERB_SEND)},
                         {None})

    def test_a_room_without_an_audio_device_never_breaks_the_voice(self):
        game, _efx = self.make(reverb=self.REVERB)
        game.audio_mngr.efx = None
        self.feed(game)
        leg = game.audio_mngr.cinema_speech.legs[7]
        self.assertEqual(len(leg.sources["front_l"].queued), 1)

    def test_the_wall_the_voice_is_behind_filters_the_reverb_too(self):
        game, efx = self.make(reverb=self.REVERB, tier=2)
        self.feed(game)
        leg = game.audio_mngr.cinema_speech.legs[7]
        source = leg.sources["front_l"]
        keep = [send for send in self.sends(efx, cinema_speech.REVERB_SEND)
                if send[0] is source]
        self.assertTrue(keep, efx.sends)
        # The reverb is sent *through* the wall filter, so a muffled voice
        # reverberates as muffled as it sounds.
        self.assertIs(keep[-1][3], source.direct_filter)
        self.assertIsNotNone(keep[-1][3])

    def test_every_speaker_of_the_room_gets_the_room_reverb(self):
        game, efx = self.make(speakers=(("front_l", 6.5, 26.5, 100.0, 0.0),
                                        ("front_r", 13.5, 26.5, 100.0, 0.0),
                                        ("side_l", -4.0, 20.0, 100.0, 0.0),
                                        ("side_r", 24.0, 20.0, 100.0, 0.0)),
                              reverb=self.REVERB)
        self.feed(game)
        leg = game.audio_mngr.cinema_speech.legs[7]
        self.assertEqual(len(leg.sources), 4)
        self.assertEqual(len(self.sends(efx, cinema_speech.REVERB_SEND)), 4)

    def test_the_cabinets_own_eq_shapes_the_voice_like_the_song(self):
        game, efx = self.make(cabinet_eq=self.EQ)
        self.feed(game)
        self.assertEqual({send[2] for send in self.sends(efx, cinema_speech.EQ_SEND)},
                         {self.EQ})
        # No reverb zone on this map, so only the EQ was sent (index 0 stays
        # dry) -- the two slots are independent.
        self.assertEqual({send[2] for send in self.sends(efx, cinema_speech.REVERB_SEND)},
                         {None})

    def test_the_reverb_is_looked_up_where_the_cabinet_stands(self):
        """The listener standing in a reverb zone does not give the room one."""
        game, efx = self.make(listener=(70.0, 20.0, 0.0))
        game.gameplay.map.reverb_list.append(FakeReverb(
            self.REVERB, bounds=(65.0, 75.0, 15.0, 25.0, -10.0, 10.0)))
        self.feed(game)
        self.assertEqual({send[2] for send in self.sends(efx, cinema_speech.REVERB_SEND)},
                         {None})

    def test_a_reverb_that_goes_away_stops_reverberating(self):
        game, efx = self.make(reverb=self.REVERB)
        self.feed(game)
        game.gameplay.map.reverb_list.clear()
        self.feed(game)
        self.assertEqual(self.sends(efx, cinema_speech.REVERB_SEND)[-1][2], None)

    def test_the_owners_own_monitor_hears_the_room_too(self):
        game, efx = self.make(reverb=self.REVERB)
        self.feed(game, producer="mic")
        self.assertEqual({send[2] for send in self.sends(efx, cinema_speech.REVERB_SEND)},
                         {self.REVERB})

    def test_the_room_is_not_re_asked_for_its_reverb_every_frame(self):
        """Both are properties of the cabinet, re-read on the room's cadence."""
        cinema_speech.RoomSpeechLeg.ENVIRONMENT_INTERVAL_S = 1.0
        game, _efx = self.make(reverb=self.REVERB)
        with mock.patch.object(cinema_speech, "room_environment",
                               wraps=cinema_speech.room_environment) as asked:
            self.feed(game)
            self.feed(game)
            self.feed(game)
        self.assertEqual(asked.call_count, 1)


class OwnerMonitorTests(unittest.TestCase):
    """What the person speaking hears of themselves: the room's speakers too."""

    def feed_local(self, game, producer="mic", frame=FRAME):
        from libs.voice_chat import _feed_local_megaphone_main
        game.gameplay.player = SimpleNamespace(id=7, name="performer")
        game.gameplay.megaphone = SimpleNamespace(
            get_megaphone_player_sources=mock.Mock(return_value=[object()]),
            player_sources={},
        )
        _feed_local_megaphone_main(game.gameplay, frame, producer=producer)
        return game.gameplay.megaphone

    def test_the_owners_own_voice_comes_out_of_the_room(self):
        game = make_game()
        megaphone = self.feed_local(game)
        # The room took the frame: the PA sources were never even asked for.
        megaphone.get_megaphone_player_sources.assert_not_called()
        leg = game.audio_mngr.cinema_speech.legs["7:mic"]
        self.assertTrue(leg.monitor)
        self.assertEqual(set(leg.sources), {"front_l", "front_r"})
        self.assertEqual(len(leg.sources["front_l"].queued), 1)

    def test_the_owners_monitor_skips_the_installers_trim(self):
        """That trim is an offset for the people out there, not for their own ears."""
        game = make_game(speakers=(("front_l", 6.5, 26.5, 100.0, 40.0),
                                   ("front_r", 13.5, 26.5, 100.0, 0.0)))
        self.feed_local(game)
        leg = game.audio_mngr.cinema_speech.legs["7:mic"]
        self.assertEqual(leg.holds["front_l"], 0)
        self.assertEqual(len(leg.sources["front_l"].queued), 1)

    def test_a_player_with_no_room_keeps_the_pa_monitor(self):
        """No cabinet (or the switch off) is the PA path, byte for byte."""
        game = make_game(cabinets=())
        megaphone = self.feed_local(game)
        megaphone.get_megaphone_player_sources.assert_called_once_with("7:mic")
        self.assertIsNone(getattr(game.audio_mngr, "cinema_speech", None))

    def test_every_local_producer_is_monitored_from_the_room(self):
        """Voice and the song they sing over stay aligned with each other."""
        for producer in ("mic", "music", "guitar"):
            game = make_game()
            megaphone = self.feed_local(game, producer=producer)
            megaphone.get_megaphone_player_sources.assert_not_called()
            self.assertIn(f"7:{producer}", game.audio_mngr.cinema_speech.legs)


class RoomOnlyChannelTests(unittest.TestCase):
    """A map with a cabinet and no PA speakers has no megaphone channel."""

    def manager(self):
        from libs.systems.megaphone_system import MegaphoneManager
        manager = MegaphoneManager.__new__(MegaphoneManager)
        manager.voice_channels = {}
        manager.game = object()
        return manager

    def test_a_room_only_channel_is_built_without_registering_it(self):
        from libs import consts
        from libs.systems import megaphone_system
        manager = self.manager()
        with mock.patch.object(megaphone_system.voice_chat,
                               "voice_chat_compression",
                               return_value="compression") as make:
            channel = manager.megaphone_channel()
            self.assertIs(manager.megaphone_channel(), channel)   # cached
        make.assert_called_once_with(manager.game, consts.CHANNEL_MEGAPHONE)
        self.assertEqual(channel.vc_compression, "compression")
        # Empty PA sources, and NOT in voice_channels: half the client walks
        # that mapping looking for a vc_source, and the entry it wants is the
        # one the map's own speakers register.
        self.assertEqual(channel.vc_source, [])
        self.assertEqual(manager.voice_channels, {})

    def test_the_maps_own_channel_still_wins(self):
        from libs import consts
        manager = self.manager()
        registered = SimpleNamespace(vc_compression="pa", vc_source=[object()])
        manager.voice_channels[consts.CHANNEL_MEGAPHONE] = registered
        self.assertIs(manager.megaphone_channel(), registered)

    def test_a_real_manager_hands_a_voiceless_map_to_the_room(self):
        """End to end through the real MegaphoneManager: no PA speakers, a
        voice, and the room still receiving.

        ``process_voice_data`` asks for PA sources before it asks about a
        room, so a manager with no speakers must answer None (not raise) or a
        map that has only a cabinet could never carry a voice.
        """
        from libs import consts
        from libs import event_handeler as event_module
        from libs.systems import megaphone_system
        game = make_game()
        manager = self.manager()
        manager.game = game
        self.assertIsNone(manager.get_megaphone_player_sources(7))
        self.assertEqual(manager.player_sources, {})
        game.gameplay.megaphone = manager
        received = []
        compression = SimpleNamespace(
            recieve=lambda *args: received.append(args))
        handler = event_module.EventHandeler.__new__(event_module.EventHandeler)
        handler.game = game
        handler.gameplay = game.gameplay
        # The real manager BUILDS the channel, so what the patch returns is
        # the compression inside it (``megaphone_channel().vc_compression``),
        # exactly like the map's own channel carries one.
        with mock.patch.object(megaphone_system.voice_chat,
                               "voice_chat_compression",
                               return_value=compression), \
                mock.patch.object(event_module.options, "get", return_value=True):
            handler.process_voice_data(b"\x07\xf8\xff\xfe",
                                       consts.CHANNEL_MEGAPHONE)
        self.assertEqual(len(received), 1)
        self.assertEqual(received[0][1], [])          # no PA sources to feed
        self.assertEqual(received[0][5], 7)           # the talker's channel id


class PaTestModeTests(unittest.TestCase):
    """The O key on a map with a cabinet and no PA speakers.

    PA Test Mode is how a room gets tried out: talk, listen, and hear it come
    out of the speakers around the cabinet. On a map with no PA speakers it
    used to refuse outright, which left nobody able to test a room without
    placing PA speakers that exist purely for the test.
    """

    def gameplay(self, game, staff=True, started=False):
        return SimpleNamespace(
            game=game, game_started=started, pa_test_mode=False,
            megaphone=SimpleNamespace(sources=[],
                                      setup_megaphone_speakers=lambda **kw: None),
            map=SimpleNamespace(megaphone_speakers=[]),
            voice_channels={},
            kc=SimpleNamespace(get=lambda key, default: default),
            is_staff=staff, is_builder=False, is_technician=False,
            can_broadcast_megaphone=False,
            voice_chat=SimpleNamespace(recording=False, vc_compression=None),
            player=SimpleNamespace(x=ANCHOR[0], y=ANCHOR[1], z=0.0),
        )

    def finish_toggle(self, gp):
        from libs import gameplay as gameplay_module
        with mock.patch.object(gameplay_module, "speak") as spoken:
            gameplay_module.Gameplay._finish_pa_toggle(gp)
        return [str(call) for call in spoken.call_args_list]

    def test_the_room_lets_the_pa_test_toggle_on(self):
        game = make_game()
        gp = self.gameplay(game)
        said = self.finish_toggle(gp)
        self.assertTrue(gp.pa_test_mode)
        self.assertFalse(any("No PA speakers" in line for line in said), said)

    def test_without_a_room_it_still_refuses(self):
        game = make_game(cabinets=())
        gp = self.gameplay(game)
        said = self.finish_toggle(gp)
        self.assertFalse(gp.pa_test_mode)
        self.assertTrue(any("No PA speakers" in line for line in said), said)

    def test_a_talker_out_of_the_rooms_reach_does_not_arm_the_key(self):
        game = make_game()
        gp = self.gameplay(game)
        gp.player = SimpleNamespace(x=ANCHOR[0] + ROOM_MAX_DISTANCE + 200.0,
                                    y=ANCHOR[1], z=0.0)
        said = self.finish_toggle(gp)
        self.assertFalse(gp.pa_test_mode)
        self.assertTrue(any("No PA speakers" in line for line in said), said)

    def test_the_switch_turned_off_gives_the_old_refusal(self):
        from libs import options
        game = make_game()
        gp = self.gameplay(game)
        options.prefs[cinema_speech.OPTION_ENABLED] = False
        self.addCleanup(options.prefs.pop, cinema_speech.OPTION_ENABLED, None)
        said = self.finish_toggle(gp)
        self.assertFalse(gp.pa_test_mode)
        self.assertTrue(any("No PA speakers" in line for line in said), said)

    def test_a_voice_arrives_on_a_map_with_no_pa_channel(self):
        """The receive path: no channel entry, but the room still plays it."""
        from libs import consts
        from libs import event_handeler as event_module
        game = make_game()
        received = []
        channel = SimpleNamespace(vc_compression=SimpleNamespace(
            recieve=lambda *args: received.append(args)))
        game.gameplay.megaphone = SimpleNamespace(
            get_megaphone_player_sources=mock.Mock(return_value=None),
            megaphone_channel=mock.Mock(return_value=channel),
        )
        handler = event_module.EventHandeler.__new__(event_module.EventHandeler)
        handler.game = game
        handler.gameplay = game.gameplay
        with mock.patch.object(event_module.options, "get", return_value=True):
            handler.process_voice_data(b"\x07\xf8\xff\xfe", consts.CHANNEL_MEGAPHONE)
        self.assertEqual(len(received), 1)
        self.assertEqual(received[0][1], [])          # no PA sources to feed
        self.assertEqual(received[0][5], 7)           # the talker's channel id


class MegaphoneDrainTests(unittest.TestCase):
    """The seam: the audio worker hands a frame to a room or to the PA."""

    def make_voice(self, game, sender_id=7, sources=(), stale=False):
        from libs.voice_chat import voice_chat_compression
        voice = voice_chat_compression.__new__(voice_chat_compression)
        voice.game = game
        buffer = SimpleNamespace(
            should_output=lambda now: True,
            get_packet=lambda: FRAME,
            reset=lambda: None,
        )
        voice._megaphone_playouts = {
            sender_id: {
                "gameplay": game.gameplay,
                "sources": list(sources),
                "jitter_buffer": buffer,
                "last_packet_monotonic": (0.0 if stale else 1e12),
            }
        }
        voice._megaphone_decoders = {sender_id: object()}
        if sources:
            # A talker the map's PA is already carrying: the stream counts as
            # live through those sources.
            game.gameplay.megaphone.player_sources[sender_id] = {
                "sources": list(sources)}
        return voice

    def test_a_talker_in_a_room_is_fed_from_the_room(self):
        game = make_game()
        voice = self.make_voice(game)
        with mock.patch.object(cinema_speech, "feed") as room_feed, \
                mock.patch("libs.voice_chat.queue_and_delay_frame") as pa:
            voice._drain_megaphone_playout(now_ms=1000.0, now_monotonic=1.0)
        room_feed.assert_called_once()
        self.assertEqual(room_feed.call_args[0][0], game)
        self.assertEqual(room_feed.call_args[0][2], 7)
        self.assertEqual(room_feed.call_args[0][3], FRAME)
        pa.assert_not_called()

    def test_a_map_with_no_pa_speakers_still_carries_the_voice(self):
        """The whole stream used to die here: no PA sources to check for."""
        game = make_game()
        voice = self.make_voice(game, sources=())
        with mock.patch.object(cinema_speech, "feed") as room_feed:
            voice._drain_megaphone_playout(now_ms=1000.0, now_monotonic=1.0)
        self.assertIn(7, voice._megaphone_playouts)
        room_feed.assert_called_once()

    def test_a_talker_with_no_room_keeps_the_pa_path(self):
        game = make_game(cabinets=())
        sources = [object(), object()]
        voice = self.make_voice(game, sources=sources)
        with mock.patch("libs.voice_chat.queue_and_delay_frame") as pa, \
                mock.patch.object(cinema_speech, "feed") as room_feed:
            voice._drain_megaphone_playout(now_ms=1000.0, now_monotonic=1.0)
        pa.assert_called_once()
        self.assertEqual(pa.call_args[0][2], sources)
        room_feed.assert_not_called()

    def test_a_stale_talker_hands_the_room_back(self):
        game = make_game()
        voice = self.make_voice(game, stale=True)
        with mock.patch.object(cinema_speech, "drop") as drop:
            voice._drain_megaphone_playout(now_ms=1000.0, now_monotonic=1e13)
        self.assertEqual(voice._megaphone_playouts, {})
        drop.assert_called_once()
        self.assertEqual(drop.call_args[0][1], 7)


class SendChannelTests(unittest.TestCase):
    """Which compression a talker SENDS on: the map's PA first, the room second.

    The map's own channel is what a hold-to-talk press has always used, and it
    must stay that (with the room-only channel only filling a gap where the map
    has no PA at all). A megaphone object that predates the room -- a stripped
    stub, an older client -- must not turn a key press into a crash either.
    """

    def gameplay(self, game, channels=None, megaphone_extra=None, room=True):
        game.direct_soundgroup = SimpleNamespace(play=mock.Mock())
        megaphone = SimpleNamespace(sources=[],
                                    setup_megaphone_speakers=lambda **kw: None)
        megaphone.__dict__.update(megaphone_extra or {})
        return SimpleNamespace(
            game=game, gameplay_state=True, game_started=True,
            pa_test_mode=False,
            wmanager=SimpleNamespace(
                activeWeapon=SimpleNamespace(name="Megaphone")),
            voice_channels=dict(channels or {}),
            megaphone=megaphone,
            map=SimpleNamespace(megaphone_speakers=[]),
            player=SimpleNamespace(x=(ANCHOR if room else (400.0, 400.0))[0],
                                   y=(ANCHOR if room else (400.0, 400.0))[1],
                                   z=0.0),
            voice_chat=SimpleNamespace(
                audio_input=SimpleNamespace(start=mock.Mock()),
                recording=False, vc_compression=None),
            voice_chat_using_megaphone=False, voice_chat_toggle_on=False,
            _default_vc_compression=None,
        )

    def start(self, gp):
        from libs import gameplay as gameplay_module
        with mock.patch.object(gameplay_module.options, "get", return_value=True), \
                mock.patch.object(gameplay_module, "speak"):
            gameplay_module.Gameplay.voice_chat_start(gp, 0)

    def test_the_maps_own_pa_channel_is_what_a_talker_sends_on(self):
        from libs import consts
        game = make_game()
        registered = SimpleNamespace(vc_compression="pa-compression")
        gp = self.gameplay(game, channels={consts.CHANNEL_MEGAPHONE: registered})
        gp.megaphone.megaphone_channel = mock.Mock()
        self.start(gp)
        self.assertEqual(gp.voice_chat.vc_compression, "pa-compression")
        # The room-only helper is the fallback, never the first answer.
        gp.megaphone.megaphone_channel.assert_not_called()
        gp.voice_chat.audio_input.start.assert_called_once()

    def test_a_map_with_no_pa_channel_sends_on_the_rooms_channel(self):
        game = make_game()
        room_channel = SimpleNamespace(vc_compression="room-compression")
        gp = self.gameplay(game, channels={})
        gp.megaphone.megaphone_channel = lambda: room_channel
        self.start(gp)
        self.assertEqual(gp.voice_chat.vc_compression, "room-compression")
        self.assertTrue(gp.voice_chat.recording)
        self.assertTrue(gp.voice_chat_using_megaphone)

    def test_a_megaphone_without_the_room_helper_still_starts(self):
        """No PA channel and no helper: the key press must not raise."""
        game = make_game()
        gp = self.gameplay(game, channels={})
        self.start(gp)
        self.assertIsNone(gp.voice_chat.vc_compression)
        self.assertTrue(gp.voice_chat.recording)
        gp.voice_chat.audio_input.start.assert_called_once()


def voiced_frame(index, samples=960, low=80.0, top=4000.0):
    """A voice-like frame: a low fundamental and a bright formant together.

    The fundamental sits below the crossover these tests dial in (120 Hz), so
    "the bass is kept" is measured where the filter really does pass it -- at
    the corner itself a cascade is already a few dB down, which is the filter
    working as designed rather than the bass being lost.
    """
    values = []
    for step in range(samples):
        when = (index * samples + step) / float(SAMPLERATE)
        values.append(int(6000 * math.sin(2 * math.pi * low * when)
                          + 6000 * math.sin(2 * math.pi * top * when)))
    return array.array("h", values).tobytes()


class BassCabinetVoiceTests(unittest.TestCase):
    """A cabinet that is a sub plays the low end of the room's voice -- and only it.

    A voice reaches a room the way a note does, at its speakers rather than
    through a frame queue, so a bass cabinet needs the same crossover the song
    gets: applied to the frames this leg is about to hand that one speaker
    (``speech._voiced``), carrying its state across frames the way a stream
    has to. A frame the holder had no buffer for is not walked through the
    filter on its way to the bin.
    """

    def leg(self, game, sender_id=7):
        self.assertTrue(cinema_speech.feed(game, game.gameplay, sender_id,
                                           voiced_frame(0)))
        return game.audio_mngr.cinema_speech.legs[sender_id]

    def test_only_the_marked_speaker_loses_the_top(self):
        game = make_game(crossovers={"front_l": 120})
        leg = self.leg(game)
        for index in range(1, 5):
            cinema_speech.feed(game, game.gameplay, 7, voiced_frame(index))
        raw = voiced_frame(4)
        plain = leg.sources["front_r"].queued[-1].data
        cabinet = leg.sources["front_l"].queued[-1].data
        self.assertEqual(plain, raw,
                         "an unmarked speaker is handed the frame as it is")
        self.assertLess(magnitude(cabinet, 4000.0),
                        0.02 * magnitude(raw, 4000.0))
        self.assertGreater(magnitude(cabinet, 80.0),
                           0.5 * magnitude(raw, 80.0))

    def test_the_voice_filter_carries_across_frames(self):
        """A stream, like the song: a per-frame filter would click every 20 ms."""
        game = make_game(crossovers={"front_l": 120})
        leg = self.leg(game)
        frames = [voiced_frame(index) for index in range(6)]
        for frame in frames[1:]:
            cinema_speech.feed(game, game.gameplay, 7, frame)
        reference = []
        state = None
        for frame in frames:
            (voiced,), state = crossover_apply((frame,), state, 120)
            reference.append(voiced)
        published = [buffer.data for buffer in leg.sources["front_l"].queued]
        self.assertEqual(digests(published), digests(reference))

    def test_a_frame_with_no_buffer_does_not_walk_the_filter_on(self):
        game = make_game(crossovers={"front_l": 120})
        leg = self.leg(game)
        before = leg.filters["front_l"]
        frame = voiced_frame(1)
        holder = game.audio_mngr.cinema_speech
        with mock.patch.object(holder, "take_buffer", lambda: None):
            self.assertTrue(cinema_speech.feed(game, game.gameplay, 7, frame))
        self.assertEqual(leg.filters["front_l"], before,
                         "a frame nobody played advanced the cabinet's filter")
        cinema_speech.feed(game, game.gameplay, 7, frame)
        reference = []
        state = None
        for chunk in (voiced_frame(0), frame):
            (voiced,), state = crossover_apply((chunk,), state, 120)
            reference.append(voiced)
        published = [buffer.data for buffer in leg.sources["front_l"].queued]
        self.assertEqual(digests(published), digests(reference))

    def test_a_mark_dialled_mid_sentence_re_cuts_the_speaker(self):
        """The map is live for a voice too: the mark is read again, not kept.

        The speaker was marked only when it was created, so a builder dialling
        a crossover heard it on the next talker -- and the same speaker's own
        voicing waited the same way. Nothing is rebuilt here: the room's
        speakers are the ones already carrying the voice.
        """
        game = make_game()
        leg = self.leg(game)
        self.assertEqual(leg.crossovers["front_l"], 0.0)
        source = leg.sources["front_l"]
        # The builder's edit, on the element the room resolves from.
        next(spec for spec in game.gameplay.map.cinema_speaker_list
             if spec.channel == "front_l").crossover = 120
        # The router re-resolves the room at most once a second (a room that
        # answers the map that fast is what a live edit expects); this test is
        # about the leg, so the cache is dropped to make the edit visible now.
        cinema_speech._router(game)._cache = None
        frame = voiced_frame(1)
        cinema_speech.feed(game, game.gameplay, 7, frame)
        self.assertEqual(leg.crossovers["front_l"], 120.0,
                         "the mark never reached the voice's room")
        self.assertIs(leg.sources["front_l"], source,
                      "the room was rebuilt for a mark")
        self.assertLess(magnitude(source.queued[-1].data, 4000.0),
                        0.02 * magnitude(frame, 4000.0))
        self.assertEqual(leg.sources["front_r"].queued[-1].data, frame,
                         "the speaker nobody marked was touched")

    def test_a_mark_taken_away_gives_the_voice_back_untouched(self):
        game = make_game(crossovers={"front_l": 120})
        leg = self.leg(game)
        source = leg.sources["front_l"]
        next(spec for spec in game.gameplay.map.cinema_speaker_list
             if spec.channel == "front_l").crossover = 0
        cinema_speech._router(game)._cache = None
        cinema_speech.feed(game, game.gameplay, 7, voiced_frame(1))
        self.assertEqual(leg.crossovers["front_l"], 0.0)
        self.assertIs(leg.sources["front_l"], source, "the room was rebuilt")
        self.assertEqual(source.queued[-1].data, voiced_frame(1),
                         "a full-range speaker must be handed the voice itself")

    def test_a_room_that_is_not_a_bass_cabinet_is_handed_the_frame_untouched(self):
        game = make_game()
        leg = self.leg(game)
        for index in range(1, 4):
            cinema_speech.feed(game, game.gameplay, 7, voiced_frame(index))
        for slot in ("front_l", "front_r"):
            self.assertEqual(leg.sources[slot].queued[-1].data, voiced_frame(3))
            self.assertEqual(leg.crossovers[slot], 0.0)


if __name__ == "__main__":
    unittest.main()
