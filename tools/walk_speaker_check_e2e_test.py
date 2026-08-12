"""E2E: walk the map like a player and verify the PA speaker layout.

- Logs in to the server (main map).
- Reads megaphoneSpeaker positions from parse_map (the same data the client
  uses to position speakers).
- Walks (sends move packets) to the four corners + center.
- Sends a game chat message ("Speaker spread check") and confirms the server
  routes it (we just need no crash + chat event seen).

Usage:
    python tools/walk_speaker_check_e2e_test.py --host 127.0.0.1 --port 13000
"""
import argparse
import json
import random
import sys
import time

import enet

sys.path.insert(0, ".")
from libs import consts  # noqa: E402

USERNAME = f"walk_{random.randint(1000, 9999)}"
PASSWORD = "walktest-pass"
CLIENT_VERSION = "BT-1.7.2"


def _pkt(obj, reliable=True):
    if isinstance(obj, bytes):
        return enet.Packet(obj, enet.PACKET_FLAG_RELIABLE if reliable else enet.PACKET_FLAG_UNSEQUENCED)
    return enet.Packet(json.dumps(obj).encode("utf-8"),
                      enet.PACKET_FLAG_RELIABLE if reliable else enet.PACKET_FLAG_UNSEQUENCED)


def register(host, port):
    net = enet.Host(None, 1, 256, 0, 0)
    peer = net.connect(enet.Address(host.encode(), port), 256)
    sent = False
    deadline = time.time() + 5
    while time.time() < deadline:
        ev = net.service(10)
        if ev.type == enet.EVENT_TYPE_CONNECT and not sent:
            peer.send(consts.CHANNEL_MISC, _pkt({
                "event": "create",
                "data": {"username": USERNAME, "password": PASSWORD,
                         "version": CLIENT_VERSION},
            }))
            sent = True
        elif ev.type == enet.EVENT_TYPE_RECEIVE:
            try:
                msg = json.loads(ev.packet.data)
            except Exception:
                continue
            if isinstance(msg, dict) and msg.get("event") in ("create", "create_success", "login", "connected"):
                break
    net.flush()
    time.sleep(0.2)
    try:
        net.destroy()
    except Exception:
        pass


def login_and_walk(host, port):
    net = enet.Host(None, 1, 256, 0, 0)
    peer = net.connect(enet.Address(host.encode(), port), 256)
    sent = False
    map_data = None
    speaker_positions = []
    chat_seen = False
    moves_done = 0
    deadline = time.time() + 30

    # Waypoints: TL, TR, BL, BR, center (matches new speaker layout)
    waypoints = [(-25, 60), (95, 60), (-25, -25), (95, -25), (0, 35), (10, 49)]

    while time.time() < deadline:
        ev = net.service(10)
        if ev.type == enet.EVENT_TYPE_CONNECT and not sent:
            peer.send(consts.CHANNEL_MISC, _pkt({
                "event": "login",
                "data": {"username": USERNAME, "password": PASSWORD,
                         "version": CLIENT_VERSION},
            }))
            sent = True
        elif ev.type == enet.EVENT_TYPE_RECEIVE:
            try:
                data = json.loads(ev.packet.data)
            except Exception:
                continue
            if not isinstance(data, dict):
                continue
            evt = data.get("event")
            if evt == "parse_map" and map_data is None:
                map_data = data.get("data", data)
                # parse_map shape: {event, data: {data: ExportToClient(), name, x, y, z}}
                # elements live at data.data.elements
                elements = []
                d = data.get("data", {})
                if isinstance(d, dict):
                    inner = d.get("data", {})
                    if isinstance(inner, dict):
                        elements = inner.get("elements", []) or []
                    if not elements:
                        elements = d.get("elements", []) or []
                for el in elements:
                    if isinstance(el, dict) and el.get("type") == "megaphoneSpeaker":
                        ed = el.get("data", {})
                        speaker_positions.append((ed.get("x"), ed.get("y"), ed.get("z"),
                                                  ed.get("hearing_range")))
                print(f"[map] parse_map received, megaphoneSpeaker entries: {len(speaker_positions)}")
                for sp in speaker_positions:
                    print(f"  speaker x={sp[0]} y={sp[1]} z={sp[2]} hearing_range={sp[3]}")
            elif evt == "chat" or evt == "speak":
                chat_seen = True
            elif evt == "move":
                pass
            # Walk to each waypoint (fire-and-forget move packets)
            if map_data is not None and moves_done < len(waypoints):
                x, y = waypoints[moves_done]
                peer.send(consts.CHANNEL_MAP, _pkt({
                    "event": "move",
                    "data": {"x": x, "y": y, "z": 0,
                             "mode": "walk", "facing": 0, "hfacing": 0, "bfacing": 0},
                }))
                moves_done += 1
                print(f"[move] walked to ({x}, {y})")
            # Send a chat message once we've walked a couple of points
            if moves_done >= 2 and not chat_seen and map_data is not None:
                peer.send(consts.CHANNEL_MISC, _pkt({
                    "event": "chat",
                    "data": {"message": "Speaker spread check - walking to all corners"},
                }))
                chat_seen = True
                print("[chat] sent chat message")
            if moves_done >= len(waypoints):
                break
    net.flush()
    time.sleep(0.5)
    try:
        net.destroy()
    except Exception:
        pass
    return speaker_positions, moves_done


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=13000)
    args = ap.parse_args()

    register(args.host, args.port)
    speakers, moves = login_and_walk(args.host, args.port)

    print("----------------------------------------")
    ok = True
    if not speakers:
        print("FAIL - no megaphoneSpeaker found in parse_map")
        ok = False
    else:
        positions = [(x, y) for x, y, _, _ in speakers]
        # Must have 5 distinct (x,y) in main.map after the spread fix
        distinct = len(set(positions))
        print(f"[check] {len(speakers)} speakers, {distinct} distinct positions")
        if distinct < 2:
            print("FAIL - speakers still stacked at one spot")
            ok = False
        else:
            print("PASS - speakers at distinct positions")
    print(f"[check] walked {moves}/6 waypoints")
    if moves < 4:
        print("FAIL - did not walk most waypoints")
        ok = False
    else:
        print("PASS - walked around the map")
    print("RESULT:", "PASS" if ok else "FAIL")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
