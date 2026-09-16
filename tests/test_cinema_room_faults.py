"""The reported symptoms of a cinema room that comes apart, offline.

Every fault this file injects was reported by a listener or an installer, and
none of them can be reproduced by playing the game: a song has to run for
minutes and the fault has to arrive at the right moment. ``room_sim.py`` is the
machine that reproduces them -- a virtual OpenAL device that behaves like the
real one *including the ways it misleads*, the two transport pump shapes the
game really uses, and a programme tape whose sample values are their own
position in the song.

Each test asserts on what a listener would **hear** -- the programme offset
every speaker played, in order -- never on an internal counter, because a
counter can be right while the room is wrong. That is what every one of these
bugs looked like from the inside.

=========================  ===================================================
"one speaker drifts apart  ``RoomRig.stall`` (a hung sink) and ``.starve``
from the others"           (an underrun)
"the delay moved by        ``.short_frame`` (a torn frame); a pump that
itself"                    reclaims while a speaker waits out its hold
"it speeds up for a        ``.drop_frames`` (the transport sheds frames at the
second"                    live edge)
"cinema mode went quiet    a room where every speaker carries a trim
when I set a delay on
each speaker"
"the song stumbles over    ``.refuse_unqueue`` (a speaker the backend will not
one bar"                   give back)
=========================  ===================================================
"""

import os
import sys
import unittest

import cyal

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from room_sim import (FRAME_40MS, SAMPLERATE, TAPE, RoomRig, VirtualBuffer,
                      VirtualSource, tape_frame)


def samples_of(ms):
    """A trim in milliseconds, in samples at the room's own rate."""
    return int(round(float(ms) * SAMPLERATE / 1000.0))


class TheHarnessModelsTheRealDevice(unittest.TestCase):
    """The fake has to mislead in the same ways the device does.

    A room's failures are timing failures: an internal counter can be right
    while the room is wrong. These pin the disclosures every fix in
    ``bank.py``/``peer.py``/``speech.py`` was written around, so a later edit
    that "cleans up" the fake cannot quietly make the rest of this suite prove
    nothing.
    """

    def test_a_never_played_source_has_processed_nothing(self):
        """Its queue is pending, which is what lets a pre-buffer be built."""
        rig = RoomRig()
        source = rig.sources["front_l"]
        rig.tick()
        rig.tick()
        self.assertEqual(source.state, cyal.SourceState.INITIAL)
        self.assertEqual(source.buffers_queued, 2)
        self.assertEqual(source.buffers_processed, 0)

    def test_a_stopped_source_reports_everything_it_holds_as_finished(self):
        """The one disclosure every reclaim has to be written around."""
        rig = RoomRig().run(30)
        source = rig.sources["front_l"]
        source.stop()
        self.assertEqual(source.buffers_queued, 4)
        self.assertEqual(source.buffers_processed, 4,
                         "a stopped source claims audio it never played")

    def test_a_playing_source_reports_only_what_it_consumed(self):
        rig = RoomRig().run(8)
        source = rig.sources["front_l"]
        source.consume()
        self.assertEqual(source.buffers_processed, len(source.finished))

    def test_an_empty_queue_stops_the_source_by_itself(self):
        rig = RoomRig().run(10)
        source = rig.sources["front_l"]
        source.buffers.clear()
        self.assertIsNone(source.consume())
        self.assertEqual(source.state, cyal.SourceState.STOPPED)
        self.assertEqual(source.underruns, 1)

    def test_a_buffer_carries_its_own_position_in_the_song(self):
        rig = RoomRig().run(12)
        self.assertEqual(rig.heard("front_l")[:4], [0, 960, 1920, 2880])
        self.assertEqual(set(rig.steps("front_l")), {960})
        self.assertEqual(rig.readable, ["front_l", "front_r"])

    def test_a_backend_that_will_not_give_buffers_back_says_so(self):
        rig = RoomRig().run(6)
        rig.refuse_unqueue("front_l")
        self.assertIsNone(rig.sources["front_l"].unqueue_buffers())

    def test_the_programme_tape_reads_back_exactly(self):
        buffer = VirtualBuffer()
        buffer.set_data(tape_frame(0, 4))
        self.assertEqual(buffer.offset, 0)
        self.assertIsNone(VirtualBuffer().offset)
        self.assertTrue(issubclass(VirtualSource, object))


class ARoomOnTheMapPlaysAsOne(unittest.TestCase):
    """The shipped cabinet: no trims, and nothing may separate the speakers."""

    def test_an_untouched_room_plays_every_speaker_identically(self):
        rig = RoomRig().run(400)
        self.assertEqual(set(rig.lag("front_r", "front_l")), {0})
        self.assertEqual(set(rig.steps("front_l")), {960})
        self.assertEqual(set(rig.steps("front_r")), {960})
        self.assertEqual(rig.max_lag(), 0)
        self.assertEqual(rig.silent_slots(), [])
        self.assertEqual({s: rig.sources[s].underruns for s in rig.slots},
                         {"front_l": 0, "front_r": 0})

    def test_the_room_is_never_level_with_the_live_edge(self):
        """A room's own queue depth is what a jam note has to wait out."""
        rig = RoomRig().run(200)
        self.assertGreaterEqual(rig.bank.buffered_ms(), 20)
        self.assertGreater(rig.audible_backlog_ms(), 0)
        self.assertGreaterEqual(rig.min_depth("front_l", 40), 1)

    def test_a_wide_room_keeps_every_speaker_playing_and_the_front_pair_level(self):
        """A theatre room's seven speakers, and where a test can read one.

        The profile hands its front pair a whole channel and mixes the rest,
        so only those carry the tape verbatim (``RoomRig.readable`` measures
        it). The others are asserted for what they are: still playing, still
        fed one frame per frame.
        """
        rig = RoomRig(profile="theatre").run(300)
        self.assertEqual(len(rig.slots), 7)
        self.assertEqual(rig.silent_slots(), [])
        self.assertEqual(rig.max_lag(), 0)
        self.assertEqual(set(rig.steps("front_l")), {960})
        self.assertEqual(set(rig.steps("front_r")), {960})


class ADelayIsHeardAtItsOwnNumber(unittest.TestCase):
    """The trim an installer dialled in, on both transports.

    Both frame sizes the game really uses are asserted: the direct streamer
    decodes 20 ms at a time and hands the room one frame per pump, while the
    server relay hands 40 ms PCM and up to four frames per pump. A trim is held
    in whole frames plus a sample-exact cut (see ``hold_frames``), so the two
    transports must arrive at the very same number of samples.
    """

    def assert_trim(self, delay_ms, expected, *, samples=960, relay=False,
                    ticks=None):
        rig = RoomRig({"front_r": float(delay_ms)}, samples=samples)
        if relay:
            rig.relay_pump(ticks or 40, 4)
        else:
            rig.run(ticks or 400)
        lags = rig.lag("front_r", "front_l")
        self.assertTrue(lags, "the room never played")
        # The first frames are the room's own start; the trim is what it
        # settles at, and the test says so rather than hiding a wobble.
        self.assertEqual(
            set(lags[40:]), {expected},
            f"a {delay_ms:g}ms trim must hold {expected} samples once the room "
            f"is playing; heard {sorted(set(lags))} instead")
        return rig

    def test_a_sub_frame_trim_is_not_rounded_away(self):
        self.assert_trim(5.0, 240)

    def test_a_trim_of_a_frame_and_a_half_is_sample_exact(self):
        self.assert_trim(30.0, 1440)

    def test_a_trim_of_three_frames_is_heard_from_the_first_bar(self):
        rig = self.assert_trim(60.0, 2880)
        self.assertLess(rig.started("front_l"), rig.started("front_r"),
                        "the trimmed speaker must start after the room's clock")

    def test_the_deepest_trim_the_map_can_carry(self):
        self.assert_trim(100.0, 4800)

    def test_the_same_trims_on_the_relay_s_forty_millisecond_frames(self):
        self.assert_trim(60.0, 2880, samples=FRAME_40MS, relay=True, ticks=20)
        self.assert_trim(100.0, 4800, samples=FRAME_40MS, relay=True, ticks=20)

    def test_a_room_where_every_speaker_is_trimmed_still_plays(self):
        rig = RoomRig({"front_l": 40.0, "front_r": 60.0}).run(300)
        self.assertEqual(rig.silent_slots(), [],
                         "a fully trimmed room went silent")
        # Only the 20 ms between the two trims may separate them.
        self.assertEqual(set(rig.lag("front_r", "front_l")[40:]), {960})

    def test_two_speakers_dialled_in_alike_are_heard_level(self):
        rig = RoomRig({"front_l": 60.0, "front_r": 60.0}).run(300)
        self.assertEqual(set(rig.lag("front_r", "front_l")[40:]), {0})

    def test_the_trim_does_not_drift_over_a_long_song(self):
        rig = RoomRig({"front_r": 60.0}).run(3000)
        self.assertEqual(set(rig.lag("front_r", "front_l")[40:]), {2880},
                         "the trim moved on its own late in the song")
        self.assertEqual(set(rig.steps("front_r")), {960})

    def test_the_trimmed_speaker_is_fed_while_it_waits_out_its_hold(self):
        """Starving a waiting speaker is how its trim disappears."""
        rig = RoomRig({"front_r": 100.0}).run(20)
        self.assertGreater(rig.max_depth("front_r"), rig.max_depth("front_l"),
                           "a speaker waiting its trim must sit deeper, not "
                           "be left out of the feeding")
        self.assertEqual(rig.sources["front_l"].state, cyal.SourceState.PLAYING)
        self.assertLess(rig.started("front_l"), rig.started("front_r"))
        self.assertEqual(sorted(set(rig.lag("front_r", "front_l")))[:1], [4800])


class ATrimSurvivesTheTransport(unittest.TestCase):
    """What a pump is allowed to take back, and what it must leave alone."""

    def test_a_pump_that_reclaims_does_not_take_back_the_hold(self):
        """The trap: a stopped source's "finished" count is not a disclosure.

        A trimmed speaker waits out its hold as the frames of its own queue
        (``hold_frames``), and a transport reclaims on every pump
        (``jukebox_relay``, ``AudioStreamer``). OpenAL reports a source that is
        not playing as having processed everything it holds, so a reclaim that
        read a waiting speaker handed its whole hold back into the pool: the
        depth never reached ``wanted_for_start`` (silence), or the speaker
        started on the newest frame it held and the delay came out as the
        wrong number -- or as none at all. Both transport shapes are asserted
        because they are the two ways the game feeds a room.
        """
        # The relay's own pump: reclaim once, then up to four frames.
        relay = RoomRig({"front_r": 100.0}).relay_pump(40, 4)
        self.assertEqual(set(relay.lag("front_r", "front_l")[40:]), {4800})
        self.assertEqual(set(relay.steps("front_l")), {960},
                         "a pump's reclaim cost the room a frame")
        # The direct streamer's: one frame per pump, reclaim every pump.
        direct = RoomRig({"front_r": 60.0}).run(400)
        self.assertEqual(set(direct.lag("front_r", "front_l")[40:]), {2880})

    def test_a_torn_frame_does_not_re_measure_the_trim(self):
        """The frame size is latched, and nothing is spliced to hide the torn one.

        A half-size frame is what a warm-up replay or a resync tail looks like.
        It must not be believed as a new frame size (that is what moves every
        trimmed speaker by the difference, then moves it back), so the trim
        stays within the torn frame's own size and no speaker re-plays or
        skips a frame to cover it.
        """
        rig = RoomRig({"front_r": 60.0}).run(80)
        self.assertEqual(set(rig.lag("front_r", "front_l")[40:]), {2880})
        rig.short_frame()
        rig.run(60)
        self.assertEqual(rig.bank.frame_size_changes, 0)
        lags = set(rig.lag("front_r", "front_l")[80:])
        self.assertTrue(lags and lags <= {2880, 2400}, lags)
        self.assertTrue(set(rig.steps("front_l")) <= {960, 480},
                        "a torn frame was papered over")

    def test_a_burst_of_frames_dropped_at_the_live_edge_keeps_the_room_level(self):
        """The song jumps forward -- both speakers together, trim intact."""
        rig = RoomRig({"front_r": 60.0}).run(80)
        rig.drop_frames(3)
        rig.run(60)
        self.assertEqual(rig.shed_frames, 3)
        self.assertEqual(set(rig.lag("front_r", "front_l")[80:]), {2880})

    def test_the_room_s_own_distance_and_trim_are_reported_beside_it(self):
        rig = RoomRig({"front_r": 100.0}).run(120)
        depth = rig.bank.buffered_ms()
        self.assertGreaterEqual(depth, 20)
        self.assertGreaterEqual(depth, rig.min_depth("front_l", 40) * 20)
        # A trim is latency the queue cannot see, so it travels beside it.
        self.assertEqual(rig.bank.extra_latency_ms(), 100)


class OneSpeakerDriftsApartFromTheOthers(unittest.TestCase):
    """A hung sink, an underrun, a backend that will not give buffers back."""

    def test_a_hung_sink_fills_its_pool_and_stops_the_room(self):
        """What the room can and cannot do about a sink that went deaf.

        Nothing is raised and nothing looks wrong: the speaker is still
        PLAYING and its queue grows instead of draining. The room keeps
        feeding it (it is playing, so ``_feed_slots`` does not leave it out),
        its pool empties, and then every frame is refused *whole* -- so the
        speakers that could be heard run out too. The room cannot heal itself
        from this one (nothing may be taken back out of an OpenAL queue), and
        the one thing left is the watchdog: so what this pins is that the room
        stops producing output, in the units ``JukeboxPlayer.update()``
        watches (the last-output stamp, and frames no longer being accepted).
        """
        rig = RoomRig().run(60)
        rig.stall("front_l")
        rig.run(30)
        self.assertEqual(rig.sources["front_l"].state, cyal.SourceState.PLAYING)
        self.assertEqual(rig.sources["front_l"].buffers_queued,
                         rig.bank.buffers_per_slot,
                         "a hung sink soaks up the whole pool")
        self.assertEqual(set(rig.played["front_l"][61:]), {None},
                         "a hung sink must consume nothing")
        # No further frame is accepted, so the room stops moving through the
        # song: whatever the other speakers are still playing comes out of the
        # history the room already holds (a refill), never out of new audio.
        queued = rig.bank.frames_queued
        furthest = max(rig.heard("front_r"))
        live = rig.offset
        rig.run(30)
        self.assertEqual(rig.bank.frames_queued, queued,
                         "the room kept taking frames a dead sink was holding")
        self.assertEqual(max(rig.heard("front_r")), furthest,
                         "the room moved through the song while a speaker held "
                         "its audio")
        self.assertGreater(rig.offset, live,
                           "the transport has to keep handing frames over: the "
                           "fault is that none of them lands")

    def test_a_hung_sink_is_never_spliced_back_by_the_room_itself(self):
        """What the room *cannot* fix: audio already queued cannot be taken back.

        A resume (or a pause and release) does not re-form a speaker that is
        holding stale frames -- ``realign`` deliberately leaves a *playing*
        speaker alone, because filling it would replay the bar it just played.
        So the room's own recovery stops at the freeze above, and the watchdog
        (which rebuilds the room from scratch) is what owns this fault. This
        test pins the limit, so nobody "fixes" the freeze by splicing a window
        into a speaker that is still holding one.
        """
        rig = RoomRig().run(60)
        rig.stall("front_l")
        rig.run(25)
        rig.resume("front_l")
        rig.bank.set_paused(True)
        rig.bank.set_paused(False)
        rig.run(60)
        # The stalled speaker's own stream: whole frames, never the past
        # inserted after live audio. (A speaker that ran *out* is a different
        # matter -- see the test above; the room may re-form it from history,
        # and while a sink pins the pool that history no longer advances.)
        steps = rig.steps("front_l")
        self.assertTrue(all(0 < step < TAPE / 2 and step % 960 == 0
                            for step in steps),
                        f"front_l was spliced out of the past: {steps[:8]}")

    def test_a_speaker_that_runs_dry_comes_back_on_the_room_s_instant(self):
        """Its trim, and the room's window -- not the live edge.

        A speaker that stopped is left out of the feeding, so its own queue
        cannot grow a backlog it would play at the live edge (which is heard as
        one speaker lagging for the rest of the song). ``realign`` is what puts
        it back on the room's own content instant -- and the frames it comes
        back with have to be the room's, cut to its own trim, or the delay
        comes back as a different number.
        """
        rig = RoomRig({"front_r": 60.0}).run(60)
        self.assertEqual(set(rig.lag("front_r", "front_l")[40:]), {2880})
        rig.starve("front_r")
        rig.run(120)
        self.assertEqual(rig.sources["front_r"].underruns, 1)
        self.assertEqual(rig.silent_slots(), [], "the speaker never came back")
        self.assertEqual(set(rig.lag("front_r", "front_l")[70:]), {2880},
                         "the trim was not restored when the speaker rejoined")
        self.assertTrue(set(rig.steps("front_r")) <= {960, 1920},
                        "it rejoined a queue's worth off the room's instant: "
                        f"{sorted(set(rig.steps('front_r')))}")

    def test_a_speaker_that_stops_does_not_hold_the_whole_room(self):
        """A stopped speaker's empty queue is not the room running low.

        Reading it as one held the room for a refill -- and a held room feeds
        *every* speaker (nothing is playing, so none is left out), which
        rebuilt the stopped one from live frames. It then started ahead of the
        room's own instant and stayed there, which is the reported "the
        cabinets come apart and then try to pull themselves back together".
        """
        rig = RoomRig({"front_r": 60.0}).run(60)
        rig.starve("front_r")
        rig.run(3)
        self.assertEqual(rig.bank.refill_holds, 0,
                         "one speaker's own stumble held the room")
        self.assertEqual(rig.sources["front_l"].state,
                         cyal.SourceState.PLAYING,
                         "the speakers that can be heard kept playing")

    def test_a_backend_that_will_not_give_buffers_back_is_left_alone(self):
        """Nothing is ever *appended* to a queue that still holds audio.

        A queue is a continuous run of the programme: a window cut from the
        room's history and put *after* frames a speaker queued earlier leaves
        a step in that speaker's own stream -- the room's instant, then the
        past, then the live edge -- heard as one speaker stumbling, and it
        stays an instant of its own afterwards. A backend that will not hand
        the buffers back is therefore left exactly as it is.
        """
        rig = RoomRig({"front_r": 60.0}).run(60)
        rig.starve("front_r")
        rig.refuse_unqueue("front_r")
        rig.run(80)
        self.assertTrue(set(rig.steps("front_r")) <= {960, 1920},
                        "a window was spliced after the audio it held: "
                        f"{sorted(set(rig.steps('front_r')))}")
        self.assertLessEqual(rig.max_lag(), 2880,
                             "the trim an installer dialled in was moved")

    def test_a_realign_does_not_move_a_playing_speaker(self):
        """A playing speaker's low depth is audio it has already played."""
        rig = RoomRig({"front_l": 30.0, "front_r": 60.0}).run(80)
        rig.realign()
        rig.run(60)
        self.assertEqual(set(rig.lag("front_r", "front_l")[80:]), {1440})
        self.assertEqual(set(rig.steps("front_l")), {960})
        self.assertEqual(set(rig.steps("front_r")), {960})


class TheRoomHoldsAsOneUnitWhenItRunsLow(unittest.TestCase):
    """One short, level hold instead of one speaker stopping alone."""

    def test_a_gap_in_the_feed_holds_the_room_rather_than_one_speaker(self):
        """The hold is only ever taken on a frame: that is when it is asked."""
        rig = RoomRig().run(80)
        rig.run(3, queue=False)
        rig.tick()
        self.assertTrue(rig.bank.refill_holds >= 1,
                        "the room let one speaker run out on its own")
        self.assertTrue(rig.bank.awaiting_refill)
        rig.run(20)
        self.assertFalse(rig.bank.awaiting_refill)
        self.assertEqual(rig.silent_slots(), [])
        self.assertEqual(set(rig.lag("front_r", "front_l")[40:]), {0})
        self.assertEqual(set(rig.steps("front_l")), {960})

    def test_a_trim_is_not_spent_on_frames_nobody_played(self):
        """The hold must never fire inside the room's own start."""
        rig = RoomRig({"front_r": 60.0}).run(60)
        self.assertEqual(rig.bank.refill_holds, 0)


class ADeadSpeakerDoesNotTakeTheRoom(unittest.TestCase):
    """A device that dies mid-frame, and the buffers it was holding."""

    def test_a_frame_that_cannot_be_queued_is_not_left_on_half_the_room(self):
        rig = RoomRig({"front_r": 60.0}).run(40)
        rig.kill("front_l")
        rig.run(3)
        self.assertGreaterEqual(rig.failure_frames, 1)
        self.assertEqual(rig.failure, "cinema speaker queue failed")
        self.assertNotEqual(rig.sources["front_l"].buffers_queued,
                            rig.sources["front_r"].buffers_queued)

    def test_no_buffer_is_ever_in_two_places(self):
        """Every buffer the room handed out is in one pool or one speaker."""
        rig = RoomRig({"front_r": 60.0}).run(200)
        held = set()
        for source in rig.sources.values():
            for buffer in list(source.buffers) + list(source.finished):
                self.assertNotIn(id(buffer), held,
                                 "one buffer was queued on two speakers")
                held.add(id(buffer))
        in_pools = set()
        for slot, pool in rig.bank._pools.items():
            for buffer in pool:
                self.assertNotIn(id(buffer), held,
                                 f"a queued buffer is also in {slot}'s pool")
                in_pools.add(id(buffer))
        self.assertLessEqual(len(held) + len(in_pools),
                             rig.bank.buffers_per_slot * len(rig.slots))


class TheBufferPoolCoversWhatASpeakerMayHold(unittest.TestCase):
    """A refused frame is audio nobody hears: the pool has to cover the room.

    A trimmed speaker legitimately holds its own hold *plus* the room's queue
    depth, and a pump hands over its frames before reclaiming any of them --
    which is more than a pool of 12 had, so the deepest trims dropped frames
    while the room refilled and the delay came out right anyway (the "the
    delays only start working minutes into the song" report).
    """

    def test_a_deep_trim_never_costs_the_room_a_frame(self):
        for samples, pumps in ((960, 40), (FRAME_40MS, 20)):
            rig = RoomRig({"front_r": 100.0},
                          samples=samples).relay_pump(pumps, 4)
            self.assertEqual(set(rig.steps("front_l")), {samples}, samples)
            self.assertEqual(set(rig.steps("front_r")), {samples}, samples)
            self.assertEqual(rig.silent_slots(), [], samples)

    def test_the_room_keeps_playing_a_long_song_on_both_transports(self):
        for samples, pumps in ((960, 800), (FRAME_40MS, 400)):
            rig = RoomRig({"front_r": 60.0},
                          samples=samples).relay_pump(pumps, 4)
            self.assertEqual(set(rig.steps("front_l")), {samples}, samples)
            self.assertEqual(set(rig.lag("front_r", "front_l")[40:]),
                             {samples_of(60)}, samples)

    def test_the_pool_is_deeper_than_the_queue_the_relay_will_keep(self):
        rig = RoomRig()
        self.assertGreater(rig.bank.buffers_per_slot, 10,
                           "the pool must cover the relay's queue cap and a "
                           "pump's worth of un-reclaimed buffers")
        self.assertGreater(rig.bank.buffers_per_slot, rig.bank.wanted_for_start())


if __name__ == "__main__":
    unittest.main()
