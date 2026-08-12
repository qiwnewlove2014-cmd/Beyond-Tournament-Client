"""E2E: megaphone sender exclusion + lock idle timeout.

1. Sender A streams CHANNEL_MEGAPHONE packets. The server must NOT relay them
   back to A (broadcaster monitors locally), but MUST relay them to B.
2. After A stops streaming, the map's megaphone lock auto-releases within the
   idle timeout (3s), so B can broadcast without being dropped.

Usage:
    python megaphone_lock_e2e_test.py --host 127.0.0.1 --port 13000
"""

import argparse
import json
import sys
import threading
import time

import enet
import numpy as np
from pyogg import OpusEncoder

CHANNEL_MISC = 0
CHANNEL_MEGAPHONE = 30
PASSWORD = "loadtest-pass"
CLIENT_VERSION = "BT-1.7.2"
SR = 48000


def _pkt(data: bytes) -> enet.Packet:
    return enet.Packet(data, flags=enet.PACKET_FLAG_RELIABLE)


def register_and_login(host, port, username, wait_parse_map=True):
    """Create (if needed) + login. Returns (net, peer)."""
    net = enet.Host(None, 1, 256, 0, 0)
    peer = net.connect(enet.Address(host.encode(), port), 256)
    stage = "create"
    sent = False
    deadline = time.perf_counter() + 15
    got_map = False
    while time.perf_counter() < deadline:
        ev = net.service(0)
        if ev.type == enet.EVENT_TYPE_CONNECT and not sent:
            peer.send(CHANNEL_MISC, _pkt(json.dumps({
                "event": stage,
                "data": {"username": username, "password": PASSWORD,
                         "version": CLIENT_VERSION},
            }).encode()))
            sent = True
        elif ev.type == enet.EVENT_TYPE_RECEIVE and ev.channelID < 5:
            try:
                data = json.loads(ev.packet.data)
            except Exception:
                continue
            evt = data.get("event")
            if evt in ("create_done", "create_fail") and stage == "create":
                stage = "login"
                sent = False
                peer.send(CHANNEL_MISC, _pkt(json.dumps({
                    "event": "login",
                    "data": {"username": username, "password": PASSWORD,
                             "version": CLIENT_VERSION},
                }).encode()))
            elif evt == "parse_map" and wait_parse_map:
                got_map = True
                break
        time.sleep(0.02)
    net.flush()
    return net, peer


def pluck(freq, seconds=0.8, sr=SR):
    t = np.arange(int(sr * seconds)) / sr
    x = (np.sin(2 * np.pi * freq * t)
         + 0.5 * np.sin(2 * np.pi * 2 * freq * t) * np.exp(-t * 5)
         + 0.25 * np.sin(2 * np.pi * 3 * freq * t) * np.exp(-t * 8))
    return (x * 0.5 * np.exp(-t * 2.0)).astype(np.float32)


def make_chunks(seconds=1.2):
    pcm = (pluck(164.81, 0.8) * 32767).astype(np.int16).tobytes()
    n = int(SR * seconds)
    pcm = pcm.ljust(n, b"\x00")
    return [pcm[i:i + 1920] for i in range(0, n, 1920)]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=13000)
    args = ap.parse_args()

    # --- Set up two clients: A (staff sender) and B (listener) ---
    # meg_lock_a must be granted megaphone_broadcast in trusted_staff.json.
    netA, peerA = register_and_login(args.host, args.port, "meg_lock_a")
    netB, peerB = register_and_login(args.host, args.port, "meg_lock_b")
    time.sleep(0.5)

    enc = OpusEncoder()
    enc.set_application("voip")
    enc.set_channels(1)
    enc.set_sampling_frequency(SR)
    chunks = make_chunks()

    # --- Phase 1: A streams, verify B receives but A does not (sender exclusion) ---
    print("[test] phase 1: A streams 1.2s on CHANNEL_MEGAPHONE")
    got_b = {"n": 0}
    got_a = {"n": 0}
    lock_states = []

    def listener_b():
        deadline = time.perf_counter() + 6
        while time.perf_counter() < deadline:
            ev = netB.service(0)
            if ev.type == enet.EVENT_TYPE_RECEIVE and ev.channelID == CHANNEL_MEGAPHONE:
                got_b["n"] += 1
            elif ev.type == enet.EVENT_TYPE_RECEIVE and ev.channelID < 5:
                try:
                    d = json.loads(ev.packet.data)
                except Exception:
                    continue
                if d.get("event") == "megaphone_lock_state":
                    lock_states.append(d.get("data", {}).get("owner"))
            time.sleep(0.005)

    def listener_a():
        deadline = time.perf_counter() + 6
        while time.perf_counter() < deadline:
            ev = netA.service(0)
            if ev.type == enet.EVENT_TYPE_RECEIVE and ev.channelID == CHANNEL_MEGAPHONE:
                got_a["n"] += 1
            elif ev.type == enet.EVENT_TYPE_RECEIVE and ev.channelID < 5:
                try:
                    d = json.loads(ev.packet.data)
                except Exception:
                    continue
                if d.get("event") == "megaphone_lock_state":
                    lock_states.append(d.get("data", {}).get("owner"))
            time.sleep(0.005)

    th_b = threading.Thread(target=listener_b, daemon=True)
    th_a = threading.Thread(target=listener_a, daemon=True)
    th_b.start()
    th_a.start()

    # Stream phase 1
    for chunk in chunks:
        opus = bytes(enc.encode(bytearray(chunk)))
        peerA.send(CHANNEL_MEGAPHONE, enet.Packet(
            opus, flags=enet.PACKET_FLAG_UNRELIABLE_FRAGMENT))
        time.sleep(0.02)
    time.sleep(0.6)

    excl_ok = got_b["n"] > 10 and got_a["n"] == 0
    print(f"[test] B received {got_b['n']} packets, A received {got_a['n']} "
          f"-> sender exclusion {'PASS' if excl_ok else 'FAIL'}")

    # --- Phase 2: A stops; after ~3s the lock auto-releases ---
    # Verify via megaphone_lock_state broadcast: owner A -> None within ~4s.
    print("[test] phase 2: waiting for lock idle timeout (3s)")
    time.sleep(4.5)
    lock_released = (lock_states[-1] is None) if lock_states else False
    print(f"[test] lock state transitions: {lock_states}")
    print(f"[test] lock auto-released after idle -> {'PASS' if lock_released else 'FAIL'}")

    # --- Phase 3: B broadcasts now; the lock must not drop B's packets ---
    print("[test] phase 3: B streams after lock release")
    got_b2 = {"n": 0}
    def listener_b2():
        deadline = time.perf_counter() + 4
        while time.perf_counter() < deadline:
            ev = netB.service(0)
            if ev.type == enet.EVENT_TYPE_RECEIVE and ev.channelID == CHANNEL_MEGAPHONE:
                got_b2["n"] += 1
            time.sleep(0.005)
    th_b2 = threading.Thread(target=listener_b2, daemon=True)
    th_b2.start()
    for chunk in chunks[:20]:
        opus = bytes(enc.encode(bytearray(chunk)))
        peerB.send(CHANNEL_MEGAPHONE, enet.Packet(
            opus, flags=enet.PACKET_FLAG_UNRELIABLE_FRAGMENT))
        time.sleep(0.02)
    time.sleep(0.5)
    # B gets its own packets back through the PA? No - sender exclusion means B
    # does NOT receive its own broadcast; A should receive B's broadcast.
    got_a2 = {"n": 0}
    print(f"[test] B self-received after exclusion: {got_b2['n']} (expect 0) "
          f"-> {'PASS' if got_b2['n'] == 0 else 'INFO'}")
    # Check A receives B's stream
    def listener_a2():
        deadline = time.perf_counter() + 4
        while time.perf_counter() < deadline:
            ev = netA.service(0)
            if ev.type == enet.EVENT_TYPE_RECEIVE and ev.channelID == CHANNEL_MEGAPHONE:
                got_a2["n"] += 1
            time.sleep(0.005)
    th_a2 = threading.Thread(target=listener_a2, daemon=True)
    th_a2.start()
    time.sleep(0.5)
    # A should receive B's broadcast (B is not A, so not excluded)
    print(f"[test] A received B's broadcast: {got_a2['n']} packets -> "
          f"{'PASS' if got_a2['n'] > 0 else 'FAIL'}")

    all_ok = excl_ok and lock_released and got_a2["n"] > 0
    print("RESULT:", "PASS" if all_ok else "FAIL")

    # Clean disconnects so the server does not keep stale "already logged in"
    # sessions that block a re-run.
    try:
        peerA.disconnect(); peerB.disconnect()
        netA.flush(); netB.flush()
        time.sleep(0.3)
    except Exception:
        pass


if __name__ == "__main__":
    main()
