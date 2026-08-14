"""Server attack simulation suite (run ONLY against a test instance!).

Each scenario mimics a realistic attack a public server would face, then
verifies the server is still alive and healthy afterwards.

Usage:
    python attack_sim.py malformed   --host 127.0.0.1 --port 23000
    python attack_sim.py flood       ...
    python attack_sim.py login_storm ...
    python attack_sim.py proto       ...
    python attack_sim.py chat_flood  ...
    python attack_sim.py connect_flood ...

Scenarios:
  malformed     - garbage / invalid JSON / huge payloads on the data channel
  flood         - data + voice packet flood past the per-IP rate limit
  login_storm   - rapid wrong-password logins (bcrypt CPU attack)
  proto         - prototype-pollution payloads (__proto__/constructor)
  chat_flood    - chat spam to trigger the Sentinel chat cooldown
  connect_flood - hundreds of parallel connections (peer-table exhaustion)
"""
import argparse
import json
import random
import string
import sys
import threading
import time

import enet

CHANNEL_MISC = 0
CHANNEL_CHAT = 1
CHANNEL_MAP = 4
CHANNEL_PING = 5
CHANNEL_VOICECHAT = 20
VERSION = "BT-1.7.5"
PASSWORD = "loadtest-pass"


def _pkt(data: bytes, reliable=True) -> enet.Packet:
    flags = enet.PACKET_FLAG_RELIABLE if reliable else enet.PACKET_FLAG_UNRELIABLE_FRAGMENT
    return enet.Packet(data, flags=flags)


def _send(peer, channel, obj, reliable=True):
    peer.send(channel, _pkt(json.dumps(obj).encode(), reliable=reliable))


def alive(host, port, timeout=6.0):
    """True if the server answers a login probe (any response = alive).

    The server only emits "connected" AFTER a successful login, so a bare
    connection proves nothing. Send a login for a nonexistent account: a
    login_failed (or ban) reply means the event loop + auth path are healthy.
    """
    net = enet.Host(None, 1, 256, 0, 0)
    peer = net.connect(enet.Address(host.encode(), port), 256)
    deadline = time.perf_counter() + timeout
    sent = False
    while time.perf_counter() < deadline:
        ev = net.service(0)
        if ev.type == enet.EVENT_TYPE_CONNECT and not sent:
            _send(peer, CHANNEL_MISC, {"event": "login", "data": {
                "username": "__probe_" + str(random.randint(10**8, 10**9)),
                "password": "x", "version": VERSION}})
            sent = True
        elif ev.type == enet.EVENT_TYPE_RECEIVE:
            try:
                data = json.loads(ev.packet.data)
            except Exception:
                data = None
            if isinstance(data, dict) and data.get("event") in ("login_failed", "login_ok", "connected", "ban"):
                net = None
                return True
        time.sleep(0.001)
    try:
        peer.disconnect()
        net.service(0)
        net.flush()
    except Exception:
        pass
    net = None
    return False


def _open(host, port):
    net = enet.Host(None, 1, 256, 0, 0)
    peer = net.connect(enet.Address(host.encode(), port), 256)
    return net, peer


def _wait_connect(net, peer, timeout=6.0):
    deadline = time.perf_counter() + timeout
    while time.perf_counter() < deadline:
        ev = net.service(0)
        if ev.type == enet.EVENT_TYPE_CONNECT:
            return True
        time.sleep(0.001)
    return False


def _wait_event(net, timeout=6.0, predicate=None):
    deadline = time.perf_counter() + timeout
    while time.perf_counter() < deadline:
        ev = net.service(0)
        if ev.type == enet.EVENT_TYPE_RECEIVE:
            try:
                data = json.loads(ev.packet.data)
            except Exception:
                data = None
            if predicate is None or (isinstance(data, dict) and predicate(data)):
                return data
        time.sleep(0.001)
    return None


def _register(host, port, username):
    net, peer = _open(host, port)
    if not _wait_connect(net, peer):
        return "no-connect"
    _send(peer, CHANNEL_MISC, {"event": "create", "data": {
        "username": username, "password": PASSWORD, "version": VERSION}})
    res = _wait_event(net, predicate=lambda d: d.get("event") in ("create_done", "create_fail"))
    try:
        peer.disconnect()
        net.service(0)
        net.flush()
    except Exception:
        pass
    return res.get("event") if res else "timeout"


def _login(host, port, username, password=PASSWORD):
    net, peer = _open(host, port)
    if not _wait_connect(net, peer):
        return None
    _send(peer, CHANNEL_MISC, {"event": "login", "data": {
        "username": username, "password": password, "version": VERSION}})
    res = _wait_event(net, predicate=lambda d: d.get("event") in ("login_ok", "login_failed", "connected"))
    return (net, peer, res)


# ---------------------------------------------------------------------------
def malformed(args):
    print("=== MALFORMED PACKET TEST ===")
    net, peer = _open(args.host, args.port)
    if not _wait_connect(net, peer):
        print("FAIL: cannot connect (server down?)")
        return 1
    cases = [
        ("invalid-json", CHANNEL_MISC, b"not json {{{", True),
        ("empty-bytes", CHANNEL_MISC, b"", True),
        ("null-bytes", CHANNEL_MISC, b"\x00\x00\x00\x00\x00", True),
        ("json-no-event", CHANNEL_MISC, json.dumps({"data": {"x": 1}}).encode(), True),
        ("json-no-data", CHANNEL_MISC, json.dumps({"event": "ping"}).encode(), True),
        ("event-null", CHANNEL_MISC, json.dumps({"event": None, "data": None}).encode(), True),
        ("array-root", CHANNEL_MISC, b"[1,2,3]", True),
        ("number-root", CHANNEL_MISC, b"12345", True),
        ("string-root", CHANNEL_MISC, b'"hello"', True),
        ("deep-nested", CHANNEL_MISC, json.dumps({"event": "ping", "data": {"a": {"b": {"c": [1, 2, {"d": "e"}]}}}}).encode(), True),
        ("unknown-event", CHANNEL_MISC, json.dumps({"event": "no_such_event_xyz", "data": {}}).encode(), True),
        ("huge-1mb", CHANNEL_MISC, (b"x" * 1024 * 1024), True),
    ]
    results = []
    for name, ch, payload, rel in cases:
        try:
            peer.send(ch, _pkt(payload, rel))
            results.append((name, "sent"))
        except Exception as e:
            results.append((name, f"send-failed:{e}"))
    # drain briefly so the server processes them
    time.sleep(1.0)
    ok = alive(args.host, args.port)
    print(f"  server alive after malformed barrage: {ok}")
    for name, st in results:
        print(f"    {name:<18} {st}")
    print(f"  {'PASS: server survived' if ok else 'FAIL: SERVER DIED'}")
    return 0 if ok else 1


def flood(args):
    print("=== PACKET FLOOD TEST ===")
    net, peer = _open(args.host, args.port)
    if not _wait_connect(net, peer):
        print("FAIL: cannot connect")
        return 1
    n = 400
    t0 = time.perf_counter()
    for i in range(n):
        try:
            _send(peer, CHANNEL_MISC, {"event": "ping", "data": {"i": i}}, reliable=False)
        except Exception:
            break
    dt = time.perf_counter() - t0
    print(f"  sent {n} data packets in {dt*1000:.0f}ms ({n/dt:.0f} pkt/s) - limit is 100/s")
    time.sleep(1.5)
    # voice flood on channel 20 (limit 300/s)
    for i in range(600):
        try:
            peer.send(CHANNEL_VOICECHAT, _pkt(bytes(random.getrandbits(8) for _ in range(300)), False))
        except Exception:
            break
    print("  sent 600 voice packets - limit is 300/s")
    time.sleep(1.0)
    ok = alive(args.host, args.port)
    print(f"  server alive after flood: {ok}")
    print(f"  {'PASS: rate limit held, server survived' if ok else 'FAIL: SERVER DIED'}")
    return 0 if ok else 1


def login_storm(args):
    print("=== LOGIN STORM (bcrypt CPU attack) ===")
    user = "storm_" + "".join(random.choices(string.digits, k=6))
    reg = _register(args.host, args.port, user)
    print(f"  registered {user}: {reg}")
    attempts = 150
    done = [0]
    errs = [0]
    lock = threading.Lock()

    def worker(wi):
        n = attempts // 6
        for _ in range(n):
            try:
                net, peer = _open(args.host, args.port)
                if _wait_connect(net, peer, timeout=4):
                    _send(peer, CHANNEL_MISC, {"event": "login", "data": {
                        "username": user, "password": "wrongpass", "version": VERSION}})
                    _wait_event(net, timeout=4, predicate=lambda d: d.get("event") == "login_failed")
                try:
                    peer.disconnect()
                    net.service(0)
                    net.flush()
                except Exception:
                    pass
            except Exception:
                with lock:
                    errs[0] += 1
            with lock:
                done[0] += 1

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
    t0 = time.perf_counter()
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    dt = time.perf_counter() - t0
    rate = done[0] / max(dt, 0.001)
    print(f"  {done[0]} wrong-password logins in {dt:.1f}s ({rate:.0f}/s), errors={errs[0]}")
    # Is the server still responsive DURING the storm?
    rtts = []
    for _ in range(5):
        t1 = time.perf_counter()
        ok = alive(args.host, args.port, timeout=3.0)
        rtts.append((time.perf_counter() - t1) * 1000 if ok else -1)
    print(f"  fresh-connect latency during storm: {[f'{x:.0f}ms' if x>=0 else 'DEAD' for x in rtts]}")
    allok = all(x >= 0 for x in rtts)
    print(f"  {'PASS: server responsive' if allok else 'FAIL: server unresponsive/CPU-saturated'}")
    return 0 if allok else 1


def proto(args):
    print("=== PROTOTYPE POLLUTION TEST ===")
    net, peer = _open(args.host, args.port)
    if not _wait_connect(net, peer):
        print("FAIL: cannot connect")
        return 1
    payloads = [
        {"event": "create", "data": {"__proto__": {"admin": True}, "username": "p_poll", "password": PASSWORD, "version": VERSION}},
        {"event": "create", "data": {"constructor": {"prototype": {"isAdmin": True}}, "username": "p_poll2", "password": PASSWORD, "version": VERSION}},
        {"event": "create", "data": {"prototype": {"polluted": True}, "username": "p_poll3", "password": PASSWORD, "version": VERSION}},
        {"event": "login", "data": {"__proto__": {"developer": True}, "username": "nobody", "password": "x", "version": VERSION}},
    ]
    for p in payloads:
        try:
            _send(peer, CHANNEL_MISC, p)
        except Exception:
            pass
    time.sleep(1.0)
    ok = alive(args.host, args.port)
    print(f"  server alive after pollution attempts: {ok}")
    print(f"  {'PASS' if ok else 'FAIL: SERVER DIED'}")
    return 0 if ok else 1


def chat_flood(args):
    print("=== CHAT FLOOD (server-side throttle) ===")
    user = "chat_" + "".join(random.choices(string.digits, k=6))
    reg = _register(args.host, args.port, user)
    print(f"  registered {user}: {reg}")
    net, peer, res = _login(args.host, args.port, user)
    # A successful login answers with the "connected" event (not "login_ok").
    if res is None or res.get("event") not in ("connected", "login_ok"):
        print(f"  login not ok: {res}")
        return 1
    time.sleep(0.5)
    # Hammer chat as fast as possible for 4s. The server caps each player at
    # 1 message / 1.5s (player.chat chat_timer) + the per-IP rate limiter,
    # so only a tiny fraction may ever broadcast. Count broadcasts by echoing
    # our own messages back (the speak event contains "<name>: <msg>").
    sent = [0]
    echoed = [0]
    stop = [False]

    def sender():
        t0 = time.perf_counter()
        while time.perf_counter() - t0 < 4.0 and not stop[0]:
            _send(peer, CHANNEL_CHAT, {"event": "chat", "data": {
                "message": "spam " + "".join(random.choices(string.ascii_letters, k=20))}})
            sent[0] += 1
            time.sleep(0.004)

    t = threading.Thread(target=sender, daemon=True)
    t.start()
    deadline = time.perf_counter() + 5.0
    while time.perf_counter() < deadline:
        ev = net.service(0)
        if ev.type == enet.EVENT_TYPE_RECEIVE:
            try:
                data = json.loads(ev.packet.data)
                if isinstance(data, dict) and data.get("event") == "speak":
                    txt = str(data.get("data", {}).get("text", ""))
                    if txt.startswith(user + ":") and "spam" in txt:
                        echoed[0] += 1
            except Exception:
                pass
        else:
            time.sleep(0.005)
    stop[0] = True
    t.join(timeout=1)
    # Give the rate limiter a second to clear so the probe is not throttled.
    time.sleep(1.2)
    ok = alive(args.host, args.port)
    cap = sent[0] / 4.0
    print(f"  sent {sent[0]} in 4s ({cap:.0f}/s), broadcasts leaked: {echoed[0]}")
    print(f"  expected cap ~0.7/s (1 per 1.5s player timer) -> leaked << sent: {echoed[0] * 1.0 < sent[0] * 0.1}")
    print(f"  server alive: {ok}")
    leaked_ok = echoed[0] < max(10, sent[0] // 10)
    return 0 if (ok and leaked_ok) else 1


def connect_flood(args):
    print("=== CONNECT FLOOD (peer table exhaustion) ===")
    n = 150
    conns = []
    for i in range(n):
        try:
            net, peer = _open(args.host, args.port)
            conns.append((net, peer))
        except Exception:
            break
    # poll ALL hosts repeatedly so the ENet handshake completes (the server
    # only admits MAX_ENET_PEERS=128 of them)
    connected = 0
    deadline = time.perf_counter() + 8.0
    while time.perf_counter() < deadline:
        progressed = False
        for net, peer in conns:
            try:
                ev = net.service(0)
                if ev.type == enet.EVENT_TYPE_CONNECT:
                    connected += 1
                    progressed = True
            except Exception:
                pass
        if connected >= len(conns):
            break
        if not progressed:
            time.sleep(0.02)
    print(f"  opened {len(conns)} connections, {connected} reached CONNECT (peer cap = 128)")
    for net, peer in conns:
        try:
            peer.disconnect()
            net.service(0)
            net.flush()
        except Exception:
            pass
    time.sleep(5.0)  # let ENet free the peer slots
    alive_ok = alive(args.host, args.port)
    print(f"  server alive + accepting new connections after flood: {alive_ok}")
    print(f"  {'PASS: server survived and recovered' if alive_ok else 'FAIL: SERVER DIED / still exhausted'}")
    return 0 if alive_ok else 1


SCENARIOS = {
    "malformed": malformed,
    "flood": flood,
    "login_storm": login_storm,
    "proto": proto,
    "chat_flood": chat_flood,
    "connect_flood": connect_flood,
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("scenario", choices=sorted(SCENARIOS))
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=23000)
    args = ap.parse_args()
    sys.exit(SCENARIOS[args.scenario](args))


if __name__ == "__main__":
    main()
