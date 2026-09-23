"""Encrypted packaging tests with disposable assets, no game/data restoration."""
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import zipfile

CLIENT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("fixture_pack_data", CLIENT / "tools/pack_data.py")
pack_data = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pack_data)

spec_vfs = importlib.util.spec_from_file_location("fixture_vfs", CLIENT / "libs/vfs.py")
vfs = importlib.util.module_from_spec(spec_vfs)
spec_vfs.loader.exec_module(vfs)


class BuildAssetExclusionTests(unittest.TestCase):
    def test_encrypted_archive_excludes_dollar_names_and_preserves_originals(self):
        with tempfile.TemporaryDirectory(prefix="bt-pack-filter-test-") as temporary:
            root = Path(temporary)
            assets = root / "data"
            files = {"sound.ogg": b"valid sound fixture", "nested/valid.ogg": b"another sound",
                     "sound$.ogg": b"excluded sound", "ffmpeg$.exe": b"excluded binary",
                     "old$/nested/normal.exe": b"excluded directory", "nested/note$.txt": b"excluded text"}
            for name, data in files.items():
                path = assets / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
            output = root / "sounds.dat"
            # Walk-level exclusion: dollar-sign paths are pruned without being
            # read, so the pack index contains only clean members.
            with mock.patch.object(pack_data.os, "walk", wraps=pack_data.os.walk) as walk_mock:
                pack_data.pack_data(assets, output, "official.example", 13000)
            visited = "".join(str(args[0]) for args in walk_mock.call_args_list)
            self.assertNotIn("old$", visited)
            self.assertNotIn(b"official.example", output.read_bytes())
            # Every member is an independently encrypted BTX1 blob.
            with zipfile.ZipFile(output) as archive:
                for name in ("sound.ogg", "nested/valid.ogg"):
                    blob = archive.read(name)
                    self.assertTrue(blob.startswith(vfs.FORMAT_MAGIC), name)
                self.assertEqual(
                    vfs.btx_decrypt(archive.read("sound.ogg")), files["sound.ogg"])
                self.assertEqual(
                    vfs.btx_decrypt(archive.read("nested/valid.ogg")), files["nested/valid.ogg"])
                self.assertEqual(
                    json.loads(vfs.btx_decrypt(archive.read(pack_data.SERVER_CONFIG_MEMBER))),
                    {"host": "official.example", "port": 13000})
                meta = json.loads(vfs.btx_decrypt(archive.read(pack_data.PACK_META_MEMBER)))
                self.assertEqual(meta["format"], vfs.FORMAT_NAME)
            # The whole pack can be served through the lazy reader.
            cache = root / "cache"
            reader = vfs.PackVFS(output, cache)
            self.assertEqual(reader.read_member("sound.ogg"), files["sound.ogg"])
            self.assertEqual(reader.server_config, {"host": "official.example", "port": 13000})
            reader.close()
            for name, data in files.items():
                self.assertEqual((assets / name).read_bytes(), data)
            self.assertFalse(any(root.glob("bt_data_*.zip")))

    def test_member_nonces_are_unique(self):
        with tempfile.TemporaryDirectory(prefix="bt-pack-nonce-test-") as temporary:
            root = Path(temporary)
            assets = root / "data"
            (assets / "a").mkdir(parents=True)
            (assets / "a" / "one.ogg").write_bytes(b"first")
            (assets / "a" / "two.ogg").write_bytes(b"second")
            output = root / "sounds.dat"
            pack_data.pack_data(assets, output, "official.example", 13000)
            with zipfile.ZipFile(output) as archive:
                n1 = archive.read("a/one.ogg")[5:17]
                n2 = archive.read("a/two.ogg")[5:17]
                self.assertNotEqual(n1, n2)

    def test_tampered_member_is_rejected(self):
        with tempfile.TemporaryDirectory(prefix="bt-pack-tamper-test-") as temporary:
            root = Path(temporary)
            assets = root / "data"
            assets.mkdir()
            (assets / "sound.ogg").write_bytes(b"precious sound")
            output = root / "sounds.dat"
            pack_data.pack_data(assets, output, "official.example", 13000)
            with zipfile.ZipFile(output, "r") as archive:
                blob = bytearray(archive.read("sound.ogg"))
            blob[30] ^= 0x01  # Flip one ciphertext byte.
            with zipfile.ZipFile(output, "w") as archive:
                archive.writestr("sound.ogg", bytes(blob))
            with self.assertRaises(Exception):
                with zipfile.ZipFile(output) as archive:
                    vfs.btx_decrypt(archive.read("sound.ogg"))

    def test_reserved_embedded_config_still_rejected(self):
        with tempfile.TemporaryDirectory(prefix="bt-pack-filter-test-") as temporary:
            root = Path(temporary)
            assets = root / "data"
            reserved = assets / pack_data.SERVER_CONFIG_MEMBER
            reserved.parent.mkdir(parents=True)
            reserved.write_text("untrusted endpoint", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "reserved"):
                pack_data.pack_data(assets, root / "sounds.dat", "official.example", 13000)
            self.assertFalse((root / "sounds.dat").exists())
            self.assertFalse(any(root.glob("bt_data_*.zip")))

class EmbeddedBackupAddressTests(unittest.TestCase):
    """The address a pack carries beside the endpoint, for the machine whose DNS
    cannot answer for the name (libs/login_attempts.py)."""

    def test_a_named_backup_is_embedded_beside_the_endpoint(self):
        with tempfile.TemporaryDirectory(prefix="bt-pack-backup-test-") as temporary:
            root = Path(temporary)
            assets = root / "data"
            assets.mkdir()
            (assets / "sound.ogg").write_bytes(b"precious sound")
            output = root / "sounds.dat"
            pack_data.pack_data(
                assets, output, "official.example", 13000, ["103.30.126.64"]
            )
            with zipfile.ZipFile(output) as archive:
                embedded = json.loads(
                    vfs.btx_decrypt(archive.read(vfs.SERVER_CONFIG_MEMBER))
                )
            self.assertEqual(
                embedded,
                {
                    "host": "official.example",
                    "port": 13000,
                    "addresses": ["103.30.126.64"],
                },
            )

    def test_an_explicit_backup_must_be_an_address_or_a_hostname(self):
        with self.assertRaises(pack_data.ServerConfigError):
            pack_data.backup_addresses_for("official.example", ["http://nope/"])

    def test_an_ipv6_backup_is_refused_rather_than_embedded(self):
        """The transport has no IPv6: embedding one would only move the failure."""
        with self.assertRaises(pack_data.ServerConfigError):
            pack_data.backup_addresses_for("official.example", ["::1"])

    def test_this_machines_answer_is_embedded_when_nothing_is_named(self):
        with mock.patch.object(
            pack_data, "resolve_host", return_value="103.30.126.64"
        ):
            self.assertEqual(
                pack_data.backup_addresses_for("official.example"),
                ("103.30.126.64",),
            )

    def test_a_private_answer_is_not_embedded(self):
        """A pack built inside the server's own network must not send players to
        a LAN, a loopback or a carrier-NAT address they can never reach."""
        for address in ("192.168.1.10", "10.0.0.5", "127.0.0.1", "100.64.0.1"):
            with mock.patch.object(pack_data, "resolve_host", return_value=address):
                self.assertEqual(
                    pack_data.backup_addresses_for("official.example"), (), address
                )

    def test_a_name_that_cannot_be_resolved_embeds_nothing(self):
        with mock.patch.object(pack_data, "resolve_host", return_value=None):
            self.assertEqual(pack_data.backup_addresses_for("official.example"), ())

    def test_an_endpoint_that_is_already_an_address_adds_nothing(self):
        with mock.patch.object(
            pack_data, "resolve_host", return_value="103.30.126.64"
        ):
            self.assertEqual(pack_data.backup_addresses_for("103.30.126.64"), ())

    def test_the_packer_reads_a_backup_from_the_build_config(self):
        with tempfile.TemporaryDirectory(prefix="bt-pack-backup-cli-test-") as temporary:
            config_path = Path(temporary) / "build_server_config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "host": "game.example.com",
                        "port": 14000,
                        "addresses": ["103.30.126.64"],
                    }
                ),
                encoding="utf-8",
            )
            with mock.patch.dict(os.environ, {}, clear=True):
                args = pack_data._parse_args(["--server-config", str(config_path)])
        self.assertEqual(args.server_host, "game.example.com")
        self.assertEqual(args.server_addresses, ["103.30.126.64"])

    def test_a_repeated_cli_backup_is_taken_in_order(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            args = pack_data._parse_args(
                [
                    "--server-host",
                    "game.example.com",
                    "--server-address",
                    "103.30.126.64",
                    "--server-address",
                    "backup.example",
                ]
            )
        self.assertEqual(args.server_addresses, ["103.30.126.64", "backup.example"])

    def test_a_bad_cli_backup_stops_the_pack_rather_than_being_dropped(self):
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch(
            "sys.stderr", new=io.StringIO()
        ), self.assertRaises(SystemExit):
            pack_data._parse_args(
                ["--server-host", "game.example.com", "--server-address", "nope://x"]
            )

    def test_asset_walk_errors_are_not_silently_ignored(self):
        def fail_walk(root, followlinks, onerror):
            onerror(PermissionError("fixture access denied"))
            return []
        with mock.patch.object(pack_data.os, "walk", side_effect=fail_walk):
            with self.assertRaises(PermissionError):
                list(pack_data.iter_build_assets(Path("fixture")))


if __name__ == "__main__":
    unittest.main()