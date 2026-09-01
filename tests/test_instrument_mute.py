"""Client-side instrument-mute cleanup tests without audio devices or a Server."""

from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from libs.event_handeler import EventHandeler


class InstrumentMuteClientTests(unittest.TestCase):
    def make_handler(self):
        handler = EventHandeler.__new__(EventHandeler)
        handler._instrument_silenced_peers = set()
        piano = SimpleNamespace(remove_peer=Mock())
        drums = SimpleNamespace(remove_peer=Mock())
        handler.game = SimpleNamespace(
            audio_mngr=SimpleNamespace(piano=piano, drums=drums),
            put=lambda callback: callback(),
        )
        return handler, piano, drums

    def test_silence_event_stops_existing_piano_and_drum_audio(self):
        handler, piano, drums = self.make_handler()
        handler.instrument_peer_silenced({"peer_id": "Pianist"})
        self.assertTrue(handler._instrument_peer_is_silenced({"peer_id": "pIaNiSt"}))
        piano.remove_peer.assert_called_once_with("Pianist")
        drums.remove_peer.assert_called_once_with("Pianist")

    def test_queued_callback_is_rechecked_after_mute(self):
        handler, _, _ = self.make_handler()
        callback = Mock()
        data = {"peer_id": "Pianist"}
        handler.instrument_peer_silenced(data)
        handler._run_if_instrument_audible(data, callback)
        callback.assert_not_called()
        handler.instrument_peer_unsilenced(data)
        handler._run_if_instrument_audible(data, callback)
        callback.assert_called_once_with()

    def test_missing_peer_id_is_ignored(self):
        handler, piano, drums = self.make_handler()
        handler.instrument_peer_silenced({})
        self.assertEqual(handler._instrument_silenced_peers, set())
        piano.remove_peer.assert_not_called()
        drums.remove_peer.assert_not_called()


if __name__ == "__main__":
    unittest.main()
