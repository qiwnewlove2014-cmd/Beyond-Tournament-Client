"""Create ONE test account, stay online briefly, then log out normally.

No traffic is sent without --run. Example (uses the release endpoint):
    python tools/character_smoke_test.py --run --hold 60 --interact

The account remains in the server database. A random password is kept only in
memory, never printed/saved. --interact additionally allows four slow, local
walking steps, one greeting and at most two fixed replies to 'smoke'. It never
fights, edits maps, runs chat commands, retries registration, or bypasses auth.
This is a bounded smoke test, NOT an audio/playability or load test.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
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


class ProbeError(RuntimeError):
    """Only fixed, non-sensitive messages may be raised by the probe."""


class EnetTransport:
    """One peer, one thread, using the same wire format as libs.networking."""

    def __init__(self, host, port):
        import enet  # Lazy: importing/testing the tool never opens a socket.
        self.enet = enet
        self.net = enet.Host(None, 1, 256, 0, 0)
        self.peer = self.net.connect(enet.Address(host.encode(), port), 256)
        self.disconnected = False

    def send(self, event, data):
        if event not in {"create", "login", "ping", "logout", "chat", "move"}:
            raise ProbeError("outgoing_event_not_allowed")
        if event == "chat" and (not isinstance(data.get("message"), str)
                                or data["message"].lstrip().startswith("/")):
            raise ProbeError("chat_commands_not_allowed")
        channel = {"ping": consts.CHANNEL_PING, "chat": consts.CHANNEL_CHAT,
                   "move": consts.CHANNEL_MAP}.get(event, consts.CHANNEL_MISC)
        packet = self.enet.Packet(
            json.dumps({"event": event, "data": data}).encode("utf-8"),
            flags=self.enet.PACKET_FLAG_RELIABLE,
        )
        self.peer.send(channel, packet)
        self.net.flush()

    def receive(self):
        event = self.net.service(50)
        if event.type == self.enet.EVENT_TYPE_CONNECT:
            return "transport_connected", {}
        if event.type == self.enet.EVENT_TYPE_DISCONNECT:
            self.disconnected = True
            raise ProbeError("transport_disconnected")
        if event.type != self.enet.EVENT_TYPE_RECEIVE:
            return None
        if event.channelID >= consts.CHANNEL_VOICECHAT:
            return None  # Never decode, play or record other players' audio.
        raw = event.packet.data
        if not raw or len(raw) > 8 * 1024 * 1024:
            return None
        try:
            payload = json.loads(raw)
        except (ValueError, UnicodeError):
            return None
        if not isinstance(payload, dict) or not isinstance(payload.get("event"), str):
            return None
        data = payload.get("data")
        return payload["event"], data if isinstance(data, dict) else {}

    def close(self):
        if self.disconnected:
            return True
        self.peer.disconnect()
        self.net.flush()
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if self.net.service(50).type == self.enet.EVENT_TYPE_DISCONNECT:
                self.disconnected = True
                return True
        self.peer.reset()  # Local fallback only; never reconnect/retry.
        return False


@dataclass
class ProbeResult:
    username: str
    create_sent: bool = False
    created: bool = False
    login_sent: bool = False
    authenticated: bool = False
    online_confirmed: bool = False
    map_received: bool = False
    ping_replies: int = 0
    chat_sent: int = 0
    chat_confirmed: int = 0
    replies_sent: int = 0
    moves_sent: int = 0
    movement_note: str = "not_requested"
    logout_ack: bool = False
    disconnect_ack: bool = False
    error: str = ""

    @property
    def passed(self):
        return (not self.error and self.created and self.authenticated
                and self.online_confirmed and self.map_received and self.logout_ack)


def safe_walk_path(payload):
    """A one-unit out/back route on the replicated floor; fail closed.

    Platform precedence follows Map.get_tile_at (last matching platform).
    No audio objects are constructed and no client-side position is invented.
    """
    try:
        origin = tuple(payload[axis] for axis in "xyz")
        world = payload["data"]
        elements = world["elements"]

        def contains(bounds, point):
            return all(type(bounds.get(prefix + axis)) in (int, float)
                       and math.isfinite(bounds[prefix + axis])
                       for axis in "xyz" for prefix in ("min", "max")) and all(
                bounds["min" + axis] <= value <= bounds["max" + axis]
                for axis, value in zip("xyz", point))

        if not all(type(value) in (int, float) and math.isfinite(value) for value in origin):
            return []
        if not isinstance(elements, list) or len(elements) > 20000:
            return []
        platforms = {}
        obstacles = []
        for index, element in enumerate(elements):
            data = element["data"]
            if element["type"] == "platform":
                platforms[data.get("id", index)] = data
            elif element["type"] not in {"zone", "ambience", "music", "reverb", "soundSource"}:
                obstacles.append(data)

        def safe(point):
            if not contains(world, point) or any(contains(obj, point) for obj in obstacles):
                return False
            tile = ""
            for platform in platforms.values():
                if contains(platform, point):
                    tile = platform.get("type", "")
            return isinstance(tile, str) and bool(tile) and not any(
                bad in tile.lower() for bad in ("wall", "water", "lava", "air", "pit", "void", "stair"))

        for dx, dy in ((1, 0), (0, 1), (-1, 0), (0, -1)):
            if all(safe((origin[0] + dx * fraction, origin[1] + dy * fraction, origin[2]))
                   for fraction in (0, 0.25, 0.5, 0.75, 1)):
                target = (origin[0] + dx, origin[1] + dy, origin[2])
                return [target, origin, target, origin]
    except (KeyError, TypeError, ValueError, OverflowError):
        pass
    return []


def reply_for_chat(data, username):
    """Return only fixed text; player messages can never become commands/code."""
    if data.get("buffer") != "chat" or not isinstance(data.get("text"), str):
        return None
    sender, separator, body = data["text"].partition(": ")
    if not separator or sender == username or sender.startswith("BT-smoke-"):
        return None
    if not re.search(r"\bsmoke\b", body, re.IGNORECASE) and username.lower() not in body.lower():
        return None
    return "รับข้อความแล้วครับ ผมเป็นตัวละครทดสอบอัตโนมัติ ทดสอบรับส่งแชตอยู่ และจะออกเองเมื่อครบเวลาครับ"


def run_probe(transport, username, password, hold=60, *, interact=False, clock=time.monotonic,
              report=lambda _message: None):
    """Bounded single-account protocol; injected transport/clock allow offline tests."""
    result = ProbeResult(username)
    map_payload = None
    walking_stopped = False
    interactions_live = False
    next_chat = 0.0
    pending_echoes = set()

    def send_chat(message):
        nonlocal next_chat
        transport.send("chat", {"message": message})
        result.chat_sent += 1
        next_chat = clock() + 10
        pending_echoes.add(username + ": " + message)

    def observe(message, *, logging_out=False):
        nonlocal map_payload, walking_stopped
        if message is None:
            return None
        event, data = message
        if event in {"create_fail", "login_failed", "ban"}:
            raise ProbeError("server_rejected_" + event)
        if event == "quit":
            if logging_out:
                result.logout_ack = True
            else:
                raise ProbeError("server_closed_session")
        elif event == "online" and data.get("username") == username:
            result.online_confirmed = True
        elif event == "parse_map":
            if result.map_received:
                walking_stopped = True
            result.map_received = True
            map_payload = data
        elif event in {"update_map", "rebuild_map", "rebuild", "death", "move"}:
            if event != "move" or data.get("name") == username:
                walking_stopped = True
        elif event == "speak":
            if data.get("buffer") == "chat" and data.get("text") in pending_echoes:
                pending_echoes.remove(data["text"])
                result.chat_confirmed += 1
            elif (interactions_live and not logging_out and result.chat_sent > 0
                  and result.replies_sent < 2 and clock() >= next_chat):
                reply = reply_for_chat(data, username)
                if reply:
                    send_chat(reply)
                    result.replies_sent += 1
                    report("REPLIED to a player mention (message content not logged)")
        elif event == "ping":
            result.ping_replies += 1
        elif event == "connected":
            if data.get("username") != username:
                raise ProbeError("unexpected_authenticated_username")
            result.authenticated = True
        return event

    def wait_for(expected, timeout=15, *, logging_out=False):
        deadline = clock() + timeout
        while clock() < deadline:
            if observe(transport.receive(), logging_out=logging_out) == expected:
                return
        raise ProbeError("timeout_" + expected)

    try:
        if not re.fullmatch(r"BT-smoke-[a-zA-Z0-9_-]{1,16}", username):
            raise ProbeError("invalid_test_username")
        if not isinstance(password, str) or not 16 <= len(password) <= 70:
            raise ProbeError("invalid_test_password")
        if not 1 <= hold <= 120:
            raise ProbeError("hold_must_be_1_to_120_seconds")
        wait_for("transport_connected")
        credentials = {"username": username, "password": password,
                       "version": consts.CLIENT_VERSION, "capabilities": []}
        result.create_sent = True
        transport.send("create", credentials)
        wait_for("create_done")
        result.created = True
        report("CREATED " + username)
        result.login_sent = True
        transport.send("login", credentials)
        wait_for("connected")
        # Login's connected packet can precede add_player() while MOTD is
        # translated. The own-online event confirms that registration finished.
        if not result.online_confirmed:
            deadline = clock() + 15
            while not result.online_confirmed and clock() < deadline:
                observe(transport.receive())
            if not result.online_confirmed:
                raise ProbeError("timeout_own_online_confirmation")
        report("ONLINE " + username + " for " + str(hold) + " seconds")
        interactions_live = interact
        deadline = clock() + hold
        next_ping = clock()
        next_step = clock() + 4
        greeting_at = clock() + 2  # Respect the server's 1.5s chat cooldown.
        path = None
        step_index = 0
        while clock() < deadline:
            if clock() >= next_ping:
                transport.send("ping", {})
                next_ping = clock() + 5
            observe(transport.receive())
            if interact and result.chat_sent == 0 and clock() >= greeting_at:
                send_chat("สวัสดีครับ ผมเป็นตัวละครทดสอบอัตโนมัติ กำลังทดสอบแชตและเดินใกล้จุดเกิด "
                          "พิมพ์ smoke สวัสดี เพื่อทดสอบการตอบกลับ ผมจะออกเองเมื่อครบเวลาครับ")
                report("CHAT greeting sent")
            if interact and clock() >= next_step and step_index < 4:
                if path is None:
                    path = safe_walk_path(map_payload or {})
                if walking_stopped or not path:
                    result.movement_note = "stopped_after_world_change" if walking_stopped else "no_safe_adjacent_floor"
                    step_index = 4
                else:
                    x, y, z = path[step_index]
                    transport.send("move", {"x": x, "y": y, "z": z, "play_sound": True, "mode": "walk"})
                    step_index += 1
                    result.moves_sent += 1
                    result.movement_note = "slow_adjacent_steps_requested"
                    report("WALK step " + str(step_index) + "/4 requested")
                next_step = clock() + 4
        if not result.map_received:
            raise ProbeError("no_map_received")
    except ProbeError as error:
        result.error = str(error)
    except KeyboardInterrupt:
        result.error = "interrupted"
    except Exception:
        # No raw exception/payload: authentication replies contain secret tokens.
        result.error = "transport_or_protocol_error"
    finally:
        if result.login_sent:
            try:
                transport.send("logout", {"message": False})
                wait_for("quit", timeout=3, logging_out=True)
            except Exception:
                if not result.error:
                    result.error = "logout_not_acknowledged"
        try:
            result.disconnect_ack = transport.close()
        except Exception:
            if not result.error:
                result.error = "disconnect_failed"
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true",
                        help="Explicitly allow ONE persistent test account on the release server")
    parser.add_argument("--hold", type=int, default=60, choices=range(1, 121), metavar="1..120")
    parser.add_argument("--interact", action="store_true",
                        help="Allow four safe walking steps, a greeting and two rate-limited fixed chat replies")
    args = parser.parse_args(argv)
    if not args.run:
        print("DRY RUN: no connection or account created. Use --run to authorize one test account.")
        return 0
    try:
        # Read only the build's endpoint, never personal account/settings files.
        config = json.loads((CLIENT_ROOT / "build_server_config.json").read_text(encoding="utf-8"))
        host, port = validate_server_endpoint(config.get("host"), config.get("port"))
        username = "BT-smoke-" + time.strftime("%H%M%S") + "-" + secrets.token_hex(2)
        password = secrets.token_urlsafe(24)
        print("START " + username + "; one account, no retries; interact=" + str(args.interact), flush=True)
        result = run_probe(EnetTransport(host, port), username, password, args.hold,
                           interact=args.interact,
                           report=lambda message: print(message, flush=True))
    except Exception:
        print("FAIL: cannot initialize probe; no credentials or endpoint details were logged.")
        return 1
    output = asdict(result)
    output["result"] = "PASS" if result.passed else "FAIL"
    output["account_remains_on_server"] = True if result.created else ("unknown" if result.create_sent else False)
    if args.interact:
        output["interaction_requests_complete"] = result.chat_confirmed > 0 and result.moves_sent == 4
    print(json.dumps(output, indent=2), flush=True)
    return 0 if result.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
