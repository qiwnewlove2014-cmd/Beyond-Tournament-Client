"""E2E: wall categories render short lists in the builder wall picker.

The wall picker asks the client to scan its local ``walls`` folder and the
server filters the results per category. This probe answers the
``request_scandir`` packet with the real client walls folder, so the server
filters exactly like it does for a real client.

Usage:
    python wall_categories_e2e_test.py --host 127.0.0.1 --port 13000
"""

import argparse
import json
import os
import time

import enet

CHANNEL_MISC = 0
CHANNEL_MENUS = 6
PASSWORD = "loadtest-pass"
CLIENT_VERSION = "BT-1.7.2"

WALLS_DIR = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "data", "walls"))


def _pkt(data: bytes) -> enet.Packet:
    return enet.Packet(data, flags=enet.PACKET_FLAG_RELIABLE)


def wall_items():
    """Simulate the client's scandir reply for the walls folder."""
    items = []
    for name in sorted(os.listdir(WALLS_DIR)):
        full = os.path.join(WALLS_DIR, name)
        items.append({
            "name": name,
            "is_dir": os.path.isdir(full),
            "is_file": os.path.isfile(full),
        })
    return items


def walk_category(net, peer, cat_key, expected_title, expected_has, expected_not):
    """Open wall picker, pick a category, answer the scan, return menu values."""
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
            if evt == "request_scandir" and ev.channelID == CHANNEL_MISC:
                d = data.get("data", {})
                if d.get("path") == "walls" and d.get("category") == "walls":
                    peer.send(CHANNEL_MISC, _pkt(json.dumps({
                        "event": "scandir_response",
                        "data": {
                            "success": True,
                            "error": "",
                            "items": wall_items(),
                            "category": "walls",
                            "path": "walls",
                            "wallCategory": d.get("wallCategory", ""),
                        },
                    }).encode()))
                continue
            if evt == "make_menu" and ev.channelID == CHANNEL_MENUS:
                d = data.get("data", {})
                title = d.get("title", "")
                opts = d.get("options", [])
                menus.append(title)
                if title == "Select Wall Category" and len(menus) == 1:
                    peer.send(CHANNEL_MENUS, _pkt(json.dumps({
                        "event": "builder_platform_type",
                        "data": {"value": {"wallCategory": cat_key}},
                    }).encode()))
                elif expected_title in title and len(menus) == 2:
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

    # Open the builder wall menu (shows the category menu first)
    peer.send(CHANNEL_MENUS, _pkt(json.dumps({
        "event": "builder_menu_select", "data": {"value": "wall"},
    }).encode()))
    time.sleep(0.6)

    cases = [
        # key, title fragment, must contain, must NOT contain
        ("wood", "Wood & Timber", "wallwood", "wallmetal"),
        ("metal", "Metal & Industrial", "wallmetal", "wallwood"),
        ("glass", "Glass & Windows", "wallglass", "wallwood"),
        ("stone", "Stone, Brick & Masonry", "wallbrick", "wallwood"),
        ("nature", "Dirt & Nature", "wallgrass", "wallmetal"),
    ]

    failures = 0
    for cat_key, frag, has, not_has in cases:
        title, vals = walk_category(net, peer, cat_key, frag, has, not_has)
        ok = bool(title) and has in vals and not_has not in vals and 0 < len(vals) <= 40
        if not ok:
            failures += 1
        print(f"{'PASS' if ok else 'FAIL'} - {cat_key}: {title} ({len(vals)} walls)")
        # go back to the category menu before the next walk
        peer.send(CHANNEL_MENUS, _pkt(json.dumps({
            "event": "builder_platform_type", "data": {"value": "back"},
        }).encode()))
        time.sleep(0.6)

    print("RESULT:", "ALL PASS" if failures == 0 else f"{failures} FAILURES")
    net.flush()


if __name__ == "__main__":
    main()
