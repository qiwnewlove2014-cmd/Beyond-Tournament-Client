"""E2E: all three floor categories (solid / liquid / special) render short lists.

Usage:
    python floor_categories_e2e_test.py --host 127.0.0.1 --port 13000
"""

import argparse
import json
import time

import enet

CHANNEL_MISC = 0
CHANNEL_MENUS = 6
PASSWORD = "loadtest-pass"
CLIENT_VERSION = "BT-1.7.2"


def _pkt(data: bytes) -> enet.Packet:
    return enet.Packet(data, flags=enet.PACKET_FLAG_RELIABLE)


def walk_category(net, peer, category):
    """Open floor menu, pick category, return (title, values-without-back)."""
    menus = []
    deadline = time.perf_counter() + 10
    while time.perf_counter() < deadline:
        ev = net.service(0)
        if ev.type == enet.EVENT_TYPE_RECEIVE:
            try:
                data = json.loads(ev.packet.data)
            except Exception:
                continue
            evt = data.get("event")
            if evt == "make_menu" and ev.channelID == CHANNEL_MENUS:
                d = data.get("data", {})
                title = d.get("title", "")
                opts = d.get("options", [])
                menus.append(title)
                if title == "Select Floor Category" and len(menus) == 1:
                    peer.send(CHANNEL_MENUS, _pkt(json.dumps({
                        "event": "builder_platform_type",
                        "data": {"value": {"floorCategory": category}},
                    }).encode()))
                elif "Select Floor Type" in title and len(menus) == 2:
                    vals = [o.get("value") for o in opts]
                    return title, [v for v in vals if v != "back"]
        time.sleep(0.02)
    return None, []


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=13000)
    args = ap.parse_args()

    net = enet.Host(None, 1, 256, 0, 0)
    peer = net.connect(enet.Address(args.host.encode(), args.port), 256)

    connected = False
    sent_login = False
    deadline = time.perf_counter() + 20
    while time.perf_counter() < deadline:
        ev = net.service(0)
        if ev.type == enet.EVENT_TYPE_CONNECT and not sent_login:
            peer.send(CHANNEL_MISC, _pkt(json.dumps({
                "event": "login",
                "data": {"username": "probe_builder", "password": PASSWORD,
                         "version": CLIENT_VERSION},
            }).encode()))
            sent_login = True
        elif ev.type == enet.EVENT_TYPE_RECEIVE:
            try:
                data = json.loads(ev.packet.data)
            except Exception:
                continue
            if data.get("event") == "connected":
                connected = True
                break
            # Account may still be "in the world" from a previous run; the
            # server logs it out and the client re-logs in.
            if data.get("event") in ("quit", "login_failed") and not sent_login:
                peer.send(CHANNEL_MISC, _pkt(json.dumps({
                    "event": "login",
                    "data": {"username": "probe_builder", "password": PASSWORD,
                             "version": CLIENT_VERSION},
                }).encode()))
                sent_login = True
        time.sleep(0.02)

    if not connected:
        print("RESULT: FAIL - login failed")
        return

    # open builder floor menu once
    peer.send(CHANNEL_MENUS, _pkt(json.dumps({
        "event": "builder_menu_select", "data": {"value": "platform"},
    }).encode()))
    time.sleep(0.6)

    failures = 0
    for cat in ("solid", "liquid", "special"):
        title, vals = walk_category(net, peer, cat)
        ok = False
        if cat == "solid" and title and "Solid" in title:
            ok = "concrete" in vals and "air" not in vals and "deep_water" not in vals
        elif cat == "liquid" and title and "Liquid" in title:
            ok = "deep_water" in vals and "underwater" in vals and "water" in vals and "concrete" not in vals
        elif cat == "special" and title and "Special" in title:
            ok = "air" in vals and "broken_glass" in vals and "concrete" not in vals
        if not ok:
            failures += 1
        print(f"{'PASS' if ok else 'FAIL'} - {cat}: {title} ({len(vals)} materials)")
        # go back to category menu before the next walk
        peer.send(CHANNEL_MENUS, _pkt(json.dumps({
            "event": "builder_platform_type", "data": {"value": "back"},
        }).encode()))
        time.sleep(0.6)

    print("RESULT:", "ALL PASS" if failures == 0 else f"{failures} FAILURES")
    net.flush()


if __name__ == "__main__":
    main()
