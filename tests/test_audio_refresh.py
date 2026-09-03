"""Focused simulation for Music Bot environment reverb recovery."""

import os
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


class TestMusicBotEnvironmentRefresh(unittest.TestCase):
    def test_force_rebinds_same_reverb_slot_after_driver_loses_send(self):
        from libs.music_bot import MapMusicBot

        bot = MapMusicBot.__new__(MapMusicBot)
        stream_source = object()
        reverb_slot = object()
        bot.stream_source = stream_source
        bot._current_reverb_slot = reverb_slot
        efx = SimpleNamespace(send=mock.Mock())
        bot.game = SimpleNamespace(audio_mngr=SimpleNamespace(efx=efx))
        map_obj = SimpleNamespace(
            get_reverb_at=lambda *_args: SimpleNamespace(reverb=reverb_slot)
        )
        bot._find_gameplay = lambda: SimpleNamespace(
            map=map_obj,
            player=SimpleNamespace(x=1.0, y=2.0, z=3.0),
        )

        bot._sync_map_reverb(force=True)

        efx.send.assert_called_once_with(stream_source, 0, reverb_slot)


if __name__ == "__main__":
    unittest.main()
