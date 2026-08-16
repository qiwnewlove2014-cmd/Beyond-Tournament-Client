import unittest
from types import SimpleNamespace
from unittest import mock

import pygame

from libs.event_handeler import EventHandeler


class TravelPointHintTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
