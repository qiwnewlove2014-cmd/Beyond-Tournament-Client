"""Regression tests: a login attempt is abandoned on *silence* from the server,
never on a stopwatch started at the "log in" button.

The reported bug: on a slow connection the client said "Connecting...", the
server finished authenticating (the account really was on the map), and the
client bounced back to the main menu about five seconds in. The server was left
holding a session nobody owned, so the player could not get straight back in
("user already logged in") until ENet's own timeout reaped the peer.

Pinned here:

1. ``consts.TIMEOUT`` used to be a budget measured from the moment the client
   was built, covering handshake + database lookup + snapshot together.
2. ``Client.loop``'s timeout branch was gated on ``not self.connected``, but
   ``game.login2`` consumes the CONNECT event itself, so ``connected`` was only
   ever set by the login snapshot -- one clock governed both waits. (Worse: had
   the event been left to ``Client.loop``, the flag would have disarmed the
   watchdog for the whole login wait.)
3. The timeout path returned to the menu WITHOUT closing the socket, leaving a
   registered-but-unserviced peer behind on the server.
"""

import json
import os
import queue
import sys
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import enet

from libs import consts
from libs import game as game_module
from libs import networking


class ClockStub:
    """Game-time clock, like libs/clock.py, but the test owns ``elapsed``."""

    def __init__(self):
        self.elapsed = 0.0

    def restart(self):
        self.elapsed = 0


class GameStub:
    def __init__(self):
        self.lock = threading.RLock()
        self.queue = queue.SimpleQueue()
        self.get = self.queue.get_nowait
        self.clock = ClockStub()
        self.errors = []
        self.replacing = []

    def new_clock(self):
        return self.clock

    def put(self, value):
        # The real Game queues the callback and drains it on the main thread
        # inside the frame; running it here is the same contract, observed
        # synchronously by the test.
        if callable(value):
            value()
        else:
            self.queue.put_nowait(value)

    def connection_error(self):
        self.errors.append("connection_error")

    def disconnected(self):
        self.errors.append("disconnected")

    def replace(self, st):
        self.replacing.append(st)


class FakeEventHandler:
    def __init__(self, client, game):
        self.client = client
        self.game = game

    def ping(self, data):
        pass


class FakePeer:
    def __init__(self):
        self.sent = []
        self.disconnected = False

    def send(self, channel, packet):
        self.sent.append((channel, packet))

    def disconnect(self):
        self.disconnected = True


class FakeNet:
    """Serviceable ENet host stub: events are pushed by the test."""

    def __init__(self):
        self.events = []
        self.flushed = 0

    def push(self, event_type, channel=0, packet=None):
        class Event:
            pass

        event = Event()
        event.type = event_type
        event.channelID = channel
        event.packet = packet
        self.events.append(event)

    def service(self, timeout):
        if self.events:
            return self.events.pop(0)

        class Idle:
            type = enet.EVENT_TYPE_NONE
            channelID = 0
            packet = None

        return Idle()

    def flush(self):
        self.flushed += 1


def ping_packet():
    return enet.Packet(
        json.dumps({"event": "ping", "data": {}}).encode(),
        flags=enet.PACKET_FLAG_RELIABLE,
    )


def make_client(test):
    """A real networking.Client on a scriptable transport.

    The worker is left idle (should_poll False, so it only drains its queue)
    and is released in tearDown: a suite that leaves enet hosts spinning
    perturbs every timing-sensitive test after it.
    """
    game = GameStub()
    client = networking.Client(game, "127.0.0.1", 1, FakeEventHandler)
    client.should_poll = False  # thread starts idle; deterministic from here
    client.net = FakeNet()
    client.peer = FakePeer()
    test.clients.append(client)
    return game, client


class ClientTestCase(unittest.TestCase):
    def setUp(self):
        self.clients = []

    def tearDown(self):
        for client in self.clients:
            client.close_socket()
            client.join(timeout=2.0)

    def make_client(self):
        return make_client(self)


class LoginWatchdogTests(ClientTestCase):
    def test_the_watchdog_measures_silence_not_total_wait(self):
        """A slow server that keeps answering is waited for, however long the
        login takes in total."""
        game, client = self.make_client()
        game.clock.elapsed = consts.TIMEOUT - 1
        client.loop()
        self.assertEqual(game.errors, [], "just under the window must not abort")

        # A packet from the server is a sign of life: the window restarts.
        client.net.push(enet.EVENT_TYPE_RECEIVE, packet=ping_packet())
        client.loop()
        self.assertEqual(
            game.clock.elapsed, 0, "a packet from the server must restart the clock"
        )

        # Total time on this attempt is now past two windows; still no abort.
        game.clock.elapsed = consts.TIMEOUT - 1
        client.loop()
        self.assertEqual(game.errors, [])
        self.assertFalse(client.disconnected)

    def test_timeout_needs_a_full_window_of_silence(self):
        game, client = self.make_client()
        game.clock.elapsed = consts.TIMEOUT
        client.loop()
        self.assertEqual(game.errors, ["connection_error"])
        self.assertTrue(client.disconnected)

    def test_the_window_is_eight_seconds(self):
        self.assertEqual(consts.TIMEOUT, 8000)

    def test_the_handshake_does_not_disarm_the_watchdog(self):
        """The transport being up says nothing about the login being finished."""
        game, client = self.make_client()
        client.net.push(enet.EVENT_TYPE_CONNECT)
        client.loop()
        self.assertTrue(client.connected)
        self.assertEqual(game.clock.elapsed, 0, "the handshake is a sign of life")
        self.assertFalse(client.logged_in)

        game.clock.elapsed = consts.TIMEOUT
        client.loop()
        self.assertEqual(
            game.errors,
            ["connection_error"],
            "a handshake must not switch off the login watchdog",
        )

    def test_a_finished_login_ends_the_watchdog(self):
        game, client = self.make_client()
        client.logged_in = True
        game.clock.elapsed = 10 * consts.TIMEOUT
        client.loop()
        self.assertEqual(game.errors, [])
        self.assertFalse(client.login_timed_out())

    def test_sending_a_request_restarts_the_window(self):
        """The server owes us an answer from the moment the request is out."""
        game, client = self.make_client()
        game.clock.elapsed = consts.TIMEOUT - 1
        client.send2(consts.CHANNEL_MISC, "login", {"username": "tester"}, True)
        self.assertEqual(game.clock.elapsed, 0)
        self.assertEqual(len(client.peer.sent), 1)
        channel, packet = client.peer.sent[0]
        self.assertEqual(channel, consts.CHANNEL_MISC)
        self.assertEqual(json.loads(packet.data)["event"], "login")


class CloseSocketTests(ClientTestCase):
    def test_timeout_teardown_is_polite_and_non_blocking(self):
        game, client = self.make_client()
        started = time.monotonic()
        client.close_socket()
        self.assertLess(time.monotonic() - started, 0.5)

        client.join(timeout=2.0)
        self.assertFalse(client.is_alive(), "the worker should have exited")
        self.assertTrue(client.peer.disconnected, "the server must be told")
        self.assertGreaterEqual(client.net.flushed, 1)
        self.assertFalse(client.should_poll)

    def test_closing_twice_is_harmless(self):
        game, client = self.make_client()
        client.close_socket()
        client.close_socket()
        client.join(timeout=2.0)
        self.assertFalse(client.is_alive())


class ConnectionErrorTests(unittest.TestCase):
    class NetworkStub:
        def __init__(self):
            self.closed = []

        def close_socket(self, polite=True):
            self.closed.append(polite)

    def make_game(self, network, reconnecting=False):
        game = game_module.Game.__new__(game_module.Game)
        game.network = network
        game.reconnecting = reconnecting
        game.replaced = []
        game.replace = game.replaced.append
        game.reconnect_state = "reconnect-state"
        return game

    def test_connection_error_releases_the_socket(self):
        network = self.NetworkStub()
        game = self.make_game(network)
        with mock.patch.object(game_module.menus, "main_menu") as main_menu, \
                mock.patch.object(game_module, "speak") as speak:
            game_module.Game.connection_error(game)
        self.assertIsNone(game.network, "the half-finished login must be dropped")
        self.assertEqual(network.closed, [True], "and politely, so the server can clean up")
        self.assertEqual(main_menu.call_count, 1)
        self.assertIn("Connection error", speak.call_args[0][0])

    def test_connection_error_without_a_network_is_safe(self):
        game = self.make_game(None)
        with mock.patch.object(game_module.menus, "main_menu"), \
                mock.patch.object(game_module, "speak"):
            game_module.Game.connection_error(game)
        self.assertIsNone(game.network)

    def test_reconnecting_goes_back_to_the_reconnect_state(self):
        network = self.NetworkStub()
        game = self.make_game(network, reconnecting=True)
        with mock.patch.object(game_module.menus, "main_menu") as main_menu, \
                mock.patch.object(game_module, "speak"):
            game_module.Game.connection_error(game)
        self.assertEqual(game.replaced, ["reconnect-state"])
        self.assertEqual(main_menu.call_count, 0)
        self.assertEqual(network.closed, [True])

    def test_close_network_is_idempotent(self):
        network = self.NetworkStub()
        game = self.make_game(network)
        game_module.Game._close_network(game)
        game_module.Game._close_network(game)
        self.assertEqual(network.closed, [True])


class FakeLoginClient:
    """The slice of networking.Client that game.login2/creating use."""

    def __init__(self, event_type, timed_out=False):
        self.handshakes = 0
        self.timed_out = timed_out
        self.sent = []
        self.event_type = event_type

        class Net:
            def service(_self, timeout):
                class Event:
                    type = event_type

                return Event()

        self.net = Net()

    def note_handshake(self):
        self.handshakes += 1

    def login_timed_out(self):
        return self.timed_out

    def send(self, channel, event, data=None, reliable=True):
        self.sent.append((channel, event, data))

    def loop(self):
        pass


class LoginFlowTests(unittest.TestCase):
    def make_game(self):
        game = game_module.Game.__new__(game_module.Game)
        game.replaced = []
        game.replace = game.replaced.append
        return game

    def credentials(self):
        return mock.patch.object(
            game_module.options,
            "get",
            side_effect=lambda name, default=None: {
                "username": "tester",
                "password": "secret",
            }.get(name, default),
        )

    def test_login2_marks_the_handshake_and_hands_over_to_the_loop(self):
        game = self.make_game()
        network = FakeLoginClient(enet.EVENT_TYPE_CONNECT)
        game.network = network
        with self.credentials(), mock.patch.object(game_module, "speak"):
            game_module.Game.login2(game)
        self.assertEqual(network.handshakes, 1)
        self.assertEqual(len(network.sent), 1)
        channel, event, data = network.sent[0]
        self.assertEqual(channel, consts.CHANNEL_MISC)
        self.assertEqual(event, "login")
        self.assertEqual(data["username"], "tester")
        self.assertIn("jam_notes_v1", data["capabilities"])
        self.assertEqual(game.replaced, [network.loop])

    def test_login2_waits_while_the_handshake_could_still_arrive(self):
        game = self.make_game()
        network = FakeLoginClient(enet.EVENT_TYPE_NONE, timed_out=False)
        game.network = network
        calls = []
        game.connection_error = lambda message="": calls.append(message)
        with self.credentials(), mock.patch.object(game_module, "speak"):
            game_module.Game.login2(game)
        self.assertEqual(calls, [])
        self.assertEqual(network.sent, [], "nothing is sent before the transport opens")

    def test_login2_reports_a_silent_handshake_once_the_attempts_run_out(self):
        """A handshake that never came back is a connection, not an account,
        and the last attempt says so (a retry is one attempt of several)."""
        game = self.make_game()
        network = FakeLoginClient(enet.EVENT_TYPE_NONE, timed_out=True)
        game.network = network
        game._login_name = "example.invalid"
        game._login_candidates = (("example.invalid", consts.DEFAULT_PORT),)
        game._login_try = 0
        game._close_network = lambda: None
        calls = []
        game.connection_error = lambda message="": calls.append(message)
        with self.credentials(), mock.patch.object(game_module, "speak"):
            game_module.Game.login2(game)
        self.assertEqual(len(calls), 1)
        self.assertIn("No answer from the server", calls[0])
        self.assertEqual(network.sent, [], "nothing is sent on a silent transport")

    def test_creating_marks_the_handshake_too(self):
        game = self.make_game()
        network = FakeLoginClient(enet.EVENT_TYPE_CONNECT)
        game.network = network
        with self.credentials(), mock.patch.object(game_module, "speak"):
            game_module.Game.creating(game)
        self.assertEqual(network.handshakes, 1)
        self.assertEqual(network.sent[0][1], "create")
        self.assertEqual(game.replaced, [network.loop])


class ConnectedHandlerTests(unittest.TestCase):
    """The login snapshot is what ends the watchdog window."""

    def test_connected_records_a_finished_login(self):
        from libs import event_handeler as event_handeler_module

        handler = event_handeler_module.EventHandeler.__new__(
            event_handeler_module.EventHandeler
        )
        handler.client = mock.MagicMock()
        handler.game = mock.MagicMock()
        handler.gameplay = mock.MagicMock()
        data = {"username": "tester", "available_languages": {}}
        with mock.patch.object(event_handeler_module, "speak"), \
                mock.patch("libs.crash_reporting.send_pending"):
            handler.connected(data)
        handler.client.put.assert_any_call(("logged_in", True))
        handler.client.put.assert_any_call(("connected", True))


if __name__ == "__main__":
    unittest.main()
