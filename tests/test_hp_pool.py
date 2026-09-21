"""A player's HP pool is the Server's number, and the client is told it.

The pool is 200, and a Juggernog bottle widens it to 300 mid-match. The client
used to clamp HP at a flat 100 of its own -- its fall and drowning predictions
are worked out locally from `self.hp` and sent back as `set_hp` -- so a pool of
200 meant the Server's heal was dropped on arrival (the setter simply refused
it) and the next drowning tick sent a figure read off a stale total, which the
Server applied as a *heal* for a player who was going under.

So the ceiling travels with every HP (`maxHp`), the client keeps what it is
told, and its own starting pool is the same number the Server counts from,
because the Server never announces a spawn's HP.
"""

import unittest
from types import SimpleNamespace
from unittest import mock

from libs import consts
from libs.event_handeler import EventHandeler
from libs.objects.entity import Entity
from libs.objects.player import Player


class EntityCeilingTests(unittest.TestCase):
    """The ceiling is a figure the entity was told, never one of its own."""

    def entity(self, hp=100, max_hp=None):
        entity = object.__new__(Entity)
        entity._hp = hp
        if max_hp is not None:
            entity.max_hp = max_hp
        return entity

    def test_an_entity_nobody_told_keeps_the_pool_it_was_built_with(self):
        self.assertEqual(Entity.max_hp, 100)
        entity = self.entity()
        entity.hp = 100
        self.assertEqual(entity.hp, 100)

    def test_a_pool_of_two_hundred_is_taken_not_clamped_away(self):
        entity = self.entity(hp=100, max_hp=200)
        entity.hp = 200
        self.assertEqual(entity.hp, 200)
        entity.hp = 199
        self.assertEqual(entity.hp, 199)

    def test_and_a_drinkers_three_hundred_is_taken_too(self):
        entity = self.entity(hp=200, max_hp=300)
        entity.hp = 300
        self.assertEqual(entity.hp, 300)

    def test_past_the_ceiling_and_below_zero_are_both_left_alone(self):
        entity = self.entity(hp=250, max_hp=300)
        entity.hp = 301
        self.assertEqual(entity.hp, 250)
        entity.hp = -5
        self.assertEqual(entity.hp, 250)

    def test_the_player_class_clamps_the_same_way(self):
        player = object.__new__(Player)
        player._hp = 200
        player.max_hp = 200
        player.lock_weapon = False
        player.hp = 200
        self.assertEqual(player.hp, 200)
        player.hp = 201
        self.assertEqual(player.hp, 200)

    def test_the_client_starts_on_the_pool_the_server_counts(self):
        # The Server never announces a spawn's HP, so this default and the
        # Server's own are one number in two places: `tools/player_hp_test.js`
        # reads this same default and compares it.
        self.assertIn(200, Player.__init__.__defaults__,
                      f"Player defaults are {Player.__init__.__defaults__}")


class FallDamageTests(unittest.TestCase):
    """A fall is reported against the pool the player really has."""

    def falling(self, hp, max_hp, distance):
        player = object.__new__(Player)
        player._hp = hp
        player.max_hp = max_hp
        player.lock_weapon = False
        player.fall_distance = distance
        player.x = player.y = player.z = 0
        player.stunned = False
        player.stunned_clock = SimpleNamespace(restart=lambda: None)
        player.map = SimpleNamespace(get_tile_at=lambda *args: "grass")
        player.sent = []
        player.game = SimpleNamespace(
            network=SimpleNamespace(send=lambda *args: player.sent.append(args))
        )
        # The landing sounds and the step simulation are Entity's half; what is
        # under test here is the arithmetic Player adds on top of it.
        with mock.patch.object(Entity, "fall_stop", lambda self: None):
            player.fall_stop()
        return player

    def test_a_landing_takes_the_damage_off_the_pool_the_player_has(self):
        player = self.falling(hp=200, max_hp=200, distance=100)
        self.assertLessEqual(player.hp, 153)
        self.assertGreaterEqual(player.hp, 147)
        self.assertEqual(player.sent, [(consts.CHANNEL_MISC, "set_hp", {"amount": player.hp})])

    def test_and_is_never_reported_as_the_hundred_it_used_to_be(self):
        player = self.falling(hp=200, max_hp=200, distance=100)
        self.assertNotEqual(player.hp, 100)
        self.assertEqual(player.sent[0][2]["amount"], player.hp)

    def test_a_fall_that_overshoots_lands_on_the_current_pool(self):
        player = self.falling(hp=300, max_hp=300, distance=5000)
        self.assertEqual(player.hp, 300)
        self.assertEqual(player.sent[0][2]["amount"], 300)


class ServerToldCeilingTests(unittest.TestCase):
    """The two packets that carry a pool, driven as the real handlers."""

    def test_the_ceiling_arrives_with_the_hp(self):
        player = SimpleNamespace(hp=100, max_hp=100, lock_weapon=False)
        handler = SimpleNamespace(gameplay=SimpleNamespace(player=player))
        EventHandeler.set_hp(handler, {"amount": 250, "maxHp": 300})
        self.assertEqual((player.hp, player.max_hp), (250, 300))

    def test_a_packet_without_a_ceiling_still_sets_the_hp(self):
        # A Server that predates the field is read exactly as it always was.
        player = SimpleNamespace(hp=100, max_hp=200, lock_weapon=False)
        handler = SimpleNamespace(gameplay=SimpleNamespace(player=player))
        EventHandeler.set_hp(handler, {"amount": 150})
        self.assertEqual((player.hp, player.max_hp), (150, 200))

    def test_a_watched_player_is_judged_against_their_own_pool(self):
        watched = object.__new__(Entity)
        watched._hp = 100
        watched.sync_network_position = lambda *args: None
        watched.face = lambda *args: None
        gameplay = SimpleNamespace(
            spectator_mode=True,
            player=SimpleNamespace(name="Me"),
            map=SimpleNamespace(entities={"Other": watched}),
            pong_team1="Team 1",
            pong_team2="Team 2",
        )
        EventHandeler.spectator_update(SimpleNamespace(gameplay=gameplay), {
            "players": [{"name": "Other", "x": 1, "y": 2, "z": 0, "hp": 250, "maxHp": 300}],
        })
        self.assertEqual((watched.hp, watched.max_hp), (250, 300))


if __name__ == "__main__":
    unittest.main()
