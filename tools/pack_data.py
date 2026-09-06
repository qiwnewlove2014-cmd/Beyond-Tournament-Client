import argparse
import json
import os
from pathlib import Path
import sys
import tempfile
import zipfile

CLIENT_ROOT = Path(__file__).resolve().parents[1]
if str(CLIENT_ROOT) not in sys.path:
    sys.path.insert(0, str(CLIENT_ROOT))

from libs.server_config import ServerConfigError, validate_server_endpoint
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


def pack_data(
    data_dir: os.PathLike[str] | str,
    output_path: os.PathLike[str] | str,
    server_host: object,
    server_port: object,
) -> Path:
    """Create an encrypted VFS archive without writing config into source data.

    Every member is encrypted independently with XChaCha20-Poly1305 and a
    fresh random nonce (see ``vfs.btx_encrypt``), so the client can decrypt
    single sounds on demand instead of unpacking the whole archive.
    """

    source_root = Path(data_dir).resolve()
    destination = Path(output_path).resolve()
    if not source_root.is_dir():
        raise FileNotFoundError(f"Data folder not found: {source_root}")
    if destination == source_root or source_root in destination.parents:
        raise ValueError("The encrypted output must be outside the source data folder.")

    host, port = validate_server_endpoint(server_host, server_port)
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
            archive.writestr(
                SERVER_CONFIG_MEMBER,
                btx_encrypt(
                    json.dumps(
                        {"host": host, "port": port},
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
    return args


def main(argv=None):
    args = _parse_args(argv)
    print("Packing and encrypting client data...")
    output = pack_data(
        args.data_dir,
        args.output,
        args.server_host,
        args.server_port,
    )
    print(f"Data packed and encrypted to {output.name} successfully.")


if __name__ == "__main__":
    main()