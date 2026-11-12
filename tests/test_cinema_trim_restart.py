"""A delay trim is a number on the map, and it has to hold for every song.

A cinema speaker's trim is played as a queue depth (``hold_frames``) plus a
sample-exact cut (``_delay_samples``). Both are *spent* -- the depth is built in
the frames after a room starts, and the cut is taken out of the history the room
has queued -- so anything that re-forms the room had to build them again:

* the room's own clock (``_start_frame``) is reset by a stop and a restart, and a
  fill that ran before the clock was pinned paid the same trim a second time
  (measured: 20 ms came back as 1920 samples instead of 960);
* the history survived a restart, so the sample-exact part of a trim was cut out
  of the *previous* song and stacked on top of the new hold (5, 10 and 30 ms came
  back at 1200, 1440 and 2400);
* a trim the map raised while the room was playing was ignored outright, because
  the depth it needed could not be built by a speaker that was already playing
  (0 -> 20 ms stayed at 0 for the rest of the song).

Each of those is a trim a listener hears as "the cabinet plays in step with the
others", on the song after the one the installer dialled it on.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from room_sim import SAMPLERATE, RoomRig


def samples_of(ms):
    """A trim in milliseconds, in samples at the room's own rate."""
    return int(round(float(ms) * SAMPLERATE / 1000.0))


#: Whole frames, part of a frame, and the deepest trim a pool can hold. The ones
#: that are not a whole frame are the ones the history owns, which is why they
#: were the ones a restart doubled.
TRIMS_MS = (5, 10, 20, 30, 40, 60, 100)


def settled(rig, after=40):
    """The trim the pair actually plays once the room has settled."""
    return sorted(set(rig.lag("front_r", "front_l")[-after:]))


class ADelayTrimBelongsToTheMapTests(unittest.TestCase):
    """What a listener hears on the song after the one the trim was set on."""

    def test_a_trim_survives_the_room_being_stopped_and_started(self):
        """A stop and a restart is the next song on the same room.

        The Music Bot hands the same room a new track, and a listener's own
        player resets the room's output for it. The new song's first frames come
        from a queue that was emptied, so every trim has to be built again from
        the map -- not inherited from the song before it, and not paid twice.
        """
        for trim in TRIMS_MS:
            rig = RoomRig({"front_r": float(trim)})
            rig.run(80)
            rig.bank._reset_output()
            rig.run(140)
            where = "%d ms trim, stopped and started" % trim
            self.assertEqual(settled(rig), [samples_of(trim)], where)
            self.assertEqual(rig.silent_slots(), [], where)

    def test_every_restart_builds_the_trim_again_and_never_twice(self):
        """Three songs in a row are one trim, not one trim per song."""
        for trim in (5, 20, 30, 100):
            rig = RoomRig({"front_r": float(trim)})
            for _ in range(3):
                rig.run(60)
                rig.bank._reset_output()
            rig.run(140)
            where = "%d ms trim, restarted three times" % trim
            self.assertEqual(settled(rig), [samples_of(trim)], where)

    def test_a_trim_the_map_raises_mid_song_is_heard_at_once(self):
        """The installer's own case: the delay is dialled in while it plays.

        A speaker that has to sit deeper cannot grow its queue while it plays,
        so the room holds it for exactly the frames its new trim needs and then
        carries on -- heard as that one speaker pausing for a few milliseconds,
        never as the room playing two delays at once.
        """
        for trim in TRIMS_MS:
            rig = RoomRig({})
            rig.run(80)
            rig.delays["front_r"] = float(trim)
            rig.reconfigure()
            rig.run(160)
            where = "%d ms trim, set while the room played" % trim
            self.assertEqual(settled(rig), [samples_of(trim)], where)
            self.assertEqual(rig.silent_slots(), [], where)

    def test_a_trim_shared_by_every_speaker_is_no_delay_at_all(self):
        """A trim is a difference between speakers, not an offset for the room.

        Dialling the same delay into every speaker cannot move one against
        another, so the room plays in step (and keeps its cut, which is what the
        choir of identical speakers was dialled for).
        """
        rig = RoomRig({"front_l": 20.0, "front_r": 20.0})
        rig.run(140)
        self.assertEqual(settled(rig), [0])
        self.assertEqual(rig.silent_slots(), [])


if __name__ == "__main__":
    unittest.main()
