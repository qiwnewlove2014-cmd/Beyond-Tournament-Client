"""A player never hears their own voice come back from the Server.

The Client plays its own microphone locally -- the PA monitor, or a cabinet
room's speakers -- so a frame the Server sends back on the player's own voice
channel, or a PA frame tagged with their own channel, is that same line one
round trip late. Beside a local Server the two copies land a few milliseconds
apart and fuse, which is why this hid for so long; on a hosted Server the same
bug is an audible slap-back echo of yourself.

The Server is fixed too (``event_handeler.voice_chat`` adds the sender to the
proximity relay's exclude list, as the megaphone and Music Bot relays always
did), but this guard is what makes the rule true for a Client talking to a
Server that predates the fix.
"""

import os
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from libs import consts


class Recorder:
    """A vc_compression that records what it was asked to play."""

    def __init__(self):
        self.played = []

    def recieve(self, *args, **kwargs):
        self.played.append((args, kwargs))


def make_handler(own_channel=90):
    from libs import event_handeler as event_module

    voice = Recorder()
    pa = Recorder()
    gameplay = SimpleNamespace(
        own_voice_channel=own_channel,
        _last_remote_megaphone_voice_ts=0.0,
        # Our own body, and the PA channel the sender-tagged frames come in on.
        voice_channels={own_channel: SimpleNamespace(vc_compression=voice),
                        consts.CHANNEL_MEGAPHONE: SimpleNamespace(vc_compression=pa)},
        megaphone=SimpleNamespace(
            get_megaphone_player_sources=mock.Mock(return_value=[object()]),
            megaphone_channel=mock.Mock(return_value=SimpleNamespace(vc_compression=pa)),
        ),
    )
    handler = event_module.EventHandeler.__new__(event_module.EventHandeler)
    handler.game = SimpleNamespace()
    handler.gameplay = gameplay
    return handler, voice, pa, gameplay


class OwnVoiceIsNeverPlayedTests(unittest.TestCase):
    def test_a_frame_on_our_own_channel_is_dropped(self):
        handler, voice, _pa, _gameplay = make_handler(own_channel=90)
        with mock.patch(
            "libs.event_handeler.options.get", return_value=True
        ):
            handler.process_voice_data(b"\xf8\xff\xfe", 90)
        self.assertEqual(voice.played, [])

    def test_another_players_channel_still_plays(self):
        handler, voice, _pa, gameplay = make_handler(own_channel=90)
        gameplay.voice_channels[91] = SimpleNamespace(vc_compression=voice)
        with mock.patch(
            "libs.event_handeler.options.get", return_value=True
        ):
            handler.process_voice_data(b"\xf8\xff\xfe", 91)
        self.assertEqual(len(voice.played), 1)

    def test_a_client_that_does_not_know_its_own_channel_drops_nothing(self):
        # An older Server may not send the field; guessing would silence a
        # real player, so an unknown own channel means "play what arrived".
        handler, voice, _pa, gameplay = make_handler(own_channel=None)
        gameplay.voice_channels[91] = SimpleNamespace(vc_compression=voice)
        with mock.patch(
            "libs.event_handeler.options.get", return_value=True
        ):
            handler.process_voice_data(b"\xf8\xff\xfe", 91)
        self.assertEqual(len(voice.played), 1)

    def test_our_own_pa_frame_is_dropped(self):
        handler, _voice, pa, gameplay = make_handler(own_channel=90)
        with mock.patch(
            "libs.event_handeler.options.get", return_value=True
        ), mock.patch("libs.audio.cinema.speech.routed", return_value=False):
            # The PA payload's first byte is the sender's voice channel.
            handler.process_voice_data(bytes([90]) + b"OPUS", consts.CHANNEL_MEGAPHONE)
        self.assertEqual(pa.played, [])
        gameplay.megaphone.get_megaphone_player_sources.assert_not_called()

    def test_another_players_pa_frame_still_plays(self):
        handler, _voice, pa, gameplay = make_handler(own_channel=90)
        with mock.patch(
            "libs.event_handeler.options.get", return_value=True
        ), mock.patch("libs.audio.cinema.speech.routed", return_value=False):
            handler.process_voice_data(bytes([91]) + b"OPUS", consts.CHANNEL_MEGAPHONE)
        self.assertEqual(len(pa.played), 1)
        gameplay.megaphone.get_megaphone_player_sources.assert_called_once_with(91)


if __name__ == "__main__":
    unittest.main()
