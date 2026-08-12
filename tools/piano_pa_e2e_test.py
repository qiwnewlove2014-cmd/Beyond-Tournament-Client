"""E2E: piano/drums reach other players + via_megaphone when the megaphone
broadcast lock is held ("Broadcast to Megaphone: ON" in the music bot).

Also verifies the lock stays alive while the performer plays (piano events
refresh the lock idle timestamp), then auto-releases after ~3s of silence.

Usage:
    python piano_pa_e2e_test.py --host 127.0.0.1 --port 13000
"""

import argparse
import json
import threading
import time

import enet

CHANNEL_MISC = 0
CHANNEL_MAP = 4
CHANNEL_PING = 5
PASSWORD = "loadtest-pass"
CLIENT_VERSION = "BT-1.7.2"


def _pkt(data: bytes) -> enet.Packet:
    return enet.Packet(data, flags=enet.PACKET_FLAG_RELIABLE)


def login(host, port, username):
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
        elif ev.type == enet.EVENT_TYPE_RECEIVE and ev.channelID < CHANNEL_PING:
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
            elif evt == "parse_map":
                got_map = True
                break
        time.sleep(0.02)
    net.flush()
    return net, peer


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=13000)
    args = ap.parse_args()

    # A must hold megaphone_broadcast in trusted_staff.json.
    netA, peerA = login(args.host, args.port, "meg_lock_a")
    netB, peerB = login(args.host, args.port, "meg_lock_b")
    time.sleep(0.3)

    received = []
    lock_states = []

    def watch_b():
        deadline = time.perf_counter() + 22
        while time.perf_counter() < deadline:
            ev = netB.service(0)
            if ev.type == enet.EVENT_TYPE_RECEIVE and ev.channelID < CHANNEL_PING:
                try:
                    d = json.loads(ev.packet.data)
                except Exception:
                    continue
                e = d.get("event")
                dd = d.get("data", {})
                if e == "play_unbound" and dd.get("piano_note"):
                    received.append(("piano", dd.get("via_megaphone"), dd.get("note", "")))
                elif e == "play_drum_hit":
                    received.append(("drum", dd.get("via_megaphone"), dd.get("pad")))
                elif e == "megaphone_lock_state":
                    lock_states.append(dd.get("owner"))
            time.sleep(0.005)

    th = threading.Thread(target=watch_b, daemon=True)
    th.start()
    time.sleep(0.3)

    # A toggles "Broadcast to Megaphone: ON" (sets the megaphone broadcast lock)
    peerA.send(CHANNEL_MISC, _pkt(json.dumps({
        "event": "megaphone_broadcast_lock",
        "data": {"locked": True},
    }).encode()))
    netA.flush()
    time.sleep(0.4)

    # A plays piano notes for ~4.5s (longer than the 3s idle timeout) - the
    # lock must stay alive because piano events refresh the lock timestamp.
    print("[test] A plays piano for 4.5s with broadcast lock ON")
    notes = ["C4", "E4", "G4", "C5", "D4", "F4", "A4", "B4"]

    def send_piano():
        start = time.perf_counter()
        while time.perf_counter() - start < 4.5:
            for n in notes:
                peerA.send(CHANNEL_MAP, _pkt(json.dumps({
                    "event": "play_piano_note",
                    "data": {"note": n, "velocity": 100},
                }).encode()))
                time.sleep(0.12)
            netA.flush()

    th_send = threading.Thread(target=send_piano, daemon=True)
    th_send.start()
    th_send.join(6)
    netA.flush()
    time.sleep(0.4)

    pianos = [r for r in received if r[0] == "piano"]
    via_ok = len(pianos) > 0 and all(v is True for _, v, _ in pianos)
    print(f"[test] B received {len(pianos)} piano notes, "
          f"all via_megaphone={via_ok} -> {'PASS' if via_ok else 'FAIL'}")

    # The lock should still be held right after the piano session (refreshed).
    lock_held_during = bool(lock_states) and lock_states[-1] == "meg_lock_a"
    print(f"[test] lock held after piano session: {lock_held_during} "
          f"(states so far: {lock_states})")

    # A stops playing entirely; the lock must auto-release after ~3s.
    print("[test] waiting 4s for idle timeout")
    time.sleep(4.0)
    lock_released = bool(lock_states) and lock_states[-1] is None
    print(f"[test] lock states: {lock_states}")
    print(f"[test] lock auto-released after idle -> {'PASS' if lock_released else 'FAIL'}")

    all_ok = via_ok and lock_held_during and lock_released
    print("RESULT:", "PASS" if all_ok else "FAIL")

    try:
        peerA.disconnect(); peerB.disconnect()
        netA.flush(); netB.flush()
        time.sleep(0.3)
    except Exception:
        pass


if __name__ == "__main__":
    main()
