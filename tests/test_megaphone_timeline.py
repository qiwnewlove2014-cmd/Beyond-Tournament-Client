"""The PA's sequenced transport (channel 31): a frame's position, and what it buys.

The PA's listener leg had to be reliable and to hold a 120 ms reserve, because
its legacy payload (sender id + Opus) cannot say whether a frame is MISSING or
merely late. Channel 31 puts version + epoch + frame sequence in front of the
same Opus frame, so a listener conceals a missing one with Opus PLC and pays a
40 ms reserve instead -- the loss table in ``tools/megaphone_latency_sim.py``
is what measured that (0 starvations at 1/3/5% loss, against 0/9/17 for the
same pair with no concealment).

These pin the two ends of that wire format, the concealment itself, and the two
reserves. Nothing here needs a socket: the upload is built by the real sender
and parsed by the real receiver.
"""

import contextlib
import os
import struct
import sys
import time
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from libs import consts
from libs import voice_chat as vc
from libs.voice_chat import MegaphoneJitterBuffer, voice_chat_compression

SENDER = 200          # a voice channel id, distinct from the other tests' keys
FRAME = b"\x01\x00" * 960


def opus_frame_bytes():
    """One real encoded 20 ms frame (PLC needs a decoder with history)."""
    from pyogg import OpusEncoder
    encoder = OpusEncoder()
    encoder.set_channels(1)
    encoder.set_sampling_frequency(48000)
    encoder.set_application("voip")
    return bytes(encoder.encode(bytearray(FRAME)))


def sender_worker(supported=True, channel=consts.CHANNEL_MEGAPHONE):
    """The real upload side, without a network thread."""
    worker = voice_chat_compression.__new__(voice_chat_compression)
    worker.channel = channel
    worker.game = SimpleNamespace(pa_timeline_supported=supported)
    worker._pa_epoch = None
    worker._pa_seq = 0
    return worker


def relayed(upload, voice_channel=7):
    """The bytes a listener receives: the Server inserts the sender's channel."""
    return bytes([upload[0], voice_channel]) + upload[1:]


class PaTimelineUploadTests(unittest.TestCase):
    """What the sending client puts on the wire."""

    def test_legacy_payload_when_the_server_did_not_advertise_the_channel(self):
        worker = sender_worker(supported=False)
        self.assertEqual(worker._outgoing_packet(b"OPUS"),
                         (consts.CHANNEL_MEGAPHONE, b"OPUS"))

    def test_legacy_payload_for_normal_voice(self):
        worker = sender_worker(supported=True, channel=consts.CHANNEL_VOICECHAT)
        self.assertEqual(worker._outgoing_packet(b"OPUS"),
                         (consts.CHANNEL_VOICECHAT, b"OPUS"))

    def test_pa_upload_carries_the_frames_position(self):
        worker = sender_worker()
        channel, payload = worker._outgoing_packet(b"OPUS")
        self.assertEqual(channel, consts.CHANNEL_MEGAPHONE_TIMELINE)
        self.assertEqual(len(payload), consts.PA_TIMELINE_UPLOAD_BYTES + 4)
        self.assertEqual(payload[0], consts.PA_TIMELINE_VERSION)
        epoch = struct.unpack_from(">I", payload, 1)[0]
        self.assertEqual(struct.unpack_from(">I", payload, 5)[0], 0)
        self.assertEqual(payload[consts.PA_TIMELINE_UPLOAD_BYTES:], b"OPUS")

        # The sequence counts frames, and the epoch stays put: a talker's pause
        # is a gap in one stream, not a new stream.
        _, second = worker._outgoing_packet(b"OPUS")
        self.assertEqual(struct.unpack_from(">I", second, 1)[0], epoch)
        self.assertEqual(struct.unpack_from(">I", second, 5)[0], 1)

    def test_sequence_wraps_as_a_uint32(self):
        worker = sender_worker()
        worker._pa_seq = 0xFFFFFFFF
        _, payload = worker._outgoing_packet(b"OPUS")
        self.assertEqual(struct.unpack_from(">I", payload, 5)[0], 0xFFFFFFFF)
        _, payload = worker._outgoing_packet(b"OPUS")
        self.assertEqual(struct.unpack_from(">I", payload, 5)[0], 0)


class PaTimelineWireTests(unittest.TestCase):
    """One end to the other: the sender's bytes, parsed by the real receiver."""

    def make_handler(self, received):
        from libs import event_handeler as event_module
        channel = SimpleNamespace(vc_compression=SimpleNamespace(
            recieve=lambda *args, **kwargs: received.append((args, kwargs))))
        gameplay = SimpleNamespace(
            _last_remote_megaphone_voice_ts=0.0,
            megaphone=SimpleNamespace(
                get_megaphone_player_sources=mock.Mock(return_value=[object()]),
                megaphone_channel=mock.Mock(return_value=channel),
            ),
            voice_channels={consts.CHANNEL_MEGAPHONE: channel},
        )
        handler = event_module.EventHandeler.__new__(event_module.EventHandeler)
        handler.game = SimpleNamespace()
        handler.gameplay = gameplay
        return handler

    def test_the_receiver_reads_what_the_sender_wrote(self):
        worker = sender_worker()
        worker._pa_seq = 41
        _, upload = worker._outgoing_packet(b"OPUS")
        received = []
        handler = self.make_handler(received)
        with mock.patch.object(self.handler_module().options, "get", return_value=True):
            handler.process_megaphone_timeline_data(relayed(upload, voice_channel=7))
        self.assertEqual(len(received), 1)
        args, kwargs = received[0]
        # (opus, sources, radio, channelID, gameplay, sender_id)
        self.assertEqual(args[0], b"OPUS")
        self.assertEqual(args[3], consts.CHANNEL_MEGAPHONE)
        self.assertEqual(args[5], 7)
        self.assertEqual(kwargs["frame_seq"], 41)
        self.assertEqual(kwargs["epoch"], struct.unpack_from(">I", upload, 1)[0])

    @staticmethod
    def handler_module():
        from libs import event_handeler as event_module
        return event_module

    def test_malformed_timeline_packets_are_ignored(self):
        received = []
        handler = self.make_handler(received)
        module = self.handler_module()
        with mock.patch.object(module.options, "get", return_value=True):
            handler.process_megaphone_timeline_data(b"")                    # empty
            handler.process_megaphone_timeline_data(b"\x01" * 9)            # no opus
            bad = bytearray(relayed(b"\x09" + b"\x00" * 12))
            handler.process_megaphone_timeline_data(bytes(bad))             # bad version
        self.assertEqual(received, [])

    def test_legacy_channel_still_routes_the_same_way(self):
        received = []
        handler = self.make_handler(received)
        module = self.handler_module()
        with mock.patch.object(module.options, "get", return_value=True):
            handler.process_voice_data(b"\x07OPUS", consts.CHANNEL_MEGAPHONE)
        self.assertEqual(len(received), 1)
        args, kwargs = received[0]
        self.assertEqual(args[0], b"OPUS")
        self.assertEqual(args[5], 7)
        # No position on the legacy payload: the listener cannot conceal.
        self.assertIsNone(kwargs["frame_seq"])


class PaSequenceGapTests(unittest.TestCase):
    """Which frames the playout is told to conceal, and which it must not."""

    def setUp(self):
        self.worker = voice_chat_compression.__new__(voice_chat_compression)
        self.worker._megaphone_timeline = {}

    def gap(self, epoch, frame_seq):
        return self.worker._sequenced_pa_gap(SENDER, epoch, frame_seq)

    def test_first_frame_of_a_stream_has_nothing_to_conceal(self):
        self.assertEqual(self.gap(5, 0), (True, 0))
        self.assertEqual(self.gap(5, 1), (True, 0))

    def test_a_missing_frame_is_counted(self):
        self.gap(5, 0)                          # the stream's first frame
        self.assertEqual(self.gap(5, 2), (True, 1))     # frame 1 never came
        self.assertEqual(self.gap(5, 3), (True, 0))     # in order again
        self.assertEqual(self.gap(5, 10), (True, 6))    # frames 4..9
        self.assertEqual(self.gap(5, 13), (True, 2))    # frames 11, 12

    def test_a_reordered_copy_is_refused(self):
        self.gap(5, 4)
        self.gap(5, 9)                  # a hole was concealed up to frame 9
        # The playout already walked past 6 (a concealment frame), so this copy
        # of frame 6 would replay audio if it were queued.
        self.assertEqual(self.gap(5, 6), (False, 0))

    def test_a_frame_the_playout_already_concealed_is_refused(self):
        self.gap(5, 0)
        state = self.worker._megaphone_timeline[SENDER]
        state["next_seq"] = 5          # the drain concealed frames 1..4
        self.assertEqual(self.gap(5, 3), (False, 0))

    def test_a_jump_too_far_to_be_a_hole_re_bases(self):
        self.gap(5, 0)
        # Nine frames may be concealed; beyond that this is a new stream (or a
        # restarted counter), and concealing a second of audio would be worse
        # than the discontinuity it is hiding.
        accept, missing = self.gap(5, 40)
        self.assertEqual((accept, missing), (True, 0))
        self.assertEqual(self.worker._megaphone_timeline[SENDER]["next_seq"], 41)
        self.assertEqual(self.gap(5, 41), (True, 0))

    def test_a_new_epoch_starts_clean(self):
        self.gap(5, 900)
        self.assertEqual(self.gap(6, 0), (True, 0))
        state = self.worker._megaphone_timeline[SENDER]
        self.assertEqual(state["epoch"], 6)
        self.assertEqual(state["next_seq"], 1)


class PaConcealmentTests(unittest.TestCase):
    """What the playout does with a hole, and what it must never do."""

    def setUp(self):
        vc._megaphone_sequenced_senders.discard(SENDER)
        self.frame = opus_frame_bytes()
        self.decoder = None

    def tearDown(self):
        vc._megaphone_sequenced_senders.discard(SENDER)

    def decoder_with_history(self):
        from pyogg import OpusDecoder
        decoder = OpusDecoder()
        decoder.set_channels(1)
        decoder.set_sampling_frequency(48000)
        # Opus PLC reads the decoder's history; a decoder that has never
        # decoded a frame faults inside the native library, so nothing may
        # conceal before one real frame has been through it.
        decoder.decode(bytearray(self.frame))
        return decoder

    def worker(self, jb, stream):
        worker = voice_chat_compression.__new__(voice_chat_compression)
        worker.game = SimpleNamespace(audio_mngr=SimpleNamespace(
            context=SimpleNamespace(batch=lambda: contextlib.nullcontext()),
            defer_audio=lambda fn: fn()))
        worker._megaphone_decoders = {SENDER: self.decoder_with_history()}
        worker._megaphone_playouts = {SENDER: stream}
        worker._megaphone_timeline = {SENDER: {
            'epoch': 5, 'next_seq': 7, 'concealed': 0,
        }}
        return worker

    def gameplay(self):
        return SimpleNamespace(
            player=SimpleNamespace(dead=False),
            megaphone=SimpleNamespace(player_sources={SENDER: {}}),
        )

    def hole_worker(self, sequenced=True):
        """A playing stream whose queue is empty: the slot due is a hole."""
        jb = MegaphoneJitterBuffer(None)
        jb.sequenced = sequenced
        jb.is_playing = True
        # Recent enough that the buffer is not read as a stream that went
        # silent for 300 ms (which would reset it out of playback).
        jb.last_pop_time = time.time()
        return jb, self.worker(jb, {
            'gameplay': self.gameplay(),
            'sources': [object()],
            'jitter_buffer': jb,
            'last_packet_monotonic': 1e12,
            'sequenced': sequenced,
        })

    def test_an_empty_queue_is_concealed_in_place(self):
        jb = MegaphoneJitterBuffer(None)
        jb.sequenced = True
        jb.is_playing = True
        # A playing buffer whose last pop is not recent is treated as a silent
        # gap over 300 ms and reset out of playback, so the stream must look
        # live: this is the state one tick after the last real frame played.
        jb.last_pop_time = time.time()
        gameplay = self.gameplay()
        worker = self.worker(jb, {
            'gameplay': gameplay,
            'sources': [object()],
            'jitter_buffer': jb,
            'last_packet_monotonic': 1e12,
            'sequenced': True,
        })
        with mock.patch.object(vc, 'queue_and_delay_frame') as output:
            worker._drain_megaphone_playout(1000.0, 10.0)
            self.assertEqual(output.call_count, 1)
            # A real frame (20 ms of PCM), not one of the source's silences.
            self.assertEqual(len(output.call_args.args[3]), MegaphoneJitterBuffer.FRAME_SIZE)
        state = worker._megaphone_timeline[SENDER]
        self.assertEqual(state['next_seq'], 8)      # the slot was consumed
        self.assertEqual(state['concealed'], 1)

    def test_concealment_gives_up_after_a_short_run(self):
        jb = MegaphoneJitterBuffer(None)
        jb.sequenced = True
        jb.is_playing = True
        gameplay = self.gameplay()
        worker = self.worker(jb, {
            'gameplay': gameplay,
            'sources': [object()],
            'jitter_buffer': jb,
            'last_packet_monotonic': 1e12,
            'sequenced': True,
        })
        state = worker._megaphone_timeline[SENDER]
        state['concealed'] = vc.PA_TIMELINE_MAX_CONCEAL
        with mock.patch.object(vc, 'queue_and_delay_frame') as output:
            worker._drain_megaphone_playout(1000.0, 10.0)
            self.assertEqual(output.call_count, 0)
        self.assertEqual(state['next_seq'], 7)      # nothing was consumed

    def test_a_legacy_stream_with_an_empty_queue_stays_silent(self):
        jb = MegaphoneJitterBuffer(None)
        jb.is_playing = True
        gameplay = self.gameplay()
        worker = self.worker(jb, {
            'gameplay': gameplay,
            'sources': [object()],
            'jitter_buffer': jb,
            'last_packet_monotonic': 1e12,
            'sequenced': False,
        })
        with mock.patch.object(vc, 'queue_and_delay_frame') as output:
            worker._drain_megaphone_playout(1000.0, 10.0)
            self.assertEqual(output.call_count, 0)

    def test_a_room_is_fed_the_same_hole_covering_frame(self):
        """A room is a DESTINATION for the PA's frame, not a transport of its own.

        Nothing in this client sends voice on a cinema channel: there is none.
        The listener picks the destination from the frame it already received
        (``process_megaphone_timeline_data`` -> ``_route_megaphone_frame``), so
        the sequenced transport -- and the concealment it buys -- reaches a room
        without a line of room code. This test is what says so, because the
        drain is one place that could quietly undo it by inventing a frame for
        the PA path only.
        """
        jb, worker = self.hole_worker()
        with mock.patch.object(vc.cinema_speech, 'routed', return_value=True), \
                mock.patch.object(vc.cinema_speech, 'feed') as room, \
                mock.patch.object(vc, 'queue_and_delay_frame') as pa:
            worker._drain_megaphone_playout(1000.0, 10.0)
        room.assert_called_once()
        pa.assert_not_called()                      # the room, not the PA
        # (game, gameplay, sender_id, packet): a real 20 ms frame, not silence.
        self.assertEqual(room.call_args[0][2], SENDER)
        self.assertEqual(len(room.call_args[0][3]), MegaphoneJitterBuffer.FRAME_SIZE)
        state = worker._megaphone_timeline[SENDER]
        self.assertEqual(state['concealed'], 1)
        self.assertEqual(state['next_seq'], 8)       # the slot was consumed

    def test_a_legacy_room_stream_invents_nothing(self):
        """A channel-30 frame carries no position, so nothing may be invented.

        The room has no silence reserve to spend either -- it queues REAL frames
        (``speech.START_FRAMES``) rather than padding silence -- so a hole on a
        room leg whose sender never sent a sequence is paid out of the frames
        already queued there, and the playout must feed the room nothing at all.
        """
        jb, worker = self.hole_worker(sequenced=False)
        with mock.patch.object(vc.cinema_speech, 'routed', return_value=True), \
                mock.patch.object(vc.cinema_speech, 'feed') as room, \
                mock.patch.object(vc, 'queue_and_delay_frame') as pa:
            worker._drain_megaphone_playout(1000.0, 10.0)
        room.assert_not_called()
        pa.assert_not_called()

    def test_nothing_is_concealed_without_a_decoded_frame(self):
        jb = MegaphoneJitterBuffer(None)
        jb.sequenced = True
        jb.is_playing = True
        gameplay = self.gameplay()
        worker = self.worker(jb, {
            'gameplay': gameplay,
            'sources': [object()],
            'jitter_buffer': jb,
            'last_packet_monotonic': 1e12,
            'sequenced': True,
        })
        # No stream position known yet: this is the state before any frame has
        # been decoded, where calling into Opus PLC for concealment faults.
        worker._megaphone_timeline[SENDER]['next_seq'] = None
        with mock.patch.object(worker, '_conceal_frame') as conceal, \
                mock.patch.object(vc, 'queue_and_delay_frame'):
            worker._drain_megaphone_playout(1000.0, 10.0)
        self.assertEqual(conceal.call_count, 0)


class PaSenderIdentityTests(unittest.TestCase):
    """The epoch: one per sender session, and never mistaken for the last one."""

    def test_a_sender_keeps_the_epoch_it_stamped(self):
        first = vc.pa_timeline_epoch()
        self.assertEqual(vc.pa_timeline_epoch(first), first)

    def test_two_sessions_do_not_share_an_epoch(self):
        # A listener re-bases its sequence only on a change of epoch, so a
        # repeated one would leave it refusing the new stream's frames as
        # reordered copies. Two streams can start inside the same second (one
        # song replacing another), which is why this is not the wall clock.
        epochs = {vc.pa_timeline_epoch() for _ in range(64)}
        self.assertEqual(len(epochs), 64)


class MusicBotPaUploadTests(unittest.TestCase):
    """The music bot's own senders put the same header on the same wire."""

    @staticmethod
    def sender(supported=True, epoch=None, seq=0):
        from libs.music_bot import streaming
        return streaming, SimpleNamespace(
            game=SimpleNamespace(pa_timeline_supported=supported),
            _pa_epoch=epoch,
            _pa_seq=seq,
        )

    def test_a_music_broadcast_aimed_at_the_pa_carries_its_position(self):
        streaming, sender = self.sender()
        channel, payload = streaming.sequence_pa_frame(
            sender, consts.CHANNEL_MEGAPHONE, b"OPUS")
        self.assertEqual(channel, consts.CHANNEL_MEGAPHONE_TIMELINE)
        self.assertEqual(len(payload), consts.PA_TIMELINE_UPLOAD_BYTES + 4)
        self.assertEqual(payload[0], consts.PA_TIMELINE_VERSION)
        self.assertEqual(struct.unpack_from(">I", payload, 5)[0], 0)
        self.assertEqual(payload[consts.PA_TIMELINE_UPLOAD_BYTES:], b"OPUS")

        # The stream remembers its epoch and counts frames, exactly as the
        # player's own PA upload does.
        epoch = struct.unpack_from(">I", payload, 1)[0]
        _, second = streaming.sequence_pa_frame(
            sender, consts.CHANNEL_MEGAPHONE, b"OPUS")
        self.assertEqual(struct.unpack_from(">I", second, 1)[0], epoch)
        self.assertEqual(struct.unpack_from(">I", second, 5)[0], 1)

    def test_the_music_bot_and_a_player_write_the_same_header(self):
        streaming, sender = self.sender(seq=0)
        _, music = streaming.sequence_pa_frame(
            sender, consts.CHANNEL_MEGAPHONE, b"OPUS")
        player = vc.pa_timeline_upload(b"OPUS", sender._pa_epoch, 0)
        self.assertEqual(music, player)

    def test_a_music_broadcast_on_the_bot_channel_is_left_alone(self):
        streaming, sender = self.sender()
        self.assertEqual(
            streaming.sequence_pa_frame(sender, consts.CHANNEL_MUSICBOT, b"OPUS"),
            (consts.CHANNEL_MUSICBOT, b"OPUS"))

    def test_an_old_server_keeps_the_music_bot_on_the_legacy_channel(self):
        streaming, sender = self.sender(supported=False)
        self.assertEqual(
            streaming.sequence_pa_frame(sender, consts.CHANNEL_MEGAPHONE, b"OPUS"),
            (consts.CHANNEL_MEGAPHONE, b"OPUS"))

    def test_hand_built_senders_can_still_frame_a_pa_upload(self):
        # Tests and diagnostics build these threads without __init__, so the
        # sequence has to live as a class default for them to stream at all.
        from libs.music_bot import streaming
        for cls in (streaming.AudioStreamer, streaming.LiveRelayStreamer):
            self.assertIsNone(cls._pa_epoch, cls)
            self.assertEqual(cls._pa_seq, 0, cls)


class PaReserveTests(unittest.TestCase):
    """Two reserves, chosen by what the sender's transport can say."""

    def setUp(self):
        vc._megaphone_sequenced_senders.discard(SENDER)

    def tearDown(self):
        vc._megaphone_sequenced_senders.discard(SENDER)

    def test_a_legacy_sender_keeps_the_six_frame_reserve(self):
        self.assertEqual(vc._megaphone_margin_frames(SENDER),
                         vc.MEGAPHONE_MARGIN_LEGACY)
        self.assertEqual(vc.MEGAPHONE_MARGIN_LEGACY, 6)

    def test_a_sequenced_sender_pays_the_small_reserve(self):
        worker = voice_chat_compression.__new__(voice_chat_compression)
        worker._mark_megaphone_sequenced(SENDER)
        self.assertEqual(vc._megaphone_margin_frames(SENDER),
                         vc.MEGAPHONE_MARGIN_SEQUENCED)
        self.assertEqual(vc.MEGAPHONE_MARGIN_SEQUENCED, 2)

    def test_marking_also_opens_the_re_bufffer_gate_on_the_buffer(self):
        vc._jitter_buffers[SENDER] = MegaphoneJitterBuffer(None)
        try:
            self.assertFalse(vc._jitter_buffers[SENDER].sequenced)
            worker = voice_chat_compression.__new__(voice_chat_compression)
            worker._mark_megaphone_sequenced(SENDER)
            self.assertTrue(vc._jitter_buffers[SENDER].sequenced)
        finally:
            vc._jitter_buffers.pop(SENDER, None)

    def test_resetting_the_world_clears_the_sequenced_senders(self):
        worker = voice_chat_compression.__new__(voice_chat_compression)
        worker._mark_megaphone_sequenced(SENDER)
        vc.reset_jitter_buffers()
        self.assertEqual(vc._megaphone_margin_frames(SENDER),
                         vc.MEGAPHONE_MARGIN_LEGACY)

    def test_a_sequenced_stream_is_not_held_for_a_re_buffer(self):
        jb = MegaphoneJitterBuffer(None)
        jb.sequenced = True
        # The playout conceals the empty slot in place, so an empty queue must
        # not raise the underrun flag: that flag would hold the next real
        # frames for RESUME_FRAMES ticks and play every one of them late.
        self.assertIsNone(jb.get_packet())
        self.assertFalse(jb._underrun)
        for _ in range(MegaphoneJitterBuffer.PRE_BUFFER_FRAMES + 1):
            jb.add_packet(FRAME)
        self.assertIsNotNone(jb.get_packet())

    def test_a_legacy_stream_keeps_the_re_buffer_gate(self):
        jb = MegaphoneJitterBuffer(None)
        for _ in range(MegaphoneJitterBuffer.PRE_BUFFER_FRAMES):
            jb.add_packet(FRAME)
        while jb.get_packet() is not None:
            pass
        self.assertTrue(jb._underrun)
        jb.add_packet(FRAME)
        self.assertIsNone(jb.get_packet())


if __name__ == "__main__":
    unittest.main()
