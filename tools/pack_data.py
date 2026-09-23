import argparse
import ipaddress
import json
import os
from pathlib import Path
import sys
import tempfile
import zipfile

CLIENT_ROOT = Path(__file__).resolve().parents[1]
if str(CLIENT_ROOT) not in sys.path:
    sys.path.insert(0, str(CLIENT_ROOT))

from libs.server_config import (
    FALLBACK_ADDRESS_KEY,
    ServerConfigError,
    resolve_host,
    validate_server_endpoint,
    validate_server_host,
)
from libs.vfs import (
    PACK_META_MEMBER,
    SERVER_CONFIG_MEMBER,
    FORMAT_NAME,
    btx_encrypt,
)

DEFAULT_BUILD_CONFIG = "build_server_config.json"


def iter_build_assets(source_root: Path):
    """Exclude dollar-sign files and whole directories without reading them."""
    def walk_error(error):
        raise error
    for directory, names, files in os.walk(source_root, followlinks=False, onerror=walk_error):
        names[:] = sorted(name for name in names if "$" not in name)
        for name in sorted(files):
            if "$" not in name:
                yield Path(directory) / name


def _as_ipv4_literal(address: str) -> bool | None:
    """True/False for a literal address, None when it is a name."""
    try:
        return ipaddress.ip_address(address).version == 4
    except ValueError:
        return None


def backup_addresses_for(host: str, addresses=()) -> tuple[str, ...]:
    """The backup addresses a pack carries beside the endpoint.

    A released build is one name and, until now, nothing else: a player whose
    resolver refuses that name could not get in at all, and no amount of
    retrying changes it (``libs/login_attempts.py``). The address this machine
    resolves the name to is embedded beside it, so that login has a door with no
    DNS in front of it.

    Named addresses win, and are an operator's override -- packing from inside
    the server's own network, or from a machine whose DNS is the very thing that
    cannot be trusted. Otherwise this machine is asked, and only a *global* IPv4
    is taken: a private, loopback or VPN answer is the packer's own network, and
    embedding it would send players somewhere they can never reach. A backup can
    be a name as well as an address, but a literal must be IPv4 -- the transport
    has no IPv6, so embedding one would only move the failure somewhere later.
    """
    chosen: list[str] = []
    seen: set[str] = set()
    for entry in addresses:
        # A named backup is an instruction: a bad one is an error, not a shrug.
        address = validate_server_host(entry)
        if _as_ipv4_literal(address) is False:
            raise ServerConfigError(
                "A backup address must be an IPv4 address or a hostname."
            )
        if address.lower() in seen:
            continue
        seen.add(address.lower())
        chosen.append(address)
    if chosen:
        return tuple(chosen)

    resolved = resolve_host(host)
    if not resolved or resolved == host:
        return ()
    try:
        is_global = ipaddress.ip_address(resolved).is_global
    except ValueError:
        is_global = False
    return (resolved,) if is_global else ()


def pack_data(
    data_dir: os.PathLike[str] | str,
    output_path: os.PathLike[str] | str,
    server_host: object,
    server_port: object,
    addresses=None,
) -> Path:
    """Create an encrypted VFS archive without writing config into source data.

    Every member is encrypted independently with XChaCha20-Poly1305 and a
    fresh random nonce (see ``vfs.btx_encrypt``), so the client can decrypt
    single sounds on demand instead of unpacking the whole archive.

    ``addresses`` is what travels beside the endpoint as a login's backup
    (``backup_addresses_for``): a sequence to name them, and None -- the
    default, which is what every caller wants -- to ask this machine for the
    endpoint's own address.
    """

    source_root = Path(data_dir).resolve()
    destination = Path(output_path).resolve()
    if not source_root.is_dir():
        raise FileNotFoundError(f"Data folder not found: {source_root}")
    if destination == source_root or source_root in destination.parents:
        raise ValueError("The encrypted output must be outside the source data folder.")

    host, port = validate_server_endpoint(server_host, server_port)
    backups = (
        backup_addresses_for(host)
        if addresses is None
        else tuple(validate_server_host(entry) for entry in addresses)
    )
    destination.parent.mkdir(parents=True, exist_ok=True)

    temporary_zip_path = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix="bt_data_",
            suffix=".zip",
            dir=destination.parent,
            delete=False,
        ) as temporary_zip:
            temporary_zip_path = Path(temporary_zip.name)

        member_count = 0
        with zipfile.ZipFile(
            temporary_zip_path, "w", compression=zipfile.ZIP_STORED
        ) as archive:
            for source_path in iter_build_assets(source_root):
                if source_path.is_file():
                    archive_name = source_path.relative_to(source_root).as_posix()
                    if archive_name == SERVER_CONFIG_MEMBER:
                        raise ValueError(
                            f"{SERVER_CONFIG_MEMBER} is reserved for the build system."
                        )
                    archive.writestr(
                        archive_name,
                        btx_encrypt(source_path.read_bytes()),
                    )
                    member_count += 1
            embedded = {"host": host, "port": port}
            if backups:
                embedded[FALLBACK_ADDRESS_KEY] = list(backups)
            archive.writestr(
                SERVER_CONFIG_MEMBER,
                btx_encrypt(
                    json.dumps(
                        embedded,
                        ensure_ascii=True,
                        separators=(",", ":"),
                        sort_keys=True,
                    ).encode("utf-8")
                ),
            )
            archive.writestr(
                PACK_META_MEMBER,
                btx_encrypt(
                    json.dumps(
                        {
                            "format": FORMAT_NAME,
                            "version": 1,
                            "members": member_count,
                            "created": None,
                        },
                        ensure_ascii=True,
                        separators=(",", ":"),
                        sort_keys=True,
                    ).encode("utf-8")
                ),
            )

        os.replace(temporary_zip_path, destination)
        return destination
    finally:
        if temporary_zip_path is not None:
            temporary_zip_path.unlink(missing_ok=True)


def _parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Pack client data and embed the official server endpoint."
    )
    parser.add_argument("--data-dir", default="data")
    parser.add_argument("--output", default="sounds.dat")
    parser.add_argument("--server-config", default=DEFAULT_BUILD_CONFIG)
    parser.add_argument("--server-host")
    parser.add_argument("--server-port")
    parser.add_argument(
        "--server-address",
        action="append",
        dest="server_addresses",
        help=(
            "a backup address to embed beside the server name (repeatable); "
            "otherwise the name is resolved on this machine"
        ),
    )
    args = parser.parse_args(argv)

    file_config = {}
    config_path = Path(args.server_config)
    if config_path.is_file():
        try:
            file_config = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            parser.error(f"could not read {config_path.name}: {error}")
        if not isinstance(file_config, dict):
            parser.error(f"{config_path.name} must contain a JSON object")

    args.server_host = (
        args.server_host
        or os.environ.get("BT_SERVER_HOST")
        or file_config.get("host")
    )
    args.server_port = (
        args.server_port
        or os.environ.get("BT_SERVER_PORT")
        or file_config.get("port", 13000)
    )
    if not args.server_host:
        parser.error(
            f"enter host in {config_path.name}, set BT_SERVER_HOST, or pass --server-host"
        )
    try:
        args.server_host, args.server_port = validate_server_endpoint(
            args.server_host, args.server_port
        )
    except ServerConfigError as error:
        parser.error(str(error))

    # CLI, then environment, then the build config -- the same precedence the
    # endpoint itself has, because they are two halves of one document.
    named = list(args.server_addresses or ())
    environment = os.environ.get("BT_SERVER_ADDRESS") or ""
    if environment and not named:
        named = [part for part in environment.split(",") if part.strip()]
    if not named:
        file_addresses = file_config.get("addresses") or []
        if not isinstance(file_addresses, (list, tuple)):
            parser.error(f"{config_path.name}'s addresses must be a list")
        named = list(file_addresses)
    try:
        args.server_addresses = [validate_server_host(entry) for entry in named]
    except ServerConfigError as error:
        parser.error(str(error))
    return args


def main(argv=None):
    args = _parse_args(argv)
    print("Packing and encrypting client data...")
    addresses = backup_addresses_for(args.server_host, args.server_addresses)
    if addresses:
        print("Embedding backup address(es): " + ", ".join(addresses))
    else:
        print(
            "No backup address embedded: "
            f"{args.server_host} did not resolve to a public IPv4 address on this "
            "machine. Pass --server-address or set BT_SERVER_ADDRESS to embed one."
        )
    output = pack_data(
        args.data_dir,
        args.output,
        args.server_host,
        args.server_port,
        addresses,
    )
    print(f"Data packed and encrypted to {output.name} successfully.")


if __name__ == "__main__":
    main()