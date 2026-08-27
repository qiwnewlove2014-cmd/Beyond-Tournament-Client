"""Bounded, opt-in online-occupancy test. No chat, movement, combat or audio send.

Default is a dry run. Live runs need --run, a fresh --report path and --stop-file.
Reads the release endpoint from build_server_config.json, never saved accounts.
Creates at most --count ordinary accounts (max 55), which remain on the server.
Passwords are random, memory-only, never included in logs/reports. No retries.

One event-loop thread owns every ENet peer; memory is O(peers + bounded samples).
Each online bot sends at most one ping per 5 seconds, with only one in flight.
The first bot requests visible player counts every 3 seconds. Server CPU/RAM,
hidden players, audio quality and gameplay performance are NOT measured.
"""

from __future__ import annotations

import argparse
from collections import deque
from dataclasses import dataclass, field
import json
import math
from pathlib import Path
import re
import secrets
import sys
import time

CLIENT_ROOT = Path(__file__).resolve().parents[1]
if str(CLIENT_ROOT) not in sys.path:
    sys.path.insert(0, str(CLIENT_ROOT))
from libs import consts
from libs.server_config import validate_server_endpoint


class RampAbort(RuntimeError):
    pass


def percentile(values, fraction=0.95):
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * fraction) - 1)] if ordered else None


def visible_count(data):
    if data.get("buffer") != "main" or not isinstance(data.get("text"), str):
        return None
    if data["text"] == "You are all alone. How sad!":
        return 1
    match = re.match(r"^(\d{1,4}) Online players: ", data["text"])
    return int(match[1]) if match else None


class EnetBackend:
    """All methods are called by the same thread; no game/audio imports."""

    def __init__(self, host, port, count):
        import enet
        self.enet = enet
        self.address = enet.Address(host.encode(), port)
        self.net = enet.Host(None, count, 256, 0, 0)
        self.peers = {}
        self.tx_packets = self.tx_bytes = self.rx_packets = self.rx_bytes = 0

    def connect(self):
        peer = self.net.connect(self.address, 256)
        key = peer.incomingPeerID  # Local slot, stable even after disconnect.
        if key in self.peers:
            raise RampAbort("peer_slot_reuse")
        self.peers[key] = peer
        return key

    def send(self, key, event, data):
        if event not in {"create", "login", "ping", "who_online", "logout"}:
            raise RampAbort("outgoing_event_not_allowed")
        raw = json.dumps({"event": event, "data": data}).encode("utf-8")
        channel = consts.CHANNEL_PING if event == "ping" else consts.CHANNEL_MISC
        self.peers[key].send(channel, self.enet.Packet(raw, flags=self.enet.PACKET_FLAG_RELIABLE))
        self.net.flush()
        self.tx_packets += 1
        self.tx_bytes += len(raw)

    def receive(self, timeout_ms=20):
        # On Windows an unbound outbound Host has no serviceable socket until
        # connect() opens its first peer. Let the scheduler admit that peer.
        if not self.peers:
            return None
        event = self.net.service(timeout_ms)
        if event.type == self.enet.EVENT_TYPE_NONE:
            return None
        key = event.peer.incomingPeerID
        if event.type == self.enet.EVENT_TYPE_CONNECT:
            return key, "transport_connected", {}
        if event.type == self.enet.EVENT_TYPE_DISCONNECT:
            return key, "transport_disconnected", {}
        if event.type != self.enet.EVENT_TYPE_RECEIVE:
            return None
        raw = event.packet.data
        self.rx_packets += 1
        self.rx_bytes += len(raw)
        if event.channelID >= consts.CHANNEL_VOICECHAT or not raw or len(raw) > 8 * 1024 * 1024:
            return None  # Never decode/save/play incoming voice or music.
        try:
            message = json.loads(raw)
        except (ValueError, UnicodeError):
            return None
        if not isinstance(message, dict) or not isinstance(message.get("event"), str):
            return None
        data = message.get("data")
        return key, message["event"], data if isinstance(data, dict) else {}

    def disconnect(self, key):
        self.peers[key].disconnect()
        self.net.flush()

    def reset(self, key):
        self.peers[key].reset()


@dataclass
class Bot:
    key: int
    username: str
    password: str = field(repr=False)
    state: str = "connecting"
    deadline: float = 0.0
    create_sent: bool = False
    created: bool = False
    login_sent: bool = False
    authenticated: bool = False
    own_online: bool = False
    map_seen: bool = False
    online_at: float = 0.0
    next_ping: float = 0.0
    pending_ping: float | None = None
    rtts: deque = field(default_factory=lambda: deque(maxlen=60))
    slow_streak: int = 0
    logout_ack: bool = False
    disconnect_ack: bool = False


class Ramp:
    def __init__(self, backend, count=5, *, interval=3, stage_hold=15, final_hold=30,
                 clock=time.monotonic, stopped=lambda: False, report=lambda _text: None,
                 run_id=None):
        if count not in (5, 10, 20, 30, 40, 50, 55):
            raise ValueError("count must be 5, 10, 20, 30, 40, 50 or 55")
        if not 3 <= interval <= 10 or not 15 <= stage_hold <= 60 or not 15 <= final_hold <= 120:
            raise ValueError("invalid timing limits")
        self.backend, self.count = backend, count
        self.interval, self.stage_hold, self.final_hold = interval, stage_hold, final_hold
        self.clock, self.stopped, self.report = clock, stopped, report
        self.run_id = run_id or time.strftime("%H%M%S") + secrets.token_hex(2)
        self.bots = {}
        self.stages = [n for n in (5, 10, 20, 30, 40, 50, 55) if n <= count]
        self.stage = 0
        self.stage_since = None
        self.stage_results = []
        self.baseline_p95 = None
        self.visible_total = None
        self.count_own_at_snapshot = 0
        self.count_at = -1.0
        self.next_count = 0.0
        self.count_requested = None
        self.last_admission = -100.0
        self.started = clock()
        self.peak_online = 0
        self.cleaned = False
        self.cleanup_errors = 0

    def ready(self):
        return [bot for bot in self.bots.values() if bot.state == "online"]

    def send(self, bot, event, data=None):
        self.backend.send(bot.key, event, {} if data is None else data)

    def handle(self, message, *, cleaning=False):
        if not cleaning and self.stopped():
            raise RampAbort("operator_stop")
        if message is None:
            return
        key, event, data = message
        bot = self.bots.get(key)
        if bot is None:
            return
        now = self.clock()
        if event == "transport_disconnected":
            bot.disconnect_ack = True
            bot.state = "closed"
            if not cleaning:
                raise RampAbort("unexpected_disconnect")
            return
        if cleaning:
            if event == "create_done":
                bot.created = True
            elif event == "quit":
                bot.logout_ack = True
                self.backend.disconnect(key)
                bot.state = "disconnecting"
                bot.deadline = now + 2
            return  # Never create/login again while draining.
        if event in {"create_fail", "login_failed", "ban", "quit"}:
            raise RampAbort("server_rejected_" + event)
        if event == "transport_connected" and bot.state == "connecting":
            bot.create_sent = True
            bot.state, bot.deadline = "creating", now + 15
            self.send(bot, "create", {"username": bot.username, "password": bot.password,
                                      "version": consts.CLIENT_VERSION, "capabilities": []})
        elif event == "create_done" and bot.state == "creating":
            bot.created = bot.login_sent = True
            bot.state, bot.deadline = "logging_in", now + 20
            self.send(bot, "login", {"username": bot.username, "password": bot.password,
                                     "version": consts.CLIENT_VERSION, "capabilities": []})
            bot.password = ""
        elif event == "connected":
            if not bot.login_sent or data.get("username") != bot.username:
                raise RampAbort("unexpected_authenticated_username")
            bot.authenticated = True
        elif event == "online" and data.get("username") == bot.username:
            bot.own_online = True
        elif event == "parse_map":
            bot.map_seen = True  # Do not retain maps, player positions or tokens.
        elif event == "ping" and bot.pending_ping is not None:
            elapsed = (now - bot.pending_ping) * 1000
            bot.pending_ping = None
            bot.rtts.append(elapsed)
            limit = max(250, 3 * (self.baseline_p95 or 0))
            bot.slow_streak = bot.slow_streak + 1 if elapsed > limit else 0
            if elapsed > 1000 or bot.slow_streak >= 2:
                raise RampAbort("latency_guard")
        elif event == "speak" and key == next(iter(self.bots), None) and self.count_requested is not None:
            count = visible_count(data)
            if count is not None:
                self.visible_total, self.count_at = count, now
                self.count_own_at_snapshot = len(self.ready())
                self.count_requested = None
        if bot.state == "logging_in" and bot.authenticated and bot.own_online and bot.map_seen:
            bot.state = "online"
            bot.online_at = now
            bot.next_ping = now + (len(self.bots) % 5) * 0.2
            self.peak_online = max(self.peak_online, len(self.ready()))
            self.report("ONLINE " + bot.username + " (" + str(len(self.ready())) + "/" + str(self.count) + ")")

    def pump(self, *, cleaning=False):
        self.handle(self.backend.receive(20), cleaning=cleaning)
        for _ in range(255):
            message = self.backend.receive(0)
            if message is None:
                break
            self.handle(message, cleaning=cleaning)

    def tick(self):
        if self.stopped():
            raise RampAbort("operator_stop")
        self.pump()
        now = self.clock()
        if self.stopped():
            raise RampAbort("operator_stop")
        if now - self.started > 600:
            raise RampAbort("hard_time_limit")
        online = self.ready()
        for bot in self.bots.values():
            if bot.state != "online" and now > bot.deadline:
                raise RampAbort("timeout_" + bot.state)
            if bot.pending_ping is not None and now - bot.pending_ping > 3:
                raise RampAbort("ping_timeout")
            if bot.state == "online" and bot.pending_ping is None and now >= bot.next_ping:
                bot.pending_ping = now
                bot.next_ping = now + 5
                self.send(bot, "ping")
        if online and self.count_requested is None and now >= self.next_count:
            self.count_requested = now
            self.next_count = now + 3
            self.send(online[0], "who_online")
        if self.count_requested is not None and now - self.count_requested > 10:
            raise RampAbort("visible_count_timeout")
        if self.visible_total is not None and self.visible_total > 60:
            raise RampAbort("visible_player_headroom_limit")
        target = self.stages[self.stage]
        if len(online) == target:
            if self.stage_since is None:
                self.stage_since = now
                self.report("STAGE " + str(target) + ": observing")
            hold = self.final_hold if target == self.count else self.stage_hold
            if now - self.stage_since >= hold and all(len(bot.rtts) >= 2 for bot in online):
                values = [value for bot in online for value in list(bot.rtts)[-3:]]
                p95 = percentile(values)
                if p95 is None or p95 > max(250, (self.baseline_p95 or 0) * 3):
                    raise RampAbort("stage_latency_guard")
                snapshot = {"bots": target, "p95_ping_ms": round(p95, 1),
                            "visible_players": self.visible_total}
                self.stage_results.append(snapshot)
                self.report("STAGE_OK " + json.dumps(snapshot))
                if self.baseline_p95 is None:
                    self.baseline_p95 = p95
                if target == self.count:
                    return True
                self.stage += 1
                self.stage_since = None
        elif len(self.bots) == len(online) and now - self.last_admission >= self.interval:
            # One authentication at a time. Require a fresh occupancy snapshot
            # and a successful ping before admitting the next bot.
            if online:
                if (self.visible_total is None or self.count_at < max(bot.online_at for bot in online)
                        or not all(bot.rtts for bot in online)):
                    return False
                estimated = self.visible_total + max(0, len(online) - self.count_own_at_snapshot)
                if estimated >= 60:
                    raise RampAbort("visible_player_headroom_limit")
            key = self.backend.connect()
            username = "BT-ramp-" + self.run_id + "-" + f"{len(self.bots)+1:02d}"
            self.bots[key] = Bot(key, username, secrets.token_urlsafe(24), deadline=now + 15)
            self.last_admission = now
        return False

    def cleanup(self):
        """Stagger ordinary logout, wait for quit, then ENet disconnect ACK."""
        def reset(bot):
            try:
                self.backend.reset(bot.key)
            except Exception:
                self.cleanup_errors += 1
            bot.state = "closed"

        def disconnect(bot):
            try:
                self.backend.disconnect(bot.key)
                bot.state, bot.deadline = "disconnecting", self.clock() + 2
            except Exception:
                self.cleanup_errors += 1
                reset(bot)

        queue = list(self.bots.values())
        next_logout = self.clock()
        limit = self.clock() + 20
        while (queue or any(bot.state != "closed" for bot in self.bots.values())) and self.clock() < limit:
            now = self.clock()
            if queue and now >= next_logout:
                bot = queue.pop(0)
                next_logout = now + 0.15
                if bot.state != "closed":
                    try:
                        if bot.login_sent:
                            self.send(bot, "logout", {"message": False})
                            bot.state, bot.deadline = "logging_out", now + 5
                        else:
                            disconnect(bot)
                    except Exception:
                        self.cleanup_errors += 1
                        reset(bot)
            try:
                self.pump(cleaning=True)
            except Exception:
                pass
            for bot in self.bots.values():
                if bot.state == "logging_out" and self.clock() >= bot.deadline:
                    disconnect(bot)
                elif bot.state == "disconnecting" and self.clock() >= bot.deadline:
                    reset(bot)
        for bot in self.bots.values():
            if bot.state != "closed":
                reset(bot)
            bot.password = ""
        self.cleaned = self.cleanup_errors == 0

    def run(self):
        reason = "completed"
        cpu_start = time.process_time()
        try:
            while not self.tick():
                pass
        except RampAbort as error:
            reason = str(error)
        except KeyboardInterrupt:
            reason = "operator_interrupt"
        except Exception:
            reason = "local_transport_error"
        finally:
            self.report("DRAINING: no more accounts or logins")
            self.cleanup()
        return {
            "reason": reason, "target_bots": self.count, "peak_online_bots": self.peak_online,
            "stages": self.stage_results, "accounts_created": [bot.username for bot in self.bots.values() if bot.created],
            "creation_uncertain": [bot.username for bot in self.bots.values() if bot.create_sent and not bot.created],
            "logout_acknowledged": sum(bot.logout_ack for bot in self.bots.values()),
            "logout_unconfirmed": [bot.username for bot in self.bots.values() if bot.login_sent and not bot.logout_ack],
            "disconnect_acknowledged": sum(bot.disconnect_ack for bot in self.bots.values()),
            "local_peers_closed": self.cleaned,
            "cleanup_errors": self.cleanup_errors,
            "elapsed_seconds": round(self.clock() - self.started, 1),
            "local_cpu_seconds": round(time.process_time() - cpu_start, 2),
            "tx_application_packets": self.backend.tx_packets, "tx_application_bytes": self.backend.tx_bytes,
            "rx_application_packets": self.backend.rx_packets, "rx_application_bytes": self.backend.rx_bytes,
            "limitations": "No server CPU/RAM or audio-quality measurement; visible count excludes hidden staff. Accounts remain in DB."
        }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--count", type=int, choices=(5, 10, 20, 30, 40, 50, 55), default=5)
    parser.add_argument("--stop-file", type=Path)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args(argv)
    if not args.run:
        print("DRY RUN: no connection or accounts. Planned maximum:", args.count)
        return 0
    if not args.stop_file or not args.report or args.stop_file.exists() or args.report.exists():
        parser.error("Live run requires a nonexistent stop-file and a fresh report path.")
    if not args.report.parent.is_dir() or not args.stop_file.parent.is_dir():
        parser.error("Create the report/stop parent directory first.")
    config = json.loads((CLIENT_ROOT / "build_server_config.json").read_text(encoding="utf-8"))
    host, port = validate_server_endpoint(config.get("host"), config.get("port"))
    ramp = Ramp(EnetBackend(host, port, args.count), args.count, stopped=args.stop_file.exists,
                report=lambda text: print(text, flush=True))
    result = ramp.run()
    args.report.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result, indent=2), flush=True)
    return 0 if (result["reason"] == "completed" and not result["logout_unconfirmed"]
                 and result["local_peers_closed"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
