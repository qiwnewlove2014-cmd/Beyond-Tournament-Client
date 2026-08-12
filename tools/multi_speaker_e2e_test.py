"""E2E: multiple staff can talk through the megaphone at the same time.

Regression for the old single-owner lock: speaker B was silently dropped
whenever speaker A held the megaphone lock (megaphone_broadcast_owner).
Now voice is never locked - the client mixes with equal power (1/sqrt(N)).
This test verifies the server relays BOTH speakers' packets to a listener.

Usage:
    python multi_speaker_e2e_test.py --host 127.0.0.1 --port 13000
"""
import argparse
import json
import os
import random
import sys
import threading
import time

import enet

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# channel constants copied from libs/consts.py
CHANNEL_MISC = 6
CHANNEL_MAP = 7
CHANNEL_VOICECHAT = 20
CHANNEL_MEGAPHONE = 30


def connect(host, port):
    client = enet.Host(None, 1, 256, 0, 0)
    addr = enet.Address(host.encode(), port)
    peer = client.connect(addr, 256)
    event = client.service(50)
    if event.type == enet.EVENT_TYPE_CONNECT:
        return client, peer
    return None, None


def login(client, peer, username, password):
    payload = json.dumps({
        "event": "login",
        "data": {
            "username": username,
            "password": password,
            "version": "BT-1.7.2",
        },
    }).encode("utf-8")
    peer.send(0, enet.Packet(payload, enet.PACKET_FLAG_RELIABLE))
    client.flush()
    deadline = time.time() + 5
    while time.time() < deadline:
        ev = client.service(50)
        if ev.type == enet.EVENT_TYPE_RECEIVE:
            try:
                msg = json.loads(ev.packet.data.decode("utf-8", "replace"))
            except Exception:
                msg = {}
            if isinstance(msg, dict) and msg.get("event") in ("connected", "login_failed"):
                return msg
    return None


def send_voice(client, peer, channel, pcm):
    packet = enet.Packet(pcm, enet.PACKET_FLAG_UNSEQUENCED)
    peer.send(channel, packet)
    client.flush()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=13000)
    ap.add_argument("--packets", type=int, default=12)
    args = ap.parse_args()

    # Fixed names pre-granted megaphone_broadcast in trusted_staff.json.
    names = {
        "A": "mspk_a",
        "B": "mspk_b",
        "L": "mspk_l",
    }

    # Create accounts
    for role, uname in names.items():
        c, p = connect(args.host, args.port)
        if not c:
            print(f"[{role}] connect failed"); return 1
        payload = json.dumps({"event": "create", "data": {
            "username": uname, "password": "pass123", "gender": "male",
            "version": "BT-1.7.2",
        }}).encode("utf-8")
        p.send(0, enet.Packet(payload, enet.PACKET_FLAG_RELIABLE))
        c.flush()
        time.sleep(0.5)
        print(f"[{role}] account {uname} created")

    conns = {}
    for role, uname in names.items():
        c, p = connect(args.host, args.port)
        if not c:
            print(f"[{role}] connect failed"); return 1
        conns[role] = (c, p)
        r = login(c, p, uname, "pass123")
        print(f"[{role}] login -> {r.get('event') if r else None}")

    # Give the server time to finish constructing the players (change_map is
    # called from the Player constructor during login, so by the time we got
    # "connected" everyone is already placed on the map quadtree).
    def service_loop(role, seconds):
        c, _ = conns[role]
        end = time.time() + seconds
        while time.time() < end:
            c.service(20)

    threads = [threading.Thread(target=service_loop, args=(r, 15), daemon=True) for r in names]
    for t in threads:
        t.start()
    time.sleep(3.0)

    # Then: A and B both broadcast voice on CHANNEL_MEGAPHONE simultaneously.
    payload = b"\x00" * 1920  # silence frame - enough for routing verification
    start = time.time() + 1.0
    listener = conns["L"]
    received = {}  # sender voice_channel byte -> packet count
    lc, lp = listener

    def listener_loop():
        lc.service(50)
        end = time.time() + 8
        while time.time() < end:
            ev = lc.service(20)
            if ev.type == enet.EVENT_TYPE_RECEIVE:
                if ev.channelID == CHANNEL_MEGAPHONE:
                    data = ev.packet.data
                    if len(data) >= 2:
                        sid = data[0]
                        received[sid] = received.get(sid, 0) + 1
    lt = threading.Thread(target=listener_loop, daemon=True)
    lt.start()

    # wait until start
    while time.time() < start:
        time.sleep(0.01)

    # A and B broadcast concurrently
    for i in range(args.packets):
        send_voice(conns["A"][0], conns["A"][1], CHANNEL_MEGAPHONE, payload)
        send_voice(conns["B"][0], conns["B"][1], CHANNEL_MEGAPHONE, payload)
        time.sleep(0.05)
    print(f"[test] sent {args.packets} packets from A and B on channel {CHANNEL_MEGAPHONE}")

    lt.join(timeout=10)

    print("Listener received per sender-id (voice_channel byte):")
    total = 0
    for sid, cnt in sorted(received.items(), key=lambda kv: str(kv[0])):
        print(f"  sender_id={sid}: {cnt} packets")
        total += cnt
    print(f"  TOTAL: {total}")
    print(f"  Expected: >= 2 distinct sender ids (two people talking), "
          f"total >= {args.packets}")

    distinct = len(received)
    # Both speakers must be heard concurrently. Allow a couple of frames to
    # be lost at the tail of the stream (different senders' pacing can slip),
    # but each must contribute several packets - the old single-owner lock
    # would have dropped one speaker's ENTIRE stream.
    min_per_speaker = max(3, args.packets // 4)
    both_heard = distinct >= 2 and all(cnt >= min_per_speaker for cnt in received.values())
    if both_heard:
        print(f"PASS: both speakers relayed simultaneously (>= {min_per_speaker} pkts each)")
        return 0
    print(f"FAIL: need >=2 speakers heard with >= {min_per_speaker} pkts each "
          f"(old single-owner lock would drop one entirely)")
    return 1


if __name__ == "__main__":
    sys.exit(main())
