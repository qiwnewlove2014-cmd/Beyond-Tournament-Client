"""A speaker's own voicing -- the map's ``tone`` -- and the one filter it makes.

The reported wish: make a room's rear pair (or a speaker inside a booth) sound
duller than the screen wall without moving anything, and let the builder set it
on the speaker itself from the element menu. What makes that more than a new
line in a menu is that an OpenAL source holds **one** direct filter: the map's
voicing and the wall really standing between that speaker and these ears cannot
be two filters, or the second one silently replaces the first and the room ends
up dull for the song and bright for the band.

So the promise these tests pin is two-sided:

* a map that says nothing about voicing plays exactly what it played before
  (the room's own shared wall filter object, and no filter at all when the path
  is clear), and
* a map that does set one gets it *composed* with the wall, at one home in
  ``libs/audio/cinema/listener.py`` that the song, a live note and a voice all
  read.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from libs.audio.cinema import CinemaLayout, CinemaSpeakerSpec, coerce_spec
from libs.audio.cinema.listener import (TONE_HF_FLOOR, occlusion_filter,
                                        speaker_filter, tone_gainhf,
                                        tone_openness, wall_params)
from libs.world_map import Map

ANCHOR = (10.0, 20.0, 0.0)


class FakeAudio:
    """Just enough audio manager: making a filter is recorded, not real."""

    def __init__(self, efx=True):
        self.made = []
        self.efx = object() if efx else None

    def gen_filter(self, kind, *params):
        self.made.append((kind, params))
        return ("filter", kind, params)


class NoFilterAudio(FakeAudio):
    """A machine whose driver could not make a filter at all."""

    def gen_filter(self, kind, *params):
        return None


class VoicingNumbersTests(unittest.TestCase):
    """The arithmetic, and the promise that unity is the room that shipped."""

    def test_a_clear_path_and_an_untouched_voicing_is_no_filter(self):
        audio = FakeAudio()
        self.assertIsNone(speaker_filter(audio, 0, 1.0))
        self.assertIsNone(speaker_filter(audio, None, 1.0))
        self.assertIsNone(speaker_filter(audio, 0, None))
        self.assertEqual(audio.made, [])

    def test_an_untouched_voicing_gives_the_very_wall_filter_it_always_did(self):
        """Not an equal filter -- the same object out of the same cache, so
        nothing about a room with no tone can move by a byte."""
        audio = FakeAudio()
        cache = {}
        heavy = speaker_filter(audio, 2, 1.0, cache)
        self.assertIs(heavy, occlusion_filter(audio, 2, cache))
        self.assertIs(speaker_filter(audio, 1, 1.0, cache),
                      occlusion_filter(audio, 1, cache))
        self.assertEqual(len(audio.made), 2)   # one object per wall, no more

    def test_a_dulled_speaker_takes_the_wall_and_is_duller_than_it(self):
        audio = FakeAudio()
        alone = speaker_filter(audio, 0, 0.5)
        self.assertEqual(alone[2][0][0], "GAINHF")
        behind = speaker_filter(audio, 2, 0.5)
        self.assertLess(behind[2][0][1], occlusion_filter(FakeAudio(), 2)[2][0][1])
        self.assertEqual(behind[2][1][0], "GAIN")
        # The wall's own loudness dip is kept: a dulled speaker is duller, not
        # quieter -- ``level`` is the map's tool for that.
        self.assertAlmostEqual(behind[2][1][1], wall_params(2)[1], places=6)
        self.assertAlmostEqual(alone[2][1][1], 1.0, places=6)

    def test_the_darkest_a_speaker_can_be_is_still_a_room_not_a_lake(self):
        audio = FakeAudio()
        darkest = speaker_filter(audio, 0, 0.0)
        self.assertAlmostEqual(darkest[2][0][1], TONE_HF_FLOOR, places=6)
        # The water at full depth is 0.02: a rear speaker that sounded like
        # that would read as a mistake, not as the back of a room.
        self.assertGreater(TONE_HF_FLOOR, 0.2)

    def test_openness_is_monotonic_and_reads_a_fraction(self):
        self.assertAlmostEqual(tone_gainhf(0.0), TONE_HF_FLOOR, places=6)
        self.assertAlmostEqual(tone_gainhf(1.0), 1.0, places=6)
        values = [tone_gainhf(v / 10.0) for v in range(11)]
        self.assertEqual(values, sorted(values))
        self.assertEqual(tone_openness(None), None)
        self.assertEqual(tone_openness("wide"), None)
        self.assertEqual(tone_openness(float("inf")), None)
        self.assertEqual(tone_openness(-3), 0.0)
        self.assertEqual(tone_openness(9), 1.0)

    def test_one_filter_object_per_voicing_the_map_uses(self):
        audio = FakeAudio()
        cache = {}
        first = speaker_filter(audio, 2, 0.4, cache)
        self.assertIs(speaker_filter(audio, 2, 0.4, cache), first)
        self.assertIsNot(speaker_filter(audio, 2, 0.5, cache), first)
        self.assertIsNot(speaker_filter(audio, 1, 0.4, cache), first)
        self.assertEqual(len(audio.made), 3)

    def test_a_machine_without_filters_says_so_instead_of_inventing_one(self):
        self.assertIsNone(speaker_filter(NoFilterAudio(), 2, 0.4))
        self.assertIsNone(speaker_filter(NoFilterAudio(), 2, 1.0))

    def test_the_walls_own_numbers_have_one_home(self):
        """The bank, a note and a voice read the wall from here, not from
        three copies of two tuples."""
        audio = FakeAudio()
        for tier in (1, 2):
            hf, gain = wall_params(tier)
            self.assertEqual(occlusion_filter(audio, tier)[2],
                             (("GAINHF", hf), ("GAIN", gain)))
        self.assertIsNone(wall_params(0))
        self.assertIsNone(wall_params("two"))


class MapAttributeTests(unittest.TestCase):
    """The trip the attribute makes from a map element to a room's spec."""

    def map_with(self, **kwargs):
        map_obj = Map(type("G", (), {"audio_mngr": None})())
        map_obj.spawn_cinemaSpeaker(minx=6.0, maxx=7.0, miny=26.0, maxy=27.0,
                                    minz=0, maxz=1, id="rear_l",
                                    channel="rear_l", **kwargs)
        return map_obj

    def test_a_speaker_nobody_dulled_reads_as_untouched(self):
        spec = coerce_spec(self.map_with().get_cinema_speakers()[0])
        self.assertAlmostEqual(spec.tone, 1.0, places=6)

    def test_the_map_writes_a_percentage_and_the_spec_holds_a_fraction(self):
        spec = coerce_spec(self.map_with(tone=40).get_cinema_speakers()[0])
        self.assertAlmostEqual(spec.tone, 0.4, places=6)
        # A percentage, always: the menu can write 1 (1%, nearly the darkest a
        # speaker can be) and the client must not read that as unity, which is
        # a change the room would swallow without a sound.
        self.assertAlmostEqual(coerce_spec({"x": 1, "y": 1, "z": 0,
                                            "tone": 1}).tone, 0.01, places=6)

    def test_a_value_the_map_cannot_mean_leaves_the_room_as_it_shipped(self):
        map_obj = Map(type("G", (), {"audio_mngr": None})())
        map_obj.spawn_cinemaSpeaker(minx=6.0, maxx=7.0, miny=26.0, maxy=27.0,
                                    minz=0, maxz=1, id="a", tone="loud")
        spec = coerce_spec(map_obj.get_cinema_speakers()[0])
        self.assertAlmostEqual(spec.tone, 1.0, places=6)

    def test_a_map_that_placed_no_speaker_has_no_tone_to_read(self):
        layout = CinemaLayout(ANCHOR)
        for slot in layout.slots:
            self.assertEqual(layout.tone(slot), 1.0)

    def test_the_layout_reports_the_maps_own_voicing(self):
        layout = CinemaLayout(ANCHOR, specs=[CinemaSpeakerSpec(
            "rear_l", (5.0, 12.0, 0.0), tone=0.4)])
        self.assertAlmostEqual(layout.tone("rear_l"), 0.4, places=6)
        self.assertAlmostEqual(layout.tone("front_l"), 1.0, places=6)

    def test_a_spec_clamps_a_voicing_it_cannot_mean(self):
        self.assertAlmostEqual(CinemaSpeakerSpec("a", (0, 0, 0), tone=-4).tone,
                               0.0, places=6)
        self.assertAlmostEqual(CinemaSpeakerSpec("a", (0, 0, 0), tone=90).tone,
                               1.0, places=6)


class TheRoomKeepsItsShapeTests(unittest.TestCase):
    """A voicing is a different room, not the same one re-used."""

    def renderer(self, tone):
        from libs.audio.cinema import CinemaRenderer
        specs = [CinemaSpeakerSpec("front_l", (6.5, 26.5, 0.0)),
                 CinemaSpeakerSpec("front_r", (13.5, 26.5, 0.0), tone=tone)]
        return CinemaRenderer(ANCHOR, "front_only", specs=specs)

    def test_two_rooms_that_differ_only_by_voicing_are_two_rooms(self):
        plain = self.renderer(1.0)
        dulled = self.renderer(0.5)
        self.assertNotEqual(plain.signature, dulled.signature)

    def test_the_same_voicing_is_the_same_room(self):
        self.assertEqual(self.renderer(0.5).signature, self.renderer(0.5).signature)


class ParityTests(unittest.TestCase):
    """The shipped jukebox: nothing here may move what a plain map plays."""

    def test_a_room_with_no_tone_is_still_byte_for_byte_the_same_render(self):
        from libs.audio.cinema import CinemaRenderer
        left = b"\x01\x02\x03\x04"
        right = b"\x05\x06\x07\x08"
        specs = [CinemaSpeakerSpec("front_l", (6.5, 26.5, 0.0)),
                 CinemaSpeakerSpec("front_r", (13.5, 26.5, 0.0))]
        renderer = CinemaRenderer(ANCHOR, "front_only", specs=specs)
        self.assertEqual(renderer.render(left, right),
                         [("front_l", left), ("front_r", right)])

    def test_a_neutral_voicing_is_not_sent_to_an_instrument_at_all(self):
        """Seven fields, exactly as before, unless the map set a tone: a caller
        that knows nothing about voicing keeps working, byte for byte."""
        from libs.audio.cinema import live as cinema_live
        seen = []

        def play_one(x, y, z, gain, tier, delay_ms, channel=None):
            seen.append((x, y, z, gain, tier, delay_ms, channel))

        game = _game_with_room(tones={"front_l": 100, "front_r": 100})
        cinema_live.route_to_room(game, (10.0, 25.0, 0.0), play_one)
        self.assertEqual(len(seen), 2)

    def test_a_dulled_speaker_hands_the_room_its_voicing(self):
        from libs.audio.cinema import live as cinema_live
        seen = []

        def play_one(x, y, z, gain, tier, delay_ms, channel=None, tone=None):
            seen.append((x, y, z, tone))

        game = _game_with_room(tones={"front_l": 40, "front_r": 100})
        cinema_live.route_to_room(game, (10.0, 25.0, 0.0), play_one)
        by_x = {round(x, 1): tone for x, _y, _z, tone in seen}
        self.assertAlmostEqual(by_x[6.5], 0.4, places=6)
        # The untouched speaker was handed no eighth field at all: the call an
        # instrument has always received.
        self.assertIsNone(by_x[13.5])


def _game_with_room(tones=None):
    """A tiny game: two front speakers and a cabinet between them."""
    from types import SimpleNamespace
    from libs import options
    options.prefs["cinema_live_instruments"] = True
    options.prefs["cinema_speakers"] = True
    tones = dict(tones or {})
    game = SimpleNamespace()
    game.audio_mngr = FakeAudio()
    game.audio_mngr.position = (10.0, 25.0, 0.0)
    map_obj = Map(game)
    for name, x in (("front_l", 6.5), ("front_r", 13.5)):
        map_obj.spawn_cinemaSpeaker(minx=x - 0.5, maxx=x + 0.5, miny=26.0,
                                    maxy=27.0, minz=0, maxz=1, id=name,
                                    channel=name, tone=tones.get(name, 100))
    map_obj.jukebox_list = [SimpleNamespace(id="j1", center=ANCHOR)]
    game.gameplay = SimpleNamespace(
        map=map_obj, game=game, megaphone=None,
        jukebox_player=SimpleNamespace(cinema_mode=lambda cabinet_id: "auto",
                                       occlusion_tier=lambda *args: 0))
    return game


if __name__ == "__main__":
    unittest.main()
