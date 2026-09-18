import unittest
from types import SimpleNamespace
from unittest.mock import patch


class _QueuedGame:
    def __init__(self):
        self.pending = []

    def put(self, callback):
        self.pending.append(callback)


def bare_map():
    """A Map built by attribute (no __init__, so no real map audio).

    The two piano warm-up timer fields come along because destroy() is the
    same code the game runs: it retires a warm-up still waiting to run.
    """
    from libs.world_map import Map

    map_obj = Map.__new__(Map)
    map_obj._piano_backfill_id = None
    map_obj._piano_warmup_token = 0
    return map_obj


class TestMapAudioThreadOwnership(unittest.TestCase):
    def test_map_lifecycle_handlers_only_apply_from_game_queue(self):
        from libs.event_handeler import EventHandeler

        handler = EventHandeler.__new__(EventHandeler)
        handler.game = _QueuedGame()

        cases = (
            ("parse_map", "_apply_parse_map"),
            ("update_map", "_apply_update_map"),
            ("rebuild_elements", "_apply_rebuild_elements"),
            ("spawn_entity", "_apply_spawn_entity"),
            ("remove_entity", "_apply_remove_entity"),
        )
        for public_name, apply_name in cases:
            with self.subTest(event=public_name):
                handler.game.pending.clear()
                applied = []
                setattr(handler, apply_name, lambda data, applied=applied: applied.append(data))
                packet = {"event": public_name}
                getattr(handler, public_name)(packet)
                self.assertEqual(applied, [])
                self.assertEqual(len(handler.game.pending), 1)
                handler.game.pending.pop(0)()
                self.assertEqual(applied, [packet])

    def test_early_megaphone_lock_snapshot_is_not_lost(self):
        from libs.event_handeler import EventHandeler

        handler = EventHandeler.__new__(EventHandeler)
        handler.gameplay = SimpleNamespace()
        state = {"owner": "alice", "owners": ["alice", "bob"]}
        handler.megaphone_lock_state(state)
        self.assertEqual(handler.gameplay._pending_megaphone_lock_state, state)

        handler.gameplay.megaphone = SimpleNamespace(lock_owner=None, lock_owners=set())
        handler.megaphone_lock_state(state)
        self.assertEqual(handler.gameplay.megaphone.lock_owner, "alice")
        self.assertEqual(handler.gameplay.megaphone.lock_owners, {"alice", "bob"})

    def test_staff_permission_refresh_updates_live_client_flags(self):
        from libs.event_handeler import EventHandeler

        handler = EventHandeler.__new__(EventHandeler)
        handler.gameplay = SimpleNamespace(
            is_staff=False, is_builder=False, is_technician=False,
            can_broadcast_megaphone=False,
        )
        handler.staff_permissions({
            "is_staff": True,
            "is_builder": True,
            "is_technician": False,
            "can_broadcast_megaphone": True,
        })
        self.assertTrue(handler.gameplay.is_staff)
        self.assertTrue(handler.gameplay.is_builder)
        self.assertFalse(handler.gameplay.is_technician)
        self.assertTrue(handler.gameplay.can_broadcast_megaphone)


class TestMapReloadResourceOwnership(unittest.TestCase):
    def test_in_place_update_keeps_live_instrument_voices(self):
        """Reload Map Data must not reset Piano/Drums like a map transition."""
        from libs.event_handeler import EventHandeler

        resets = []
        audio = SimpleNamespace(
            apply_filter=lambda *args, **kwargs: None,
            piano=SimpleNamespace(reset_for_map_change=lambda: resets.append("piano")),
            drums=SimpleNamespace(reset_for_map_change=lambda: resets.append("drums")),
        )
        handler = EventHandeler.__new__(EventHandeler)
        handler.game = SimpleNamespace(
            automations=[], audio_mngr=audio, exclude_water=set(),
            ignore_others_water=False,
            network=SimpleNamespace(send=lambda *args, **kwargs: None),
        )
        handler.gameplay = SimpleNamespace(
            player=SimpleNamespace(in_water=False, x=1, y=2, z=3, move=lambda *args, **kwargs: None),
            map=SimpleNamespace(entities={}),
            parser=SimpleNamespace(load=lambda *args, **kwargs: None),
        )
        handler._begin_map_audio_reload = lambda: None
        handler._finish_map_audio_reload = lambda: None

        handler._apply_update_map({"data": {}})
        self.assertEqual(resets, [])

    def test_full_parse_requests_jukebox_resync_before_grace_sweep(self):
        """A Reload Map Data parse must preserve a live server relay."""
        from libs import consts
        from libs.event_handeler import EventHandeler

        sent = []
        handler = EventHandeler.__new__(EventHandeler)
        handler.game = SimpleNamespace(
            automations=[],
            audio_mngr=SimpleNamespace(apply_filter=lambda *args, **kwargs: None),
            exclude_water=set(),
            network=SimpleNamespace(send=lambda *args: sent.append(args)),
        )
        handler.gameplay = SimpleNamespace(
            voice_channels={},
            player=SimpleNamespace(move=lambda *args, **kwargs: None),
            parser=SimpleNamespace(load=lambda *args, **kwargs: None),
        )
        handler._begin_map_audio_reload = lambda: None
        handler._finish_map_audio_reload = lambda: None
        handler._reset_instruments_for_map_change = lambda: None
        handler._stop_jukebox_players_for_map_change = lambda same_map=False: None

        handler._apply_parse_map({"data": {}, "x": 0, "y": 0, "z": 0})
        self.assertIn((consts.CHANNEL_MISC, "jukebox_resync"), sent)

    def test_parse_map_stops_jukebox_immediately_on_real_transition(self):
        """parse_map naming a DIFFERENT map must stop all old-map jukebox
        audio synchronously (no 4s mark-and-sweep grace, no ghost tail)."""
        from libs.event_handeler import EventHandeler

        marks = []
        handler = EventHandeler.__new__(EventHandeler)
        handler.gameplay = SimpleNamespace(
            map_name="oldmap",
            jukebox_state={},
            jukebox_player=SimpleNamespace(
                control_serial=7,
                mark_pending_map_change=lambda serial: marks.append(serial),
                stop_all=lambda: marks.append("stop_all"),
            ),
        )
        handler._stop_jukebox_players_for_map_change(same_map=False)
        self.assertEqual(marks, ["stop_all"])
        self.assertEqual(handler.gameplay.jukebox_state, {"jukeboxes": {}})

    def test_parse_map_keeps_mark_and_sweep_for_same_map(self):
        """A same-name full reparse keeps the graceful mark-and-sweep so a
        re-broadcast jukebox_play can preserve the stream seamlessly."""
        from libs.event_handeler import EventHandeler

        marks = []
        handler = EventHandeler.__new__(EventHandeler)
        handler.gameplay = SimpleNamespace(
            map_name="oldmap",
            jukebox_state={},
            jukebox_player=SimpleNamespace(
                control_serial=7,
                mark_pending_map_change=lambda serial: marks.append(serial),
                stop_all=lambda: marks.append("stop_all"),
            ),
        )
        handler._stop_jukebox_players_for_map_change(same_map=True)
        self.assertEqual(marks, [7])

    def test_apply_parse_map_detects_real_transition_by_name(self):
        """_apply_parse_map compares the incoming map name against the current
        one: different name -> immediate stop, same name -> mark-and-sweep."""
        from libs.event_handeler import EventHandeler

        def make_handler(previous_name):
            handler = EventHandeler.__new__(EventHandeler)
            cache_events = []
            illusion_events = []
            samples = SimpleNamespace(clear=lambda: cache_events.append("clear"))
            handler.game = SimpleNamespace(
                automations=[],
                audio_mngr=SimpleNamespace(
                    apply_filter=lambda *args, **kwargs: None,
                    instrument_samples=samples,
                ),
                exclude_water=set(),
                network=SimpleNamespace(send=lambda *args, **kwargs: None),
            )
            handler.gameplay = SimpleNamespace(
                voice_channels={},
                map_name=previous_name,
                jukebox_state={},
                warlock_intro_illusion=SimpleNamespace(
                    destroy=lambda: illusion_events.append("destroy")
                ),
                player=SimpleNamespace(move=lambda *args, **kwargs: None),
                parser=SimpleNamespace(load=lambda *args, **kwargs: cache_events.append("parse")),
            )
            handler.cache_events = cache_events
            handler.illusion_events = illusion_events
            handler._begin_map_audio_reload = lambda: None
            handler._finish_map_audio_reload = lambda: None
            handler._reset_instruments_for_map_change = lambda: None
            handler._stop_jukebox_players_for_map_change = (
                lambda same_map=False: marks.append(same_map)
            )
            return handler

        marks = []
        handler = make_handler("oldmap")
        handler._apply_parse_map(
            {"name": "newmap", "data": {}, "x": 0, "y": 0, "z": 0}
        )
        self.assertEqual(marks, [False])
        self.assertEqual(handler.cache_events, ["clear", "parse"])
        self.assertEqual(handler.illusion_events, ["destroy"])

        marks = []
        handler = make_handler("oldmap")
        handler._apply_parse_map(
            {"name": "oldmap", "data": {}, "x": 0, "y": 0, "z": 0}
        )
        self.assertEqual(marks, [True])
        self.assertEqual(handler.cache_events, ["parse"])
        self.assertEqual(handler.illusion_events, [])
        # The new map name is remembered for the next comparison.
        self.assertEqual(handler.gameplay.map_name, "oldmap")

        # First map load (no previous name): treated as a transition (nothing
        # to stop) and the name is recorded.
        marks = []
        handler = make_handler(None)
        handler._apply_parse_map(
            {"name": "first", "data": {}, "x": 0, "y": 0, "z": 0}
        )
        self.assertEqual(marks, [False])
        self.assertEqual(handler.gameplay.map_name, "first")
        self.assertEqual(handler.cache_events, ["clear", "parse"])
        self.assertEqual(handler.illusion_events, ["destroy"])

    def test_resync_preserves_existing_entity_and_continuous_sources(self):
        from libs.world_map import Map

        class Existing:
            is_vehicle = False

            def __init__(self):
                self.hp = 100
                self.destroyed = False
                self.moves = []
                self.music_source = object()
                self.vc_source = object()

            def move(self, x, y, z, play_sound=True):
                self.moves.append((x, y, z, play_sound))

            def destroy(self):
                self.destroyed = True

        existing = Existing()
        map_obj = Map.__new__(Map)
        map_obj.entities = {"alice": existing}
        result = map_obj.spawn_entity(
            "alice", 4, 5, 6, hp=90, preserve_existing=True
        )
        self.assertIs(result, existing)
        self.assertFalse(existing.destroyed)
        self.assertEqual(existing.moves, [(4, 5, 6, False)])
        self.assertEqual(existing.hp, 100)  # resync packet carries no HP authority

    def test_fresh_stationary_entity_syncs_room_reverb_on_spawn(self):
        from libs.world_map import Map

        class Spawned:
            is_vehicle = False

            def __init__(self):
                self.sync_calls = 0

            def sync_reverb(self):
                self.sync_calls += 1
                return True

        spawned = Spawned()
        map_obj = Map.__new__(Map)
        map_obj.game = object()
        map_obj.entities = {}
        with patch("libs.world_map.entity.Entity", return_value=spawned):
            result = map_obj.spawn_entity("stationary-animal", 4, 5, 6)

        self.assertIs(result, spawned)
        self.assertIs(map_obj.entities["stationary-animal"], spawned)
        self.assertEqual(spawned.sync_calls, 1)

    def test_fifty_resyncs_keep_the_same_voice_and_music_sources(self):
        from libs.world_map import Map

        class Existing:
            is_vehicle = False
            hp = 100

            def __init__(self):
                self.music_source = object()
                self.vc_source = object()
                self.destroy_count = 0

            def move(self, *args, **kwargs):
                pass

            def destroy(self):
                self.destroy_count += 1

        existing = Existing()
        music_source = existing.music_source
        voice_source = existing.vc_source
        map_obj = Map.__new__(Map)
        map_obj.entities = {"alice": existing}
        for index in range(50):
            result = map_obj.spawn_entity(
                "alice", index, index + 1, 0, preserve_existing=True
            )
            self.assertIs(result, existing)
        self.assertIs(existing.music_source, music_source)
        self.assertIs(existing.vc_source, voice_source)
        self.assertEqual(existing.destroy_count, 0)

    def test_map_sources_are_disposed_before_reverb_slots(self):
        from libs.world_map import Map

        order = []

        class AudioObject:
            sound = None
            playing = True

            def __init__(self, name):
                self.name = name

            def destroy(self):
                order.append(self.name)

        class Entity:
            def detach_environment_effects(self):
                order.append("entity_detach")

        map_obj = bare_map()
        map_obj.entities = {"player": Entity()}
        map_obj.reverb_list = [AudioObject("reverb_release")]
        map_obj.ambience_list = [AudioObject("ambience_destroy")]
        map_obj.pannable_list = [AudioObject("pannable_destroy")]
        map_obj.source_list = [AudioObject("source_destroy")]
        map_obj.music_list = [AudioObject("music_destroy")]
        map_obj.tile_list = []
        map_obj.zone_list = []
        map_obj.door_list = []
        map_obj.wallbuy_list = []
        map_obj.interactable_list = []
        map_obj.perk_machine_list = []
        map_obj.minigame_table_list = []
        map_obj.travel_point_list = []
        map_obj.jukebox_list = []
        map_obj.megaphone_speakers = []

        map_obj.destroy(destroy_entities=False)
        release_index = order.index("reverb_release")
        self.assertLess(order.index("entity_detach"), release_index)
        for name in (
            "ambience_destroy", "pannable_destroy", "source_destroy", "music_destroy"
        ):
            self.assertLess(order.index(name), release_index)


class TestPersistentStreamReverbDetach(unittest.TestCase):
    def test_jukebox_detaches_both_channels_without_stopping_stream(self):
        from libs.jukebox import JukeboxPlayer

        left, right = object(), object()
        sends = []
        audio = SimpleNamespace(
            efx=SimpleNamespace(send=lambda source, index, slot: sends.append((source, index, slot)))
        )
        game = SimpleNamespace(audio_mngr=audio)
        player = JukeboxPlayer(game)
        streamer = SimpleNamespace(reverb_slot=object())
        player.players["box"] = {
            "source": left,
            "secondary_source": right,
            "streamer": streamer,
        }
        player.detach_reverb()
        self.assertEqual(sends, [(left, 0, None), (right, 0, None)])
        self.assertIn("box", player.players)
        self.assertIsNone(streamer.reverb_slot)

    def test_music_bot_detach_keeps_stream_source_alive(self):
        from libs.music_bot import MapMusicBot

        source = object()
        sends = []
        bot = MapMusicBot.__new__(MapMusicBot)
        bot.stream_source = source
        bot._current_reverb_slot = object()
        bot.game = SimpleNamespace(
            audio_mngr=SimpleNamespace(
                efx=SimpleNamespace(send=lambda src, index, slot: sends.append((src, index, slot)))
            )
        )
        bot._detach_map_reverb()
        self.assertEqual(sends, [(source, 0, None)])
        self.assertIs(bot.stream_source, source)
        self.assertIsNone(bot._current_reverb_slot)

    def test_megaphone_detaches_map_send_from_every_live_source(self):
        from libs.systems.megaphone_system import MegaphoneManager

        main, reflection, remote = object(), object(), object()
        sends = []
        manager = MegaphoneManager.__new__(MegaphoneManager)
        manager.game = SimpleNamespace(
            audio_mngr=SimpleNamespace(
                efx=SimpleNamespace(send=lambda src, index, slot: sends.append((src, index, slot)))
            )
        )
        manager.gameplay = SimpleNamespace(current_player_reverb_slot=object())
        manager.speaker_data = [{"source": main, "reflection_source": reflection}]
        manager.player_sources = {7: {"sources": [remote]}}
        manager.current_player_reverb_slot = object()
        manager.detach_map_reverb()
        self.assertEqual(
            sends,
            [(main, 3, None), (reflection, 3, None), (remote, 3, None)],
        )
        self.assertEqual(manager.current_player_reverb_slot, "UNINIT")
        self.assertIsNone(manager.gameplay.current_player_reverb_slot)



class TestReverbSlotRecovery(unittest.TestCase):
    """Reverb zones must survive a momentary effect-slot pool shortage.

    Room reverbs borrow their EAXREVERB slot from the AudioManager pool at map
    load. An in-place map reload used to re-create the zones while the old
    megaphone speaker set (12-13 slots on big PA maps) and every remote
    player's EQ/distortion slots were still held, so a zone could receive None
    and stay silent until a client restart. These tests cover the fixes:
    megaphone speaker slots are released BEFORE the parser re-creates reverbs,
    and a starved zone retries allocation on demand.
    """

    def test_megaphone_release_speaker_slots_returns_efx_resources(self):
        from libs.systems.megaphone_system import MegaphoneManager

        released_slots = []
        released_filters = []
        sends = []

        class FakeAudio:
            efx = SimpleNamespace(
                send=lambda src, index, slot: sends.append((src, index, slot))
            )

            def release_effect_slot(self, slot):
                released_slots.append(slot)

            def release_filter(self, flt):
                released_filters.append(flt)

        manager = MegaphoneManager.__new__(MegaphoneManager)
        manager.game = SimpleNamespace(audio_mngr=FakeAudio())
        reverb_slot, filter_obj = object(), object()
        source, reflection = object(), object()
        manager.sources = [source]
        manager.speaker_data = [{
            "source": source,
            "reflection_source": reflection,
            "reverb_slot": reverb_slot,
            "filter": filter_obj,
        }]
        manager.player_sources = {}
        manager.release_speaker_slots()
        self.assertEqual(released_slots, [reverb_slot])
        self.assertEqual(released_filters, [filter_obj])
        # Every aux send on the old sources is detached before the slot reuse.
        self.assertTrue(all(slot is None for _, _, slot in sends))
        self.assertEqual(manager.sources, [])
        self.assertEqual(manager.speaker_data, [])
        # Releasing an already-empty speaker set must be a safe no-op.
        manager.release_speaker_slots()

    def test_begin_reload_releases_speaker_slots_before_parser(self):
        from libs.event_handeler import EventHandeler

        events = []

        class FakeMegaphone:
            def detach_map_reverb(self):
                events.append("detach")

            def release_speaker_slots(self):
                events.append("release_speakers")

        class FakePlayer:
            def detach_environment_effects(self):
                events.append("player_detach")

        handler = EventHandeler.__new__(EventHandeler)
        handler.gameplay = SimpleNamespace(
            player=FakePlayer(),
            megaphone=FakeMegaphone(),
        )
        handler._begin_map_audio_reload()
        self.assertEqual(events, ["player_detach", "detach", "release_speakers"])

    def test_starved_reverb_zone_retries_slot_on_demand(self):
        from libs.world_map import Reverb

        calls = []
        slot_b = object()

        class FakeAudio:
            """The pool's lease face of the API: one slot per setting."""

            def lease_effect(self, etype, params, label, ref=None, kind=None):
                calls.append((etype, tuple(params)))
                # First allocation attempt is starved (pool exhausted); the
                # retry succeeds with a freshly freed slot.
                return None if len(calls) == 1 else slot_b

            def release_effect_lease(self, etype, params, label):
                return True

        class FakeGame:
            audio_mngr = FakeAudio()

        class FakeMap:
            game = FakeGame()

        rev = Reverb(
            FakeMap(), "zone1",
            0, 5, 0, 5, 0, 5,
            1.49, 1.0, 1.0, 0.32, 0.89, 1.0, 0.83, 1.0,
            0.05, 0.007, (0.0, 0.0, 0.0),
            1.26, 0.011, (0.0, 0.0, 0.0),
            0.25, 0.0, 0.25, 0.0, 0.994, 5000.0, 250.0, 0.0,
        )
        # Initial borrow failed -> zone is dry.
        self.assertIsNone(rev.reverb)
        # On-demand retry borrows the same parameter set and succeeds.
        slot = rev.ensure_slot(force=True)
        self.assertIs(slot, slot_b)
        self.assertIs(rev.reverb, slot_b)
        self.assertEqual(len(calls), 2)
        etype, params = calls[0]
        self.assertEqual(etype, "EAXREVERB")
        # The retry asked for the exact same setting as the first attempt, so
        # a room that recovers shares the slot of every room built like it.
        self.assertEqual(params, calls[1][1])
        # A healthy zone returns its slot immediately without re-allocating.
        self.assertIs(rev.ensure_slot(force=True), slot_b)
        self.assertEqual(len(calls), 2)

    def test_sync_reverb_applies_retried_slot_on_zone(self):
        from libs.objects.entity import Entity

        applied = []
        slot = object()

        class FakeZone:
            reverb = None

            def ensure_slot(self, force=False):
                self.reverb = slot
                return self.reverb

        class FakeMap:
            def get_reverb_at(self, x, y, z):
                return FakeZone()

        ent = Entity.__new__(Entity)
        ent.map = FakeMap()
        ent.x = 0
        ent.y = 0
        ent.z = 0
        ent.soundgroup = SimpleNamespace(
            filter=[],
            apply_effect=lambda s, idx: applied.append((s, idx)),
        )
        ent._player = False
        ent.sync_reverb()
        self.assertEqual(applied, [(slot, 0)])



class AMapChangeKeepsTheListenersRoomTests(unittest.TestCase):
    """What a map change does to reverb on a step, a shot or a voice.

    Everything walked, fired or said on this machine plays through a
    SoundGroup (the listener's, or a megaphone/PA source), and its send 0 is
    the room that object stands in. A reload detaches those sends before the
    old map hands its slots back -- so the property worth pinning is that the
    reload path itself binds the NEW map's room again, and that a map which
    ends up with no room leaves the listener dry instead of ringing in the
    room that just went away. Both are run through the real
    ``_apply_update_map``, with the real pool arithmetic underneath.
    """

    class Group:
        def __init__(self):
            # The real shape: one entry per send index, as a list.
            self.sends = [None, None, None, None]
            self.position = None

        def apply_effect(self, slot, sendnum=0, filter=None):
            self.sends[sendnum] = slot

        def apply_filter(self, filter_obj, replace=True, clear=False):
            self.filter = filter_obj

        def play(self, path, **kwargs):
            # The step itself is not what this class is about; the send 0 that
            # shapes it is.
            self.played = getattr(self, "played", []) + [path]

    class Slot:
        def __init__(self, name):
            self.name = name
            self.effect = None

        def unload(self):
            self.effect = None

    def _audio(self, slots=8):
        """A real AudioManager with a fake device: only the pool is real."""
        from libs.audio_manager import AudioManager

        class FakeEfx:
            created = 0

            def gen_effect(self, type):
                return SimpleNamespace(set=lambda *a: None)

            def send(self, source, index, slot, filter=None):
                pass

        class _TestAudio(AudioManager):
            # The armor INCREFs an effect wrapper so cyal's dealloc can never
            # run; a fake has no such dealloc.
            def _armor_filter(self, filter_obj, site, label="filter"):
                return None

        audio = _TestAudio.__new__(_TestAudio)
        audio._slot_pool = [self.Slot(f"slot{i}") for i in range(slots)]
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
        audio.filter = {}
        return audio

    def _scene(self, group, map_obj=None):
        """A listener, a real map and the handler that reloads it."""
        from libs.event_handeler import EventHandeler
        from libs.objects.entity import Entity

        audio = self._audio()
        audio.soundgroups = {group}
        if map_obj is None:
            map_obj = bare_map()
            map_obj.game = SimpleNamespace(audio_mngr=audio)
            map_obj.reverb_list = []
            map_obj.entities = {}
            # A floor to stand on, so the listener is not read as falling and
            # the reload's move() stays the plain walk it is in game.
            map_obj.tile_list = [SimpleNamespace(
                in_bound=lambda x, y, z: True, tiletype="grass")]

        player = Entity.__new__(Entity)
        player.map = map_obj
        player.name = "listener"
        player.x = player.y = player.z = 1
        player.soundgroup = group
        player.on_move = None
        player._player = False
        player.falling = False
        map_obj.player = player

        game = SimpleNamespace(
            automations=[],
            exclude_water=set(),
            ignore_others_water=False,
            audio_mngr=audio,
            network=SimpleNamespace(send=lambda *a, **k: None),
        )
        player.game = game
        handler = EventHandeler.__new__(EventHandeler)
        handler.game = game
        handler.gameplay = SimpleNamespace(
            player=player,
            map=map_obj,
            megaphone=None,
            music_bot=None,
            jukebox_player=None,
        )
        return handler, audio, player, map_obj

    def _room(self, map_obj, room_id="hall"):
        map_obj.spawn_reverb(0, 8, 0, 8, 0, 4, id=room_id)
        return map_obj.reverb_list[0]

    def test_the_new_maps_room_is_bound_back_by_the_reload_itself(self):
        group = self.Group()
        handler, audio, player, map_obj = self._scene(group)
        self._room(map_obj)
        player.sync_reverb()
        first_slot = map_obj.reverb_list[0].reverb
        self.assertIs(group.sends[0], first_slot)

        seen = []
        parser = SimpleNamespace()

        def load(data, in_place=True):
            # Exactly what a rebuild does: the old elements are destroyed (and
            # hand their slots back) before the new ones are spawned.
            seen.append(("before", group.sends[0]))
            map_obj.reverb_list[0].destroy()
            map_obj.reverb_list = []
            seen.append(("torn_down", group.sends[0]))
            self._room(map_obj)
            seen.append(("rebuilt", group.sends[0]))

        parser.load = load
        handler.gameplay.parser = parser
        handler._apply_update_map({"data": {}})

        # The send was detached when the old room let go -- never left pointing
        # at a slot another room is about to stand on -- and the reload bound
        # the new map's room before it returned.
        self.assertEqual([step for step, _ in seen],
                         ["before", "torn_down", "rebuilt"])
        self.assertIsNone(seen[1][1])
        self.assertIs(group.sends[0], map_obj.reverb_list[0].reverb)
        self.assertIsNotNone(group.sends[0], "the room must come back")
        # The old room handed its slot back and the new one holds exactly one:
        # a reload may not leak a slot per rebuild (that is what made a client
        # run out and need a restart).
        self.assertEqual(len(audio._slot_in_use), 1)
        self.assertIs(audio._slot_in_use[0], map_obj.reverb_list[0].reverb)

    def test_a_rebuilt_map_without_a_room_leaves_the_listener_dry(self):
        group = self.Group()
        handler, audio, player, map_obj = self._scene(group)
        self._room(map_obj)
        player.sync_reverb()
        self.assertIsNotNone(group.sends[0])

        def load(data, in_place=True):
            map_obj.reverb_list[0].destroy()
            map_obj.reverb_list = []

        handler.gameplay.parser = SimpleNamespace(load=load)
        handler._apply_update_map({"data": {}})

        # No room on this map is dry, not a tail of the room that went away.
        self.assertIsNone(group.sends[0])
        self.assertEqual(len(audio._slot_pool), 8)


if __name__ == "__main__":
    unittest.main()
