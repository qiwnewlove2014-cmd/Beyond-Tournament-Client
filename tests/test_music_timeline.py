import queue
import threading
import time
import unittest
from collections import deque
from types import SimpleNamespace

from libs import consts
from libs.event_handeler import EventHandeler
from libs.music_bot import AudioStreamer, _next_audio_deadline
from libs.voice_chat import MusicCompression


class MusicTimelineSchedulerTests(unittest.TestCase):
    def make_scheduler(self):
        scheduler = MusicCompression.__new__(MusicCompression)
        scheduler._timeline_epoch = None
        scheduler._timeline_last_received_seq = None
        scheduler._timeline_first_queued_seq = None
        scheduler._timeline_anchor_seq = None
        scheduler._timeline_anchor_time = None
        scheduler._timeline_pending = []
        return scheduler

    def test_event_waits_until_audible_frame(self):
        scheduler = self.make_scheduler()
        fired = []
        now = time.perf_counter()
        scheduler._timeline_epoch = 77
        scheduler._timeline_anchor_seq = 100
        scheduler._timeline_anchor_time = now
        scheduler._queue_timeline_event(77, 105, lambda: fired.append("note"))

        scheduler._dispatch_timeline_events(now + 0.099)
        self.assertEqual(fired, [])
        scheduler._dispatch_timeline_events(now + 0.101)
        self.assertEqual(fired, ["note"])

    def test_sequence_wrap_is_ordered(self):
        self.assertFalse(MusicCompression._sequence_reached(0xFFFFFFFE, 1))
        self.assertTrue(MusicCompression._sequence_reached(1, 0xFFFFFFFE))

    def test_new_epoch_discards_stale_event(self):
        scheduler = self.make_scheduler()
        fired = []
        scheduler._timeline_pending.append((1, 10, time.perf_counter() + 5, lambda: fired.append(1)))
        scheduler._timeline_epoch = 2
        scheduler._dispatch_timeline_events()
        self.assertEqual(fired, [])
        self.assertEqual(scheduler._timeline_pending, [])

    def test_sender_deadline_does_not_accumulate_scheduler_lateness(self):
        deadline = _next_audio_deadline(None, 100.000)
        self.assertEqual(deadline, 100.000)
        deadline = _next_audio_deadline(deadline, 100.021)
        self.assertAlmostEqual(deadline, 100.020)
        deadline = _next_audio_deadline(deadline, 100.041)
        self.assertAlmostEqual(deadline, 100.040)

    def test_sender_deadline_resets_after_long_stall(self):
        deadline = _next_audio_deadline(100.000, 100.100)
        self.assertEqual(deadline, 100.100)

class MusicTimelinePacketTests(unittest.TestCase):
    def test_sender_delay_line_tracks_local_prebuffer(self):
        streamer = AudioStreamer.__new__(AudioStreamer)
        streamer.bot = SimpleNamespace(broadcast_to_megaphone=False)
        streamer._timeline_delay = deque([b"frame0", b"frame1"])
        streamer._timeline_lock = threading.Lock()
        streamer._timeline_epoch = 123
        streamer._timeline_next_seq = 0
        streamer.network_queue = queue.Queue()

        streamer._route_aligned_network_frame()
        self.assertEqual(streamer.network_queue.get_nowait(), (b"frame0", 123, 0))
        streamer._route_aligned_network_frame(b"frame2")
        self.assertEqual(streamer.network_queue.get_nowait(), (b"frame1", 123, 1))

    def test_receiver_parses_versioned_header(self):
        received = []
        compression = SimpleNamespace(
            recieve_timeline=lambda *args: received.append(args)
        )
        entity = SimpleNamespace(music_source=object(), music_compression=compression)
        handler = EventHandeler.__new__(EventHandeler)
        handler.gameplay = SimpleNamespace(voice_channels={42: entity})
        handler.game = SimpleNamespace()
        packet = (
            bytes([1, 42])
            + (0x11223344).to_bytes(4, "big")
            + (99).to_bytes(4, "big")
            + b"opus"
        )

        handler.process_music_timeline_data(packet)

        self.assertEqual(len(received), 1)
        self.assertEqual(received[0][0], b"opus")
        self.assertEqual(received[0][-2:], (0x11223344, 99))
        self.assertGreater(entity._music_last_recv, 0)

    def test_capable_sender_wraps_opus_with_epoch_and_sequence(self):
        sent = []
        streamer = AudioStreamer.__new__(AudioStreamer)
        streamer.game = SimpleNamespace(
            music_timeline_supported=True,
            network=SimpleNamespace(send=lambda *args, **kwargs: sent.append((args, kwargs))),
        )
        streamer.bot = SimpleNamespace(
            broadcast_enabled=True,
            broadcast_to_megaphone=False,
            duck_multiplier=1.0,
            mic_pcm_queue=None,
            guitar_pcm_queue=None,
        )
        streamer.encoder = SimpleNamespace(encode=lambda _pcm: b"opus")
        streamer.volume = 100
        streamer._timeline_lock = threading.Lock()
        streamer._timeline_last_sent_seq = None

        streamer._send_to_network_actual(b"\x00" * 3840, 0x11223344, 99)

        self.assertEqual(len(sent), 1)
        args, kwargs = sent[0]
        self.assertEqual(args[0], consts.CHANNEL_MUSICBOT_TIMELINE)
        self.assertEqual(args[2][:9], b"\x01\x11\x22\x33\x44\x00\x00\x00\x63")
        self.assertEqual(args[2][9:], b"opus")
        self.assertFalse(kwargs["reliable"])
        self.assertEqual(streamer._timeline_last_sent_seq, 99)

    def test_instrument_without_marker_keeps_legacy_immediate_path(self):
        notes = []
        handler = EventHandeler.__new__(EventHandeler)
        handler.game = SimpleNamespace(
            audio_mngr=SimpleNamespace(
                piano=SimpleNamespace(enqueue_remote_note=notes.append)
            )
        )
        handler.gameplay = SimpleNamespace(voice_channels={})
        packet = {"peer_id": "old-client", "piano_note": "C4"}

        handler.play_piano_note(packet)

        self.assertEqual(notes, [packet])

    def test_timeline_channel_is_distinct_from_voice_and_legacy_music(self):
        self.assertLess(consts.CHANNEL_MUSICBOT_TIMELINE, consts.CHANNEL_MEGAPHONE)
        self.assertNotEqual(consts.CHANNEL_MUSICBOT_TIMELINE, consts.CHANNEL_MUSICBOT)
        self.assertNotEqual(consts.CHANNEL_MUSICBOT_TIMELINE, consts.CHANNEL_JUKEBOX_RELAY)


if __name__ == "__main__":
    unittest.main()
