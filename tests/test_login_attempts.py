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
7. An attempt is also an *address*: the endpoint's name first, then the backup
   addresses a release build embedded (server_config.get_fallback_addresses). A
   name this machine cannot look up is not asked twice, and a backup that turns
   out to be what the name resolved to is not dialled twice -- the walk never
   spends a silence window on a door it already knows is shut.
8. A door is a *(host, port)* pair, and the port walk survives the fallback: an
   address dialled on the configured port says nothing about the port above it,
   and a filter that drops one and passes the other is exactly why the second
   port is walked at all. `tests/login_backup_address_live.py` found this the
   hard way -- a unit test had it right only because its fake transport reported
   no resolved address, which the real one always does.
9. The retry notice counts doors, not list entries: the ports of a name with no
   answer are not attempts a player has waited on, so a login that falls back to
   an address is not told "2 of 2" over a door it never reached -- and both
   numbers describe the same walk the log's `tried=` list shows.
10. The sentence a login speaks before anything else is the same one as ever,
   but it is said before the walk rather than after it: "trying the connection
   again" before "connecting to the server" reads as a retry that never
   happened (a configured port this machine will not open).
11. The failure says which one it was. A name with no answer is the one case
   where the player's machine never sent a packet: the Server can see nothing,
   the connection log has nothing to read, and only this end can say so -- while
   a silent server keeps the sentence it always had.
12. One line goes to the client log whatever the outcome (attempt_report): the
   name, what this machine made of it, every door dialled and how it ended. It
   is the only witness for the players who reach neither the game nor the log.
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


def walk(*entries, host="server.example"):
    """A candidate list written the way a reader thinks of it: ports, or pairs."""
    return tuple(
        (host, entry) if isinstance(entry, int) else tuple(entry)
        for entry in entries
    )


class RetryGame:
    """The slice of Game that login2/_retry_login/_open_first_login_attempt use."""

    # _retry_login re-enters the flow by name, and both the port that answered
    # and the way a socket failure is reported are the real implementations.
    login2 = game_module.Game.login2
    _open_candidate = game_module.Game._open_candidate
    _silence_report = game_module.Game._silence_report
    _login_walk = game_module.Game._login_walk
    _login_failure_words = game_module.Game._login_failure_words
    _report_login_failure = game_module.Game._report_login_failure
    _report_login_reached = game_module.Game._report_login_reached
    _remember_login_port = game_module.Game._remember_login_port
    _connection_failure_message = game_module.Game._connection_failure_message

    def __init__(self, candidates=None, try_index=0, port=None, host="server.example"):
        self.network = FakeNetwork("first")
        self._login_name = host
        self._login_candidates = (
            login_attempts.candidate_addresses(host, 13000)
            if candidates is None
            else tuple(candidates)
        )
        self._login_try = try_index
        if port is not None:
            self._login_port = port
        self.closed = 0
        self.replaced = []
        self.errors = []

    # The player pressing log in, and the stack a failure lands on: both are
    # the real code, so the order the sentences come out in is the real order.
    login = game_module.Game.login

    def replace(self, state):
        self.replaced.append(state)

    def pop(self):
        self.popped = getattr(self, "popped", 0) + 1
        return None

    def _close_network(self, polite=True):
        self.closed += 1
        self.network = None

    def connection_error(self, message="Connection error [timeout]"):
        self.errors.append(message)


class LoginRetryFlowTests(unittest.TestCase):
    def test_a_silent_handshake_walks_to_the_port_above_it(self):
        game = RetryGame()
        opened = []

        def open_client(address=None):
            opened.append(address)
            return FakeNetwork("second")

        game._new_network_client = open_client
        with mock.patch.object(game_module, "speak") as speak:
            game_module.Game._retry_login(game)

        self.assertEqual(game.closed, 1, "the silent socket is released first")
        self.assertEqual(
            opened, [("server.example", 13002)],
            "the retry opens the port above the endpoint, on the same name",
        )
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
        game = RetryGame(candidates=walk(13002, 13000))
        opened = []
        game._new_network_client = lambda address=None: (
            opened.append(address), FakeNetwork("x")
        )[1]
        with mock.patch.object(game_module, "speak"):
            game_module.Game._retry_login(game)
        self.assertEqual(
            opened, [("server.example", 13000)],
            "the fallback starting first does not remove the primary",
        )

    def test_the_last_attempt_reports_a_connection_rather_than_an_account(self):
        game = RetryGame(candidates=walk(13000, 13002), try_index=1)
        called = []
        game._new_network_client = lambda address=None: called.append(address)
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
        game = RetryGame(candidates=walk(13000, 13002, 13003))
        opened = []

        def open_client(address=None):
            if address[1] == 13002:
                raise OSError("socket unavailable")
            opened.append(address)
            return FakeNetwork("walked")

        game._new_network_client = open_client
        with mock.patch.object(game_module, "speak") as speak:
            game_module.Game._retry_login(game)

        self.assertEqual(
            opened, [("server.example", 13003)],
            "the candidate that will not open is skipped",
        )
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
        game = RetryGame(candidates=(), try_index=0)
        del game._login_candidates
        with mock.patch.object(game_module, "speak"):
            game_module.Game._retry_login(game)
        self.assertEqual(len(game.errors), 1)
        self.assertIn("No answer from the server", game.errors[0])


class FirstAttemptTests(unittest.TestCase):
    def test_both_entry_points_open_the_first_candidate_and_remember_the_list(self):
        for entry in (game_module.Game._open_first_login_attempt,):
            game = RetryGame()
            del game._login_candidates
            del game._login_try
            opened = []

            def open_client(address=None):
                opened.append(address)
                return FakeNetwork("opened")

            game._new_network_client = open_client
            with mock.patch.object(
                game_module.server_config,
                "get_server_endpoint",
                return_value=("example.invalid", consts.DEFAULT_PORT),
            ), mock.patch.object(
                game_module.server_config, "get_fallback_addresses", return_value=()
            ), mock.patch.object(game_module.options, "get_login_port", return_value=None):
                entry(game)

            self.assertEqual(opened, [("example.invalid", consts.DEFAULT_PORT)])
            self.assertEqual(login_attempts.candidate_ports(consts.DEFAULT_PORT), (13000, 13002))
            self.assertEqual(
                game._login_candidates, walk(13000, 13002, host="example.invalid"),
                "the endpoint and the port above it, on the endpoint's own name",
            )
            self.assertEqual(game._login_name, "example.invalid")
            self.assertEqual(game._login_port, consts.DEFAULT_PORT)
            self.assertEqual(game._login_try, 0)
            self.assertEqual(game.network.label, "opened")

    def test_a_login_starts_on_the_port_that_last_answered(self):
        game = RetryGame()
        del game._login_candidates
        del game._login_try
        opened = []
        game._new_network_client = lambda address=None: (
            opened.append(address), FakeNetwork("opened")
        )[1]
        with mock.patch.object(
            game_module.server_config,
            "get_server_endpoint",
            return_value=("example.invalid", 13000),
        ), mock.patch.object(
            game_module.server_config, "get_fallback_addresses", return_value=()
        ), mock.patch.object(game_module.options, "get_login_port", return_value=13002):
            game_module.Game._open_first_login_attempt(game)

        self.assertEqual(
            opened, [("example.invalid", 13002)],
            "the fallback that worked is opened first",
        )
        self.assertEqual(
            game._login_candidates,
            walk(13002, 13000, host="example.invalid"),
            "the port that answered is walked first on this name",
        )
        self.assertEqual(
            game._login_port, 13002,
            "and the port this login is on is the port it will remember",
        )

    def test_a_first_candidate_that_will_not_open_lands_on_the_port_above_it(self):
        """The configured port is where a filter shows up first, so failing to
        open a socket there must not be the end of the login."""
        game = RetryGame()
        del game._login_candidates
        del game._login_try
        opened = []

        def open_client(address=None):
            if address[1] == 13000:
                raise OSError("socket unavailable")
            opened.append(address)
            return FakeNetwork("fallback")

        game._new_network_client = open_client
        with mock.patch.object(
            game_module.server_config,
            "get_server_endpoint",
            return_value=("example.invalid", 13000),
        ), mock.patch.object(
            game_module.server_config, "get_fallback_addresses", return_value=()
        ), mock.patch.object(game_module.options, "get_login_port", return_value=None), \
                mock.patch.object(game_module, "speak") as speak:
            game_module.Game._open_first_login_attempt(game)

        self.assertEqual(opened, [("example.invalid", 13002)])
        self.assertEqual(game._login_try, 1)
        self.assertEqual(game._login_port, 13002)
        self.assertEqual(
            speak.call_args[0][0], "Trying the connection again (2 of 2).",
            "the player is told the login moved to the port above the first one",
        )

    def test_the_sentence_a_login_speaks_comes_before_the_walk(self):
        """A retry notice is spoken from inside the walk (a configured port this
        machine will not open), and "trying the connection again" said before
        "connecting to the server" is a retry the player never had."""
        game = RetryGame()
        order = []
        game._open_first_login_attempt = lambda: order.append("walk")
        with mock.patch.object(
            game_module.options,
            "get",
            lambda key, default=None: {
                "username": "player",
                "password": "secret",
            }.get(key, default),
        ), mock.patch.object(
            game_module, "speak", lambda text, *rest: order.append(text)
        ):
            game_module.Game.login(game)

        self.assertEqual(
            order,
            ["Connecting to the server. Please wait...", "walk"],
            "the connecting line is said before the walk can speak about retrying",
        )
        self.assertEqual(
            game.replaced and game.replaced[0].__func__, game_module.Game.login2,
            "and the login still waits for the handshake when the walk opened one",
        )

    def test_a_login_walks_to_the_backup_address_the_pack_embedded(self):
        """The failure this exists for: the name is not the door this machine can
        open, and a release build carries a second one that needs no DNS."""
        game = RetryGame()
        del game._login_candidates
        del game._login_try
        opened = []

        def open_client(address=None):
            if address[0] == "example.invalid":
                raise game_module.server_config.NameLookupError(
                    "The server's name could not be looked up: example.invalid"
                )
            opened.append(address)
            network = FakeNetwork("backup")
            network.resolved_host = address[0]
            return network

        game._new_network_client = open_client
        with mock.patch.object(
            game_module.server_config,
            "get_server_endpoint",
            return_value=("example.invalid", 13000),
        ), mock.patch.object(
            game_module.server_config,
            "get_fallback_addresses",
            return_value=("103.30.126.64",),
        ), mock.patch.object(game_module.options, "get_login_port", return_value=None), \
                mock.patch.object(game_module, "speak") as speak:
            game_module.Game._open_first_login_attempt(game)

        self.assertEqual(
            opened, [("103.30.126.64", 13000)],
            "the backup address is dialled, and only once",
        )
        self.assertEqual(
            game._login_walk()["unresolvable"], {"example.invalid"},
            "the name is recorded as unanswerable, so no port of it is tried again",
        )
        self.assertTrue(game._login_walk()["backup"])
        self.assertEqual(
            [call.args[0] for call in speak.call_args_list],
            [],
            "nothing is said about retrying: the name was never a door this "
            "machine could knock on, and the login has not yet tried one it can",
        )

    def test_no_candidate_will_open_reports_the_error_that_stopped_the_last_one(self):
        """``login()`` catches what this raises, so it has to be what a login
        that cannot open anything has always reported."""
        game = RetryGame()
        del game._login_candidates
        del game._login_try
        game._new_network_client = mock.Mock(side_effect=OSError("socket unavailable"))
        with mock.patch.object(
            game_module.server_config,
            "get_server_endpoint",
            return_value=("example.invalid", 13000),
        ), mock.patch.object(
            game_module.server_config, "get_fallback_addresses", return_value=()
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
        game = RetryGame(candidates=walk(13000, 13002), port=13000)
        game.network = FakeTransport(game_module.enet.EVENT_TYPE_CONNECT)
        game._new_network_client = lambda address=None: FakeNetwork("x")
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


class CandidateAddressTests(unittest.TestCase):
    """What a login walks: the name, then the addresses, and no door twice."""

    def test_without_addresses_the_walk_is_exactly_what_it_always_was(self):
        self.assertEqual(
            login_attempts.candidate_addresses("server.example", 13000),
            tuple(
                ("server.example", port)
                for port in login_attempts.candidate_ports(13000)
            ),
        )

    def test_the_name_is_first_and_every_address_follows_it(self):
        self.assertEqual(
            login_attempts.candidate_addresses(
                "server.example", 13000, ("103.30.126.64",)
            ),
            (
                ("server.example", 13000),
                ("server.example", 13002),
                ("103.30.126.64", 13000),
                ("103.30.126.64", 13002),
            ),
        )

    def test_the_port_that_answered_is_first_on_every_address(self):
        self.assertEqual(
            login_attempts.candidate_addresses(
                "server.example", 13000, ("103.30.126.64",), preferred=13002
            )[2:],
            (("103.30.126.64", 13002), ("103.30.126.64", 13000)),
        )

    def test_a_backup_that_is_the_endpoint_is_not_a_second_door(self):
        for host in ("server.example", "SERVER.EXAMPLE"):
            self.assertEqual(
                login_attempts.candidate_addresses("server.example", 13000, (host,)),
                (("server.example", 13000), ("server.example", 13002)),
            )

    def test_a_repeated_address_is_listed_once(self):
        self.assertEqual(
            login_attempts.candidate_addresses(
                "server.example", 13000, ("103.30.126.64", "103.30.126.64")
            ),
            (
                ("server.example", 13000),
                ("server.example", 13002),
                ("103.30.126.64", 13000),
                ("103.30.126.64", 13002),
            ),
        )

    def test_an_entry_that_is_not_an_address_is_ignored(self):
        self.assertEqual(
            login_attempts.candidate_addresses("server.example", 13000, (None, "", 7)),
            (("server.example", 13000), ("server.example", 13002)),
        )

    def test_a_name_that_could_not_be_looked_up_is_retired_for_every_port(self):
        self.assertFalse(
            login_attempts.candidate_available(
                ("server.example", 13002), ("SERVER.EXAMPLE",)
            )
        )
        self.assertTrue(
            login_attempts.candidate_available(
                ("103.30.126.64", 13000), ("server.example",)
            )
        )

    def test_an_address_already_dialled_is_not_dialled_again(self):
        """The backup that turns out to be what the name resolved to: the walk
        reaches it, recognises the door, and buys nothing but the wait."""
        self.assertFalse(
            login_attempts.candidate_available(
                ("103.30.126.64", 13000), (), (("103.30.126.64", 13000),)
            )
        )
        self.assertTrue(
            login_attempts.candidate_available(
                ("103.30.126.64", 13000), (), (("10.0.0.1", 13000),)
            )
        )

    def test_an_address_dialled_on_one_port_still_has_the_other_one(self):
        """A door is an address *and* a port. Retiring the whole address when one
        of its ports was dialled used to leave a login with a backup address one
        door instead of two -- and a filter that drops the configured port is
        the reason the second one exists, on a backup no less than on the name.
        """
        dialled = (("103.30.126.64", 13000),)
        self.assertTrue(
            login_attempts.candidate_available(("103.30.126.64", 13002), (), dialled)
        )
        self.assertFalse(
            login_attempts.candidate_available(("103.30.126.64", 13000), (), dialled)
        )

    def test_the_count_a_notice_reads_is_the_doors_that_will_be_dialled(self):
        candidates = login_attempts.candidate_addresses(
            "server.example", 13000, ("103.30.126.64",)
        )
        self.assertEqual(login_attempts.diallable_count(candidates), 4)
        self.assertEqual(
            login_attempts.diallable_count(candidates, ("server.example",)), 2
        )
        self.assertEqual(
            login_attempts.diallable_count(
                candidates, ("server.example",), (("103.30.126.64", 13000),)
            ),
            1,
            "the port above the one that was dialled is still worth trying",
        )
        self.assertEqual(
            login_attempts.diallable_count(
                candidates,
                ("server.example",),
                (("103.30.126.64", 13000), ("103.30.126.64", 13002)),
            ),
            0,
        )

    def test_the_notice_counts_doors_a_login_really_knocked_on(self):
        """A name with no answer is not an attempt the player waited on: the walk
        never opened a socket for it, so a login falling back to an address is on
        its first door, not its third list entry."""
        candidates = login_attempts.candidate_addresses(
            "server.example", 13000, ("103.30.126.64",)
        )
        name_died = ("server.example",)
        self.assertEqual(
            login_attempts.retry_position(candidates, 2, name_died), (1, 2)
        )
        self.assertEqual(
            login_attempts.retry_position(
                candidates, 3, name_died, (("103.30.126.64", 13000),)
            ),
            (2, 2),
            "the second door of the backup is the second of two it will dial",
        )
        # Nothing was retired: the port above the configured one is door two of
        # two, exactly as it has always been said.
        ports_only = login_attempts.candidate_addresses("server.example", 13000)
        self.assertEqual(
            login_attempts.retry_position(
                ports_only, 1, (), (("server.example", 13000),)
            ),
            (2, 2),
        )
        self.assertEqual(
            login_attempts.doors_before(ports_only, 1), 1,
            "a candidate that was tried counts even when its socket would not open",
        )

    def test_a_backup_is_a_candidate_that_is_not_the_endpoints_name(self):
        self.assertFalse(
            login_attempts.is_backup(("server.example", 13000), "server.example")
        )
        self.assertFalse(
            login_attempts.is_backup(("SERVER.example", 13000), "server.example")
        )
        self.assertTrue(
            login_attempts.is_backup(("103.30.126.64", 13000), "server.example")
        )


class ResolutionMessageTests(unittest.TestCase):
    """A name with no answer is said out loud, never as a silence."""

    def test_the_message_says_nothing_was_sent_and_whose_problem_it_is(self):
        message = login_attempts.resolution_message()
        self.assertIn("name could not be looked up", message)
        self.assertIn("Nothing was ever sent", message)
        self.assertIn("DNS", message)
        self.assertIn("mobile hotspot", message)
        self.assertNotIn("No answer from the server", message)

    def test_the_message_can_name_no_endpoint(self):
        """It is never handed one, and a released build says nothing about where
        the official server is (test_server_config pins the same rule)."""
        for message in (
            login_attempts.resolution_message(),
            login_attempts.resolution_message(backup_tried=True),
        ):
            self.assertNotIn("http", message)
            self.assertNotRegex(message, r"\S+:\d", "no host:port may be spelled out")

    def test_a_backup_that_was_tried_says_so_and_keeps_the_kind_of_failure(self):
        message = login_attempts.resolution_message(backup_tried=True)
        self.assertIn("backup address was tried", message)
        self.assertIn("connection rather than your account", message)
        self.assertNotIn("Nothing was ever sent", message)

    def test_a_public_resolver_that_was_asked_is_named(self):
        """By the time this sentence is reached the name has already been put to a
        public resolver over HTTPS (``server_config.resolve_host``), so the
        reader should be told two ways of answering it failed rather than one --
        the advice is the same, the diagnosis is not."""
        plain = login_attempts.resolution_message()
        asked = login_attempts.resolution_message(public_resolver_tried=True)
        self.assertNotIn("public resolver", plain)
        self.assertIn(
            "could not be looked up by this computer or by a public resolver over", asked
        )
        self.assertIn("Nothing was ever sent", asked)
        self.assertIn("try a mobile hotspot", asked)
        self.assertNotIn(
            "so this is this computer's DNS rather than the server",
            asked,
            "two ways of answering the name failed, not one",
        )
        self.assertIn("this computer's connection rather than your account", asked)
        self.assertNotIn("No answer from the server", asked)

    def test_the_public_resolver_is_named_alongside_the_backup(self):
        both = login_attempts.resolution_message(
            backup_tried=True, public_resolver_tried=True
        )
        self.assertIn("public resolver over the internet", both)
        self.assertIn("backup address was tried", both)
        self.assertIn("connection rather than your account", both)

    def test_the_silent_message_names_the_backup_only_when_there_was_one(self):
        without = login_attempts.silence_message(2, (13000, 13002))
        self.assertNotIn("backup", without)
        with_backup = login_attempts.silence_message(2, (13000, 13002), True)
        self.assertIn("backup address was tried as well", with_backup)
        self.assertIn(
            "No answer from the server after 2 tries on ports 13000 and 13002.",
            with_backup,
        )
        self.assertIn("connection log", with_backup)


class AttemptReportTests(unittest.TestCase):
    """The one line the client log keeps -- the only witness for the players
    whose packets never left the machine."""

    def line(self, **kwargs):
        return login_attempts.attempt_report("server.example", **kwargs)

    def test_every_field_is_a_key_and_a_value(self):
        line = self.line(
            tried=(("server.example", 13000), ("103.30.126.64", 13002)),
            resolved="103.30.126.64",
            answered=("103.30.126.64", 13002),
            backup_tried=True,
        )
        self.assertTrue(line.startswith("[LOGIN] "), line)
        fields = dict(part.split("=", 1) for part in line[len("[LOGIN] "):].split(" "))
        self.assertEqual(fields["host"], "server.example")
        self.assertEqual(fields["resolved"], "103.30.126.64")
        self.assertEqual(fields["answered"], "103.30.126.64:13002")
        self.assertEqual(fields["outcome"], "answered")
        self.assertEqual(fields["tried"], "server.example:13000,103.30.126.64:13002")
        self.assertEqual(fields["backup"], "yes")

    def test_how_the_address_was_found_is_on_the_line(self):
        """A player who only gets in through a public resolver has a DNS problem,
        and the one line is where that is known first."""
        for lookup in ("literal", "local", "public", "none"):
            with self.subTest(lookup=lookup):
                self.assertIn(
                    f"lookup={lookup}", self.line(resolved="103.30.126.64", lookup=lookup)
                )
        self.assertNotIn(
            "lookup=", self.line(resolved="103.30.126.64"),
            "a report that is not about a resolution says nothing about one",
        )

    def test_a_name_with_no_answer_says_so_and_what_was_tried_instead(self):
        line = self.line(
            tried=(("server.example", 13000), ("103.30.126.64", 13000)),
            lookup_failed=True,
            backup_tried=True,
        )
        self.assertIn("resolved=none", line)
        self.assertIn("outcome=name-not-looked-up", line)
        self.assertIn("tried=server.example:13000,103.30.126.64:13000", line)
        self.assertIn("backup=yes", line)

    def test_a_silent_walk_says_no_answer(self):
        line = self.line(tried=(("server.example", 13000),))
        self.assertIn("outcome=no-answer", line)
        self.assertNotIn("resolved=", line)

    def test_an_error_stays_on_one_line(self):
        line = self.line(error=OSError("socket unavailable\r\nsecond line"))
        self.assertIn("error=socket unavailable second line", line)
        self.assertNotIn("\n", line)
        self.assertNotIn("\r", line)

    def test_a_report_without_a_host_still_reads(self):
        self.assertIn("host=unknown", login_attempts.attempt_report(None))


class LoginFailureWordTests(unittest.TestCase):
    """What the player hears, and what the log keeps, depend on what happened."""

    def failing(self, error):
        # The second candidate is the one that fails, so the walk really ends on
        # the error rather than never reaching it.
        game = RetryGame(candidates=walk(13000, 13002), try_index=0)
        game._new_network_client = mock.Mock(side_effect=error)
        with mock.patch.object(game_module, "speak"), mock.patch.object(
            game_module, "log"
        ) as log:
            game_module.Game._retry_login(game)
        return game, log

    def test_a_name_with_no_answer_is_not_reported_as_a_silent_server(self):
        game, log = self.failing(
            game_module.server_config.NameLookupError("could not look up")
        )
        self.assertEqual(len(game.errors), 1)
        self.assertIn("name could not be looked up", game.errors[0])
        self.assertNotIn("No answer from the server", game.errors[0])
        self.assertIn("outcome=name-not-looked-up", log.call_args[0][0])

    def test_the_log_gets_one_line_with_the_error_whatever_the_failure(self):
        for error in (
            game_module.server_config.NameLookupError("could not look up"),
            OSError("socket unavailable"),
        ):
            game, log = self.failing(error)
            self.assertEqual(log.call_count, 1, error)
            line = log.call_args[0][0]
            self.assertTrue(line.startswith("[LOGIN] "), line)
            self.assertIn("error=", line, error)

    def test_the_line_says_how_the_opened_door_was_found(self):
        game = RetryGame(candidates=walk(("103.30.126.64", 13000)))
        game._login_walk()["dialled"].append(("103.30.126.64", 13000))
        game._login_walk()["lookup"] = "public"

        class HomeNetwork:
            resolved_host = "103.30.126.64"
            resolved_via = "public"

        game.network = HomeNetwork()
        with mock.patch.object(game_module, "log") as log:
            game_module.Game._report_login_reached(game)
        self.assertIn("lookup=public", log.call_args[0][0])

    def test_a_walk_that_found_no_way_to_answer_the_name_says_so(self):
        game, log = self.failing(OSError("socket unavailable"))
        self.assertIn("lookup=none", log.call_args[0][0])

    def test_the_message_names_the_public_resolver_the_build_has(self):
        game = RetryGame()
        game._login_walk()["lookup_failed"] = True
        with mock.patch.object(
            game_module.server_config, "public_lookup_enabled", return_value=True
        ):
            self.assertIn(
                "public resolver over the internet",
                game_module.Game._login_failure_words(game),
            )
        with mock.patch.object(
            game_module.server_config, "public_lookup_enabled", return_value=False
        ):
            self.assertNotIn(
                "public resolver",
                game_module.Game._login_failure_words(game),
                "a build that cannot ask one must not claim it did",
            )

    def test_a_walk_that_only_stayed_silent_keeps_the_silence_wording(self):
        game = RetryGame(candidates=walk(13000, 13002), try_index=1)
        game._new_network_client = mock.Mock()
        game._login_walk()["dialled"].extend(walk(13000, 13002))
        with mock.patch.object(game_module, "speak"), mock.patch.object(
            game_module, "log"
        ) as log:
            game_module.Game._retry_login(game)
        self.assertIn("No answer from the server", game.errors[0])
        self.assertIn("No answer from the server", game.errors[0])
        self.assertIn("outcome=no-answer", log.call_args[0][0])

    def test_a_name_with_no_answer_that_a_backup_could_not_save_says_both(self):
        """The whole point, end to end: the name is not asked twice, the backup
        is dialled on both ports, and the last word names the kind of failure --
        while the log names the host the player is never told."""
        game = RetryGame()
        del game._login_candidates
        del game._login_try
        dialled = []

        def open_client(address=None):
            if address[0] == "example.invalid":
                raise game_module.server_config.NameLookupError(
                    "The server's name could not be looked up: example.invalid"
                )
            dialled.append(address)
            network = FakeNetwork("backup")
            # The real transport always knows the address it went to, and that
            # is what the walk retires a door by -- a fake without it made the
            # backup look one door wider than it is.
            network.resolved_host = address[0]
            return network

        game._new_network_client = open_client
        with mock.patch.object(
            game_module.server_config,
            "get_server_endpoint",
            return_value=("example.invalid", 13000),
        ), mock.patch.object(
            game_module.server_config,
            "get_fallback_addresses",
            return_value=("103.30.126.64",),
        ), mock.patch.object(game_module.options, "get_login_port", return_value=None), \
                mock.patch.object(game_module, "speak"), \
                mock.patch.object(game_module, "log") as log:
            game_module.Game._open_first_login_attempt(game)
            game_module.Game._retry_login(game)
            # The walk is spent and nothing answered; the name is still the most
            # specific thing known about it.
            message = game_module.Game._report_login_failure(game)

        self.assertEqual(
            dialled,
            [("103.30.126.64", 13000), ("103.30.126.64", 13002)],
            "both ports of the backup are dialled, and the name never again",
        )
        self.assertEqual(
            game._login_walk()["unresolvable"], {"example.invalid"}
        )
        self.assertIn("backup address was tried", message)
        self.assertIn("connection rather than your account", message)
        self.assertNotIn("example.invalid", message, "the player is told no endpoint")
        line = log.call_args[0][0]
        self.assertIn("host=example.invalid", line)
        self.assertIn("resolved=none", line)
        self.assertIn("tried=103.30.126.64:13000,103.30.126.64:13002", line)
        self.assertIn("backup=yes", line)

    def test_a_backup_address_is_walked_on_both_of_its_ports(self):
        """A door is an address *and* a port. The rig found this one: the walk
        fell back to an address and then treated it as a single door, so the
        port above the configured one was never tried on the very path that
        exists for the players who cannot be reached the ordinary way."""
        game = RetryGame()
        del game._login_candidates
        del game._login_try
        dialled = []
        said = []

        def open_client(address=None):
            if address[0] == "example.invalid":
                raise game_module.server_config.NameLookupError("no answer")
            dialled.append(address)
            network = FakeNetwork("backup")
            network.resolved_host = address[0]
            return network

        game._new_network_client = open_client
        with mock.patch.object(
            game_module.server_config,
            "get_server_endpoint",
            return_value=("example.invalid", 13000),
        ), mock.patch.object(
            game_module.server_config,
            "get_fallback_addresses",
            return_value=("103.30.126.64",),
        ), mock.patch.object(game_module.options, "get_login_port", return_value=None), \
                mock.patch.object(
                    game_module, "speak", lambda text, *rest: said.append(text)
                ), mock.patch.object(game_module, "log") as log:
            game_module.Game._open_first_login_attempt(game)
            game_module.Game._retry_login(game)
            game_module.Game._retry_login(game)

        self.assertEqual(
            dialled,
            [("103.30.126.64", 13000), ("103.30.126.64", 13002)],
            "both ports of the backup address are doors, exactly as the name's are",
        )
        self.assertEqual(
            said,
            ["Trying the connection again (2 of 2)."],
            "the second backup door is the second of the two doors this login dials",
        )
        self.assertIn("backup address was tried", game.errors[0])
        line = log.call_args[0][0]
        self.assertIn("tried=103.30.126.64:13000,103.30.126.64:13002", line)
        self.assertIn("outcome=name-not-looked-up", line)


class BackupThatCostsNothingTests(unittest.TestCase):
    def test_the_backup_that_is_the_names_own_address_is_skipped(self):
        """A login whose name resolves and whose server is merely quiet must not
        pay two extra silence windows to rediscover its own address."""
        game = RetryGame(
            candidates=walk(
                ("server.example", 13000),
                ("server.example", 13002),
                ("103.30.126.64", 13000),
                ("103.30.126.64", 13002),
            ),
            try_index=1,
        )
        # What the two candidates before it already did: each dialled, each
        # resolved to that address, and the server stayed quiet on both -- so
        # both of the backup's doors are doors this login has already knocked
        # on, and neither is worth another silence window.
        facts = game._login_walk()
        for port in (13000, 13002):
            facts["dialled"].append(("server.example", port))
            facts["doors"].add(("server.example", port))
            facts["doors"].add(("103.30.126.64", port))
        game._new_network_client = mock.Mock()
        with mock.patch.object(game_module, "speak"), mock.patch.object(
            game_module, "log"
        ):
            game_module.Game._retry_login(game)

        self.assertEqual(
            game._new_network_client.call_count, 0,
            "the address the name already resolved to is the same door",
        )
        self.assertIn("No answer from the server", game.errors[0])
        self.assertNotIn("backup address was tried", game.errors[0])

    def test_a_login_that_opened_records_which_door_it_was(self):
        class HomeNetwork:
            resolved_host = "103.30.126.64"

        game = RetryGame(candidates=walk(("103.30.126.64", 13000)), port=13000)
        game._login_walk()["dialled"].append(("103.30.126.64", 13000))
        game._login_walk()["backup"] = True
        game.network = HomeNetwork()
        with mock.patch.object(game_module, "log") as log:
            game_module.Game._report_login_reached(game)

        line = log.call_args[0][0]
        self.assertIn("answered=103.30.126.64:13000", line)
        self.assertIn("resolved=103.30.126.64", line)
        self.assertIn("outcome=answered", line)
        self.assertIn(
            "backup=yes",
            line,
            "a player who only gets in through a backup address has a DNS problem",
        )


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
