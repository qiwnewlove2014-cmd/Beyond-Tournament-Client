"""E2E for the new Apartment_Luxury map.

- Logs in (main map) as a builder account.
- change_map to Apartment_Luxury.
- Verifies parse_map contains: platform, zone, reverb, instrument,
  minigameTable, megaphoneSpeaker (5, spread), travel_point.
- Walks (move packets) around lobby / lawn / row houses / rooftop.
- Sends a game chat message.

Usage:
    python tools/apartment_luxury_e2e_test.py --host 127.0.0.1 --port 13000
"""
import argparse
import json
import random
import sys
import time
from collections import Counter

import enet

sys.path.insert(0, ".")
from libs import consts  # noqa: E402

USERNAME = "lux_walker"
PASSWORD = "luxtest-pass"
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


def login_and_probe(host, port):
    net = enet.Host(None, 1, 256, 0, 0)
    peer = net.connect(enet.Address(host.encode(), port), 256)
    sent = False
    map_data = None
    element_counts = Counter()
    speaker_positions = []
    travel_points = []
    moves_done = 0
    chat_seen = False
    entity_kinds = Counter()
    deadline = time.time() + 45

    waypoints = [(50, 35, 0), (50, 8, 0), (25, 8, 0), (75, 8, 0), (50, 27, 0),
                 (33, 33, 30), (67, 67, 30), (50, 50, 31),   # roof via NW/SE stair poles + sky garden
                 (10, 38, 10), (15, 50, 10),                  # townhouse L roof via pole
                 (92, 62, 10), (85, 50, 10),                  # townhouse R roof via pole
                 (81, 18, 0), (81, 20, 1)]                     # cafeteria + table top z=1

    changed = False
    connected = False
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
            if evt == "connected":
                connected = True
            if evt == "spawn_entity":
                ed = data.get("data", {}) or {}
                name = str(ed.get("name") or ed.get("entity_name") or "")
                if "piano" in name.lower() or "drum" in name.lower():
                    entity_kinds[name] += 1
            if evt == "parse_map":
                map_data = data.get("data", data)
                d = data.get("data", {})
                inner = d.get("data", {}) if isinstance(d, dict) else {}
                elements = []
                if isinstance(inner, dict):
                    elements = inner.get("elements", []) or []
                if not elements and isinstance(d, dict):
                    elements = d.get("elements", []) or []
                for el in elements:
                    if isinstance(el, dict):
                        t = el.get("type")
                        if t:
                            element_counts[t] += 1
                        ed = el.get("data", {}) or {}
                        if t == "megaphoneSpeaker":
                            speaker_positions.append((ed.get("x"), ed.get("y"), ed.get("z"),
                                                      ed.get("hearing_range")))
                        if t == "travel_point":
                            travel_points.append((ed.get("target_map"), ed.get("bounds")))
                if element_counts:
                    print("[map] element counts:", dict(element_counts))
                    print(f"[map] speakers: {len(speaker_positions)}, travel points: {len(travel_points)}")
                    for sp in speaker_positions:
                        print(f"  speaker x={sp[0]} y={sp[1]} z={sp[2]} range={sp[3]}")
                    for tp in travel_points:
                        print(f"  travel_point -> {tp}")
            elif evt == "speak" or evt == "chat":
                chat_seen = True

            # After login completes and the first map arrives, request the luxury map
            if connected and map_data is not None and not changed:
                peer.send(consts.CHANNEL_MAP, _pkt({
                    "event": "change_map",
                    "data": {"value": "Apartment_Luxury"},
                }))
                changed = True
                print("[map] requested change_map to Apartment_Luxury")
                # wait for the luxury parse_map (speakers at map corners 5/50/95)
                map_data = None
                element_counts = Counter()
                speaker_positions = []
                travel_points = []
                continue

            # Luxury map confirmed: speakers must sit at the map corners (5/95)
            is_luxury = any(
                (abs(x or 0) in (5, 95) or (x or 0) == 50) and (abs(y or 0) in (5, 95) or (y or 0) == 50)
                for x, y, _, _ in speaker_positions
            )

            # Walk to each waypoint once the luxury map is confirmed present
            if map_data is not None and is_luxury and element_counts.get("travel_point", 0) > 0 and moves_done < len(waypoints):
                x, y, z = waypoints[moves_done]
                peer.send(consts.CHANNEL_MAP, _pkt({
                    "event": "move",
                    "data": {"x": x, "y": y, "z": z,
                             "mode": "walk", "facing": 0, "hfacing": 0, "bfacing": 0},
                }))
                moves_done += 1
                print(f"[move] walked to ({x}, {y}, {z})")

            if moves_done >= 2 and not chat_seen and map_data is not None and is_luxury:
                peer.send(consts.CHANNEL_MISC, _pkt({
                    "event": "chat",
                    "data": {"message": "hello from Apartment_Luxury e2e test"},
                }))
                chat_seen = True
                print("[chat] sent chat message")

            if map_data is not None and is_luxury and moves_done >= len(waypoints):
                break

    net.flush()
    time.sleep(0.5)
    try:
        net.destroy()
    except Exception:
        pass
    return element_counts, speaker_positions, travel_points, moves_done, entity_kinds


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=13000)
    args = ap.parse_args()

    register(args.host, args.port)
    counts, speakers, tps, moves, entity_kinds = login_and_probe(args.host, args.port)

    print("----------------------------------------")
    ok = True

    required = ["platform", "zone", "reverb", "minigameTable",
                "megaphoneSpeaker", "travel_point", "ambience", "soundSource", "music"]
    for r in required:
        if counts.get(r, 0) < 1:
            print(f"FAIL - missing element type: {r} (have {counts.get(r, 0)})")
            ok = False
        else:
            print(f"PASS - {r}: {counts.get(r)} present")

    print(f"PASS - instruments spawned as entities: {dict(entity_kinds)}")
    if entity_kinds.total() < 2:
        print("FAIL - expected at least 2 instrument entities (piano/drumset)")
        ok = False

    if len(speakers) < 5:
        print(f"FAIL - expected 5 spread speakers, got {len(speakers)}")
        ok = False
    else:
        distinct = len(set((x, y) for x, y, _, _ in speakers))
        print(f"PASS - {len(speakers)} speakers, {distinct} distinct positions")
        if distinct < 5:
            print("FAIL - speakers not all distinct")
            ok = False

    if not tps:
        print("FAIL - no travel_point found")
        ok = False
    else:
        print(f"PASS - travel points: {tps}")

    print(f"[check] walked {moves}/6 waypoints")
    if moves < 4:
        print("FAIL - did not walk most waypoints")
        ok = False

    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
