"""Armor, heard: the cloth on a walker's steps, and how it is picked.

Armor cannot be seen. In a game played by ear the whole of it is what it sounds
like, so what a listener actually gets has to be pinned:

  * **a step in armor is two sounds, not one**. The surface still says what the
    walker is walking on -- armor is added on top at its own quiet volume, never
    instead of it -- and only the *pitch* of the cloth answers a sprint, because
    there is no run folder of cloth samples to choose from. A coat does not make
    gravel silent, and a piece of armor that muted the floor would be a
    different feature;
  * **no cloth sample comes round again until the folder has dealt all of them**.
    That is a shuffle bag, not `random_item` (which stutters) and not
    `get_next_cycle_item` (which is the same six in the same order forever). The
    bag is per wearer, so three armored players on one map do not deal from one
    deck, and the seam between two rounds of the folder is not a repeat either;
  * **every other player hears it too**, and the piece arrives with the step
    itself: what the server says a walker is wearing is applied *before* the move
    that carries it, a step without the field takes the armor off by itself (a
    break, a hidden walker, an older Server), and an entity that does not know
    the field at all still walks;
  * **the samples are what the game's own decoder reads**. Every sound in this
    game goes through libvorbisfile and nothing else, so an Opus export dropped
    in beside the right name opens as *nothing at all* -- and a silent armor that
    looks like a broken speaker is exactly the report this file exists to
    prevent;
  * **a wall says the name of the piece, not the id the map stores**, because
    "armor colon plate underscore armor" is what a player heard before.

The client half of the armor is the state the server sends (``ArmorManager``),
the two fields every entity carries (``Entity.set_armor_cloth``), the layer over
the step (``Entity._play_armor_cloth``, reached through the real ``Entity.move``)
and the wall label -- all of them the shipping code, driven directly.
"""

import os
import random
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from libs import path_utils  # noqa: E402
from libs.armor import ArmorManager  # noqa: E402
from libs.event_handeler import EventHandeler  # noqa: E402
from libs.gameplay import Gameplay  # noqa: E402
from libs.objects.entity import Entity  # noqa: E402

ARMOR_FOLDER = Path(__file__).resolve().parent.parent / "data" / "items" / "Armor"
CLOTH_SAMPLES = sorted(ARMOR_FOLDER.glob("cloth*.ogg"))


def clear_bags():
    """A bag remembers what a wearer has heard, so a test starts from none."""
    path_utils._bags.clear()


class ShuffleBagTests(unittest.TestCase):
    """The picking rule itself: random, but with no repeat until the folder is out."""

    def setUp(self):
        clear_bags()

    def tearDown(self):
        clear_bags()

    def test_every_cloth_sample_is_dealt_before_any_of_them_comes_round_again(self):
        drawn = [
            os.path.basename(path_utils.bag_item(str(ARMOR_FOLDER), key="p1", prefix="cloth"))
            for _ in range(len(CLOTH_SAMPLES))
        ]
        self.assertEqual(sorted(drawn), [sample.name for sample in CLOTH_SAMPLES])

    def test_no_sample_plays_twice_in_a_row_across_the_seam_of_two_rounds(self):
        for _ in range(3):
            drawn = [
                os.path.basename(path_utils.bag_item(str(ARMOR_FOLDER), key="p1", prefix="cloth"))
                for _ in range(len(CLOTH_SAMPLES) * 2)
            ]
            for index in range(1, len(drawn)):
                self.assertNotEqual(drawn[index], drawn[index - 1], drawn)

    def test_the_order_comes_from_the_shuffle_and_not_from_a_fixed_walk(self):
        # A bag deals the shuffled pool from the end, so a shuffle that reverses
        # the pool deals the folder's own order -- and a bag that walked a fixed
        # order of its own (sorted, or the order the folder happens to list in)
        # could not be moved by touching the shuffle at all.
        with mock.patch.object(random, "shuffle", lambda pool: pool.reverse()):
            drawn = [
                os.path.basename(path_utils.bag_item(str(ARMOR_FOLDER), key="p1", prefix="cloth"))
                for _ in range(len(CLOTH_SAMPLES))
            ]
        self.assertEqual(drawn, [sample.name for sample in CLOTH_SAMPLES])
        clear_bags()
        unshuffled = [
            os.path.basename(path_utils.bag_item(str(ARMOR_FOLDER), key="p1", prefix="cloth"))
            for _ in range(len(CLOTH_SAMPLES))
        ]
        self.assertNotEqual(unshuffled, drawn,
                            "two bags in a row dealt the same order, which is what a fixed walk looks like")

    def test_two_wearers_do_not_deal_from_one_deck(self):
        first = [path_utils.bag_item(str(ARMOR_FOLDER), key="wearer-a", prefix="cloth")
                 for _ in range(len(CLOTH_SAMPLES) - 1)]
        second = [path_utils.bag_item(str(ARMOR_FOLDER), key="wearer-b", prefix="cloth")
                  for _ in range(len(CLOTH_SAMPLES))]
        self.assertEqual(len(first), len(CLOTH_SAMPLES) - 1)
        self.assertEqual(sorted(os.path.basename(path) for path in second),
                         [sample.name for sample in CLOTH_SAMPLES])

    def test_only_the_samples_that_are_that_kind_of_sound_are_in_the_bag(self):
        drawn = {
            os.path.basename(path_utils.bag_item(str(ARMOR_FOLDER), key="p1", prefix="cloth"))
            for _ in range(len(CLOTH_SAMPLES))
        }
        self.assertEqual(len(drawn), len(CLOTH_SAMPLES))
        self.assertTrue(all(name.startswith("cloth") for name in drawn), drawn)
        # The impacts and the equip sound live in the same folder and are never
        # a footstep.
        self.assertNotIn("equip.ogg", drawn)
        self.assertNotIn("dodge3.ogg", drawn)

    def test_nothing_to_play_is_an_empty_answer_rather_than_a_crash(self):
        self.assertEqual(path_utils.bag_item(str(ARMOR_FOLDER / "nope"), key="p", prefix="cloth"), "")
        self.assertEqual(path_utils.bag_item(str(ARMOR_FOLDER), key="p", prefix="nothing"), "")
        self.assertEqual(path_utils.bag_item("", key="p"), "")

    def test_a_bag_with_one_sample_keeps_answering_with_it(self):
        with mock.patch.object(path_utils.os, "listdir", lambda folder: ["cloth3.ogg"]):
            drawn = [path_utils.bag_item(str(ARMOR_FOLDER), key="p1", prefix="cloth") for _ in range(3)]
        self.assertEqual(drawn, [f"{ARMOR_FOLDER}/cloth3.ogg"] * 3)


def armor_piece(sounds_path="items/Armor", volume=60, cloth=True):
    return {"id": "plate_armor", "name": "Plate Armor", "sounds_path": sounds_path,
            "durability": 450, "max_durability": 450, "cloth": cloth, "cloth_volume": volume}


class ArmorStateTests(unittest.TestCase):
    """What the client keeps: the piece the Server says is worn."""

    def make_manager(self):
        player = SimpleNamespace(set_armor_cloth=mock.Mock())
        gameplay = SimpleNamespace(player=player, game=SimpleNamespace())
        return ArmorManager(gameplay), player

    def test_a_piece_the_server_names_is_handed_to_the_walker(self):
        manager, player = self.make_manager()
        manager.equip_armor(armor_piece(volume=70))
        player.set_armor_cloth.assert_called_once_with("items/Armor", 70)
        self.assertEqual(manager.equipped_armor["name"], "Plate Armor")
        self.assertEqual(manager.sounds_path(), "items/Armor")

    def test_a_piece_worn_silently_takes_the_cloth_off_the_steps(self):
        manager, player = self.make_manager()
        manager.equip_armor(armor_piece(cloth=False))
        player.set_armor_cloth.assert_called_once_with(None)

    def test_taking_it_off_silences_the_steps(self):
        manager, player = self.make_manager()
        manager.equip_armor(armor_piece())
        player.set_armor_cloth.reset_mock()
        manager.unequip_armor()
        player.set_armor_cloth.assert_called_once_with(None)
        self.assertIsNone(manager.equipped_armor)
        self.assertIsNone(manager.sounds_path())

    def test_a_payload_that_is_not_a_piece_is_not_remembered(self):
        manager, player = self.make_manager()
        manager.equip_armor(None)
        manager.equip_armor("shield:wood1")
        player.set_armor_cloth.assert_called_with(None)
        self.assertIsNone(manager.equipped_armor)

    def test_the_packet_handlers_route_to_the_manager(self):
        handler = EventHandeler.__new__(EventHandeler)
        manager = mock.Mock()
        handler.gameplay = SimpleNamespace(armor_mngr=manager)
        handler.equip_armor({"name": "Plate Armor"})
        manager.equip_armor.assert_called_once_with({"name": "Plate Armor"})
        handler.unequip_armor({})
        manager.unequip_armor.assert_called_once_with()


class WalkInArmorTests(unittest.TestCase):
    """The step itself: surface first, cloth over it, from a bag of its own."""

    def setUp(self):
        clear_bags()

    def tearDown(self):
        clear_bags()

    def make_entity(self, name="bob", tile="wood"):
        entity = Entity.__new__(Entity)
        entity.x = entity.y = entity.z = 0.0
        entity.name = name
        entity.falling = False
        entity.player = False
        entity.is_user = False
        entity.on_move = None
        entity.dead = False
        entity.hfacing = 0
        entity.sync_reverb = lambda: None
        entity.soundgroup = SimpleNamespace(position=None)
        entity.game = SimpleNamespace()
        entity.map = SimpleNamespace(
            get_tile_at=lambda x, y, z: tile,
            in_bound=lambda *args: True,
            player=None,
            entities={},
        )
        entity.played = []
        entity.play_sound = lambda sound, **kwargs: entity.played.append((sound, kwargs))
        return entity

    def test_a_step_in_armor_plays_the_surface_and_then_the_cloth(self):
        entity = self.make_entity()
        entity.set_armor_cloth("items/Armor", 55)
        entity.move(1.0, 2.0, 0.0, True, "walk")

        self.assertEqual(len(entity.played), 2, entity.played)
        self.assertEqual(entity.played[0][0], "steps/wood/walk")
        cloth, kwargs = entity.played[1]
        self.assertTrue(cloth.startswith(f"data/items/Armor{os.sep}cloth")
                        or cloth.startswith("data/items/Armor/cloth"), cloth)
        self.assertEqual(kwargs["volume"], 55)
        self.assertEqual(kwargs["cat"], "players")

    def test_the_surface_is_never_replaced_by_the_cloth(self):
        entity = self.make_entity()
        entity.set_armor_cloth("items/Armor", 60)
        entity.move(1.0, 2.0, 0.0, True, "walk")
        self.assertIn("steps/wood/walk", [sound for sound, _kw in entity.played])

    def test_only_the_pitch_of_the_cloth_answers_a_sprint(self):
        # Grass is one of the surfaces with no run samples of its own (the
        # fallback the checklist in docs/footstep_run_samples.md is about), so a
        # sprint there walks -- and the cloth still knows the step was a run.
        entity = self.make_entity(tile="grass")
        entity.set_armor_cloth("items/Armor", 60)
        entity.move(1.0, 2.0, 0.0, True, "run")

        surface, cloth = entity.played[0][1], entity.played[1][1]
        self.assertEqual(entity.played[0][0], "steps/grass/walk")
        self.assertNotIn("pitch", surface)
        self.assertGreater(cloth["pitch"], 1.0, cloth)

    def test_a_piece_worn_silently_adds_nothing_to_the_step(self):
        entity = self.make_entity()
        entity.set_armor_cloth(None, 60)
        entity.set_armor_cloth("items/Armor", 0)
        entity.move(1.0, 2.0, 0.0, True, "walk")
        self.assertEqual([sound for sound, _kw in entity.played], ["steps/wood/walk"])

    def test_a_wearer_with_no_folder_at_all_still_walks(self):
        entity = self.make_entity()
        entity.set_armor_cloth("items/NothingHere", 60)
        entity.move(1.0, 2.0, 0.0, True, "walk")
        self.assertEqual([sound for sound, _kw in entity.played], ["steps/wood/walk"])

    def test_a_still_step_plays_nothing_at_all(self):
        entity = self.make_entity()
        entity.set_armor_cloth("items/Armor", 60)
        entity.move(1.0, 2.0, 0.0, False, "walk")
        self.assertEqual(entity.played, [])

    def test_the_cloth_bag_is_the_walker_s_own(self):
        first, second = self.make_entity(name="bob"), self.make_entity(name="ann")
        for entity in (first, second):
            entity.set_armor_cloth("items/Armor", 60)
        for _ in range(len(CLOTH_SAMPLES)):
            first.move(1.0, 0.0, 0.0, True, "walk")
            second.move(1.0, 0.0, 0.0, True, "walk")

        def cloths(entity):
            return sorted(os.path.basename(sound) for sound, _kw in entity.played
                          if "cloth" in sound)

        self.assertEqual(cloths(first), [sample.name for sample in CLOTH_SAMPLES])
        self.assertEqual(cloths(second), [sample.name for sample in CLOTH_SAMPLES])


class StepPacketTests(unittest.TestCase):
    """The other half of it: hearing somebody *else* walk in armor."""

    def make_handler(self, entity):
        handler = EventHandeler.__new__(EventHandeler)
        handler.gameplay = SimpleNamespace(
            map=SimpleNamespace(entities={"bob": entity}),
            player=SimpleNamespace(name="me"),
        )
        return handler

    def make_walker(self):
        walker = SimpleNamespace(armor_sounds_path=None, armor_cloth_volume=60,
                                 moved=[], faced=[], vfacing=0, bfacing=0)
        walker.set_armor_cloth = Entity.set_armor_cloth.__get__(walker)
        walker.move = lambda *args: walker.moved.append(args)
        walker.face = lambda *args, **kwargs: walker.faced.append((args, kwargs))
        return walker

    def test_a_step_carrying_armor_tells_the_listener_what_to_hear(self):
        walker = self.make_walker()
        self.make_handler(walker)._apply_move({
            "name": "bob", "x": 1, "y": 2, "z": 3,
            "armor_cloth": "items/Armor", "armor_cloth_volume": 70,
        })
        self.assertEqual(walker.armor_sounds_path, "items/Armor")
        self.assertEqual(walker.armor_cloth_volume, 70)
        self.assertEqual(len(walker.moved), 1)

    def test_the_piece_is_set_before_the_step_that_carries_it(self):
        walker = self.make_walker()
        seen = []
        walker.move = lambda *args: seen.append(walker.armor_sounds_path)
        self.make_handler(walker)._apply_move({
            "name": "bob", "x": 1, "y": 2, "z": 3, "armor_cloth": "items/Armor",
        })
        self.assertEqual(seen, ["items/Armor"])

    def test_a_step_without_armor_takes_it_off_by_itself(self):
        walker = self.make_walker()
        walker.armor_sounds_path = "items/Armor"
        self.make_handler(walker)._apply_move({"name": "bob", "x": 1, "y": 2, "z": 3})
        self.assertIsNone(walker.armor_sounds_path)

    def test_a_walker_the_field_means_nothing_to_still_walks(self):
        walker = SimpleNamespace(moved=[], faced=[], vfacing=0, bfacing=0)
        walker.move = lambda *args: walker.moved.append(args)
        walker.face = lambda *args, **kwargs: walker.faced.append((args, kwargs))
        self.make_handler(walker)._apply_move({
            "name": "bob", "x": 1, "y": 2, "z": 3,
            "armor_cloth": "items/Armor", "armor_cloth_volume": 70,
        })
        self.assertEqual(len(walker.moved), 1)


class WallLabelTests(unittest.TestCase):
    """What a wall is called, which is never the id the map stores."""

    def label(self, **fields):
        return Gameplay._wallbuy_label(SimpleNamespace(**fields))

    def test_the_name_the_builder_wrote_is_what_is_said(self):
        self.assertEqual(self.label(displayName="Plate Armor", weaponName="armor:plate_armor"),
                         "Plate Armor")

    def test_a_wall_with_no_name_of_its_own_is_read_out_of_the_id(self):
        self.assertEqual(self.label(weaponName="armor:plate_armor"), "Plate Armor")
        self.assertEqual(self.label(weaponName="shield:iron_shield_1"), "Iron Shield 1")
        self.assertEqual(self.label(weaponName="armor:cloth_armor"), "Cloth Armor")

    def test_a_plain_weapon_is_left_exactly_as_its_file_spells_it(self):
        self.assertEqual(self.label(weaponName="M4A1"), "M4A1")
        self.assertEqual(self.label(weaponName="357_magnum_revolver"), "357_magnum_revolver")

    def test_a_wall_that_sells_nothing_says_so(self):
        self.assertEqual(self.label(weaponName=""), "Weapon Buy")
        self.assertEqual(self.label(weaponName="", displayName=""), "Weapon Buy")


class SampleFileTests(unittest.TestCase):
    """The files themselves: the game decodes Vorbis and nothing else."""

    def test_the_cloth_the_code_deals_from_is_really_there(self):
        self.assertGreaterEqual(len(CLOTH_SAMPLES), 2, CLOTH_SAMPLES)

    def test_every_armor_sound_is_what_the_game_s_own_decoder_reads(self):
        from libs.safe_vorbis import load_vorbis_pcm

        names = ["equip.ogg", "dodge3.ogg"] + [f"impact{index}.ogg" for index in range(1, 5)]
        names += [sample.name for sample in CLOTH_SAMPLES]
        for name in names:
            with self.subTest(name=name):
                path = ARMOR_FOLDER / name
                self.assertTrue(path.exists(), name)
                pcm = load_vorbis_pcm(os.path.relpath(str(path)))
                self.assertTrue(pcm, name)


if __name__ == "__main__":
    unittest.main()
