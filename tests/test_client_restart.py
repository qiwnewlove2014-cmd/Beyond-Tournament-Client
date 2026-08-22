"""Regression tests for the main-menu process restart hand-off."""

import os
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from libs import game as game_module
import beyond_tournament


class TestClientRestart(unittest.TestCase):
    def test_confirmation_menu_offers_restart_and_cancel(self):
        class CaptureMenu:
            def __init__(self, game, title):
                self.title = title
                self.items = []
            def add_items(self, items):
                self.items.extend(items)

        fake = object.__new__(game_module.Game)
        fake._restart_in_progress = False
        fake.restart_client = object()
        fake.replace = mock.Mock()
        with mock.patch.object(game_module.menu, "Menu", CaptureMenu), \
                mock.patch.object(game_module.menus, "set_default_sounds"):
            fake.ask_to_restart_client()

        opened = fake.replace.call_args.args[0]
        self.assertIn("clear all audio", opened.title)
        self.assertEqual(
            [label for label, _ in opened.items],
            ["Yes, restart the client", "No, return to the main menu"],
        )
        self.assertIs(opened.items[0][1], fake.restart_client)

    def test_entry_point_stops_parent_before_audio_initialization(self):
        argv = ["Beyond Tournament.exe", "restart_client", "C:/game", "2468"]
        with mock.patch.object(beyond_tournament.sys, "argv", argv), \
                mock.patch.object(beyond_tournament.os, "getpid", return_value=1357), \
                mock.patch.object(beyond_tournament.os, "kill") as kill, \
                mock.patch("psutil.pid_exists", side_effect=[True, False]), \
                mock.patch("time.sleep") as sleep:
            beyond_tournament._complete_restart_parent_handoff()

        self.assertEqual(kill.call_args.args[0], 2468)
        sleep.assert_called_once_with(0.02)

    def test_entry_point_ignores_normal_launch(self):
        with mock.patch.object(beyond_tournament.sys, "argv", ["beyond_tournament.py"]), \
                mock.patch.object(beyond_tournament.os, "kill") as kill:
            beyond_tournament._complete_restart_parent_handoff()
        kill.assert_not_called()

    def test_game_argument_parser_does_not_repeat_restart_termination(self):
        fake = object.__new__(game_module.Game)
        argv = ["beyond_tournament.py", "restart_client", "C:/game", "2468"]
        with mock.patch.object(game_module.sys, "argv", argv), \
                mock.patch.object(game_module.os, "kill") as kill:
            fake.parse_arguments()
        kill.assert_not_called()

    def test_source_command_preserves_restart_argument_positions(self):
        script = os.path.abspath("beyond_tournament.py")
        with mock.patch("libs.logger.is_compiled", return_value=False), \
                mock.patch.object(game_module.sys, "argv", [script]), \
                mock.patch.object(game_module.sys, "executable", "python.exe"), \
                mock.patch.object(game_module.os, "getpid", return_value=4321):
            command = game_module.Game._restart_launch_command()

        self.assertEqual(command[:2], ["python.exe", script])
        self.assertEqual(command[-3:], ["restart_client", os.getcwd(), "4321"])

    def test_compiled_command_relaunches_executable_directly(self):
        with mock.patch("libs.logger.is_compiled", return_value=True), \
                mock.patch.object(game_module.sys, "executable", "Beyond Tournament.exe"), \
                mock.patch.object(game_module.os, "getpid", return_value=7654):
            command = game_module.Game._restart_launch_command()

        self.assertEqual(command[0], "Beyond Tournament.exe")
        self.assertEqual(command[1:], ["restart_client", os.getcwd(), "7654"])

    def test_restart_fades_and_blocks_menu_input(self):
        fake = SimpleNamespace(
            _restart_in_progress=False,
            _launch_restarted_client=object(),
            replace=mock.Mock(),
        )
        captured = {}

        def start_exit_fade(**kwargs):
            captured.update(kwargs)
            return True

        fake.start_exit_fade = start_exit_fade
        result = game_module.Game.restart_client(fake)

        self.assertTrue(result)
        self.assertTrue(fake._restart_in_progress)
        self.assertIs(captured["on_faded"], fake._launch_restarted_client)
        self.assertFalse(captured["exit_after"])
        self.assertEqual(captured["announce"], "Restarting client")
        fake.replace.assert_called_once()

    def test_launch_marks_expected_shutdown_spawns_and_exits(self):
        fake = object.__new__(game_module.Game)
        fake.exit = mock.Mock()
        command = ["Beyond Tournament.exe", "restart_client", "C:/game", "123"]
        with mock.patch.object(game_module.Game, "_restart_launch_command", return_value=command), \
                mock.patch("libs.crash_reporting.mark_expected_shutdown") as mark_expected, \
                mock.patch.object(game_module.subprocess, "Popen") as popen:
            game_module.Game._launch_restarted_client(fake)

        mark_expected.assert_called_once_with("client_restart")
        popen.assert_called_once()
        self.assertEqual(popen.call_args.args[0], command)
        fake.exit.assert_called_once_with()

    def test_spawn_failure_restores_session_and_main_menu(self):
        fake = object.__new__(game_module.Game)
        fake._restart_in_progress = True
        fake._exit_fade_started = True
        with mock.patch.object(game_module.Game, "_restart_launch_command", return_value=["bad.exe"]), \
                mock.patch("libs.crash_reporting.mark_expected_shutdown"), \
                mock.patch("libs.crash_reporting.begin_session") as begin_session, \
                mock.patch.object(game_module.subprocess, "Popen", side_effect=OSError("blocked")), \
                mock.patch.object(game_module.menus, "main_menu") as main_menu, \
                mock.patch.object(game_module, "speak"):
            game_module.Game._launch_restarted_client(fake)

        self.assertFalse(fake._restart_in_progress)
        self.assertFalse(fake._exit_fade_started)
        begin_session.assert_called_once_with()
        main_menu.assert_called_once_with(fake)


if __name__ == "__main__":
    unittest.main()
