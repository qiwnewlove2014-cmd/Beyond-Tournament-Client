"""Cross-map Party Sync — the half that plays a session off its own map.

A session follows its members across maps (server libs/party_sync.ts: every leg
is addressed to a player, and `voice_channel` is assigned once per login), but
every receive leg in the client is entity-bound: process_music_data /
process_voice_data look the sender's channel up in `gameplay.voice_channels`,
which is filled from this map's spawn packets and cleared on every map load.
So a member standing somewhere else needs a source of their own —
libs/party_sync_audio.py — and a map load must not end the session.

This file pins that half: the sink table, the entity-always-wins rule, the
map-load rule, the gains, and the invite list that now spans maps.
"""

import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import cyal  # noqa: E402

# Imported up front (this also imports libs.voice_chat): `patch` resolves
# "libs.voice_chat.voice_chat_compression" through the package, so the module
# has to be importable before the first patch is entered.
from libs import party_sync_audio  # noqa: E402,F401
from libs import voice_chat  # noqa: E402,F401


# ── fakes (no OpenAL, no compression threads) ───────────────────────────

class FakeSource:
    """A source with a real queue and OpenAL's disclosure of it.

    `buffers_processed` is the whole point of modelling the queue at all: a
    PLAYING source reports only the buffers it has finished (so a caller has to
    stop it to take its queue back), while a source that is not playing reports
    everything it holds -- the rule the pre-buffer guard in `_play_music_frame`
    is built on, and the one the Party Sync seam carries a queue through.
    """

    def __init__(self, mode=cyal.SourceState.STOPPED, playing=False):
        self.mode = mode
        self.playing = playing
        self.queue = []
        self.gain = 1.0
        self.spatialize = True
        self.relative = False
        self.direct_channels = False
        self.rolloff_factor = 1.0
        self.position = (1.0, 2.0, 3.0)
        self.stopped = False
        self.deleted = False

    @property
    def state(self):
        return cyal.SourceState.PLAYING if self.playing else self.mode

    @property
    def buffers_queued(self):
        return len(self.queue)

    @property
    def buffers_processed(self):
        return 0 if self.playing else len(self.queue)

    def queue_buffers(self, buf):
        self.queue.append(buf)

    def unqueue_buffers(self, max=None):
        taken = list(self.queue)
        self.queue = []
        return taken

    def play(self):
        self.playing = True

    def stop(self):
        self.playing = False
        self.stopped = True

    def delete(self):
        self.deleted = True


class FakeContext:
    def __init__(self):
        self.created = []

    def gen_sources(self, count):
        made = [FakeSource() for _ in range(count)]
        self.created.extend(made)
        return made


class FakeAudio:
    def __init__(self, music_volume=50):
        self.context = FakeContext()
        self.volume_categories = {"music": [music_volume]}


class FakeCompression:
    """Stands in for the Opus worker threads a sink normally owns."""

    def __init__(self, *args, **kwargs):
        self.closed = False

    def close(self):
        self.closed = True


def make_gameplay(state=None, entities=(), own_channel=90, music_volume=50):
    """(gameplay, audio) with this map's channel table and a fake context."""
    audio = FakeAudio(music_volume)
    gameplay = SimpleNamespace(
        game=SimpleNamespace(audio_mngr=audio),
        voice_channels={},
        own_voice_channel=own_channel,
        party_sync=state,
    )
    for channel in entities:
        gameplay.voice_channels[channel] = SimpleNamespace(is_user=False)
    return gameplay, audio


def make_state(host="Alice", host_channel=20, guests=(), role="guest"):
    return SimpleNamespace(
        session_id="alice:1",
        role=role,
        host_name=host,
        host_voice_channel=host_channel,
        guests=[{"name": name, "voice_channel": channel}
                for name, channel in guests],
    )


def _function_source(source, name):
    """The body of one top-level method, up to the next one."""
    marker = f"def {name}("
    start = source.index(marker)
    rest = source[start:]
    return rest[:rest.index("\n    def ", 1)]


# ── the member table ────────────────────────────────────────────────────

class TestMemberTable(unittest.TestCase):
    def test_host_and_guests_become_channels(self):
        from libs.party_sync_audio import state_members

        members = state_members(make_state(guests=[("Bob", 21), ("Carol", 22)]))
        self.assertEqual(set(members), {20, 21, 22})
        self.assertTrue(members[20]["host"])
        self.assertEqual(members[21]["name"], "Bob")
        self.assertFalse(members[21]["host"])

    def test_no_session_is_empty(self):
        from libs.party_sync_audio import state_members

        self.assertEqual(state_members(None), {})
        self.assertEqual(state_members(SimpleNamespace(session_id="")), {})

    def test_garbage_entries_are_skipped(self):
        from libs.party_sync_audio import state_members

        state = SimpleNamespace(
            session_id="s",
            host_name="Alice",
            host_voice_channel="nonsense",
            guests=[None, "Bob", {"name": "Carol"},
                    {"name": "Dave", "voice_channel": 21}],
        )
        members = state_members(state)
        self.assertEqual(set(members), {21})
        self.assertEqual(members[21]["name"], "Dave")


# ── sink lifecycle ──────────────────────────────────────────────────────

class TestSinkLifecycle(unittest.TestCase):
    def _set(self, gameplay):
        from libs.party_sync_audio import sinks_for
        return sinks_for(gameplay)

    def test_off_map_member_gets_a_sink(self):
        state = make_state()
        gameplay, audio = make_gameplay(state)
        with patch("libs.voice_chat.voice_chat_compression", FakeCompression):
            sinks = self._set(gameplay)
            sinks.sync(gameplay, state)
        sink = sinks.sink_for(20)
        self.assertIsNotNone(sink)
        self.assertEqual(sink.name, "Alice")
        self.assertTrue(sink.is_host)
        self.assertEqual(len(audio.context.created), 3)
        # Direct-to-ear, exactly the flags the entity path sets: a member must
        # sound the same whether they are here or on another map.
        for source in (sink.vc_source, sink.music_source):
            self.assertFalse(source.spatialize)
            self.assertTrue(source.relative)
            self.assertTrue(source.direct_channels)
            self.assertEqual(source.position, (0.0, 0.0, 0.0))
        self.assertTrue(sink._party_sync_direct)
        self.assertTrue(sink._party_sync_voice_direct)

    def test_an_entity_on_this_map_wins(self):
        state = make_state()
        gameplay, audio = make_gameplay(state, entities=[20])
        sinks = self._set(gameplay)
        sinks.sync(gameplay, state)
        self.assertIsNone(sinks.sink_for(20))
        self.assertEqual(audio.context.created, [])

    def test_the_local_player_never_gets_a_sink(self):
        state = make_state()
        gameplay, _ = make_gameplay(state, own_channel=20)
        sinks = self._set(gameplay)
        sinks.sync(gameplay, state)
        self.assertEqual(len(sinks), 0)

    def test_own_channel_falls_back_to_the_local_entity(self):
        # No login snapshot: our own entity answers which channel is ours.
        state = make_state()
        gameplay, _ = make_gameplay(state, own_channel=None)
        gameplay.voice_channels[20] = SimpleNamespace(is_user=True)
        from libs.party_sync_audio import _own_channel
        self.assertEqual(_own_channel(gameplay), 20)

    def test_walking_away_hands_over_to_the_sink(self):
        # The host was here (entity), then travelled: no session packet is
        # sent for that, so the per-frame reconcile has to notice.
        state = make_state()
        gameplay, _ = make_gameplay(state, entities=[20])
        with patch("libs.voice_chat.voice_chat_compression", FakeCompression):
            from libs.party_sync_audio import sinks_for
            sinks = sinks_for(gameplay)
            sinks.tick(gameplay)
            self.assertIsNone(sinks.sink_for(20))
            gameplay.voice_channels.clear()
            sinks.tick(gameplay)
        self.assertIsNotNone(sinks.sink_for(20))

    def test_walking_in_hands_back_to_the_entity(self):
        state = make_state()
        gameplay, _ = make_gameplay(state)
        with patch("libs.voice_chat.voice_chat_compression", FakeCompression):
            from libs.party_sync_audio import sinks_for
            sinks = sinks_for(gameplay)
            sinks.tick(gameplay)
            sink = sinks.sink_for(20)
            self.assertIsNotNone(sink)
            gameplay.voice_channels[20] = SimpleNamespace(is_user=False)
            sinks.tick(gameplay)
        self.assertIsNone(sinks.sink_for(20))
        self.assertTrue(sink.music_source is None)   # released
        self.assertTrue(sink.vc_source is None)

    def test_session_end_releases_every_sink(self):
        state = make_state()
        gameplay, _ = make_gameplay(state)
        with patch("libs.voice_chat.voice_chat_compression", FakeCompression):
            from libs.party_sync_audio import sinks_for
            sinks = sinks_for(gameplay)
            sinks.sync(gameplay, state)
            self.assertEqual(len(sinks), 1)
            state.role = None
            state.session_id = ""
            sinks.sync(gameplay, state)
        self.assertEqual(len(sinks), 0)

    def test_a_departed_guest_is_released(self):
        state = make_state(guests=[("Bob", 21)])
        gameplay, _ = make_gameplay(state)
        with patch("libs.voice_chat.voice_chat_compression", FakeCompression):
            from libs.party_sync_audio import sinks_for
            sinks = sinks_for(gameplay)
            sinks.sync(gameplay, state)
            self.assertIsNotNone(sinks.sink_for(21))
            state.guests = []
            sinks.sync(gameplay, state)
        self.assertIsNone(sinks.sink_for(21))
        self.assertIsNotNone(sinks.sink_for(20))

    def test_release_all_frees_sources(self):
        state = make_state(guests=[("Bob", 21)])
        gameplay, _ = make_gameplay(state)
        with patch("libs.voice_chat.voice_chat_compression", FakeCompression):
            from libs.party_sync_audio import sinks_for
            sinks = sinks_for(gameplay)
            sinks.sync(gameplay, state)
            created = list(gameplay.game.audio_mngr.context.created)
            sinks.release_all()
        self.assertEqual(len(sinks), 0)
        for source in created:
            self.assertTrue(source.deleted)


# ── the map-load rule ───────────────────────────────────────────────────

class TestMapLoad(unittest.TestCase):
    def _handler_source(self):
        path = os.path.join(os.path.dirname(__file__), "..", "libs",
                            "event_handeler.py")
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read()

    def test_map_load_no_longer_ends_the_session(self):
        body = _function_source(self._handler_source(), "_apply_parse_map")
        self.assertNotIn("ps.end_session()", body)
        self.assertNotIn("party_sync_force_upload = False", body)
        self.assertIn("_sync_party_sync_direct_audio()", body)

    def test_sinks_survive_a_map_load(self):
        # What a map load does to the client: every entity reference goes.
        state = make_state()
        gameplay, _ = make_gameplay(state)
        with patch("libs.voice_chat.voice_chat_compression", FakeCompression):
            from libs.party_sync_audio import sinks_for
            sinks = sinks_for(gameplay)
            sinks.sync(gameplay, state)
            gameplay.voice_channels.clear()
            sinks.keep_across_map_load(gameplay, state)
            sink = sinks.sink_for(20)
            self.assertIsNotNone(sink)
            self.assertIsNotNone(sink.music_source)
        # A member who is on the new map hands back to their entity.
        gameplay.voice_channels[20] = SimpleNamespace(is_user=False)
        with patch("libs.voice_chat.voice_chat_compression", FakeCompression):
            sinks.keep_across_map_load(gameplay, state)
        self.assertIsNone(sinks.sink_for(20))

    def test_the_hosts_forced_upload_is_not_cleared_by_a_map(self):
        body = _function_source(self._handler_source(), "_apply_parse_map")
        self.assertIn("Party Sync spans maps", body)


# ── gains and routing ───────────────────────────────────────────────────

class TestGains(unittest.TestCase):
    def test_music_follows_the_listeners_own_slider(self):
        state = make_state()
        gameplay, audio = make_gameplay(state, music_volume=30)
        with patch("libs.voice_chat.voice_chat_compression", FakeCompression):
            from libs.party_sync_audio import sinks_for
            sink = sinks_for(gameplay)
            sink.sync(gameplay, state)
            sink = sink.sink_for(20)
            self.assertAlmostEqual(sink.music_source.gain, 0.30, places=3)
            audio.volume_categories["music"][0] = 0
            sink.apply_gains()
            self.assertAlmostEqual(sink.music_source.gain, 0.0, places=3)

    def test_voice_is_flat_at_any_distance(self):
        state = make_state()
        gameplay, _ = make_gameplay(state)
        with patch("libs.voice_chat.voice_chat_compression", FakeCompression):
            from libs.party_sync_audio import sinks_for
            sink = sinks_for(gameplay)
            sink.sync(gameplay, state)
            sink = sink.sink_for(20)
            self.assertEqual(sink.voice_gain(), 1.0)
            self.assertEqual(sink.vc_source.gain, 1.0)


class TestRouting(unittest.TestCase):
    def _handler(self, gameplay, game):
        from libs.event_handeler import EventHandeler
        handler = EventHandeler.__new__(EventHandeler)
        handler.gameplay = gameplay
        handler.game = game
        return handler

    def test_entity_first_then_sink_then_nothing(self):
        state = make_state()
        gameplay, audio = make_gameplay(state)
        game = gameplay.game
        handler = self._handler(gameplay, game)
        # No entity here and no sink yet: the frames are not ours.
        self.assertIsNone(handler._party_audio_receiver(20))
        with patch("libs.voice_chat.voice_chat_compression", FakeCompression):
            from libs.party_sync_audio import sinks_for
            sinks_for(gameplay).sync(gameplay, state)
        self.assertIsNotNone(handler._party_audio_receiver(20))
        entity = SimpleNamespace(is_user=False)
        gameplay.voice_channels[20] = entity
        self.assertIs(handler._party_audio_receiver(20), entity)
        self.assertIsNone(handler._party_audio_receiver(77))

    def test_member_is_local(self):
        from libs.party_sync_audio import member_is_local
        state = make_state()
        gameplay, _ = make_gameplay(state, entities=[20])
        self.assertTrue(member_is_local(gameplay, 20))
        self.assertFalse(member_is_local(gameplay, 21))
        self.assertFalse(member_is_local(None, 20))
        self.assertFalse(member_is_local(gameplay, None))


# ── the invite list that now spans maps ─────────────────────────────────

class TestInviteList(unittest.TestCase):
    def test_entries_carry_where_each_player_is(self):
        from libs.party_sync import parse_player_list_entries

        entries = parse_player_list_entries({"players": [
            {"name": "Carol", "near": True},
            {"name": "Dave", "near": False},
        ]})
        self.assertEqual(entries, [{"name": "Carol", "near": True},
                                   {"name": "Dave", "near": False}])

    def test_a_payload_without_the_field_reads_as_here(self):
        from libs.party_sync import parse_player_list_entries

        self.assertEqual(parse_player_list_entries({"players": [{"name": "Bob"}]}),
                         [{"name": "Bob", "near": True}])

    def test_entries_are_deduplicated_and_capped(self):
        from libs.party_sync import parse_player_list_entries

        self.assertEqual(
            parse_player_list_entries({"players": [
                {"name": "Bob"}, {"name": "Bob"}, "x", {"name": ""},
            ]}),
            [{"name": "Bob", "near": True}],
        )
        self.assertEqual(parse_player_list_entries(None), [])

    def test_name_only_list_still_works(self):
        from libs.party_sync import parse_player_list

        self.assertEqual(parse_player_list({"players": [
            {"name": "Bob", "near": False}, {"name": "Carol", "near": True},
        ]}), ["Bob", "Carol"])


# ── the seam between an entity and a sink ─────────────────────────────
#
# One member, two possible outputs: their entity while they stand on this map,
# their sink while they are on another. Crossing between them must not restart
# the song -- the old leg's queue (and the clock the remote jam notes are
# scheduled against) has to travel, or the new leg starts from an empty source
# and owes its whole pre-buffer again (12 frames of music = 240 ms of nothing).

class FakeBuffer:
    def __init__(self, tag):
        self.tag = tag


def headless_compression(stereo=True):
    """A real MusicCompression with every field and no decoder worker."""
    from libs import voice_chat
    comp = voice_chat.MusicCompression.__new__(voice_chat.MusicCompression)
    comp.game = None
    comp._stereo = bool(stereo)
    comp._format_generation = 1
    comp._pending_format_flush = False
    comp._has_started = False
    comp._last_recv_time = None
    comp._timeline_epoch = None
    comp._timeline_last_received_seq = None
    comp._timeline_first_queued_seq = None
    comp._timeline_anchor_seq = None
    comp._timeline_anchor_time = None
    comp._timeline_pending = []
    return comp


def buffered(count, prefix="frame"):
    return [FakeBuffer(f"{prefix}{i}") for i in range(count)]


class StubMusicCompression:
    """Stands in for the decoder worker a leg creates on demand."""

    def __init__(self, game=None):
        self.game = game
        self.carried = None

    def carry_over(self, other, old_source, new_source):
        self.carried = (other, old_source, new_source)
        return 1


class TestCarryingOneOutputToAnother(unittest.TestCase):
    def _carry(self, old, new, old_comp, new_comp):
        return new_comp.carry_over(old_comp, old, new)

    def test_the_queue_crosses_in_order_and_keeps_playing(self):
        # The whole point: the frames the listener was about to hear are still
        # the next frames they hear, on an output that is already playing.
        old = FakeSource(playing=True)
        old.queue = buffered(3)
        new = FakeSource()
        moved = self._carry(old, new, headless_compression(), headless_compression())
        self.assertEqual(moved, 3)
        self.assertEqual([b.tag for b in new.queue], ["frame0", "frame1", "frame2"])
        self.assertTrue(new.playing)
        self.assertEqual(old.queue, [])
        self.assertFalse(old.playing)

    def test_the_song_keeps_its_place_and_its_clock(self):
        # `_has_started` is what makes the next frame use RESUME_FRAMES instead
        # of PRE_BUFFER_FRAMES, and the anchor is what the remote band's notes
        # are scheduled against. Re-pinning it at a seam puts them a
        # pre-buffer off the beat.
        old_comp = headless_compression()
        old_comp._has_started = True
        old_comp._last_recv_time = 123.0
        old_comp._timeline_epoch = 7
        old_comp._timeline_last_received_seq = 400
        old_comp._timeline_first_queued_seq = 390
        old_comp._timeline_anchor_seq = 200
        old_comp._timeline_anchor_time = 55.5
        old_comp._timeline_pending = [(7, 1)]
        new_comp = headless_compression()
        self._carry(FakeSource(playing=True), FakeSource(), old_comp, new_comp)
        self.assertTrue(new_comp._has_started)
        self.assertEqual(new_comp._last_recv_time, 123.0)
        self.assertEqual(new_comp._timeline_epoch, 7)
        self.assertEqual(new_comp._timeline_last_received_seq, 400)
        self.assertEqual(new_comp._timeline_first_queued_seq, 390)
        self.assertEqual(new_comp._timeline_anchor_seq, 200)
        self.assertEqual(new_comp._timeline_anchor_time, 55.5)
        self.assertEqual(new_comp._timeline_pending, [(7, 1)])   # copied, not shared
        old_comp._timeline_pending.append((9, 2))
        self.assertEqual(new_comp._timeline_pending, [(7, 1)])

    def test_the_new_outputs_idle_silence_is_dropped(self):
        # A fresh entity source can already hold the mono keep-alive buffer;
        # queueing the carried (stereo) frames beside it is an error OpenAL
        # refuses, so the seam starts from the carried frames only.
        old = FakeSource(playing=True)
        old.queue = buffered(2)
        new = FakeSource(mode=cyal.SourceState.INITIAL)
        new.queue = [FakeBuffer("idle-silence")]
        self._carry(old, new, headless_compression(), headless_compression())
        self.assertEqual([b.tag for b in new.queue], ["frame0", "frame1"])

    def test_a_format_switch_is_not_paid_for_with_a_flush(self):
        # `set_output_stereo` would swap the decoder on the worker AND arm the
        # flush that empties the source -- the very queue just carried. The
        # handover swaps the format itself and leaves the flag clear.
        new_comp = headless_compression(stereo=False)
        carried = self._carry(FakeSource(playing=True), FakeSource(),
                              headless_compression(stereo=True), new_comp)
        self.assertEqual(carried, 0)                    # queue was empty
        self.assertTrue(new_comp._stereo)               # format travelled
        self.assertFalse(new_comp._pending_format_flush)
        self.assertGreater(new_comp._format_generation, 1)

    def test_an_empty_output_carries_nothing(self):
        new = FakeSource()
        moved = self._carry(FakeSource(playing=True), new,
                            headless_compression(), headless_compression())
        self.assertEqual(moved, 0)
        self.assertFalse(new.playing)

    def test_one_output_is_never_moved_onto_itself(self):
        src = FakeSource(playing=True)
        src.queue = buffered(2)
        comp = headless_compression()
        self.assertEqual(comp.carry_over(comp, src, src), 0)
        self.assertEqual(len(src.queue), 2)
        self.assertEqual(comp.carry_over(None, src, src), 0)

    def test_missing_sources_are_ignored(self):
        from libs.voice_chat import carry_output, drain_source_queue
        self.assertEqual(carry_output(None, FakeSource()), (0, False))
        self.assertEqual(carry_output(FakeSource(), None), (0, False))
        self.assertEqual(drain_source_queue(None), ([], False))


class TestEntitySinkSeams(unittest.TestCase):
    """`take_over_from_entity` / `hand_back_to_entity` (main thread)."""

    def _leg(self, kind="entity", playing=True, frames=2, stereo=True,
             with_compression=True):
        leg = SimpleNamespace(
            kind=kind,
            music_source=FakeSource(playing=playing),
            vc_source=FakeSource(playing=playing),
            music_compression=(headless_compression(stereo=stereo)
                               if with_compression else None),
        )
        leg.music_source.queue = buffered(frames, prefix=f"{kind}-music")
        leg.vc_source.queue = buffered(frames, prefix=f"{kind}-voice")
        if with_compression:
            leg.music_compression._has_started = True
            leg.music_compression._timeline_anchor_seq = 42
        return leg

    def _sink_with_a_queue(self, sinks, gameplay, channel=20):
        sink = sinks.ensure_sink(gameplay, channel, "Alice", True)
        sink.music_source.queue = buffered(4, prefix="sink-music")
        sink.music_source.play()
        sink.vc_source.queue = buffered(4, prefix="sink-voice")
        sink.music_compression = headless_compression()
        sink.music_compression._has_started = True
        return sink

    def test_a_member_leaving_carries_into_their_sink(self):
        state = make_state()
        gameplay, _ = make_gameplay(state)
        from libs.party_sync_audio import sinks_for, take_over_from_entity
        entity = self._leg("entity")
        with patch("libs.voice_chat.voice_chat_compression", FakeCompression):
            sinks = sinks_for(gameplay)
            sink = sinks.ensure_sink(gameplay, 20, "Alice", True)
            sink.music_compression = headless_compression(stereo=False)
            carried = take_over_from_entity(gameplay, entity, 20, gameplay.game)
            sink = sinks.sink_for(20)
        self.assertTrue(carried)
        self.assertEqual([b.tag for b in sink.music_source.queue],
                         ["entity-music0", "entity-music1"])
        self.assertTrue(sink.music_source.playing)
        self.assertTrue(sink.music_compression._has_started)
        self.assertEqual(sink.music_compression._timeline_anchor_seq, 42)
        self.assertTrue(sink.music_compression._stereo)   # format with the queue
        self.assertEqual([b.tag for b in sink.vc_source.queue],
                         ["entity-voice0", "entity-voice1"])
        self.assertEqual(entity.music_source.queue, [])
        self.assertEqual(entity.vc_source.queue, [])
        self.assertFalse(entity.music_source.playing)   # the old leg let go

    def test_a_member_leaving_creates_the_sink_it_needs(self):
        # The per-frame reconcile runs a frame later, and the entity's sources
        # are destroyed in this same call: the seam cannot wait for it.
        state = make_state()
        gameplay, _ = make_gameplay(state)
        from libs.party_sync_audio import sinks_for, take_over_from_entity
        entity = self._leg("entity", with_compression=False)
        with patch("libs.voice_chat.voice_chat_compression", FakeCompression):
            sinks = sinks_for(gameplay)
            self.assertIsNone(sinks.sink_for(20))
            carried = take_over_from_entity(gameplay, entity, 20, gameplay.game)
            sink = sinks.sink_for(20)
        self.assertIsNotNone(sink)
        self.assertEqual(sink.name, "Alice")
        self.assertTrue(carried)                       # the voice queue crossed
        self.assertEqual([b.tag for b in sink.vc_source.queue],
                         ["entity-voice0", "entity-voice1"])

    def test_a_non_member_is_left_alone(self):
        gameplay, _ = make_gameplay(None)
        from libs.party_sync_audio import sinks_for, take_over_from_entity
        entity = self._leg()
        with patch("libs.voice_chat.voice_chat_compression", FakeCompression):
            sinks = sinks_for(gameplay)
            self.assertFalse(take_over_from_entity(gameplay, entity, 20, gameplay.game))
            self.assertEqual(len(sinks), 0)
            # A session that does not mention the channel is the same answer.
            state = make_state(guests=[("Bob", 21)])
            gameplay.party_sync = state
            gameplay.voice_channels[21] = entity
            gameplay.party_sync = state
            self.assertFalse(take_over_from_entity(gameplay, entity, 22, gameplay.game))

    def test_the_local_player_never_gets_a_sink_for_themselves(self):
        state = make_state()
        gameplay, _ = make_gameplay(state, own_channel=20)
        from libs.party_sync_audio import sinks_for, take_over_from_entity
        entity = self._leg()
        with patch("libs.voice_chat.voice_chat_compression", FakeCompression):
            sinks = sinks_for(gameplay)
            self.assertFalse(take_over_from_entity(gameplay, entity, 20, gameplay.game))
            self.assertEqual(len(sinks), 0)

    def test_a_member_walking_in_hands_back_and_releases_the_sink(self):
        state = make_state()
        gameplay, audio = make_gameplay(state)
        from libs.party_sync_audio import sinks_for, hand_back_to_entity
        with patch("libs.voice_chat.voice_chat_compression", FakeCompression):
            sinks = sinks_for(gameplay)
            sink = self._sink_with_a_queue(sinks, gameplay)
            sink_music, sink_voice = sink.music_source, sink.vc_source
            sink_compression = sink.music_compression
            entity = self._leg("entity", playing=False, frames=0)
            carried = hand_back_to_entity(gameplay, entity, 20, gameplay.game)
        self.assertTrue(carried)
        self.assertIsNone(sinks.sink_for(20))                    # released
        self.assertTrue(sink_music.deleted)
        self.assertTrue(sink_voice.deleted)
        self.assertIsNone(sink.music_source)              # and let go of it
        self.assertIsNone(sink.vc_source)
        self.assertIsNone(sink.music_compression)
        self.assertFalse(sink_compression._running)       # decoder shut down
        self.assertEqual([b.tag for b in entity.music_source.queue],
                         ["sink-music0", "sink-music1", "sink-music2", "sink-music3"])
        self.assertTrue(entity.music_source.playing)
        self.assertTrue(entity.music_compression._has_started)
        self.assertEqual([b.tag for b in entity.vc_source.queue],
                         ["sink-voice0", "sink-voice1", "sink-voice2", "sink-voice3"])

    def test_walking_in_puts_the_entity_on_the_sessions_direct_legs(self):
        # Without this the member stops sounding like the sink they replaced:
        # a guest's host-channel music would play as a 3D boombox at their
        # feet (and the mono format that flag implies would flush the queue the
        # seam had just carried).
        state = make_state(host="Alice", host_channel=20, guests=[("Bob", 21)])
        gameplay, _ = make_gameplay(state)
        from libs.party_sync_audio import hand_back_to_entity, sinks_for
        with patch("libs.voice_chat.voice_chat_compression", FakeCompression):
            sinks = sinks_for(gameplay)
            self._sink_with_a_queue(sinks, gameplay, channel=20)
            host_entity = SimpleNamespace(
                music_source=FakeSource(), vc_source=FakeSource(),
                music_compression=headless_compression(),
            )
            hand_back_to_entity(gameplay, host_entity, 20, gameplay.game)
            self._sink_with_a_queue(sinks, gameplay, channel=21)
            guest_entity = SimpleNamespace(
                music_source=FakeSource(), vc_source=FakeSource(),
                music_compression=headless_compression(),
            )
            hand_back_to_entity(gameplay, guest_entity, 21, gameplay.game)
        self.assertTrue(getattr(host_entity, "_party_sync_direct", False))
        self.assertTrue(getattr(host_entity, "_party_sync_voice_direct", False))
        # A guest's channel is voice-direct only: their music is their own.
        self.assertFalse(getattr(guest_entity, "_party_sync_direct", False))
        self.assertTrue(getattr(guest_entity, "_party_sync_voice_direct", False))

    def test_the_seam_gives_a_bare_leg_a_music_compression(self):
        # The leg that takes over may never have received a frame (a sink is
        # built from the session, not from audio), and the song's queue and
        # clock need somewhere to land.
        state = make_state()
        gameplay, _ = make_gameplay(state)
        from libs.party_sync_audio import hand_back_to_entity, sinks_for
        with patch("libs.voice_chat.voice_chat_compression", FakeCompression), \
                patch("libs.voice_chat.MusicCompression", StubMusicCompression):
            sinks = sinks_for(gameplay)
            sink = sinks.ensure_sink(gameplay, 20, "Alice", True)
            sink.music_source.queue = buffered(2, prefix="sink-music")
            sink.music_source.play()
            sink.music_compression = headless_compression()
            sink_source = sink.music_source
            sink_compression = sink.music_compression
            entity = SimpleNamespace(
                music_source=FakeSource(), vc_source=FakeSource(),
                music_compression=None,
            )
            hand_back_to_entity(gameplay, entity, 20, gameplay.game)
        self.assertIsInstance(entity.music_compression, StubMusicCompression)
        other, old_source, new_source = entity.music_compression.carried
        self.assertIs(other, sink_compression)
        self.assertIs(old_source, sink_source)
        self.assertIs(new_source, entity.music_source)

    def test_a_fresh_entity_without_a_sink_still_goes_on_the_legs(self):
        # The map-load / walk-back-in case, and the reason the legs may not be
        # conditional on a sink: a map load replaces every entity, the roster
        # does not change, and a member left off the legs plays the host's song
        # as a 3D boombox at their body instead of a private feed. Nothing was
        # carried (there was no sink) -- the return value says so -- but the
        # flags are the whole point of being called here.
        state = make_state(host="Alice", host_channel=20, guests=[("Bob", 21)])
        gameplay, _ = make_gameplay(state)
        from libs.party_sync_audio import hand_back_to_entity, sinks_for
        sinks = sinks_for(gameplay)
        host_entity = self._leg("entity")
        guest_entity = self._leg("entity")
        self.assertFalse(hand_back_to_entity(gameplay, host_entity, 20, gameplay.game))
        self.assertFalse(hand_back_to_entity(gameplay, guest_entity, 21, gameplay.game))
        self.assertTrue(host_entity._party_sync_direct)          # a guest's feed
        self.assertTrue(host_entity._party_sync_voice_direct)
        self.assertFalse(getattr(guest_entity, "_party_sync_direct", False))
        self.assertTrue(guest_entity._party_sync_voice_direct)    # team talk
        self.assertEqual(len(sinks), 0)

    def test_the_legs_are_never_written_to_a_non_member(self):
        state = make_state(host="Alice", host_channel=20)
        gameplay, _ = make_gameplay(state)
        from libs.party_sync_audio import hand_back_to_entity, sinks_for
        sinks = sinks_for(gameplay)
        stranger = self._leg("entity")
        self.assertFalse(hand_back_to_entity(gameplay, stranger, 77, gameplay.game))
        self.assertFalse(getattr(stranger, "_party_sync_direct", False))
        self.assertFalse(getattr(stranger, "_party_sync_voice_direct", False))
        # Degenerate arguments are ignored rather than raising.
        self.assertFalse(hand_back_to_entity(gameplay, None, 20, gameplay.game))
        self.assertFalse(hand_back_to_entity(gameplay, stranger, "x", gameplay.game))
        self.assertFalse(hand_back_to_entity(None, stranger, 20, None))
        self.assertEqual(len(sinks), 0)

    def test_the_local_players_own_entity_is_never_on_a_session_leg(self):
        # `_sync_party_sync_direct_audio` skips it, so the spawn path must too:
        # nobody sends a client its own audio, and flagging the local entity
        # would let a later `clear_direct_mode` "restore" sources that were
        # never anything else.
        state = make_state(host="Alice", host_channel=20, guests=[("Bob", 21)])
        gameplay, _ = make_gameplay(state, own_channel=21)
        from libs.party_sync_audio import hand_back_to_entity, sinks_for
        sinks = sinks_for(gameplay)
        mine = self._leg("entity")
        mine.is_user = True
        self.assertFalse(hand_back_to_entity(gameplay, mine, 21, gameplay.game))
        self.assertFalse(getattr(mine, "_party_sync_direct", False))
        self.assertFalse(getattr(mine, "_party_sync_voice_direct", False))
        host_entity = self._leg("entity")
        hand_back_to_entity(gameplay, host_entity, 20, gameplay.game)
        self.assertTrue(host_entity._party_sync_direct)          # still applies
        self.assertEqual(len(sinks), 0)

    def test_a_voice_queue_crosses_without_a_music_leg(self):
        state = make_state()
        gameplay, _ = make_gameplay(state)
        from libs.party_sync_audio import sinks_for, take_over_from_entity
        entity = self._leg("entity", with_compression=False)
        with patch("libs.voice_chat.voice_chat_compression", FakeCompression):
            sinks = sinks_for(gameplay)
            carried = take_over_from_entity(gameplay, entity, 20, gameplay.game)
            sink = sinks.sink_for(20)
        self.assertTrue(carried)
        self.assertEqual([b.tag for b in sink.vc_source.queue],
                         ["entity-voice0", "entity-voice1"])


class TestAMapLoadPutsTheNewEntitiesBackOnTheLegs(unittest.TestCase):
    """The reported bug, driven through the real spawn path.

    A map load replaces every entity in `voice_channels` and the roster does
    not change, so the legs have to be re-applied per entity as it arrives.
    Nothing else says it again: with the flag lost, the listener's copy of the
    host's song plays as a 3D source standing at the host's body (silent past
    50 tiles), which is read as "the party is silent" while the host's own
    machine plays their music perfectly.
    """

    def _handler(self, state, own_channel=90, local_name=None):
        from libs.event_handeler import EventHandeler
        gameplay, audio = make_gameplay(state, own_channel=own_channel)

        def spawn_entity(name, x=0.0, y=0.0, z=0.0, **kwargs):
            return SimpleNamespace(
                name=name, is_user=(name == local_name), is_vehicle=False,
                player=False,
                object_tracking=False, soundgroup=None,
                music_source=FakeSource(), vc_source=FakeSource(),
                radio_source=FakeSource(), music_compression=None,
            )

        gameplay.map = SimpleNamespace(entities={}, spawn_entity=spawn_entity)
        gameplay.camera = SimpleNamespace(focus_object=None,
                                          set_focus_object=lambda e: None)
        gameplay.automations = SimpleNamespace(clear=lambda: None)
        handler = EventHandeler.__new__(EventHandeler)
        handler.game = SimpleNamespace(
            audio_mngr=audio,
            put=lambda cb: cb(),
            exclude_water=set(),
            automations=gameplay.automations,
        )
        handler.gameplay = gameplay
        handler._party_sync_prompt_menu = None
        return handler, gameplay

    def test_a_fresh_host_entity_lands_on_the_guest_legs(self):
        state = make_state(host="Alice", host_channel=20, guests=[("Bob", 21)])
        handler, gameplay = self._handler(state)
        gameplay.voice_channels.clear()          # the map load just cleared it
        handler._sync_party_sync_direct_audio()  # ...and asked for the legs
        packet = {"name": "Alice", "voice_channel": 20, "player": True,
                  "x": 1.0, "y": 2.0, "z": 0.0}
        handler._apply_spawn_entity(packet)
        entity = gameplay.voice_channels[20]
        self.assertTrue(entity._party_sync_direct)
        self.assertTrue(entity._party_sync_voice_direct)
        # ...and the source holds the private-feed shape, not a boombox's.
        self.assertFalse(entity.music_source.spatialize)
        self.assertTrue(entity.music_source.relative)
        self.assertTrue(entity.music_source.direct_channels)

    def test_a_fresh_guest_entity_gets_team_talk_only(self):
        state = make_state(host="Alice", host_channel=20, guests=[("Bob", 21)])
        handler, gameplay = self._handler(state)
        gameplay.voice_channels.clear()
        handler._apply_spawn_entity({"name": "Bob", "voice_channel": 21,
                                     "player": True})
        entity = gameplay.voice_channels[21]
        self.assertFalse(getattr(entity, "_party_sync_direct", False))
        self.assertTrue(entity._party_sync_voice_direct)

    def test_a_stranger_spawns_untouched(self):
        state = make_state(host="Alice", host_channel=20)
        handler, gameplay = self._handler(state)
        gameplay.voice_channels.clear()
        handler._apply_spawn_entity({"name": "Zed", "voice_channel": 77,
                                     "player": True})
        entity = gameplay.voice_channels[77]
        self.assertFalse(getattr(entity, "_party_sync_direct", False))
        self.assertFalse(getattr(entity, "_party_sync_voice_direct", False))
        self.assertTrue(entity.music_source.spatialize)

    def test_the_local_players_own_spawn_is_left_alone(self):
        state = make_state(host="Alice", host_channel=20, guests=[("Bob", 21)])
        handler, gameplay = self._handler(state, own_channel=21, local_name="Bob")
        gameplay.voice_channels.clear()
        handler._apply_spawn_entity({"name": "Bob", "voice_channel": 21,
                                     "player": True})
        entity = gameplay.voice_channels[21]
        self.assertFalse(getattr(entity, "_party_sync_direct", False))
        self.assertFalse(getattr(entity, "_party_sync_voice_direct", False))
        # The host's entity on this client is still put on the legs.
        handler._apply_spawn_entity({"name": "Alice", "voice_channel": 20,
                                     "player": True})
        self.assertTrue(gameplay.voice_channels[20]._party_sync_direct)

    def test_without_a_session_a_spawn_is_untouched(self):
        handler, gameplay = self._handler(None)
        gameplay.voice_channels.clear()
        handler._apply_spawn_entity({"name": "Zed", "voice_channel": 77,
                                     "player": True})
        entity = gameplay.voice_channels[77]
        self.assertFalse(getattr(entity, "_party_sync_direct", False))
        self.assertFalse(getattr(entity, "_party_sync_voice_direct", False))


class TestTheLegReport(unittest.TestCase):
    """One routine line per change: which output carries the session's audio.

    A silent party has no trace today, and "no frames", "frames playing at the
    member's body" and "a cabinet's room took them" are indistinguishable from
    the listener's chair. The line names the leg, so the next report is a fact.
    """

    def _handler(self, state):
        from libs.event_handeler import EventHandeler
        gameplay, audio = make_gameplay(state)
        handler = EventHandeler.__new__(EventHandeler)
        handler.game = SimpleNamespace(audio_mngr=audio)
        handler.gameplay = gameplay
        return handler

    def _report(self, handler, entity, channel=20, via_sink=False):
        lines = []
        with patch("libs.logger.log", lambda *a, **k: lines.append(a[0] if a else "")):
            handler._party_leg_report(entity, channel, via_sink)
        return lines

    def test_the_healthy_entity_leg_is_named_once(self):
        state = make_state(host="Alice", host_channel=20)
        handler = self._handler(state)
        entity = SimpleNamespace(_party_sync_direct=True)
        first = self._report(handler, entity)
        self.assertEqual(len(first), 1)
        self.assertIn("direct-to-ear", first[0])
        self.assertIn("channel 20", first[0])
        # Same leg again, immediately: silent (this runs per frame).
        self.assertEqual(self._report(handler, entity), [])

    def test_a_member_left_as_a_3d_source_is_called_out(self):
        # The failure that was reported as "the listener hears nothing": the
        # frames arrive, and the listener's copy of the song is a 3D source
        # standing at the host's body instead of a private feed.
        state = make_state(host="Alice", host_channel=20)
        handler = self._handler(state)
        entity = SimpleNamespace(_party_sync_direct=False)
        lines = self._report(handler, entity)
        self.assertEqual(len(lines), 1)
        self.assertIn("3D SOURCE", lines[0])
        self.assertIn("50 tiles", lines[0])

    def test_a_sink_is_named_as_such(self):
        state = make_state(host="Alice", host_channel=20)
        handler = self._handler(state)
        lines = self._report(handler, SimpleNamespace(), via_sink=True)
        self.assertEqual(len(lines), 1)
        self.assertIn("party sink", lines[0])

    def test_a_change_of_leg_is_reported_again(self):
        state = make_state(host="Alice", host_channel=20)
        handler = self._handler(state)
        self._report(handler, SimpleNamespace(_party_sync_direct=True))
        lines = self._report(handler, SimpleNamespace(), via_sink=True)
        self.assertEqual(len(lines), 1)
        self.assertIn("party sink", lines[0])

    def test_a_guest_hearing_another_guest_says_nothing(self):
        # Only a member's own audio is on a session leg, and only the host's
        # channel is a private feed: a guest's plain broadcast is ordinary
        # positional audio and needs no line.
        state = make_state(host="Alice", host_channel=20, guests=[("Bob", 21)])
        handler = self._handler(state)
        self.assertEqual(
            self._report(handler, SimpleNamespace(_party_sync_direct=False), 21),
            [],
        )

    def test_without_a_session_nothing_is_reported(self):
        handler = self._handler(None)
        self.assertEqual(self._report(handler, SimpleNamespace()), [])


class TestSeamWiring(unittest.TestCase):
    """The two hooks are where the queues still exist to be moved."""

    def _source(self):
        path = os.path.join(os.path.dirname(__file__), "..", "libs",
                            "event_handeler.py")
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read()

    def test_removal_carries_before_the_entity_is_destroyed(self):
        body = _function_source(self._source(), "_apply_remove_entity")
        self.assertIn("take_over_from_entity", body)
        # The sink can only be filled while the entity's sources are alive.
        self.assertLess(body.index("take_over_from_entity"),
                        body.index('self.gameplay.map.remove_entity(data["name"])'))

    def test_a_spawn_hands_the_sink_back_after_the_sources_exist(self):
        body = _function_source(self._source(), "_apply_spawn_entity")
        self.assertIn("hand_back_to_entity", body)
        self.assertLess(body.index("entity.player = True"),
                        body.index("hand_back_to_entity"))


# ── the entity path's flat session voice ────────────────────────────────

class TestEntitySessionVoice(unittest.TestCase):
    """A session voice must not fade with distance on either audio path."""

    def test_both_paths_treat_a_session_voice_as_flat(self):
        path = os.path.join(os.path.dirname(__file__), "..", "libs", "objects",
                            "entity.py")
        with open(path, "r", encoding="utf-8") as handle:
            source = handle.read()
        for name in ("move", "loop"):
            body = _function_source(source, name)
            self.assertIn("_party_sync_voice_direct", body, name)
            self.assertIn("gain = 1.0", body, name)
