import types
import unittest
from unittest import mock

from libs import event_handeler


class FakeSource:
    pass


class FakeSound:
    def __init__(self):
        self.source = FakeSource()


class FakeEfx:
    def __init__(self):
        self.sent = []

    def send(self, source, slot, effect, filter=None):
        self.sent.append((source, slot, effect))


class FakeAudioMngr:
    def __init__(self):
        self.played = []
        self.efx = FakeEfx()

    def play_unbound(self, path, x, y, z, looping=False, **kwargs):
        self.played.append((path, x, y, z, kwargs))
        return FakeSound()


class FakeReverb:
    def __init__(self):
        self.reverb = "REVERB_EFFECT"


class FakeEntity:
    def __init__(self):
        self.x, self.y, self.z = 10, 20, 5


class FakeMap:
    def __init__(self, reverb_at=None):
        self.entities = {"Alice": FakeEntity()}
        self.reverb_at = reverb_at
        self.looked_up = []

    def get_reverb_at(self, x, y, z):
        self.looked_up.append((x, y, z))
        return self.reverb_at


class FakePlayer:
    def __init__(self):
        self.x, self.y, self.z = 1, 2, 3


def make_handler(reverb_at=None):
    map_obj = FakeMap(reverb_at=reverb_at)
    gameplay = types.SimpleNamespace(map=map_obj, player=FakePlayer())
    game = types.SimpleNamespace(audio_mngr=FakeAudioMngr())
    return types.SimpleNamespace(game=game, gameplay=gameplay)


class TypingSoundTests(unittest.TestCase):
    def test_plays_at_typer_position(self):
        h = make_handler()
        event_handeler.EventHandeler.typing_sound(h, {"name": "Alice"})
        self.assertEqual(len(h.game.audio_mngr.played), 1)
        path, x, y, z, _ = h.game.audio_mngr.played[0]
        self.assertTrue(path.startswith("keyboard/press_key"))
        self.assertEqual((x, y, z), (10, 20, 5))

    def test_reverb_looked_up_at_listener_position(self):
        h = make_handler(reverb_at=FakeReverb())
        event_handeler.EventHandeler.typing_sound(h, {"name": "Alice"})
        self.assertEqual(h.gameplay.map.looked_up, [(1, 2, 3)])
        self.assertEqual(len(h.game.audio_mngr.efx.sent), 1)
        source, slot, effect = h.game.audio_mngr.efx.sent[0]
        self.assertIsInstance(source, FakeSource)
        self.assertEqual(slot, 0)
        self.assertEqual(effect, "REVERB_EFFECT")

    def test_no_reverb_outside_room(self):
        h = make_handler(reverb_at=None)
        event_handeler.EventHandeler.typing_sound(h, {"name": "Alice"})
        self.assertEqual(h.gameplay.map.looked_up, [(1, 2, 3)])
        self.assertEqual(h.game.audio_mngr.efx.sent, [])

    def test_silent_when_typing_sounds_disabled(self):
        h = make_handler()
        with mock.patch(
            "libs.event_handeler.options",
            types.SimpleNamespace(get=lambda key, default=None: False),
        ):
            event_handeler.EventHandeler.typing_sound(h, {"name": "Alice"})
        self.assertEqual(h.game.audio_mngr.played, [])

    def test_silent_when_typer_not_found(self):
        h = make_handler()
        event_handeler.EventHandeler.typing_sound(h, {"name": "Nobody"})
        self.assertEqual(h.game.audio_mngr.played, [])


if __name__ == "__main__":
    unittest.main()
