"""Client-cheat simulation suite (run ONLY against a test instance!).

Each scenario sends the exact packets a modified game client would send and
reports what the server accepts vs rejects:

  speed_hack  - 'move' events far faster than the walk interval (270ms)
  teleport    - a single 'move' jumping far away (and outside the map bounds)
  give_self   - '/give <self> ...' chat as a normal player (staff command)
  change_map  - 'change_map' event as a non-builder

The server re-broadcasts every accepted 'move' on CHANNEL_MOVEMENT (2) to
everyone else on the map (main.map has no move_radius), so a second observer
bot confirms what the server really accepted — no guessing from echoes.

Implementation notes (learned the hard way):
- Both bots MUST be serviced from the same thread: pymeternet delivers events
  only as the return value of host.service(), so discarding one host's return
  loses its packets.
- After every send you must service the sender's host or the packet never
  leaves the queue. pump_both() services both hosts every ~3ms so sends flush
  immediately and broadcasts are captured with no timing holes.
- The observer must be fully in the map (parse_map received) before moves are
  sent, otherwise broadcasts are missed.

Usage:
    python cheat_sim.py speed_hack --port 23000
    python cheat_sim.py teleport   --port 23000
    python cheat_sim.py give_self  --port 23000
    python cheat_sim.py change_map --port 23000
"""
import argparse
import json
import random
import string
import sys
import time

import enet

CHANNEL_MISC = 0
CHANNEL_CHAT = 1
CHANNEL_MOVEMENT = 2
CHANNEL_MAP = 4
CHANNEL_PING = 5
VERSION = "BT-1.7.5"
PASSWORD = "loadtest-pass"


def _pkt(data: bytes, reliable=True) -> enet.Packet:
    flags = enet.PACKET_FLAG_RELIABLE if reliable else enet.PACKET_FLAG_UNRELIABLE_FRAGMENT
    return enet.Packet(data, flags=flags)


def _send(peer, channel, obj, reliable=True):
    peer.send(channel, _pkt(json.dumps(obj).encode(), reliable=reliable))


def _register(host, port, username):
    net = enet.Host(None, 1, 256, 0, 0)
    peer = net.connect(enet.Address(host.encode(), port), 256)
    deadline = time.perf_counter() + 6
    while time.perf_counter() < deadline:
        ev = net.service(0)
        if ev.type == enet.EVENT_TYPE_CONNECT:
            break
        time.sleep(0.001)
    _send(peer, CHANNEL_MISC, {"event": "create", "data": {
        "username": username, "password": PASSWORD, "version": VERSION}})
    res = None
    deadline = time.perf_counter() + 6
    while time.perf_counter() < deadline:
        ev = net.service(0)
        if ev.type == enet.EVENT_TYPE_RECEIVE:
            try:
                res = json.loads(ev.packet.data)
            except Exception:
                pass
            if res and res.get("event") in ("create_done", "create_fail"):
                break
        time.sleep(0.001)
    peer.disconnect()
    return res


def _login(host, port, username, password=PASSWORD):
    net = enet.Host(None, 1, 256, 0, 0)
    peer = net.connect(enet.Address(host.encode(), port), 256)
    deadline = time.perf_counter() + 6
    while time.perf_counter() < deadline:
        ev = net.service(0)
        if ev.type == enet.EVENT_TYPE_CONNECT:
            break
        time.sleep(0.001)
    _send(peer, CHANNEL_MISC, {"event": "login", "data": {
        "username": username, "password": password, "version": VERSION}})
    res = None
    deadline = time.perf_counter() + 6
    while time.perf_counter() < deadline:
        ev = net.service(0)
        if ev.type == enet.EVENT_TYPE_RECEIVE:
            try:
                res = json.loads(ev.packet.data)
            except Exception:
                pass
            if res and res.get("event") in ("connected", "login_failed", "ban"):
                break
        time.sleep(0.001)
    return net, peer, res


def _spawn(host, port, username):
    """Register (if needed) and log in. Returns (net, peer) or (None, None)."""
    _register(host, port, username)
    net, peer, res = _login(host, port, username)
    if not net or not res or res.get("event") != "connected":
        print(f"  login failed for {username}: {res}")
        return None, None
    return net, peer


def _pump(net, dur, on_event=None):
    deadline = time.perf_counter() + dur
    while time.perf_counter() < deadline:
        ev = net.service(0)
        if ev.type == enet.EVENT_TYPE_RECEIVE:
            try:
                d = json.loads(ev.packet.data)
            except Exception:
                d = None
            if on_event and d:
                on_event(d, ev.channelID)
        elif ev.type == enet.EVENT_TYPE_DISCONNECT:
            print("  !! DISCONNECTED by server")
        else:
            time.sleep(0.005)


def _warm_up_observer(net, peer, obs_net, obs_peer, observer_name, timeout=4.0):
    """Prove the observer is fully in the map before the real test.

    The observer sends a small move; the cheater must see the broadcast within
    `timeout` seconds. This exercises the exact send -> server -> broadcast ->
    receive path we rely on for the verdicts.
    """
    cheater_saw = []

    def on_ev(d, chan):
        if (chan == CHANNEL_MOVEMENT and isinstance(d, dict)
                and d.get("event") == "move"):
            dd = d.get("data")
            if isinstance(dd, dict) and dd.get("name") == observer_name:
                cheater_saw.append(dd.get("x"))

    _send(obs_peer, CHANNEL_MAP, {"event": "move", "data": {
        "x": 35, "y": 0, "z": 35, "play_sound": True, "mode": "walk",
        "angle": 0}})
    deadline = time.perf_counter() + timeout
    while time.perf_counter() < deadline and not cheater_saw:
        _pump_both(net, obs_net, 0.1, on_ev)
    return bool(cheater_saw)


def _pump_both(net_a, net_b, dur, on_ev=None):
    """Service both hosts from THIS thread every ~3ms.

    pymeternet delivers events only as host.service()'s return value, so both
    return values MUST be read — a discarded return loses the packet.
    """
    deadline = time.perf_counter() + dur
    while time.perf_counter() < deadline:
        for net in (net_a, net_b):
            ev = net.service(0)
            if ev.type == enet.EVENT_TYPE_RECEIVE:
                try:
                    d = json.loads(ev.packet.data)
                except Exception:
                    d = None
                if on_ev and d:
                    on_ev(d, ev.channelID)
        time.sleep(0.003)


def speed_hack(args):
    print("=== SPEED HACK (move events at ~30ms, walk is 270ms) ===")
    user = "spd_" + "".join(random.choices(string.digits, k=5))
    net, peer = _spawn(args.host, args.port, user)
    if net is None:
        return 1
    obs_name = "obs_" + "".join(random.choices(string.digits, k=5))
    obs_net, obs_peer = _spawn(args.host, args.port, obs_name)
    if obs_net is None:
        return 1

    # Let both bots settle, then prove the observer is in the map.
    _pump(net, 0.7, lambda d, c: None)
    _pump(obs_net, 0.7, lambda d, c: None)
    if not _warm_up_observer(net, peer, obs_net, obs_peer, obs_name):
        print("  observer never joined the map (no warm-up move seen)")
        return 1

    seen = []

    def on_ev(d, chan):
        if (chan == CHANNEL_MOVEMENT and isinstance(d, dict)
                and d.get("event") == "move"):
            dd = d.get("data")
            if isinstance(dd, dict) and dd.get("name") == user:
                seen.append((time.perf_counter(), dd.get("x")))

    x, y, z = 35, 0, 35
    t0 = time.perf_counter()
    for _ in range(30):
        x += 3
        _send(peer, CHANNEL_MAP, {"event": "move", "data": {
            "x": x, "y": y, "z": z, "play_sound": True, "mode": "walk",
            "angle": 0}})
        _pump_both(net, obs_net, 0.03, on_ev)  # ~30ms between moves (legit walk is 270ms)
    dt = time.perf_counter() - t0
    speed = 30 * 3 / max(dt, 0.001)
    print(f"  cheater sent 30 x +3 in {dt*1000:.0f}ms ({speed:.0f} units/s vs legit ~11)")
    _pump_both(net, obs_net, 1.0, on_ev)  # catch any stragglers

    if not seen:
        print("  observer saw NO moves -> server dropped the flood entirely")
        return 0
    stamps = [s[0] for s in seen]
    gaps = [round((b - a) * 1000) for a, b in zip(stamps, stamps[1:])]
    gap_avg = sum(gaps) / len(gaps) if gaps else 0
    max_x = max(s[1] for s in seen)
    print(f"  observer saw {len(seen)}/30 moves, avg gap {gap_avg:.0f}ms, last x={max_x}")
    if len(seen) >= 25 and gap_avg < 60:
        print("  result: ACCEPTED (server re-broadcast the 30ms flood -> "
              "no speed protection active)")
        return 1
    print("  result: REJECTED/throttled (server did not relay the flood rate)")
    return 0


def teleport(args):
    print("=== TELEPORT (single move jumping far away) ===")
    user = "tel_" + "".join(random.choices(string.digits, k=5))
    net, peer = _spawn(args.host, args.port, user)
    if net is None:
        return 1
    obs_name = "obs_" + "".join(random.choices(string.digits, k=5))
    obs_net, obs_peer = _spawn(args.host, args.port, obs_name)
    if obs_net is None:
        return 1

    _pump(net, 0.7, lambda d, c: None)
    _pump(obs_net, 0.7, lambda d, c: None)
    if not _warm_up_observer(net, peer, obs_net, obs_peer, obs_name):
        print("  observer never joined the map (no warm-up move seen)")
        return 1

    seen = []
    t0 = time.perf_counter()

    def on_ev(d, chan):
        if (chan == CHANNEL_MOVEMENT and isinstance(d, dict)
                and d.get("event") == "move"):
            dd = d.get("data")
            if isinstance(dd, dict) and dd.get("name") == user:
                seen.append((time.perf_counter() - t0, dd.get("x"), dd.get("z")))

    # Jump #1: far inside-ish (500, 0, 500)
    target = (500, 0, 500)
    _send(peer, CHANNEL_MAP, {"event": "move", "data": {
        "x": target[0], "y": target[1], "z": target[2],
        "play_sound": True, "mode": "walk", "angle": 0}})
    _pump_both(net, obs_net, 2.0, on_ev)

    # Jump #2: outside the map bounds (-5000, 0, -5000)
    target2 = (-5000, 0, -5000)
    _send(peer, CHANNEL_MAP, {"event": "move", "data": {
        "x": target2[0], "y": target2[1], "z": target2[2],
        "play_sound": True, "mode": "walk", "angle": 0}})
    _pump_both(net, obs_net, 2.0, on_ev)

    if not seen:
        print("  observer saw NO moves -> server rejected both jumps")
        return 0
    for t, x, z in seen:
        print(f"  observer saw cheater at t={t*1000:5.0f}ms  x={x}  z={z}")
    outside_ok = any(abs(x) > 1000 for _, x, _ in seen)
    far_ok = any(abs(x) >= 500 for _, x, _ in seen)
    if outside_ok:
        print("  result: ACCEPTED OUT OF BOUNDS (-5000) -> no position/teleport "
              "validation at all!")
        return 1
    if far_ok:
        print("  result: ACCEPTED (teleport to (500,0,500) relayed as-is -> "
              "no distance validation)")
        return 1
    print("  result: REJECTED/clamped (observer never saw the far positions)")
    return 0


def give_self(args):
    print("=== GIVE SELF (staff command via chat as a normal player) ===")
    user = "giv_" + "".join(random.choices(string.digits, k=5))
    net, peer = _spawn(args.host, args.port, user)
    if net is None:
        return 1
    replies = []

    def on_event(d, chan):
        if d.get("event") == "speak" and isinstance(d.get("data"), dict):
            replies.append(d["data"].get("text", ""))

    time.sleep(0.5)
    _send(peer, CHANNEL_CHAT, {"event": "chat", "data": {
        "message": f"/give {user} 9999 weapon"}})
    _pump(net, 2.0, on_event)
    # drain the chat-timer cooldown then try the shield variant
    time.sleep(1.6)
    _send(peer, CHANNEL_CHAT, {"event": "chat", "data": {
        "message": f"/giveshield {user}"}})
    _pump(net, 2.0, on_event)
    for r in replies[-6:]:
        print(f"  server replied: {r[:110]}")
    denied = any("Usage" in r or "staff" in r.lower() or "can't" in r.lower()
                 or "permission" in r.lower() for r in replies)
    print(f"  result: {'DENIED (staff check works)' if denied else 'CHECK LOG - no denial reply seen'}")
    return 0 if denied else 1


def change_map(args):
    print("=== CHANGE_MAP event as a non-builder ===")
    user = "map_" + "".join(random.choices(string.digits, k=5))
    net, peer = _spawn(args.host, args.port, user)
    if net is None:
        return 1
    replies = []

    def on_event(d, chan):
        if d.get("event") == "speak" and isinstance(d.get("data"), dict):
            replies.append(d["data"].get("text", ""))

    time.sleep(0.5)
    _send(peer, CHANNEL_MISC, {"event": "change_map", "data": {"map_name": "test_room"}})
    _pump(net, 2.0, on_event)
    for r in replies[-4:]:
        print(f"  server replied: {r[:110]}")
    denied = any("builder" in r.lower() for r in replies)
    print(f"  result: {'DENIED (builder check works)' if denied else 'NO DENIAL - may have switched maps!'}")
    return 0 if denied else 1


SCENARIOS = {
    "speed_hack": speed_hack,
    "teleport": teleport,
    "give_self": give_self,
    "change_map": change_map,
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
