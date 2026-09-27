import types
import unittest

from libs.shields.shield_manager import ShieldManager


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
    def __init__(self, slot="REVERB_EFFECT"):
        self.reverb = slot


class RecoveringReverb:
    """A zone whose pool slot was momentarily exhausted; ensure_slot is the
    retry the camera's listener state makes, and the shield sound makes too."""

    def __init__(self, recovered="RECOVERED_SLOT"):
        self.reverb = None
        self.recovered = recovered
        self.ensure_calls = 0

    def ensure_slot(self):
        self.ensure_calls += 1
        return self.recovered


class ExhaustedReverb(RecoveringReverb):
    def __init__(self):
        super().__init__(recovered=None)


class FakePlayer:
    def __init__(self, position=(1, 2, 3)):
        self.x, self.y, self.z = position


class FakeMap:
    def __init__(self, reverb_at=None):
        self.reverb_at = reverb_at
        self.looked_up = []

    def get_reverb_at(self, x, y, z):
        self.looked_up.append((x, y, z))
        return self.reverb_at


def make_manager(reverb_at=None, focus=None, with_map=True):
    """A ShieldManager whose gameplay answers `listener_object` for real, the
    way the live one does: the shield sound follows the ears, and while
    spectating that is the watched player, not this client's parked body."""
    from libs.gameplay import Gameplay

    map_obj = FakeMap(reverb_at=reverb_at) if with_map else None
    gameplay = types.SimpleNamespace(
        map=map_obj,
        player=FakePlayer(),
        camera=types.SimpleNamespace(focus_object=focus),
    )
    gameplay.listener_object = lambda: Gameplay.listener_object(gameplay)
    gameplay.game = types.SimpleNamespace(audio_mngr=FakeAudioMngr())
    return ShieldManager(gameplay)


class ShieldRoomReverbTests(unittest.TestCase):
    def test_raise_is_direct_and_carries_the_rooms_reverb(self):
        mgr = make_manager(reverb_at=FakeReverb())
        mgr.raise_shield()
        audio = mgr.game.audio_mngr
        self.assertEqual(len(audio.played), 1)
        path, x, y, z, kwargs = audio.played[0]
        self.assertEqual(path, "shields/wood1/raise.ogg")
        self.assertTrue(kwargs.get("direct"))
        self.assertEqual(mgr.gameplay.map.looked_up, [(1, 2, 3)])
        source, slot, effect = audio.efx.sent[0]
        self.assertIsInstance(source, FakeSource)
        self.assertEqual(slot, 0)
        self.assertEqual(effect, "REVERB_EFFECT")

    def test_lower_carries_the_rooms_reverb_too(self):
        mgr = make_manager(reverb_at=FakeReverb())
        mgr.raise_shield()
        mgr.lower_shield()
        audio = mgr.game.audio_mngr
        self.assertEqual([p[0] for p in audio.played],
                         ["shields/wood1/raise.ogg", "shields/wood1/lower.ogg"])
        self.assertEqual(len(audio.efx.sent), 2)

    def test_outside_a_room_the_shield_stays_dry(self):
        mgr = make_manager(reverb_at=None)
        mgr.raise_shield()
        self.assertEqual(mgr.gameplay.map.looked_up, [(1, 2, 3)])
        self.assertEqual(mgr.game.audio_mngr.efx.sent, [])

    def test_a_zone_still_waiting_for_its_slot_is_asked_to_recover(self):
        zone = RecoveringReverb()
        mgr = make_manager(reverb_at=zone)
        mgr.raise_shield()
        self.assertEqual(zone.ensure_calls, 1)
        self.assertEqual(mgr.game.audio_mngr.efx.sent[0][2], "RECOVERED_SLOT")

    def test_an_exhausted_zone_stays_dry_without_breaking(self):
        mgr = make_manager(reverb_at=ExhaustedReverb())
        mgr.raise_shield()
        self.assertEqual(mgr.game.audio_mngr.efx.sent, [])

    def test_the_ears_rule_follows_the_watched_player_while_spectating(self):
        mgr = make_manager(reverb_at=None, focus=FakePlayer((40, 41, 0)))
        mgr.raise_shield()
        self.assertEqual(mgr.gameplay.map.looked_up, [(40, 41, 0)])

    def test_no_map_is_survived(self):
        mgr = make_manager(with_map=False)
        mgr.raise_shield()
        self.assertEqual(len(mgr.game.audio_mngr.played), 1)
        self.assertEqual(mgr.game.audio_mngr.efx.sent, [])

    def test_the_raise_guard_still_holds(self):
        mgr = make_manager(reverb_at=FakeReverb())
        mgr.raise_shield()
        mgr.raise_shield()
        self.assertEqual(len(mgr.game.audio_mngr.played), 1)
        self.assertTrue(mgr.is_raising)
        mgr.lower_shield()
        mgr.lower_shield()
        self.assertEqual(len(mgr.game.audio_mngr.played), 2)
        self.assertFalse(mgr.is_raising)


if __name__ == "__main__":
    unittest.main()
