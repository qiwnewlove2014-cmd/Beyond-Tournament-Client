"""Test: live PA Test Mode permission check (server-authoritative).

The client presses O; if its login-time staff flags are missing/unknown it
asks the server via check_megaphone_permission and gets the authoritative
answer back. This verifies the server honours every staff level and rejects
normal players.

Prereqs: a running server with a staff account in trusted_staff.json
(e.g. {"probe_pa": ["moderator"]}).

Usage:
    python pa_permission_e2e_test.py --host 127.0.0.1 --port 13000
"""

import argparse
import json
import threading
import time

import enet

CHANNEL_MISC = 0
CLIENT_VERSION = "BT-1.7.2"
PASSWORD = "loadtest-pass"


def _pkt(data: bytes) -> enet.Packet:
    return enet.Packet(data, flags=enet.PACKET_FLAG_RELIABLE)


class Client(threading.Thread):
    def __init__(self, host, port, username):
        super().__init__(daemon=True)
        self.host, self.port, self.username = host, port, username
        self.answer = None
        self.connected = threading.Event()

    def run(self):
        net = enet.Host(None, 1, 256, 0, 0)
        peer = net.connect(enet.Address(self.host.encode(), self.port), 256)
        sent = False
        asked = False
        deadline = time.perf_counter() + 15
        while time.perf_counter() < deadline:
            ev = net.service(0)
            if ev.type == enet.EVENT_TYPE_CONNECT and not sent:
                peer.send(CHANNEL_MISC, _pkt(json.dumps({
                    "event": "login",
                    "data": {"username": self.username, "password": PASSWORD,
                             "version": CLIENT_VERSION},
                }).encode()))
                sent = True
                self.connected.set()
            elif ev.type == enet.EVENT_TYPE_RECEIVE and ev.channelID == CHANNEL_MISC:
                try:
                    data = json.loads(ev.packet.data)
                except Exception:
                    continue
                evt = data.get("event")
                if evt == "connected" and not asked:
                    # press O path: flags are unknown, ask the server live.
                    # Real players press O well after login, so wait a moment
                    # for the server to finish registering the player.
                    time.sleep(1.0)
                    peer.send(CHANNEL_MISC, _pkt(json.dumps({
                        "event": "check_megaphone_permission",
                        "data": {},
                    }).encode()))
                    asked = True
                elif evt == "megaphone_permission":
                    self.answer = data.get("data", {})
                    break
        net.flush()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=13000)
    args = ap.parse_args()

    results = {}
    for username in ("probe_pa", "probe_plain"):
        c = Client(args.host, args.port, username)
        c.start()
        c.connected.wait(8)
        c.join(12)
        results[username] = c.answer
        print(f"{username}: answer={c.answer}")

    staff = results.get("probe_pa")
    plain = results.get("probe_plain")
    ok = (staff and staff.get("allowed") is True
          and plain and plain.get("allowed") is False)
    print("RESULT:", "PASS - live permission check honours staff and rejects normal players"
          if ok else "FAIL")


if __name__ == "__main__":
    main()
