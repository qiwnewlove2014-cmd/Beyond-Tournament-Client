"""E2E: two guitarists can plug into the PA at the same time.

Both performers talk on the megaphone (setting last_mega_voice_ts), then
send play_guitar_note events simultaneously. The listener must receive
both performers' notes with via_megaphone=True - the old code only routed
notes through the PA when the sender held the single music-bot owner slot.

Usage:
    python guitar_multi_pa_e2e_test.py --host 127.0.0.1 --port 13000
"""
import argparse
import json
import threading
import time

import enet

CHANNEL_SOUND = 1
CHANNEL_MISC = 6
CHANNEL_MAP = 7
CHANNEL_PING = 5
CHANNEL_MEGAPHONE = 30
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
                return net, peer
        time.sleep(0.02)
    net.flush()
    return net, peer


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=13000)
    args = ap.parse_args()

    # meg_gtr_a / meg_gtr_b need megaphone_broadcast in trusted_staff.json.
    netA, peerA = login(args.host, args.port, "meg_gtr_a")
    netB, peerB = login(args.host, args.port, "meg_gtr_b")
    netL, peerL = login(args.host, args.port, "meg_gtr_l")
    time.sleep(0.5)

    received = []

    def watch_l():
        deadline = time.perf_counter() + 20
        while time.perf_counter() < deadline:
            ev = netL.service(0)
            if ev.type == enet.EVENT_TYPE_RECEIVE and ev.channelID == CHANNEL_SOUND:
                try:
                    d = json.loads(ev.packet.data)
                except Exception:
                    continue
                if d.get("event") == "play_unbound" and d.get("data", {}).get("guitar_note"):
                    dd = d["data"]
                    received.append((dd.get("peer_id"), dd.get("via_megaphone"), dd.get("guitar_note")))
            time.sleep(0.005)

    th = threading.Thread(target=watch_l, daemon=True)
    th.start()
    time.sleep(0.3)

    def say_and_play(net, peer, notes):
        # Talk on the megaphone so last_mega_voice_ts is stamped on the server.
        peer.send(CHANNEL_MEGAPHONE, enet.Packet(b"\x00" * 1920,
                  flags=enet.PACKET_FLAG_UNSEQUENCED))
        net.flush()
        time.sleep(0.1)
        for n in notes:
            peer.send(CHANNEL_MAP, _pkt(json.dumps({
                "event": "play_guitar_note",
                "data": {"note": n, "velocity": 100},
            }).encode()))
            time.sleep(0.06)
        net.flush()

    notes_a = ["E2", "A2", "D2", "E2", "A2", "D2"]
    notes_b = ["A1", "E1", "B1", "A1", "E1", "B1"]
    ta = threading.Thread(target=say_and_play, args=(netA, peerA, notes_a), daemon=True)
    tb = threading.Thread(target=say_and_play, args=(netB, peerB, notes_b), daemon=True)
    ta.start()
    tb.start()
    ta.join(8)
    tb.join(8)
    time.sleep(0.5)

    # Count distinct peer_ids that arrived with via_megaphone=True
    by_peer = {}
    for pid, via, note in received:
        if via:
            by_peer.setdefault(pid, []).append(note)

    print(f"listener received {len(received)} guitar notes total")
    for pid, notes in by_peer.items():
        print(f"  {pid}: {len(notes)} notes via PA ({notes[:3]}...)")
    print(f"distinct performers heard via PA: {len(by_peer)} (expect 2)")

    ok = len(by_peer) >= 2 and all(len(v) >= 4 for v in by_peer.values())
    print("RESULT:", "PASS - both guitarists route through the PA simultaneously"
          if ok else "FAIL - one guitarist was not routed via PA")

    try:
        peerA.disconnect(); peerB.disconnect(); peerL.disconnect()
        netA.flush(); netB.flush(); netL.flush()
        time.sleep(0.3)
    except Exception:
        pass
    return 0 if ok else 1


if __name__ == "__main__":
    import sys
    sys.exit(main())
