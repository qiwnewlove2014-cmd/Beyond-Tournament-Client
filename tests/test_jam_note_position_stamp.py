"""The position stamp: where in the song a machine's own ears are.

A live note has to land at the *position in the song* the performer heard when
they struck it. Neither the instant they struck (a wall clock, shared) nor the
distance they reported (``sender_lag_ms``, a number the listener cannot check)
says that: a performer whose own stream sits a whole queue behind the Server's
clock reports a number that is true about their player and false about the
music, and the note lands a whole queue behind the beat for everyone listening
(``tests/two_machine_jam_sim.py``, the ``sender_missing`` and
``sender_slow_nostamp`` rows).

``EventHandeler._audible_song_position_ms`` is that position, computed the same
way on both machines from what the Server said about the song -- the only ruler
two clients share without trusting each other's clocks -- and
``_sender_position_ms`` is the stamp read back off an arriving note. These
checks pin the arithmetic itself (the rig pins what it does to the music).

The rule that matters most here is the one subtraction: a direct stream's late
audible start is *part of* the trail ``_active_jukebox_buffer_ms`` measures, so
subtracting the lateness again reads the song too early by it. One rule for both
transports, and this file is where that stays true.
"""

import os
import sys
import time
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from libs.event_handeler import EventHandeler


def handler_for(entry, *, buffer_kind="relay"):
    """A real handler with the one entry and the trail already measured."""
    handler = EventHandeler.__new__(EventHandeler)
    handler.gameplay = SimpleNamespace(
        jukebox_player=SimpleNamespace(players={"box": entry}))
    handler._jam_buffer_kind = buffer_kind
    handler._jam_entry = entry
    return handler


class TheStampReadOffANoteTests(unittest.TestCase):
    """What a note may carry, and what this side refuses to believe."""

    def test_a_position_is_a_position(self):
        self.assertEqual(EventHandeler._sender_position_ms({"sender_position_ms": 42000}), 42000.0)
        self.assertEqual(EventHandeler._sender_position_ms({"sender_position_ms": 0}), 0.0)

    def test_a_note_with_no_stamp_says_nothing(self):
        self.assertIsNone(EventHandeler._sender_position_ms({}))
        self.assertIsNone(EventHandeler._sender_position_ms({"sender_position_ms": None}))

    def test_and_a_nonsense_one_is_not_a_position(self):
        # A Server that predates the field never forwards it; a build that
        # cannot measure itself sends none; a client sending nonsense must not
        # be able to aim anybody's notes.
        for value in (-1, "42000", float("nan"), float("inf"), {"ms": 1}):
            self.assertIsNone(
                EventHandeler._sender_position_ms({"sender_position_ms": value}),
                f"{value!r} is not a position in a song")

    def test_a_packet_that_is_not_a_mapping_is_not_a_crash(self):
        self.assertIsNone(EventHandeler._sender_position_ms(None))


class WhereThisMachinesEarsAreTests(unittest.TestCase):
    """The position, from the Server's own number and this machine's trail."""

    def _entry(self, start_ms, received_ago_ms=0.0, **streamer):
        return {
            "streamer": SimpleNamespace(**streamer),
            "start_offset": start_ms / 1000.0,
            "start_offset_received_at": time.monotonic() - (received_ago_ms / 1000.0),
        }

    def test_the_server_position_minus_what_this_machine_trails_by(self):
        # The song was at 40.0 s when the play event was built, the event
        # arrived 500 ms ago, and this machine holds 320 ms of it ahead of the
        # ear: its own ears are at 40.0 + 0.5 - 0.32 = 40.18 s of the song.
        handler = handler_for(self._entry(40000.0, 500.0))
        position = handler._audible_song_position_ms(buffer_ms=320.0)
        self.assertAlmostEqual(position, 40180.0, delta=20.0)

    def test_a_direct_streams_late_start_is_already_inside_the_trail(self):
        """The one rule: ``buffer_ms`` is the whole distance, not part of it.

        ``_active_jukebox_buffer_ms`` answers ``queued * 20 + late_ms`` on a
        direct stream -- being late to start and holding frames ahead of the ear
        are the same distance behind the music -- so a machine with 80 ms of
        queue and a 240 ms late start is exactly as far behind the song as a
        relay machine holding 320 ms of frames, and both read off the same
        position. Subtracting ``direct_late_s`` again here would read the direct
        machine 240 ms early, which is what this pins.
        """
        relay = handler_for(self._entry(50000.0, 1000.0))
        direct = handler_for(
            self._entry(50000.0, 1000.0, _direct_anchor=True, direct_late_s=0.240),
            buffer_kind="direct")
        relay_position = relay._audible_song_position_ms(buffer_ms=320.0)
        direct_position = direct._audible_song_position_ms(
            buffer_ms=4 * 20.0 + 240.0)      # what the player really measured
        self.assertAlmostEqual(direct_position, relay_position, delta=1.0)
        self.assertNotAlmostEqual(
            direct_position, relay_position - 240.0, delta=1.0)

    def test_a_machine_with_no_song_here_has_no_position(self):
        # No entry (a song that belongs to no cabinet, or a Party Sync leg),
        # no measurement, or an entry the Server never stamped: an unknown
        # position must fall back to the wall-clock path rather than guess.
        self.assertIsNone(handler_for(None)._audible_song_position_ms(buffer_ms=100.0))
        self.assertIsNone(handler_for(self._entry(0.0))._audible_song_position_ms(
            buffer_ms=None))
        stamp_less = {"streamer": SimpleNamespace(), "start_offset": 5.0,
                      "start_offset_received_at": None}
        self.assertIsNone(handler_for(stamp_less)._audible_song_position_ms(
            buffer_ms=100.0))

    def test_a_position_before_the_song_began_is_not_a_position(self):
        # A queue deeper than the song is *behind* its first instant: nothing
        # has been heard yet, and a negative position would aim a note at a
        # place the song does not have.
        handler = handler_for(self._entry(0.0, 0.0))
        self.assertIsNone(handler._audible_song_position_ms(buffer_ms=900.0))

    def test_the_same_arithmetic_reads_both_ends_of_a_note(self):
        """Performer and listener differ only by their own trails.

        This is the whole point: two machines that each know where they are in
        the same song can agree on a beat without sharing a clock.
        """
        performer = handler_for(self._entry(60000.0, 400.0))
        listener = handler_for(self._entry(60000.0, 400.0))
        performer_position = performer._audible_song_position_ms(buffer_ms=160.0)
        listener_position = listener._audible_song_position_ms(buffer_ms=320.0)
        # The listener trails 160 ms further behind the song, so the beat the
        # performer played is that much *later* in the listener's own music.
        self.assertAlmostEqual(performer_position - listener_position, 160.0, delta=20.0)


if __name__ == "__main__":
    unittest.main()
