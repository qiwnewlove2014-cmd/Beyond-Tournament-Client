"""A map's PA cabinets are one system: they fire together.

The report this file answers: put more than a couple of megaphone speakers on
a map and they are plainly late against each other -- an echo, "not at the same
time", and it happened even with a mono source, because nothing here is about
the stereo pair.

The cause was the *origin* of the delay, not its size. Every speaker paid
`distance(listener, speaker) / 343 m/s` plus the installer's trim, so:

  * two cabinets 30 m apart arrived 87 ms apart at that listener;
  * the nearest cabinet was still paid its own distance (a 50 m hall = 145 ms
    of start latency before any stagger);
  * the stagger was measured from the listener, so the image moved as they
    walked, and two listeners never heard the same PA.

It was also counted twice: a frame queued `d/343` late is played out of a
speaker `d/343` away, so it ARRIVES `2d/343` late -- two cabinets 30 m apart
were 174 ms apart at the ear, not 87 ms.

The rule now: **the air does the propagation, and only the air.** The shipped
spread is zero, so a cabiniet plays the frame the moment it arrives and the
room's own geometry is the whole spatial effect. The maths that used to run
here (`speaker_path_distances`, `aligned_stagger_seconds`) is kept and pinned
with an explicit cap, because a bounded decorrelation is what a stadium ring
would ask for later; the shipped `MEGAPHONE_SPREAD_SECONDS` is 0. The
installer's per-speaker `delay` (0-0.5 s) is added on top uncapped -- it is the
builder's own alignment trim and must stay what they typed.

Pins `libs/voice_chat.py` (`speaker_path_distances`,
`aligned_stagger_seconds`, `speaker_frames_delay`).
"""

import math
import unittest

from libs import voice_chat

SPEED = voice_chat.SPEED_OF_SOUND
CAP = voice_chat.MEGAPHONE_SPREAD_SECONDS
FRAME = voice_chat.MEGAPHONE_FRAME_SECONDS
# The cap the maths supports when a map asks for a bounded decorrelation: three
# 20 ms frames. The SHIPPED cap is 0 s (see the comment in libs/voice_chat.py).
THREE_FRAMES = 3 * FRAME


def speaker(x=0.0, y=0.0, z=0.0, delay=0.0):
    return {"position": (float(x), float(y), float(z)), "delay": float(delay)}


class PathTests(unittest.TestCase):
    def test_two_paths_per_speaker_direct_then_floor(self):
        distances = voice_chat.speaker_path_distances(
            [speaker(0, 0, 0), speaker(10, 0, 0)], (0, 0, 0), ground_z=0.0)
        self.assertEqual(len(distances), 4)
        self.assertAlmostEqual(distances[0], 0.0)
        self.assertAlmostEqual(distances[1], 0.0)   # standing in it: no bounce
        self.assertAlmostEqual(distances[2], 10.0)
        self.assertAlmostEqual(distances[3], 10.0)

    def test_the_bounce_is_the_longer_path(self):
        distances = voice_chat.speaker_path_distances(
            [speaker(0, 0, 4)], (10, 0, 0), ground_z=0.0)
        direct, bounce = distances
        self.assertAlmostEqual(direct, math.hypot(10, 4))
        self.assertAlmostEqual(bounce, 4 + 10)

    def test_a_half_built_speaker_list_does_not_raise(self):
        distances = voice_chat.speaker_path_distances(
            [None, {"position": None}, speaker(3, 4, 0)], (0, 0, 0))
        self.assertEqual(len(distances), 6)
        self.assertAlmostEqual(distances[4], 5.0)

    def test_a_missing_list_is_empty(self):
        self.assertEqual(voice_chat.speaker_path_distances(None, (0, 0, 0)), [])


class StaggerTests(unittest.TestCase):
    def test_the_shipped_spread_is_zero_because_the_air_already_delays(self):
        # 200 m across a hall: every source still plays on arrival. Any number
        # here would be added on top of the air's own propagation.
        self.assertEqual(voice_chat.MEGAPHONE_SPREAD_SECONDS, 0.0)
        delays = voice_chat.aligned_stagger_seconds([5.0, 60.0, 200.0])
        self.assertEqual(delays, [0.0, 0.0, 0.0])

    def test_the_nearest_speaker_is_the_reference(self):
        delays = voice_chat.aligned_stagger_seconds(
            [40.0, 10.0, 25.0], max_spread=0.06)
        self.assertAlmostEqual(delays[1], 0.0)
        # The two others are ordered and positive: the precedence effect keeps
        # the sound at the nearest cabinet.
        self.assertGreater(delays[2], 0.0)
        self.assertGreater(delays[0], delays[2])

    def test_the_spread_is_capped_at_three_frames(self):
        delays = voice_chat.aligned_stagger_seconds([500.0, 0.0],
                                                   max_spread=THREE_FRAMES)
        self.assertAlmostEqual(delays[0], THREE_FRAMES)
        self.assertLessEqual(max(delays), THREE_FRAMES)
        self.assertLessEqual(int(max(delays) / FRAME), 3)

    def test_a_hall_of_twenty_cabinets_spans_at_most_three_frames(self):
        # The report: "many cabinets, always late against each other". With the
        # cap at three frames the whole hall is inside one fusion window.
        distances = [float(metres) for metres in range(5, 105, 5)]
        frames = [int(d / FRAME) for d in
                  voice_chat.aligned_stagger_seconds(distances, max_spread=0.06)]
        self.assertLessEqual(max(frames) - min(frames), 3)

    def test_the_old_rule_is_gone(self):
        # distance / 343 for a 50 m cabinet was 145 ms of pure latency, for
        # every cabinet, before its trim -- and twice that at the ear.
        delays = voice_chat.aligned_stagger_seconds([50.0])
        self.assertEqual(delays, [0.0])

    def test_an_empty_or_unusable_set_is_all_zeroes(self):
        self.assertEqual(voice_chat.aligned_stagger_seconds([]), [])
        self.assertEqual(voice_chat.aligned_stagger_seconds([None, None]), [0.0, 0.0])

    def test_none_entries_keep_their_index(self):
        delays = voice_chat.aligned_stagger_seconds([10.0, None, 30.0], max_spread=0.06)
        self.assertAlmostEqual(delays[0], 0.0)
        self.assertEqual(delays[1], 0.0)
        self.assertAlmostEqual(delays[2], 20.0 / SPEED)


class FrameTests(unittest.TestCase):
    def test_the_frame_quantum_is_twenty_milliseconds(self):
        self.assertEqual(voice_chat.speaker_frames_delay(0.0, 0.0), 0)
        self.assertEqual(voice_chat.speaker_frames_delay(0.02, 0.0), 1)
        self.assertEqual(voice_chat.speaker_frames_delay(0.0, 0.04), 2)

    def test_the_installers_trim_is_not_capped(self):
        # 0.5 s is the element's own maximum and the whole point of the trim.
        self.assertEqual(voice_chat.speaker_frames_delay(0.5, 0.0), 25)
        self.assertEqual(voice_chat.speaker_frames_delay(0.35, THREE_FRAMES), 20)

    def test_the_trim_keeps_the_builders_order(self):
        trims = [0.0, 0.1, 0.25, 0.5]
        frames = [voice_chat.speaker_frames_delay(trim, 0.0) for trim in trims]
        self.assertEqual(frames, sorted(frames))
        self.assertEqual(frames, [0, 5, 12, 25])

    def test_junk_reads_as_no_delay(self):
        self.assertEqual(voice_chat.speaker_frames_delay(None, None), 0)
        self.assertEqual(voice_chat.speaker_frames_delay("", ""), 0)
        self.assertEqual(voice_chat.speaker_frames_delay(-4, -4), 0)


class QuotesTheElementTests(unittest.TestCase):
    """The attribute the builder writes is the number this file's maths uses.

    `megaphone_speaker.ts` clamps nothing but documents 0-0.5 s, the input flow
    refuses outside `0.0 <= value <= 0.5`, and `world_map.spawn_megaphoneSpeaker`
    stores it as seconds. A trim above 0.5 s would be a different unit
    somewhere, so the boundary is asserted here rather than assumed.
    """

    def test_half_a_second_is_twenty_five_frames(self):
        self.assertAlmostEqual(0.5 / FRAME, 25.0)
        self.assertEqual(voice_chat.speaker_frames_delay(0.5, 0.0), 25)


if __name__ == "__main__":
    unittest.main()
