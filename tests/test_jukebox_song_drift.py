"""What the song-drift rig says: song 1 clean, then the songs after it off.

``tools/jukebox_song_drift_sim.py`` prints, per song per machine, how far that
machine's music is from the room's -- the number that decides whether a band
sounds on the beat, because a note is aimed at a *position in the song* and no
timing rule can make up for two machines whose music is seconds apart.

These checks are the gate on that table. They say what each room shape must
read, so the rig cannot quietly stop describing the game:

* a room where every machine plays the same way reads level, on every song --
  the control, without which none of the others means anything;
* a machine whose resolve+startup outruns the lead-in plays the WHOLE song that
  far behind, and reads that far off in the song it was slow in -- the reported
  shape ("the first song is exact, the ones after it drift") is this machine
  being slow on the second song and not the first;
* a machine that joins a song already playing is exact at any offset, unless it
  holds a lead-in the room it joined does not (the mixed room the per-listener
  fallback exists for).
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))

from libs.music_bot.streaming import AudioStreamer
import jukebox_song_drift_sim as rig


def rows(name):
    title, table = rig.rows_for(name)
    return {(row["song"], machine): value
            for row in table
            for machine, value in row["drift_ms"].items()}


def late(name):
    title, table = rig.rows_for(name)
    return {(row["song"], machine): value
            for row in table
            for machine, value in row["late_ms"].items()}


class ARoomOnTheSameBarTests(unittest.TestCase):
    """The controls: a room with nothing to disagree about stays level."""

    def test_everyone_direct_is_level_on_both_songs(self):
        table = rows("all_direct")
        for key, drift in table.items():
            self.assertLess(abs(drift), 30.0, f"{key} drifted {drift:.1f}ms")

    def test_everyone_relayed_is_level_on_both_songs(self):
        # A relay machine holds no lead-in and is audible at the broadcast, so
        # the only difference left between two of them is their own latency.
        table = rows("all_relay")
        self.assertLess(abs(table[(1, "Ann")]), 30.0)
        self.assertAlmostEqual(table[(1, "Bob")], -80.0, delta=20.0)
        self.assertAlmostEqual(table[(2, "Bob")], table[(1, "Bob")], delta=5.0)


class ASlowStartIsWhatTheSongsAfterTheFirstGetTests(unittest.TestCase):
    """The reported shape, and the reason it is not about the song number."""

    def test_a_fast_machine_never_drifts(self):
        # The lead-in is what makes this true: a machine that finishes its own
        # resolve before the deadline waits, and the wait is the alignment.
        table = rows("all_direct")
        self.assertAlmostEqual(table[(1, "Ann")], 0.0, delta=30.0)
        self.assertAlmostEqual(table[(2, "Ann")], 0.0, delta=30.0)

    def test_the_second_song_drifts_when_that_is_the_song_that_started_late(self):
        table = rows("slow_second_song")
        # Song 1: this machine was quick, so it is level -- "the first song is
        # exactly in sync".
        self.assertAlmostEqual(table[(1, "Bob")], 0.0, delta=30.0)
        # Song 2: its own resolve+startup outran the lead-in, and it plays the
        # rest of the song that far behind the room.
        overrun_ms = (2.0 + 2.4 - AudioStreamer.DIRECT_LEAD_IN_S) * 1000.0
        self.assertAlmostEqual(table[(2, "Bob")], -overrun_ms, delta=30.0)
        # ...and it is the LATENESS that is reported, not a drift nobody knows.
        self.assertAlmostEqual(late("slow_second_song")[(2, "Bob")],
                               overrun_ms, delta=30.0)
        self.assertAlmostEqual(late("slow_second_song")[(1, "Bob")], 0.0,
                               delta=10.0)

    def test_the_same_machine_slow_on_the_first_song_drifts_there_instead(self):
        # The mirror image: if the song's *number* were the cause, this row
        # would be clean and the previous one off. It is the slow start.
        table = rows("slow_first_song")
        overrun_ms = (2.0 + 2.4 - AudioStreamer.DIRECT_LEAD_IN_S) * 1000.0
        self.assertAlmostEqual(table[(1, "Bob")], -overrun_ms, delta=30.0)
        self.assertAlmostEqual(table[(2, "Bob")], 0.0, delta=30.0)

    def test_a_machine_slow_on_every_song_drifts_on_every_song(self):
        table = rows("every_song_slow")
        overrun_ms = (2.0 + 2.4 - AudioStreamer.DIRECT_LEAD_IN_S) * 1000.0
        for song in (1, 2):
            self.assertAlmostEqual(table[(song, "Bob")], -overrun_ms,
                                   delta=30.0)

    def test_the_drift_is_exactly_what_the_machine_started_past_the_deadline(self):
        # The rule the rig exists to state: what a machine trails by is its own
        # measured overrun (``direct_late_s``), whole, for the whole song --
        # which is why the client logs it and why nothing downstream can undo
        # it (a note aimed at a position already gone can only sound at once).
        table = rows("slow_second_song")
        for song in (1, 2):
            self.assertAlmostEqual(table[(song, "Bob")],
                                   -late("slow_second_song")[(song, "Bob")],
                                   delta=30.0)


class TheMixedRoomTests(unittest.TestCase):
    """One machine on direct, the room on the relay: the lead-in is the whole
    difference, and holding the right one is the whole fix."""

    def test_a_fallback_that_joins_the_room_level_is_exact(self):
        table = rows("fallback_in_relay_room")
        for key, drift in table.items():
            self.assertLess(abs(drift), 50.0, f"{key} drifted {drift:.1f}ms")

    def test_the_same_machine_holding_a_lead_in_trails_by_one(self):
        table = rows("fallback_holding_the_lead_in")
        self.assertAlmostEqual(table[(1, "Bob")],
                               -AudioStreamer.DIRECT_LEAD_IN_S * 1000.0,
                               delta=50.0)
        self.assertAlmostEqual(table[(2, "Bob")],
                               -AudioStreamer.DIRECT_LEAD_IN_S * 1000.0,
                               delta=50.0)


if __name__ == "__main__":
    unittest.main()
