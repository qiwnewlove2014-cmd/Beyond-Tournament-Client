"""The optional layers, measured against the room they were added to.

"Wider and taller" is not an opinion here: ``tools/cinema_layer_balance_sim.py``
builds the seven-speaker theatre and the twelve-speaker one from the real
renderer (so the profile weights and the equal-power share are the ones that
play), puts a listener at a seat behind the cabinet, and adds up what arrives.

Two things had to be true before the numbers could move at all:

* **a layer may not turn the room down.** The share is computed over the room's
  authored shape, not over every fed speaker, so a placed roof leaves the
  centre and the walls exactly where the seven-speaker room had them. Measuring
  the first build (which counted all twelve), five new speakers had diluted
  every wall from 1/sqrt(5) to 1/sqrt(10): the room's width rose only 1.4 points
  (55.39% -> 56.75%) and its difference signal fell 17% -- the layer was paid
  for by the walls it was supposed to surround.
* **the layers have a budget of their own** (``profiles.LAYER_POWER_SHARE``,
  half the shape's power), so the room is not a stack of speakers that each
  want to be the loudest.

Measured at the seat 5 m behind the cabinet, seven-speaker room -> twelve:

    power            2.218 -> 2.515   (0.55 dB louder)
    stage width     55.39% -> 58.73%  (left/right energy further off centre)
    height           0.00% -> 5.70%   (share of the room's energy overhead)
    front share     84.06% -> 74.13%  (the screen pair still carries the room)

These tests pin those numbers as bands, not as an exact table: a weight may be
re-tuned, but a change that makes the room narrower, flattens the roof, or buys
its width out of the screen wall fails here and has to be argued for.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from libs.audio.cinema import router
from libs.audio.cinema.profiles import (IMAGE_SLOTS, LAYER_POWER_SHARE,
                                        LAYER_WEIGHTS, PROFILES, get_profile)
from tools import cinema_layer_balance_sim as balance

WALLS = balance.WALLS
LAYERS = balance.LAYERS
# A front seat, the middle of the audience, and the back row.
SEATS = ((0.0, -2.0), (0.0, -5.0), (0.0, -12.0))
MIDDLE = (0.0, -5.0)


def plan_of(renderer):
    """``{slot: (gain_l, gain_r)}`` for a room, as the renderer will play it."""
    return {slot: (gain_l, gain_r) for slot, gain_l, gain_r in renderer.plan()}


class TheRoomKeepsWhatItHadTests(unittest.TestCase):
    def setUp(self):
        self.old = balance.room(WALLS)
        self.new = balance.room(WALLS + LAYERS)

    def test_a_layer_does_not_turn_down_the_room_it_joins(self):
        """The seven wall speakers play at exactly the level they always did.

        This is the promise the profile has always made ("adding a layer must
        not change the level of the speakers already there") and the one the
        renderer used to break: counting the layers in the equal-power divisor
        dropped every wall from 1/sqrt(5) to 1/sqrt(10).
        """
        before = plan_of(self.old)
        after = plan_of(self.new)
        for slot in WALLS:
            self.assertEqual(after[slot], before[slot], slot)

    def test_the_screen_pair_is_untouched(self):
        """The image slots keep their authored feeds, at their authored level."""
        terms = plan_of(self.new)
        self.assertEqual(terms["front_l"], (1.0, 0.0))
        self.assertEqual(terms["front_r"], (0.0, 1.0))
        self.assertNotIn("front_l", LAYER_WEIGHTS)
        self.assertNotIn("front_r", LAYER_WEIGHTS)

    def test_the_front_pair_still_carries_the_room(self):
        """A layer spreads the image; it does not take the screen wall's place."""
        for seat in SEATS:
            front = balance.measure(self.new, balance.seat(*seat))[3]
            self.assertGreaterEqual(front, 0.72, seat)

    def test_the_room_is_half_a_decibel_louder_not_louder_than_that(self):
        """A bigger room is not a louder room: the layer share is a level."""
        for seat in SEATS:
            before = balance.measure(self.old, balance.seat(*seat))[4]
            after = balance.measure(self.new, balance.seat(*seat))[4]
            ratio = after / before
            self.assertGreaterEqual(ratio, 1.10, seat)
            self.assertLessEqual(ratio, 1.17, seat)


class TheRoomIsWiderAndTallerTests(unittest.TestCase):
    def setUp(self):
        self.old = balance.room(WALLS)
        self.new = balance.room(WALLS + LAYERS)

    def test_the_stage_is_wider_at_every_seat(self):
        for seat in SEATS:
            before = balance.measure(self.old, balance.seat(*seat))[0]
            after = balance.measure(self.new, balance.seat(*seat))[0]
            self.assertGreaterEqual(after - before, 0.03, seat)

    def test_the_measured_width_is_the_number_this_tuning_was_chosen_for(self):
        width = balance.measure(self.new, balance.seat(*MIDDLE))[0]
        self.assertGreaterEqual(width, 0.585)
        self.assertLessEqual(width, 0.595)

    def test_the_room_has_a_roof_layer_and_the_seven_speaker_one_did_not(self):
        self.assertEqual(balance.measure(self.old, balance.seat(*MIDDLE))[1], 0.0)
        height = balance.measure(self.new, balance.seat(*MIDDLE))[1]
        self.assertGreaterEqual(height, 0.050)
        self.assertLessEqual(height, 0.065)

    def test_the_roof_does_not_fade_towards_the_back_row(self):
        """It is a layer over the audience, not a speaker over the cabinet."""
        shares = [balance.measure(self.new, balance.seat(*seat))[1] for seat in SEATS]
        self.assertLessEqual(max(shares) - min(shares), 0.005)

    def test_a_side_seat_hears_the_same_room(self):
        """Not one lucky sweet spot: off the centre line the numbers hold."""
        before = balance.measure(self.old, balance.seat(3.0, -6.0))
        after = balance.measure(self.new, balance.seat(3.0, -6.0))
        self.assertGreaterEqual(after[0] - before[0], 0.03)
        self.assertGreaterEqual(after[1], 0.050)
        self.assertGreaterEqual(after[3], 0.72)


class TheLayerShareTests(unittest.TestCase):
    def test_the_layers_share_is_the_level_the_router_applies(self):
        """Quadruple the share and every layer speaker gains exactly 2x.

        ``layer_scale = shape_scale * sqrt(share * n_shape / n_layer)``, so the
        constant is wired through the renderer rather than merely documented --
        and the walls do not move when it changes.
        """
        whole = plan_of(balance.room(WALLS + LAYERS))
        original = router.LAYER_POWER_SHARE
        router.LAYER_POWER_SHARE = original * 4.0
        try:
            louder = plan_of(balance.room(WALLS + LAYERS))
        finally:
            router.LAYER_POWER_SHARE = original
        for slot in LAYERS:
            self.assertAlmostEqual(louder[slot][0], whole[slot][0] * 2.0, places=9, msg=slot)
            self.assertAlmostEqual(louder[slot][1], whole[slot][1] * 2.0, places=9, msg=slot)
        for slot in WALLS:
            self.assertEqual(louder[slot], whole[slot], slot)

    def test_a_mono_room_is_one_budget_with_the_layers_inside_it(self):
        """No image to protect means every speaker, layers included, is equal.

        ``mono_spread`` exists to be the same middle of the song everywhere; a
        layer on a different share from the walls would be a stereo difference
        nobody asked for, so a mono plan takes one divisor over the whole room.
        """
        room = balance.room(WALLS + LAYERS, "mono_spread")
        self.assertEqual(len(room.slots), len(WALLS) + len(LAYERS))
        scale = 1.0 / len(room.slots) ** 0.5
        for slot, (gain_l, gain_r) in plan_of(room).items():
            weight = get_profile("mono_spread").weight(slot)
            self.assertAlmostEqual(gain_l, weight[0] * scale, places=9, msg=slot)
            self.assertAlmostEqual(gain_r, weight[1] * scale, places=9, msg=slot)

    def test_the_share_is_the_documented_half(self):
        self.assertGreaterEqual(LAYER_POWER_SHARE, 0.40)
        self.assertLessEqual(LAYER_POWER_SHARE, 0.60)

    def test_a_room_with_no_layers_is_untouched_by_the_share(self):
        """The parity baseline and every wall-only room never read it."""
        for profile in ("front_only", "front_stage", "surround"):
            slots = PROFILES[profile].slots
            if any(slot in LAYERS for slot in slots):
                continue
            room = balance.room(slots, profile)
            self.assertFalse(any(slot in LAYERS for slot in room.slots), profile)


class TheVoicingTests(unittest.TestCase):
    def test_the_layers_play_the_programme_and_carry_their_own_character(self):
        """Every layer speaker is the mid, plus a lean that names its job.

        ``gain_l + gain_r`` is the mid coefficient and ``gain_l - gain_r`` the
        difference one. The wide pair leans hardest (it is the layer that
        widens the stage), the roof pair a little (air over the audience, not a
        point source), and the ceiling centre a touch (a wide top, not a
        second ``front_c``).
        """
        def lean(slot):
            gain_l, gain_r = LAYER_WEIGHTS[slot]
            self.assertAlmostEqual(gain_l + gain_r, 1.0, places=9, msg=slot)
            return abs(gain_l - gain_r)

        self.assertGreater(lean("wide_l"), lean("height_l"))
        self.assertGreater(lean("height_l"), lean("top_c"))
        self.assertGreater(lean("top_c"), 0.0)
        self.assertAlmostEqual(lean("wide_l"), lean("wide_r"), places=9)
        self.assertAlmostEqual(lean("height_l"), lean("height_r"), places=9)

    def test_no_two_speakers_of_the_room_play_the_same_signal(self):
        """Two identical feeds at different distances comb filter at the ear."""
        weights = list(PROFILES["theatre"].weights.items())
        seen = {}
        for slot, pair in weights:
            self.assertNotIn(pair, seen, f"{slot} plays what {seen.get(pair)} plays")
            seen[pair] = slot
        self.assertEqual(len(seen), len(weights))

    def test_the_layers_are_not_image_slots(self):
        self.assertFalse(set(LAYER_WEIGHTS) & set(IMAGE_SLOTS))

    def test_custom_plays_the_layers_as_placed(self):
        """The one mode with no scale: the weights are the feeds."""
        room = balance.room(WALLS + LAYERS, "custom")
        terms = plan_of(room)
        for slot in LAYERS:
            gain_l, gain_r = LAYER_WEIGHTS[slot]
            self.assertAlmostEqual(terms[slot][0], gain_l, places=9, msg=slot)
            self.assertAlmostEqual(terms[slot][1], gain_r, places=9, msg=slot)


if __name__ == "__main__":
    unittest.main()
