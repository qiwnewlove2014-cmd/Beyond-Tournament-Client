"""Is this package ready to publish? Ask it, before zipping it.

A release can be wrong in four places, and none of them can be seen from
inside the package:

    python tools/release_preflight.py --pack
        Re-pack the data into the compiled package (the pack is where the
        official endpoint and the backup address live), then check everything
        below. Run this once the client has been compiled and before the
        package is zipped: a ZIP made before this step carries last release's
        data.

    python tools/release_preflight.py
        Check the package as it stands. Nothing is written.

    python tools/release_preflight.py --no-probe
        Skip the four UDP doors (offline, or the server is being restarted).

What it checks, in the order a player meets it:

- **The pack** decrypts, and its embedded endpoint is a name and a port.
- **The backup address is embedded**, and it is an address a player can
  actually reach: `pack_data` refuses to embed a private, loopback or VPN
  answer, and a release that carries none leaves a player whose resolver
  refuses the server's name with no door at all (``libs/login_attempts.py``).
- **The name still means that address**: the endpoint is resolved on this
  machine and through a public resolver (``libs/server_config``), and an answer
  that is not the embedded one means the pack is already stale -- re-pack.
- **Both UDP doors answer**, on the endpoint *and* on each embedded address:
  the walk is ``login_attempts.candidate_addresses``, the same one a login
  takes, because a check that restates the walk is not checking the walk. A
  port that answers with `Client version outdated ... Required: BT-x.y.z` is
  still an answer -- and it is the one line that says what the live server is
  running, which is what the release order depends on.

Two smaller things are reported as they are build-time rules, not runtime
ones: the four player documents the package ships must be byte-for-byte what
``server/docs`` holds now, and the three version locations (``consts.py``,
``version.py``, ``consts.ts``) must agree.

Exit code 1 means do not publish. Every failure names what to do about it.
"""

from __future__ import annotations

import argparse
import importlib.util
import ipaddress
import json
import os
import random
import re
import string
import sys
import time
import zipfile
from pathlib import Path

import enet

CLIENT_ROOT = Path(__file__).resolve().parents[1]
TOOLS_ROOT = Path(__file__).resolve().parent
if str(CLIENT_ROOT) not in sys.path:
    sys.path.insert(0, str(CLIENT_ROOT))

from libs import consts, login_attempts, server_config, vfs
from libs.server_config import ServerConfigError


def _load_tool(name):
    """One of this folder's modules, loaded by path (``tools/`` is not a package)."""
    spec = importlib.util.spec_from_file_location(name, TOOLS_ROOT / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# The packer itself, never a second copy of it: the endpoint's precedence
# (CLI, then environment, then ``build_server_config.json``), the validation of
# that endpoint and the rule about which addresses may be embedded are all
# ``pack_data``'s, and this tool's job is to run it and then ask the result.
pack_data = _load_tool("pack_data")

DEFAULT_PACKAGE = CLIENT_ROOT / "Beyond Tournament"
DEFAULT_CONFIG = CLIENT_ROOT / "build_server_config.json"
DEFAULT_NOTES = CLIENT_ROOT.parent / "server" / "docs"
SERVER_CONSTS = CLIENT_ROOT.parent / "server" / "libs" / "consts.ts"
PACK_NAME = "sounds.dat"
NOTES = ("player_patch_notes.txt", "player_patch_notes_th.txt")

# A probe is not a login: it waits as long as one handshake takes, and it is
# spent on a door rather than on a player's session.
PROBE_TIMEOUT_S = 4.0
# What the server's own words are cut to on one line of this report.
DETAIL_LIMIT = 160


def _short(path):
    """A path as short as it can be said: relative to the client, else its name.

    The report is read on a console whose codepage is not UTF-8, and a checkout
    can sit under a folder name that is not ASCII at all -- printing the short
    form is what keeps the line readable there.
    """
    try:
        return str(Path(path).relative_to(CLIENT_ROOT))
    except ValueError:
        return Path(path).name


def _ascii(text, limit=DETAIL_LIMIT):
    """One short line of server words, safe for any console codepage."""
    text = " ".join(str(text).split())
    if len(text) > limit:
        text = text[: limit - 1] + "\u2026"
    return text.encode("ascii", "replace").decode("ascii")


def read_embedded(pack_path):
    """The endpoint a pack carries (``.bt/server_endpoint.json``), decrypted.

    This is what a released Client reads before it can talk to anybody
    (``vfs.get_embedded_server_config``), so it is what decides where the game
    points -- and the reason the backup address works at all.
    """
    try:
        with zipfile.ZipFile(pack_path) as archive:
            raw = btx_decrypt_member(archive, vfs.SERVER_CONFIG_MEMBER)
    except (OSError, KeyError, zipfile.BadZipFile) as error:
        raise ServerConfigError(f"{pack_path} carries no readable endpoint: {error}")
    try:
        embedded = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ServerConfigError(f"{pack_path} has an unreadable endpoint: {error}")
    if not isinstance(embedded, dict):
        raise ServerConfigError(f"{pack_path} has an endpoint that is not an object")
    return embedded


def pack_size(pack_path):
    """``(data members, bytes)`` of a pack, or ``(None, None)`` when it will not read.

    The count is the packer's own (``.bt/pack.json``), so it is the number of
    game files the pack holds rather than the two bookkeeping members the
    archive also carries -- a release whose data never changed should report
    the same figure it did last time.
    """
    try:
        with zipfile.ZipFile(pack_path) as archive:
            size = Path(pack_path).stat().st_size
            try:
                meta = json.loads(btx_decrypt_member(archive, vfs.PACK_META_MEMBER))
                reported = int(meta.get("members"))
            except (KeyError, ValueError, TypeError, UnicodeError, json.JSONDecodeError):
                reported = len(archive.infolist())
            return reported, size
    except (OSError, zipfile.BadZipFile):
        return None, None


def btx_decrypt_member(archive, name):
    """One member of an open archive, decrypted (the pack's own reader)."""
    return vfs.btx_decrypt(archive.read(name))


def compiled_version(package, version=consts.CLIENT_VERSION):
    """``(ok, text)`` for the executable the package would ship.

    The compiled game carries its version string once -- it is the constant a
    login sends, and the one ``auth_handler`` refuses a mismatch on -- so
    asking the executable is the only way to know, without starting it, that
    the ZIP is being made from code of *this* version. A package compiled
    before the bump looks finished in every other way and would be refused by
    the new server, which is the worst released state there is.
    """
    exe = Path(package) / "Beyond Tournament.exe"
    wanted = str(version)
    try:
        data = exe.read_bytes()
    except OSError as error:
        return False, f"{_short(exe)} cannot be read ({error}) -- compile the client first"
    if wanted.encode("ascii") not in data:
        return False, (
            f"{_short(exe)} does not carry {wanted} -- it was compiled before this "
            "version's bump, so re-compile the client before making the ZIP"
        )
    return True, f"{_short(exe)} carries {wanted}"


def embedded_problems(embedded):
    """Everything wrong with the endpoint a pack carries, as sentences.

    An empty list is the answer a release needs. The backup rule is
    ``pack_data``'s own (a literal that is not IPv4 is refused, and a private,
    loopback or VPN answer is never embedded because it is the packer's own
    network and no player can reach it), asked here with the same helpers so
    the two can never disagree about what a usable backup is.
    """

    problems = []
    try:
        host, port = server_config.validate_server_endpoint(
            embedded.get("host"), embedded.get("port")
        )
    except ServerConfigError as error:
        return [f"the embedded endpoint is not usable: {error}"]

    entries = embedded.get(server_config.FALLBACK_ADDRESS_KEY)
    if isinstance(entries, str):
        entries = (entries,)
    if not isinstance(entries, (list, tuple)) or not entries:
        problems.append(
            "no backup address is embedded -- a player whose resolver refuses "
            f"{host} has no door without DNS. Re-pack with --pack "
            "(or pass --server-address / BT_SERVER_ADDRESS)."
        )
        return problems

    for entry in entries:
        try:
            address = server_config.validate_server_host(entry)
        except ServerConfigError as error:
            problems.append(f"backup address {entry!r} is unusable: {error}")
            continue
        literal = pack_data._as_ipv4_literal(address)
        if literal is False:
            problems.append(f"backup address {address} is not IPv4 (this transport has none)")
        elif literal and not ipaddress.ip_address(address).is_global:
            problems.append(
                f"backup address {address} is not a public address -- a player "
                "cannot reach it. Re-pack from a machine whose resolver answers "
                "with the server's public address."
            )
    return problems


def ddns_answers(host):
    """``[(label, address or None), ...]`` for the endpoint's name.

    Two answers rather than one, because they are two different failures: this
    machine's resolver is what a player's resolver usually looks like, and a
    public resolver over HTTPS is what the Client falls back to when it is not
    (``server_config.resolve_host_via_doh``, new in 1.8.8).
    """
    local = server_config.resolve_host_with_source(host, public_resolver=False)[0]
    public = server_config.resolve_host_via_doh(host)
    return [("local", local), ("public", public)]


def ddns_problems(embedded, answers):
    """Whether the name still means the address the pack embedded."""

    host = str(embedded.get("host"))
    entries = embedded.get(server_config.FALLBACK_ADDRESS_KEY)
    if isinstance(entries, str):
        entries = (entries,)
    embedded_addresses = {
        str(entry).strip().lower() for entry in (entries or ()) if str(entry).strip()
    }

    problems = []
    answered = False
    for label, address in answers:
        if address is None:
            continue
        answered = True
        if address.strip().lower() not in embedded_addresses:
            problems.append(
                f"the {label} answer for {host} is {address}, which the pack does "
                f"not carry ({', '.join(sorted(embedded_addresses)) or 'nothing'}) "
                "-- the pack is older than the server's address. Re-pack with "
                "--pack and make the ZIP after it."
            )
    if not answered:
        problems.append(
            f"{host} has no answer on this machine and none from a public "
            "resolver -- a player will need the embedded backup address to get "
            "in at all. Check the DDNS update, then re-pack."
        )
    return problems


def probe_door(target, port, *, timeout=PROBE_TIMEOUT_S, version=consts.CLIENT_VERSION,
               clock=time.monotonic):
    """Knock on one ``(target, port)``: ``'reply'``, ``'handshake'`` or ``'silent'``.

    A datagram that comes back is the whole test: nothing but the host we
    connected to can answer on this socket. Two grades are kept apart because
    they prove different things -- an ENet handshake means a listener is on
    that UDP port, and an application reply on the MISC channel means the
    game's own event loop took the packet and said something, which is also
    where a version mismatch is reported.

    The login packet is the shipped shape with an account that cannot exist:
    this is a knock, not a session, and it must never be a real account's.
    """
    result = {"target": target, "port": int(port), "answer": "silent", "detail": ""}
    net = enet.Host(None, 1, 256, 0, 0)
    peer = net.connect(enet.Address(str(target).encode("ascii"), int(port)), 256)
    probe_user = "__preflight_" + "".join(random.choices(string.ascii_lowercase, k=10))
    payload = json.dumps(
        {
            "event": "login",
            "data": {"username": probe_user, "password": "x", "version": version},
        }
    ).encode("utf-8")
    deadline = clock() + timeout
    try:
        while clock() < deadline:
            event = net.service(0)
            if event.type == enet.EVENT_TYPE_CONNECT:
                if result["answer"] == "silent":
                    result["answer"] = "handshake"
                peer.send(
                    consts.CHANNEL_MISC,
                    enet.Packet(payload, flags=enet.PACKET_FLAG_RELIABLE),
                )
            elif event.type == enet.EVENT_TYPE_RECEIVE:
                result["answer"] = "reply"
                result["detail"] = _reply_detail(event.packet.data)
                break
            time.sleep(0.002)
    finally:
        try:
            peer.disconnect()
            net.service(0)
            net.flush()
        except Exception:
            pass
    return result


def _reply_detail(data):
    """One line of what the server said, or a note that it was not JSON."""
    try:
        message = json.loads(bytes(data))
    except Exception:
        return "binary packet"
    if not isinstance(message, dict):
        return "non-object packet"
    event = str(message.get("event", "?"))
    detail = message.get("data")
    words = ""
    if isinstance(detail, dict):
        words = detail.get("message") or ""
    return _ascii(f"{event}: {words}" if words else event)


def doors(host, port, addresses=()):
    """The ``(target, port)`` pairs a login on this pack would walk through.

    ``login_attempts.candidate_addresses`` is the shipped walk -- the name
    first, then each backup, each over both ports -- so this list is what a
    player gets, not a second opinion about it.
    """
    return login_attempts.candidate_addresses(host, port, addresses)


def door_label(pair, host):
    """``name:port (backup)``-style label for one door."""
    target, port = pair
    kind = "backup" if login_attempts.is_backup(pair, host) else "endpoint"
    return f"{target}:{port} ({kind})"


def versions_line():
    """``(ok, text)`` for the three version locations."""

    client = str(consts.CLIENT_VERSION)
    try:
        from libs import version as version_module

        described = str(version_module.version)
    except Exception as error:  # pragma: no cover - import machinery, not logic
        return False, f"could not read libs/version.py: {error}"
    semver = described.split("+")[0].lstrip("v")
    try:
        server_text = SERVER_CONSTS.read_text(encoding="utf-8")
    except OSError as error:
        return True, f"server/libs/consts.ts not readable here ({error}); client is {client}"
    match = re.search(r'SERVER_VERSION\s*[:=]\s*"([^"]+)"', server_text)
    server = match.group(1) if match else None
    same_client = client.startswith("BT-") and client[3:] == semver
    same_server = server == client
    ok = bool(same_client and same_server)
    text = f"client {client}, version.py {semver}, server {server or 'not found'}"
    if not same_client:
        text += " -- consts.py and version.py disagree (the updater compares version.py)"
    if not same_server:
        text += " -- consts.ts and consts.py disagree (auth_handler rejects a mismatch)"
    return ok, text


def notes_problems(package, notes_source):
    """Player documents that are not what ``server/docs`` holds now."""

    if not notes_source.is_dir():
        return [], f"{notes_source} is not here; notes not compared"
    problems = []
    for name in NOTES:
        source = notes_source / name
        shipped = package / name
        if not source.is_file():
            problems.append(f"{source} is missing -- the build copies it into the package")
            continue
        if not shipped.is_file():
            problems.append(f"{shipped} is missing from the package")
            continue
        if shipped.read_bytes() != source.read_bytes():
            problems.append(
                f"{name} in the package differs from server/docs/{name} -- the "
                "package was assembled before the notes were finished. Re-build "
                "or copy the notes, then make the ZIP."
            )
    return problems, ""


def compare_packs(old_path, new_path):
    """``(added, removed, changed)`` member names, by decrypted content.

    A pack is re-encrypted with fresh nonces on every run, so the two files
    never look alike byte-for-byte; comparing what they *mean* is the only way
    to say whether a re-pack actually moved anything.
    """

    def contents(path):
        with zipfile.ZipFile(path) as archive:
            return {item.filename: btx_decrypt_member(archive, item.filename)
                    for item in archive.infolist()}

    old, new = contents(old_path), contents(new_path)
    added = sorted(set(new) - set(old))
    removed = sorted(set(old) - set(new))
    changed = sorted(name for name in set(old) & set(new) if old[name] != new[name])
    return added, removed, changed


def repack(package, *, data_dir, config_path, log=print):
    """Pack the data into *package*, through ``pack_data`` and nothing else.

    Assembled beside the package and moved into place only after it reads back
    with a usable endpoint, which is the same order the real build uses: a pack
    that fails its own check leaves the package exactly as it was.
    """
    resident = Path(package) / PACK_NAME
    staged = resident.with_name(resident.name + ".new")
    staged.unlink(missing_ok=True)
    pack_data.main(
        [
            "--data-dir", str(data_dir),
            "--output", str(staged),
            "--server-config", str(config_path),
        ]
    )
    embedded = read_embedded(staged)
    problems = embedded_problems(embedded)
    if problems:
        log(f"[PACK] refusing to promote {staged.name}: " + "; ".join(problems))
        return None, problems

    if resident.is_file():
        added, removed, changed = compare_packs(resident, staged)
        if added or removed or changed:
            log(
                "[PACK] the package's data was packed before these changed: "
                f"{len(changed)} file(s) changed, {len(added)} added, "
                f"{len(removed)} removed"
            )
            for name in (changed + added + removed)[:8]:
                log(f"       {name}")
        else:
            log("[PACK] same data as the package already had (only the endpoint refreshed)")
    try:
        os.replace(staged, resident)
    except OSError as error:
        # A running client holds the pack open: the new one is kept beside it
        # rather than the release silently keeping the old endpoint.
        log(f"[PACK] could not replace {resident.name} ({error}); close the running "
            f"client and rename {staged.name} over it")
        return None, [f"{resident.name} is locked by a running process"]
    return resident, []


def collect(package=DEFAULT_PACKAGE, notes_source=DEFAULT_NOTES, *, do_probe=True,
            probe=probe_door, timeout=PROBE_TIMEOUT_S):
    """Run every check: ``(lines, problems)``."""
    lines = []
    problems = []
    pack_path = Path(package) / PACK_NAME

    if not Path(package).is_dir():
        return lines, [f"{package} is not here -- compile the client first"]
    if not pack_path.is_file():
        return lines, [f"{pack_path} is missing -- run with --pack"]

    members, size = pack_size(pack_path)
    lines.append(f"[PACK] {_short(pack_path)}  {members} data members  {size} bytes")

    compiled_ok, compiled_text = compiled_version(package)
    lines.append(f"[BUILD] {compiled_text}  {'OK' if compiled_ok else 'STALE'}")
    if not compiled_ok:
        problems.append(compiled_text)

    if members is None:
        return lines, [f"{pack_path} is not a readable pack"]

    try:
        embedded = read_embedded(pack_path)
    except ServerConfigError as error:
        return lines, [str(error)]
    host, port = embedded.get("host"), embedded.get("port")
    backups = embedded.get(server_config.FALLBACK_ADDRESS_KEY) or []
    lines.append(f"[ENDPOINT] host {host}  port {port}  backup {', '.join(map(str, backups)) or 'none'}")

    endpoint_problems = embedded_problems(embedded)
    problems.extend(endpoint_problems)

    if not endpoint_problems:
        answers = ddns_answers(str(host))
        described = ", ".join(
            f"{label} {address if address else 'no answer'}" for label, address in answers
        )
        stale = ddns_problems(embedded, answers)
        lines.append(f"[DDNS] {host} -> {described}  {'STALE' if stale else 'MATCH'}")
        problems.extend(stale)

        notes, note = notes_problems(Path(package), Path(notes_source))
        if note:
            lines.append(f"[NOTES] {note}")
        else:
            lines.append("[NOTES] the package's player documents match server/docs "
                         f"{'OK' if not notes else 'STALE'}")
        problems.extend(notes)

        ok, text = versions_line()
        lines.append(f"[VERSIONS] {text}  {'OK' if ok else 'MISMATCH'}")
        if not ok:
            problems.append("the three version locations do not agree")

        if do_probe:
            for pair in doors(str(host), port, backups):
                label = door_label(pair, str(host))
                target, target_port = pair
                address = server_config.resolve_host(str(target))
                if address is None:
                    lines.append(f"[DOOR] {label}  NO DNS ANSWER -- this door cannot be walked")
                    problems.append(f"{label} has no DNS answer on this machine")
                    continue
                result = probe(str(address), target_port, timeout=timeout)
                detail = f" -- {result['detail']}" if result["detail"] else ""
                lines.append(f"[DOOR] {label} -> {address}  {result['answer'].upper()}{detail}")
                if result["answer"] == "silent":
                    problems.append(
                        f"{label} did not answer within {timeout:.0f}s -- the player "
                        "facing this door gets silence. Check the server is up, the "
                        "port is forwarded and nothing filters it."
                    )
        else:
            lines.append("[DOOR] probing skipped (--no-probe)")

    return lines, problems


def _parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--package", default=str(DEFAULT_PACKAGE),
                        help="the compiled package folder (default: Beyond Tournament)")
    parser.add_argument("--notes", default=str(DEFAULT_NOTES),
                        help="where server/docs is, for the notes comparison")
    parser.add_argument("--pack", action="store_true",
                        help="re-pack the data into the package before checking")
    parser.add_argument("--data-dir", default=str(CLIENT_ROOT / "data"),
                        help="the source data folder to pack")
    parser.add_argument("--server-config", default=str(DEFAULT_CONFIG),
                        help="build_server_config.json (host, port, addresses)")
    parser.add_argument("--no-probe", action="store_true",
                        help="skip the UDP doors")
    parser.add_argument("--timeout", type=float, default=PROBE_TIMEOUT_S,
                        help="seconds to wait on each door")
    return parser.parse_args(argv)


def main(argv=None):
    args = _parse_args(argv)
    if args.pack:
        _packed, refusing = repack(
            args.package, data_dir=args.data_dir, config_path=args.server_config
        )
        if refusing:
            print("")
            print(f"RESULT: NOT READY ({len(refusing)} problem(s))")
            for problem in refusing:
                print(f"  - {problem}")
            return 1

    lines, problems = collect(
        args.package, args.notes, do_probe=not args.no_probe, timeout=args.timeout
    )
    for line in lines:
        print(line)
    print("")
    if problems:
        print(f"RESULT: NOT READY ({len(problems)} problem(s))")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    print("RESULT: READY TO PUBLISH -- make the ZIP from this package now, "
          "then upload it before the server is restarted.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
