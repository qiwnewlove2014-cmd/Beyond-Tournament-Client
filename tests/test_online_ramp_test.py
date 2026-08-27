"""Network-free ramp/abort/cleanup simulations; no accounts or settings touched."""

import contextlib
import heapq
import io
import json
import unittest
from unittest import mock

from tools import online_ramp_test as ramp


class Backend:
    def __init__(self, existing=2):
        self.now = 0.0
        self.events = []
        self.sequence = 0
        self.names = {}
        self.keys = []
        self.admissions = []
        self.live = set()
        self.sent = []
        self.existing = existing
        self.reject_key = None
        self.slow_at = None
        self.drop_key = None
        self.logout_acks = True
        self.fail_disconnect = False
        self.tx_packets = self.tx_bytes = self.rx_packets = self.rx_bytes = 0

    def later(self, key, event, data=None, delay=0.02):
        self.sequence += 1
        heapq.heappush(self.events, (self.now+delay, self.sequence, (key,event,data or {})))

    def connect(self):
        key = len(self.keys)
        self.keys.append(key)
        self.admissions.append(self.now)
        self.later(key, "transport_connected")
        return key

    def send(self, key, event, data):
        self.sent.append((key,event,data))
        self.tx_packets += 1
        self.tx_bytes += len(json.dumps(data))
        if event == "create":
            self.names[key] = data["username"]
            self.later(key, "create_fail" if key == self.reject_key else "create_done")
        elif event == "login":
            self.live.add(key)
            self.later(key, "connected", {"username":self.names[key],"presence_upload_token":"DO_NOT_LOG"})
            self.later(key, "parse_map", {"private_map":"DO_NOT_LOG"})
            self.later(key, "online", {"username":self.names[key]})
            if key == self.drop_key:
                self.later(key, "transport_disconnected", delay=2)
        elif event == "ping":
            delay = 1.1 if self.slow_at and len(self.live) >= self.slow_at else 0.02
            self.later(key, "ping", delay=delay)
        elif event == "who_online":
            count = self.existing + len(self.live)
            text = "You are all alone. How sad!" if count == 1 else f"{count} Online players: redacted"
            self.later(key, "speak", {"buffer":"main","text":text})
        elif event == "logout":
            self.live.discard(key)
            if self.logout_acks:
                self.later(key, "quit")

    def receive(self, timeout_ms=20):
        self.now += timeout_ms / 1000
        if self.events and self.events[0][0] <= self.now:
            message = heapq.heappop(self.events)[2]
            self.rx_packets += 1
            self.rx_bytes += len(json.dumps(message))
            return message
        return None

    def disconnect(self, key):
        if self.fail_disconnect:
            raise RuntimeError("simulated")
        self.live.discard(key)
        self.later(key, "transport_disconnected")

    def reset(self, key):
        self.live.discard(key)


class OnlineRampTests(unittest.TestCase):
    def run_ramp(self, backend, count=5, **kwargs):
        subject = ramp.Ramp(backend, count=count, clock=lambda:backend.now, run_id="test", **kwargs)
        return subject, subject.run()

    def test_fifty_staggered_connections_complete_all_stages_and_logout(self):
        backend = Backend()
        subject, result = self.run_ramp(backend, 50)
        self.assertEqual(result["reason"], "completed", result)
        self.assertEqual(result["peak_online_bots"], 50)
        self.assertEqual([stage["bots"] for stage in result["stages"]], [5,10,20,30,40,50])
        self.assertTrue(all(b-a >= 3 for a,b in zip(backend.admissions, backend.admissions[1:])))
        self.assertEqual(result["logout_acknowledged"], 50)
        self.assertEqual(result["disconnect_acknowledged"], 50)
        self.assertEqual(backend.live, set())
        self.assertTrue(all(bot.password == "" for bot in subject.bots.values()))
        self.assertEqual(set(event for _,event,_ in backend.sent), {"create","login","ping","who_online","logout"})
        for key in backend.keys:
            self.assertEqual(sum(k==key and e=="create" for k,e,_ in backend.sent), 1)
            self.assertEqual(sum(k==key and e=="login" for k,e,_ in backend.sent), 1)

    def test_rejection_stops_admission_and_drains_previous_bots(self):
        backend = Backend()
        backend.reject_key = 2
        _, result = self.run_ramp(backend, 50)
        self.assertEqual(result["reason"], "server_rejected_create_fail")
        self.assertEqual(len(backend.keys), 3)
        self.assertEqual(result["logout_acknowledged"], 2)
        self.assertFalse(backend.live)

    def test_fifty_five_complete_all_stages_and_logout_without_extra_accounts(self):
        backend = Backend(existing=5)
        subject, result = self.run_ramp(backend, 55)
        self.assertEqual(result["reason"], "completed", result)
        self.assertEqual(result["peak_online_bots"], 55)
        self.assertEqual([stage["bots"] for stage in result["stages"]], [5,10,20,30,40,50,55])
        self.assertEqual(result["stages"][-1]["visible_players"], 60)
        self.assertEqual(len(backend.keys), 55)
        self.assertTrue(all(b-a >= 3 for a,b in zip(backend.admissions, backend.admissions[1:])))
        self.assertEqual(result["logout_acknowledged"], 55)
        self.assertEqual(result["disconnect_acknowledged"], 55)
        self.assertEqual(result["logout_unconfirmed"], [])
        self.assertEqual(result["cleanup_errors"], 0)
        self.assertTrue(result["local_peers_closed"])
        self.assertFalse(backend.live)
        self.assertTrue(all(bot.password == "" for bot in subject.bots.values()))

    def test_fifty_five_retains_total_player_headroom_guard(self):
        backend = Backend(existing=6)
        _, result = self.run_ramp(backend, 55)
        self.assertEqual(result["reason"], "visible_player_headroom_limit")
        self.assertEqual(result["peak_online_bots"], 54)
        self.assertEqual(len(backend.keys), 54)
        self.assertEqual(result["logout_acknowledged"], 54)
        self.assertFalse(backend.live)

    def test_more_than_fifty_five_is_rejected_before_connection(self):
        backend = Backend()
        with self.assertRaises(ValueError):
            ramp.Ramp(backend, 60)
        self.assertEqual(backend.keys, [])

    def test_slow_server_aborts_before_later_stages(self):
        backend = Backend()
        backend.slow_at = 10
        _, result = self.run_ramp(backend, 50)
        self.assertEqual(result["reason"], "latency_guard")
        self.assertLessEqual(len(backend.keys), 10)
        self.assertFalse(backend.live)

    def test_player_headroom_stops_before_more_accounts(self):
        backend = Backend(existing=59)
        _, result = self.run_ramp(backend, 50)
        self.assertEqual(result["reason"], "visible_player_headroom_limit")
        self.assertEqual(len(backend.keys), 1)
        self.assertEqual(result["logout_acknowledged"], 1)

    def test_operator_stop_drains_every_started_peer(self):
        backend = Backend()
        _, result = self.run_ramp(backend, 50, stopped=lambda:backend.now>=20)
        self.assertEqual(result["reason"], "operator_stop")
        self.assertLess(len(backend.keys), 10)
        self.assertFalse(backend.live)
        self.assertTrue(result["local_peers_closed"])

    def test_stop_before_connect_sends_nothing(self):
        backend = Backend()
        _, result = self.run_ramp(backend, stopped=lambda:True)
        self.assertEqual(result["reason"], "operator_stop")
        self.assertEqual(backend.keys, [])
        self.assertEqual(backend.sent, [])

    def test_unexpected_disconnect_stops_entire_run(self):
        backend = Backend()
        backend.drop_key = 1
        _, result = self.run_ramp(backend, 50)
        self.assertEqual(result["reason"], "unexpected_disconnect")
        self.assertLessEqual(len(backend.keys), 2)

    def test_missing_logout_ack_is_reported_and_local_peers_closed(self):
        backend = Backend()
        backend.logout_acks = False
        _, result = self.run_ramp(backend)
        self.assertEqual(len(result["logout_unconfirmed"]), 5)
        self.assertFalse(backend.live)
        self.assertTrue(result["local_peers_closed"])

    def test_disconnect_failure_does_not_prevent_cleanup_of_other_peers(self):
        backend = Backend()
        backend.fail_disconnect = True
        _, result = self.run_ramp(backend)
        self.assertFalse(backend.live)
        self.assertGreater(result["cleanup_errors"], 0)
        self.assertFalse(result["local_peers_closed"])

    def test_reports_never_include_password_tokens_maps_or_other_player_names(self):
        backend = Backend()
        messages = []
        _, result = self.run_ramp(backend, report=messages.append)
        text = json.dumps(result) + repr(messages)
        self.assertNotIn("DO_NOT_LOG", text)
        for _,event,data in backend.sent:
            if event == "create":
                self.assertNotIn(data["password"], text)

    def test_dry_run_opens_no_socket(self):
        with mock.patch.object(ramp,"EnetBackend") as backend, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(ramp.main(["--count","55"]), 0)
        backend.assert_not_called()

    def test_unbound_host_is_not_polled_before_first_connection(self):
        backend = ramp.EnetBackend.__new__(ramp.EnetBackend)
        backend.peers = {}
        backend.net = mock.Mock()
        backend.net.service.side_effect = OSError("unbound")
        self.assertIsNone(backend.receive())
        backend.net.service.assert_not_called()

    def test_count_parser_ignores_other_buffers_and_chat(self):
        self.assertIsNone(ramp.visible_count({"buffer":"chat","text":"50 Online players: spoof"}))
        self.assertIsNone(ramp.visible_count({"buffer":"main","text":"Welcome 50 Online players: spoof"}))
        self.assertEqual(ramp.visible_count({"buffer":"main","text":"50 Online players: redacted"}), 50)
        self.assertEqual(ramp.visible_count({"buffer":"main","text":"You are all alone. How sad!"}), 1)


if __name__ == "__main__":
    unittest.main()
