"""The attack pools, heard: a swing comes out of a shuffle bag, never off a loop.

A melee weapon's whole presence in the game is its swing, and the folder behind
it holds a *pool* of samples -- `data/weapons/<weapon>/fire/attack1-3.ogg`, with
a matching `impact/hit1-3.ogg` for what the blade lands on. Two things can go
wrong with a pool and they are opposites:

  * **the same sample twice in a row** (`random_item`'s failure): a listener
    hears attack2, attack2, attack2 and the weapon sounds broken, not random;
  * **the same order forever** (what this code did until 2026-09-28): a fixed
    `2 -> 1 -> 3` walk, so a whole match of swings is one three-sample loop. The
    owner's report is the measurement that mattered -- *"the melee sounds still
    play attack1.ogg, attack2.ogg, attack3.ogg -- do not repeat and do not loop,
    make it random"*.

`path_utils.next_sound_item` is the one door every folder sound is picked
through (`AudioManager.load_buffer`, `PianoAudio.load_stereo_split_buffers`),
so the pool rule lives there and these checks drive it against the real shipped
folders rather than fixtures -- a weapon whose samples were moved or renamed is
a broken swing whose test should say so.

What is pinned here:

  * a swing pool is dealt (every sample once, then a fresh order), and never
    twice in a row across the seam of two rounds;
  * the order is the shuffle's, not a walk -- the check patches `random.shuffle`
    and expects the pool's own reversed order, which no fixed sequence can
    answer, and which the old `2 -> 1 -> 3` cycle fails outright;
  * the prefix is the pool: `Mjolnir/fire/` keeps its swing set beside its
    block set (`block1-3.ogg`) and a swing never plays a block; an `impact/`
    pool never plays an `attack/` sample;
  * a folder with no such prefix keeps the plain fresh pick it always had, and
    a path that is already a file is handed back untouched;
  * the old name is gone from the client's own code, so no caller can be left
    walking a pool that no longer exists.
"""

import os
import random
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from libs import path_utils  # noqa: E402

DATA = Path(__file__).resolve().parent.parent / "data"

# The real pools a melee weapon ships: a swing set, the same weapon's impact
# set, and a swing set that shares its folder with a block set.
SWING = DATA / "weapons" / "arming_sword" / "fire"
IMPACT = DATA / "weapons" / "arming_sword" / "impact"
MIXED = DATA / "weapons" / "Mjolnir" / "fire"
PLAIN = DATA / "weapons" / "knife" / "fire"

# The fixed order this replaced, exactly as `get_next_cycle_item` walked it.
OLD_CYCLE = ["attack2.ogg", "attack1.ogg", "attack3.ogg"]


def clear_bags():
    """A bag remembers what it has dealt, so a test starts from a fresh deck."""
    path_utils._bags.clear()


def names(folder):
    return sorted(name for name in os.listdir(folder) if name.endswith(".ogg"))


class PickedFromAPoolTests(unittest.TestCase):
    """The picking rule as a weapon actually meets it."""

    def setUp(self):
        clear_bags()
        self.pool = names(SWING)

    def tearDown(self):
        clear_bags()

    def test_the_folders_these_checks_lean_on_are_the_ones_the_weapons_ship(self):
        for folder in (SWING, IMPACT, MIXED, PLAIN):
            self.assertTrue(folder.is_dir(), folder)
        self.assertEqual(self.pool, ["attack1.ogg", "attack2.ogg", "attack3.ogg"])
        self.assertEqual(names(IMPACT), ["hit1.ogg", "hit2.ogg", "hit3.ogg"])
        self.assertIn("block1.ogg", names(MIXED))

    def test_every_sample_is_dealt_before_any_of_them_comes_round_again(self):
        dealt = [os.path.basename(path_utils.next_sound_item(str(SWING)))
                 for _ in range(len(self.pool))]
        self.assertEqual(sorted(dealt), self.pool)

    def test_a_swing_never_plays_twice_in_a_row_not_even_across_the_seam(self):
        dealt = [os.path.basename(path_utils.next_sound_item(str(SWING)))
                 for _ in range(len(self.pool) * 4)]
        for index in range(1, len(dealt)):
            self.assertNotEqual(dealt[index], dealt[index - 1], dealt)
        # And each round of the pool is still a whole pool, not a shorter deck.
        for start in range(0, len(dealt), len(self.pool)):
            self.assertEqual(sorted(dealt[start:start + len(self.pool)]), self.pool)

    def test_the_order_is_the_shuffles_and_not_a_walk(self):
        # A bag deals its shuffled pool from the end, so a shuffle that reverses
        # the pool deals the folder's own order -- the deck answers the shuffle,
        # which no fixed sequence can do (a walk does not read it at all) and
        # which the removed `2 -> 1 -> 3` cycle fails outright.
        with mock.patch.object(random, "shuffle", lambda pool: pool.reverse()):
            dealt = [os.path.basename(path_utils.next_sound_item(str(SWING)))
                     for _ in range(len(self.pool))]
        self.assertEqual(dealt, self.pool)
        self.assertNotEqual(dealt, OLD_CYCLE)

    def test_two_decks_do_not_come_out_in_one_frozen_order(self):
        # Seeded, so this is a deterministic run whose answer is not a
        # property of the shuffle implementation -- only of "the order varies".
        random.seed(20260928)
        dealt = [os.path.basename(path_utils.next_sound_item(str(SWING)))
                 for _ in range(len(self.pool) * 4)]
        decks = {tuple(dealt[start:start + len(self.pool)])
                 for start in range(0, len(dealt), len(self.pool))}
        self.assertGreater(len(decks), 1, dealt)

    def test_a_swing_never_plays_the_block_set_beside_it(self):
        drawn = {os.path.basename(path_utils.next_sound_item(str(MIXED)))
                 for _ in range(len(names(MIXED)))}
        self.assertEqual(drawn, {"attack1.ogg", "attack2.ogg", "attack3.ogg"}, drawn)
        self.assertFalse(any(name.startswith("block") for name in drawn), drawn)

    def test_an_impact_pool_is_its_own_deck(self):
        dealt = [os.path.basename(path_utils.next_sound_item(str(IMPACT)))
                 for _ in range(3)]
        self.assertEqual(sorted(dealt), ["hit1.ogg", "hit2.ogg", "hit3.ogg"], dealt)


class NotAPoolTests(unittest.TestCase):
    """The folders that keep the plain fresh pick, and the paths that are files."""

    def setUp(self):
        clear_bags()

    def tearDown(self):
        clear_bags()

    def test_a_folder_without_attack_or_hit_samples_is_a_plain_fresh_pick(self):
        drawn = {os.path.basename(path_utils.next_sound_item(str(PLAIN)))
                 for _ in range(40)}
        self.assertTrue(drawn)
        self.assertTrue(drawn <= set(names(PLAIN)), drawn)

    def test_a_path_that_is_already_a_file_comes_back_untouched(self):
        sample = str(SWING / "attack1.ogg")
        self.assertEqual(path_utils.next_sound_item(sample), sample)

    def test_a_missing_folder_comes_back_untouched_rather_than_crashing(self):
        missing = str(DATA / "weapons" / "no_such_weapon" / "fire")
        self.assertEqual(path_utils.next_sound_item(missing), missing)
        self.assertEqual(path_utils.next_sound_item(""), "")


class TheOldWalkIsGoneTests(unittest.TestCase):
    """A caller left behind would swing a pool that no longer exists."""

    def test_no_client_code_still_names_the_removed_cycle(self):
        libs = Path(__file__).resolve().parent.parent / "libs"
        offenders = []
        for path in libs.rglob("*.py"):
            if "get_next_cycle_item" in path.read_text(encoding="utf-8"):
                offenders.append(path.name)
        self.assertEqual(offenders, [], offenders)
        self.assertFalse(hasattr(path_utils, "get_next_cycle_item"))


if __name__ == "__main__":
    unittest.main()
