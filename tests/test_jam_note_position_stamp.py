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
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from libs.audio_diagnostics import probe as audio_probe
from libs.event_handeler import EventHandeler
from libs.gameplay import Gameplay
from libs.jukebox import JukeboxPlayer, JukeboxRelayReceiver


class _FakeSrc:
    """An OpenAL source name is all ``play()`` does with one."""


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


class TheSongPositionMustBeOnTheEntryTests(unittest.TestCase):
    """The real player must record it, or none of the above ever runs.

    Every check in ``WhereThisMachinesEarsAreTests`` builds the entry by hand,
    and that hand-built entry is exactly how this feature stayed unreachable in
    the shipped game: ``JukeboxPlayer.play()`` put the position the Server named
    into ``play_params`` (for its own replay path) and never onto the *entry*
    -- the one object ``EventHandeler._active_jukebox_buffer_ms`` leaves behind
    for a note's own scheduler. So ``_audible_song_position_ms`` answered
    ``None`` on every live server, no note was ever stamped, and every jam note
    rode the wall clock; the whole machinery was pinned by tests that supplied
    the two keys themselves. These checks drive the real ``play()`` and the real
    reader together, so the fixture cannot drift away from the code again.
    """

    def _game(self):
        return SimpleNamespace(
            audio_mngr=SimpleNamespace(
                context=SimpleNamespace(gen_source=lambda: _FakeSrc()),
                filter=[None], position=None, efx=None,
                volume_categories={"jukebox": [100]}),
            gameplay=None,
        )

    def _play(self, player, **kwargs):
        """Run the REAL ``play()`` with a fake OpenAL under it."""
        captured = {}
        real_call = audio_probe.call

        def fake_call(name, *args, **kw):
            if name in ("jukebox.direct_create", "jukebox.receiver_create"):
                captured["kwargs"] = kw
                return SimpleNamespace(
                    main_thread_audio=False,
                    running=False,
                    start=lambda: None,
                    set_cabinet_volume=lambda value: None,
                    ready_event=SimpleNamespace(is_set=lambda: False),
                )
            if name == "jukebox.gen_source":
                return _FakeSrc()
            if name == "jukebox.thread_start":
                return None
            return real_call(name, *args, **kw)

        with mock.patch.object(audio_probe, "call", side_effect=fake_call):
            player.play("box", 1.0, 2.0, 0.0, "Song", "https://youtu.be/abc",
                        210, playback_id=7, **kwargs)
        return player.players["box"], captured

    def _assert_answers_the_song(self, entry, start_ms, received_ago_ms, buffer_ms):
        """The reader a note's scheduler uses, over the entry the player wrote."""
        position = handler_for(entry)._audible_song_position_ms(
            buffer_ms=float(buffer_ms))
        self.assertIsNotNone(
            position,
            "a playback the player itself started must be able to say where in "
            "the song this machine's ears are")
        self.assertAlmostEqual(position, start_ms + received_ago_ms - buffer_ms,
                               delta=60.0)

    def test_a_direct_play_records_the_songs_position(self):
        player = JukeboxPlayer(self._game())
        arrival = time.monotonic() - 2.5
        entry, captured = self._play(player, transport="direct",
                                     start_offset=42.0, received_at=arrival)
        self.assertEqual(entry["start_offset"], 42.0)
        # The NETWORK arrival instant, not the deferred main-thread time: the
        # song is anchored where its play event reached this machine, and the
        # streamer the same call built is anchored on the same number.
        self.assertAlmostEqual(entry["start_offset_received_at"], arrival,
                               places=6)
        self.assertAlmostEqual(captured["kwargs"]["start_offset_received_at"],
                               arrival, places=6)
        self._assert_answers_the_song(entry, 42000.0, 2500.0, 200.0)

    def test_a_relay_play_records_it_too(self):
        # The relay has no lead-in and no decoder of its own, but the song is
        # the same song: one rule for every transport, or a map that switches
        # transport loses the beat at the switch.
        player = JukeboxPlayer(self._game())
        arrival = time.monotonic() - 1.0
        entry, _ = self._play(player, transport="relay", relay_id=1,
                              stream_epoch=2, start_offset=17.0,
                              received_at=arrival)
        self.assertEqual(entry["start_offset"], 17.0)
        self.assertAlmostEqual(entry["start_offset_received_at"], arrival,
                               places=6)
        self._assert_answers_the_song(entry, 17000.0, 1000.0, 320.0)

    def test_a_play_with_no_arrival_stamp_still_anchors(self):
        # Callers that pass none (a test harness, an older caller) get the
        # moment itself rather than no anchor at all.
        player = JukeboxPlayer(self._game())
        entry, captured = self._play(player, transport="direct",
                                     start_offset=5.0)
        self.assertIsNotNone(entry["start_offset_received_at"])
        self.assertIsNotNone(captured["kwargs"]["start_offset_received_at"])
        self._assert_answers_the_song(entry, 5000.0, 0.0, 20.0)

    def test_a_seamless_re_offer_does_not_move_the_anchor(self):
        # A re-offer of the same song and transport is continuity, not a new
        # playback: re-stamping it would make the song's own position jump by
        # however long the busy frame that delivered the re-offer took.
        player = JukeboxPlayer(self._game())
        arrival = time.monotonic() - 3.0
        first, _ = self._play(player, transport="direct", start_offset=0.0,
                              received_at=arrival)
        anchor = first["start_offset_received_at"]
        second, _ = self._play(player, transport="direct", start_offset=0.0,
                               received_at=time.monotonic())
        self.assertIs(second, first)
        self.assertAlmostEqual(second["start_offset_received_at"], anchor,
                               places=6)

    def test_a_room_listener_knows_where_it_is_in_the_song_as_well(self):
        """A cabinet heard through a room is the same song, so the entry comes
        with the measurement -- a room replaces the pair, not the song."""
        receiver = type("FakeReceiver", (JukeboxRelayReceiver,), {})
        streamer = receiver.__new__(receiver)
        streamer.running = True
        streamer._play_started = True
        streamer.source_l = SimpleNamespace(buffers_queued=2)
        streamer.source_r = SimpleNamespace(buffers_queued=2)
        streamer.cinema = SimpleNamespace(
            sources=[object()],
            buffered_ms=lambda: 320.0,
            extra_latency_ms=lambda: 0.0)
        entry = {"streamer": streamer, "start_offset": 12.0,
                 "start_offset_received_at": time.monotonic() - 0.5}
        handler = EventHandeler.__new__(EventHandeler)
        handler.gameplay = SimpleNamespace(
            jukebox_player=SimpleNamespace(players={"box": entry}))
        buffer_ms = handler._active_jukebox_buffer_ms(cabinet="box")
        self.assertEqual(buffer_ms, 320.0)
        self.assertIs(handler._jam_entry, entry)
        self._assert_answers_the_song(entry, 12000.0, 500.0, 320.0)

    def test_the_performer_stamps_where_their_own_ears_are(self):
        """The other half: the packet carries the position, not just a lag.

        ``_attach_jukebox_sender_lag`` is what the performer's client sends,
        and it can only stamp a note when the entry can answer -- so a dead
        entry is not one broken feature, it is both ends of every note falling
        back to two wall clocks that never had to agree.
        """
        streamer = SimpleNamespace(
            running=True, _direct_anchor=True, direct_late_s=0.0, cinema=None,
            ready_event=SimpleNamespace(is_set=lambda: True),
            spatial_src_l=SimpleNamespace(buffers_queued=4),
            source=SimpleNamespace(buffers_queued=4))
        entry = {"streamer": streamer, "transport": "direct",
                 "start_offset": 30.0,
                 "start_offset_received_at": time.monotonic() - 1.0}
        handler = EventHandeler.__new__(EventHandeler)
        handler.gameplay = SimpleNamespace(
            jukebox_player=SimpleNamespace(players={"box": entry}))
        handler._note_song_cabinet = lambda position, peer=None: "box"
        gameplay = SimpleNamespace(
            player=SimpleNamespace(name="Ann", x=1.0, y=2.0, z=0.0),
            game=SimpleNamespace(network=SimpleNamespace(
                event_handeler=handler)))
        packet = Gameplay._attach_jukebox_sender_lag(gameplay,
                                                     {"server_time": 1})
        self.assertEqual(packet.get("sender_lag_ms"), 80)   # 4 buffers * 20 ms
        self.assertIsNotNone(
            packet.get("sender_position_ms"),
            "a performer who can measure their own trail can say where in the "
            "song they are, and the listener's whole job is easier when they do")
        self.assertAlmostEqual(packet["sender_position_ms"], 30920.0, delta=60.0)


if __name__ == "__main__":
    unittest.main()
