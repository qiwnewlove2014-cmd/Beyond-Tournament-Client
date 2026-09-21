"""A cabinet whose transport changes frame size under the song.

A speaker's delay trim is played as two numbers: whole **frames** of queue
depth, and a sample-exact cut (``hold_frames`` / ``_delay_samples``). The count
is a duration only while the frame's size is known, and it is spent when the
room starts -- so a transport that changes size mid-song leaves every trimmed
speaker playing the delay the *old* size put there. Nothing on the listener's
side can see that happen: the room keeps playing, every speaker keeps its
queue, and only the alignment between them is wrong.

Both sizes are real, and the change is a recovery the game takes: the direct
streamer decodes 20 ms frames, the server relay hands 40 ms PCM ones, and
``request_room`` moves a room from one transport to the other while its song
plays (a cabinet the watchdog pulls onto the direct path, a room the relay
takes back over).

Measured on this harness (``tools/cinema_transport_change_sim.py``): a 20 ms trim heard as
960 samples read 1920 after a room that started on 20 ms frames was fed 40 ms
ones, and 2880 the other way. Re-spelling the trims where they stand cannot fix
that -- the delay is carried by how many frames deeper a trimmed speaker's
queue sits, and a queue only ever grows -- so a change re-forms the room, which
is what these tests hold it to.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from room_sim import FRAME_20MS, FRAME_40MS, SAMPLERATE, RoomRig


def samples_of(ms):
    """A trim in milliseconds, in samples at the room's own rate."""
    return int(round(float(ms) * SAMPLERATE / 1000.0))


#: Every trim whose spelling the two sizes disagree about. The values under a
#: frame are the interesting ones (5 and 30 ms are carried by the cut, 20 and
#: 40 ms by whole frames), and 100 ms is the deepest one a pool can hold.
TRIMS_MS = (5, 10, 20, 30, 40, 60, 100)

#: The two transports, in both directions: 20 <-> 40 ms.
SWAPS = ((FRAME_20MS, FRAME_40MS), (FRAME_40MS, FRAME_20MS))


class ATransportThatChangesSizeTests(unittest.TestCase):
    """What a listener hears across the change, both ways."""

    def swap(self, trim_ms, first, second):
        """Play at one frame size, change it, and read the delay each way."""
        rig = RoomRig({"front_r": float(trim_ms)}, samples=first)
        rig.run(300)
        before = sorted(set(rig.lag("front_r", "front_l")[60:]))
        rig.samples = second
        rig.run(400)
        after = sorted(set(rig.lag("front_r", "front_l")[60:]))
        return rig, before, after

    def test_every_trim_still_lands_where_the_map_asked_both_ways(self):
        """The delay a listener hears is the installer's number, not a frame.

        An installer dials a delay to decorrelate two speakers, or to aim a
        room. A change of transport that leaves the pair a frame (or two) out
        is heard as the image moving and staying moved -- the song has not
        changed, only which machine is delivering it.
        """
        for trim in TRIMS_MS:
            wanted = samples_of(trim)
            for first, second in SWAPS:
                rig, before, after = self.swap(trim, first, second)
                where = "%d ms trim, %d -> %d samples" % (trim, first, second)
                self.assertEqual(before, [wanted], where)
                self.assertEqual(after, [wanted],
                                 "%s: the trim moved when the transport "
                                 "changed" % where)
                self.assertEqual(rig.silent_slots(), [], where)

    def test_a_re_formed_room_still_steps_by_whole_frames(self):
        """A re-form may drop frames; it may never splice one.

        The frames already queued are given back, so a listener hears the room
        hold for a moment and the song continue on the live edge -- a step in
        the programme, but a whole number of frames of one size or the other.
        Anything else is audio spliced together (a frame replayed, or half of
        one), which is the "it stumbles over the same bar" failure this room
        has had before. Read on the speaker's own stream, which is what a
        splice would show up in.
        """
        rig, _before, after = self.swap(30, FRAME_20MS, FRAME_40MS)
        self.assertEqual(after, [samples_of(30)])
        for slot in ("front_l", "front_r"):
            steps = rig.steps(slot)
            self.assertTrue(steps, slot)
            self.assertEqual([step % FRAME_20MS for step in steps],
                             [0] * len(steps),
                             "%s was spliced out of the past: %s"
                             % (slot, sorted(set(steps))))

    def test_a_torn_frame_does_not_re_form_a_room(self):
        """Only a size that keeps arriving is a new transport (see bank.py)."""
        rig = RoomRig({"front_r": 30.0}).run(120)
        rig.short_frame()
        rig.run(120)

        self.assertEqual(rig.bank.frame_size_changes, 0)
        # The torn frame is a frame the room played, so the read lag moves by
        # its own shorter size for that one buffer and back -- what must not
        # happen is a re-form (the room rebuilt, and the delay moved with it).
        lags = set(rig.lag("front_r", "front_l")[80:])
        self.assertTrue(lags and lags <= {samples_of(30),
                                          samples_of(30) - FRAME_20MS // 2},
                        sorted(lags))

    def test_a_room_that_cannot_give_its_buffers_back_is_left_standing(self):
        """A speaker the backend will not empty is not half of a new room.

        Emptying a speaker is the one thing that cannot be worked around: its
        queue is what carries its delay, and OpenAL cannot put audio back. So
        the attempt stops at the first speaker that refuses, before the room's
        clock is started over -- the room stays the room it was, playing, one
        frame out at worst, rather than a new session growing around a queue
        from the old one.
        """
        rig = RoomRig({"front_r": 30.0}, samples=FRAME_20MS)
        rig.run(300)
        rig.refuse_unqueue("front_l")
        rig.samples = FRAME_40MS
        rig.run(10)                 # long enough for the new size to be believed

        self.assertEqual(rig.bank.frame_size_changes, 1)
        self.assertEqual(rig.silent_slots(), [],
                         "a room that could not re-form went quiet")
        self.assertTrue(rig.bank.playing(),
                        "a room that could not re-form stopped playing")

    def test_the_notes_waiting_on_the_room_are_kept_not_dropped(self):
        """A re-form is not a teardown: a performer's note still sounds.

        The room's clock starts over with its queue (the waits are measured
        against a queue that no longer exists), but the note itself was played
        by somebody and the song is not rewinding -- dropping it would cost
        one note per re-form, silently, on the machine that cannot hear it.
        """
        fired = []
        rig = RoomRig({"front_r": 30.0}, samples=FRAME_20MS)
        rig.run(120)
        self.assertTrue(rig.bank.wait_advance(500.0, lambda: fired.append(1)))
        self.assertEqual(rig.bank.pending_waits(), 1)

        rig.samples = FRAME_40MS
        rig.run(60)

        self.assertEqual(rig.bank.pending_waits(), 1,
                         "the re-form dropped a note waiting on the room")
        self.assertEqual(fired, [])

    def test_a_size_change_before_the_song_only_has_to_be_believed(self):
        """No speaker has started, so there is no delay to re-form.

        ``frame_size_changes`` is what a re-form is decided on, and it counts
        only a change to a room that has *played* -- a room still filling its
        first pre-buffer is learning its transport, not losing an alignment.
        (What a first start makes of a queue holding two sizes at once is a
        question of its own, and not one a real room asks: a cabinet builds its
        room empty and is then fed by one transport.)
        """
        rig = RoomRig({"front_r": 30.0}, samples=FRAME_20MS)
        rig.run(3, start=False)
        rig.samples = FRAME_40MS
        rig.run(3, start=False)

        self.assertEqual(rig.bank.frame_size_changes, 0)
        self.assertFalse(rig.bank.playing(), "the room started on its own")
        rig.run(200)
        self.assertEqual(rig.silent_slots(), [],
                         "the room never started on the size it was given")


if __name__ == "__main__":
    unittest.main()
