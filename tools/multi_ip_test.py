"""Multi-IP / multi-location player simulation against a live server.

Binds each simulated client to a DISTINCT loopback IP (127.0.0.2, .3, ...) so
the game server sees N different client addresses - the closest thing to N
players on N different networks that one machine can produce without
administrator rights. Every bot then registers (if needed), logs in, and
pings the server on the normal cadence.

Bot #1 additionally sends an in-game chat message so a human player sitting on
the 'main' map can confirm they see a stranger appear and talk.

The distance labels (10..80 km) are the network contexts each bot pretends to
be in: on a real internet route that distance adds roughly 1-8ms of RTT; on
this loopback LAN the measured RTT stays near zero for every bot, which is
exactly why the interesting signal is the *count* of simultaneous players and
their distinct addresses, not the ping.

Run:  python tools/multi_ip_test.py --players 12 --hold 25
"""

import argparse
import json
import statistics
import sys
import threading
import time

import enet

sys.path.insert(0, __file__.rsplit("\\", 1)[0] if "\\" in __file__ else __file__.rsplit("/", 1)[0])
from loadtest_bot import _pkt, PASSWORD

CHANNEL_MISC = 0
CHANNEL_CHAT = 1
CHANNEL_PING = 5
CLIENT_VERSION = "BT-1.7.5"
PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {detail}")


def bot_ip(i):
    """127.0.0.2, .3, ... - distinct source address per bot."""
    return f"127.0.0.{i + 1}"


def register(host, port, bind_ip, username):
    """One-shot registration from a distinct source IP."""
    net = enet.Host(enet.Address(bind_ip.encode(), 0), 1, 256, 0, 0)
    peer = net.connect(enet.Address(host.encode(), port), 256)
    payload = json.dumps(
        {
            "event": "create",
            "data": {
                "username": username,
                "password": PASSWORD,
                "version": CLIENT_VERSION,
            },
        }
    ).encode()
    deadline = time.perf_counter() + 15
    sent = False
    result = "timeout"
    while time.perf_counter() < deadline:
        ev = net.service(0)
        if ev.type == enet.EVENT_TYPE_CONNECT and not sent:
            peer.send(CHANNEL_MISC, _pkt(payload))
            sent = True
        elif ev.type == enet.EVENT_TYPE_RECEIVE:
            try:
                d = json.loads(ev.packet.data)
            except Exception:
                continue
            if isinstance(d, dict) and d.get("event") in ("create_done", "create_fail"):
                result = d.get("event")
                break
        time.sleep(0.001)
    try:
        peer.disconnect()
        net.service(0)
        net.flush()
    except Exception:
        pass
    return result


class IPBot(threading.Thread):
    """A simulated player on its own source IP; optionally sends one chat line."""

    def __init__(self, idx, host, port, username, bind_ip, distance_km, chat_message=None):
        super().__init__(daemon=True)
        self.idx = idx
        self.host = host
        self.port = port
        self.username = username
        self.password = PASSWORD
        self.bind_ip = bind_ip
        self.distance_km = distance_km
        self.chat_message = chat_message
        self.connect_latency_ms = None
        self.login_latency_ms = None
        self.ping_rtts = []
        self.error = None
        self.logged_in = threading.Event()
        self.chat_sent = threading.Event()
        self._halt = threading.Event()
        self.quit = threading.Event()

    def stop(self):
        self._halt.set()

    def run(self):
        try:
            net = enet.Host(enet.Address(self.bind_ip.encode(), 0), 1, 256, 0, 0)
            peer = net.connect(enet.Address(self.host.encode(), self.port), 256)
            t0 = time.perf_counter()
            deadline = t0 + 25
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
                            self.login_latency_ms = (time.perf_counter() - login_sent_at) * 1000
                            self.logged_in.set()
                            deadline = time.perf_counter() + 180
                            if self.chat_message and not self.chat_sent.is_set():
                                chat_payload = json.dumps(
                                    {"event": "chat", "data": {"message": self.chat_message}}
                                ).encode()
                                peer.send(CHANNEL_CHAT, _pkt(chat_payload))
                                self.chat_sent.set()
                        elif name == "login_failed":
                            self.error = "login_failed: " + str(data.get("data"))
                            break
                        elif name == "ban":
                            self.error = "banned"
                            break
                        elif name == "quit":
                            self.quit.set()
                            break
                    elif ev.channelID == CHANNEL_PING:
                        try:
                            data = json.loads(ev.packet.data)
                        except Exception:
                            continue
                        if data.get("event") == "ping" and last_ping_at:
                            self.ping_rtts.append((time.perf_counter() - last_ping_at) * 1000)
                now = time.perf_counter()
                if self.logged_in.is_set():
                    if now - last_ping_at >= 3.0:
                        last_ping_at = now
                        payload = json.dumps({"event": "ping", "data": {"seq": self.idx}}).encode()
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=13000)
    ap.add_argument("--players", type=int, default=12)
    ap.add_argument("--hold", type=float, default=25.0, help="seconds bots stay online")
    args = ap.parse_args()

    n = args.players
    distances = [10, 20, 30, 40, 50, 60, 70, 80]
    dist_for = lambda i: distances[(i - 1) % len(distances)]
    username = lambda i: f"multiip_{i:04d}"

    print(f"[setup] registering {n} accounts (each from its own source IP)...")
    for i in range(1, n + 1):
        res = register(args.host, args.port, bot_ip(i), username(i))
        ok = res in ("create_done", "create_fail")
        if not ok:
            print(f"  WARN {username(i)} @ {bot_ip(i)}: register={res}")
            time.sleep(0.3)
    print("[setup] registration done")

    chat_line = (
        f"สวัสดีครับ! ผมคือผู้เล่นจำลอง multiip_0001 จากระยะ {dist_for(1)} กม. "
        f"(IP {bot_ip(1)}) มีบอท {n} คนเข้าพร้อมกันบนแผนที่ main แล้วครับ"
    )
    print(f"\n[chat] bot #1 will send: {chat_line}")

    bots = []
    for i in range(1, n + 1):
        b = IPBot(
            i,
            args.host,
            args.port,
            username(i),
            bot_ip(i),
            dist_for(i),
            chat_message=chat_line if i == 1 else None,
        )
        bots.append(b)
        b.start()
        time.sleep(0.03)

    # Wait for all bots to log in
    deadline = time.perf_counter() + 40
    while time.perf_counter() < deadline:
        ok = sum(1 for b in bots if b.logged_in.is_set())
        if ok >= n:
            break
        time.sleep(0.2)
    logged = sum(1 for b in bots if b.logged_in.is_set())
    print(f"\n=== LOGIN RESULT: {logged}/{n} players logged in ===")

    for b in bots:
        conn = f"{b.connect_latency_ms:.1f}" if b.connect_latency_ms is not None else "-"
        login = f"{b.login_latency_ms:.1f}" if b.login_latency_ms is not None else "-"
        err = f"  ERROR: {b.error}" if b.error else ""
        print(f"  bot {b.idx:>2} | IP {b.bind_ip:<10} | {b.distance_km:>3} km | connect {conn:>6}ms | login {login:>6}ms{err}")
    check(f"all {n} players logged in", logged == n, f"only {logged}/{n}")

    # Hold for --hold seconds to gather ping RTTs and let the human see the chat
    print(f"\n[hold] bots stay online for {args.hold}s (watch for the chat on 'main')...")
    time.sleep(args.hold)

    print("\n=== PING RTT SUMMARY (3s cadence) ===")
    all_rtts = []
    for b in bots:
        if b.ping_rtts:
            med = statistics.median(b.ping_rtts)
            mx = max(b.ping_rtts)
            all_rtts.extend(b.ping_rtts)
            print(
                f"  bot {b.idx:>2} | {b.distance_km:>3} km (sim) | RTT median {med:5.1f}ms | max {mx:5.1f}ms | n={len(b.ping_rtts)}"
            )
        else:
            print(f"  bot {b.idx:>2} | no ping samples")
    if all_rtts:
        print(
            f"\n  ALL BOTS: median {statistics.median(all_rtts):.1f}ms, "
            f"p95 {sorted(all_rtts)[int(len(all_rtts)*0.95)]:.1f}ms, max {max(all_rtts):.1f}ms (n={len(all_rtts)})"
        )
        check("median RTT <= 20ms (LAN/regional playable)", statistics.median(all_rtts) <= 20.0)
        check("max RTT <= 100ms (no pathological spikes)", max(all_rtts) <= 100.0)

    chat_ok = any(b.chat_sent.is_set() for b in bots)
    check("bot #1 sent its in-game chat message", chat_ok)

    print("\n[cleanup] disconnecting all bots...")
    for b in bots:
        b.stop()
    for b in bots:
        b.join(timeout=5)
    print("\n" + "=" * 60)
    print(f"RESULT: {PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
