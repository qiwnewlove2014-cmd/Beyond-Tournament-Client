"""Load-test bot for the Beyond Tournament server.

Simulates many real clients (same pyenet usage as libs/networking.py):
registers accounts, then connects + logs in N concurrent bots and measures
connect latency, login latency, and ping RTT against the game server.

Usage:
    python loadtest_bot.py --host 127.0.0.1 --port 13000 --stages 5,15,30,45,55,60 --accounts 80

Run it with the client's virtualenv python so enet is importable, e.g.:
    client/.venv/Scripts/python.exe client/tools/loadtest_bot.py ...
"""

import argparse
import json
import queue
import statistics
import sys
import threading
import time

import enet

CHANNEL_MISC = 0
CHANNEL_MAP = 4
CHANNEL_PING = 5
CLIENT_VERSION = "BT-1.7.0"
PASSWORD = "loadtest-pass"


def _pkt(data: bytes, reliable=True) -> enet.Packet:
    flags = enet.PACKET_FLAG_RELIABLE if reliable else enet.PACKET_FLAG_UNRELIABLE_FRAGMENT
    return enet.Packet(data, flags=flags)


class OneShotClient:
    """A short-lived client used for sequential account registration."""

    def __init__(self, host: str, port: int):
        self.host = host
        self.port = port
        self.net = enet.Host(None, 1, 256, 0, 0)
        self.peer = self.net.connect(enet.Address(host.encode(), port), 256)
        self.connected = False
        self.done = threading.Event()
        self.result = None

    def poll(self, timeout: float, payload: dict, expect: set):
        """Poll until an expected event arrives. payload is sent on CONNECT."""
        deadline = time.perf_counter() + timeout
        sent = False
        while time.perf_counter() < deadline:
            ev = self.net.service(0)
            if ev.type == enet.EVENT_TYPE_CONNECT and not sent:
                self.connected = True
                self.peer.send(
                    CHANNEL_MISC,
                    _pkt(json.dumps(payload).encode()),
                )
                sent = True
            elif ev.type == enet.EVENT_TYPE_RECEIVE:
                try:
                    data = json.loads(ev.packet.data)
                except Exception:
                    continue
                if isinstance(data, dict) and data.get("event") in expect:
                    self.result = data
                    self.done.set()
                    return data
            time.sleep(0.001)
        self.done.set()
        return None

    def close(self):
        try:
            self.peer.disconnect()
            self.net.service(0)
            self.net.flush()
        except Exception:
            pass
        self.net = None


class Bot(threading.Thread):
    """A single simulated game client that connects, logs in, then pings."""

    def __init__(self, idx: int, host: str, port: int, username: str, walk_target=None):
        super().__init__(daemon=True)
        self.idx = idx
        self.host = host
        self.port = port
        self.username = username
        self.password = PASSWORD
        self.connect_latency_ms = None
        self.login_latency_ms = None
        self.error = None
        self._halt = threading.Event()
        self.logged_in = threading.Event()
        self.quit = threading.Event()
        self.ping_rtts = []
        self._ping_deadline = None
        self.waypoints = list(walk_target) if walk_target else []  # list of (x, y, z)
        self.map_changes = []  # (map_name, unix_time) from parse_map events
        self.cur_pos = None  # last known position on the server (from parse_map)
        self._last_map_name = None
        self._wp_at = 0.0
        self._wp_interact_at = 0.0
        self._last_move_at = 0.0

    def stop(self):
        self._halt.set()

    def _do_walk(self, peer, now):
        """Step toward the current waypoint, sending real 'move' events."""
        if not self.waypoints or self.cur_pos is None:
            return
        tx, ty, tz = self.waypoints[0]
        cx, cy, cz = self.cur_pos
        dx, dy, dz = tx - cx, ty - cy, tz - cz
        dist = abs(dx) + abs(dy) + abs(dz)
        if dist <= 1:
            # Arrived at the travel point zone: hold still, then press F
            # (interact) to travel to the next map. Retry a few times in case
            # the first press landed during a cooldown.
            if self._wp_at == 0:
                self._wp_at = now
                self._wp_interact_at = 0.0
            if now - self._wp_at >= 1.0 and now - self._wp_interact_at >= 1.5:
                self._wp_interact_at = now
                payload = json.dumps(
                    {
                        "event": "interact",
                        "data": {"angle": 0, "pitch": 0, "selected_slot": -1},
                    }
                ).encode()
                peer.send(CHANNEL_MISC, _pkt(payload))
            if now - self._wp_at >= 6.0:
                self.waypoints.pop(0)
                self._wp_at = 0.0
            return
        if now - self._last_move_at < 0.35:
            return  # throttle to a believable walk speed
        # Adaptive step: never overshoot the waypoint so we land exactly on the
        # travel point zone center (fixed 3-tile steps could stop just outside).
        step_x = min(3, abs(dx)) if dx != 0 else 0
        step_y = min(3, abs(dy)) if dy != 0 else 0
        nx = cx + (step_x if dx > 0 else -step_x)
        ny = cy + (step_y if dy > 0 else -step_y)
        nz = cz + (1 if dz > 0 else (-1 if dz < 0 else 0))
        payload = json.dumps(
            {
                "event": "move",
                "data": {
                    "x": nx,
                    "y": ny,
                    "z": nz,
                    "play_sound": True,
                    "mode": "walk",
                },
            }
        ).encode()
        peer.send(CHANNEL_MAP, _pkt(payload))
        self.cur_pos = (nx, ny, nz)
        self._last_move_at = now

    def run(self):
        try:
            net = enet.Host(None, 1, 256, 0, 0)
            peer = net.connect(enet.Address(self.host.encode(), self.port), 256)
            t0 = time.perf_counter()
            deadline = t0 + 20
            login_sent = False
            login_sent_at = None
            last_ping_at = 0.0
            while not self._halt.is_set():
                if time.perf_counter() > deadline:
                    self.error = "timeout-waiting-login"
                    break
                ev = net.service(0)
                if ev.type == enet.EVENT_TYPE_CONNECT:
                    self.connect_latency_ms = (time.perf_counter() - t0) * 1000
                    if not login_sent:
                        payload = json.dumps(
                            {
                                "event": "login",
                                "data": {
                                    "username": self.username,
                                    "password": self.password,
                                    "version": CLIENT_VERSION,
                                },
                            }
                        ).encode()
                        peer.send(CHANNEL_MISC, _pkt(payload))
                        login_sent = True
                        login_sent_at = time.perf_counter()
                elif ev.type == enet.EVENT_TYPE_RECEIVE:
                    if ev.channelID < CHANNEL_PING:
                        try:
                            data = json.loads(ev.packet.data)
                        except Exception:
                            continue
                        if not isinstance(data, dict):
                            continue
                        name = data.get("event")
                        if name == "connected" and login_sent_at is not None:
                            self.login_latency_ms = (
                                time.perf_counter() - login_sent_at
                            ) * 1000
                            self.logged_in.set()
                            deadline = time.perf_counter() + 180  # extend for walking
                        elif name == "login_failed":
                            self.error = "login_failed: " + str(data.get("data"))
                            break
                        elif name == "ban":
                            self.error = "banned"
                            break
                        elif name == "quit":
                            self.quit.set()
                            break
                        elif name == "parse_map":
                            d = data.get("data") or {}
                            map_name = d.get("name") or "?"
                            self.map_changes.append((map_name, time.time()))
                            # Teleported to a new map: drop the waypoint we were
                            # standing in (its zone already worked).
                            if (
                                self._last_map_name is not None
                                and map_name != self._last_map_name
                                and self.waypoints
                            ):
                                self.waypoints.pop(0)
                                self._wp_at = 0.0
                            self._last_map_name = map_name
                            if all(
                                isinstance(d.get(k), (int, float))
                                for k in ("x", "y", "z")
                            ):
                                self.cur_pos = (d["x"], d["y"], d["z"])
                    elif ev.channelID == CHANNEL_PING:
                        try:
                            data = json.loads(ev.packet.data)
                        except Exception:
                            continue
                        if data.get("event") == "ping" and last_ping_at:
                            self.ping_rtts.append(
                                (time.perf_counter() - last_ping_at) * 1000
                            )
                now = time.perf_counter()
                if self.logged_in.is_set():
                    if self.waypoints:
                        self._do_walk(peer, now)
                    if now - last_ping_at >= 3.0:
                        last_ping_at = now
                        payload = json.dumps(
                            {"event": "ping", "data": {"seq": self.idx}}
                        ).encode()
                        peer.send(CHANNEL_PING, _pkt(payload, reliable=False))
                time.sleep(0.001)
            try:
                peer.disconnect()
                net.service(0)
                net.flush()
            except Exception:
                pass
        except Exception as e:
            self.error = f"{type(e).__name__}: {e}"


class BuilderWarpBot(threading.Thread):
    """Drives the in-game builder travel point menu: login -> open builder -> create travel point.

    Assumes the account is already in the server's builder.txt (set up before
    the server starts). Answers every prompt with sensible defaults.
    """

    def __init__(self, host, port, username, password, target_map):
        super().__init__(daemon=True)
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.target_map = target_map
        self._halt = threading.Event()
        self.log = []
        self.ok = False
        self.error = None
        self.phase = 1  # 1=create, 2=pick warp, 3=toggle announce, 4=delete, 5=done

    def stop(self):
        self._halt.set()

    def _send(self, peer, event, data):
        payload = json.dumps({"event": event, "data": data}).encode()
        peer.send(CHANNEL_MISC, _pkt(payload))

    def run(self):
        try:
            net = enet.Host(None, 1, 256, 0, 0)
            peer = net.connect(enet.Address(self.host.encode(), self.port), 256)
            t0 = time.perf_counter()
            logged_in = False
            opened_builder = False
            stage_answer = {
                "warpPosX": "",
                "warpPosY": "",
                "warpPosZ": "",
                "warpTargetX": "",
                "warpTargetY": "",
                "warpTargetZ": "",
                "warpSize": "3",
                "warpCooldown": "1",
                "warpAnnounce": "y",
            }
            last_state = None
            while not self._halt.is_set() and time.perf_counter() - t0 < 45:
                ev = net.service(0)
                if ev.type == enet.EVENT_TYPE_CONNECT and not logged_in:
                    payload = json.dumps(
                        {
                            "event": "login",
                            "data": {
                                "username": self.username,
                                "password": self.password,
                                "version": CLIENT_VERSION,
                            },
                        }
                    ).encode()
                    peer.send(CHANNEL_MISC, _pkt(payload))
                    logged_in = True
                elif ev.type == enet.EVENT_TYPE_RECEIVE:
                    if ev.channelID < 20:  # all JSON channels incl. menus (6)
                        try:
                            data = json.loads(ev.packet.data)
                        except Exception:
                            continue
                        if not isinstance(data, dict):
                            continue
                        name = data.get("event")
                        d = data.get("data") or {}
                        if name == "connected":
                            self.log.append("connected")
                            self._send(peer, "open_builder", {})
                            opened_builder = True
                        elif name == "travel_point_hint":
                            self.log.append(f"hint: travel to {d.get('map')}")
                        elif name == "make_menu":
                            menu_name = d.get("event") or ""
                            options = d.get("options") or []
                            if menu_name == "builder_menu_select":
                                self.log.append("menu:builder_menu_select")
                                val = next(
                                    (o["value"] for o in options if o.get("value") == "travel_point"), None
                                )
                                self.log.append(f"select builder option: {val}")
                                self._send(peer, "builder_menu_select", {"value": val})
                            elif menu_name == "builder_warp_menu":
                                self.log.append("menu:builder_warp_menu")
                                if self.phase == 1:
                                    self._send(peer, "builder_warp_menu", {"value": "create"})
                                else:
                                    self._send(peer, "builder_warp_menu", {"value": "manage"})
                            elif menu_name == "builder_warp_target_select":
                                self.log.append("menu:builder_warp_target_select")
                                val = next(
                                    (
                                        o["value"]
                                        for o in options
                                        if isinstance(o.get("value"), str)
                                        and o["value"] == self.target_map
                                    ),
                                    None,
                                )
                                self.log.append(f"select target map: {val}")
                                self._send(peer, "builder_warp_target_select", {"value": val})
                            elif menu_name == "builder_warp_manage_select":
                                self.log.append("menu:builder_warp_manage_select")
                                has_actions = any(
                                    isinstance(o.get("value"), dict)
                                    and o["value"].get("action")
                                    for o in options
                                )
                                if not has_actions:
                                    # travel point list menu
                                    if self.phase in (2, 4):
                                        # Prefer the travel point that leads to our
                                        # target map (the one we just created).
                                        def _pick():
                                            for o in options:
                                                if (
                                                    isinstance(o.get("value"), dict)
                                                    and o["value"].get("warpId")
                                                    and self.target_map
                                                    in str(o.get("title", ""))
                                                ):
                                                    return o["value"]
                                            return next(
                                                (
                                                    o["value"]
                                                    for o in options
                                                    if isinstance(o.get("value"), dict)
                                                    and o["value"].get("warpId")
                                                ),
                                                None,
                                            )
                                        val = _pick()
                                        self.log.append(f"pick warp: {val}")
                                        self._send(peer, "builder_warp_manage_select", {"value": val})
                                        self.phase = 3 if self.phase == 2 else 5
                                else:
                                    # per-warp action menu
                                    if self.phase == 3:
                                        val = next(
                                            (
                                                o["value"]
                                                for o in options
                                                if isinstance(o.get("value"), dict)
                                                and o["value"].get("action") == "announce"
                                            ),
                                            None,
                                        )
                                        self.log.append(f"toggle announce: {val}")
                                        self._send(peer, "builder_warp_manage_select", {"value": val})
                                    elif self.phase == 5:
                                        val = next(
                                            (
                                                o["value"]
                                                for o in options
                                                if isinstance(o.get("value"), dict)
                                                and o["value"].get("action") == "delete"
                                            ),
                                            None,
                                        )
                                        self.log.append(f"delete: {val}")
                                        self._send(peer, "builder_warp_manage_select", {"value": val})
                                        self.phase = 6
                            elif menu_name == "builder_warp_delete_confirm":
                                self.log.append("menu:builder_warp_delete_confirm")
                                val = next(
                                    (
                                        o["value"]
                                        for o in options
                                        if isinstance(o.get("value"), dict)
                                        and o["value"].get("elementType") == "Travel Point"
                                    ),
                                    None,
                                )
                                self.log.append(f"confirm delete: {val}")
                                self._send(peer, "builder_warp_delete_confirm", {"value": val})
                                self.phase = 6
                        elif name == "make_input":
                            state = d.get("data") or {}
                            stage = state.get("stage")
                            last_state = state
                            self.log.append(f"input:{stage}")
                            ans = stage_answer.get(stage, "")
                            self._send(
                                peer,
                                "builder_warp_input",
                                {"value": ans, "data": state},
                            )
                        elif name == "speak":
                            text = (d.get("text") or "").lower()
                            if "travel point created" in text:
                                if self.phase == 1:
                                    self.ok = True
                                    self.log.append("speak:PHASE1-SUCCESS " + text[:60])
                                    # go to manage phase
                                    self.phase = 2
                                    self._send(peer, "open_builder", {})
                            elif "travel point updated" in text:
                                if self.phase == 3:
                                    self.log.append("speak:PHASE3-TOGGLED " + text[:40])
                                    self.phase = 4
                                    self._send(peer, "open_builder", {})
                            elif "element deleted" in text:
                                if self.phase == 6:
                                    self.log.append("speak:PHASE4-DELETED " + text[:40])
                                    self.phase = 7
                            elif "permission denied" in text:
                                self.error = "permission denied"
                                self.log.append("speak:PERMISSION DENIED")
                            elif "no travel points" in text:
                                self.log.append("speak:NO-WARPS " + text[:60])
                            else:
                                self.log.append("speak: " + text[:70])
                        elif name == "login_failed":
                            self.error = "login_failed: " + str(d)
                            break
                time.sleep(0.002)
            if not self.ok and not self.error:
                self.error = "timeout"
            try:
                peer.disconnect()
                net.service(0)
                net.flush()
            except Exception:
                pass
        except Exception as e:
            self.error = f"{type(e).__name__}: {e}"


def register_account(host, port, username, timeout=15):
    payload = {
        "event": "create",
        "data": {
            "username": username,
            "password": PASSWORD,
            "version": CLIENT_VERSION,
        },
    }
    c = OneShotClient(host, port)
    try:
        res = c.poll(timeout, payload, {"create_done", "create_fail"})
        if res is None:
            return "timeout"
        if res.get("event") == "create_fail":
            return "create_fail (probably exists)"
        return "ok"
    finally:
        c.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=13000)
    ap.add_argument("--accounts", type=int, default=80)
    ap.add_argument("--stages", default="5,15,30,45,55,60,63")
    ap.add_argument("--hold", type=float, default=8.0, help="seconds to hold each stage")
    ap.add_argument("--burst", type=int, default=0, help="bots in the final login-burst test (default: max stage)")
    ap.add_argument("--warp-test", action="store_true", help="walk one bot through the demo warp chain (main -> plaza -> arena)")
    ap.add_argument("--builder-warp-test", action="store_true", help="drive the in-game builder travel point menu to create a travel point")
    ap.add_argument("--register-only", action="store_true")
    args = ap.parse_args()

    stages = [int(s) for s in args.stages.split(",") if int(s) > 0]
    username = lambda i: f"loadtest_{i:04d}"

    if args.builder_warp_test:
        print("[builder-warp-test] using account loadtest_0001 (must be in builder.txt)")
        b = BuilderWarpBot(
            args.host, args.port, "loadtest_0001", PASSWORD, "warp_demo_arena"
        )
        b.start()
        b.join(timeout=60)
        print("\n=== BUILDER WARP TEST RESULT ===")
        for line in b.log:
            safe = line.encode("ascii", "replace").decode("ascii")
            print("  " + safe)
        result = "PASS" if b.phase >= 7 else "FAIL"
        print(f"  RESULT: {result} (final phase={b.phase})" + (f" ({b.error})" if b.error else ""))
        return

    if args.warp_test:
        print("[warp-test] registering accounts...")
        for i in range(1, 3):
            res = register_account(args.host, args.port, username(i))
            print(f"  {username(i)}: {res}")
        # main travel point sits at (5..7, 5..7); plaza east at (27..29, 13..15);
        # arena west at (0..2, 8..10). The bot presses F once inside each zone.
        b1 = Bot(
            1,
            args.host,
            args.port,
            username(1),
            walk_target=[(6, 6, 0), (28, 14, 1), (1, 9, 1)],
        )
        b2 = Bot(2, args.host, args.port, username(2))
        b1.start()
        b2.start()
        t0 = time.time()
        while time.time() - t0 < 90:
            maps_seen = [m for m, _ in b1.map_changes]
            if maps_seen.count("warp_demo_arena") >= 1:
                break
            if b1.error:
                break
            time.sleep(0.5)
        print("\n=== WARP TEST RESULT ===")
        print("  bot1 map timeline:")
        for m, t in b1.map_changes:
            print(f"    {t - b1.map_changes[0][1]:6.1f}s  {m}")
        if b1.error:
            print(f"  bot1 error: {b1.error}")
        ok = any(m == "warp_demo_plaza" for m, _ in b1.map_changes) and any(
            m == "warp_demo_arena" for m, _ in b1.map_changes
        )
        print(f"  bot2 (stayed on main) ping RTTs: {len(b2.ping_rtts)} samples")
        if b2.ping_rtts:
            print(
                f"    median {statistics.median(b2.ping_rtts):.1f}ms, max {max(b2.ping_rtts):.1f}ms"
            )
        print(f"  RESULT: {'PASS' if ok else 'FAIL'}")
        b1.stop()
        b2.stop()
        b1.join(timeout=5)
        b2.join(timeout=5)
        return

    if args.register_only:
        t0 = time.perf_counter()
        for i in range(1, args.accounts + 1):
            res = register_account(args.host, args.port, username(i))
            print(f"register {username(i)}: {res}", flush=True)
        print(f"registration done in {time.perf_counter() - t0:.1f}s", flush=True)
        return

    # Phase 1: register accounts
    print(f"[setup] registering {args.accounts} accounts...", flush=True)
    t0 = time.perf_counter()
    for i in range(1, args.accounts + 1):
        res = register_account(args.host, args.port, username(i))
        if res == "timeout":
            print(f"[setup] WARN {username(i)}: timeout", flush=True)
    print(f"[setup] registration done in {time.perf_counter() - t0:.1f}s", flush=True)

    bots = []
    max_stage = max(stages)
    for stage in stages:
        # spawn new bots up to this stage count
        while len(bots) < stage:
            i = len(bots) + 1
            b = Bot(i, args.host, args.port, username(i))
            bots.append(b)
            b.start()
            time.sleep(0.02)  # stagger login burst a little (shared IP rate limit)
        # wait for the newly added bots to log in
        new_start = stage - len([x for x in bots if x.login_latency_ms is not None])
        deadline = time.perf_counter() + 25
        while time.perf_counter() < deadline:
            ok = sum(1 for b in bots if b.logged_in.is_set())
            if ok >= stage:
                break
            time.sleep(0.2)
        ok = sum(1 for b in bots if b.logged_in.is_set())
        print(f"\n=== STAGE {stage} players (logged in {ok}/{stage}) ===", flush=True)
        # report login latency for all bots that logged in during this stage
        logged = [b for b in bots if b.logged_in.is_set()]
        lats = [b.login_latency_ms for b in logged if b.login_latency_ms is not None]
        cons = [b.connect_latency_ms for b in logged if b.connect_latency_ms is not None]
        if lats:
            print(
                f"  connect : median {statistics.median(cons):.1f}ms, max {max(cons):.1f}ms",
                flush=True,
            )
            print(
                f"  login   : median {statistics.median(lats):.1f}ms, p95 {_pct(lats, 95):.1f}ms, max {max(lats):.1f}ms",
                flush=True,
            )
        failed = [b for b in bots if b.error and not b.logged_in.is_set()]
        for b in failed:
            print(f"  FAIL bot {b.idx}: {b.error}", flush=True)
        # hold: collect pings
        time.sleep(args.hold)
        rtts = []
        for b in bots:
            if b.logged_in.is_set():
                rtts.extend(b.ping_rtts)
        if rtts:
            print(
                f"  ping RTT: median {statistics.median(rtts):.1f}ms, p95 {_pct(rtts, 95):.1f}ms, max {max(rtts):.1f}ms (n={len(rtts)})",
                flush=True,
            )
        if stage == max_stage:
            break

    # Phase 3: disconnect all, then login burst from zero
    burst_n = args.burst or max_stage
    print(f"\n=== LOGIN BURST TEST (0 -> {burst_n} all at once) ===")
    for b in bots:
        b.stop()
    for b in bots:
        b.join(timeout=10)
    bots.clear()
    t_burst = time.perf_counter()
    for i in range(1, burst_n + 1):
        b = Bot(i, args.host, args.port, username(i))
        bots.append(b)
        b.start()
        time.sleep(0.02)
    deadline = time.perf_counter() + 90
    while time.perf_counter() < deadline:
        ok = sum(1 for b in bots if b.logged_in.is_set())
        if ok >= burst_n:
            break
        time.sleep(0.2)
    burst_elapsed = time.perf_counter() - t_burst
    ok = sum(1 for b in bots if b.logged_in.is_set())
    lats = [b.login_latency_ms for b in bots if b.login_latency_ms is not None]
    print(f"  {ok}/{burst_n} logged in within {burst_elapsed:.1f}s")
    if lats:
        print(
            f"  login   : median {statistics.median(lats):.1f}ms, p95 {_pct(lats, 95):.1f}ms, max {max(lats):.1f}ms"
        )
    failed = [b for b in bots if b.error and not b.logged_in.is_set()]
    for b in failed:
        print(f"  FAIL bot {b.idx}: {b.error}")

    print("\n[cleanup] disconnecting all bots...")
    for b in bots:
        b.stop()
    for b in bots:
        b.join(timeout=5)
    print("[done]")


def _pct(vals, p):
    if not vals:
        return 0.0
    s = sorted(vals)
    return s[min(len(s) - 1, int(len(s) * p / 100))]


if __name__ == "__main__":
    main()
