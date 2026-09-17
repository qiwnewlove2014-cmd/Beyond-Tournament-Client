"""Regression tests for the login retry and the port above the configured one.

The reported bug: a few players could not get in at all, and every one of them
saw the same "Connection error [timeout]" -- which is also what a wrong
password, a banned IP and a slow map snapshot would have said, had any of them
been the cause. One attempt, one line, no answer.

Pinned here:

1. A handshake that never came back is retried once on a fresh socket (a new
   local port, so a new NAT mapping) instead of being given up on immediately.
2. The attempts are the endpoint and the port above it, derived on both sides
   rather than named, and the port that last answered only ever reorders them.
3. The retry is bounded and its last word names the *kind* of failure: nothing
   was ever answered, so this is the connection rather than the account.
4. Only the handshake phase retries. A login the server accepted and then left
   silent belongs to ``Client.loop``'s own watchdog (see test_login_timeout.py),
   which never retries: a server that is working is left to work.
5. Both entry points -- the login button and a reconnect -- open the same
   attempt list, so a retry after a drop knows where it may go too.
6. A port this machine will not open a socket for is walked past, not reported:
   the point of two candidates is that one may open where another cannot.
"""

import os
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from libs import consts
from libs import game as game_module
from libs import login_attempts


class LoginAttemptPolicyTests(unittest.TestCase):
    def test_the_list_is_the_endpoint_and_the_port_above_it(self):
        self.assertEqual(
            login_attempts.candidate_ports(consts.DEFAULT_PORT),
            (consts.DEFAULT_PORT, consts.DEFAULT_PORT + login_attempts.FALLBACK_OFFSET),
        )
        self.assertEqual(login_attempts.candidate_ports(13000), (13000, 13002))

    def test_the_candidate_list_is_read_from_a_service_port_too(self):
        """Service ports are just numbers here: nothing assumes 13000."""
        self.assertEqual(login_attempts.candidate_ports(443), (443, 445))

    def test_the_pair_is_derived_from_the_endpoint_and_never_named(self):
        """One rule on both sides: the Server derives its second listener from
        its own primary port (server/libs/consts.ts), so this end must derive
        the same number rather than hold a second constant to drift."""
        self.assertEqual(login_attempts.FALLBACK_OFFSET, 2)
        self.assertEqual(login_attempts.fallback_port(13000), 13000 + login_attempts.FALLBACK_OFFSET)
        self.assertEqual(login_attempts.fallback_port(700), 700 + login_attempts.FALLBACK_OFFSET)

    def test_the_port_next_to_the_game_port_is_left_to_the_presence_service(self):
        """The port *beside* the game port is this project's own: the presence
        sound service listens on TCP 13001 (env ``PRESENCE_HTTP_PORT``, opened
        by the Windows installer for Radmin). A different protocol on one
        number does not conflict, but one number telling two stories is how a
        future reader ends up asking why 13001 is sometimes UDP -- so the
        game's second listener skips it."""
        self.assertEqual(login_attempts.FALLBACK_OFFSET, 2)
        self.assertNotIn(13001, login_attempts.candidate_ports(13000))
        self.assertNotIn(13001, login_attempts.candidate_ports(9999))
        self.assertEqual(login_attempts.fallback_port(13000), 13002)

    def test_an_endpoint_with_no_room_above_it_is_one_port(self):
        self.assertEqual(login_attempts.candidate_ports(65535), (65535,))
        self.assertIsNone(login_attempts.fallback_port(65535))

    def test_the_port_that_answered_is_walked_first(self):
        self.assertEqual(
            login_attempts.candidate_ports(13000, preferred=13002), (13002, 13000)
        )
        # It is an ordering hint, and nothing more: the endpoint itself is
        # never last when it is the port that answered.
        self.assertEqual(
            login_attempts.candidate_ports(13000, preferred=13000), (13000, 13002)
        )

    def test_a_remembered_port_that_is_not_one_of_ours_is_ignored(self):
        for value in (None, "", "nonsense", 0, -1, 65536, 9999, 13003, [], {}):
            self.assertEqual(
                login_attempts.candidate_ports(13000, preferred=value),
                (13000, 13002),
                value,
            )
        # A stored string is still a port; Options hands back what it saved.
        self.assertEqual(
            login_attempts.candidate_ports(13000, preferred="13002"), (13002, 13000)
        )

    def test_a_remembered_value_is_only_a_port(self):
        self.assertEqual(login_attempts.remembered_port(13002), 13002)
        self.assertEqual(login_attempts.remembered_port("13002"), 13002)
        for value in (None, "", "x", 0, -1, 65536, [], {}, True, 1.5):
            self.assertNotEqual(login_attempts.remembered_port(value), 13002, value)
        self.assertIsNone(login_attempts.remembered_port("nonsense"))

    def test_the_candidate_count_is_at_least_one(self):
        self.assertGreaterEqual(len(login_attempts.candidate_ports(13000)), 1)

    def test_the_retry_notice_counts_from_one_and_cannot_exceed_the_total(self):
        self.assertEqual(login_attempts.retry_notice(2, 2), "Trying the connection again (2 of 2).")
        self.assertEqual(login_attempts.retry_notice(9, 2), "Trying the connection again (2 of 2).")
        self.assertEqual(login_attempts.retry_notice(0, 2), "Trying the connection again (1 of 2).")

    def test_the_silent_handshake_message_names_the_kind_of_failure(self):
        message = login_attempts.silence_message(2, (13000, 13000))
        self.assertIn("No answer from the server after 2 tries", message)
        self.assertIn("on port 13000.", message)
        self.assertNotIn("ports", message.split(".")[0], "one port is not a pair")
        self.assertIn("connection rather than your account", message)
        self.assertIn("connection log", message)
        # One port, listed once: two attempts on the same port is one port.
        self.assertNotIn("13000 and 13000", message)

    def test_the_message_lists_both_ports_when_there_are_two(self):
        message = login_attempts.silence_message(2, (13000, 13002))
        self.assertIn("on ports 13000 and 13002.", message)


class FakeEvent:
    def __init__(self, event_type):
        self.type = event_type


class FakeTransport:
    """What ``login2`` asks of a network client before the login is sent."""

    def __init__(self, event_type):
        self.net = SimpleNamespace(service=lambda timeout: FakeEvent(event_type))
        self.handshakes = 0
        self.sent = []

    def note_handshake(self):
        self.handshakes += 1

    def loop(self):
        return "transport-loop"

    def send(self, channel, event, data=None):
        self.sent.append((channel, event, data))


class FakeNetwork:
    def __init__(self, label="network"):
        self.label = label
        self.sent = []

    def loop(self):
        return f"{self.label}-loop"


class RetryGame:
    """The slice of Game that login2/_retry_login/_open_first_login_attempt use."""

    # _retry_login re-enters the flow by name, and both the port that answered
    # and the way a socket failure is reported are the real implementations.
    login2 = game_module.Game.login2
    _open_candidate = game_module.Game._open_candidate
    _silence_report = game_module.Game._silence_report
    _remember_login_port = game_module.Game._remember_login_port
    _connection_failure_message = game_module.Game._connection_failure_message

    def __init__(self, ports=(13000, 13002), try_index=0, port=None):
        self.network = FakeNetwork("first")
        self._login_ports = tuple(ports)
        self._login_try = try_index
        if port is not None:
            self._login_port = port
        self.closed = 0
        self.replaced = []
        self.errors = []

    def replace(self, state):
        self.replaced.append(state)

    def _close_network(self, polite=True):
        self.closed += 1
        self.network = None

    def connection_error(self, message="Connection error [timeout]"):
        self.errors.append(message)


class LoginRetryFlowTests(unittest.TestCase):
    def test_a_silent_handshake_walks_to_the_port_above_it(self):
        game = RetryGame()
        opened = []

        def open_client(port=None):
            opened.append(port)
            return FakeNetwork("second")

        game._new_network_client = open_client
        with mock.patch.object(game_module, "speak") as speak:
            game_module.Game._retry_login(game)

        self.assertEqual(game.closed, 1, "the silent socket is released first")
        self.assertEqual(opened, [13002], "the retry opens the port above the endpoint")
        self.assertEqual(game.errors, [], "a retry that can still be made is not a report")
        self.assertEqual(game._login_port, 13002)
        self.assertEqual(len(game.replaced), 1)
        self.assertIs(
            game.replaced[0].__func__, game_module.Game.login2,
            "the retry re-enters the handshake wait rather than the menu",
        )
        self.assertEqual(
            speak.call_args[0][0], "Trying the connection again (2 of 2).",
            "the player is told the game is retrying, not dumped at the menu",
        )

    def test_the_remembered_port_is_tried_first_and_the_other_one_is_still_there(self):
        game = RetryGame(ports=(13002, 13000))
        opened = []
        game._new_network_client = lambda port=None: (opened.append(port), FakeNetwork("x"))[1]
        with mock.patch.object(game_module, "speak"):
            game_module.Game._retry_login(game)
        self.assertEqual(opened, [13000], "the fallback starting first does not remove the primary")

    def test_the_last_attempt_reports_a_connection_rather_than_an_account(self):
        game = RetryGame(ports=(13000, 13002), try_index=1)
        called = []
        game._new_network_client = lambda port=None: called.append(port)
        with mock.patch.object(game_module, "speak"):
            game_module.Game._retry_login(game)

        self.assertEqual(called, [], "there is no third attempt to make")
        self.assertEqual(game.replaced, [], "and nothing is re-entered")
        self.assertEqual(len(game.errors), 1)
        self.assertIn("No answer from the server", game.errors[0])
        self.assertIn("connection rather than your account", game.errors[0])
        self.assertIn("ports 13000 and 13002", game.errors[0])

    def test_a_candidate_that_cannot_be_opened_is_walked_past_to_the_next(self):
        """A filter that drops a port and a machine that will not open a socket
        are different failures with the same answer: try the next one. The walk
        is a walk, not two ports hardcoded, because the day there is a third
        candidate this has to keep working."""
        game = RetryGame(ports=(13000, 13002, 13003))
        opened = []

        def open_client(port=None):
            if port == 13002:
                raise OSError("socket unavailable")
            opened.append(port)
            return FakeNetwork("walked")

        game._new_network_client = open_client
        with mock.patch.object(game_module, "speak") as speak:
            game_module.Game._retry_login(game)

        self.assertEqual(opened, [13003], "the candidate that will not open is skipped")
        self.assertEqual(game.errors, [], "a candidate that opened is not a failure")
        self.assertEqual(game._login_port, 13003, "the port that opened is the one remembered")
        self.assertEqual(
            speak.call_args[0][0], "Trying the connection again (3 of 3).",
            "a candidate reached by walking past another still says where it is",
        )
        self.assertEqual(
            game.replaced and game.replaced[0].__func__, game_module.Game.login2,
            "and the handshake wait is re-entered on it",
        )

    def test_when_the_list_is_spent_the_error_that_stopped_the_last_one_is_reported(self):
        game = RetryGame()
        game._new_network_client = mock.Mock(side_effect=OSError("socket unavailable"))
        with mock.patch.object(game_module, "speak"):
            game_module.Game._retry_login(game)

        self.assertEqual(len(game.errors), 1)
        self.assertIn("socket unavailable", game.errors[0])
        self.assertEqual(game.replaced, [])

    def test_a_game_without_an_attempt_list_falls_back_to_a_plain_report(self):
        """The login flow is the only thing that fills the list in, so a
        hand-built Game must not crash on the way out."""
        game = RetryGame(ports=(), try_index=0)
        del game._login_ports
        with mock.patch.object(game_module, "speak"):
            game_module.Game._retry_login(game)
        self.assertEqual(len(game.errors), 1)
        self.assertIn("No answer from the server", game.errors[0])


class FirstAttemptTests(unittest.TestCase):
    def test_both_entry_points_open_the_first_candidate_and_remember_the_list(self):
        for entry in (game_module.Game._open_first_login_attempt,):
            game = RetryGame()
            del game._login_ports
            del game._login_try
            opened = []

            def open_client(port=None):
                opened.append(port)
                return FakeNetwork("opened")

            game._new_network_client = open_client
            with mock.patch.object(
                game_module.server_config,
                "get_server_endpoint",
                return_value=("example.invalid", consts.DEFAULT_PORT),
            ), mock.patch.object(game_module.options, "get_login_port", return_value=None):
                entry(game)

            self.assertEqual(opened, [consts.DEFAULT_PORT])
            self.assertEqual(game._login_ports, login_attempts.candidate_ports(consts.DEFAULT_PORT))
            self.assertEqual(game._login_port, consts.DEFAULT_PORT)
            self.assertEqual(game._login_try, 0)
            self.assertEqual(game.network.label, "opened")

    def test_a_login_starts_on_the_port_that_last_answered(self):
        game = RetryGame()
        del game._login_ports
        del game._login_try
        opened = []
        game._new_network_client = lambda port=None: (opened.append(port), FakeNetwork("opened"))[1]
        with mock.patch.object(
            game_module.server_config,
            "get_server_endpoint",
            return_value=("example.invalid", 13000),
        ), mock.patch.object(game_module.options, "get_login_port", return_value=13002):
            game_module.Game._open_first_login_attempt(game)

        self.assertEqual(opened, [13002], "the fallback that worked is opened first")
        self.assertEqual(game._login_ports, (13002, 13000))
        self.assertEqual(
            game._login_port, 13002,
            "and the port this login is on is the port it will remember",
        )

    def test_a_first_candidate_that_will_not_open_lands_on_the_port_above_it(self):
        """The configured port is where a filter shows up first, so failing to
        open a socket there must not be the end of the login."""
        game = RetryGame()
        del game._login_ports
        del game._login_try
        opened = []

        def open_client(port=None):
            if port == 13000:
                raise OSError("socket unavailable")
            opened.append(port)
            return FakeNetwork("fallback")

        game._new_network_client = open_client
        with mock.patch.object(
            game_module.server_config,
            "get_server_endpoint",
            return_value=("example.invalid", 13000),
        ), mock.patch.object(game_module.options, "get_login_port", return_value=None), \
                mock.patch.object(game_module, "speak") as speak:
            game_module.Game._open_first_login_attempt(game)

        self.assertEqual(opened, [13002])
        self.assertEqual(game._login_try, 1)
        self.assertEqual(game._login_port, 13002)
        self.assertEqual(
            speak.call_args[0][0], "Trying the connection again (2 of 2).",
            "the player is told the login moved to the port above the first one",
        )

    def test_no_candidate_will_open_reports_the_error_that_stopped_the_last_one(self):
        """``login()`` catches what this raises, so it has to be what a login
        that cannot open anything has always reported."""
        game = RetryGame()
        del game._login_ports
        del game._login_try
        game._new_network_client = mock.Mock(side_effect=OSError("socket unavailable"))
        with mock.patch.object(
            game_module.server_config,
            "get_server_endpoint",
            return_value=("example.invalid", 13000),
        ), mock.patch.object(game_module.options, "get_login_port", return_value=None), \
                mock.patch.object(game_module, "speak"):
            with self.assertRaises(OSError) as raised:
                game_module.Game._open_first_login_attempt(game)
        self.assertIn("socket unavailable", str(raised.exception))
        self.assertEqual(game._login_try, 2, "every candidate was tried")

    def test_the_reconnect_path_uses_the_same_opening(self):
        source = open(
            os.path.join(os.path.dirname(__file__), "..", "libs", "game.py"),
            "r", encoding="utf-8",
        ).read()
        reconnect = source.index("    def reconnect_state(self):")
        login = source.index("    def login(self):")
        retry = source.index("    def _retry_login(self):")
        self.assertIn(
            "_open_first_login_attempt()", source[reconnect:reconnect + 900],
            "a reconnect must know where a retry may go, exactly as a login does",
        )
        self.assertIn(
            "_open_first_login_attempt()", source[login:retry],
            "the login path opens its first attempt through the shared helper",
        )
        self.assertNotIn(
            "_new_network_client()", source[login:retry],
            "the login path must not open a socket the retry list knows nothing about",
        )


class RememberedPortTests(unittest.TestCase):
    def test_the_port_that_answered_is_kept(self):
        game = RetryGame(port=13002)
        with mock.patch.object(game_module.options, "get_login_port", return_value=13000), \
                mock.patch.object(game_module.options, "set_login_port") as saved:
            game_module.Game._remember_login_port(game)
        saved.assert_called_once_with(13002)

    def test_a_port_already_remembered_is_not_written_again(self):
        game = RetryGame(port=13002)
        with mock.patch.object(game_module.options, "get_login_port", return_value=13002), \
                mock.patch.object(game_module.options, "set_login_port") as saved:
            game_module.Game._remember_login_port(game)
        saved.assert_not_called()

    def test_a_login_without_a_port_records_nothing(self):
        game = RetryGame()
        with mock.patch.object(game_module.options, "get_login_port", return_value=None), \
                mock.patch.object(game_module.options, "set_login_port") as saved:
            game_module.Game._remember_login_port(game)
        saved.assert_not_called()

    def test_the_handshake_is_what_makes_a_port_worth_remembering(self):
        """It is the port the *transport* answered on, not the port that was
        asked for: a login that never got a packet back must not be recorded as
        the way in."""
        game = RetryGame(ports=(13000, 13002), port=13000)
        game.network = FakeTransport(game_module.enet.EVENT_TYPE_CONNECT)
        game._new_network_client = lambda port=None: FakeNetwork("x")
        with mock.patch.object(game_module, "speak"), \
                mock.patch.object(game_module.options, "get_login_port", return_value=13002), \
                mock.patch.object(game_module.options, "set_login_port") as saved:
            game_module.Game.login2(game)

        self.assertEqual(game.network.handshakes, 1)
        self.assertEqual(game.network.sent[0][1], "login")
        saved.assert_called_once_with(13000)

    def test_options_keeps_the_port_through_one_validator(self):
        source = open(
            os.path.join(os.path.dirname(__file__), "..", "libs", "options.py"),
            "r", encoding="utf-8",
        ).read()
        self.assertIn("def get_login_port():", source)
        self.assertIn("def set_login_port(port):", source)
        self.assertEqual(
            source.count("login_attempts.remembered_port("), 2,
            "the reader and the writer both go through the policy's validator",
        )


class ConnectionErrorMessageTests(unittest.TestCase):
    class NetworkStub:
        def __init__(self):
            self.closed = []

        def close_socket(self, polite=True):
            self.closed.append(polite)

    def make_game(self):
        game = game_module.Game.__new__(game_module.Game)
        game.network = self.NetworkStub()
        game.reconnecting = False
        game.replace = lambda state: None
        return game

    def test_the_caller_may_replace_the_line_the_player_hears(self):
        game = self.make_game()
        with mock.patch.object(game_module.menus, "main_menu"), \
                mock.patch.object(game_module, "speak") as speak:
            game_module.Game.connection_error(game, login_attempts.silence_message(2, (13000, 13002)))
        self.assertEqual(speak.call_count, 1)
        self.assertIn("No answer from the server", speak.call_args[0][0])

    def test_a_generic_timeout_keeps_the_line_it_always_had(self):
        game = self.make_game()
        with mock.patch.object(game_module.menus, "main_menu"), \
                mock.patch.object(game_module, "speak") as speak:
            game_module.Game.connection_error(game)
        self.assertEqual(speak.call_args[0][0], "Connection error [timeout]")


class OneOwnerTests(unittest.TestCase):
    """The retry policy has one reader and one writer: game.py."""

    def test_only_the_login_flow_retries(self):
        libs = os.path.join(os.path.dirname(__file__), "..", "libs")
        readers = []
        for name in sorted(os.listdir(libs)):
            if not name.endswith(".py"):
                continue
            with open(os.path.join(libs, name), "r", encoding="utf-8") as handle:
                if "login_attempts" in handle.read():
                    readers.append(name)
        self.assertEqual(readers, ["game.py", "options.py"], readers)

    def test_the_transport_worker_does_not_retry_behind_the_flow(self):
        with open(
            os.path.join(os.path.dirname(__file__), "..", "libs", "networking.py"),
            "r", encoding="utf-8",
        ) as handle:
            source = handle.read()
        self.assertNotIn(
            "login_attempts", source,
            "the silence watchdog owns its window; a second retry owner would "
            "hammer a server that is answering nothing",
        )


if __name__ == "__main__":
    unittest.main()
