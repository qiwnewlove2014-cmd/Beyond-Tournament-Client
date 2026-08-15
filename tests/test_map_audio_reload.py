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


class TestMapReloadResourceOwnership(unittest.TestCase):
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
        player.players["box"] = {
            "source": left,
            "secondary_source": right,
            "streamer": object(),
        }
        player.detach_reverb()
        self.assertEqual(sends, [(left, 0, None), (right, 0, None)])
        self.assertIn("box", player.players)

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
