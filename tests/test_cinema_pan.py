"""Staff panning: where one player's voice -- and their band -- comes out.

A room already answers "which speakers carry this sound" from *where the sound
is*: a talker standing in a cabinet's reach is heard from that room, a band
plays at the room nearest the performer. A staff pan replaces that question
with a named destination -- this cabinet, leaning this way -- and these tests
pin the four things that make it a feature rather than a toy:

    * the direction law is equal-energy (a pan is a place, not a fader) and
      carries a speaker's trim, wall and channel half through untouched;
    * a destination is a cabinet the map actually has -- a deleted one, or one
      the map set to ``off``, resolves to nothing rather than to an invented
      room;
    * staff outrank the rule that a talker has to be standing *in* the room
      (that is the whole point of a pan) but **never** the listener's own
      switches -- a pan chooses which room, not whether somebody hears one, so
      a listener who asked for the PA keeps the PA;
    * the same destination reaches a note and a voice through one table, keyed
      by the two names a player has (their name on the instrument path, their
      voice channel on the voice path).
"""

import os
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from libs import cinema_pan_menu
from libs.audio.cinema import ROOM_RADIUS
from libs.audio.cinema import live as cinema_live
from libs.audio.cinema import pan as cinema_pan
from libs.audio.cinema import plugin as cinema_plugin
from libs.audio.cinema import speech as cinema_speech
from libs.world_map import Map

ANCHOR_ONE = (10.0, 20.0, 0.0)
ANCHOR_TWO = (160.0, 20.0, 0.0)

# The listener's own switch (libs/audio/cinema/speech.py). Set straight into
# ``options.prefs`` so a test never writes the player's settings file, and put
# back by the module so one test cannot decide what the next one hears.
SPEECH_KEY = cinema_speech.OPTION_ENABLED
# The jukebox's own switch ("Cinema rooms:"). It belongs to the *song*: one
# listening choice per shape of sound, so it is set here only to prove it never
# takes a voice or a band with it.
ROOMS_KEY = cinema_plugin.OPTION_ENABLED
_SNAPSHOT = None


def setUpModule():
    global _SNAPSHOT
    from libs import options
    _SNAPSHOT = (options.prefs.get(SPEECH_KEY), options.prefs.get(ROOMS_KEY))


def tearDownModule():
    from libs import options
    speech, rooms = _SNAPSHOT or (None, None)
    if speech is None:
        options.prefs.pop(SPEECH_KEY, None)
    else:
        options.prefs[SPEECH_KEY] = speech
    if rooms is None:
        options.prefs.pop(ROOMS_KEY, None)
    else:
        options.prefs[ROOMS_KEY] = rooms


def make_game(cabinets=((("j1", ANCHOR_ONE), (("front_l", 6.5, 26.5),
                                             ("front_r", 13.5, 26.5))),
                        (("j2", ANCHOR_TWO), (("front_l", 156.5, 26.5),
                                             ("front_r", 163.5, 26.5)))),
              talker=(12.0, 20.0, 0.0), modes=None, listener=(10.0, 25.0, 0.0),
              speech=True, rooms=True):
    """A map with two cabinets (each with its own stereo front pair).

    The speakers are placed with the labels the room expects so a real resolver
    runs -- nothing here hand-builds a placement -- and the two cabinets stand
    far enough apart that a speaker belongs to exactly one of them.
    """
    from libs import options
    options.prefs[SPEECH_KEY] = bool(speech)
    options.prefs[ROOMS_KEY] = bool(rooms)
    game = SimpleNamespace()
    game.audio_mngr = SimpleNamespace(position=listener,
                                      volume_categories={"jukebox": [100]})
    map_obj = Map(game)
    for (cabinet_id, _anchor), speakers in cabinets:
        for slot, x, y in speakers:
            map_obj.spawn_cinemaSpeaker(minx=x - 0.5, maxx=x + 0.5,
                                        miny=y - 0.5, maxy=y + 0.5,
                                        minz=0, maxz=1,
                                        id=f"{cabinet_id}_{slot}", channel=slot,
                                        level=100.0, delay=0.0)
    map_obj.jukebox_list = [SimpleNamespace(id=cabinet_id, center=center)
                            for (cabinet_id, center), _speakers in cabinets]
    modes = dict(modes or {})
    entity = SimpleNamespace(name="Somchai", x=talker[0], y=talker[1],
                             z=talker[2], voice_channel=7)
    gameplay = SimpleNamespace(
        map=map_obj, voice_channels={7: entity}, player=entity,
        music_bot=SimpleNamespace(volume=50),
        jukebox_player=SimpleNamespace(
            cinema_mode=lambda cabinet_id: modes.get(cabinet_id, "auto"),
            occlusion_tier=lambda *args: 0),
        game=game)
    game.gameplay = gameplay
    game.call_after = lambda ms, fn: None
    return game


def terms_by_slot(router, plan, **kwargs):
    return {term[0]: term for term in router.terms_for_plan(plan, **kwargs)}


class DirectionLawTests(unittest.TestCase):
    """The arithmetic: a pan moves the sound, it does not change its level."""

    def test_auto_is_the_shipped_mix_and_touches_nothing(self):
        terms = (("front_l", (0, 0, 0), 1.0, 0.0, 0, "l"),
                 ("front_r", (0, 0, 0), 1.0, 0.0, 0, "r"))
        self.assertEqual(cinema_pan.apply_direction(terms, "auto"), terms)
        self.assertEqual(cinema_pan.apply_direction(terms, None), terms)
        self.assertEqual(cinema_pan.apply_direction(terms, "sideways"), terms)

    def test_each_side_only_moves_its_own_side(self):
        self.assertGreater(cinema_pan.direction_factor("side_l", "left"),
                           cinema_pan.direction_factor("side_r", "left"))
        self.assertGreater(cinema_pan.direction_factor("side_r", "right"),
                           cinema_pan.direction_factor("side_l", "right"))
        # A centre speaker has no side of its own, so it neither wins nor dies.
        centre = cinema_pan.direction_factor("front_c", "left")
        self.assertGreater(centre, cinema_pan.direction_factor("side_r", "left"))
        self.assertLess(centre, cinema_pan.direction_factor("side_l", "left"))

    def test_front_and_back_lean_by_depth_not_by_side(self):
        self.assertGreater(cinema_pan.direction_factor("front_l", "front"),
                           cinema_pan.direction_factor("rear_l", "front"))
        self.assertGreater(cinema_pan.direction_factor("rear_r", "back"),
                           cinema_pan.direction_factor("front_r", "back"))
        self.assertEqual(cinema_pan.direction_factor("front_c", "front"),
                         cinema_pan.direction_factor("front_l", "front"))

    def test_centre_lifts_the_middle_and_keeps_the_rest(self):
        self.assertGreater(cinema_pan.direction_factor("front_c", "centre"), 1.0)
        self.assertLess(cinema_pan.direction_factor("side_l", "centre"), 1.0)
        self.assertGreater(cinema_pan.direction_factor("side_l", "centre"), 0.0)

    def test_a_pan_keeps_the_rooms_energy(self):
        terms = (("front_l", (0, 0, 0), 1.0, 0.0, 0, "l"),
                 ("front_r", (0, 0, 0), 1.0, 0.0, 0, "r"),
                 ("side_l", (0, 0, 0), 0.5, 20.0, 1, None),
                 ("side_r", (0, 0, 0), 0.5, 20.0, 1, None))
        for direction in cinema_pan.DIRECTIONS:
            moved = cinema_pan.apply_direction(terms, direction)
            before = sum(term[2] ** 2 for term in terms)
            after = sum(term[2] ** 2 for term in moved)
            self.assertAlmostEqual(before, after, places=9, msg=direction)

    def test_a_pan_carries_everything_after_the_gain_untouched(self):
        terms = (("side_l", (1.0, 2.0, 3.0), 1.0, 20.0, 2, None),
                 ("side_r", (4.0, 5.0, 6.0), 1.0, 0.0, 0, None))
        moved = cinema_pan.apply_direction(terms, "left")
        self.assertEqual(moved[0][0], "side_l")
        self.assertEqual(moved[0][1], (1.0, 2.0, 3.0))
        self.assertEqual(moved[0][3:], (20.0, 2, None))
        self.assertEqual(moved[1][3:], (0.0, 0, None))
        self.assertNotEqual(moved[0][2], terms[0][2])

    def test_one_speaker_cannot_be_panned(self):
        # Equal energy means a room with a single speaker has nowhere to move
        # the sound to: the direction is heard as nothing at all, which is the
        # honest answer rather than a level change nobody asked for.
        terms = (("front_c", (0.0, 0.0, 0.0), 1.0, 0.0, 0, None),)
        self.assertEqual(cinema_pan.apply_direction(terms, "left"), terms)


class PanTableTests(unittest.TestCase):
    """One player, two names, one entry -- and no entry means no pan."""

    def test_a_pan_is_found_by_name_and_by_voice_channel(self):
        gameplay = SimpleNamespace()
        table = cinema_pan.table_for(gameplay)
        table.set("Somchai", 7, "j2", "left")
        self.assertEqual(cinema_pan.target_for_name(gameplay, "Somchai"),
                         ("j2", "left"))
        self.assertEqual(cinema_pan.target_for_channel(gameplay, 7),
                         ("j2", "left"))
        self.assertEqual(cinema_pan.direction_for_channel(gameplay, 7), "left")

    def test_clearing_removes_both_names(self):
        gameplay = SimpleNamespace()
        table = cinema_pan.table_for(gameplay)
        table.set("Somchai", 7, "j2", "left")
        table.clear("Somchai")
        self.assertIsNone(cinema_pan.target_for_name(gameplay, "Somchai"))
        self.assertIsNone(cinema_pan.target_for_channel(gameplay, 7))
        self.assertEqual(cinema_pan.describe(gameplay), [])

    def test_an_empty_cabinet_clears_the_pan(self):
        gameplay = SimpleNamespace()
        cinema_pan.table_for(gameplay).set("Somchai", 7, "j2", "left")
        cinema_pan.apply_packet(gameplay, {"channel": 7, "name": "Somchai",
                                           "cabinet": "", "direction": "auto"})
        self.assertIsNone(cinema_pan.target_for_channel(gameplay, 7))

    def test_a_packet_without_a_name_finds_one_from_the_table(self):
        gameplay = SimpleNamespace()
        cinema_pan.apply_packet(gameplay, {"channel": 7, "name": "Somchai",
                                           "cabinet": "j1", "direction": "back"})
        cinema_pan.apply_packet(gameplay, {"channel": 7, "cabinet": "",
                                           "direction": "auto"})
        self.assertIsNone(cinema_pan.target_for_channel(gameplay, 7))

    def test_a_junk_direction_falls_back_to_the_rooms_own_mix(self):
        gameplay = SimpleNamespace()
        entry = cinema_pan.apply_packet(gameplay, {"channel": 7, "name": "S",
                                                   "cabinet": "j1",
                                                   "direction": "diagonal"})
        self.assertEqual(entry[3], "auto")

    def test_a_client_with_no_pan_owns_no_table(self):
        gameplay = SimpleNamespace()
        self.assertIsNone(cinema_pan.table_for(gameplay, create=False))
        self.assertIsNone(cinema_pan.target_for_channel(gameplay, 7))
        self.assertIsNone(cinema_pan.target_for_name(gameplay, "Somchai"))

    def test_a_missing_gameplay_is_never_an_error(self):
        self.assertIsNone(cinema_pan.table_for(None))
        self.assertIsNone(cinema_pan.target_for_channel(None, 7))
        self.assertIsNone(cinema_pan.apply_packet(None, {"channel": 7}))


class RoomByNameTests(unittest.TestCase):
    """A destination is a cabinet the map has, resolved the way a room is."""

    def test_a_named_cabinet_resolves_to_its_own_room(self):
        game = make_game()
        router = cinema_live.router_for(game.audio_mngr)
        router.game = game
        first = router.room_by_id("j1")
        second = router.room_by_id("j2")
        self.assertEqual(first[0], "j1")
        self.assertEqual(second[0], "j2")
        first_slots = {term[0] for term in router.terms_for_plan(first[1])}
        self.assertIn("front_l", first_slots)

    def test_an_unknown_cabinet_resolves_to_nothing(self):
        game = make_game()
        router = cinema_live.router_for(game.audio_mngr)
        router.game = game
        self.assertIsNone(router.room_by_id("nope"))
        self.assertIsNone(router.room_by_id(""))
        self.assertIsNone(router.room_by_id(None))

    def test_a_cabinet_the_map_turned_off_is_not_a_destination(self):
        game = make_game(modes={"j2": "off"})
        router = cinema_live.router_for(game.audio_mngr)
        router.game = game
        self.assertIsNone(router.room_by_id("j2"))
        self.assertIsNotNone(router.room_by_id("j1"))

    def test_a_direction_reaches_the_plan_without_moving_the_room(self):
        game = make_game()
        router = cinema_live.router_for(game.audio_mngr)
        router.game = game
        plan = router.room_by_id("j1")[1]
        plain = terms_by_slot(router, plan)
        left = terms_by_slot(router, plan, direction="left")
        self.assertEqual(sorted(plain), sorted(left))
        self.assertGreater(left["front_l"][2], plain["front_l"][2])
        self.assertLess(left["front_r"][2], plain["front_r"][2])
        self.assertAlmostEqual(sum(term[2] ** 2 for term in plain.values()),
                               sum(term[2] ** 2 for term in left.values()),
                               places=9)


class VoicePanTests(unittest.TestCase):
    """Voice: a pan outranks the radius rule, never the listener's switches."""

    def test_a_panned_talker_needs_no_room_around_them(self):
        game = make_game(talker=(500.0, 500.0, 0.0))
        cinema_pan.table_for(game.gameplay).set("Somchai", 7, "j1", "left")
        target = cinema_speech.room_target(game, game.gameplay, 7)
        self.assertIsNotNone(target)
        self.assertEqual(target[0], "j1")
        self.assertEqual(cinema_speech.routed(game, game.gameplay, 7), True)

    def test_the_direction_travels_with_the_voice(self):
        game = make_game(talker=(500.0, 500.0, 0.0))
        cinema_pan.table_for(game.gameplay).set("Somchai", 7, "j1", "right")
        target = cinema_speech.room_target(game, game.gameplay, 7)
        self.assertEqual(target[2], "right")

    def test_a_listener_who_turned_the_room_off_keeps_the_pa(self):
        """A pan names a room; it is not a way past somebody's own choice.

        The voice still reaches them -- on the map's PA, exactly what the
        Speech line promises -- so the announcement is never lost, only not
        placed for that pair of ears.
        """
        game = make_game(talker=(500.0, 500.0, 0.0), speech=False)
        cinema_pan.table_for(game.gameplay).set("Somchai", 7, "j1", "")
        self.assertIsNone(cinema_speech.room_target(game, game.gameplay, 7))
        self.assertFalse(cinema_speech.routed(game, game.gameplay, 7))

    def test_turning_the_jukeboxs_rooms_off_leaves_a_panned_voice_alone(self):
        """One listening choice per shape of sound: ``Cinema rooms:`` is the song.

        It used to be a hidden master -- off meant no room carried anything
        here, a panned voice included -- which made a panned voice look dead
        for whoever had turned off a line about *jukeboxes*. The Speech line is
        the voice's own answer, so it is the only one that can take a voice
        off the room.
        """
        game = make_game(talker=(500.0, 500.0, 0.0), rooms=False)
        cinema_pan.table_for(game.gameplay).set("Somchai", 7, "j1", "")
        self.assertIsNotNone(cinema_speech.room_target(game, game.gameplay, 7))
        # ...and the Speech line still does, which is what keeps the switch
        # honest instead of redundant.
        from libs import options
        options.prefs[SPEECH_KEY] = False
        self.assertIsNone(cinema_speech.room_target(game, game.gameplay, 7))
        options.prefs[SPEECH_KEY] = True

    def test_an_unpanned_voice_keeps_every_rule_it_had(self):
        from libs import options
        options.prefs[SPEECH_KEY] = False
        inside = make_game(speech=False)          # talker stands in the room
        self.assertIsNone(cinema_speech.room_target(inside, inside.gameplay, 7))
        options.prefs[SPEECH_KEY] = True
        self.assertIsNotNone(cinema_speech.room_target(inside, inside.gameplay, 7))
        away = make_game(talker=(500.0, 500.0, 0.0))
        self.assertIsNone(cinema_speech.room_target(away, away.gameplay, 7))

    def test_a_pan_at_a_cabinet_the_map_lost_puts_the_voice_back_on_the_pa(self):
        game = make_game(talker=(500.0, 500.0, 0.0), modes={"j1": "off"})
        cinema_pan.table_for(game.gameplay).set("Somchai", 7, "j1", "left")
        self.assertIsNone(cinema_speech.room_target(game, game.gameplay, 7))
        self.assertFalse(cinema_speech.routed(game, game.gameplay, 7))

    def test_the_owners_own_monitor_stays_where_they_stand(self):
        # feed_local answers about the body, not about the pan: a performer
        # panned away still hears their own line from the room they stand in.
        game = make_game(talker=(500.0, 500.0, 0.0))
        cinema_pan.table_for(game.gameplay).set("Somchai", 7, "j1", "left")
        self.assertIsNone(cinema_speech.local_room_target(game, game.gameplay))


class NotePanTests(unittest.TestCase):
    """Notes: the band comes out of the cabinet staff named, not the nearest."""

    def _spawns(self, game, position, pan, listener=(10.0, 25.0, 0.0)):
        played = []
        played_at = lambda x, y, z, gain, tier, delay, channel: played.append(
            (round(x), round(y), round(z)))
        spoken = cinema_live.route_to_room(
            game, position, played_at, listener=listener,
            occlusion_provider=game.gameplay.jukebox_player.occlusion_tier,
            schedule=game.call_after, pan=pan)
        return spoken, played

    def test_a_pan_overrides_which_room_the_note_plays_in(self):
        # The performer stands at j1, the listener at j2's side, and the note
        # was sent to j2: the performer's own position decides nothing.
        game = make_game()
        spoken, played = self._spawns(game, ANCHOR_ONE, ("j2", "auto"),
                                      listener=(160.0, 25.0, 0.0))
        self.assertEqual(spoken, len(played))
        self.assertTrue(played)
        self.assertTrue(all(x > 100 for x, _y, _z in played), played)

    def test_without_a_pan_the_performers_own_cabinet_wins(self):
        game = make_game()
        _spoken, played = self._spawns(game, ANCHOR_ONE, None)
        self.assertTrue(played)
        self.assertTrue(all(x < 100 for x, _y, _z in played), played)

    def test_a_pan_naming_nothing_plays_the_note_nowhere(self):
        game = make_game()
        spoken, played = self._spawns(game, ANCHOR_ONE, ("nope", "auto"))
        self.assertEqual(spoken, 0)
        self.assertEqual(played, [])

    def test_a_destination_is_still_as_far_as_the_room_is(self):
        # Naming a cabinet does not carry the sound to a listener standing
        # across the map: the room's own ramp still decides who is close
        # enough to hear it (that is what makes it a room and not a broadcast).
        game = make_game()
        spoken, played = self._spawns(game, ANCHOR_ONE, ("j2", "auto"),
                                      listener=(10.0, 25.0, 0.0))
        self.assertEqual(spoken, 0)
        self.assertEqual(played, [])

    def test_the_pan_table_is_what_the_instruments_ask(self):
        # The instrument paths reach the same entry by name (piano/drums pass
        # ``peer_id``, which the Server sends as the player's name).
        game = make_game()
        cinema_pan.table_for(game.gameplay).set("Somchai", 7, "j2", "front")
        self.assertEqual(cinema_pan.target_for_name(game.gameplay, "Somchai"),
                         ("j2", "front"))

    def test_the_room_is_still_the_rooms_own_size(self):
        # A pan names a room; it must not quietly change how far that room is
        # heard (ROOM_RADIUS == ROOM_MAX_DISTANCE is the room's own scale).
        game = make_game()
        router = cinema_live.router_for(game.audio_mngr)
        router.game = game
        plan = router.room_by_id("j2")[1]
        self.assertLessEqual(
            max(((term[1][0] - ANCHOR_TWO[0]) ** 2
                 + (term[1][1] - ANCHOR_TWO[1]) ** 2) ** 0.5
                for term in router.terms_for_plan(plan)),
            ROOM_RADIUS)


class SelfPanTests(unittest.TestCase):
    """Panning *yourself* -- the one move nobody else can make for you.

    A player's own name never reaches their own client through a spawn packet:
    ``Map.add_player`` tells a joiner about everyone on the map *except* them
    (they are not in the player tree yet, and the broadcast announcing them
    excludes the sender). So the local voice channel comes from the login
    snapshot, and these tests pin that the picker uses it -- self first, marked,
    and never invented when the Server did not send one.
    """

    def _gameplay(self, own=9, others=(), me="Somchai"):
        channels = {channel: SimpleNamespace(name=name)
                    for name, channel in others}
        gp = SimpleNamespace(voice_channels=channels,
                             player=SimpleNamespace(name=me))
        gp.own_voice_channel = own
        return gp

    def test_you_are_listed_from_the_login_channel(self):
        gp = self._gameplay(own=9, others=(("Anong", 4),))
        self.assertEqual(cinema_pan_menu._players(gp),
                         [("Somchai", 9), ("Anong", 4)])

    def test_you_are_marked_in_the_picker_and_named_by_the_menu(self):
        gp = self._gameplay(own=9, others=(("Anong", 4),))
        # No map to resolve a room from: the \"you\" tagging is the reader's
        # own, and the room a player belongs to is a separate tag (below).
        self.assertEqual(cinema_pan_menu._label(None, gp, "Somchai", 9),
                         "Somchai (you)")
        self.assertEqual(cinema_pan_menu._label(None, gp, "Anong", 4), "Anong")
        self.assertEqual(cinema_pan_menu._display(gp, "Somchai"), "you")
        self.assertEqual(cinema_pan_menu._display(gp, "Anong"), "Anong")

    def test_a_pan_you_already_have_is_marked_on_your_own_line(self):
        gp = self._gameplay(own=9)
        cinema_pan.table_for(gp).set("Somchai", 9, "j1", "left")
        self.assertEqual(cinema_pan_menu._label(None, gp, "Somchai", 9),
                         "Somchai (you, moved)")

    def test_your_own_spawn_entry_is_never_listed_twice(self):
        # Belt and braces: if a future Server ever spawned you to yourself, the
        # picker still shows one line per person.
        gp = self._gameplay(own=9, others=(("Somchai", 9),))
        self.assertEqual(cinema_pan_menu._players(gp), [("Somchai", 9)])

    def test_a_server_without_the_channel_lists_nobody_of_your_own(self):
        gp = SimpleNamespace(voice_channels={},
                             player=SimpleNamespace(name="Somchai"))
        self.assertEqual(cinema_pan_menu._players(gp), [])
        for junk in ("not a number", True, None):
            gp.own_voice_channel = junk
            self.assertEqual(cinema_pan_menu._players(gp), [])

    def test_a_self_pan_is_sent_under_your_own_name_and_channel(self):
        # The Server resolves the target by that channel and relays the pan back
        # with the player's real name -- the key the instrument path uses.
        sent = []
        game = SimpleNamespace(
            network=SimpleNamespace(send=lambda *args: sent.append(args)))
        self.assertTrue(
            cinema_pan_menu.send_pan(game, "Somchai", 9, "j1", "left"))
        _channel, event, payload = sent[0]
        self.assertEqual(event, "staff_pan")
        self.assertEqual(payload["channel"], 9)
        self.assertEqual(payload["name"], "Somchai")
        self.assertEqual(payload["cabinet"], "j1")
        self.assertEqual(payload["direction"], "left")

    def test_your_own_voice_is_shaped_by_the_destination_like_anybody_elses(self):
        # Panning yourself is not a special case downstream: the same table and
        # the same room shaping carry it, so the voice path and the instrument
        # path agree about where you are heard.
        game = make_game()
        gp = game.gameplay
        gp.own_voice_channel = 7
        cinema_pan.apply_packet(gp, {"channel": 7, "name": "Somchai",
                                     "cabinet": "j2", "direction": "front"})
        self.assertEqual(cinema_pan.target_for_channel(gp, 7), ("j2", "front"))
        self.assertEqual(cinema_pan.target_for_name(gp, "Somchai"),
                         ("j2", "front"))


class PanMenuTests(unittest.TestCase):
    """What the menu says about where somebody's sound *is* right now.

    With nobody having panned a player, the room their voice and band belong to
    is the one at their feet -- resolved the same way on every machine, which is
    what lets this menu name it. Before that the list only ever spoke about
    explicit pans, so "the band came out of a room I never chose" and "clearing
    the pan put the instrument back" had no answer inside the game.
    """

    class FakeMenu:
        """Drop-in for libs.menu.Menu capturing the items it was built with."""

        def __init__(self, *args, **kwargs):
            self.items = []

        def add_items(self, items):
            self.items = list(items)

        def speak_current_item(self):
            pass

    def setUp(self):
        self.game = make_game()
        self.gp = self.game.gameplay
        self.captured = []
        self.gp.add_substate = self.captured.append
        self.gp.pop_last_substate = lambda: None
        self.gp.can_use_cinema_pan = True
        # The reader is somebody else: the picker writes "you" for the staff
        # member holding the menu, and these tests are about how *another*
        # player's line reads.
        self.gp.player = SimpleNamespace(name="Kanya", x=12.0, y=20.0, z=0.0)

    def build(self, opener):
        with mock.patch("libs.menu.Menu", self.FakeMenu), \
                mock.patch("libs.menus.set_default_sounds"):
            opener()
        return self.captured[-1]

    def labels(self, menu):
        return [label() if callable(label) else label for label, _action in menu.items]

    def test_the_picker_says_which_room_a_player_belongs_to(self):
        menu = self.build(lambda: cinema_pan_menu._players_menu(self.game, self.gp))
        self.assertIn("Somchai (at jukebox j1)", self.labels(menu))

    def test_a_moved_player_is_tagged_moved_instead(self):
        cinema_pan.table_for(self.gp).set("Somchai", 7, "j2", "left")
        menu = self.build(lambda: cinema_pan_menu._players_menu(self.game, self.gp))
        labels = self.labels(menu)
        self.assertIn("Somchai (moved)", labels)
        self.assertEqual([label for label in labels if "at jukebox" in label], [])

    def test_the_cabinet_list_marks_the_room_they_are_standing_in(self):
        menu = self.build(lambda: cinema_pan_menu._cabinets_menu(
            self.game, self.gp, "Somchai", 7))
        marked = [label for label in self.labels(menu) if label.startswith("* ")]
        self.assertEqual(len(marked), 1)
        self.assertTrue(marked[0].startswith("* Jukebox j1 "), marked[0])
        self.assertIn("(they are here)", marked[0])

    def test_clearing_names_the_room_they_go_back_to(self):
        """Clearing means the rule, not a fixed place -- and not "the map's PA"."""
        cinema_pan.table_for(self.gp).set("Somchai", 7, "j2", "left")
        menu = self.build(lambda: cinema_pan_menu._cabinets_menu(
            self.game, self.gp, "Somchai", 7))
        self.assertIn("Clear - Somchai goes back to jukebox j1 (the room they are in)",
                      self.labels(menu))
        # ...and where there is no room for them at all, the PA is the truth:
        # the nearest cabinet to somebody across the map is not their room.
        far = make_game(talker=(400.0, 400.0, 0.0))
        gp = far.gameplay
        gp.player = SimpleNamespace(name="Kanya", x=400.0, y=400.0, z=0.0)
        gp.add_substate = self.captured.append
        gp.pop_last_substate = lambda: None
        cinema_pan.table_for(gp).set("Somchai", 7, "j2", "left")
        menu = self.build(lambda: cinema_pan_menu._cabinets_menu(
            far, gp, "Somchai", 7))
        self.assertIn("Clear - Somchai goes back to the map's PA",
                      self.labels(menu))

    def test_the_menu_speaks_about_you_as_you(self):
        # Your own line is the one the reader knows without looking, and it is
        # tagged like anybody else's: you, in the room you are standing in.
        self.gp.own_voice_channel = 9
        menu = self.build(lambda: cinema_pan_menu._players_menu(self.game, self.gp))
        self.assertIn("Kanya (you, at jukebox j1)", self.labels(menu))


class PanReportTests(unittest.TestCase):
    """What the pan that just arrived means *on this machine*.

    The routing is per listener, so the half a tester cannot see is this
    client's own answer: with two clients open a pan lands on both and can be
    heard on neither, and a switch that is off, a listener standing out of the
    destination's reach and an old build all look exactly alike. The report and
    the menu's own confirmation both say ``plugin.listening_summary()``.
    """

    def test_the_report_names_the_destination_and_this_clients_own_answer(self):
        game = make_game()
        with mock.patch.object(cinema_pan, "log_line") as log:
            cinema_pan.apply_packet(game.gameplay,
                                    {"channel": 7, "name": "Somchai",
                                     "cabinet": "j2", "direction": "left"})
        self.assertEqual(len(log.call_args_list), 1)
        said = log.call_args_list[0].args[0]
        self.assertIn("Somchai -> jukebox j2 (left)", said)
        self.assertIn("this client hears:", said)
        self.assertIn("voices from the room", said)

    def test_the_report_says_which_switch_is_the_reason_here(self):
        from libs import options
        game = make_game(speech=False, rooms=False)
        with mock.patch.object(cinema_pan, "log_line") as log:
            cinema_pan.apply_packet(game.gameplay,
                                    {"channel": 7, "name": "Somchai",
                                     "cabinet": "j2", "direction": "auto"})
        said = log.call_args_list[0].args[0]
        self.assertIn("jukebox songs at your ears", said)
        self.assertIn("voices on the map's PA", said)
        options.prefs[SPEECH_KEY] = True
        options.prefs[ROOMS_KEY] = True

    def test_a_cleared_pan_is_reported_too(self):
        game = make_game()
        cinema_pan.table_for(game.gameplay).set("Somchai", 7, "j2")
        with mock.patch.object(cinema_pan, "log_line") as log:
            self.assertIsNone(cinema_pan.apply_packet(
                game.gameplay, {"channel": 7, "name": "Somchai", "cabinet": ""}))
        self.assertIn("Somchai back to their own room",
                      log.call_args_list[0].args[0])

    def test_a_send_says_what_this_client_will_hear(self):
        """The per-listener half of a pan, said to whoever just sent one.

        The routing happens on the *listening* machine, so the client sending a
        pan gets its own answer -- and that answer is the one thing that tells a
        staff member whether the silence they are about to hear is a switch of
        their own or a room that would not resolve.
        """
        sent = []
        game = SimpleNamespace(
            network=SimpleNamespace(send=lambda *args: sent.append(args)))
        gp = SimpleNamespace(pop_last_substate=lambda: None)
        with mock.patch.object(cinema_pan_menu, "speak") as spoke:
            cinema_pan_menu._apply(game, gp, "Somchai", 7, "j2", "left")()
        self.assertTrue(sent)
        said = [call.args[0] for call in spoke.call_args_list]
        self.assertEqual(said[0], "Sending Somchai to jukebox j2.")
        self.assertTrue(said[1].startswith("Here: "), said)
        self.assertIn("voices from the room", said[1])

    def test_a_packet_nobody_can_use_is_not_worth_a_line(self):
        game = make_game()
        with mock.patch.object(cinema_pan, "log_line") as log:
            cinema_pan.apply_packet(game.gameplay, {"channel": 99, "cabinet": "j1"})
            cinema_pan.apply_packet(game.gameplay, None)
        self.assertEqual(log.call_args_list, [])


class PanOpenedFromTheTechnicianMenuTests(unittest.TestCase):
    """The pan menu has no key of its own: the Builder/Technician menu opens it.

    It used to ride F6, which is also the beacon toggle's key
    (``default_keyconfig.json`` binds ``toggle_beacons`` to ``f6``), so one of
    the two could never be reached -- reported as the pan menu opening over the
    radar. The Server lists the line beside the Cinema Speaker entry the same
    job places (``libs/builder/menu_manager.ts``) and sends
    ``cinema_pan_menu``; these pin the client end of that, and the key F6 was
    taken back for.
    """

    HERE = os.path.dirname(os.path.abspath(__file__))

    def _read(self, *parts):
        with open(os.path.join(self.HERE, "..", *parts), encoding="utf-8") as handle:
            return handle.read()

    @staticmethod
    def _handler():
        from libs.event_handeler import EventHandeler
        return object.__new__(EventHandeler)

    def test_the_event_the_server_sends_opens_this_clients_menu(self):
        opened = []
        handler = self._handler()
        handler.gameplay = SimpleNamespace(
            open_cinema_pan=lambda mod=None: opened.append(mod))
        handler.cinema_pan_menu({})
        self.assertEqual(opened, [None])

    def test_a_gameplay_without_the_opener_is_not_a_crash(self):
        handler = self._handler()
        handler.gameplay = SimpleNamespace()
        handler.cinema_pan_menu(None)

    def test_the_pan_has_no_key_of_its_own_any_more(self):
        self.assertNotIn('kc.get("open_cinema_pan"', self._read("libs", "gameplay.py"))

    def test_f6_belongs_to_the_beacon_toggle(self):
        source = self._read("libs", "gameplay.py")
        # Exactly one binding, and it is the one the default keyconfig file
        # already names -- which is the collision this replaced.
        self.assertEqual(source.count("pygame.K_F6"), 1)
        self.assertIn('kc.get("toggle_beacons", pygame.K_F6)', source)
        self.assertIn('"toggle_beacons": "f6"',
                      self._read("default_keyconfig.json"))


def land(gp, packet):
    """One relayed pan, as ``event_handeler.staff_pan`` handles it.

    The handler reads the table *either side* of the packet: the entry that was
    there before is what makes an identical (replayed) pan say nothing, so the
    two reads and the packet belong together and are exercised together here.
    """
    before = cinema_pan.own_entry(gp)
    cinema_pan.apply_packet(gp, packet)
    return cinema_pan.own_notice(gp, cinema_pan.own_entry(gp), before)


class OwnerSeesThePanTests(unittest.TestCase):
    """The player a pan moved: they *see* it, and staff still decide.

    A pan is a performance decision, relayed to the whole map and resolved on
    every listener's own machine -- so the one person who cannot look it up is
    the player whose voice it moved (their own name never arrives in a spawn
    packet; the login snapshot carries the channel instead). These tests pin the
    half they get: a sentence when it lands under *either* key, a line they can
    check by hand, and no say in it at all.
    """

    def _gameplay(self, own=9, me="Somchai", others=()):
        gp = SimpleNamespace(
            voice_channels={channel: SimpleNamespace(name=name)
                            for name, channel in others},
            player=SimpleNamespace(name=me))
        gp.own_voice_channel = own
        cinema_pan.table_for(gp)
        return gp

    # ---- hearing about it -------------------------------------------------
    def test_a_pan_under_your_name_reaches_you(self):
        gp = self._gameplay(own=9, me="Somchai")
        notice = land(gp, {"channel": 9, "name": "Somchai", "cabinet": "j2",
                           "direction": "auto"})
        self.assertIn("jukebox j2", notice)

    def test_the_owner_is_found_by_the_key_the_voice_routing_uses(self):
        # A panned voice is looked up by voice channel (``target_for_channel``),
        # and the login snapshot is where this client learns its own -- so the
        # person told is whoever the routing would move, even when the table
        # holds a name string that is not this client's current one (a session
        # where the player was renamed after the pan was placed).
        gp = self._gameplay(own=9, me="Somchai")
        cinema_pan.table_for(gp).set("Somchai (old name)", 9, "j1", "left")
        self.assertEqual(cinema_pan.own_entry(gp)[0], "Somchai (old name)")
        notice = cinema_pan.own_notice(gp, cinema_pan.own_entry(gp), None)
        self.assertIn("jukebox j1", notice)

    def test_somebody_elses_pan_is_not_your_news(self):
        gp = self._gameplay(own=9, others=(("Anong", 4),))
        self.assertIsNone(land(gp, {"channel": 4, "name": "Anong",
                                    "cabinet": "j1", "direction": "left"}))

    def test_the_same_pan_twice_is_said_once(self):
        # A map hands its pans over again to a joiner; being read the same
        # sentence on every arrival is how a real change stops being noticed.
        gp = self._gameplay(own=9)
        packet = {"channel": 9, "name": "Somchai", "cabinet": "j1",
                  "direction": "left"}
        self.assertIsNotNone(land(gp, packet))
        self.assertIsNone(land(gp, packet))

    def test_a_changed_destination_is_news_again(self):
        gp = self._gameplay(own=9)
        land(gp, {"channel": 9, "name": "Somchai", "cabinet": "j1",
                  "direction": "left"})
        notice = land(gp, {"channel": 9, "name": "Somchai", "cabinet": "j2",
                           "direction": "right"})
        self.assertIn("jukebox j2", notice)

    def test_clearing_says_where_your_voice_goes_back_to(self):
        gp = self._gameplay(own=9)
        land(gp, {"channel": 9, "name": "Somchai", "cabinet": "j1",
                  "direction": "left"})
        notice = land(gp, {"channel": 9, "name": "Somchai", "cabinet": "",
                           "direction": "auto"})
        self.assertIn("no longer moved", notice)
        self.assertIn("where you stand", notice)

    def test_a_cleared_pan_you_never_had_says_nothing(self):
        gp = self._gameplay(own=9)
        self.assertIsNone(land(gp, {"channel": 9, "name": "Somchai",
                                    "cabinet": ""}))

    # ---- what the sentence may and may not claim ---------------------------
    def test_the_two_questions_stay_apart(self):
        # "Where others hear me" is the pan; "what I hear" is my own switches
        # (the confusion that produced the switches-off-but-still-audible
        # report). One sentence says the first and names the second.
        gp = self._gameplay(own=9)
        notice = land(gp, {"channel": 9, "name": "Somchai", "cabinet": "j1",
                           "direction": "left"})
        self.assertIn("Other players now hear your voice", notice)
        self.assertIn("your own listening switches", notice)
        self.assertIn("towards the left of the room", notice)

    def test_the_words_are_the_pickers_own(self):
        # One table, or the menu and the confirmation start naming the same
        # destination two ways.
        self.assertIs(cinema_pan_menu.DIRECTION_LABELS,
                      cinema_pan.DIRECTION_LABELS)
        self.assertEqual([value for value, _label in cinema_pan.DIRECTION_LABELS],
                         list(cinema_pan.DIRECTIONS))
        self.assertEqual(cinema_pan.direction_phrase("left"),
                         "towards the left of the room")
        self.assertEqual(cinema_pan.direction_phrase("right"),
                         "towards the right of the room")
        self.assertEqual(cinema_pan.direction_phrase("front"),
                         "towards the screen")

    def test_the_rooms_own_mix_is_not_a_direction(self):
        # "auto" is what clearing restores, so it is the absence of a lean and
        # must not be read out as one.
        self.assertEqual(cinema_pan.direction_phrase("auto"), "")
        self.assertEqual(cinema_pan.direction_phrase(None), "")
        gp = self._gameplay(own=9)
        notice = land(gp, {"channel": 9, "name": "Somchai", "cabinet": "j1",
                           "direction": "auto"})
        self.assertNotIn("towards", notice)

    # ---- the line they can check by hand ----------------------------------
    def test_the_line_says_where_you_stand_when_nobody_moved_you(self):
        gp = self._gameplay(own=9)
        self.assertEqual(cinema_pan.own_label(gp),
                         "Your sound: where you stand (no staff pan)")

    def test_the_line_names_the_room_and_the_side_once_you_are_moved(self):
        gp = self._gameplay(own=9)
        cinema_pan.table_for(gp).set("Somchai", 9, "j3", "left")
        self.assertEqual(cinema_pan.own_label(gp),
                         "Your sound: heard from jukebox j3, towards the left "
                         "of the room")
        cinema_pan.table_for(gp).set("Somchai", 9, "j3", "auto")
        self.assertEqual(cinema_pan.own_label(gp),
                         "Your sound: heard from jukebox j3")

    # ---- it is sight, never a veto ---------------------------------------
    def test_looking_and_being_told_never_write_the_table(self):
        gp = self._gameplay(own=9)
        land(gp, {"channel": 9, "name": "Somchai", "cabinet": "j1",
                  "direction": "left"})
        table = cinema_pan.table_for(gp)
        version = table.version
        for _ in range(3):
            cinema_pan.own_label(gp)
            cinema_pan.own_entry(gp)
            cinema_pan.own_notice(gp, cinema_pan.own_entry(gp), None)
        self.assertEqual(table.version, version)
        self.assertEqual(cinema_pan.own_entry(gp)[2], "j1")

    def test_a_client_that_knows_nothing_about_itself_is_told_nothing(self):
        # Nobody to name, no channel to match: silence, because the only other
        # answer would be telling the wrong player about somebody else's pan.
        gp = SimpleNamespace(voice_channels={}, player=SimpleNamespace(name=""))
        gp.own_voice_channel = None
        cinema_pan.table_for(gp).set("Anong", 4, "j1", "left")
        self.assertIsNone(cinema_pan.own_entry(gp))
        self.assertIsNone(cinema_pan.own_notice(gp, cinema_pan.table_for(gp).entries()[0], None))
        self.assertEqual(cinema_pan.own_label(gp),
                         "Your sound: where you stand (no staff pan)")

    def test_the_handler_tells_the_owner_through_these_functions(self):
        # The wiring, read from the source: the handler must not decide for
        # itself who the owner is (the packet's name was the old, narrow rule)
        # or what to say (a second copy of the words would drift).
        with open(os.path.join(os.path.dirname(__file__), "..", "libs",
                               "event_handeler.py"), encoding="utf-8") as handle:
            source = handle.read()
        self.assertIn("cinema_pan.own_entry", source)
        self.assertIn("cinema_pan.own_notice", source)
        self.assertNotIn("Your voice has been moved to jukebox", source)


class ListeningSummaryTests(unittest.TestCase):
    """The three listening switches, each said as its own menu line says it."""

    KEYS = (cinema_plugin.OPTION_ENABLED, cinema_speech.OPTION_ENABLED,
            cinema_live.OPTION_ENABLED)

    def setUp(self):
        from libs import options
        self._saved = {key: options.prefs.get(key) for key in self.KEYS}

    def tearDown(self):
        from libs import options
        for key, value in self._saved.items():
            if value is None:
                options.prefs.pop(key, None)
            else:
                options.prefs[key] = value

    def test_all_on_says_every_shape_comes_out_of_the_room(self):
        from libs import options
        for key in self.KEYS:
            options.prefs[key] = True
        self.assertEqual(
            cinema_plugin.listening_summary(),
            "jukebox songs from the room, the band from the room, "
            "voices from the room")

    def test_each_switch_off_says_where_that_shape_goes_instead(self):
        from libs import options
        for key in self.KEYS:
            options.prefs[key] = False
        self.assertEqual(
            cinema_plugin.listening_summary(),
            "jukebox songs at your ears, the band where they stand, "
            "voices on the map's PA")

    def test_it_reads_the_same_keys_the_menu_lines_write(self):
        from libs import options
        self.assertEqual(cinema_plugin.OPTION_ENABLED, "cinema_speakers")
        self.assertEqual(cinema_speech.OPTION_ENABLED, "cinema_speech")
        self.assertEqual(cinema_live.OPTION_ENABLED, "cinema_live_instruments")
        for key in self.KEYS:
            options.prefs[key] = True
        options.prefs[cinema_live.OPTION_ENABLED] = False
        self.assertIn("the band where they stand",
                      cinema_plugin.listening_summary())


if __name__ == "__main__":
    unittest.main()
