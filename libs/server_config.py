"""Resolve the game server endpoint without exposing production overrides.

Source builds remain flexible for local development.  Compiled builds only
trust the endpoint embedded in the encrypted VFS package by the release build.
"""

from __future__ import annotations

import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import sys
import time
from typing import Callable, Mapping, Sequence

from . import consts


DEV_CONFIG_FILENAME = "dev_config.json"
ENDPOINT_OPTION_KEYS = frozenset(("host", "port"))
# Where the release build keeps the addresses it resolved for the endpoint.
# Read, never written, by the client, and never an Option for the same reason
# the endpoint is not one.
FALLBACK_ADDRESS_KEY = "addresses"


class ServerConfigError(ValueError):
    """Raised when no safe, valid server endpoint can be resolved."""


class NameLookupError(OSError):
    """The server's name could not be looked up on this machine.

    An ``OSError`` on purpose: a name with no answer and a socket that will not
    open are one *kind* of failure to every handler that already exists here
    ("this attempt did not open, try the next one"), and only the sentence the
    player hears -- and the line behind it -- has to tell them apart. A
    filtering or family resolver, an ad-blocking DNS or a router that blocks the
    name all land here, and a name cannot be retried into working, which is why
    a login may fall back to an address instead (``get_fallback_addresses``).
    """


def is_production_build() -> bool:
    """Return True for frozen/Nuitka release builds, False for source runs."""

    return bool(getattr(sys, "frozen", False) or "__compiled__" in globals())


def validate_server_host(host: object) -> str:
    """Validate and normalize a bare hostname or IP address.

    One rule for every host this client is ever handed -- the endpoint, and a
    backup address a release build embedded beside it -- because a backup that
    is validated differently from the endpoint is a second door with a second
    set of locks.
    """

    if not isinstance(host, str):
        raise ServerConfigError("The server hostname must be text.")
    host = host.strip()
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    if not host or len(host) > 253:
        raise ServerConfigError("The server hostname is missing or too long.")
    if any(character.isspace() for character in host):
        raise ServerConfigError("The server hostname cannot contain whitespace.")
    if any(token in host for token in ("://", "/", "\\", "@", "\x00")):
        raise ServerConfigError("Enter only a hostname or IP address, not a URL.")

    try:
        return str(ipaddress.ip_address(host))
    except ValueError:
        pass
    host = host.rstrip(".")
    try:
        normalized_host = host.encode("idna").decode("ascii")
    except UnicodeError as error:
        raise ServerConfigError("The server hostname is not valid.") from error
    labels = normalized_host.split(".")
    if (
        not normalized_host
        or len(normalized_host) > 253
        or any(not 1 <= len(label) <= 63 for label in labels)
        or any(label.startswith("-") or label.endswith("-") for label in labels)
        or any(not re.fullmatch(r"[A-Za-z0-9-]+", label) for label in labels)
    ):
        raise ServerConfigError("The server hostname is not valid.")
    return normalized_host


def validate_server_endpoint(host: object, port: object) -> tuple[str, int]:
    """Validate and normalize a bare hostname/IP plus UDP port."""

    normalized_host = validate_server_host(host)

    if isinstance(port, bool):
        raise ServerConfigError("The server port must be a number from 1 to 65535.")
    try:
        normalized_port = int(port)
    except (TypeError, ValueError) as error:
        raise ServerConfigError(
            "The server port must be a number from 1 to 65535."
        ) from error
    if not 1 <= normalized_port <= 65535:
        raise ServerConfigError("The server port must be from 1 to 65535.")

    return normalized_host, normalized_port


# The public resolvers a login may ask when this machine's own resolver cannot
# answer -- asked *by address*, so the question needs no lookup of its own, and
# both carry their address in their own certificate, so the reply is verified
# like any other HTTPS call. They are two rather than one because the whole
# point is a machine whose DNS is being interfered with, and an address that is
# blocked on one network is not necessarily blocked on the next.
#
# This is deliberately *not* an Option: it is how a login answers the one
# question it cannot answer for itself, not a setting a player chose, and the
# endpoint is not an Option either (see ENDPOINT_OPTION_KEYS).
DOH_QUERY_URLS = ("https://1.1.1.1/dns-query", "https://8.8.8.8/resolve")
DOH_TIMEOUT_S = 3.0
# An answer is kept for a while, and a failure is never kept: a login resolves
# the same name once per port it walks, and the second question is the same
# question. A failure that stuck would outlive the moment it happened on.
DOH_CACHE_TTL_S = 300.0
_PUBLIC_LOOKUP_CACHE: dict[str, tuple[float, str]] = {}


def public_lookup_enabled() -> bool:
    """Whether this build can ask a public resolver at all.

    ``requests`` is what asks, and a build without it (a trimmed one) keeps the
    login it always had: the sentence a player hears may promise a public
    resolver was asked only when this is True, and ``resolve_host_via_doh``
    never runs when it is False.
    """

    try:
        import requests  # noqa: F401  (what asks; the import is the question)
    except ImportError:
        return False
    return True


def _first_ipv4_answer(body: object) -> str | None:
    """The first A record of a DNS-over-HTTPS JSON answer, if there is one.

    A CNAME chain arrives before the address it ends at, and anything that is
    not an A record (an AAAA, a signature) is not an address this transport can
    use, so the first IPv4 answer is the answer.
    """

    if not isinstance(body, dict):
        return None
    answers = body.get("Answer")
    if not isinstance(answers, (list, tuple)):
        return None
    for answer in answers:
        if not isinstance(answer, dict) or answer.get("type") != 1:
            continue
        try:
            literal = ipaddress.ip_address(str(answer.get("data", "")).strip())
        except ValueError:
            continue
        if literal.version == 4:
            return str(literal)
    return None


def resolve_host_via_doh(
    host: object,
    *,
    urls: Sequence[str] = DOH_QUERY_URLS,
    timeout: float = DOH_TIMEOUT_S,
    clock: Callable[[], float] = time.monotonic,
) -> str | None:
    """The IPv4 address a public resolver gives for *host*, or None.

    The second way to answer a question this machine's own resolver cannot, and
    the reason it exists: a filtering or family DNS, an ad-blocking resolver, a
    router that blocks *the name*, and a DNS that answers a landing page all
    leave a player able to play every other online game and unable to reach this
    one -- and the first answer to that (an address embedded in the pack) is a
    snapshot that goes stale the day the server moves.

    Nothing here can fail the login: a refused, timed out, blocked, malformed or
    unverifiable answer is simply no answer (the walk falls back to the
    addresses the pack carries), and a build without ``requests`` asks nobody.
    """

    if not isinstance(host, str) or not host.strip():
        return None
    name = host.strip().lower()
    cached = _PUBLIC_LOOKUP_CACHE.get(name)
    if cached is not None and clock() - cached[0] < DOH_CACHE_TTL_S:
        return cached[1]
    if not public_lookup_enabled():
        return None

    import requests

    for url in urls:
        try:
            response = requests.get(
                url,
                params={"name": host.strip(), "type": "A"},
                headers={"accept": "application/dns-json"},
                timeout=timeout,
            )
            if getattr(response, "status_code", 0) != 200:
                continue
            body = response.json()
        except Exception:
            continue
        address = _first_ipv4_answer(body)
        if address is None:
            continue
        _PUBLIC_LOOKUP_CACHE[name] = (clock(), address)
        return address
    return None


def _lookup_locally(host: object) -> str | None:
    """This machine's own resolver: an IPv4 address, or None."""

    try:
        answers = socket.getaddrinfo(
            str(host), None, socket.AF_INET, socket.SOCK_DGRAM
        )
    except (OSError, UnicodeError, TypeError):
        return None
    for answer in answers:
        address = answer[4][0] if len(answer) > 4 else None
        if address:
            return str(address)
    return None


def resolve_host_with_source(
    host: object, *, public_resolver: bool = True
) -> tuple[str | None, str | None]:
    """``(address, how it was answered)`` for *host*, the second half for the log.

    The source is ``"literal"`` (an address dialled exactly as given, which is
    what a backup address is), ``"local"`` (this machine's own resolver), or
    ``"public"`` (a public resolver over HTTPS, asked only after the local one
    had nothing). A login that only got in through a public resolver is a player
    whose own DNS is the problem, and that is a fact nothing else records: the
    login flow puts it on the one ``[LOGIN]`` line it writes.

    ``public_resolver=False`` is the old behaviour exactly (this machine's
    resolver or nothing), which is what a caller that must not reach out wants.
    """

    if isinstance(host, str):
        try:
            literal = ipaddress.ip_address(host.strip())
        except ValueError:
            literal = None
        if literal is not None:
            return (str(literal), "literal") if literal.version == 4 else (None, None)

    address = _lookup_locally(host)
    if address is not None:
        return address, "local"
    if not public_resolver or not public_lookup_enabled():
        return None, None
    address = resolve_host_via_doh(host)
    if address is not None:
        return address, "public"
    return None, None


def resolve_host(host: object, *, public_resolver: bool = True) -> str | None:
    """The IPv4 address *host* means here, or None when nothing can say.

    One lookup, in one place, and its answer is handed to the transport: this
    client used to pass the *name* to ENet and let it resolve, so a name that
    could not be looked up reached the login as a bare ``OSError`` -- the same
    shape as a socket that would not open, and the same sentence at the menu.
    An answer is a value here, and a value can be acted on: an address to try
    instead, a field in the log, a line the player hears.

    A literal address is returned as it is and never looked up (a backup
    address is one), and anything that is not IPv4 answers None rather than an
    address this transport could only fail on later -- ENet here has no IPv6.

    A name this machine cannot answer gets one more chance before it is given
    up on (``resolve_host_via_doh``), because that is the failure the whole
    fallback exists for and the public resolver is the one that keeps working
    when the server's address changes.
    """

    return resolve_host_with_source(host, public_resolver=public_resolver)[0]


def _default_dev_config_path() -> Path:
    return Path(__file__).resolve().parents[1] / DEV_CONFIG_FILENAME


def _load_dev_config(path: Path) -> Mapping[str, object] | None:
    if not path.is_file():
        return None
    try:
        with path.open("r", encoding="utf-8") as config_file:
            data = json.load(config_file)
    except (OSError, json.JSONDecodeError) as error:
        raise ServerConfigError(f"Could not read {path.name}: {error}") from error
    if not isinstance(data, dict):
        raise ServerConfigError(f"{path.name} must contain a JSON object.")
    return data


def _parse_dev_cli(argv: Sequence[str]) -> dict[str, object]:
    """Read only endpoint arguments and leave unrelated game flags untouched."""

    overrides: dict[str, object] = {}
    index = 0
    while index < len(argv):
        argument = argv[index]
        if argument == "local":
            overrides["host"] = "127.0.0.1"
        elif argument.startswith("--host="):
            overrides["host"] = argument.split("=", 1)[1]
        elif argument == "--host":
            if index + 1 >= len(argv):
                raise ServerConfigError("--host requires a hostname or IP address.")
            index += 1
            overrides["host"] = argv[index]
        elif argument.startswith("--port="):
            overrides["port"] = argument.split("=", 1)[1]
        elif argument == "--port":
            if index + 1 >= len(argv):
                raise ServerConfigError("--port requires a number from 1 to 65535.")
            index += 1
            overrides["port"] = argv[index]
        index += 1
    return overrides


def get_fallback_addresses(
    *,
    dev_config_path: os.PathLike[str] | str | None = None,
) -> tuple[str, ...]:
    """The addresses a login may fall back to when the name is no use.

    They exist for exactly one failure, and it is the one nothing can retry: a
    machine whose resolver cannot answer for the server's *name* -- a filtering
    or family DNS, an ad-blocking resolver, a router that blocks it -- while
    every other name on that machine works. A backup address the release build
    resolved and embedded in the encrypted pack needs no DNS at all, so that
    player reaches the game rather than a sentence about their connection.

    Production reads them from the same place the endpoint comes from (the pack,
    never Options -- ``ENDPOINT_OPTION_KEYS`` are stripped from a released
    build), where a source build may name them in ``dev_config.json`` beside host
    and port. A backup may never *break* a login: an entry that is not a bare
    hostname or address is dropped rather than raised, and the empty answer --
    the normal case -- means a login walks precisely the doors it always did.
    """

    if is_production_build():
        # Import lazily: vfs.init_vfs() runs before the game imports its options.
        from . import vfs

        embedded = vfs.get_embedded_server_config() or {}
        candidates: object = embedded.get(FALLBACK_ADDRESS_KEY)
    else:
        config_path = (
            Path(dev_config_path)
            if dev_config_path is not None
            else _default_dev_config_path()
        )
        try:
            dev_config = _load_dev_config(config_path)
        except ServerConfigError:
            dev_config = None
        candidates = (dev_config or {}).get(FALLBACK_ADDRESS_KEY)

    if isinstance(candidates, str):
        candidates = (candidates,)
    if not isinstance(candidates, (list, tuple)):
        return ()

    addresses: list[str] = []
    seen: set[str] = set()
    for entry in candidates:
        try:
            address = validate_server_host(entry)
        except ServerConfigError:
            continue
        if address.lower() in seen:
            continue
        seen.add(address.lower())
        addresses.append(address)
    return tuple(addresses)


def _production_endpoint() -> tuple[str, int]:
    # Import lazily: vfs.init_vfs() runs before the game imports its options.
    from . import vfs

    embedded = vfs.get_embedded_server_config()
    if not embedded:
        raise ServerConfigError(
            "The official server configuration is missing. Please reinstall the game."
        )
    return validate_server_endpoint(embedded.get("host"), embedded.get("port"))


def get_server_endpoint(
    *,
    argv: Sequence[str] | None = None,
    dev_config_path: os.PathLike[str] | str | None = None,
    settings_getter: Callable[[str, object], object] | None = None,
) -> tuple[str, int]:
    """Resolve an endpoint using production-safe, deterministic precedence.

    Production: encrypted VFS config only.
    Source: CLI overrides -> dev_config.json -> developer Options -> defaults.
    """

    if is_production_build():
        return _production_endpoint()

    if settings_getter is None:
        # Kept lazy to avoid a module import cycle with libs.options.
        from . import options

        settings_getter = options.get

    host: object = settings_getter("host", consts.DEFAULT_HOST)
    port: object = settings_getter("port", consts.DEFAULT_PORT)

    config_path = (
        Path(dev_config_path) if dev_config_path is not None else _default_dev_config_path()
    )
    dev_config = _load_dev_config(config_path)
    if dev_config is not None:
        host = dev_config.get("host", host)
        port = dev_config.get("port", port)

    cli_overrides = _parse_dev_cli(sys.argv[1:] if argv is None else argv)
    host = cli_overrides.get("host", host)
    port = cli_overrides.get("port", port)
    return validate_server_endpoint(host, port)
