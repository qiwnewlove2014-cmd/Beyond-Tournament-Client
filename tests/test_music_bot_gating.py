import unittest
from unittest import mock

import pygame

from libs.gameplay import Gameplay


class FakeMusicBot:
    def __init__(self):
        self.volume = 50
        self.calls = []
        self.playing = False
        self.paused = False
        self.last_stop_clear_queue = True

    def open_search(self):
        self.calls.append("open_search")

    def set_volume(self, vol):
        self.calls.append(("set_volume", vol))

    def previous_feed_track(self):
        self.calls.append("previous_feed_track")

    def next_feed_track(self):
        self.calls.append("next_feed_track")

    def stop(self, clear_queue=True):
        self.calls.append("stop")
        self.last_stop_clear_queue = clear_queue

    def toggle_pause(self):
        self.calls.append("toggle_pause")

    def has_last_track(self):
        return False

    def set_cinema_target(self, cabinet_id):
        self.calls.append(("set_cinema_target", cabinet_id))
        self.cinema_target = cabinet_id


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

    def test_losing_cinema_permission_hands_the_track_back_to_the_ears(self):
        from types import SimpleNamespace
        from libs.event_handeler import EventHandeler

        gp = make_gp(can_use_music_bot=True, can_use_cinema_speakers=True)
        gp.music_bot.cinema_target = "box_a"
        scheduled = []
        handler = SimpleNamespace(
            gameplay=gp,
            game=SimpleNamespace(put=lambda callback: scheduled.append(callback)),
        )
        EventHandeler.staff_permissions(handler, {
            "is_staff": True,
            "can_use_music_bot": True,
            "can_use_cinema_speakers": False,
        })
        self.assertFalse(gp.can_use_cinema_speakers)
        self.assertEqual(len(scheduled), 1,
                         "the routing must be dropped on the main thread")
        scheduled[0]()
        self.assertEqual(gp.music_bot.calls, [("set_cinema_target", None)])
        self.assertIsNone(gp.music_bot.cinema_target)

    def test_a_plain_account_never_touches_the_cinema_routing(self):
        from types import SimpleNamespace
        from libs.event_handeler import EventHandeler

        gp = make_gp(can_use_music_bot=True)
        scheduled = []
        handler = SimpleNamespace(
            gameplay=gp,
            game=SimpleNamespace(put=lambda callback: scheduled.append(callback)),
        )
        EventHandeler.staff_permissions(handler, {
            "is_staff": True,
            "can_use_music_bot": True,
            "can_use_cinema_speakers": False,
        })
        self.assertEqual(scheduled, [])

    def test_staff_volume_and_feed_work(self):
        gp = make_gp(is_staff=True)
        with mock.patch("libs.gameplay.speak"):
            gp.music_bot_volume(10)
        gp.buffer_cycle_l(pygame.KMOD_CTRL)
        gp.buffer_cycle_r(pygame.KMOD_CTRL)
        self.assertIn(("set_volume", 60), gp.music_bot.calls)
        self.assertIn("previous_feed_track", gp.music_bot.calls)
        self.assertIn("next_feed_track", gp.music_bot.calls)

    def test_ctrl_m_stop_preserves_play_queue(self):
        gp = make_gp(is_staff=True)
        gp.music_bot.playing = True
        with mock.patch("libs.gameplay.speak") as speak:
            gp.music_bot_control(pygame.KMOD_CTRL)
        self.assertEqual(gp.music_bot.calls, ["stop"])
        self.assertFalse(
            gp.music_bot.last_stop_clear_queue,
            "Ctrl+M stop must keep the queued songs (clear_queue=False)",
        )
        speak.assert_called_once_with("Music stopped.")

    def test_shift_m_pause_does_not_stop_or_clear(self):
        gp = make_gp(is_staff=True)
        with mock.patch("libs.gameplay.speak"):
            gp.music_bot_control(pygame.KMOD_SHIFT)
        self.assertEqual(gp.music_bot.calls, ["toggle_pause"])
        self.assertNotIn("stop", gp.music_bot.calls)

    def test_no_music_bot_is_safe(self):
        gp = Gameplay.__new__(Gameplay)  # no music_bot attribute at all
        gp.music_bot_control(0)
        gp.music_bot_volume(10)
        gp.buffer_cycle_l(pygame.KMOD_CTRL)
        gp.buffer_cycle_r(pygame.KMOD_CTRL)


if __name__ == "__main__":
    unittest.main()
