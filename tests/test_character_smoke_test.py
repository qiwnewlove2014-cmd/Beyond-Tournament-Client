"""Offline tests: this module never imports ENet or connects to a server."""

import contextlib
import copy
import io
import json
import unittest
from unittest import mock

from tools import character_smoke_test as smoke


NAME = "BT-smoke-unittest"
PASSWORD = "only-a-fake-test-password"
BOUNDS = dict(minx=0, maxx=10, miny=0, maxy=10, minz=0, maxz=10)
MAP = dict(x=5, y=5, z=0, data=dict(**BOUNDS, elements=[
    dict(type="platform", data=dict(**BOUNDS, type="concrete", id="floor")),
]))


class FakeTransport:
    def __init__(self):
        self.now = 0.0
        self.messages = [("transport_connected", {})]
        self.sent = []
        self.closed = False
        self.reject = None
        self.disconnect_during_hold = False
        self.ack_logout = True
        self.mentions = False
        self.move_times = []

    def send(self, event, data):
        self.sent.append((event, data))
        if event == "create":
            self.messages.append((self.reject or "create_done", {}))
        elif event == "login":
            self.messages.extend([
                ("parse_map", copy.deepcopy(MAP)),
                ("connected", {"username": NAME, "presence_upload_token": "SECRET"}),
                ("online", {"username": NAME}),
            ])
        elif event == "ping":
            if self.disconnect_during_hold:
                raise smoke.ProbeError("transport_disconnected")
            self.messages.append(("ping", {}))
            if self.mentions:
                self.messages.append(("speak", {"buffer": "chat", "text": "Player: smoke สวัสดี /ban everyone"}))
        elif event == "chat":
            self.messages.append(("speak", {"buffer": "chat", "text": NAME + ": " + data["message"]}))
        elif event == "move":
            self.move_times.append(self.now)
        elif event == "logout" and self.ack_logout:
            self.messages.append(("quit", {}))

    def receive(self):
        self.now += 0.1
        return self.messages.pop(0) if self.messages else None

    def close(self):
        self.closed = True
        return True


class CharacterSmokeTests(unittest.TestCase):
    def run_fake(self, transport, **kwargs):
        return smoke.run_probe(transport, NAME, PASSWORD, clock=lambda: transport.now, **kwargs)

    def test_one_account_normal_flow_bounded_pings_and_clean_logout(self):
        transport = FakeTransport()
        result = self.run_fake(transport, hold=60)
        self.assertTrue(result.passed, result)
        events = [event for event, _ in transport.sent]
        self.assertEqual(events.count("create"), 1)
        self.assertEqual(events.count("login"), 1)
        self.assertEqual(events.count("logout"), 1)
        self.assertLessEqual(events.count("ping"), 12)
        self.assertEqual(set(events), {"create", "login", "ping", "logout"})
        self.assertEqual(transport.sent[0][1]["version"], smoke.consts.CLIENT_VERSION)
        self.assertTrue(transport.closed)

    def test_create_rejection_stops_without_retry_or_login(self):
        for event in ("create_fail", "login_failed", "ban"):
            transport = FakeTransport()
            transport.reject = event
            result = self.run_fake(transport)
            self.assertFalse(result.passed)
            self.assertFalse(result.created)
            self.assertEqual([x[0] for x in transport.sent], ["create"])
            self.assertTrue(transport.closed)

    def test_connect_timeout_sends_nothing(self):
        transport = FakeTransport()
        transport.messages.clear()
        result = self.run_fake(transport)
        self.assertEqual(result.error, "timeout_transport_connected")
        self.assertEqual(transport.sent, [])
        self.assertTrue(transport.closed)

    def test_hold_failure_still_attempts_logout(self):
        transport = FakeTransport()
        transport.disconnect_during_hold = True
        result = self.run_fake(transport)
        self.assertFalse(result.passed)
        self.assertEqual(transport.sent[-1][0], "logout")
        self.assertTrue(transport.closed)

    def test_missing_logout_ack_is_not_success(self):
        transport = FakeTransport()
        transport.ack_logout = False
        result = self.run_fake(transport, hold=1)
        self.assertEqual(result.error, "logout_not_acknowledged")
        self.assertFalse(result.passed)
        self.assertTrue(transport.closed)

    def test_invalid_hold_never_creates_account(self):
        for hold in (0, 121):
            transport = FakeTransport()
            result = self.run_fake(transport, hold=hold)
            self.assertFalse(result.passed)
            self.assertEqual(transport.sent, [])
            self.assertTrue(transport.closed)

    def test_no_credentials_or_server_token_in_report(self):
        transport = FakeTransport()
        reports = []
        result = self.run_fake(transport, hold=1, report=reports.append)
        output = json.dumps(smoke.asdict(result)) + repr(reports)
        self.assertNotIn(PASSWORD, output)
        self.assertNotIn("SECRET", output)

    def test_no_run_flag_never_constructs_transport(self):
        with mock.patch.object(smoke, "EnetTransport") as transport, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(smoke.main([]), 0)
        transport.assert_not_called()

    def test_interaction_has_four_slow_steps_and_at_most_three_messages(self):
        transport = FakeTransport()
        transport.mentions = True
        result = self.run_fake(transport, hold=60, interact=True)
        self.assertTrue(result.passed, result)
        self.assertEqual(result.moves_sent, 4)
        self.assertEqual(result.chat_sent, 3)
        self.assertEqual(result.chat_confirmed, 3)
        self.assertEqual(result.replies_sent, 2)
        self.assertTrue(all(b-a >= 4 for a, b in zip(transport.move_times, transport.move_times[1:])))
        moves = [data for event, data in transport.sent if event == "move"]
        self.assertEqual([(data["x"], data["y"], data["z"]) for data in moves], [(6,5,0),(5,5,0),(6,5,0),(5,5,0)])
        chats = [data["message"] for event, data in transport.sent if event == "chat"]
        self.assertTrue(all(not message.startswith("/") for message in chats))
        self.assertTrue(all("/ban" not in message for message in chats))

    def test_reply_only_to_mentions_in_global_chat_never_echoes(self):
        for data in (
            {"buffer": "chat", "text": NAME + ": smoke hello"},
            {"buffer": "chat", "text": "BT-smoke-other: smoke hello"},
            {"buffer": "staff", "text": "Player: smoke hello"},
            {"buffer": "map chat", "text": "Player: smoke hello"},
            {"buffer": "chat", "text": "Player: hello everyone"},
        ):
            self.assertIsNone(smoke.reply_for_chat(data, NAME))
        self.assertIsNotNone(smoke.reply_for_chat({"buffer": "chat", "text": "Player: smoke hello"}, NAME))

    def test_walk_refuses_unknown_floor_water_walls_or_invalid_coordinates(self):
        self.assertEqual(smoke.safe_walk_path({}), [])
        for tile in ("", "air", "deep_water", "wallconcrete", "lava"):
            payload = copy.deepcopy(MAP)
            payload["data"]["elements"][0]["data"]["type"] = tile
            self.assertEqual(smoke.safe_walk_path(payload), [])
        payload = copy.deepcopy(MAP)
        payload["x"] = float("nan")
        self.assertEqual(smoke.safe_walk_path(payload), [])

    def test_walk_avoids_door_and_map_boundary(self):
        payload = copy.deepcopy(MAP)
        payload["x"] = 10
        payload["data"]["elements"].append(dict(type="door", data=dict(
            minx=10,maxx=10,miny=6,maxy=6,minz=0,maxz=0)))
        self.assertEqual(smoke.safe_walk_path(payload)[0], (9,5,0))

    def test_map_change_stops_movement(self):
        transport = FakeTransport()
        original_receive = transport.receive
        changed = False
        def receive():
            nonlocal changed
            if transport.now >= 6 and not changed:
                changed = True
                return "update_map", {}
            return original_receive()
        transport.receive = receive
        result = self.run_fake(transport, hold=20, interact=True)
        self.assertEqual(result.moves_sent, 1)
        self.assertEqual(result.movement_note, "stopped_after_world_change")


if __name__ == "__main__":
    unittest.main()
