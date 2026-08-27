import unittest
from types import SimpleNamespace


class _QueuedGame:
    def __init__(self):
        self.pending = []

    def put(self, callback):
        self.pending.append(callback)


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
                player=SimpleNamespace(move=lambda *args, **kwargs: None),
                parser=SimpleNamespace(load=lambda *args, **kwargs: cache_events.append("parse")),
            )
            handler.cache_events = cache_events
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

        marks = []
        handler = make_handler("oldmap")
        handler._apply_parse_map(
            {"name": "oldmap", "data": {}, "x": 0, "y": 0, "z": 0}
        )
        self.assertEqual(marks, [True])
        self.assertEqual(handler.cache_events, ["parse"])
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

        map_obj = Map.__new__(Map)
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


if __name__ == "__main__":
    unittest.main()
