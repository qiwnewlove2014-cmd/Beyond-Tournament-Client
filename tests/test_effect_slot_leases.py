"""One slot per sound, and a pool that gets its slots back.

The sound card grants a fixed number of auxiliary effect slots for the life of
the process -- measured through the shipped build at exactly 64, with no
request raising it (the 65th alive slot answers ``MemoryError`` whatever
``max_auxiliary_sends`` asked for).  Because the ceiling cannot move, two
properties decide whether a busy map keeps its rooms:

* **identical settings share one slot.** A map that places five identical
  reverb zones, or thirteen PA speakers carrying three decays, used to spend
  one of the 64 per *element* -- so the rooms that could not borrow one went
  dry.  A lease gives one slot to every holder whose parameters match.
* **a slot whose holder is gone comes back at the next map load.** The holder
  is recorded (as a weakref, never a strong one), so an element replaced in
  place or an entity overwritten in the table no longer costs a slot until the
  client is restarted.

Everything here is device-free: the pool is built by attribute with fake slot
objects, which is what the real pool does the same arithmetic on.
"""

import gc
import unittest
import weakref
from types import SimpleNamespace
from unittest.mock import patch


class FakeEffect:
    def __init__(self, kind):
        self.kind = kind
        self.params = []

    def set(self, name, value):
        self.params.append((name, value))


class FakeSlot:
    def __init__(self, name):
        self.name = name
        self.effect = None
        self.unloads = 0

    def unload(self):
        self.unloads += 1
        self.effect = None


class FakeEfx:
    """The EFX extension: effects to create, and a log of every send."""

    def __init__(self):
        self.sent = []
        self.created = {}

    def gen_effect(self, type):
        self.created[type] = self.created.get(type, 0) + 1
        return FakeEffect(type)

    def send(self, source, index, slot, filter=None):
        self.sent.append((source, index, slot))


class FakeSoundGroup:
    """The real shape: one entry per send index, as a list."""

    def __init__(self):
        self.sends = [None, None, None, None]

    def apply_effect(self, slot, sendnum=0, filter=None):
        self.sends[sendnum] = slot


from libs.audio_manager import AudioManager


class _TestAudio(AudioManager):
    """The real pool arithmetic, with the ctypes armor left out.

    The armor permanently INCREFs an effect wrapper so cyal's crash-prone
    dealloc can never run; a fake effect has no such dealloc, and keeping the
    INCREF would hold every fake for the life of the test process and print a
    shutdown warning for each.
    """

    def _armor_filter(self, filter_obj, site, label="filter"):
        return None


def bare_audio(slots=4):
    """A real AudioManager with a fake device: only the pool is real.

    ``__init__`` opens the sound card, which a test must not do, so the few
    attributes the pool arithmetic touches are set by hand -- the same values,
    the same types, no OpenAL.
    """
    audio = _TestAudio.__new__(_TestAudio)
    audio._slot_pool = [FakeSlot(f"slot{i}") for i in range(slots)]
    audio._slot_in_use = []
    audio._slot_pool_size = slots
    audio._slot_hold = {}
    audio._effect_leases = {}
    audio._reclaimed_slots = 0
    audio._exhaustion_reported = False
    audio.efx = FakeEfx()
    audio.sends = [None, None, None, None]
    audio.soundgroups = set()
    audio.unbound_sources = []
    audio._filter_pool = []
    return audio


REVERB_SETTING = (("decay_time", 3.0), ("diffusion", 1.0))
OTHER_SETTING = (("decay_time", 1.0), ("diffusion", 0.4))


class IdenticalSettingsShareOneSlotTests(unittest.TestCase):
    def test_two_rooms_built_alike_borrow_one_slot(self):
        audio = bare_audio()
        first = audio.lease_effect("EAXREVERB", REVERB_SETTING, "room:a", kind="room")
        second = audio.lease_effect("EAXREVERB", REVERB_SETTING, "room:b", kind="room")
        self.assertIsNotNone(first)
        self.assertIs(first, second)
        self.assertEqual(len(audio._slot_in_use), 1)
        self.assertEqual(len(audio._slot_pool), 3)

    def test_a_different_setting_gets_its_own_slot(self):
        audio = bare_audio()
        first = audio.lease_effect("EAXREVERB", REVERB_SETTING, "room:a", kind="room")
        second = audio.lease_effect("EAXREVERB", OTHER_SETTING, "room:b", kind="room")
        self.assertIsNot(first, second)
        self.assertEqual(len(audio._slot_in_use), 2)

    def test_the_same_setting_of_another_effect_type_is_not_shared(self):
        audio = bare_audio()
        first = audio.lease_effect("EAXREVERB", REVERB_SETTING, "room:a", kind="room")
        second = audio.lease_effect("EQUALIZER", REVERB_SETTING, "eq:a", kind="voice_effects")
        self.assertIsNot(first, second)

    def test_the_slot_returns_when_the_last_holder_lets_go(self):
        audio = bare_audio()
        audio.lease_effect("EAXREVERB", REVERB_SETTING, "room:a", kind="room")
        audio.lease_effect("EAXREVERB", REVERB_SETTING, "room:b", kind="room")
        self.assertFalse(audio.release_effect_lease("EAXREVERB", REVERB_SETTING, "room:a"))
        self.assertEqual(len(audio._slot_in_use), 1)
        self.assertTrue(audio.release_effect_lease("EAXREVERB", REVERB_SETTING, "room:b"))
        self.assertEqual(len(audio._slot_in_use), 0)
        self.assertEqual(len(audio._slot_pool), 4)

    def test_a_lease_the_pool_could_not_grant_stays_dry(self):
        audio = bare_audio(slots=0)
        self.assertIsNone(
            audio.lease_effect("EAXREVERB", REVERB_SETTING, "room:a", kind="room")
        )
        self.assertEqual(audio._effect_leases, {})


class RecyclingDetachesWhatStillListensTests(unittest.TestCase):
    def test_the_last_release_clears_a_soundgroup_send(self):
        audio = bare_audio()
        group = FakeSoundGroup()
        audio.soundgroups.add(group)
        slot = audio.lease_effect("EAXREVERB", REVERB_SETTING, "room:a", kind="room")
        group.apply_effect(slot, 0)
        audio.release_effect_lease("EAXREVERB", REVERB_SETTING, "room:a")
        # The slot is about to be lent to somebody else: a source still sending
        # into it would play through the next holder's effect instead of the
        # room it asked for.
        self.assertIsNone(group.sends[0])

    def test_a_soundgroup_that_keeps_its_sends_in_a_dict_is_read_too(self):
        class DictGroup:
            def __init__(self):
                self.sends = {}

            def apply_effect(self, slot, sendnum=0, filter=None):
                self.sends[sendnum] = slot

        audio = bare_audio()
        group = DictGroup()
        audio.soundgroups.add(group)
        slot = audio.lease_effect("EAXREVERB", REVERB_SETTING, "room:a", kind="room")
        group.apply_effect(slot, 2)
        audio.release_effect_lease("EAXREVERB", REVERB_SETTING, "room:a")
        self.assertIsNone(group.sends[2])

    def test_a_shared_slot_is_not_detached_while_another_holder_listens(self):
        audio = bare_audio()
        group = FakeSoundGroup()
        audio.soundgroups.add(group)
        slot = audio.lease_effect("EAXREVERB", REVERB_SETTING, "room:a", kind="room")
        audio.lease_effect("EAXREVERB", REVERB_SETTING, "room:b", kind="room")
        group.apply_effect(slot, 0)
        audio.release_effect_lease("EAXREVERB", REVERB_SETTING, "room:a")
        self.assertIs(group.sends[0], slot)

    def test_the_pools_own_send_table_is_cleared_too(self):
        audio = bare_audio()
        slot = audio.lease_effect("EAXREVERB", REVERB_SETTING, "room:a", kind="room")
        audio.apply_effect(slot, 1)
        audio.release_effect_lease("EAXREVERB", REVERB_SETTING, "room:a")
        self.assertIsNone(audio.sends[1])

    def test_recycling_unloads_the_effect(self):
        audio = bare_audio()
        slot = audio.lease_effect("EAXREVERB", REVERB_SETTING, "room:a", kind="room")
        audio.release_effect_lease("EAXREVERB", REVERB_SETTING, "room:a")
        self.assertEqual(slot.unloads, 1)
        self.assertIsNone(slot.effect)


class TheSweepReturnsOrphansTests(unittest.TestCase):
    class Holder:
        pass

    def test_a_holder_that_vanished_comes_back_on_the_sweep(self):
        audio = bare_audio()
        holder = self.Holder()
        ref = weakref.ref(holder)
        slot = audio.gen_effect(
            "EQUALIZER", hold=("voice_effects", "zomby-1:eq", holder)
        )
        del holder
        gc.collect()
        self.assertIsNone(ref())
        self.assertEqual(audio.reclaim_orphaned_slots(), 1)
        self.assertNotIn(slot, audio._slot_in_use)
        self.assertEqual(len(audio._slot_pool), 4)

    def test_a_live_holder_is_never_taken_away(self):
        audio = bare_audio()
        holder = self.Holder()  # kept alive on purpose
        slot = audio.gen_effect("EQUALIZER", hold=("voice_effects", "here:eq", holder))
        self.assertEqual(audio.reclaim_orphaned_slots(), 0)
        self.assertIn(slot, audio._slot_in_use)
        self.assertIs(holder, holder)

    def test_a_slot_nobody_recorded_is_left_where_it_is(self):
        audio = bare_audio()
        slot = audio.gen_effect("CHORUS")
        self.assertEqual(audio.reclaim_orphaned_slots(), 0)
        self.assertIn(slot, audio._slot_in_use)

    def test_a_lease_whose_holders_all_vanished_is_reclaimed(self):
        audio = bare_audio()
        holder = self.Holder()
        slot = audio.lease_effect(
            "EAXREVERB", REVERB_SETTING, "room:a", ref=holder, kind="room"
        )
        del holder
        gc.collect()
        self.assertEqual(audio.reclaim_orphaned_slots(), 1)
        self.assertNotIn(slot, audio._slot_in_use)

    def test_the_sweep_detaches_before_it_recycles(self):
        audio = bare_audio()
        group = FakeSoundGroup()
        audio.soundgroups.add(group)
        holder = self.Holder()
        slot = audio.gen_effect(
            "EQUALIZER", hold=("voice_effects", "gone:eq", holder)
        )
        group.apply_effect(slot, 0)
        del holder
        gc.collect()
        audio.reclaim_orphaned_slots()
        self.assertIsNone(group.sends[0])


class TheReportNamesWhatHoldsThePoolTests(unittest.TestCase):
    def test_the_line_counts_the_holders_by_their_job(self):
        audio = bare_audio(slots=8)
        audio.gen_effect("EQUALIZER", hold=("voice_effects", "a:eq", None))
        audio.gen_effect("DISTORTION", hold=("voice_effects", "a:radio", None))
        audio.lease_effect("EAXREVERB", REVERB_SETTING, "room:a", kind="room")
        line = audio.slot_report_line()
        self.assertIn("3/8 in use", line)
        self.assertIn("room 1", line)
        self.assertIn("voice_effects 2", line)

    def test_releasing_a_slot_forgets_its_holder(self):
        audio = bare_audio(slots=8)
        slot = audio.gen_effect("EQUALIZER", hold=("voice_effects", "a:eq", None))
        audio.release_effect_slot(slot)
        self.assertEqual(audio.slot_report()["kinds"], {})
        self.assertIn("0/8 in use", audio.slot_report_line())

    def test_an_exhausted_pool_names_the_holders_in_its_warning(self):
        audio = bare_audio(slots=1)
        audio.gen_effect("EQUALIZER", hold=("voice_effects", "a:eq", None))
        with patch("builtins.print") as printed:
            self.assertIsNone(audio.gen_effect("CHORUS"))
        said = " ".join(str(call) for call in printed.call_args_list)
        self.assertIn("pool exhausted", said)
        self.assertIn("voice_effects 1", said)


class ARoomSharesTheSlotOfEveryRoomBuiltLikeItTests(unittest.TestCase):
    """The wiring, not just the pool: real zones, real map element code."""

    def _map(self, audio):
        from libs.world_map import Map

        map_obj = Map.__new__(Map)
        map_obj.game = SimpleNamespace(audio_mngr=audio)
        map_obj.reverb_list = []
        return map_obj

    def test_five_identical_zones_spend_one_slot(self):
        audio = bare_audio(slots=8)
        map_obj = self._map(audio)
        for index in range(5):
            map_obj.spawn_reverb(id=f"room{index}")
        self.assertEqual(len(map_obj.reverb_list), 5)
        self.assertEqual(len(audio._slot_in_use), 1)
        shared = audio._slot_in_use[0]
        self.assertTrue(all(zone.reverb is shared for zone in map_obj.reverb_list))

    def test_zones_with_different_settings_keep_their_own(self):
        audio = bare_audio(slots=8)
        map_obj = self._map(audio)
        map_obj.spawn_reverb(id="dry", decayTime=0.5)
        map_obj.spawn_reverb(id="hall", decayTime=3.0)
        self.assertEqual(len(audio._slot_in_use), 2)

    def test_replacing_a_zone_does_not_release_the_slot_it_reborrowed(self):
        audio = bare_audio(slots=8)
        map_obj = self._map(audio)
        map_obj.spawn_reverb(id="hall")
        first = map_obj.reverb_list[0]
        # A save that keeps the same element id: the newcomer leases, then the
        # zone it replaces lets go. Releasing by element id would hand away a
        # slot the new zone is already standing on.
        map_obj.spawn_reverb(id="hall")
        self.assertEqual(len(map_obj.reverb_list), 1)
        self.assertEqual(len(audio._slot_in_use), 1)
        self.assertIs(map_obj.reverb_list[0].reverb, audio._slot_in_use[0])
        self.assertIsNot(first, map_obj.reverb_list[0])

    def test_the_last_zone_to_go_returns_the_shared_slot(self):
        audio = bare_audio(slots=8)
        map_obj = self._map(audio)
        map_obj.spawn_reverb(id="a")
        map_obj.spawn_reverb(id="b")
        map_obj.reverb_list[0].destroy()
        self.assertEqual(len(audio._slot_in_use), 1)
        map_obj.reverb_list[1].destroy()
        self.assertEqual(len(audio._slot_in_use), 0)
        self.assertEqual(len(audio._slot_pool), 8)

    def test_a_zone_that_could_not_borrow_retries_the_same_setting(self):
        audio = bare_audio(slots=0)
        map_obj = self._map(audio)
        map_obj.spawn_reverb(id="hall")
        zone = map_obj.reverb_list[0]
        self.assertIsNone(zone.reverb)
        audio._slot_pool = [FakeSlot("freed")]
        audio._slot_pool_size = 1
        self.assertIsNotNone(zone.ensure_slot(force=True))
        self.assertEqual(len(audio._slot_in_use), 1)


class AListenerKeepsItsRoomAcrossAReloadTests(unittest.TestCase):
    """Footsteps, shots and the rest of the map's sounds after a map change.

    Everything walked and fired is played in the object's own SoundGroup, whose
    send 0 is the room that object stands in (camera applies the listener's,
    ``Entity.sync_reverb`` applies every other entity's). A reload detaches
    those sends before the old map returns its slots -- so the question this
    class answers is whether the new map's room is bound back afterwards, and
    whether sharing a slot can silence a listener whose twin room is torn down.
    """

    class Group:
        def __init__(self):
            self.sends = [None, None, None, None]
            self.filter = []
            self.applied = []

        def apply_effect(self, slot, sendnum=0, filter=None):
            self.sends[sendnum] = slot
            self.applied.append((slot, sendnum))

    def _map(self, audio):
        from libs.world_map import Map

        map_obj = Map.__new__(Map)
        map_obj.game = SimpleNamespace(audio_mngr=audio)
        map_obj.reverb_list = []
        return map_obj

    def _entity(self, map_obj, group):
        from libs.objects.entity import Entity

        entity = Entity.__new__(Entity)
        entity.map = map_obj
        entity.x = entity.y = entity.z = 1
        entity.soundgroup = group
        entity._player = False
        return entity

    def _spawn_room(self, map_obj, room_id="hall"):
        # Real maps place a zone around the walkable area; the box is what
        # ``get_reverb_at`` tests the entity's position against.
        map_obj.spawn_reverb(0, 8, 0, 8, 0, 4, id=room_id)
        return map_obj.reverb_list[0]

    def test_the_new_maps_room_is_bound_back_after_a_reload(self):
        audio = bare_audio(slots=8)
        map_obj = self._map(audio)
        group = self.Group()
        entity = self._entity(map_obj, group)

        zone = self._spawn_room(map_obj)
        entity.sync_reverb()
        first_slot = zone.reverb
        self.assertIs(group.sends[0], first_slot)

        # What a reload does, in order: every entity detaches before the old
        # map returns its slots, the map is torn down, the new one is built.
        entity.detach_environment_effects()
        self.assertIsNone(group.sends[0])
        zone.destroy()
        map_obj.reverb_list = []
        self.assertEqual(len(audio._slot_pool), 8)

        self._spawn_room(map_obj)
        entity.sync_reverb()
        self.assertIs(group.sends[0], map_obj.reverb_list[0].reverb)
        self.assertIsNotNone(group.sends[0], "a room must come back after a reload")

    def test_an_in_place_reload_re_syncs_every_entity_still_standing_there(self):
        audio = bare_audio(slots=8)
        map_obj = self._map(audio)
        group = self.Group()
        entity = self._entity(map_obj, group)

        zone = self._spawn_room(map_obj)
        entity.sync_reverb()
        self.assertIs(group.sends[0], zone.reverb)
        # update_map keeps its entities, so the reload path itself re-syncs them
        # (`_apply_update_map` walks the table) instead of relying on a step.
        zone.destroy()
        map_obj.reverb_list = []
        entity.sync_reverb()
        self.assertIsNone(group.sends[0])
        self._spawn_room(map_obj)
        entity.sync_reverb()
        self.assertIs(group.sends[0], map_obj.reverb_list[0].reverb)

    def test_tearing_down_one_of_two_identical_rooms_leaves_the_listener_alone(self):
        audio = bare_audio(slots=8)
        map_obj = self._map(audio)
        group = self.Group()
        entity = self._entity(map_obj, group)

        self._spawn_room(map_obj, "north")
        self._spawn_room(map_obj, "south")
        shared = map_obj.reverb_list[0].reverb
        self.assertIs(shared, map_obj.reverb_list[1].reverb)
        entity.sync_reverb()
        self.assertIs(group.sends[0], shared)

        # One room of a shared pair goes: the slot is still leased by the other,
        # so the listener's send must be left exactly as it was.
        map_obj.reverb_list[0].destroy()
        self.assertIs(group.sends[0], shared)
        self.assertIn(shared, audio._slot_in_use)


class APASpeakerSharesWithItsTwinsTests(unittest.TestCase):
    def _manager(self, audio):
        from libs.systems.megaphone_system import MegaphoneManager

        manager = MegaphoneManager.__new__(MegaphoneManager)
        manager.game = SimpleNamespace(audio_mngr=audio)
        return manager

    def test_two_speakers_of_the_same_setting_share_one_slot(self):
        audio = bare_audio(slots=8)
        manager = self._manager(audio)
        setting = (("decay_time", 3.0), ("diffusion", 1.0))
        first = {
            "reverb_slot": audio.lease_effect("EAXREVERB", setting, "pa:1", kind="pa_speaker"),
            "reverb_lease": (setting, "pa:1"),
        }
        second = {
            "reverb_slot": audio.lease_effect("EAXREVERB", setting, "pa:2", kind="pa_speaker"),
            "reverb_lease": (setting, "pa:2"),
        }
        self.assertIs(first["reverb_slot"], second["reverb_slot"])
        shared = first["reverb_slot"]
        manager._release_speaker_reverb(first)
        self.assertIn(shared, audio._slot_in_use)
        self.assertIn("pa_speaker 1", audio.slot_report_line())
        manager._release_speaker_reverb(second)
        self.assertNotIn(shared, audio._slot_in_use)
        self.assertIsNone(first["reverb_lease"])

    def test_a_speaker_from_an_older_build_still_returns_its_plain_slot(self):
        audio = bare_audio(slots=8)
        manager = self._manager(audio)
        slot = audio.gen_effect("EAXREVERB")
        manager._release_speaker_reverb({"reverb_slot": slot})
        self.assertNotIn(slot, audio._slot_in_use)


class TheTechnicianReadOutTests(unittest.TestCase):
    def test_the_read_out_speaks_the_line_and_writes_nothing(self):
        import libs.event_handeler as event_handeler
        from libs.event_handeler import EventHandeler

        handler = EventHandeler.__new__(EventHandeler)
        audio = bare_audio(slots=8)
        audio.gen_effect("EQUALIZER", hold=("voice_effects", "a:eq", None))
        handler.game = SimpleNamespace(audio_mngr=audio)

        spoken = []
        with patch.object(event_handeler, "speak", spoken.append), \
                patch("libs.logger.log") as logged:
            handler.sound_engine_report({})

        self.assertEqual(len(spoken), 1)
        self.assertIn("1/8 in use", spoken[0])
        self.assertTrue(logged.called)

    def test_an_audio_manager_without_a_pool_is_not_a_crash(self):
        import libs.event_handeler as event_handeler
        from libs.event_handeler import EventHandeler

        handler = EventHandeler.__new__(EventHandeler)
        handler.game = SimpleNamespace(audio_mngr=None)
        spoken = []
        with patch.object(event_handeler, "speak", spoken.append):
            handler.sound_engine_report({})
        self.assertEqual(spoken, ["Effect slot report unavailable."])


class TheMapLoadSweepsOrphansTests(unittest.TestCase):
    def test_finishing_a_map_reload_reclaims_what_the_old_map_left(self):
        from libs.event_handeler import EventHandeler

        handler = EventHandeler.__new__(EventHandeler)
        audio = bare_audio(slots=4)
        calls = []
        audio.reclaim_orphaned_slots = lambda: calls.append(1) or 2
        handler.game = SimpleNamespace(audio_mngr=audio)
        handler.gameplay = SimpleNamespace(
            jukebox_player=None, music_bot=None, megaphone=None,
        )
        handler._finish_map_audio_reload()
        self.assertEqual(calls, [1])


if __name__ == "__main__":
    unittest.main()
