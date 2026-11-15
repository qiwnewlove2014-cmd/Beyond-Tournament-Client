"""A captured program reaches Party Sync guests with its two sides intact.

Report (2026-11-15): *a host capturing external audio inside a Party Sync
session -- no song playing -- is heard fine by the guests, but in mono.* That is
the live jam leg (channel 25): the sender shapes the capture into a 10 ms stereo
frame (`mix_live_stereo`) and encodes it with its stereo Opus encoder, so the
question is whether the frame really leaves with different sides.

No device and no socket: the relay thread is the real one, its network send is
captured, and the payload is decoded by a real Opus decoder.
"""

import os
import struct
import sys
import unittest
from collections import deque
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from libs import consts
from libs.music_bot import streaming as stream_mod
from libs.music_bot.streaming import LiveRelayStreamer

LEFT = 12000
RIGHT = -12000


def stereo_frame(samples=480, left=LEFT, right=RIGHT):
    """One 10 ms stereo frame (480 pairs = 1920 bytes)."""
    return struct.pack("<hh", left, right) * samples


def make_relay(bot=None, live_frame=None):
    from pyogg import OpusEncoder
    game = SimpleNamespace(music_live_supported=True, gameplay=None)
    sent = []

    def send(channel, _addr, payload, reliable=True):
        sent.append((channel, bytes(payload)))
        relay.running = False

    game.network = SimpleNamespace(send=send)
    relay = LiveRelayStreamer.__new__(LiveRelayStreamer)
    relay.game = game
    relay.bot = bot
    relay.running = True
    relay.last_send_time = None
    relay.encoder = OpusEncoder()
    relay.encoder.set_application("audio")
    relay.encoder.set_channels(1)
    relay.encoder.set_sampling_frequency(48000)
    relay.encoder_stereo = OpusEncoder()
    relay.encoder_stereo.set_application("audio")
    relay.encoder_stereo.set_channels(2)
    relay.encoder_stereo.set_sampling_frequency(48000)
    relay._pa_epoch = None
    relay._pa_seq = 0
    return relay, sent


def party_bot(live_frames=(), song_frames=()):
    """The Music Bot's own state for a Party Sync host, no song playing."""
    bot = SimpleNamespace(
        broadcast_enabled=False,
        broadcast_to_megaphone=False,
        party_sync_force_upload=True,
        cinema_force_upload=False,
        volume=100,
        duck_multiplier=1.0,
        guitar_pcm_queue=deque(),
        mic_pcm_queue=deque(),
        external_pcm_queue=deque(song_frames),
        external_live_queue=deque(live_frames),
        party_room_mixer=None,
        streamer=None,
    )
    bot._find_gameplay = lambda: None
    return bot


def decode_stereo(payload):
    from pyogg import OpusDecoder
    decoder = OpusDecoder()
    decoder.set_channels(2)
    decoder.set_sampling_frequency(48000)
    pcm = bytes(decoder.decode(bytearray(payload)))
    return struct.unpack("<%dh" % (len(pcm) // 2), pcm)


def side_means(samples):
    left = samples[0::2]
    right = samples[1::2]
    return (sum(left) / float(len(left)), sum(right) / float(len(right)))


class PartyLiveLegStereoTests(unittest.TestCase):
    """What the jam leg actually puts on the wire for a captured program."""

    def test_a_captured_program_keeps_its_two_sides(self):
        relay, sent = make_relay(live_frame=stereo_frame())
        relay.bot = party_bot(live_frames=[stereo_frame()])
        relay.run()

        self.assertEqual(len(sent), 1, "the relay sent no frame at all")
        channel, payload = sent[0]
        self.assertEqual(channel, consts.CHANNEL_MUSICBOT_LIVE)

        left, right = side_means(decode_stereo(payload))
        # A mono upload decodes to two identical sides; the capture asked for a
        # left-only/right-only pair, so the two must differ by most of 12000.
        self.assertGreater(abs(left - right), 6000,
                           "the jam leg folded the capture to mono")

    def test_the_megaphone_toggle_does_not_fold_a_private_party_leg(self):
        # The PA is one channel by design, but the session's private leg is not
        # the PA: with Broadcast to Megaphone left ON the upload still goes to
        # the guests alone, so the capture keeps its sides.
        relay, sent = make_relay()
        bot = party_bot(song_frames=[stereo_frame(samples=960)])
        bot.broadcast_to_megaphone = True
        relay.bot = bot
        relay.run()

        self.assertEqual(len(sent), 1)
        left, right = side_means(decode_stereo(sent[0][1]))
        self.assertGreater(abs(left - right), 6000,
                           "the megaphone toggle folded the session to mono")

    def test_the_frame_carries_two_channels_at_ten_milliseconds(self):
        relay, sent = make_relay()
        relay.bot = party_bot(live_frames=[stereo_frame()])
        relay.run()

        self.assertEqual(len(sent), 1)
        samples = decode_stereo(sent[0][1])
        # 10 ms at 48 kHz, two channels: 480 frames of a pair.
        self.assertEqual(len(samples), 960)


class PartySongLegStereoTests(unittest.TestCase):
    """The same capture with a song playing rides the song leg, not the jam one."""

    def make_song_streamer(self, bot):
        from pyogg import OpusEncoder
        sent = []

        def send(channel, _addr, payload, reliable=True):
            sent.append((channel, bytes(payload)))

        game = SimpleNamespace(music_live_supported=True, gameplay=None,
                               network=SimpleNamespace(send=send))
        streamer = stream_mod.AudioStreamer.__new__(stream_mod.AudioStreamer)
        streamer.game = game
        streamer.bot = bot
        streamer.channels = 2
        streamer.volume = 100
        streamer.encoder = OpusEncoder()
        streamer.encoder.set_application("audio")
        streamer.encoder.set_channels(1)
        streamer.encoder.set_sampling_frequency(48000)
        streamer.encoder_stereo = OpusEncoder()
        streamer.encoder_stereo.set_application("audio")
        streamer.encoder_stereo.set_channels(2)
        streamer.encoder_stereo.set_sampling_frequency(48000)
        streamer._pa_epoch = None
        streamer._pa_seq = 0
        return streamer, sent

    def test_the_capture_keeps_its_sides_beside_a_song(self):
        bot = party_bot(song_frames=[stereo_frame(samples=960)])
        streamer, sent = self.make_song_streamer(bot)
        song = struct.pack("<hh", 3000, -3000) * 960     # 20 ms stereo

        streamer._send_to_network_actual(song)

        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][0], consts.CHANNEL_MUSICBOT)
        left, right = side_means(decode_stereo(sent[0][1]))
        self.assertGreater(abs(left - right), 4000,
                           "the capture was folded into the song's mono mix")


if __name__ == "__main__":
    unittest.main()
