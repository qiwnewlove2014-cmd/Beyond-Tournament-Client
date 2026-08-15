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
import sys
from typing import Callable, Mapping, Sequence

from . import consts


DEV_CONFIG_FILENAME = "dev_config.json"
ENDPOINT_OPTION_KEYS = frozenset(("host", "port"))


class ServerConfigError(ValueError):
    """Raised when no safe, valid server endpoint can be resolved."""


def is_production_build() -> bool:
    """Return True for frozen/Nuitka release builds, False for source runs."""

    return bool(getattr(sys, "frozen", False) or "__compiled__" in globals())


def validate_server_endpoint(host: object, port: object) -> tuple[str, int]:
    """Validate and normalize a bare hostname/IP plus UDP port."""

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
        normalized_host = str(ipaddress.ip_address(host))
    except ValueError:
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
