import unittest
from types import SimpleNamespace
from unittest import mock

import pygame

from libs.event_handeler import EventHandeler


class TravelPointHintTests(unittest.TestCase):
    def make_gameplay(self, keyconfig=None, target_map="tour", zone="Plaza"):
        from libs.gameplay import Gameplay

        gameplay = Gameplay.__new__(Gameplay)
        gameplay.kc = {"interact": pygame.K_RETURN} if keyconfig is None else keyconfig
        gameplay.game = SimpleNamespace(keyconfig=gameplay.kc, network=mock.Mock())
        world = SimpleNamespace(
            get_travel_point_at=mock.Mock(return_value=(
                SimpleNamespace(target_map=target_map) if target_map else None
            )),
            get_zone_at=mock.Mock(return_value=zone),
        )
        gameplay.camera = SimpleNamespace(
            focus_object=SimpleNamespace(map=world, x=10, y=20, z=0)
        )
        return gameplay

    def make_handler(self):
        handler = EventHandeler.__new__(EventHandeler)
        handler.game = SimpleNamespace(keyconfig={"interact": pygame.K_RETURN})
        return handler

    @mock.patch("libs.event_handeler.speak")
    def test_unmounted_hint_announces_shift_plus_configured_key(self, speak_mock):
        self.make_handler().travel_point_hint({"map": "Outpost", "mounted": False})
        message = speak_mock.call_args.args[0]
        self.assertIn("Shift plus ENTER", message)

    @mock.patch("libs.event_handeler.speak")
    def test_mounted_hint_announces_shift_modifier(self, speak_mock):
        self.make_handler().travel_point_hint({"map": "Outpost", "mounted": True})
        message = speak_mock.call_args.args[0]
        self.assertIn("Shift plus ENTER", message)

    @mock.patch("libs.event_handeler.speak")
    def test_created_hint_uses_configured_key_and_coordinates(self, speak_mock):
        self.make_handler().travel_point_hint({
            "map": "main",
            "created": True,
            "x": 0,
            "y": 0,
            "z": 0,
            "target_x": 34,
            "target_y": 39,
            "target_z": 0,
        })
        message = speak_mock.call_args.args[0]
        self.assertIn("Travel point created at (0, 0, 0) to main (34, 39, 0)", message)
        self.assertIn("press Shift plus ENTER to travel", message)
        self.assertNotIn("press F to travel", message)

    @mock.patch("libs.event_handeler.speak")
    @mock.patch("libs.gameplay.speak")
    def test_zone_check_matches_walk_in_hint_for_configured_and_fallback_keys(self, zone_speak, entry_speak):
        for config, label in [
            ({"interact": pygame.K_RETURN}, "ENTER"),
            ({"interact": pygame.K_f}, "F"),
            ({"interact": pygame.K_SPACE}, "SPACE"),
            ({}, "F"),
        ]:
            with self.subTest(config=config):
                gameplay = self.make_gameplay(config)
                handler = self.make_handler()
                handler.game = gameplay.game
                gameplay.speak_zone(0)
                handler.travel_point_hint({"map": "tour"})
                self.assertEqual(zone_speak.call_args.args[0], entry_speak.call_args.args[0])
                self.assertEqual(zone_speak.call_args.args[0],
                                 f"You are at a travel point to tour. Press Shift plus {label} to travel.")
                gameplay.game.network.send.assert_not_called()
                gameplay.camera.focus_object.map.get_zone_at.assert_not_called()

    @mock.patch("libs.gameplay.speak")
    def test_zone_check_uses_updated_binding(self, speak_mock):
        gameplay = self.make_gameplay()
        gameplay.speak_zone(0)
        gameplay.kc["interact"] = pygame.K_SPACE
        gameplay.speak_zone(0)
        self.assertIn("Shift plus SPACE", speak_mock.call_args.args[0])

    @mock.patch("libs.gameplay.speak")
    def test_normal_zone_and_no_zone_announcements_are_unchanged(self, speak_mock):
        for zone, expected in [("Plaza", "Plaza"), (None, "No zone")]:
            with self.subTest(zone=zone):
                gameplay = self.make_gameplay(target_map=None, zone=zone)
                gameplay.speak_zone(0)
                speak_mock.assert_called_with(expected)
                gameplay.game.network.send.assert_not_called()


if __name__ == "__main__":
    unittest.main()
