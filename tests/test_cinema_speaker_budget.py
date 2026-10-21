"""The listener's speaker budget, and the roof layer it can never cut.

A room costs one OpenAL source and a 16-buffer pool per speaker, plus one mix
per speaker per frame, so "how many speakers may this machine feed at once" is
a frame budget rather than a taste. The rule these tests pin is that a machine
over its budget narrows a room from the back -- the ceiling centre first, then
the roof pair, then the wide pair, the screen wall never -- that a cut pair goes
whole, and that nothing here ever drops a *frame* to save the CPU: a dropped
frame is the song jumping forward, while one speaker fewer is a room that is a
little less wide.

Also pinned: a voice or a live note plays the speakers the cabinet's *mode*
feeds (``RoomPlan.fed``), never every speaker standing around it, which is what
makes ``front_only`` a room whose walls stay silent for a talker too.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from libs.audio.cinema import plugin as cinema_plugin
from libs.audio.cinema import speech as cinema_speech
from libs.audio.cinema.layout import IDEAL_BEARING, CinemaSpeakerSpec
from libs.audio.cinema.placement import RoomPlan, resolve_room
from libs.audio.cinema.plugin import LISTENER_SPEAKER_BUDGET, CinemaSpeakerHost
from libs.audio.cinema.router import CinemaRenderer, renderer_for

ANCHOR = (0.0, 0.0, 0.0)
FLOOR = ("front_l", "front_c", "front_r", "side_l", "side_r", "rear_l", "rear_r")
WIDE = ("wide_l", "wide_r")
ROOF = ("height_l", "height_r")
TOP = "top_c"


def _placed(slot, height=0.0):
    """One speaker at its slot's own bearing, ``height`` metres overhead."""
    from math import cos, radians, sin
    angle = radians(IDEAL_BEARING[slot])
    return CinemaSpeakerSpec(slot, (sin(angle) * 8.0, cos(angle) * 8.0, height))


def twelve_speakers():
    """A full room: seven on the walls, a wide pair, and the overhead layer."""
    return ([_placed(slot) for slot in FLOOR]
            + [_placed(slot) for slot in WIDE]
            + [_placed(slot, height=5.0) for slot in ROOF]
            + [_placed(TOP, height=5.0)])


class TruncationTests(unittest.TestCase):
    def test_a_narrowed_room_gives_up_the_roof_layer_first(self):
        room = CinemaRenderer(ANCHOR, "theatre", specs=twelve_speakers(),
                              max_speakers=7)
        self.assertEqual(room.slots, FLOOR)
        self.assertNotIn("height_l", room.slots)
        self.assertNotIn("height_r", room.slots)

    def test_a_narrowed_room_never_gives_up_the_screen_wall(self):
        room = CinemaRenderer(ANCHOR, "theatre", specs=twelve_speakers(),
                              max_speakers=3)
        self.assertEqual(room.slots, ("front_l", "front_c", "front_r"))

    def test_the_full_room_feeds_every_speaker_placed(self):
        room = CinemaRenderer(ANCHOR, "theatre", specs=twelve_speakers())
        self.assertEqual(room.slots, FLOOR + WIDE + ROOF + (TOP,))

    def test_a_two_speaker_budget_is_the_stereo_pair_not_the_left_and_the_centre(self):
        # A listener already holding two full rooms has two speakers left for
        # the third. Room order would give front_l and front_c -- the left of
        # the image, twice -- so the floor a budget promises is the stereo
        # pair, the same pair the plain jukebox plays.
        room = CinemaRenderer(ANCHOR, "theatre", specs=twelve_speakers(),
                              max_speakers=2)
        self.assertEqual(room.slots, ("front_l", "front_r"))

    def test_a_cut_inside_a_pair_drops_the_half_that_survived(self):
        # Ten speakers keeps seven walls, the wide pair and one roof speaker:
        # half a roof is a steering bias, so that half goes and the plan is
        # nine -- one under the cap, which is the price of a whole pair.
        room = CinemaRenderer(ANCHOR, "theatre", specs=twelve_speakers(),
                              max_speakers=10)
        self.assertEqual(room.slots, FLOOR + WIDE)

    def test_a_pair_the_map_never_had_is_left_alone(self):
        # A custom room's lone wall speaker is that mode's whole point.
        lone = [CinemaSpeakerSpec("side_l", (-8.0, 0.0, 0.0))]
        room = renderer_for(ANCHOR, "custom", specs=lone, fill=False)
        self.assertEqual(room.slots, ("side_l",))


class _FakeBank:
    """The two things the budget reads off a live room, and nothing else."""

    def __init__(self, speakers, spent=False):
        self.renderer = _FakeRenderer(speakers)
        self.sources = tuple(range(speakers))
        self.spent = spent


class _FakeRenderer:
    def __init__(self, speakers):
        self.slots = tuple(f"slot{i}" for i in range(speakers))


class BudgetTests(unittest.TestCase):
    def test_a_listener_with_no_rooms_has_the_whole_budget(self):
        host = CinemaSpeakerHost(enabled=True)
        self.assertEqual(host.budget_for("j2"), LISTENER_SPEAKER_BUDGET)

    def test_the_rooms_already_playing_are_counted(self):
        host = CinemaSpeakerHost(enabled=True)
        host._banks = {"j1": _FakeBank(9)}
        self.assertEqual(host.budget_for("j2"),
                         LISTENER_SPEAKER_BUDGET - 9)

    def test_a_room_does_not_count_its_own_speakers(self):
        host = CinemaSpeakerHost(enabled=True)
        host._banks = {"j1": _FakeBank(9)}
        self.assertEqual(host.budget_for("j1"), LISTENER_SPEAKER_BUDGET)

    def test_a_room_that_is_leaving_counts_nothing(self):
        host = CinemaSpeakerHost(enabled=True)
        host._banks = {"j1": _FakeBank(9, spent=True)}
        self.assertEqual(host.budget_for("j2"), LISTENER_SPEAKER_BUDGET)

    def test_the_budget_never_squeezes_a_room_below_the_front_pair(self):
        host = CinemaSpeakerHost(enabled=True)
        host._banks = {f"j{index}": _FakeBank(12) for index in range(3)}
        self.assertEqual(host.budget_for("new"), 2)

    def test_a_new_room_is_built_with_only_what_is_left(self):
        host = CinemaSpeakerHost(enabled=True)
        host._banks = {"j1": _FakeBank(LISTENER_SPEAKER_BUDGET - 4)}
        renderer = host.acquire("j2", ANCHOR, profile="theatre",
                                specs=twelve_speakers(), fill=False)
        self.assertIsNotNone(renderer)
        self.assertLessEqual(len(renderer.slots), 4)


class _ModePlayer:
    def __init__(self, mode):
        self.mode = mode

    def cinema_mode(self, cabinet_id):
        return self.mode


class _Gameplay:
    pass


class _ModeGame:
    """Just enough of a game to answer "which mode is this cabinet set to"."""

    def __init__(self, mode):
        self.gameplay = _Gameplay()
        self.gameplay.jukebox_player = _ModePlayer(mode)
        self.map = None


def _mode_game(mode):
    return _ModeGame(mode), mode


class FedSlotsTests(unittest.TestCase):
    def _plan(self, profile, fed):
        placement = resolve_room(twelve_speakers(), ANCHOR, profile=profile)
        self.assertIsNotNone(placement)
        plan = RoomPlan(profile, placement.specs, placement)
        plan.fed = tuple(fed)
        return plan

    def test_a_voice_plays_the_speakers_the_mode_feeds(self):
        plan = self._plan("theatre", FLOOR + WIDE + ROOF + (TOP,))
        self.assertEqual(set(cinema_speech._specs(plan)),
                         set(FLOOR + WIDE + ROOF + (TOP,)))

    def test_front_only_keeps_the_walls_silent_for_a_voice(self):
        plan = self._plan("front_only", ("front_l", "front_r"))
        self.assertEqual(set(cinema_speech._specs(plan)),
                         {"front_l", "front_r"})

    def test_a_plan_with_no_renderer_still_names_every_placed_speaker(self):
        plan = self._plan("theatre", FLOOR + WIDE + ROOF + (TOP,))
        plan.fed = None
        self.assertEqual(set(cinema_speech._specs(plan)),
                         set(FLOOR + WIDE + ROOF + (TOP,)))

    def test_a_preview_plays_the_cabinet_mode_not_the_auto_shape(self):
        """A voice, a note and a pan ask this; all three must follow the mode."""
        game, _ = _mode_game("front_only")
        plan = cinema_plugin.preview_room(game, ANCHOR, room_id="j1")
        self.assertIsNotNone(plan)
        self.assertEqual(plan.profile, "front_only")
        self.assertEqual(tuple(plan.fed), ("front_l", "front_r"))

    def test_a_preview_of_a_theatre_cabinet_never_invents_a_layer(self):
        game, _ = _mode_game("theatre")
        plan = cinema_plugin.preview_room(game, ANCHOR, room_id="j1")
        self.assertIsNotNone(plan)
        for slot in ("height_l", "height_r", "top_c", "wide_l", "wide_r"):
            self.assertNotIn(slot, plan.fed)
        self.assertEqual(len(plan.fed), 7)

    def test_a_cabinet_set_to_off_previews_no_room_at_all(self):
        game, _ = _mode_game("off")
        self.assertIsNone(cinema_plugin.preview_room(game, ANCHOR, room_id="j1"))

    def test_a_custom_cabinet_with_no_speakers_previews_silence(self):
        game, _ = _mode_game("custom")
        plan = cinema_plugin.preview_room(game, ANCHOR, room_id="j1")
        self.assertIsNotNone(plan)
        self.assertTrue(plan.silent)
        self.assertFalse(plan.fed)

    def test_the_budget_is_a_module_constant_the_host_reads(self):
        # A full 12-speaker room still fits twice in one listener's budget, which
        # is the width this budget was sized for.
        self.assertGreaterEqual(LISTENER_SPEAKER_BUDGET, 12 * 2)
        self.assertIs(cinema_plugin.LISTENER_SPEAKER_BUDGET,
                      LISTENER_SPEAKER_BUDGET)


if __name__ == "__main__":
    unittest.main()
