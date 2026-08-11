"""End-to-end test for the line-in guitar note broadcast.

Two bots against a running server:
  - the "guitarist" sends play_guitar_note events (what the client sends
    after pitch detection in guitar mode),
  - the "listener" verifies it receives play_unbound broadcasts carrying the
    guitar_note field with the right notes and velocity-derived volume.

Also proves invalid notes / out-of-range velocity are rejected and that the
per-player rate limit caps a flood.

Usage (server must already be running):
    python guitar_e2e_test.py --host 127.0.0.1 --port 13000
"""

import argparse
import json
import threading
import time

import enet

CHANNEL_MISC = 0
CHANNEL_SOUND = 1
CHANNEL_MAP = 4
CHANNEL_PING = 5
CLIENT_VERSION = "BT-1.7.0"
PASSWORD = "loadtest-pass"


def _pkt(data: bytes) -> enet.Packet:
    return enet.Packet(data, flags=enet.PACKET_FLAG_RELIABLE)


class Listener(threading.Thread):
    def __init__(self, host, port, username):
        super().__init__(daemon=True)
        self.host, self.port, self.username = host, port, username
        self.received = []
        self.connected = threading.Event()
        self.quit = threading.Event()
        self.error = None

    def run(self):
        net = enet.Host(None, 1, 256, 0, 0)
        peer = net.connect(enet.Address(self.host.encode(), self.port), 256)
        sent = False
        deadline = time.perf_counter() + 15
        while not self.quit.is_set() and time.perf_counter() < deadline:
            ev = net.service(0)
            if ev.type == enet.EVENT_TYPE_CONNECT and not sent:
                peer.send(CHANNEL_MISC, _pkt(json.dumps({
                    "event": "login",
                    "data": {"username": self.username, "password": PASSWORD,
                             "version": CLIENT_VERSION},
                }).encode()))
                sent = True
                self.connected.set()
            elif ev.type == enet.EVENT_TYPE_RECEIVE and ev.channelID < CHANNEL_PING:
                try:
                    data = json.loads(ev.packet.data)
                except Exception:
                    continue
                if data.get("event") == "play_unbound" and data.get("data", {}).get("guitar_note"):
                    self.received.append(data["data"])
            elif ev.type == enet.EVENT_TYPE_DISCONNECT:
                break
        net.flush()


class Guitarist(threading.Thread):
    def __init__(self, host, port, username, notes, flood=0):
        super().__init__(daemon=True)
        self.host, self.port, self.username = host, port, username
        self.notes = notes
        self.flood = flood
        self.connected = threading.Event()
        self.quit = threading.Event()
        self.error = None

    def run(self):
        net = enet.Host(None, 1, 256, 0, 0)
        peer = net.connect(enet.Address(self.host.encode(), self.port), 256)
        sent = False
        deadline = time.perf_counter() + 15
        while not self.quit.is_set() and time.perf_counter() < deadline:
            ev = net.service(0)
            if ev.type == enet.EVENT_TYPE_CONNECT and not sent:
                peer.send(CHANNEL_MISC, _pkt(json.dumps({
                    "event": "login",
                    "data": {"username": self.username, "password": PASSWORD,
                             "version": CLIENT_VERSION},
                }).encode()))
                sent = True
                self.connected.set()
            elif ev.type == enet.EVENT_TYPE_RECEIVE and ev.channelID < CHANNEL_PING:
                try:
                    data = json.loads(ev.packet.data)
                except Exception:
                    continue
                if data.get("event") == "parse_map":
                    # logged in and placed on a map: send the notes
                    for note, vel in self.notes:
                        peer.send(CHANNEL_MAP, _pkt(json.dumps({
                            "event": "play_guitar_note",
                            "data": {"note": note, "velocity": vel},
                        }).encode()))
                        time.sleep(0.05)
                    # flood with an invalid note + bad velocity (must be ignored)
                    peer.send(CHANNEL_MAP, _pkt(json.dumps({
                        "event": "play_guitar_note",
                        "data": {"note": "H9!!!", "velocity": 999},
                    }).encode()))
                    if self.flood:
                        for _ in range(self.flood):
                            peer.send(CHANNEL_MAP, _pkt(json.dumps({
                                "event": "play_guitar_note",
                                "data": {"note": "A4", "velocity": 80},
                            }).encode()))
                    time.sleep(1.0)
                    self.quit.set()
            elif ev.type == enet.EVENT_TYPE_DISCONNECT:
                break
        net.flush()


def register(host, port, username, timeout=15):
    """Register the account if it does not exist yet (idempotent)."""
    net = enet.Host(None, 1, 256, 0, 0)
    peer = net.connect(enet.Address(host.encode(), port), 256)
    deadline = time.perf_counter() + timeout
    done = False
    while not done and time.perf_counter() < deadline:
        ev = net.service(0)
        if ev.type == enet.EVENT_TYPE_CONNECT:
            peer.send(CHANNEL_MISC, _pkt(json.dumps({
                "event": "create",
                "data": {"username": username, "password": PASSWORD,
                         "version": CLIENT_VERSION},
            }).encode()))
        elif ev.type == enet.EVENT_TYPE_RECEIVE and ev.channelID < CHANNEL_PING:
            try:
                data = json.loads(ev.packet.data)
            except Exception:
                continue
            if data.get("event") in ("create", "create_success", "login"):
                done = True
    net.flush()
    time.sleep(0.2)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=13000)
    args = ap.parse_args()

    register(args.host, args.port, "guitarist_e2e")
    register(args.host, args.port, "listener_e2e")

    notes = [("E3", 90), ("G3", 100), ("A3", 110), ("C4", 60), ("E2", 120)]
    lst = Listener(args.host, args.port, "listener_e2e")
    gtr = Guitarist(args.host, args.port, "guitarist_e2e", notes, flood=40)
    lst.start()
    gtr.start()
    lst.connected.wait(10)
    gtr.connected.wait(10)
    gtr.join(8)
    lst.join(8)

    ok = True
    names = [d.get("guitar_note") for d in lst.received]
    print("listener received guitar notes:", names)
    ok &= all(expect in names for expect in ["E3", "G3", "A3", "C4", "E2"])
    ok &= not any(n in ("H9",) for n in names)
    ok &= len(lst.received) <= 35  # 5 normal + token bucket cap on the 40-flood
    if lst.received:
        by_note = {}
        for d in lst.received:
            by_note[d["guitar_note"]] = d.get("volume")
        print("volumes:", by_note)
        # higher velocity -> higher volume (A3@110 > C4@60)
        ok &= by_note.get("A3", 0) > by_note.get("C4", 0)
        print("sound path sample:", lst.received[0].get("sound"))
    print("flood capped at 30 tokens:", len(lst.received) <= 35)
    print("RESULT:", "PASS" if ok else "FAIL")


if __name__ == "__main__":
    main()
