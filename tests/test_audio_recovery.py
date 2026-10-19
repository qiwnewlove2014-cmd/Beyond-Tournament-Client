"""The room puts itself back -- and the log says whether it held.

Stage 3 of ``audio_reverb_recovery_plan.md``.  The recovery is policy: a room
that is dry, a slot whose handle stopped answering, and a send that raised are
the only three faults it can know, and each is repaired with a mechanism the
game already has (``ensure_slot``, ``slot.reload``, ``sync_reverb``).

Everything here runs without a device: the policy is driven by a fake clock and
a fake view, and the view's own two OpenAL reads (``slot.gain``, ``reload``)
are exercised against fakes that raise exactly as cyal does.

The rules pinned here are the ones a wrong version would break in the field:
confirm before acting, never repair a fault that cleared itself, bound every
fault's attempts (and the audible re-assert's rate), back off, give up out
loud, and never call a repair recovered before its verification window has
passed.
"""

import unittest
from types import SimpleNamespace
from unittest import mock

from libs.audio_health import (
    AudioHealth,
    AudioRecovery,
    GameAudioView,
    RECOVERY_DEAD,
    RECOVERY_DRY,
    RECOVERY_ROUTE,
)


class Clock:
    """A clock the test moves, so no scenario has to sleep."""

    def __init__(self, now=0.0):
        self.now = now

    def __call__(self):
        return self.now

    def tick(self, seconds=1.0):
        self.now += seconds
        return self.now

    def run(self, recovery, view, seconds, step=1.0):
        """Tick once a ``step`` for ``seconds``; return every report."""
        reports = []
        left = float(seconds)
        while left >= step:
            self.tick(step)
            left -= step
            reports.append(recovery.tick(view))
        return reports


class FakeSlot:
    """A leased slot: ``gain`` is a real AL read, so a dead one raises."""

    def __init__(self, live=True):
        self.live = live
        self.reloads = 0

    @property
    def gain(self):
        if not self.live:
            raise RuntimeError("dead handle")
        return 0.9

    def reload(self):
        self.reloads += 1
        self.live = True


class FakeRoom:
    def __init__(self, room_id, slot=None):
        self.id = room_id
        self.reverb = slot
        self.asks = 0

    def ensure_slot(self, force=False):
        self.asks += 1
        if self.reverb is None:
            self.reverb = FakeSlot()
        return self.reverb


class FakeView:
    def __init__(self, rooms=()):
        self.room_list = list(rooms)
        self.repaired = []
        self.refuse = set()
        self.raise_on_rooms = False

    def rooms(self):
        if self.raise_on_rooms:
            raise RuntimeError("no map")
        return self.room_list

    def room_label(self, room):
        return room.id

    def room_slot(self, room):
        return room.reverb

    def slot_live(self, slot):
        return slot.live

    def repair(self, kind, room):
        self.repaired.append(kind)
        if kind in self.refuse:
            return False
        if kind == RECOVERY_ROUTE:
            return True
        if kind == RECOVERY_DRY:
            return room.ensure_slot() is not None
        if kind == RECOVERY_DEAD:
            room.reverb.reload()
            return room.reverb.live
        return False


class RecoveryCase(unittest.TestCase):
    def make(self, rooms=(), health=None, **kwargs):
        self.clock = Clock()
        self.lines = []
        kwargs.setdefault("enabled", True)
        self.health = health
        self.recovery = AudioRecovery(
            health=health, clock=self.clock, emit=self.lines.append, **kwargs
        )
        self.view = FakeView(rooms)
        return self.recovery, self.view

    def advance(self, seconds, step=1.0):
        return self.clock.run(self.recovery, self.view, seconds, step)


class TheHealthyCaseTests(RecoveryCase):
    def test_a_clean_map_is_never_touched(self):
        recovery, view = self.make([FakeRoom("a", FakeSlot()),
                                    FakeRoom("b", FakeSlot())])

        self.advance(30)

        self.assertEqual(view.repaired, [])
        self.assertEqual(recovery.state, AudioRecovery.HEALTHY)
        self.assertEqual(self.lines, [])
        self.assertEqual((recovery.repairs, recovery.gave_up), (0, 0))

    def test_a_room_that_clears_itself_is_never_repaired(self):
        room = FakeRoom("a")
        _recovery, view = self.make([room])

        self.clock.tick()
        self.recovery.tick(view)
        room.reverb = FakeSlot()          # the pool freed a slot on its own
        self.advance(10)

        self.assertEqual(view.repaired, [],
                         "a transient dry room was repaired as a fault")

    def test_a_view_that_cannot_be_read_never_raises(self):
        _recovery, view = self.make([FakeRoom("a")])
        view.raise_on_rooms = True

        report = self.recovery.tick(view)

        self.assertEqual(report["repaired"], [])


class TheDryRoomTests(RecoveryCase):
    def test_a_room_that_stays_dry_is_repaired_and_then_verified(self):
        room = FakeRoom("hall")
        recovery, view = self.make([room])

        self.advance(1.0)
        self.assertEqual(view.repaired, [], "the first second is the baseline")
        self.advance(2.0)
        self.assertEqual(view.repaired, [RECOVERY_DRY])
        self.assertEqual(room.asks, 1)
        self.assertIsNotNone(room.reverb)
        self.assertEqual(recovery.state, AudioRecovery.VERIFYING)

        self.advance(5.0)
        self.assertEqual(recovery.state, AudioRecovery.HEALTHY)
        self.assertEqual(recovery.recovered, 1)
        self.assertTrue(any("recovered dry" in line for line in self.lines))

    def test_the_repair_line_names_the_room_and_the_attempt(self):
        _recovery, view = self.make([FakeRoom("hall")])

        self.advance(4.0)

        repairs = [line for line in self.lines if line.startswith("[AUDIO RECOVERY] repair")]
        self.assertEqual(len(repairs), 1)
        self.assertIn("dry", repairs[0])
        self.assertIn("room=hall", repairs[0])
        self.assertIn("ok=True", repairs[0])
        self.assertIn("attempt=1", repairs[0])

    def test_a_dead_slot_is_reloaded_in_place(self):
        slot = FakeSlot(live=False)
        room = FakeRoom("hall", slot)
        recovery, view = self.make([room])

        self.advance(4.0)

        self.assertEqual(view.repaired, [RECOVERY_DEAD])
        self.assertEqual(slot.reloads, 1)
        self.assertTrue(slot.live)
        self.assertEqual(recovery.recovered, 0, "verification is still pending")


class TheVerificationWindowTests(RecoveryCase):
    def test_a_repair_that_came_back_is_reported_unconfirmed(self):
        room = FakeRoom("hall")
        _recovery, view = self.make([room], verify_seconds=3.0)

        self.advance(3.0)                              # repaired at ~2 s
        self.assertEqual(view.repaired, [RECOVERY_DRY])
        room.reverb = None                         # it went dry again
        self.advance(4.0)

        self.assertTrue(any("unconfirmed" in line for line in self.lines))
        self.assertFalse(any("recovered" in line for line in self.lines))

    def test_recovered_is_never_said_before_the_window(self):
        room = FakeRoom("hall")
        _recovery, view = self.make([room], verify_seconds=10.0)

        self.advance(4.0)                              # repaired at ~2 s
        self.assertTrue(any("repair" in line for line in self.lines))
        self.assertFalse(any("recovered" in line for line in self.lines))

        self.advance(10.0)
        self.assertTrue(any("recovered" in line for line in self.lines))


class TheLoopGuardTests(RecoveryCase):
    def test_every_dry_room_gets_its_own_attempts(self):
        """An exhausted map must be able to take the slots as they come free.

        A three-a-minute budget for *all* repairs starved this: twelve dry
        rooms would have waited four minutes for twelve slots that were already
        free. Room repairs are bounded per room instead (confirm, backoff,
        max_attempts), which is what this pins.
        """
        rooms = [FakeRoom(f"r{index}") for index in range(6)]
        recovery, view = self.make(rooms)

        self.advance(3.0)

        self.assertEqual(len(view.repaired), 6)
        self.assertEqual(len({room.id for room in rooms if room.reverb is None}), 0)
        self.assertEqual(recovery.gave_up, 0)

    def test_a_room_whose_repair_never_works_gives_up_after_its_attempts(self):
        rooms = [FakeRoom(f"r{index}") for index in range(6)]
        recovery, view = self.make(rooms)
        view.refuse.add(RECOVERY_DRY)

        self.advance(60.0)

        self.assertEqual(len(view.repaired), 6 * recovery.max_attempts)
        self.assertEqual(recovery.gave_up, 6)
        self.assertEqual(recovery.state, AudioRecovery.GAVE_UP)
        self.assertEqual(
            len([line for line in self.lines if "gave_up" in line]), 6
        )

    def test_a_failed_repair_waits_before_the_next_attempt(self):
        recovery, view = self.make([FakeRoom("hall")], cooldown=5.0)
        view.refuse.add(RECOVERY_DRY)

        self.advance(1.0)                              # baseline
        attempts = []
        for _ in range(30):                        # 30 s at 1 s a tick
            before = len(view.repaired)
            self.advance(1.0)
            if len(view.repaired) > before:
                attempts.append(self.clock.now)

        self.assertGreaterEqual(len(attempts), 2)
        self.assertGreaterEqual(attempts[1] - attempts[0], 5.0,
                                "a failed repair was retried without backoff")
        self.assertEqual(recovery.state, AudioRecovery.GAVE_UP)
        self.assertTrue(any("gave_up dry" in line for line in self.lines))

    def test_giving_up_is_terminal_until_the_next_map(self):
        recovery, view = self.make([FakeRoom("hall")])
        view.refuse.add(RECOVERY_DRY)
        self.advance(1.0)
        self.advance(40.0)
        self.assertEqual(recovery.state, AudioRecovery.GAVE_UP)
        repaired = len(view.repaired)

        self.advance(120.0)
        self.assertEqual(len(view.repaired), repaired,
                         "a room given up on was repaired again on the same map")

        self.clock.tick()
        recovery.reset_incident()
        self.advance(3.0)
        self.assertGreater(len(view.repaired), repaired,
                           "a new map did not give the room another chance")


class TheRouteTests(RecoveryCase):
    def make_health(self):
        health = AudioHealth(verbose=False, sink=lambda line: None)
        return health

    def test_a_counted_send_failure_reasserts_the_owners_once(self):
        health = self.make_health()
        recovery, view = self.make([], health=health)

        health.note_send_failure("group", "InvalidNameError")
        self.advance(4.0)

        self.assertEqual(view.repaired, [RECOVERY_ROUTE],
                         "a failed send was never re-asserted")
        self.advance(30.0)
        self.assertEqual(view.repaired, [RECOVERY_ROUTE],
                         "the owners were re-asserted again with no new failure")

    def test_a_second_failure_is_repaired_again(self):
        health = self.make_health()
        recovery, view = self.make([], health=health)

        health.note_send_failure("group", "InvalidNameError")
        self.advance(10.0)
        health.note_send_failure("unbound", "InvalidNameError")
        self.advance(10.0)

        self.assertEqual(view.repaired, [RECOVERY_ROUTE, RECOVERY_ROUTE])

    def test_a_route_is_never_called_recovered(self):
        health = self.make_health()
        _recovery, view = self.make([], health=health)

        health.note_send_failure("group", "InvalidNameError")
        self.advance(20.0)

        self.assertFalse(any("recovered" in line for line in self.lines),
                         "a route cannot be re-inspected, so it cannot be proven")

    def test_at_most_three_reasserts_a_minute(self):
        health = self.make_health()
        recovery, view = self.make([], health=health)

        for _ in range(45):
            health.note_send_failure("group", "InvalidNameError")
            self.advance(1.0)

        self.assertEqual(view.repaired.count(RECOVERY_ROUTE), 3,
                         "a failing route was re-asserted more than three times a minute")
        self.assertGreaterEqual(recovery.gave_up, 1)
        self.assertTrue(any("reason=rate" in line for line in self.lines))

    def test_the_switch_turns_the_whole_thing_off(self):
        view = FakeView([FakeRoom("hall")])
        with mock.patch.dict("os.environ", {"BT_AUDIO_RECOVERY": "0"}):
            off = AudioRecovery(health=None, clock=Clock(),
                                emit=lambda line: None)
            self.assertFalse(off.enabled)
            self.assertEqual(off.tick(view)["state"], AudioRecovery.DISABLED)
        self.assertEqual(view.repaired, [])

    def test_the_default_is_on(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertTrue(AudioRecovery(health=None, clock=Clock()).enabled)
        with mock.patch.dict("os.environ", {"BT_AUDIO_RECOVERY": "1"}):
            self.assertTrue(AudioRecovery(health=None, clock=Clock()).enabled)


class RecordingGroup:
    direct = False

    def __init__(self):
        self.sends = [None, None, None, None]
        self.effects = []

    def apply_effect(self, slot, sendnum=0, filter=None):
        self.sends[sendnum] = slot
        self.effects.append((slot, sendnum))


class TheGameViewTests(unittest.TestCase):
    """The only place the recovery is allowed to touch a real game object."""

    def make_camera(self, room, group):
        from libs.camera import Camera

        map_obj = SimpleNamespace(
            reverb_list=[room] if room is not None else [],
            entities={},
            get_reverb_at=lambda x, y, z: room,
        )
        cam = Camera.__new__(Camera)
        cam.game = SimpleNamespace(gameplay=SimpleNamespace(map=map_obj))
        cam.focus_object = SimpleNamespace(map=map_obj, dead=False,
                                           soundgroup=group)
        cam.soundgroup = group
        cam.x = cam.y = cam.z = 0.0
        cam.reverb = None
        cam.currentzone = ""
        return cam, map_obj

    def test_a_dead_slot_is_read_through_the_driver_and_reloaded(self):
        slot = FakeSlot(live=False)
        view = GameAudioView(SimpleNamespace(gameplay=None))

        self.assertFalse(view.slot_live(slot))
        self.assertTrue(view.repair(RECOVERY_DEAD, SimpleNamespace(reverb=slot)))
        self.assertEqual(slot.reloads, 1)
        self.assertTrue(view.slot_live(slot))

    def test_no_device_is_not_a_fault(self):
        view = GameAudioView(SimpleNamespace(gameplay=None))
        self.assertIsNone(view.slot_live(None))

    def test_a_repaired_room_reaches_the_ears_standing_in_it(self):
        group = RecordingGroup()
        room = FakeRoom("hall", FakeSlot())
        cam, map_obj = self.make_camera(room, group)
        game = SimpleNamespace(gameplay=SimpleNamespace(map=map_obj, camera=cam))

        view = GameAudioView(game)
        self.assertTrue(view.repair(RECOVERY_DRY, room))

        self.assertEqual(room.asks, 1, "ensure_slot(force=True) was not asked")
        self.assertIs(cam.reverb, room)
        self.assertEqual(group.effects[-1], (room.reverb, 0))

    def test_a_room_that_cannot_lease_again_is_a_failed_repair(self):
        class Refusing(FakeRoom):
            def ensure_slot(self, force=False):
                self.asks += 1
                return None

        room = Refusing("hall")
        game = SimpleNamespace(gameplay=None)
        self.assertFalse(GameAudioView(game).repair(RECOVERY_DRY, room))

    def test_reassert_routes_the_ears_and_every_peer(self):
        group = RecordingGroup()
        room = FakeRoom("hall", FakeSlot())
        cam, map_obj = self.make_camera(room, group)
        calls = []

        class Peer:
            player = True

            def sync_reverb(self):
                calls.append("sync")
                return True

        class Zombie:
            player = False

            def sync_reverb(self):
                calls.append("zombie")
                return True

        map_obj.entities = {"peer": Peer(), "walker": Zombie()}
        view = GameAudioView(
            SimpleNamespace(gameplay=SimpleNamespace(map=map_obj, camera=cam))
        )

        self.assertEqual(view.reassert(), 2)
        self.assertEqual(calls, ["sync"],
                         "a monster's position does not route anything")
        self.assertEqual(group.effects[-1], (room.reverb, 0))

    def test_reassert_with_no_map_or_camera_is_a_no_op(self):
        self.assertEqual(GameAudioView(SimpleNamespace(gameplay=None)).reassert(), 0)


if __name__ == "__main__":
    unittest.main()
