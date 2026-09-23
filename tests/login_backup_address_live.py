"""A player whose resolver cannot answer: which door does the login use?

The reason the build embeds a backup address beside the server's name is a
machine that cannot look that name up -- a filtering or family DNS, an
ad-blocking resolver, a router that blocks the name -- and the only honest way
to ask whether the login survives it is to make one such player out of this
machine and watch a real socket open.

What is real here:

* the name is one no resolver may answer (``.invalid``, RFC 6761), and the
  lookup goes to this machine's own resolver;
* the endpoint and the backup address come through the release readers
  (``server_config.get_server_endpoint`` / ``get_fallback_addresses``) out of
  a real ``dev_config.json`` -- the same two keys the pack carries;
* the walk is ``libs/login_attempts.py``'s own and the flow is ``libs/game.py``'s
  own: ``login`` -> ``_open_first_login_attempt`` -> ``_open_candidate`` ->
  ``login2`` -> ``_retry_login`` -> ``connection_error``;
* the transport is the real ``networking.Client`` -- a real thread, a real ENet
  host, a real UDP socket -- and the door it opens on is a real ENet server
  (the same library) bound to the backup address;
* the log line is written by the real ``logger.log`` into a real file and read
  back off the disk, and the console the player's helper reads is captured too.

What is the rig's half, and deliberately so:

* the server is a listener, not the game's server: it proves the transport
  connected on the backup address and that the login request arrived there.
  Whether the account is accepted is the Server's own answer, and the
  connection log is where a real login reads it;
* the event handler is a stand-in. The real one builds a whole Gameplay; the
  rig's server sends no game packet, so it is never used;
* the clock is the rig's: that a silent door really costs ``consts.TIMEOUT`` is
  the code's fact, not something to sit through four times, so the rig decides
  when a door has been silent that long instead of waiting it out. Nothing else
  about the walk changes.

Run it: ``python tests/login_backup_address_live.py`` prints the transcript for
both players. ``test_login_backup_address_live.py`` is the gate on top.
"""

import contextlib
import io
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
from unittest import mock

import enet

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from libs import clock, consts, event_handeler, game as game_module, logger
from libs import login_attempts, options, server_config


# A name no resolver may answer (RFC 6761). The reservation is the point: if a
# machine's DNS answers this, the rig says so rather than pretending.
UNANSWERABLE_NAME = "bt-login-test.invalid"
BACKUP_ADDRESS = "127.0.0.1"
PLAYER_NAME = "TestPlayer"
PLAYER_PASSWORD = "an-unremarkable-password"

# How long the rig waits for a real handshake, and for a sent packet to arrive.
WAIT_S = 4.0


def live_lookup(name):
    """What this machine's resolver says about *name*: an address, or None."""

    try:
        answers = socket.getaddrinfo(name, None, socket.AF_INET, socket.SOCK_DGRAM)
    except OSError:
        return None
    return answers[0][4][0] if answers else None


@contextlib.contextmanager
def resolver():
    """Make the name unanswerable, and say whose failure it was.

    On a machine whose resolver is honest -- the normal case -- a reserved name
    has no answer and nothing is patched: the failure the player is having is
    this machine's own. A machine that answers a reserved name (a hijacking
    DNS, an ISP landing page) has no unanswerable name to offer, so the rig
    injects the one failure the reservation stands for and says which it did.
    """

    answered = live_lookup(UNANSWERABLE_NAME)
    if answered is None:
        yield "this machine's resolver (no answer, live)"
        return
    real_lookup = socket.getaddrinfo

    def hijacked(name, *args, **kwargs):
        if str(name).lower() == UNANSWERABLE_NAME:
            raise socket.gaierror(-2, "Name or service not known")
        return real_lookup(name, *args, **kwargs)

    with mock.patch.object(socket, "getaddrinfo", hijacked):
        yield f"injected (this machine answered {answered} for a reserved name)"


class LiveServer(threading.Thread):
    """A real ENet door on the backup address, and everything it saw."""

    def __init__(self):
        super().__init__(daemon=True)
        self.host = enet.Host(enet.Address(BACKUP_ADDRESS.encode(), 0), 8, 256, 0, 0)
        self.port = self.host.address.port
        self.connects = []  # the address of every peer that completed the handshake
        self.packets = []  # (channel, bytes) exactly as they arrived
        self.disconnects = 0
        self._done = threading.Event()

    def run(self):
        while not self._done.is_set():
            event = self.host.service(2)
            if event.type == enet.EVENT_TYPE_CONNECT:
                host = event.peer.address.host
                if isinstance(host, bytes):
                    host = host.decode("ascii", "replace")
                self.connects.append((host, event.peer.address.port))
            elif event.type == enet.EVENT_TYPE_RECEIVE:
                self.packets.append((event.channelID, bytes(event.packet.data)))
            elif event.type == enet.EVENT_TYPE_DISCONNECT:
                self.disconnects += 1

    def wait_for_packet(self, seconds=WAIT_S):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if self.packets:
                return True
            time.sleep(0.005)
        return False

    def stop(self):
        self._done.set()
        self.join(timeout=2)


class StubEventHandler:
    """What ``networking.Client`` is handed, and never uses.

    The real handler builds a whole Gameplay and a Ticket desk out of the Game;
    the rig's server never sends a game packet, so building one would be work
    whose only outcome is an object nobody asks a question.
    """

    def __init__(self, client, game):
        self.client = client
        self.game = game


class PlayerSession:
    """One player's login, driven the way the frame loop drives it.

    Every method below is ``game.Game``'s own code (a stub class with the real
    functions bound to it, the way ``test_login_attempts.py`` does it). What the
    rig adds is only what a Game carries *around* a login: a stack to replace
    states on, a clock factory, the frame lock, and somewhere for a failure to
    land.
    """

    _new_network_client = game_module.Game._new_network_client
    _close_network = game_module.Game._close_network
    login = game_module.Game.login
    login2 = game_module.Game.login2
    _open_first_login_attempt = game_module.Game._open_first_login_attempt
    _open_candidate = game_module.Game._open_candidate
    _silence_report = game_module.Game._silence_report
    _login_walk = game_module.Game._login_walk
    _login_failure_words = game_module.Game._login_failure_words
    _report_login_failure = game_module.Game._report_login_failure
    _report_login_reached = game_module.Game._report_login_reached
    _retry_login = game_module.Game._retry_login
    _remember_login_port = game_module.Game._remember_login_port
    _connection_failure_message = game_module.Game._connection_failure_message
    connection_error = game_module.Game.connection_error
    pop = game_module.Game.pop
    append = game_module.Game.append
    replace = game_module.Game.replace

    def __init__(self):
        self.stack = []
        self.clocks = set()
        self.lock = threading.RLock()
        self.network = None
        self.queued = []
        self.back_at_menu = False
        self.left_at = []

    def new_clock(self):
        made = clock.Clock()
        self.clocks.add(made)
        return made

    def put(self, value):
        # The real Game services this queue on the frame (a disconnect, a
        # timeout raised by the worker, an instruction for that worker). A rig
        # has no frame, so it keeps what the worker said and reads it after.
        self.queued.append(value)

    def disconnected(self):
        self.left_at.append("disconnected")


def _endpoint_reader(reader, config_path):
    """The release reader, on a real config file: host and port out of the pack.

    ``reader`` is the real function taken *before* the patch below, so the
    reader reads the real thing while the game reads the reader.
    """

    return reader(
        argv=[],
        dev_config_path=config_path,
        settings_getter=lambda key, default: default,
    )


def _address_reader(reader, config_path):
    """The release reader for the addresses embedded beside that endpoint."""

    return reader(dev_config_path=config_path)


def _decode(packets):
    """The datagrams as the game would read them: a JSON envelope per event."""

    decoded = []
    for channel, data in packets:
        try:
            decoded.append((channel, json.loads(data.decode("utf-8"))))
        except (UnicodeDecodeError, json.JSONDecodeError):
            decoded.append((channel, None))
    return decoded


def _pump_until_seen(client, server, seconds=WAIT_S):
    """Service the transport until the server has what it is waiting for.

    The worker sends the login packet; the host reaches the wire when the frame
    services it (``Client.loop`` does that in the game). The rig is the frame.
    """

    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        with contextlib.suppress(OSError):
            client.net.service(0)
        if server is None or server.packets:
            return
        time.sleep(0.005)


def play(*, port, server=None, silence=False, backup=BACKUP_ADDRESS, public_answer=None):
    """One player's login through the backup door, and everything it left.

    ``server`` is the real door to open (None means nothing is listening, the
    case where the login has nowhere to go). ``silence`` decides whether the
    rig waits a door's silence out or simply decides it has lasted
    ``consts.TIMEOUT``; the words and the walk are the same either way.
    ``backup`` is the address the pack carries, and ``public_answer`` is what
    the public resolver says about the name -- the rig's stand-in for the
    internet, which answers nothing unless a scenario says otherwise.
    """

    folder = tempfile.mkdtemp(prefix="bt_login_live_")
    try:
        asked_public = []

        def public_lookup(host, **rest):
            # The rig's stand-in for the internet. The endpoint name is reserved
            # (no resolver may answer it), so a real public resolver over HTTPS
            # would answer nothing here either -- this only makes that
            # deterministic and keeps the suite off the network. That the call
            # happens at all is part of what is being watched, so it is recorded.
            asked_public.append(host)
            return public_answer
        config_path = os.path.join(folder, "dev_config.json")
        with open(config_path, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "host": UNANSWERABLE_NAME,
                    "port": port,
                    "addresses": list(backup) if isinstance(backup, (list, tuple)) else [backup],
                },
                handle,
            )
        log_path = os.path.join(folder, "client_debug.log")
        player = PlayerSession()
        said = []
        chosen = []
        # Taken before the patch: what the game calls is the release reader,
        # and what the release reader calls is this.
        real_endpoint = server_config.get_server_endpoint
        real_addresses = server_config.get_fallback_addresses

        def back_at_menu(game):
            player.back_at_menu = True

        with resolver() as resolver_words, mock.patch.object(
            event_handeler, "EventHandeler", StubEventHandler
        ), mock.patch.object(
            server_config, "resolve_host_via_doh", public_lookup
        ), mock.patch.object(
            game_module, "speak", lambda text, *rest: said.append(text)
        ), mock.patch.object(
            game_module.menus, "main_menu", back_at_menu
        ), mock.patch.object(
            options,
            "get",
            lambda key, default=None: {
                "username": PLAYER_NAME,
                "password": PLAYER_PASSWORD,
            }.get(key, default),
        ), mock.patch.object(
            options, "set", lambda key, value: chosen.append((key, value))
        ), mock.patch.object(
            options, "get_login_port", lambda: None
        ), mock.patch.object(
            options, "set_login_port", lambda value: chosen.append(("login_port", value))
        ), mock.patch.object(
            server_config,
            "get_server_endpoint",
            lambda **rest: _endpoint_reader(real_endpoint, config_path),
        ), mock.patch.object(
            server_config,
            "get_fallback_addresses",
            lambda **rest: _address_reader(real_addresses, config_path),
        ):
            # The log is a real file written by the real logger; the handle the
            # module keeps open for the process is swapped for this one, so the
            # player's own client_debug.log is never touched.
            with open(log_path, "w", encoding="utf-8") as log_handle, mock.patch.object(
                logger, "LOG_FILE", log_path
            ), mock.patch.object(logger, "_LOG_HANDLE", log_handle):
                console = io.StringIO()
                with contextlib.redirect_stdout(console):
                    game_module.Game.login(player)
                    opened = (
                        getattr(player.network, "resolved_host", None),
                        getattr(player, "_login_port", None),
                    )
                    _drive(player, silence=silence)
                    client = player.network
                    if client is not None:
                        _pump_until_seen(client, server)
                        client.close_socket()
                log_handle.flush()
                console_text = console.getvalue()

        with open(log_path, "r", encoding="utf-8") as handle:
            logged = handle.read()

        facts = player._login_walk()
        dialled = list(facts["dialled"])
        return {
            "resolver": resolver_words,
            "public_resolver_asked": list(asked_public),
            "public_answer": public_answer,
            "log_path": log_path,
            "endpoint": UNANSWERABLE_NAME,
            "backup": backup,
            "port": port,
            "said": said,
            "chosen": chosen,
            "opened_host": opened[0],
            "opened_port": opened[1],
            "dialled": dialled,
            "answered": dialled[-1] if dialled else None,
            "unresolvable": sorted(facts["unresolvable"]),
            "lookup_failed": facts["lookup_failed"],
            "backup_tried": facts["backup"],
            "back_at_menu": player.back_at_menu,
            "log_text": logged,
            "log_lines": [line for line in logged.splitlines() if "[LOGIN]" in line],
            "console_lines": [line for line in console_text.splitlines() if "[LOGIN]" in line],
            "server_port": getattr(server, "port", None),
            "server_connects": list(getattr(server, "connects", ())),
            "server_packets": _decode(getattr(server, "packets", ())),
            "server_disconnects": getattr(server, "disconnects", 0),
        }
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def _drive(player, *, silence):
    """Run the login states the way the game's frame runs them.

    Stops when the handshake has landed (the stack holds ``Client.loop``, which
    is what the next frame would call), or when the login ended -- back at the
    menu, with nothing left to dial.
    """

    deadline = time.monotonic() + WAIT_S
    while True:
        if player.back_at_menu or player.network is None or not player.stack:
            return
        state = player.stack[-1]
        if getattr(state, "__name__", "") == "loop":
            return
        if silence:
            # A door that is silent is silent for ``consts.TIMEOUT``; the rig
            # decides that has happened rather than sitting through it.
            client = player.network
            if client is not None and not client.logged_in:
                client.timeout_clock.elapsed = consts.TIMEOUT
        state()
        if time.monotonic() > deadline:
            return
        time.sleep(0.002)


def play_a_login_that_gets_in():
    """A real door on the backup address, and a player who reaches it."""

    server = LiveServer()
    server.start()
    try:
        # The player's own network answers 127.0.0.1 as it always has; it is
        # the *name* this machine cannot answer, which is the whole failure.
        player = play(port=server.port, server=server)
        player["silence_forced"] = False
        return player
    finally:
        server.stop()


def play_a_login_that_gets_nothing():
    """The same walk with nobody behind either door: what is the player told?"""

    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.bind((BACKUP_ADDRESS, 0))
        port = probe.getsockname()[1]
    # Bound and closed, so nothing is listening on either door of the backup.
    player = play(port=port, silence=True)
    player["silence_forced"] = True
    return player


def play_a_login_that_only_the_public_resolver_can_reach():
    """A machine whose own DNS refuses the name, saved by a public resolver.

    The name is answered over HTTPS instead, and that answer is the address of
    the real door standing here -- so this proves the *whole* new path at once:
    the name is asked for, the public resolver's answer is what is dialled, the
    transport opens on it, and the address the pack carries is not needed. The
    packed address this scenario is given is a reserved one (TEST-NET-1) that
    nothing can be listening on, so a walk that fell through to it would show.
    """

    server = LiveServer()
    server.start()
    try:
        player = play(
            port=server.port,
            server=server,
            backup="192.0.2.1",
            public_answer=BACKUP_ADDRESS,
        )
        player["silence_forced"] = False
        return player
    finally:
        server.stop()


def transcript(report):
    """The report as a person reads it: the doors, the words, the log line."""

    lines = []
    lines.append("resolver          " + report["resolver"])
    if report["public_resolver_asked"]:
        answered = (
            "answered " + report["public_answer"]
            if report["public_answer"]
            else "answered nothing (the rig's own stand-in for the internet)"
        )
        lines.append(
            "public resolver   asked for {} over HTTPS, {}".format(
                ", ".join(report["public_resolver_asked"]), answered
            )
        )
    lines.append(
        "endpoint name     {}  (the release build carries a backup address beside it)".format(
            report["endpoint"]
        )
    )
    packed = report["backup"] if isinstance(report["backup"], str) else ", ".join(report["backup"])
    lines.append(
        "backup address    {}:{}  (what the pack carries beside the name)".format(
            packed, report["port"]
        )
    )
    lines.append("")
    opened = (report["opened_host"], report["opened_port"])
    for host, port in report["dialled"]:
        door = "{}:{}".format(host, port)
        mark = "  <- the login opened its transport here" if (host, port) == opened else ""
        lines.append("  dialled         " + door + mark)
    if not report["dialled"]:
        lines.append("  dialled         nothing: no door could be opened")
    lines.append("")
    lines.append("  heard (in order):")
    for sentence in report["said"]:
        lines.append("    " + sentence)
    lines.append("")
    lines.append("  log file:")
    for line in report["log_lines"] or ["    (no [LOGIN] line was written)"]:
        lines.append("    " + line)
    lines.append("")
    if report["server_port"] is not None:
        connects = ", ".join(
            "{}:{}".format(host, port) for host, port in report["server_connects"]
        ) or "none"
        lines.append("  the server saw: connects [{}]".format(connects))
        for channel, packet in report["server_packets"]:
            lines.append(
                "    packet on channel {}: {}".format(channel, json.dumps(packet))
            )
    else:
        lines.append("  the server saw: nothing (no door had anything behind it)")
    return "\n".join(lines)


if __name__ == "__main__":
    for title, scenario in (
        ("A player who gets in through the backup address", play_a_login_that_gets_in),
        ("A player whose backup address answers nothing either", play_a_login_that_gets_nothing),
    ):
        print("=" * 78)
        print(title)
        print("=" * 78)
        print(transcript(scenario()))
        print("")
