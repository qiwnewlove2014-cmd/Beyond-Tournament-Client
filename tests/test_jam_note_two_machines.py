"""Two machines, one song: the note has to land on the same music.

``tests/two_machine_jam_sim.py`` plays one song on several machines and
measures, per machine, the music position a note came out at against the
position the performer was hearing when they struck. This is the gate on top
of it -- the properties that must hold whatever a queue does, and the offsets
the code is *expected* to have, each pinned so a later change cannot quietly
move it.

The rig's own resolution is part of the truth and no assertion here is
tighter than it: a note is spawned on a gameplay frame (20 ms), a relay pair
reads its position one frame at a time (40 ms), and a wait that is re-projected
may hand the note in up to ``JAM_WAIT_EARLY_FRAMES`` frames early. So the tests
are phrased in frames and in *differences between two runs of the same rig*
(the same strike phases, the same machines), which is where the millisecond in
a number means something.

What the runs have shown, and what each class here is about:

  * with every number honest, the band lands on the beat (both regimes: Bob's
    note is *held*, Cid's arrives too late to be thrown and sounds at once);
  * the lateness that is left is the *performer's* unmeasured in-flight delay
    -- the same person on a slower line lands the whole band later;
  * a performer whose own song cannot be measured at that instant leaves the
    note a whole queue window late -- the "behind by one window" report;
  * a machine's own note costs what it costs: less than the constant and it
    lands early, more and it lands late until that machine has measured
    itself, and exactly on the beat once it has;
  * a queue that sheds or holds takes the note *with* it: the wall instant
    moves, the music does not (beyond the clock's own early bound);
  * and one machine's note is lost today if its output is replaced while the
    note is waiting -- pinned at the bottom, with the reason.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from libs.event_handeler import EventHandeler
from libs.jukebox_clock import JAM_WAIT_EARLY_FRAMES

import two_machine_jam_sim
from two_machine_jam_sim import RELAY_FRAME_MS, STRIKES, run

# One relay frame: the grain a measurable claim may not go under.
FRAME = RELAY_FRAME_MS
# The machines the scenarios were built around (``two_machine_jam_sim``).
HELD = "Bob"          # a listener whose note is waited out, not fired at once
NOW = "Cid"           # a listener whose note arrives after its beat

JAM_NOTE_ADVANCE_MS = EventHandeler.JAM_NOTE_ADVANCE_MS


def means(name, strikes=STRIKES):
    """The mean error per listener, and the run behind it."""
    result = run(name, strikes=strikes)
    return ({machine: sum(values) / len(values)
             for machine, values in result["errors"].items()}, result)


class TheRigIsTheGamesOwnCodeTests(unittest.TestCase):
    """A measurement is only worth what it is measuring."""

    def test_the_hold_is_placed_by_the_real_handler(self):
        _error, result = means("honest")
        detail = result["details"][HELD]
        # The number the performer sent is its own relay reading (four frames
        # of 40 ms), the buffer is this listener's own, and the detail line is
        # the one the game builds -- so the scheduler, the measurement and the
        # clock under test are all the shipping ones.
        self.assertEqual(detail["sender_lag_ms"], 160)
        self.assertEqual(detail["buffer_ms"], 320)
        self.assertEqual(detail["detail"], "relay 8 frames spawn=45ms")
        self.assertGreater(detail["held_ms"], 0)

    def test_the_other_regime_is_the_one_with_nothing_to_wait_for(self):
        """A queue as deep as the performer's leaves the beat already gone."""
        _error, result = means("honest")
        detail = result["details"][NOW]
        self.assertEqual(detail["held_ms"], 0)      # nothing to wait out
        self.assertEqual(detail["sender_lag_ms"], 160)


class TheBandLandsOnTheBeatTests(unittest.TestCase):
    """Every number honest: both regimes come out on the music."""

    def test_both_machines_land_on_the_music(self):
        error, _result = means("honest")
        self.assertLessEqual(abs(error[HELD]), FRAME)
        self.assertLessEqual(abs(error[NOW]), FRAME)

    def test_the_two_machines_are_a_frame_of_each_other_not_a_queue(self):
        """The sentence this rig exists for, with its own grain stated.

        Two machines on one song, one of them holding a queue twice as deep:
        the music they put the note on is within a couple of frames, and a
        queue (160 ms) is four.
        """
        error, _result = means("honest")
        self.assertLessEqual(abs(error[HELD] - error[NOW]), 2 * FRAME)


class ALatenessNobodyMeasuresTests(unittest.TestCase):
    """What is left after the queue is counted: the part nobody sent."""

    def test_the_performers_own_distance_lands_the_whole_band_late(self):
        """The same band, the same song, one performer on a slower line.

        Their ear is further behind the song while ours is not, so the beat
        they struck is an older piece of music -- with nothing but a wall clock
        the note can only be late by exactly the difference in the *unmeasured*
        half of the two distances (120 ms against 40 ms), and that is the same
        number for every listener: the band behind the song on every machine at
        once, by a fixed amount. This is the row above, on a build whose
        packets carry ``sender_lag_ms`` only -- what shipped before the
        position stamp -- and it is here so that removing the stamp means
        something.
        """
        honest, _a = means("honest")
        without, _c = means("sender_slow_nostamp")
        self.assertAlmostEqual(without[HELD] - honest[HELD], 80.0, delta=FRAME)
        self.assertAlmostEqual(without[NOW] - honest[NOW], 80.0, delta=FRAME)

    def test_a_position_stamp_takes_the_performers_stream_out_of_the_hold(self):
        """What the stamp buys, on the very same machines.

        The lag number says how far the performer's stream trails the Server's
        clock; the stamp says where in the *song* their ears were. Only the
        second one is what a listener can aim at, so the note that is held (a
        deep queue) lands on the beat instead of a fixed distance behind it,
        and the difference from the unstamped run is the performer's own
        transit -- the half of their distance that no packet ever measured.

        The listener whose queue is no deeper than the performer's is *not*
        helped, and must not be claimed to be: their note arrives after the
        beat it was aimed at (the performer's slow link is in the packet's own
        flight time), and no scheduling on the listening side can pull a note
        back in time. That half is a network, not a bug.
        """
        honest, _a = means("honest")
        stamped, _b = means("sender_slow")
        without, _c = means("sender_slow_nostamp")
        self.assertLessEqual(abs(stamped[HELD] - honest[HELD]), 2 * FRAME)
        self.assertAlmostEqual(without[HELD] - honest[HELD], 80.0, delta=FRAME)
        self.assertAlmostEqual(stamped[NOW], without[NOW], delta=FRAME)

    def test_a_stamp_that_never_arrives_changes_nothing(self):
        """A Server that predates the field drops it; the timing is the same.

        ``packet_validator`` strips what it does not declare, so an old Server
        simply forwards no stamp -- and an old listener ignores one. Both runs
        must be the same music to the millisecond, because a build that cannot
        answer where it is must keep the path it always had.
        """
        for scenario in ("honest", "listener_shed"):
            stamped, _a = means(scenario)
            without, _b = means(scenario + "_nostamp")
            for machine in stamped:
                self.assertAlmostEqual(stamped[machine], without[machine], delta=0.5)

    def test_a_performer_that_cannot_measure_leaves_a_whole_queue_window(self):
        """The reported symptom, reproduced: behind by one queue.

        When the performer's own client cannot answer at that instant (their
        stream mid-rebuild, still pre-buffering, or its output handed to a
        room) nothing is subtracted for their distance, and every listener
        holds the note for their own queue *on top of* the beat -- late by the
        performer's own measured backlog, which is the four frames they would
        have reported.
        """
        honest, _a = means("honest")
        missing, result = means("sender_missing")
        self.assertAlmostEqual(missing[HELD] - honest[HELD], 160.0, delta=FRAME)
        self.assertIsNone(result["details"][HELD]["sender_lag_ms"])
        self.assertGreater(missing[HELD], 3 * FRAME)         # a queue, not a frame


class AServerOlderThanTheStampTests(unittest.TestCase):
    """What a listener loses when the note's own Server has not been updated.

    The stamp crosses inside the packet the Server relays, so a Server that
    predates the field strips it (it has stripped unknown keys since its
    earliest schema, which is what makes adding one safe) and the listener is
    left with the lag path. That is the whole reason the Server has to be
    updated -- and the reason a *bridge* was looked for: an older Server that
    would carry the stamp some other way. None exists, and this class is where
    that is measured rather than assumed: the two ways a note can reach a
    listener without a stamp (a performer who cannot send one, and a Server
    that does not carry it) land the band in exactly the same place.
    """

    def test_a_stripped_stamp_is_the_same_music_as_no_stamp_at_all(self):
        stripped, _a = means("sender_slow_oldserver")
        absent, _b = means("sender_slow_nostamp")
        for machine in stripped:
            self.assertAlmostEqual(stripped[machine], absent[machine], delta=0.5)

    def test_and_the_band_is_a_fixed_distance_behind_the_beat_again(self):
        """The old timing, to the millisecond: +80 ms on every listener.

        The performer's own transit (120 ms against 40 ms) is unreported and
        nothing can see it, so the note waits its own queue and lands late by
        the difference -- the report the users gave, reproduced on a Server that
        predates the stamp.
        """
        honest, _a = means("honest")
        stripped, _b = means("sender_slow_oldserver")
        self.assertAlmostEqual(stripped[HELD] - honest[HELD], 80.0, delta=FRAME)
        self.assertAlmostEqual(stripped[NOW] - honest[NOW], 80.0, delta=FRAME)

    def test_a_band_that_was_already_honest_loses_nothing(self):
        """Which is why this is a *refinement*: an old Server costs nobody a note.

        The stamp only ever repairs a performer whose own stream trails the
        Server's clock; when every machine is level with it, the lost
        refinement is invisible, so an old Server is not a reason to stay off
        the new Client.
        """
        honest, _a = means("honest")
        old, _b = means("honest_oldserver")
        for machine in honest:
            self.assertAlmostEqual(honest[machine], old[machine], delta=0.5)


class WhatANoteCostsHereTests(unittest.TestCase):
    """The constant is an allowance; the machine measures what it really pays."""

    def test_a_machine_faster_than_the_constant_lands_early(self):
        """25 ms of spawn against the 45 ms the target already spent."""
        honest, _a = means("honest")
        fast, _b = means("fast_machine")
        self.assertAlmostEqual(fast[HELD] - honest[HELD],
                               -(JAM_NOTE_ADVANCE_MS - 25.0), delta=FRAME)

    def test_a_slow_machine_is_late_until_it_has_measured_itself(self):
        honest, _a = means("honest")
        slow, result = means("slow_unmeasured")
        self.assertAlmostEqual(slow[HELD] - honest[HELD],
                               95.0 - JAM_NOTE_ADVANCE_MS, delta=FRAME)
        self.assertNotIn("spawn=", str(result["details"][HELD]["detail"]))

    def test_and_on_the_beat_once_it_has(self):
        """The same slow machine after one note: the excess is spent early."""
        honest, _a = means("honest")
        measured, result = means("slow_measured")
        self.assertAlmostEqual(measured[HELD], honest[HELD], delta=FRAME)
        self.assertIn("spawn=95ms", str(result["details"][HELD]["detail"]))


class AQueueThatMovesTakesTheNoteWithItTests(unittest.TestCase):
    """What the re-projection buys: the wall moves, the music does not."""

    def test_a_shed_hands_the_note_in_sooner_and_keeps_the_music(self):
        """A queue that skips music: the beat is closer, so the note comes in.

        Both halves matter -- the wall instant follows the output (which is
        what a wait measured once could not do), and the music does not move
        beyond the clock's own early bound.
        """
        honest_error, honest = means("honest")
        shed_error, shed = means("listener_shed")
        self.assertLess(shed["wall"][HELD], honest["wall"][HELD])
        self.assertLessEqual(
            abs(shed_error[HELD] - honest_error[HELD]),
            JAM_WAIT_EARLY_FRAMES * FRAME)
        # ... and the machine nothing happened to did not move at all.
        self.assertAlmostEqual(shed_error[NOW], honest_error[NOW], delta=FRAME)
        self.assertEqual(shed["lost"].get(NOW, 0), 0)

    def test_a_hold_does_not_wait_for_the_song_it_stopped(self):
        """This machine's song stops, and the note keeps its instant.

        The projection is anchored to the instant the note was given and only
        the *earlier* direction is re-projected freely, so a stall does not
        push the note out with the stopped music: the note comes out while the
        song is still at its older position, i.e. early in the music by about
        the stall -- bounded above by the stall itself, never more. A later
        change that makes a stalled machine wait for its song moves this
        number, which is why it is written down.
        """
        honest_error, honest = means("honest")
        hold_error, hold = means("listener_hold")
        moved = hold_error[HELD] - honest_error[HELD]
        self.assertLess(moved, 0.0)
        self.assertGreaterEqual(moved, -140.0)          # the stall's own length
        self.assertAlmostEqual(hold["wall"][HELD], honest["wall"][HELD],
                               delta=FRAME)
        self.assertAlmostEqual(hold_error[NOW], honest_error[NOW], delta=FRAME)
        self.assertEqual(hold["lost"].get(NOW, 0), 0)


class AnOutputThatWentAwayTests(unittest.TestCase):
    """A note waiting when its output is replaced is still heard.

    The wait lives on the clock of the streamer that *measured* it, and the
    only pump of a pair's clock is the jukebox player's own frame, which pumped
    the entry's *current* streamer -- so a note waiting when the relay fell back
    to direct (``JukeboxPlayer._switch_to_direct``) was never fired and never
    dropped: not late, simply not heard. The player keeps a replaced output's
    clock now and pumps it until it has nothing left, and the mechanism is
    pinned against the real ``JukeboxPlayer`` in
    ``test_jam_note_pair_clock.AnOutputThatIsReplacedTests``; this is what it
    buys at the level of the music -- the note lands where the others do, on
    the song it was played against, instead of vanishing.
    """

    def test_a_note_waiting_when_the_relay_goes_still_lands_on_the_music(self):
        honest_error, honest = means("honest")
        error, result = means("switch_to_direct")
        self.assertEqual(result["lost"].get(HELD, 0), 0)
        self.assertLessEqual(abs(error[HELD] - honest_error[HELD]),
                             JAM_WAIT_EARLY_FRAMES * FRAME)
        self.assertLessEqual(abs(error[HELD]), 2 * FRAME)

    def test_the_machine_that_did_not_switch_is_unaffected(self):
        honest_error, honest = means("honest")
        error, result = means("switch_to_direct")
        self.assertEqual(result["lost"].get(NOW, 0), 0)
        self.assertAlmostEqual(error[NOW], honest_error[NOW], delta=FRAME)


if __name__ == "__main__":
    unittest.main()
