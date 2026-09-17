"""Following a map's new name without treating it as a map change.

A map *is* its file name on the Server, so renaming one -- now a Builder menu
line rather than stopping the server to rename a file -- is a label moving, not
the room moving. The client is told with `map_renamed`, and that has to stay a
label-only event: `_apply_parse_map` is what stops jukebox playback, resets the
instruments and drops the moving intro, and it decides all of that by comparing
the name on the map packet with the name it already holds. So the handler has to
land on the main thread, change the one attribute, and touch no audio at all.

Pinned here:

1. The name this client holds follows the Server's.
2. Following it touches no audio: no jukebox stop, no map load, no parser call,
   no instrument reset. A rename that tore the room down would be exactly the
   "the song stumbled" report nobody could explain.
3. The payoff, exercised for real: after a rename, the *next* map packet naming
   the new name is still the same map (`same_map=True`, so jukebox playback is
   preserved), while the same packet without the rename is a map change.
4. The change is queued on the main thread -- where the map-name comparison is
   made -- and a malformed packet changes nothing.
5. The player is shown the new name in the feed.

No OpenAL context, device or Server is opened.
"""

import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from libs import buffer as buffer_module
from libs.event_handeler import EventHandeler


class MapRenameEventTests(unittest.TestCase):
    def make_event_handler(self):
        queued = []
        gameplay = SimpleNamespace(
            map_name="Pool",
            player=SimpleNamespace(move=Mock()),
            parser=SimpleNamespace(load=Mock()),
            voice_channels={},
            party_sync=None,
        )
        game = SimpleNamespace(
            put=lambda callback: (queued.append(callback), callback())[1],
            automations=[],
            exclude_water=set(),
            ignore_others_water=False,
            audio_mngr=SimpleNamespace(apply_filter=Mock()),
            network=SimpleNamespace(send=Mock()),
        )
        handler = EventHandeler.__new__(EventHandeler)
        handler.game = game
        handler.gameplay = gameplay
        handler._begin_map_audio_reload = Mock()
        handler._finish_map_audio_reload = Mock()
        handler._stop_jukebox_players_for_map_change = Mock()
        return handler, queued, gameplay

    def test_the_name_this_client_holds_follows_the_server(self):
        handler, _, gameplay = self.make_event_handler()
        handler.map_renamed({"name": "Sunset Pool", "previous": "Pool"})
        self.assertEqual(gameplay.map_name, "Sunset Pool")

    def test_following_the_name_touches_no_audio(self):
        handler, _, gameplay = self.make_event_handler()
        handler.map_renamed({"name": "Sunset Pool", "previous": "Pool"})

        handler._stop_jukebox_players_for_map_change.assert_not_called()
        handler._begin_map_audio_reload.assert_not_called()
        gameplay.parser.load.assert_not_called()
        handler.game.audio_mngr.apply_filter.assert_not_called()

    def test_the_change_is_queued_on_the_main_thread(self):
        """The map-name comparison is made on the main thread, so the name has to
        arrive there in order with the map packets it is compared against."""
        handler = EventHandeler.__new__(EventHandeler)
        pending = []

        class QueuedGame:
            def put(self, callback):
                pending.append(callback)

        handler.game = QueuedGame()
        handler.gameplay = SimpleNamespace(map_name="Pool")

        handler.map_renamed({"name": "Sunset Pool"})

        self.assertEqual(handler.gameplay.map_name, "Pool", "nothing applied off-thread")
        self.assertEqual(len(pending), 1)
        pending.pop()()
        self.assertEqual(handler.gameplay.map_name, "Sunset Pool")

    def test_a_malformed_packet_changes_nothing(self):
        for packet in ({}, None, [], {"name": ""}, {"name": 7}, {"name": None}):
            handler, _, gameplay = self.make_event_handler()
            handler.map_renamed(packet)
            self.assertEqual(gameplay.map_name, "Pool", packet)

    def test_the_player_is_shown_the_new_name_in_the_feed(self):
        handler, _, _ = self.make_event_handler()
        main_buffer = next(b for b in buffer_module.buffers if b.name == "main")
        before = len(main_buffer.items)
        handler.map_renamed({"name": "Sunset Pool", "previous": "Pool"})

        added = main_buffer.items[before:]
        self.assertEqual(len(added), 1)
        self.assertEqual(added[0].text, 'This map is now called "Sunset Pool" (was "Pool").')

    def test_the_next_map_packet_is_still_the_same_map(self):
        """The whole point of saying the name separately: the packet that follows
        a rename -- a builder edit, a Reload Map Data -- keeps the room's audio."""
        handler, _, _ = self.make_event_handler()
        handler.map_renamed({"name": "Sunset Pool", "previous": "Pool"})
        handler._apply_parse_map(
            {"name": "Sunset Pool", "data": "<map/>", "x": 1, "y": 2, "z": 3}
        )
        handler._stop_jukebox_players_for_map_change.assert_called_once_with(
            same_map=True
        )

    def test_without_the_rename_the_same_packet_is_a_map_change(self):
        """The other half of that pair: the name is what makes the difference, so
        a client that was never told really does treat it as a new map."""
        handler, _, _ = self.make_event_handler()
        handler._apply_parse_map(
            {"name": "Sunset Pool", "data": "<map/>", "x": 1, "y": 2, "z": 3}
        )
        handler._stop_jukebox_players_for_map_change.assert_called_once_with(
            same_map=False
        )


if __name__ == "__main__":
    unittest.main()
