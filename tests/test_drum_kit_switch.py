"""Switching the kit of a drum session that is already running.

A kit is which samples a drumset plays, and the Server can change it on a
drumset already standing in the map (the builder's element menu). Every
*listener* hears the new kit on the very next hit, because a drum event carries
the session's kit id with it -- but the performer hears their own hits from
their own client, so without this the person testing the change would keep the
old samples until they stepped away from the kit and back. That is the same
"my ears are the ones listening" trap the cinema switches taught, and this is
the fix for it: `drum_kit` arrives, the running session's kit changes.

Pinned here:

1. A running session switches to the kit the Server named, and the new kit's
   real sample paths are the ones this client now tracks and preloads (the
   performer's own hit reads those, so a kit that was picked but not prepared
   would be heard as a hit that plays nothing).
2. A session that is *not* running is left completely alone: a kit change while
   walking around must not arm drum mode nobody entered, or preload samples for
   a mode the player is not in.
3. A kit this client cannot load changes nothing, rather than leaving the
   session pointing at a kit with no samples.
4. The event is queued onto the main thread (the drum and audio states live
   there) and a malformed packet is ignored instead of crashing the network
   thread.

No OpenAL context, device or Server is opened: the audio side is the real
`DrumAudio` kit table with a stubbed sample cache.
"""

import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from libs.drum_handler import DrumHandler
from libs.drums import DrumAudio
from libs.event_handeler import EventHandeler


def real_pad_paths(drums, kit):
    """Exactly what `start`/`set_kit` track: the kit's real, loadable paths."""
    return tuple(path for _, path, _, _ in drums.pad_defs(kit) if path is not None)


class DrumKitSwitchTests(unittest.TestCase):
    def make_handler(self, active=True):
        requested = []

        class SampleCache:
            def request(self, paths):
                requested.append(tuple(paths))

            def status(self, paths):
                return "ready"

        samples = SampleCache()
        audio = SimpleNamespace(instrument_samples=samples)
        drums = DrumAudio(audio)
        gameplay = SimpleNamespace(game=SimpleNamespace(
            audio_mngr=SimpleNamespace(drums=drums, instrument_samples=samples),
        ))
        handler = DrumHandler(gameplay)
        handler.active = active
        return handler, drums, requested

    def test_a_running_session_switches_to_the_kit_the_server_named(self):
        handler, drums, requested = self.make_handler()
        handler._sample_paths = real_pad_paths(drums, "default")

        self.assertTrue(handler.set_kit("diw"))

        self.assertEqual(drums._active_kit, "diw")
        self.assertEqual(handler._sample_paths, real_pad_paths(drums, "diw"))
        self.assertTrue(
            any(paths == real_pad_paths(drums, "diw") for paths in requested),
            "the new kit's own samples are requested, not the old kit's",
        )

    def test_the_performers_own_hit_reads_the_kit_that_was_picked(self):
        """The local hit path asks `drums.pad_defs(drums._active_kit)`, so the
        kit the Server named is the kit the performer strikes."""
        handler, drums, _ = self.make_handler()
        handler.set_kit("salamander")
        pad_path = drums.pad_defs(drums._active_kit)[0][1]
        self.assertIn(pad_path, handler._sample_paths)

    def test_a_session_that_is_not_running_is_left_alone(self):
        handler, drums, requested = self.make_handler(active=False)
        handler._sample_paths = ()

        self.assertFalse(handler.set_kit("diw"))

        self.assertEqual(drums._active_kit, "default")
        self.assertEqual(handler._sample_paths, ())
        self.assertEqual(requested, [], "nothing is preloaded for a mode nobody entered")

    def test_a_kit_this_client_cannot_load_changes_nothing(self):
        handler, drums, requested = self.make_handler()
        handler._sample_paths = real_pad_paths(drums, "default")

        for kit in (None, "", "orchestral", 7, ["diw"]):
            self.assertFalse(handler.set_kit(kit), kit)

        self.assertEqual(drums._active_kit, "default")
        self.assertEqual(handler._sample_paths, real_pad_paths(drums, "default"))
        self.assertEqual(requested, [], "an impossible kit is not preloaded either")


class DrumKitEventTests(unittest.TestCase):
    def make_event_handler(self):
        handler = EventHandeler.__new__(EventHandeler)
        queued = []
        set_kit = Mock()
        handler.game = SimpleNamespace(put=lambda callback: (queued.append(callback), callback())[1])
        handler.gameplay = SimpleNamespace(drum=SimpleNamespace(set_kit=set_kit))
        return handler, queued, set_kit

    def test_the_switch_is_queued_on_the_main_thread(self):
        handler, queued, set_kit = self.make_event_handler()
        handler.drum_kit({"kit": "diw"})
        self.assertEqual([call.args for call in set_kit.call_args_list], [("diw",)])
        self.assertTrue(queued, "the drum and audio states are touched on the main thread")

    def test_a_malformed_packet_is_ignored(self):
        handler, _, set_kit = self.make_event_handler()
        for data in ({}, None, [], {"kit": None}):
            handler.drum_kit(data)
        self.assertEqual([call.args for call in set_kit.call_args_list],
                         [(None,)] * 4,
                         "a missing kit is handed on as a value the drum handler refuses")


if __name__ == "__main__":
    unittest.main()
