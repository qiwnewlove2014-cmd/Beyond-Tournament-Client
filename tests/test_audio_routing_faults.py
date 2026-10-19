"""One bad handle must not leave a room half-routed.

OpenAL raises out of ``efx.send`` when a source name is no longer valid. Every
routing loop used to call it unguarded, so the first dead handle ended the whole
rebuild: the listener's own group kept the previous room (or stayed dry) while
the manager's ledger already recorded the new one, and nothing asked again --
``Camera`` only acted when the *zone* changed. That is the "some sounds keep the
reverb, some lose it, and it never comes back in the same room" report.

These tests drive the real ``AudioManager`` pool arithmetic, the real
``SoundGroup.apply_effect`` and the real ``Camera.apply_listener_state`` against
a fake EFX that can refuse a chosen source.
"""

import unittest
from types import SimpleNamespace
from unittest import mock

from libs.audio.soundgroup import SoundGroup
from libs.audio_health import probe as audio_health
from libs.camera import Camera


class FakeError(Exception):
    """Stands in for cyal's InvalidNameError -- the class name is what we keep."""


class FakeEfx:
    def __init__(self, refuse=()):
        self.refuse = {id(source) for source in refuse}
        self.sends = []

    def send(self, source, index, slot, filter=None):
        if id(source) in self.refuse:
            raise FakeError("dead handle")
        self.sends.append((source, index, slot))


class FakeBatch:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class FakeContext:
    def batch(self):
        return FakeBatch()


class FakeGroup:
    def __init__(self):
        self.sends = [None, None, None, None]
        self.applied = []

    def apply_effect(self, slot, sendnum=0, filter=None):
        self.sends[sendnum] = slot
        self.applied.append((slot, sendnum))


def bare_audio(*, slots=8, refuse=()):
    """A real AudioManager with a fake device: only the pool arithmetic is real."""
    from libs.audio_manager import AudioManager

    class _TestAudio(AudioManager):
        # The armor permanently INCREFs a wrapper so cyal's crash-prone dealloc
        # can never run; a fake has no such dealloc.
        def _armor_filter(self, filter_obj, site, label="filter"):
            return None

    audio = _TestAudio.__new__(_TestAudio)
    audio._slot_pool = []
    audio._slot_in_use = []
    audio._slot_pool_size = slots
    audio._slot_hold = {}
    audio._effect_leases = {}
    audio._reclaimed_slots = 0
    audio._exhaustion_reported = False
    audio._lease_serial = 0
    audio.efx = FakeEfx(refuse)
    audio.sends = [None, None, None, None]
    audio.soundgroups = set()
    audio.unbound_sources = []
    audio.filter = []
    audio._filter_pool = []
    return audio


def make_group(sounds, *, refuse=()):
    group = SoundGroup.__new__(SoundGroup)
    group.context = FakeContext()
    group.parent = SimpleNamespace(efx=FakeEfx(refuse))
    group.direct = False
    group.filterable = False
    group.sends = [None, None, None, None]
    group.filter = []
    group.unlabeled_sources = list(sounds)
    group.labeled_sources = {}
    return group


class QuietFaults(unittest.TestCase):
    """Keep the singleton's fault lines out of the test run's stdout."""

    def setUp(self):
        audio_health.reset()
        self._sink = audio_health._sink
        audio_health._sink = lambda line: None

    def tearDown(self):
        audio_health._sink = self._sink
        audio_health.reset()


class TheManagerRoutesPastADeadSourceTests(QuietFaults):
    def setUp(self):
        super().setUp()

    def test_a_dead_unbound_source_does_not_leave_the_rest_dry(self):
        dead_source = object()
        audio = bare_audio(refuse=[dead_source])
        audio.unbound_sources = [
            SimpleNamespace(source=dead_source),
            SimpleNamespace(source=object()),
        ]
        group = FakeGroup()
        audio.soundgroups = {group}
        slot = object()

        audio.apply_effect(slot, 0)

        self.assertIs(audio.sends[0], slot)
        self.assertIs(group.sends[0], slot,
                      "the listener's group kept the previous room")
        self.assertEqual(audio_health.total("send_failure"), 1)

    def test_a_group_is_created_with_every_send_at_its_own_index(self):
        audio = bare_audio()
        audio.context = SimpleNamespace()
        calls = []

        class Group:
            def apply_filter(self, f):
                pass

            def apply_effect(self, slot, index, filter=None):
                calls.append((index, slot))

        with mock.patch("libs.audio_manager.SoundGroup", return_value=Group()):
            audio.create_soundgroup()

        self.assertEqual([index for index, _slot in calls], [0, 1, 2, 3],
                         "a send index was aliased to whichever slot came first")


class TheSoundGroupRoutesPastADeadSourceTests(QuietFaults):
    def setUp(self):
        super().setUp()

    def test_the_sounds_after_a_dead_one_still_get_the_room(self):
        dead_source = object()
        alive_source = object()
        group = make_group([SimpleNamespace(source=dead_source),
                            SimpleNamespace(source=alive_source)],
                           refuse=[dead_source])
        slot = object()

        group.apply_effect(slot, 0)

        self.assertIs(group.sends[0], slot)
        self.assertIn((alive_source, 0, slot), group.parent.efx.sends)
        self.assertEqual(audio_health.total("send_failure"), 1)


class Room:
    """A reverb zone as much of one as the listener's routing touches."""

    def __init__(self, slot=None):
        self.reverb = slot
        self.asks = 0

    def ensure_slot(self, force=False):
        self.asks += 1
        return self.reverb


class RoomMap:
    def __init__(self, room=None):
        self.room = room
        self.reverb_list = [room] if room is not None else []

    def get_reverb_at(self, x, y, z):
        return self.room

    def get_ambiences_at(self, x, y, z):
        return []

    def get_musics_at(self, x, y, z):
        return []

    def get_zone_at(self, x, y, z):
        return ""


class RecordingGroup:
    direct = False

    def __init__(self):
        self.sends = [None, None, None, None]
        self.effects = []

    def apply_effect(self, slot, sendnum=0, filter=None):
        self.sends[sendnum] = slot
        self.effects.append((slot, sendnum))


class TheListenerKeepsTryingTests(unittest.TestCase):
    """A room that could not borrow a slot must not be recorded as done."""

    def make(self, map_obj):
        group = RecordingGroup()
        focus = SimpleNamespace(map=map_obj, dead=False, soundgroup=group)
        cam = Camera.__new__(Camera)
        cam.game = SimpleNamespace(gameplay=SimpleNamespace(map=map_obj))
        cam.focus_object = focus
        cam.soundgroup = group
        cam.x = cam.y = cam.z = 0.0
        cam.reverb = None
        cam.currentzone = ""
        return cam, group

    def test_a_room_whose_slot_was_not_ready_is_tried_again(self):
        room = Room(slot=None)
        cam, group = self.make(RoomMap(room))

        cam.apply_listener_state(1.0, 0.0, 0.0)
        self.assertIsNone(cam.reverb, "an unready room was recorded as bound")
        self.assertEqual(group.effects, [])

        room.reverb = "slot"
        cam.apply_listener_state(1.0, 0.0, 0.0)
        self.assertIs(cam.reverb, room)
        self.assertEqual(group.effects, [("slot", 0)])

    def test_the_room_that_is_already_bound_is_not_reapplied_every_step(self):
        room = Room(slot="slot")
        cam, group = self.make(RoomMap(room))

        cam.apply_listener_state(1.0, 0.0, 0.0)
        cam.apply_listener_state(1.0, 0.0, 0.0)
        cam.apply_listener_state(1.0, 0.0, 0.0)

        self.assertEqual(group.effects, [("slot", 0)])

    def test_walking_out_of_the_room_clears_the_send(self):
        room = Room(slot="slot")
        map_obj = RoomMap(room)
        cam, group = self.make(map_obj)
        cam.apply_listener_state(1.0, 0.0, 0.0)

        map_obj.room = None
        cam.apply_listener_state(9.0, 0.0, 0.0)

        self.assertIsNone(cam.reverb)
        self.assertEqual(group.effects[-1], (None, 0))


class TheFrameKeepsGoingTests(QuietFaults):
    def setUp(self):
        super().setUp()

    def test_a_faulting_audio_batch_is_counted_and_does_not_raise(self):
        from libs.game import Game

        game = Game.__new__(Game)
        game.audio_mngr = SimpleNamespace(loop=mock.Mock(side_effect=FakeError("x")))

        game._pump_audio()

        self.assertEqual(audio_health.total("loop_fault"), 1)


if __name__ == "__main__":
    unittest.main()
