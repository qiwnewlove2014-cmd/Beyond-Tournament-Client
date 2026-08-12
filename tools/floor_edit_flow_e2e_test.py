"""E2E: editing an existing floor platform walks 6 coordinate prompts, then the
new two-level floor category menu appears (Solid / Liquid & Water / Special).

Usage:
    python floor_edit_flow_e2e_test.py --host 127.0.0.1 --port 13000
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


def recv_until(net, pred, timeout=8.0):
    deadline = time.perf_counter() + timeout
    while time.perf_counter() < deadline:
        ev = net.service(0)
        if ev.type == enet.EVENT_TYPE_RECEIVE:
            try:
                data = json.loads(ev.packet.data)
            except Exception:
                continue
            if pred(data):
                return data
        time.sleep(0.02)
    return None


def send_menu(net, peer, event, value):
    peer.send(CHANNEL_MENUS, _pkt(json.dumps({
        "event": event,
        "data": {"value": value},
    }).encode()))


def send_raw(net, peer, event, payload):
    # edit_element_input expects data.value + data.data at the top level
    peer.send(CHANNEL_MENUS, _pkt(json.dumps({
        "event": event,
        "data": payload,
    }).encode()))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=13000)
    args = ap.parse_args()

    net = enet.Host(None, 1, 256, 0, 0)
    peer = net.connect(enet.Address(args.host.encode(), args.port), 256)

    sent_login = False
    connected = False
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
            if data.get("event") in ("quit", "login_failed"):
                # Account may still be in the world from an earlier run; the
                # server kicks it and expects a fresh login.
                time.sleep(0.5)
                sent_login = False
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

    # Open Edit Map Elements (main builder menu -> option value "editMapElements")
    send_menu(net, peer, "builder_menu_select", "editMapElements")

    menu = recv_until(net, lambda d: d.get("event") == "make_menu")
    if not menu:
        print("RESULT: FAIL - no element list menu")
        return
    opts = menu["data"]["options"]
    idx = None
    for i, o in enumerate(opts):
        label = o.get("title", "")
        if label.startswith("Platform:") or label.startswith("Platform "):
            idx = i
            break
    print(f"element list: {len(opts)} entries, floor at index {idx} ({opts[idx]['title'] if idx is not None else '?'})")
    if idx is None:
        print("RESULT: FAIL - no floor platform in element list")
        return
    send_menu(net, peer, "edit_element_select", opts[idx]["value"])

    # Options for Platform -> pick "Edit Element"
    menu = recv_until(net, lambda d: d.get("event") == "make_menu")
    if not menu:
        print("RESULT: FAIL - no element options menu")
        return
    print("options menu:", menu["data"]["title"], "->", [o.get("title") for o in menu["data"]["options"]])
    edit_opt = None
    for o in menu["data"]["options"]:
        if o.get("title", "").lower().startswith("edit"):
            edit_opt = o
            break
    if edit_opt is None:
        print("RESULT: FAIL - no Edit option")
        return
    send_menu(net, peer, "element_action_select", edit_opt["value"])

    # Walk the 6 coordinate prompts (minX -> maxX -> minY -> maxY -> minZ -> maxZ)
    coords = [0, 10, 0, 10, 0, 3]
    inp = recv_until(net, lambda d: d.get("event") == "make_input")
    if not inp:
        print("RESULT: FAIL - no first coord prompt")
        return
    print("first prompt:", inp["data"]["prompt"])
    for i, val in enumerate(coords):
        input_data = inp["data"].get("data", {})
        send_raw(net, peer, "edit_element_input", {"value": val, "data": input_data})
        nxt = recv_until(net, lambda d: d.get("event") in ("make_input", "make_menu"))
        if not nxt:
            print(f"  coord {i}: no response")
            break
        if nxt.get("event") == "make_input":
            inp = nxt
            print(f"  coord {i} -> prompt: {nxt['data']['prompt']}")
        else:
            title = nxt["data"]["title"]
            print(f"  coord {i} -> MENU: {title}")
            if title == "Select Floor Category":
                cats = [o.get("title") for o in nxt["data"]["options"]]
                print("  category options:", cats)
                ok = "Solid Floor" in cats and "Liquid & Water" in cats and "Special" in cats
                if not ok:
                    print("RESULT: FAIL - bad category menu")
                    break
                # Pick "Solid Floor" and verify the material list follows
                solid = next(o for o in nxt["data"]["options"] if o.get("title") == "Solid Floor")
                # Category pick travels as a plain menu value (no data field):
                # server's floorCategory branch reads it from data.value.
                send_menu(net, peer, "edit_element_input", solid["value"])
                m2 = recv_until(net, lambda d: d.get("event") == "make_menu")
                if m2 and "Select Floor Type" in m2["data"]["title"]:
                    vals = [o.get("value") for o in m2["data"]["options"]]
                    # Edit-flow values carry inputData ({...inputData, value: type})
                    types = [v if isinstance(v, str) else (v or {}).get("value") for v in vals]
                    ok2 = "concrete" in types and "air" not in types and "deep_water" not in types
                    print(f"  floor type menu: {m2['data']['title']} ({len(types)} materials) -> {'PASS' if ok2 else 'FAIL'}")
                    print("RESULT:", "ALL PASS" if ok2 else "FAIL - bad floor list")
                else:
                    print("RESULT: FAIL - no floor type menu after category")
            else:
                print("RESULT: FAIL - unexpected menu after coords:", title)
            break
    net.flush()


if __name__ == "__main__":
    main()
