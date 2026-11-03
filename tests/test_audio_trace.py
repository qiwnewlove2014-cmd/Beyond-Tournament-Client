"""The two-machine audio trace: opt-in, bounded, flushable and private.

The trace is a diagnostic file, not gameplay: it must not start merely because
a test imported the module, and a parser must be able to read it line by line
after the client exits. These tests use a temporary explicit path so the real
``client/audio_trace_*.jsonl`` files are never touched.
"""

import json
import os
import tempfile
import time
import unittest
from collections import deque
from types import SimpleNamespace
from unittest import mock

from libs import audio_trace


class AudioTraceTests(unittest.TestCase):
    def tearDown(self):
        audio_trace.close()

    def test_it_does_not_start_without_a_marker_or_environment(self):
        with mock.patch.object(audio_trace, "_marker_path", return_value=None), \
                mock.patch.dict(os.environ, {"BT_AUDIO_TRACE": "0"}):
            self.assertIsNone(audio_trace.start())
            self.assertFalse(audio_trace.enabled())
            self.assertFalse(audio_trace.event("should_not_be_written"))

    def test_an_explicit_path_writes_readable_jsonl(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "trace.jsonl")
            self.assertEqual(audio_trace.start(path=path), path)
            self.assertTrue(audio_trace.enabled())
            self.assertTrue(audio_trace.event(
                "relay_send", seq=7, source="external_live", gap_ms=10.1234,
                role="host",
            ))
            self.assertTrue(audio_trace.event(
                "room_mix_idle", context={"cabinet": "context", "room": 17},
                cabinet="explicit",
            ))
            audio_trace.close()
            with open(path, encoding="utf-8") as handle:
                lines = [json.loads(line) for line in handle if line.strip()]
        self.assertEqual(lines[0]["event"], "trace_start")
        self.assertEqual(lines[1]["event"], "relay_send")
        self.assertEqual(lines[1]["seq"], 7)
        self.assertEqual(lines[1]["gap_ms"], 10.123)
        self.assertEqual(lines[1]["role"], "host")
        self.assertTrue(lines[1]["session"])
        self.assertTrue(lines[0]["host"])
        self.assertEqual(lines[0]["host"], lines[1]["host"])
        idle = lines[2]
        self.assertEqual(idle["event"], "room_mix_idle")
        self.assertEqual(idle["cabinet"], "explicit")
        self.assertEqual(idle["room"], 17)

    def test_a_marker_enables_a_trace_beside_it(self):
        with tempfile.TemporaryDirectory() as folder:
            marker = os.path.join(folder, "audio_trace.on")
            with open(marker, "w", encoding="utf-8") as handle:
                handle.write("on\n")
            with mock.patch.object(audio_trace, "_marker_path",
                                   return_value=marker):
                path = audio_trace.start()
            self.assertIsNotNone(path)
            self.assertEqual(os.path.dirname(path), folder)
            self.assertTrue(os.path.exists(path))
            audio_trace.close()

    def test_party_context_carries_role_and_room(self):
        state = SimpleNamespace(
            role="guest", session_id="session-1", cinema_cabinet="cine-7",
            cinema_room_key=17,
        )
        game = SimpleNamespace(gameplay=SimpleNamespace(party_sync=state))
        self.assertEqual(audio_trace.party_context(game), {
            "party_role": "guest",
            "party_id": "session-1",
            "cabinet": "cine-7",
            "room": 17,
        })

    def test_a_missing_game_is_not_an_error(self):
        self.assertEqual(audio_trace.party_context(None), {})
        self.assertEqual(audio_trace.party_context(SimpleNamespace()), {})

    def test_gap_is_none_for_the_first_sample(self):
        self.assertIsNone(audio_trace.gap_ms(1.0, None))
        self.assertEqual(audio_trace.gap_ms(1.0125, 1.0), 12.5)

    def test_a_room_key_and_a_room_feed_flag_do_not_collide(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "room-key.jsonl")
            audio_trace.start(path=path)
            self.assertTrue(audio_trace.event(
                "party_live_play", context={"room": 7}, room_feed=True))
            audio_trace.close()
            with open(path, encoding="utf-8") as handle:
                lines = [json.loads(line) for line in handle if line.strip()]
        self.assertEqual(lines[1]["room"], 7)
        self.assertTrue(lines[1]["room_feed"])


class TracedPathTests(unittest.TestCase):
    """The trace points themselves run with tracing on, not just disabled."""

    def tearDown(self):
        audio_trace.close()

    @staticmethod
    def _events(path):
        with open(path, encoding="utf-8") as handle:
            return [json.loads(line) for line in handle if line.strip()]

    def test_the_live_relay_writes_send_events(self):
        from libs.music_bot.streaming import LiveRelayStreamer

        class Network:
            def __init__(self):
                self.sent = []

            def send(self, channel, _target, payload, reliable=False):
                self.sent.append((channel, payload))

        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "relay.jsonl")
            audio_trace.start(path=path)
            network = Network()
            game = SimpleNamespace(music_live_supported=True, network=network)
            bot = SimpleNamespace(
                broadcast_enabled=False, broadcast_to_megaphone=False,
                party_sync_force_upload=True, cinema_force_upload=False,
                streamer=None, guitar_pcm_queue=deque(), mic_pcm_queue=deque(),
                external_pcm_queue=deque(),
                external_live_queue=deque([b"\x00" * 1920] * 40),
                volume=100, duck_multiplier=1.0, party_room_mixer=None,
                _find_gameplay=lambda: None,
            )
            relay = LiveRelayStreamer(game, bot)
            relay.start()
            time.sleep(0.12)
            relay.running = False
            relay.join(1.0)
            audio_trace.close()
            events = self._events(path)
        names = [item["event"] for item in events]
        self.assertIn("relay_start", names)
        self.assertIn("relay_send", names)
        sends = [item for item in events if item["event"] == "relay_send"]
        self.assertGreaterEqual(len(sends), 3)
        self.assertEqual(sends[0]["channel"], 25)
        self.assertEqual(sends[0]["source"], "external_live")

    def test_the_party_room_mixer_writes_send_events(self):
        from libs.music_bot.party_room import PartyRoomMixer

        class Network:
            def __init__(self):
                self.sent = []

            def send(self, _channel, _target, payload, reliable=False):
                self.sent.append(payload)

        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "room.jsonl")
            audio_trace.start(path=path)
            state = SimpleNamespace(
                role="host", session_id="party-1",
                cinema_cabinet="cine-7", cinema_room_key=7,
            )
            network = Network()
            game = SimpleNamespace(
                network=network,
                gameplay=SimpleNamespace(party_sync=state),
            )
            mixer = PartyRoomMixer(game, 77, "cine-7")
            mixer.start()
            for _ in range(12):
                mixer.push("host", b"\x01\x00" * 1920, stereo=True)
                time.sleep(0.006)
            deadline = time.monotonic() + 1.0
            while not network.sent and time.monotonic() < deadline:
                time.sleep(0.01)
            mixer.stop()
            mixer.join(1.0)
            audio_trace.close()
            events = self._events(path)
        names = [item["event"] for item in events]
        self.assertIn("room_push", names)
        self.assertIn("room_mix_start", names)
        self.assertIn("room_mix", names)
        self.assertTrue(network.sent)
        mixes = [item for item in events if item["event"] == "room_mix"]
        self.assertTrue(mixes)
        self.assertEqual(mixes[0]["cabinet"], "cine-7")
        self.assertEqual(mixes[0]["room"], 7)

    def test_the_cinema_bank_receives_trace_events(self):
        from libs.audio.cinema.peer import PeerRoomFeed

        class Bank:
            def __init__(self):
                self.refill_holds = 0
                self.awaiting_refill = False
                self.sources = [object()]
                self.queued = 0

            def queue_frame(self, _left, _right):
                self.queued += 1
                return True

            def queued_frames(self):
                return self.queued

            def wanted_for_start(self):
                return 2

            def playing(self):
                return True

            def frame_ms(self):
                return 20.0

            def buffered_ms(self):
                return self.queued * 20

            def reclaim(self):
                return False

            def start_playback(self):
                return True

            def update_output(self):
                return None

        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "bank.jsonl")
            audio_trace.start(path=path)
            state = SimpleNamespace(
                role="listener", session_id="party-1",
                cinema_cabinet="cine-7", cinema_room_key=7,
            )
            game = SimpleNamespace(gameplay=SimpleNamespace(party_sync=state))
            feed = PeerRoomFeed(game, 77, "cine-7", SimpleNamespace(), Bank())
            for _ in range(4):
                feed.push(b"\x01\x00" * 1920, stereo=True)
            audio_trace.close()
            events = self._events(path)
        names = [item["event"] for item in events]
        self.assertIn("room_bank_push", names)
        self.assertIn("room_bank_start", names)
        pushes = [item for item in events if item["event"] == "room_bank_push"]
        self.assertTrue(any(item.get("queued", 0) > 0 for item in pushes))
        self.assertEqual(pushes[0]["cabinet"], "cine-7")
        self.assertEqual(pushes[0]["room"], 7)

    def test_the_room_receiver_writes_ordered_play_events(self):
        from pyogg import OpusEncoder
        from libs.music_bot import party_room

        class Feed:
            cabinet_id = "cine-7"

            def __init__(self):
                self.frames = 0

            def push(self, _pcm, stereo=False, epoch=None):
                self.frames += 1
                return True

        encoder = OpusEncoder()
        encoder.set_application("audio")
        encoder.set_channels(2)
        encoder.set_sampling_frequency(48000)
        payload = bytes(encoder.encode(bytearray(b"\x01\x00" * 1920)))
        self.assertTrue(payload)

        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "receiver.jsonl")
            audio_trace.start(path=path)
            state = SimpleNamespace(
                role="listener", session_id="party-1",
                cinema_cabinet="cine-7", cinema_room_key=7,
            )
            game = SimpleNamespace(gameplay=SimpleNamespace(party_sync=state))
            feed = Feed()
            receiver = party_room.PartyRoomReceiver(game)
            with mock.patch("libs.audio.cinema.peer.route", return_value=feed):
                accepted = False
                for seq in range(8):
                    accepted = receiver.play(77, 1, seq, payload) or accepted
            audio_trace.close()
            events = self._events(path)
        names = [item["event"] for item in events]
        self.assertTrue(accepted)
        self.assertIn("room_rx", names)
        self.assertIn("room_play", names)
        self.assertGreaterEqual(feed.frames, 8)
        play = next(item for item in events if item["event"] == "room_play")
        self.assertEqual(play["cabinet"], "cine-7")
        self.assertEqual(play["room"], 7)


if __name__ == "__main__":
    unittest.main()
