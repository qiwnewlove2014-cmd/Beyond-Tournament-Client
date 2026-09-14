"""Placement and facing tests for the Cinema Speaker System.

These cover the part of the feature that has nothing to do with audio: what
happens when the map data is wrong. A room is authored by hand, in the map
editor, by someone standing in it, so the interesting cases are the mistakes
-- a swapped left and right, a room rotated ninety degrees, two speakers
fighting over one label, a left wall with no right wall, every speaker left
on ``auto`` -- and the guarantee that a room which cannot be resolved falls
back to the plain jukebox instead of going silent.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from libs.audio.cinema.layout import (AUTO_SLOT, CinemaLayout, CinemaSpeakerSpec,
                                      bearing_from, coerce_spec, slot_for_bearing)
from libs.audio.cinema.listener import (ListenerPose, aim_vector, cone_gain,
                                        facing_report)
from libs.audio.cinema.placement import (AUTO_PROFILE, RoomPlan, auto_profile,
                                         exclusive_speakers, nearest_anchor,
                                         resolve_room, room_profile)
from libs.audio.cinema.profiles import get_profile
from libs.audio.cinema.router import PARITY_PLAN, CinemaRenderer

ANCHOR = (0.0, 0.0, 0.0)


def _room_local(room, position):
    """A world position expressed in the room's own frame (screen wall = +Y)."""
    from math import cos, radians, sin
    yaw = radians(room.yaw_deg)
    return (position[0] * cos(yaw) - position[1] * sin(yaw),
            position[1] * cos(yaw) + position[0] * sin(yaw))


def speaker(channel="auto", bearing=0.0, distance=8.0, **kwargs):
    """A speaker placed at ``bearing`` degrees from the anchor."""
    from math import cos, radians, sin
    angle = radians(bearing)
    position = (sin(angle) * distance, cos(angle) * distance, kwargs.pop("z", 0.0))
    return CinemaSpeakerSpec(channel, position, **kwargs)


class RoomResolutionTests(unittest.TestCase):
    def test_a_clean_room_takes_its_labels(self):
        room = resolve_room(
            [speaker("front_l", -30), speaker("front_c", 0), speaker("front_r", 30),
             speaker("side_l", -90), speaker("side_r", 90),
             speaker("rear_l", -150), speaker("rear_r", 150)],
            ANCHOR,
        )
        self.assertIsNotNone(room)
        self.assertEqual(room.profile_name, "theatre")
        self.assertEqual(set(room.slots), {
            "front_l", "front_c", "front_r", "side_l", "side_r", "rear_l", "rear_r"})
        self.assertEqual(room.warnings, ())

    def test_front_pair_only_resolves_to_the_parity_profile(self):
        room = resolve_room([speaker("front_l", -30), speaker("front_r", 30)], ANCHOR)
        self.assertEqual(room.profile_name, "front_only")
        renderer = CinemaRenderer(ANCHOR, "front_only", specs=room.specs)
        # The bit-exact property: a two-speaker room is today's jukebox pair.
        self.assertEqual(renderer.plan(), PARITY_PLAN)
        frames = renderer.render(b"\x01\x00\x02\x00", b"\x03\x00\x04\x00")
        self.assertEqual(frames[0][1], b"\x01\x00\x02\x00")
        self.assertEqual(frames[1][1], b"\x03\x00\x04\x00")

    def test_swapped_left_and_right_is_corrected_by_geometry(self):
        """The classic mistake: labels swapped, audio image would be mirrored."""
        room = resolve_room(
            [speaker("front_r", -30), speaker("front_l", 30), speaker("front_c", 0)],
            ANCHOR,
        )
        left = room.speakers["front_l"]
        right = room.speakers["front_r"]
        # The speaker standing on the audience's left plays the left channel,
        # whatever the builder typed into its channel field.
        self.assertLess(left.position[0], 0.0)
        self.assertGreater(right.position[0], 0.0)
        self.assertEqual(left.source, "geometry")
        self.assertTrue(any("trusting the position" in warning for warning in room.warnings))

    def test_a_rotated_room_snaps_back_to_the_labels(self):
        """Speakers authored for a screen wall that does not face the engine +Y."""
        rotated = []
        for channel, bearing in (("front_l", -30), ("front_c", 0), ("front_r", 30),
                                 ("rear_l", -150), ("rear_r", 150)):
            rotated.append(speaker(channel, bearing + 90.0))
        room = resolve_room(rotated, ANCHOR)
        self.assertAlmostEqual(room.yaw_deg, 90.0, places=4)
        self.assertEqual(set(room.slots), {
            "front_l", "front_c", "front_r", "rear_l", "rear_r"})
        self.assertEqual(room.profile_name, "theatre")
        self.assertEqual(room.warnings, ())
        # In the room's own frame (not the engine's) the image is intact: the
        # left speaker is still on the audience's left.
        self.assertLess(_room_local(room, room.speakers["front_l"].position)[0], 0.0)
        self.assertGreater(_room_local(room, room.speakers["front_r"].position)[0], 0.0)

    def test_two_speakers_may_not_claim_the_same_slot(self):
        room = resolve_room(
            [speaker("front_l", -30), speaker("front_l", 30), speaker("front_r", 60)],
            ANCHOR,
        )
        self.assertEqual(len(room.slots), 3)
        self.assertEqual(set(room.slots), {"front_l", "front_c", "front_r"})
        self.assertTrue(any("already labelled" in warning for warning in room.warnings))

    def test_auto_speakers_land_on_the_slots_they_stand_in(self):
        room = resolve_room([speaker(AUTO_SLOT, bearing) for bearing in
                             (-90.0, -30.0, 0.0, 30.0, 90.0)], ANCHOR)
        self.assertEqual(set(room.slots), {
            "side_l", "front_l", "front_c", "front_r", "side_r"})
        self.assertEqual(room.profile_name, "surround")

    def test_a_lone_wall_speaker_is_dropped_rather_than_steering_the_room(self):
        room = resolve_room(
            [speaker("front_l", -30), speaker("front_r", 30), speaker("side_l", -90)],
            ANCHOR,
        )
        self.assertNotIn("side_l", room.slots)
        self.assertEqual(room.profile_name, "front_only")
        self.assertTrue(any("has no partner" in warning for warning in room.warnings))

    def test_one_front_speaker_is_not_a_room(self):
        room = resolve_room([speaker("front_l", -30), speaker("front_c", 0)], ANCHOR)
        self.assertIsNone(room)

    def test_a_heap_of_speakers_is_not_a_room(self):
        """Seven speakers stacked on the cabinet: nothing to resolve, no room."""
        room = resolve_room([speaker(AUTO_SLOT, bearing, distance=1.0 + index * 0.05)
                             for index, bearing in enumerate((0, 1, 2, 3, 4, 5, 6))],
                            ANCHOR)
        self.assertIsNone(room)

    def test_speakers_far_from_the_cabinet_are_another_room(self):
        room = resolve_room(
            [speaker("front_l", -30), speaker("front_r", 30),
             speaker("rear_l", -150, distance=200.0),
             speaker("rear_r", 150, distance=200.0)],
            ANCHOR,
        )
        self.assertEqual(set(room.slots), {"front_l", "front_r"})

    def test_a_back_row_speaker_belongs_to_the_room(self):
        """A theatre's rear pair stands further out than a pair's own fade.

        The resolver's radius is the room's own scale, not the plain jukebox
        pair's 40 m falloff: at 55 m this is still one room, and under the
        pair's number the rear pair used to be dropped as another room's.
        """
        room = resolve_room(
            [speaker("front_l", -30), speaker("front_r", 30),
             speaker("rear_l", -150, distance=55.0),
             speaker("rear_r", 150, distance=55.0)],
            ANCHOR,
        )
        self.assertEqual(set(room.slots),
                         {"front_l", "front_r", "rear_l", "rear_r"})

    def test_room_id_selects_the_speakers_of_one_cabinet(self):
        here = [speaker("front_l", -30, room="box_a"), speaker("front_r", 30, room="box_a")]
        there = [speaker("front_l", -30, room="box_b"), speaker("front_r", 30, room="box_b")]
        room = resolve_room(here + there, ANCHOR, room="box_a")
        self.assertEqual(len(room.specs), 2)
        self.assertEqual(set(room.slots), {"front_l", "front_r"})
        self.assertTrue(all(spec.room == "box_a" for spec in room.specs))

    def test_explicit_profile_survives_a_room_that_can_carry_it(self):
        room = resolve_room([speaker("front_l", -30), speaker("front_r", 30)],
                            ANCHOR, profile="theatre")
        self.assertEqual(room_profile(room), "theatre")
        self.assertEqual(room_profile(room, "surround"), "surround")
        # An unknown request falls back to what the room can actually do.
        self.assertEqual(room_profile(room, AUTO_PROFILE), "theatre")

    def test_auto_profile_reads_the_slots_it_is_given(self):
        self.assertEqual(auto_profile({"front_l", "front_r"}), "front_only")
        self.assertEqual(auto_profile({"front_l", "front_r", "front_c"}), "front_stage")
        self.assertEqual(auto_profile({"front_l", "front_r", "side_l", "side_r"}), "surround")
        self.assertEqual(auto_profile({"front_l", "front_r", "rear_l", "rear_r"}), "theatre")
        self.assertIsNone(auto_profile({"front_l"}))

    def test_every_speaker_of_a_resolved_room_is_usable_by_the_renderer(self):
        room = resolve_room(
            [speaker("front_l", -30), speaker("front_c", 0), speaker("front_r", 30),
             speaker("side_l", -90), speaker("side_r", 90)], ANCHOR)
        renderer = CinemaRenderer(ANCHOR, room.profile_name, specs=room.specs)
        self.assertEqual(set(renderer.slots), set(room.slots))
        frames = renderer.render(b"\x00\x00" * 4, b"\x10\x00" * 4)
        self.assertEqual({slot for slot, _ in frames}, set(room.slots))

    def test_level_and_delay_are_carried_through_from_the_map(self):
        room = resolve_room(
            [speaker("front_l", -30, level=0.5), speaker("front_r", 30, delay_ms=12.0)],
            ANCHOR)
        self.assertAlmostEqual(room.specs[0].level, 0.5)
        self.assertAlmostEqual(room.specs[1].delay_ms, 12.0)
        renderer = CinemaRenderer(ANCHOR, room.profile_name, specs=room.specs)
        self.assertAlmostEqual(renderer.extra_latency_s, 0.012)
        # A trimmed speaker is no longer a bit-exact front pair.
        self.assertNotEqual(renderer.plan(), PARITY_PLAN)

    def test_a_room_beyond_the_speaker_budget_is_trimmed_from_the_back(self):
        speakers = [speaker("front_l", -30), speaker("front_c", 0), speaker("front_r", 30)]
        speakers += [speaker(AUTO_SLOT, bearing, distance=20.0, name=f"extra{i}")
                     for i, bearing in enumerate((-60.0, -45.0, 45.0, 60.0))]
        room = resolve_room(speakers, ANCHOR, max_speakers=3)
        self.assertEqual(len(room.slots), 3)
        self.assertIn("front_l", room.slots)
        self.assertIn("front_r", room.slots)


class SpeakerOwnershipTests(unittest.TestCase):
    """Two rooms on one map must never play two songs through one speaker."""

    NEAR = (0.0, 0.0, 0.0)
    FAR = (60.0, 0.0, 0.0)

    def test_a_speaker_goes_to_the_cabinet_it_stands_closest_to(self):
        speakers = [{"id": "a", "x": 4.0, "y": 6.0, "z": 0.0, "channel": "front_l"},
                    {"id": "b", "x": 64.0, "y": 6.0, "z": 0.0, "channel": "front_l"}]
        mine = exclusive_speakers(speakers, self.NEAR, rivals=[self.FAR])
        self.assertEqual([spec["id"] for spec in mine], ["a"])
        theirs = exclusive_speakers(speakers, self.FAR, rivals=[self.NEAR])
        self.assertEqual([spec["id"] for spec in theirs], ["b"])

    def test_a_speaker_that_names_its_room_is_left_alone(self):
        speakers = [{"id": "a", "x": 4.0, "y": 6.0, "z": 0.0, "room": "box_b"}]
        # It stands next to box_a but was assigned to box_b on purpose.
        self.assertEqual(exclusive_speakers(speakers, self.NEAR, rivals=[]), speakers)

    def test_speakers_beyond_the_room_radius_are_dropped(self):
        speakers = [{"id": "far", "x": 200.0, "y": 0.0, "z": 0.0}]
        self.assertEqual(exclusive_speakers(speakers, self.NEAR, rivals=[]), [])

    def test_a_tie_goes_to_the_cabinet_asking(self):
        speakers = [{"id": "middle", "x": 30.0, "y": 0.0, "z": 0.0}]
        self.assertEqual(len(exclusive_speakers(speakers, self.NEAR,
                                                rivals=[(60.0, 0.0, 0.0)])), 1)

    def test_nearest_anchor_reports_which_and_how_far(self):
        anchors = [(0.0, 0.0, 0.0), (10.0, 0.0, 0.0)]
        self.assertEqual(nearest_anchor((9.0, 0.0, 0.0), anchors), (1, 1.0))
        self.assertIsNone(nearest_anchor((99.0, 0.0, 0.0), anchors, limit=40.0))
        self.assertIsNone(nearest_anchor((1.0, 0.0, 0.0), []))


class MapDataCoercionTests(unittest.TestCase):
    def test_a_map_element_is_read_from_its_bounds_when_it_has_no_point(self):
        raw = {"id": "spk1", "channel": "front_l", "minx": -1, "maxx": 1,
               "miny": 6, "maxy": 8, "minz": 2, "maxz": 4, "level": 75}
        spec = coerce_spec(raw)
        self.assertEqual(spec.slot, "front_l")
        self.assertEqual(spec.position, (0.0, 7.0, 3.0))
        self.assertAlmostEqual(spec.level, 0.75)

    def test_a_percent_level_and_a_millisecond_delay_are_understood(self):
        spec = coerce_spec({"x": 1, "y": 2, "z": 3, "channel": "AUTO",
                            "level": 120, "delay_ms": 25})
        self.assertEqual(spec.slot, AUTO_SLOT)
        self.assertAlmostEqual(spec.level, 1.2)
        self.assertAlmostEqual(spec.delay_ms, 25.0)

    def test_junk_in_a_map_element_never_reaches_the_room(self):
        self.assertIsNone(coerce_spec({"channel": "front_l"}))
        self.assertIsNone(coerce_spec({"x": "left", "y": 0, "z": 0}))
        self.assertIsNone(coerce_spec(None))
        # An unrecognised label is not a slot: the position decides instead.
        spec = coerce_spec({"id": "junk1", "x": 0, "y": 4, "z": 0,
                            "channel": "speakers_go_here"})
        self.assertEqual(spec.slot, "speakers_go_here")
        self.assertNotIn(spec.declared, ())
        room = resolve_room([spec, speaker("front_l", -30), speaker("front_r", 30)],
                            ANCHOR)
        self.assertNotIn("speakers_go_here", room.slots)
        self.assertEqual(room.slot_of("junk1"), "front_c")


class ListenerFacingTests(unittest.TestCase):
    def test_bearings_read_the_way_a_room_is_laid_out(self):
        facing_north = ListenerPose((0, 1, 0, 0, 0, 1), (0, 0, 0))
        self.assertAlmostEqual(facing_north.bearing_to((0, 10, 0)), 0.0)
        self.assertAlmostEqual(facing_north.bearing_to((10, 0, 0)), 90.0)
        self.assertAlmostEqual(facing_north.bearing_to((-10, 0, 0)), -90.0)
        self.assertAlmostEqual(abs(facing_north.bearing_to((0, -10, 0))), 180.0)

    def test_turning_around_swaps_what_is_in_front_and_behind(self):
        """This is what makes the room follow the player's head."""
        front_speaker = (0.0, 6.0, 0.0)
        rear_speaker = (0.0, -6.0, 0.0)
        facing_screen = ListenerPose((0, 1, 0, 0, 0, 1), (0, 0, 0))
        facing_away = ListenerPose((0, -1, 0, 0, 0, 1), (0, 0, 0))
        self.assertTrue(facing_screen.is_facing(front_speaker))
        self.assertTrue(facing_screen.has_back_to(rear_speaker))
        self.assertEqual(facing_screen.quadrant_to(front_speaker), "front")
        self.assertEqual(facing_screen.quadrant_to(rear_speaker), "rear")
        # Turning a half-circle: the screen wall is now behind the player.
        self.assertTrue(facing_away.has_back_to(front_speaker))
        self.assertTrue(facing_away.is_facing(rear_speaker))
        self.assertEqual(facing_away.quadrant_to(front_speaker), "rear")

    def test_side_classification_survives_an_off_axis_yaw(self):
        pose = ListenerPose((45.0, 0.0, 0.0), (0, 0, 0))
        # Facing north-east: a speaker due north is on the listener's left.
        self.assertEqual(pose.quadrant_to((0, 10, 0)), "front-left")
        self.assertEqual(pose.quadrant_to((10, 0, 0)), "front-right")
        # Dead ahead of a north-east facing is north-east, not a side.
        self.assertEqual(pose.quadrant_to((10, 10, 0)), "front")

    def test_degrees_orientation_matches_the_openal_vectors(self):
        from_degrees = ListenerPose((90.0, 0.0, 0.0), (0, 0, 0))
        from_vectors = ListenerPose((1, 0, 0, 0, 0, 1), (0, 0, 0))
        self.assertAlmostEqual(from_degrees.yaw_deg, from_vectors.yaw_deg, places=6)

    def test_a_pose_without_a_position_reports_nothing_rather_than_guessing(self):
        pose = ListenerPose((0, 1, 0, 0, 0, 1), None)
        self.assertIsNone(pose.relative((1, 2, 3)))
        self.assertIsNone(pose.quadrant_to((1, 2, 3)))
        self.assertFalse(pose.is_facing((1, 2, 3)))

    def test_a_speaker_aimed_at_the_wall_does_not_reach_the_seats(self):
        """Aimed speakers are physical objects; unaimed ones never fade out."""
        listener = (0.0, 6.0, 0.0)
        speaker_position = (0.0, 0.0, 0.0)
        # Aimed straight at the audience.
        aimed_in = cone_gain(listener, speaker_position, aim_yaw=0.0,
                             inner=120.0, outer=180.0, outer_gain=0.1)
        self.assertEqual(aimed_in, 1.0)
        # Same speaker, turned to face the back wall.
        aimed_away = cone_gain(listener, speaker_position, aim_yaw=180.0,
                               inner=120.0, outer=180.0, outer_gain=0.1)
        self.assertAlmostEqual(aimed_away, 0.1)
        # No aim at all: full coverage, because a cinema with holes is worse
        # than a cinema with imperfect coverage.
        self.assertEqual(cone_gain(listener, speaker_position, aim_yaw=None,
                                   inner=120.0, outer=180.0), 1.0)

    def test_aim_yaw_uses_the_map_data_convention(self):
        self.assertEqual(aim_vector(0.0), (0.0, 1.0, 0.0))
        east = aim_vector(90.0)
        self.assertAlmostEqual(east[0], 1.0)
        self.assertAlmostEqual(east[1], 0.0)

    def test_the_facing_report_reads_a_resolved_room(self):
        room = resolve_room(
            [speaker("front_l", -30), speaker("front_r", 30),
             speaker("rear_l", -150), speaker("rear_r", 150)], ANCHOR)
        pose = ListenerPose((0, 1, 0, 0, 0, 1), (0.0, 0.0, 0.0))
        report = dict((slot, (quadrant, in_front)) for slot, quadrant, in_front, _ in
                      facing_report(pose, room))
        self.assertEqual(report["front_l"][0], "front-left")
        self.assertTrue(report["front_l"][1])
        self.assertFalse(report["rear_l"][1])
        # No listener or no room is an empty reading, never an exception.
        self.assertEqual(facing_report(None, room), ())
        self.assertEqual(facing_report(pose, None), ())


class LayoutIntegrationTests(unittest.TestCase):
    def test_a_resolved_room_drives_the_layout_positions(self):
        room = resolve_room([speaker("front_l", -30), speaker("front_r", 30)], ANCHOR)
        layout = CinemaLayout(ANCHOR, room.specs, use_ring=False)
        self.assertEqual(set(layout.slots), {"front_l", "front_r"})
        self.assertAlmostEqual(layout.position("front_l")[0], -4.0, places=3)

    def test_bearing_from_and_slot_for_bearing_agree_with_the_rooms(self):
        self.assertAlmostEqual(bearing_from(ANCHOR, (0, 5, 0)), 0.0)
        self.assertAlmostEqual(bearing_from(ANCHOR, (5, 0, 0)), 90.0)
        self.assertEqual(slot_for_bearing(0.0), "front_c")
        self.assertEqual(slot_for_bearing(-90.0), "side_l")
        self.assertEqual(slot_for_bearing(150.0), "rear_r")

    def test_the_plan_and_the_placement_report_the_same_room(self):
        room = resolve_room(
            [speaker("front_l", -30), speaker("front_c", 0), speaker("front_r", 30),
             speaker("side_l", -90), speaker("side_r", 90)], ANCHOR)
        plan = RoomPlan(room.profile_name, room.specs, room)
        self.assertEqual(plan.profile, "surround")
        self.assertEqual(len(plan.specs), 5)
        self.assertEqual(plan.warnings, ())
        self.assertEqual(get_profile(plan.profile).name, "surround")


if __name__ == "__main__":
    unittest.main()
