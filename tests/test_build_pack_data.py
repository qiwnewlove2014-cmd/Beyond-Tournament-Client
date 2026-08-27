"""Encrypted packaging tests with disposable assets, no game/data restoration."""
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import zipfile

from cryptography.fernet import Fernet

CLIENT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("fixture_pack_data", CLIENT / "tools/pack_data.py")
pack_data = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pack_data)


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
            original_write = zipfile.ZipFile.write
            written = []
            def checked_write(archive, filename, *args, **kwargs):
                relative = Path(filename).relative_to(assets)
                self.assertFalse(any("$" in part for part in relative.parts))
                written.append(relative.as_posix())
                return original_write(archive, filename, *args, **kwargs)
            with mock.patch.object(zipfile.ZipFile, "write", checked_write):
                pack_data.pack_data(assets, output, "official.example", 13000)
            self.assertEqual(set(written), {"sound.ogg", "nested/valid.ogg"})
            self.assertNotIn(b"official.example", output.read_bytes())
            key = bytes.fromhex(pack_data.KEY_PART_1 + pack_data.KEY_PART_2)
            decrypted = Fernet(key).decrypt(output.read_bytes())
            with zipfile.ZipFile(io.BytesIO(decrypted)) as archive:
                self.assertEqual(set(archive.namelist()),
                                 {"sound.ogg", "nested/valid.ogg", pack_data.SERVER_CONFIG_MEMBER})
                self.assertEqual(archive.read("sound.ogg"), files["sound.ogg"])
                self.assertEqual(json.loads(archive.read(pack_data.SERVER_CONFIG_MEMBER)),
                                 {"host": "official.example", "port": 13000})
            for name, data in files.items():
                self.assertEqual((assets / name).read_bytes(), data)
            self.assertFalse(any(root.glob("bt_data_*.zip")))

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
