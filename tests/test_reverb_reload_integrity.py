"""The room a listener hears is the room they are standing in -- after any reload.

A reverb zone borrows one of the driver's effect slots and every sound that
belongs to that zone is pointed at it (a SoundGroup's send 0, and for a player
entity the voice/music sources' send 0).  A slot is a *place*, not a sound: it
holds whichever effect was attached to it last.  So the invariant that keeps a
room sounding like itself is that a slot nobody is listening to may be handed
on, and a slot somebody is still listening to may not -- the moment a live
slot is returned to the pool, the next borrower attaches a different effect to
it and every source still sending into it (footsteps, a voice, a song) starts
playing through that stranger's sound: not dry, *wrong*.  That is the symptom
this file exists for: reverb that comes back "changed" or buzzing after a
builder edits a room or a map is loaded many times.

Everything here is device-free: the real ``AudioManager`` pool arithmetic, the
real ``Reverb`` zone and the real ``Entity.sync_reverb`` run over a fake EFX
extension that models what OpenAL does with a slot (``slot.effect`` is whoever
attached last) and records every send.
"""

import contextlib
import gc
import random
import unittest
from types import SimpleNamespace


class FakeEffect:
    def __init__(self, kind):
        self.kind = kind
        self.params = {}

    def set(self, name, value):
        self.params[name] = value


class FakeSlot:
    def __init__(self, name):
        self.name = name
        self.effect = None
        self.unloads = 0

    def unload(self):
        self.unloads += 1
        self.effect = None


class FakeEfx:
    """Model of the EFX extension: slots hold one effect, sends are recorded."""

    def __init__(self):
        self.created = []
        self.sends = {}

    def gen_effect(self, type):
        effect = FakeEffect(type)
        self.created.append(effect)
        return effect

    def send(self, source, index, slot, filter=None):
        self.sends[(id(source), index)] = slot


class FakeGroup:
    """The real shape of a SoundGroup's send table: one entry per index."""

    def __init__(self):
        self.sends = [None, None, None, None]
        self.position = None
        self.filter = []
        self.unlabeled_sources = []
        self.labeled_sources = {}

    def apply_effect(self, slot, sendnum=0, filter=None):
        self.sends[sendnum] = slot


def bare_audio(slots=8):
    """A real AudioManager with a fake device: only the pool is real."""
    from libs.audio_manager import AudioManager

    class _TestAudio(AudioManager):
        # The armor permanently INCREFs a wrapper so cyal's crash-prone dealloc
        # can never run; a fake has no such dealloc.
        def _armor_filter(self, filter_obj, site, label="filter"):
            return None

    audio = _TestAudio.__new__(_TestAudio)
    audio._slot_pool = [FakeSlot(f"slot{i}") for i in range(slots)]
    audio._slot_in_use = []
    audio._slot_pool_size = slots
    audio._slot_hold = {}
    audio._effect_leases = {}
    audio._reclaimed_slots = 0
    audio._exhaustion_reported = False
    audio._lease_serial = 0
    audio.efx = FakeEfx()
    audio.sends = [None, None, None, None]
    audio.soundgroups = set()
    audio.unbound_sources = []
    audio._filter_pool = []
    return audio


def bare_map(audio):
    from libs.world_map import Map

    map_obj = Map.__new__(Map)
    map_obj.game = SimpleNamespace(audio_mngr=audio)
    map_obj.reverb_list = []
    map_obj.entities = {}
    return map_obj


def hall_params(decay=1.49):
    """The parameter set a room built with ``decayTime=decay`` leases."""
    return (("decay_time", float(decay)),
            ("density", 1.0), ("diffusion", 1.0), ("gain", 0.32),
            ("gainhf", 0.89), ("gainlf", 1.0),
            ("decay_hfratio", 0.83), ("decay_lfratio", 1.0),
            ("reflections_gain", 0.05), ("reflections_delay", 0.007),
            ("reflections_pan", (0.0, 0.0, 0.0)),
            ("late_reverb_gain", 1.26), ("late_reverb_delay", 0.011),
            ("late_reverb_pan", (0.0, 0.0, 0.0)),
            ("echo_time", 0.25), ("echo_depth", 0.0),
            ("modulation_time", 0.25), ("modulation_depth", 0.0),
            ("air_absorption_gainhf", 0.994),
            ("hfreference", 5000.0), ("lfreference", 250.0),
            ("room_rolloff_factor", 0.0))


class TheListenerAlwaysHearsTheirOwnRoomTests(unittest.TestCase):
    """Repeated reload shapes, and one question asked after each of them.

    The room the listener stands in is what their own SoundGroup's send 0
    names.  Whatever the reload did -- a map load, a zone re-saved in the
    Builder, a zone retuned, a room added or removed -- the slot that send
    names must still carry *that room's* effect, and it must not be sitting in
    the pool where the next borrower can overwrite it.
    """

    def setUp(self):
        self.audio = bare_audio()
        self.map = bare_map(self.audio)
        self.group = FakeGroup()
        self.audio.soundgroups = {self.group}

        from libs.objects.entity import Entity

        self.player = Entity.__new__(Entity)
        self.player.map = self.map
        self.player.name = "listener"
        self.player.x = self.player.y = self.player.z = 1
        self.player.soundgroup = self.group
        # A player entity also plays its own voice and music through raw
        # sources, whose send 0 is the same room (this is the pair a wrong
        # slot would garble "right at the player's own head").
        self.player._player = True
        self.player.vc_source = SimpleNamespace(name="vc")
        self.player.music_source = SimpleNamespace(name="music")
        self.player.game = SimpleNamespace(audio_mngr=self.audio)
        self.player.dead = False

    def _room_slots(self):
        """Every send index 0 that names the listener's own room."""
        return [self.group.sends[0],
                self.audio.efx.sends.get((id(self.player.vc_source), 0)),
                self.audio.efx.sends.get((id(self.player.music_source), 0))]

    def _spawn_room(self, room_id="hall", decay=1.49):
        self.map.spawn_reverb(0, 8, 0, 8, 0, 4, id=room_id, decayTime=decay)
        return self.map.reverb_list[-1]

    def _rebind(self):
        """What a reload does after the elements are built: re-sync everyone."""
        self.player.sync_reverb()

    def _reload(self, build):
        """One map reload, in the order the real one runs.

        ``build`` rebuilds the map's elements (the parser's job).  Old rooms
        are destroyed and hand their slots back, then the new ones are spawned;
        the reload's own sweep then runs at the end, as
        ``_finish_map_audio_reload`` does.
        """
        for zone in self.map.reverb_list:
            zone.destroy()
        self.map.reverb_list = []
        build()
        self._rebind()
        self.audio.reclaim_orphaned_slots()

    def _assert_room_heard(self, decay):
        """The listener's sends name their room's effect, and hold the slot."""
        for slot in self._room_slots():
            self.assertIsNotNone(slot, "the listener was left dry")
            effect = getattr(slot, "effect", None)
            self.assertIsNotNone(effect, "the slot is carrying no effect at all")
            self.assertEqual(
                effect.kind, "EAXREVERB",
                "the listener's own sounds are playing through another "
                f"holder's {effect.kind} effect")
            self.assertEqual(
                effect.params.get("decay_time"), float(decay),
                "the listener hears the wrong room")
            self.assertIn(slot, self.audio._slot_in_use,
                          "the slot the listener hears is not in use")
            self.assertNotIn(slot, self.audio._slot_pool,
                             "the slot the listener hears is back in the pool")

    def test_a_map_load_many_times_keeps_the_room_the_listener_stands_in(self):
        self._spawn_room()
        self._rebind()

        for _ in range(20):
            self._reload(lambda: self._spawn_room())
            self._assert_room_heard(decay=1.49)

    def test_a_zone_re_saved_in_place_does_not_lend_out_its_slot(self):
        """The Builder's element edit: same id, same echo, new instance.

        The newcomer leases *before* the zone it replaces lets go -- a label
        keyed to the element id would otherwise release the slot the newcomer
        is already standing on.  What must not happen is the pool being handed
        that slot while the newcomer still holds it.
        """
        self._spawn_room()
        self._rebind()
        first = self.map.reverb_list[0].reverb

        # The element edit itself, then the reload's sweep at the end.
        self.map.spawn_reverb(0, 8, 0, 8, 0, 4, id="hall")
        self._rebind()
        self.audio.reclaim_orphaned_slots()

        self.assertEqual(len(self.map.reverb_list), 1)
        self.assertIs(self.map.reverb_list[0].reverb, first,
                      "the re-saved zone lost the slot it was already holding")
        self._assert_room_heard(decay=1.49)

        # And a stranger taking a slot afterwards must not steal the room's.
        other = self.audio.gen_effect("DISTORTION")
        self.assertIsNot(other, self.group.sends[0],
                         "another sound was handed the room's live slot")
        self._assert_room_heard(decay=1.49)

    def test_a_retuned_zone_is_the_room_the_listener_hears(self):
        self._spawn_room(decay=1.49)
        self._rebind()

        for decay in (2.6, 3.4, 1.1, 1.49, 2.6):
            self.map.spawn_reverb(0, 8, 0, 8, 0, 4, id="hall", decayTime=decay)
            self._rebind()
            self.audio.reclaim_orphaned_slots()
            self._assert_room_heard(decay=decay)

    def test_a_mixed_run_of_reload_shapes_never_hands_the_room_to_a_stranger(self):
        """Twenty cycles of the shapes a builder's session really produces."""
        self._spawn_room()
        self._rebind()
        rng = random.Random(20260926)

        for _ in range(20):
            shape = rng.choice(("map_load", "resave", "retune", "map_load"))
            if shape == "map_load":
                self._reload(lambda: self._spawn_room())
                decay = 1.49
            elif shape == "resave":
                self.map.spawn_reverb(0, 8, 0, 8, 0, 4, id="hall")
                self._rebind()
                self.audio.reclaim_orphaned_slots()
                decay = 1.49
            else:
                decay = rng.choice((0.8, 2.2, 3.6))
                self.map.spawn_reverb(0, 8, 0, 8, 0, 4, id="hall", decayTime=decay)
                self._rebind()
                self.audio.reclaim_orphaned_slots()
            self._assert_room_heard(decay=decay)

        # No slot was duplicated into use and into the pool at the same time,
        # and the eight the driver granted are all accounted for.
        in_use = set(self.audio._slot_in_use)
        pool = set(self.audio._slot_pool)
        self.assertEqual(len(in_use & pool), 0,
                         "a slot is in use and in the pool at the same time")
        self.assertEqual(len(in_use | pool), 8)
        self.assertEqual(len(in_use), len(self.audio._slot_in_use))
        self.assertEqual(len(pool), len(self.audio._slot_pool))

    def test_a_zone_that_vanished_never_leaves_the_listener_in_its_slot(self):
        """A removed room empties the send instead of ringing in the next one."""
        self._spawn_room()
        self._rebind()

        self._reload(lambda: None)  # the rebuild has no room at all
        for slot in self._room_slots():
            self.assertIsNone(slot)

        # And the room coming back is the room the listener hears.
        self._spawn_room()
        self._rebind()
        self._assert_room_heard(decay=1.49)

    def test_a_room_sharing_its_setting_keeps_the_slot_when_its_twin_leaves(self):
        """Two identical zones, one of them deleted: the other keeps listening."""
        self._spawn_room("north")
        self._spawn_room("south")
        self._rebind()
        slot = self.group.sends[0]
        self.assertIs(self.map.reverb_list[0].reverb, self.map.reverb_list[1].reverb)

        # The Builder deletes one of the two (the map reload rebuilds with one).
        self._reload(lambda: self._spawn_room("south"))
        self.assertEqual(len(self.map.reverb_list), 1)
        self._assert_room_heard(decay=1.49)
        self.assertIs(self.group.sends[0], slot,
                      "the surviving twin's slot was taken away")
        self.assertEqual(len(self.audio._slot_in_use), 1)


class TheSweepNeverFreesASlotSomebodyStillHoldsTests(unittest.TestCase):
    """The map-load sweep is allowed to free what is *provably* gone.

    A shared lease records the holder that created it.  That holder can be
    replaced while its twins stay (an element re-saved in place, a map that
    joins the room of a map just left), so the sweep's own view of a slot has
    to be the lease's holder table, never the first holder's ref.
    """

    class Holder:
        pass

    def test_a_slot_whose_lease_still_has_a_live_holder_is_left_alone(self):
        audio = bare_audio()
        setting = (("decay_time", 3.0),)
        first = self.Holder()
        second = self.Holder()
        slot = audio.lease_effect("EAXREVERB", setting, "room:a",
                                  ref=first, kind="room")
        audio.lease_effect("EAXREVERB", setting, "room:b",
                           ref=second, kind="room")
        # The room that created the lease is replaced; its twin still listens.
        audio.release_effect_lease("EAXREVERB", setting, "room:a")
        del first
        gc.collect()

        self.assertEqual(audio.reclaim_orphaned_slots(), 0)
        self.assertIn(slot, audio._slot_in_use)
        self.assertNotIn(slot, audio._slot_pool)
        # A stranger borrowing a slot now must not be handed this one, and
        # the room's effect must be exactly what it was.
        kind_before = slot.effect.kind
        other = audio.gen_effect("DISTORTION")
        self.assertIsNot(other, slot)
        self.assertEqual(slot.effect.kind, kind_before)
        self.assertEqual(slot.effect.kind, "EAXREVERB")

    def test_a_stale_summary_of_a_live_lease_does_not_free_the_slot(self):
        """The lease's holder table beats the summary, however the summary looks.

        The sweep reads two records: the lease's own holder table and the
        slot's one-line summary.  The summary can name a holder that is gone
        while the lease still lists a live one -- that is exactly what an
        element re-saved in place leaves behind on an older build -- and the
        lease must win, because freeing the slot detaches sends that are still
        listening and lends the slot to the next borrower.
        """
        audio = bare_audio()
        setting = (("decay_time", 3.0),)
        holder = self.Holder()
        live = self.Holder()
        slot = audio.lease_effect("EAXREVERB", setting, "room:",
                                  ref=live, kind="room")
        # A summary pointing at somebody who is gone, written straight into the
        # bookkeeping the sweep reads.
        audio.hold_slot(slot, "room", "room:stale", holder)
        del holder
        gc.collect()

        self.assertEqual(audio.reclaim_orphaned_slots(), 0)
        self.assertIn(slot, audio._slot_in_use)
        self.assertNotIn(slot, audio._slot_pool)
        self.assertIsNotNone(live)

    def test_the_slots_summary_follows_a_surviving_holder(self):
        """After a holder leaves, the summary names one that is still here.

        The summary is what the sweep reads first; leaving it on the holder
        that just went is how a live room's slot came to look orphaned.
        """
        audio = bare_audio()
        setting = (("decay_time", 3.0),)
        first = self.Holder()
        second = self.Holder()
        slot = audio.lease_effect("EAXREVERB", setting, "room:a",
                                  ref=first, kind="room")
        audio.lease_effect("EAXREVERB", setting, "room:b",
                           ref=second, kind="room")

        audio.release_effect_lease("EAXREVERB", setting, "room:a")
        self.assertIs(audio._slot_hold[slot][2](), second)

        # A holder nobody can see through a weakref (a PA speaker) is recorded
        # as exactly that, never as a dead holder -- and the sweep leaves a
        # slot whose remaining holder cannot be seen exactly where it is.
        unseen = audio.lease_effect("EAXREVERB", setting, "room:pa",
                                    kind="room")
        self.assertIs(unseen, slot)
        audio.release_effect_lease("EAXREVERB", setting, "room:b")
        del second
        gc.collect()
        self.assertIsNone(audio._slot_hold[slot][2])
        self.assertEqual(audio.reclaim_orphaned_slots(), 0)
        self.assertIn(slot, audio._slot_in_use)
        self.assertNotIn(slot, audio._slot_pool)

    def test_the_last_holder_leaving_still_frees_the_slot(self):
        audio = bare_audio()
        setting = (("decay_time", 3.0),)
        first = self.Holder()
        second = self.Holder()
        slot = audio.lease_effect("EAXREVERB", setting, "room:a",
                                  ref=first, kind="room")
        audio.lease_effect("EAXREVERB", setting, "room:b",
                           ref=second, kind="room")
        del first
        gc.collect()
        self.assertEqual(audio.reclaim_orphaned_slots(), 0)

        del second
        gc.collect()
        self.assertEqual(audio.reclaim_orphaned_slots(), 1)
        self.assertNotIn(slot, audio._slot_in_use)
        self.assertIn(slot, audio._slot_pool)


if __name__ == "__main__":
    unittest.main()
