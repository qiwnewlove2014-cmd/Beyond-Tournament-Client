"""Probe: what staff flags does the server send in the "connected" payload?

Logs in a few accounts (staff + normal) against a running server and prints
the is_staff / is_builder / is_technician / can_broadcast_megaphone values so
we can see why the client blocks PA Test Mode.

Usage:
    python staff_probe.py --host 127.0.0.1 --port 13000
"""

import argparse
import json
import time

import enet

CHANNEL_MISC = 0
CLIENT_VERSION = "BT-1.7.0"
PASSWORD = "loadtest-pass"


def _pkt(data: bytes) -> enet.Packet:
    return enet.Packet(data, flags=enet.PACKET_FLAG_RELIABLE)


def login(host, port, username, register_first=False):
    net = enet.Host(None, 1, 256, 0, 0)
    peer = net.connect(enet.Address(host.encode(), port), 256)
    sent = False
    created = False
    payload = None
    deadline = time.perf_counter() + 12
    while time.perf_counter() < deadline:
        ev = net.service(0)
        if ev.type == enet.EVENT_TYPE_CONNECT and not sent:
            if register_first and not created:
                peer.send(CHANNEL_MISC, _pkt(json.dumps({
                    "event": "create",
                    "data": {"username": username, "password": PASSWORD,
                             "version": CLIENT_VERSION},
                }).encode()))
                created = True
            else:
                peer.send(CHANNEL_MISC, _pkt(json.dumps({
                    "event": "login",
                    "data": {"username": username, "password": PASSWORD,
                             "version": CLIENT_VERSION},
                }).encode()))
                sent = True
        elif ev.type == enet.EVENT_TYPE_RECEIVE and ev.channelID == CHANNEL_MISC:
            try:
                data = json.loads(ev.packet.data)
            except Exception:
                continue
            evt = data.get("event")
            if evt == "create_success":
                created = False  # now login
            elif evt == "connected":
                payload = data.get("data", {})
                break
            elif evt == "login_fail" or evt == "login_error":
                payload = {"error": data.get("data", {})}
                break
    net.flush()
    return payload


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=13000)
    args = ap.parse_args()

    for username in ("test", "memo", "icesound", "probe_plain"):
        payload = login(args.host, args.port, username,
                        register_first=(username == "probe_plain"))
        if payload is None:
            print(f"{username}: <no connected payload>")
            continue
        if "error" in payload:
            print(f"{username}: ERROR {payload['error']}")
            continue
        print(f"{username}: username={payload.get('username')!r} "
              f"is_staff={payload.get('is_staff')!r} "
              f"is_builder={payload.get('is_builder')!r} "
              f"is_technician={payload.get('is_technician')!r} "
              f"can_broadcast_megaphone={payload.get('can_broadcast_megaphone')!r}")


if __name__ == "__main__":
    main()
