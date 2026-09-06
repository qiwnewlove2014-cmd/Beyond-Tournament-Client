"""Encrypted packaging tests with disposable assets, no game/data restoration."""
import importlib.util
import io
import json
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

    def test_asset_walk_errors_are_not_silently_ignored(self):
        def fail_walk(root, followlinks, onerror):
            onerror(PermissionError("fixture access denied"))
            return []
        with mock.patch.object(pack_data.os, "walk", side_effect=fail_walk):
            with self.assertRaises(PermissionError):
                list(pack_data.iter_build_assets(Path("fixture")))


if __name__ == "__main__":
    unittest.main()