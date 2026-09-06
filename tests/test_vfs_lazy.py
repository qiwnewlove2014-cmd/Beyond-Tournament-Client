"""Lazy VFS reader tests: per-member decryption, LRU cache, path hooks."""
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

CLIENT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CLIENT))

spec_pack = importlib.util.spec_from_file_location("fixture_pack_data", CLIENT / "tools/pack_data.py")
pack_data = importlib.util.module_from_spec(spec_pack)
spec_pack.loader.exec_module(pack_data)

spec_vfs = importlib.util.spec_from_file_location("fixture_vfs", CLIENT / "libs/vfs.py")
vfs = importlib.util.module_from_spec(spec_vfs)
spec_vfs.loader.exec_module(vfs)


class LazyPackFixture:
    """Build a small sounds.dat-style pack in a temp dir."""

    def __init__(self):
        self.root = Path(tempfile.mkdtemp(prefix="bt-vfs-lazy-"))
        self.assets = self.root / "data"
        self.assets.mkdir()
        self.files = {
            "menu/click.ogg": b"OGG-CLICK" * 40,
            "menu/hover.ogg": b"OGG-HOVER" * 80,
            "ambience/loop.ogg": b"OGG-LOOP" * 200,
            "ui/warn.ogg": b"OGG-WARN" * 10,
        }
        for name, data in self.files.items():
            path = self.assets / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        self.pak = self.root / "sounds.dat"
        pack_data.pack_data(self.assets, self.pak, "official.example", 13000)
        self.cache = self.root / "cache"
        self.reader = vfs.PackVFS(self.pak, self.cache)

    def close(self):
        self.reader.close()
        import shutil
        shutil.rmtree(self.root, ignore_errors=True)


class PackVFSReaderTests(unittest.TestCase):
    def test_read_member_roundtrip(self):
        fixture = LazyPackFixture()
        try:
            for name, data in fixture.files.items():
                self.assertEqual(fixture.reader.read_member(name), data)
            # No file materialized by pure in-memory reads.
            self.assertEqual(sorted(os.listdir(fixture.cache)), [])
        finally:
            fixture.close()

    def test_materialize_is_lazy_and_cached(self):
        fixture = LazyPackFixture()
        try:
            path_a = fixture.reader.materialize("menu/click.ogg")
            self.assertTrue(os.path.isfile(path_a))
            self.assertEqual(open(path_a, "rb").read(), fixture.files["menu/click.ogg"])
            path_b = fixture.reader.materialize("menu/click.ogg")
            self.assertEqual(path_a, path_b)  # Same cached file, refreshed LRU.
            self.assertEqual(sorted(os.listdir(fixture.cache)), ["menu"])
        finally:
            fixture.close()

    def test_lru_eviction_frees_oldest(self):
        fixture = LazyPackFixture()
        try:
            # Cap fits hover+loop but not click+hover+loop.
            hover = len(fixture.files["menu/hover.ogg"])
            loop = len(fixture.files["ambience/loop.ogg"])
            fixture.reader._max_bytes = hover + loop + 1
            p1 = fixture.reader.materialize("menu/click.ogg")
            p2 = fixture.reader.materialize("menu/hover.ogg")
            p3 = fixture.reader.materialize("ambience/loop.ogg")
            self.assertTrue(os.path.isfile(p2))
            self.assertTrue(os.path.isfile(p3))
            self.assertFalse(os.path.exists(p1))  # Oldest evicted.
            # Evicted member can be re-materialized.
            p1_again = fixture.reader.materialize("menu/click.ogg")
            self.assertTrue(os.path.isfile(p1_again))
        finally:
            fixture.close()

    def test_case_insensitive_lookup_matches_windows_normcase(self):
        # Regression: instrument samples are requested through
        # os.path.normcase (lowercased on Windows) while the pack keeps the
        # original data/ folder case (e.g. ``piano/Piano.mf.A1.ogg``).
        # Exact-case matching made every instrument silent in the built game.
        fixture = LazyPackFixture()
        try:
            fixture.reader.close()
            mixed = Path(tempfile.mkdtemp(prefix="bt-vfs-mixedcase-"))
            try:
                assets = mixed / "data"
                assets.mkdir()
                sub = assets / "piano"
                sub.mkdir()
                (sub / "Piano.mf.A1.ogg").write_bytes(b"OGG-PIANO" * 50)
                pak = mixed / "sounds.dat"
                pack_data.pack_data(assets, pak, "official.example", 13000)
                reader = vfs.PackVFS(pak, mixed / "cache")
                try:
                    lower = reader.materialize("piano/piano.mf.a1.ogg")
                    upper = reader.materialize("PIANO/PIANO.MF.A1.OGG")
                    exact = reader.materialize("piano/Piano.mf.A1.ogg")
                    # One materialization, shared by every spelling.
                    self.assertEqual(lower, upper)
                    self.assertEqual(lower, exact)
                    self.assertEqual(
                        open(lower, "rb").read(), b"OGG-PIANO" * 50)
                    self.assertTrue(reader.member_exists("piano/piano.mf.a1.ogg"))
                    self.assertTrue(reader.member_is_dir("PIANO"))
                    self.assertEqual(
                        reader.list_members("PIANO"),
                        [("Piano.mf.A1.ogg", False)])
                finally:
                    reader.close()
            finally:
                import shutil
                shutil.rmtree(mixed, ignore_errors=True)
        finally:
            fixture.close()

    def test_unknown_member_and_unsafe_names_rejected(self):
        fixture = LazyPackFixture()
        try:
            with self.assertRaises(KeyError):
                fixture.reader.materialize("nope.ogg")
            with self.assertRaises(ValueError):
                fixture.reader.materialize("../evil.ogg")
            with self.assertRaises(ValueError):
                fixture.reader.materialize("/abs.ogg")
        finally:
            fixture.close()

    def test_directory_listing_from_index(self):
        fixture = LazyPackFixture()
        try:
            self.assertEqual(
                fixture.reader.list_members(""),
                [("ambience", True), ("menu", True), ("ui", True)])
            self.assertEqual(
                fixture.reader.list_members("menu"),
                [("click.ogg", False), ("hover.ogg", False)])
            self.assertTrue(fixture.reader.member_is_dir("menu"))
            self.assertFalse(fixture.reader.member_is_dir("menu/click.ogg"))
        finally:
            fixture.close()

    def test_tampered_pack_rejected_on_read(self):
        fixture = LazyPackFixture()
        try:
            import zipfile as zf
            with zf.ZipFile(fixture.pak, "r") as archive:
                blob = bytearray(archive.read("menu/click.ogg"))
            blob[20] ^= 0xFF
            # Rebuild the zip with the tampered member.
            tmp = fixture.root / "tampered.dat"
            with zf.ZipFile(fixture.pak, "r") as src, zf.ZipFile(tmp, "w") as dst:
                for info in src.infolist():
                    data = src.read(info.filename)
                    if info.filename == "menu/click.ogg":
                        data = bytes(blob)
                    dst.writestr(info, data)
            reader = vfs.PackVFS(tmp, fixture.root / "cache2")
            with self.assertRaises(Exception):
                reader.read_member("menu/click.ogg")  # Authentication fails.
            reader.close()
        finally:
            fixture.close()

    def test_wrong_format_meta_rejected(self):
        fixture = LazyPackFixture()
        try:
            import zipfile as zf
            tmp = fixture.root / "old.dat"
            with zf.ZipFile(fixture.pak, "r") as src, zf.ZipFile(tmp, "w") as dst:
                for info in src.infolist():
                    data = src.read(info.filename)
                    if info.filename == vfs.PACK_META_MEMBER:
                        data = vfs.btx_encrypt(
                            json.dumps({"format": "fernet-zip-v0"}).encode()
                        )
                    dst.writestr(info, data)
            with self.assertRaisesRegex(ValueError, "incompatible"):
                vfs.PackVFS(tmp, fixture.root / "cache3")
        finally:
            fixture.close()


class PackVFSHookTests(unittest.TestCase):
    def setUp(self):
        self._orig = (os.path.exists, os.path.isfile, os.path.isdir,
                      os.path.getsize, os.listdir, os.scandir)

    def tearDown(self):
        vfs.uninstall_hooks()

    def test_hooks_materialize_and_delegate(self):
        fixture = LazyPackFixture()
        try:
            vfs._INSTANCE = fixture.reader
            import libs.consts as consts
            consts.SOUNDPREPEND = str(fixture.cache) + "/"
            consts.SOUNDSPREPEND = "/" + consts.SOUNDPREPEND
            vfs.install_hooks()

            root = os.path.abspath(consts.SOUNDPREPEND)
            path = os.path.join(root, "menu/click.ogg")
            # exists/isfile materialize without ever being asked to open.
            self.assertTrue(os.path.exists(path))
            self.assertTrue(os.path.isfile(path))
            self.assertTrue(os.path.isfile(os.path.join(root, "menu/hover.ogg")))
            self.assertFalse(os.path.exists(os.path.join(root, "missing.ogg")))
            self.assertTrue(os.path.isdir(os.path.join(root, "menu")))
            self.assertFalse(os.path.isdir(os.path.join(root, "menu/click.ogg")))

            # builtins.open on a pack path returns decrypted bytes.
            with open(path, "rb") as handle:
                self.assertEqual(handle.read(), fixture.files["menu/click.ogg"])

            # Paths outside the root delegate untouched.
            outside = fixture.root / "plain.txt"
            outside.write_text("hello")
            self.assertTrue(os.path.exists(str(outside)))

            # Directory listing merges pack index without materializing all,
            # and hides internal .bt build members.
            names = os.listdir(root)
            self.assertIn("menu", names)
            self.assertIn("ui", names)
            self.assertNotIn(".bt", names)
            entries = list(os.scandir(os.path.join(root, "menu")))
            by_name = {entry.name: entry for entry in entries}
            self.assertIn("click.ogg", by_name)
            self.assertIn("hover.ogg", by_name)
            self.assertTrue(by_name["click.ogg"].is_file())
            self.assertFalse(by_name["click.ogg"].is_dir())
        finally:
            vfs._INSTANCE = None
            fixture.close()

    def test_listing_virtual_subdir_without_materializing(self):
        # Browsing a pack folder that has no physical directory yet (the
        # common first-browse case) must not crash the real scandir/listdir.
        fixture = LazyPackFixture()
        try:
            vfs._INSTANCE = fixture.reader
            import libs.consts as consts
            consts.SOUNDPREPEND = str(fixture.cache) + "/"
            consts.SOUNDSPREPEND = "/" + consts.SOUNDPREPEND
            vfs.install_hooks()

            root = os.path.abspath(consts.SOUNDPREPEND)
            # Physical disk truth bypasses the path hooks.
            real_listdir = vfs._ORIGINALS["listdir"]
            self.assertTrue(os.path.isdir(os.path.join(root, "ambience")))
            # Nothing materialized yet, and the listing still works.
            self.assertEqual(sorted(real_listdir(fixture.cache)), [])
            names = os.listdir(os.path.join(root, "ambience"))
            self.assertEqual(names, ["loop.ogg"])
            with os.scandir(os.path.join(root, "ambience")) as it:
                entries = {e.name: e for e in it}
            self.assertTrue(entries["loop.ogg"].is_file())
            # Listing alone must not materialize the member.
            self.assertEqual(sorted(real_listdir(fixture.cache)), [])
            # Root listing shows every top-level folder.
            self.assertEqual(
                sorted(os.listdir(root)),
                ["ambience", "menu", "ui"],
            )
        finally:
            vfs._INSTANCE = None
            fixture.close()

    def test_vorbis_hook_materializes_before_decode(self):
        fixture = LazyPackFixture()
        try:
            vfs._INSTANCE = fixture.reader
            import libs.consts as consts
            consts.SOUNDPREPEND = str(fixture.cache) + "/"
            consts.SOUNDSPREPEND = "/" + consts.SOUNDPREPEND
            vfs.install_hooks()
            calls = []

            def fake_decode(path, *args, **kwargs):
                calls.append(path)
                return "decoded:" + path

            # Replace the saved original AFTER install so the wrapper uses it.
            vfs._ORIGINALS["load_vorbis_pcm"] = fake_decode
            from libs import safe_vorbis
            result = safe_vorbis.load_vorbis_pcm(os.path.join(fixture.cache, "ui/warn.ogg"))
            self.assertTrue(calls)
            # The decoder received a real materialized file path.
            self.assertTrue(os.path.isfile(calls[0]))
            self.assertEqual(result, "decoded:" + calls[0])
        finally:
            vfs._INSTANCE = None
            fixture.close()

    def test_uninstall_restores_originals(self):
        fixture = LazyPackFixture()
        try:
            import builtins
            original_open = builtins.open
            vfs._INSTANCE = fixture.reader
            import libs.consts as consts
            consts.SOUNDPREPEND = str(fixture.cache) + "/"
            consts.SOUNDSPREPEND = "/" + consts.SOUNDPREPEND
            vfs.install_hooks()
            self.assertIsNot(builtins.open, original_open)
            vfs.uninstall_hooks()
            self.assertIs(builtins.open, original_open)
        finally:
            fixture.close()


class InitVFSTests(unittest.TestCase):
    def test_source_mode_unchanged(self):
        import libs.consts as consts
        vfs._reset_for_tests()
        with tempfile.TemporaryDirectory(prefix="bt-vfs-src-") as temporary:
            old_cwd = os.getcwd()
            try:
                os.chdir(temporary)
                os.makedirs("data")
                vfs.init_vfs()
                self.assertTrue(vfs.VFS_INITIALIZED)
                self.assertEqual(consts.SOUNDPREPEND, "data/")
                self.assertIsNone(vfs.get_embedded_server_config())
            finally:
                os.chdir(old_cwd)
                vfs._reset_for_tests()

    def test_pack_mode_mounts_lazy(self):
        import libs.consts as consts
        vfs._reset_for_tests()
        fixture = LazyPackFixture()
        fixture.reader.close()  # Release the pack handle so rename works.
        old_cwd = os.getcwd()
        try:
            os.chdir(fixture.root)
            vfs.init_vfs()
            self.assertTrue(vfs.VFS_INITIALIZED)
            self.assertEqual(vfs.get_embedded_server_config(),
                             {"host": "official.example", "port": 13000})
            # Nothing materialized at startup except lazily on access.
            self.assertEqual(sorted(os.listdir(fixture.cache)), [])
            path = os.path.join(consts.SOUNDPREPEND, "ui/warn.ogg")
            self.assertTrue(os.path.exists(path))
            self.assertTrue(os.path.isfile(path))
            with open(path, "rb") as handle:
                self.assertEqual(handle.read(), fixture.files["ui/warn.ogg"])
        finally:
            os.chdir(old_cwd)
            vfs._reset_for_tests()
            fixture.close()


if __name__ == "__main__":
    unittest.main()