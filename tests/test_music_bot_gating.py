import unittest
from unittest import mock

import pygame

from libs.gameplay import Gameplay


class FakeMusicBot:
    def __init__(self):
        self.volume = 50
        self.calls = []

    def open_search(self):
        self.calls.append("open_search")

    def set_volume(self, vol):
        self.calls.append(("set_volume", vol))

    def previous_feed_track(self):
        self.calls.append("previous_feed_track")

    def next_feed_track(self):
        self.calls.append("next_feed_track")

    def stop(self):
        self.calls.append("stop")


def make_gp(**role_flags):
    gp = Gameplay.__new__(Gameplay)
    gp.music_bot = FakeMusicBot()
    for key, val in role_flags.items():
        setattr(gp, key, val)
    return gp


class TestMusicBotGating(unittest.TestCase):
    def test_non_staff_press_m_is_silent(self):
        gp = make_gp()  # no staff flags at all
        gp.music_bot_control(0)
        self.assertEqual(gp.music_bot.calls, [], "non-staff M key must do nothing")

    def test_non_staff_volume_keys_are_silent(self):
        gp = make_gp()
        with mock.patch("libs.gameplay.speak") as speak:
            gp.music_bot_volume(10)
        self.assertEqual(gp.music_bot.calls, [])
        speak.assert_not_called()

    def test_non_staff_feed_keys_are_silent(self):
        gp = make_gp()
        gp.buffer_cycle_l(pygame.KMOD_CTRL)
        gp.buffer_cycle_r(pygame.KMOD_CTRL)
        self.assertEqual(gp.music_bot.calls, [])

    def test_staff_can_open_search(self):
        gp = make_gp(is_staff=True)
        gp.music_bot_control(0)
        self.assertEqual(gp.music_bot.calls, ["open_search"])

    def test_builder_can_open_search(self):
        gp = make_gp(is_builder=True)
        gp.music_bot_control(0)
        self.assertEqual(gp.music_bot.calls, ["open_search"])

    def test_technician_can_open_search(self):
        gp = make_gp(is_technician=True)
        gp.music_bot_control(0)
        self.assertEqual(gp.music_bot.calls, ["open_search"])

    def test_server_granted_player_can_open_search(self):
        gp = make_gp(can_use_music_bot=True)
        gp.music_bot_control(0)
        self.assertEqual(gp.music_bot.calls, ["open_search"])

    def test_revoked_permission_stops_active_bot_on_main_thread(self):
        from types import SimpleNamespace
        from libs.event_handeler import EventHandeler

        gp = make_gp(can_use_music_bot=True)
        handler = SimpleNamespace(
            gameplay=gp,
            game=SimpleNamespace(put=lambda callback: callback()),
        )
        EventHandeler.staff_permissions(handler, {
            "is_staff": False,
            "is_builder": False,
            "is_technician": False,
            "can_broadcast_megaphone": False,
            "can_use_music_bot": False,
        })
        self.assertFalse(gp._can_use_music_bot())
        self.assertEqual(gp.music_bot.calls, ["stop"])

    def test_staff_volume_and_feed_work(self):
        gp = make_gp(is_staff=True)
        with mock.patch("libs.gameplay.speak"):
            gp.music_bot_volume(10)
        gp.buffer_cycle_l(pygame.KMOD_CTRL)
        gp.buffer_cycle_r(pygame.KMOD_CTRL)
        self.assertIn(("set_volume", 60), gp.music_bot.calls)
        self.assertIn("previous_feed_track", gp.music_bot.calls)
        self.assertIn("next_feed_track", gp.music_bot.calls)

    def test_no_music_bot_is_safe(self):
        gp = Gameplay.__new__(Gameplay)  # no music_bot attribute at all
        gp.music_bot_control(0)
        gp.music_bot_volume(10)
        gp.buffer_cycle_l(pygame.KMOD_CTRL)
        gp.buffer_cycle_r(pygame.KMOD_CTRL)


if __name__ == "__main__":
    unittest.main()
