"""Tests for the clock-face turning mode.

Players who navigate by clock positions ("the door is at 3 o'clock") can
switch the turning UI from raw degrees to a clock face: continuous turning
keeps the existing feel but announces hours, and the one-hour-per-press mode
rotates exactly 30 degrees per tap. The internal hfacing stays in degrees, so
nothing else (server, compass cue, tracking) changes.
"""

import unittest
from types import SimpleNamespace
from unittest import mock

from libs import gameplay, menus, options, string_utils


class TestClockDirection(unittest.TestCase):
    def test_mapping_degrees_to_hours(self):
        cases = {
            0: "12 o'clock",
            30: "1 o'clock",
            60: "2 o'clock",
            90: "3 o'clock",
            120: "4 o'clock",
            150: "5 o'clock",
            180: "6 o'clock",
            210: "7 o'clock",
            240: "8 o'clock",
            270: "9 o'clock",
            300: "10 o'clock",
            330: "11 o'clock",
            359: "12 o'clock",
        }
        for degrees, expected in cases.items():
            self.assertEqual(string_utils.clock_direction(degrees), expected, degrees)

    def test_rounds_to_nearest_hour(self):
        self.assertEqual(string_utils.clock_direction(14), "12 o'clock")
        self.assertEqual(string_utils.clock_direction(16), "1 o'clock")
        self.assertEqual(string_utils.clock_direction(95), "3 o'clock")


def _make_gp(hfacing=90, turn_mode="clock_continuous", speak_on_turn=True):
    gp = SimpleNamespace(
        game=SimpleNamespace(pong_mode=False),
        turning=True,
        compass_turn_cue=SimpleNamespace(
            stop_turning=mock.Mock(), on_turn=mock.Mock()
        ),
        player=SimpleNamespace(
            locked=False,
            hfacing=hfacing,
            vfacing=0,
            play_sound=mock.Mock(),
            face=mock.Mock(
                side_effect=lambda h, v: (
                    setattr(gp.player, "hfacing", h),
                    setattr(gp.player, "vfacing", v),
                )
            ),
        ),
        turn_mod=False,
    )
    # Bind the real helper so turn_left/right can dispatch into it even
    # though gp is a bare SimpleNamespace, not a Gameplay instance.
    gp._clock_hour_step = gameplay.Gameplay._clock_hour_step.__get__(gp)
    return gp


class TestTurnStopAnnouncement(unittest.TestCase):
    def test_clock_mode_speaks_hour(self):
        gp = _make_gp(hfacing=90)
        with mock.patch.object(gameplay.options, "get", return_value=True), \
                mock.patch.object(gameplay.options, "get_turn_mode", return_value="clock_continuous"), \
                mock.patch.object(gameplay, "speak") as speak:
            gameplay.Gameplay.turn_stop(gp, 0)
        speak.assert_called_once_with("turned to 3 o'clock")

    def test_degrees_mode_keeps_degree_text(self):
        gp = _make_gp(hfacing=90)
        with mock.patch.object(gameplay.options, "get", return_value=True), \
                mock.patch.object(gameplay.options, "get_turn_mode", return_value="degrees"), \
                mock.patch.object(gameplay, "speak") as speak:
            gameplay.Gameplay.turn_stop(gp, 0)
        speak.assert_called_once_with("turned to 90 degrees")


class TestClockHourMode(unittest.TestCase):
    def test_tap_left_rotates_exactly_one_hour(self):
        gp = _make_gp(hfacing=90)
        with mock.patch.object(gameplay.options, "get_turn_mode", return_value="clock_hour"), \
                mock.patch.object(gameplay, "speak") as speak:
            gameplay.Gameplay.turn_left(gp, 0, turn=True)
        self.assertEqual(gp.player.hfacing, 60)
        speak.assert_called_once_with("turned to 2 o'clock")

    def test_tap_right_rotates_exactly_one_hour(self):
        gp = _make_gp(hfacing=90)
        with mock.patch.object(gameplay.options, "get_turn_mode", return_value="clock_hour"), \
                mock.patch.object(gameplay, "speak") as speak:
            gameplay.Gameplay.turn_right(gp, 0, turn=True)
        self.assertEqual(gp.player.hfacing, 120)
        speak.assert_called_once_with("turned to 4 o'clock")

    def test_holding_key_does_not_drift(self):
        gp = _make_gp(hfacing=90)
        with mock.patch.object(gameplay.options, "get_turn_mode", return_value="clock_hour"):
            gameplay.Gameplay.turn_left(gp, 0, turn=False)
            gameplay.Gameplay.turn_right(gp, 0, turn=False)
        self.assertEqual(gp.player.hfacing, 90)
        gp.player.face.assert_not_called()

    def test_ctrl_snap_turn_still_available(self):
        gp = _make_gp(hfacing=90)
        gp.turn_mod = True
        with mock.patch.object(gameplay.options, "get_turn_mode", return_value="clock_hour"):
            gameplay.Gameplay.turn_left(gp, 0, turn=True)
        self.assertEqual(gp.player.hfacing, 45)


class TestCheckDirectionClock(unittest.TestCase):
    def test_check_direction_speaks_clock_in_clock_mode(self):
        gp = SimpleNamespace(
            player=SimpleNamespace(hfacing=90, x=0.0, y=0.0, z=0.0),
            game=SimpleNamespace(
                audio_mngr=SimpleNamespace(play_unbound=mock.Mock(return_value=None))
            ),
        )
        with mock.patch.object(gameplay.options, "get_turn_mode", return_value="clock_continuous"), \
                mock.patch.object(gameplay, "speak") as speak, \
                mock.patch.object(
                    gameplay.movement, "move",
                    return_value=SimpleNamespace(get_tuple=(0.0, 0.0, 0.0)),
                ):
            gameplay.Gameplay.check_direction(gp)
        speak.assert_called_once_with("3 o'clock")

    def test_check_direction_keeps_compass_in_degrees_mode(self):
        gp = SimpleNamespace(
            player=SimpleNamespace(hfacing=90, x=0.0, y=0.0, z=0.0),
            game=SimpleNamespace(
                audio_mngr=SimpleNamespace(play_unbound=mock.Mock(return_value=None))
            ),
        )
        with mock.patch.object(gameplay.options, "get_turn_mode", return_value="degrees"), \
                mock.patch.object(gameplay, "speak") as speak, \
                mock.patch.object(
                    gameplay.movement, "move",
                    return_value=SimpleNamespace(get_tuple=(0.0, 0.0, 0.0)),
                ):
            gameplay.Gameplay.check_direction(gp)
        speak.assert_called_once_with("East")


class TestOptionsTurnModeControl(unittest.TestCase):
    def test_right_arrow_cycles_to_next_mode(self):
        with mock.patch.object(menus.options, "get_turn_mode", return_value="degrees"), \
                mock.patch.object(menus.options, "get_turn_mode_label", return_value="Degrees"), \
                mock.patch.object(menus.options, "set_turn_mode", side_effect=lambda mode: mode) as set_mode, \
                mock.patch.object(menus.speech, "speak"):
            m = menus.OptionsMenu.__new__(menus.OptionsMenu)
            m.edge = ""
            m.direct_soundgroup = object()
            m.turning_mode_item_index = None
            m._adjust_turning_mode(1)
        set_mode.assert_called_once_with("clock_continuous")

    def test_left_arrow_wraps_to_last_mode(self):
        with mock.patch.object(menus.options, "get_turn_mode", return_value="degrees"), \
                mock.patch.object(menus.options, "get_turn_mode_label", return_value="Degrees"), \
                mock.patch.object(menus.options, "set_turn_mode", side_effect=lambda mode: mode) as set_mode, \
                mock.patch.object(menus.speech, "speak"):
            m = menus.OptionsMenu.__new__(menus.OptionsMenu)
            m.edge = ""
            m.direct_soundgroup = object()
            m.turning_mode_item_index = None
            m._adjust_turning_mode(-1)
        set_mode.assert_called_once_with("clock_hour")

    def test_default_turn_mode_is_degrees(self):
        self.assertEqual(options.get_turn_mode(), "degrees")

    def test_unknown_saved_mode_falls_back_to_degrees(self):
        with mock.patch.object(options, "get", return_value="banana"):
            self.assertEqual(options.get_turn_mode(), "degrees")


if __name__ == "__main__":
    unittest.main()