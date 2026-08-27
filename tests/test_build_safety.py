"""Build safety tests use temporary fixtures only; no build or game imports."""
import contextlib
import copy
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

CLIENT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("bt_build_safety", CLIENT / "tools/build_safety.py")
safety = importlib.util.module_from_spec(spec)
spec.loader.exec_module(safety)


def fake_pe(tag):
    data = bytearray(128)
    data[:2] = b"MZ"
    data[60:64] = (80).to_bytes(4, "little")
    data[80:84] = b"PE\0\0"
    data[100:100 + len(tag)] = tag
    return bytes(data)


class BuildSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="bt-build-guard-test-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.project = self.base / "client"
        self.project.mkdir()
        self.entries = {}
        for name in safety.HELPERS:
            data = fake_pe(name.encode("ascii"))
            self.write(self.project / name, data)
            self.entries[name] = {"size": len(data), "sha256": hashlib.sha256(data).hexdigest(),
                                  "source_url": "https://example.invalid/fixture"}

    def write(self, path, data=b"fixture"):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def manifest(self):
        return {"schema_version": 1, "files": copy.deepcopy(self.entries)}

    def project_fixture(self):
        for name in ("data", "dlls_windows", "urlextract", "libs", "third_party"):
            self.write(self.project / name / "fixture.txt")
        for name in ("beyond_tournament.py", "CyalPlugin.py", "default_keyconfig.json", "tools/pack_data.py",
                     "profile.mhr", "openal.dll"):
            self.write(self.project / name)
        for name in safety.PLAYER_PATCH_NOTES:
            self.write(self.base / "server/docs" / name, ("Public notes: " + name).encode())

    def package_fixture(self):
        package = self.project / safety.STAGING_NAME
        for name in safety.HELPERS:
            self.write(package / name, (self.project / name).read_bytes())
        for name in ("Beyond Tournament.exe", "sounds.dat", "default_keyconfig.json", *safety.PLAYER_PATCH_NOTES,
                     "openal.dll", "opus.dll", "third_party/ffmpeg/LICENSE.txt", "yt_dlp/__init__.py",
                     "urlextract/__init__.py"):
            self.write(package / name)
        return package

    def snapshot(self):
        return {str(path.relative_to(self.base)): path.read_bytes()
                for path in self.base.rglob("*") if path.is_file()}

    def test_real_manifest_and_blocklist_are_well_formed(self):
        self.assertEqual(set(safety.load_manifest()), safety.HELPERS)
        for value in safety.BLOCKED_HASHES:
            self.assertRegex(value, r"^[0-9a-f]{64}$")

    def test_valid_helpers(self):
        safety.verify_helpers(self.project, self.entries)

    def test_missing_helper(self):
        (self.project / "ffmpeg.exe").unlink()
        with self.assertRaises(safety.BuildSafetyError):
            safety.verify_helpers(self.project, self.entries)

    def test_same_size_modified_helper(self):
        self.write(self.project / "ffmpeg.exe", fake_pe(b"modified"))
        with self.assertRaises(safety.BuildSafetyError):
            safety.verify_helpers(self.project, self.entries)

    def test_wrong_size_helper(self):
        self.write(self.project / "ffmpeg.exe", b"MZ")
        with self.assertRaises(safety.BuildSafetyError):
            safety.verify_helpers(self.project, self.entries)

    def test_manifest_cannot_make_zip_a_valid_executable(self):
        for payload in (b"PK\x03\x04" + bytes(124), b"MZ" + bytes(126),
                        fake_pe(b"x")[:80] + b"NOPE" + bytes(44)):
            with self.subTest(payload=payload[:4]):
                self.write(self.project / "ffmpeg.exe", payload)
                self.entries["ffmpeg.exe"].update(size=len(payload), sha256=hashlib.sha256(payload).hexdigest())
                with self.assertRaises(safety.BuildSafetyError):
                    safety.verify_helpers(self.project, self.entries)

    def test_unresolved_binary_names_and_quarantine_are_rejected(self):
        for name in ("app$.exe", "app$.EXE", "library$.dll", "fixture.blocked", "hidden/tool$.pyd"):
            with self.subTest(name=name):
                path = self.write(self.project / name)
                with self.assertRaises(safety.BuildSafetyError):
                    safety.inspect_file(path)
                path.unlink()

    def test_dollar_in_ordinary_document_is_rejected_in_package(self):
        with self.assertRaises(safety.BuildSafetyError):
            safety.inspect_file(self.write(self.project / "cost$.txt"))

    def test_known_hash_detected_under_another_extension(self):
        data = b"known bad fixture"
        path = self.write(self.project / "renamed.bin", data)
        with mock.patch.object(safety, "BLOCKED_SAMPLE_SIZE", len(data)), mock.patch.object(
                safety, "BLOCKED_HASHES", {hashlib.sha256(data).hexdigest()}):
            with self.assertRaises(safety.BuildSafetyError):
                safety.inspect_file(path)

    def test_autoplay_markers_detected_across_chunk_boundary(self):
        data = b"x" * (1024 * 1024 - 7) + safety.AUTOPLAY_MARKERS[0] + b"x" + safety.AUTOPLAY_MARKERS[1]
        with self.assertRaises(safety.BuildSafetyError):
            safety.inspect_file(self.write(self.project / "renamed.exe", data))

    def test_single_marker_is_not_sufficient(self):
        safety.inspect_file(self.write(self.project / "ordinary.exe", safety.AUTOPLAY_MARKERS[0]))

    def test_scan_checks_nested_files(self):
        self.write(self.project / "nested/hidden/app$.exe")
        with self.assertRaises(safety.BuildSafetyError):
            safety.scan_tree(self.project)

    def test_missing_directory_fails(self):
        with self.assertRaises(safety.BuildSafetyError):
            safety.scan_tree(self.project / "absent")

    def test_walk_error_is_not_silently_ignored(self):
        def fail_walk(root, followlinks, onerror):
            onerror(PermissionError("fixture access denied"))
            return []
        with mock.patch.object(safety.os, "walk", side_effect=fail_walk):
            with self.assertRaises(PermissionError):
                safety.scan_tree(self.project)

    def test_reparse_point_and_symlink_attributes_are_rejected(self):
        for mode, attrs in ((stat.S_IFDIR, 0x400), (stat.S_IFLNK, 0)):
            with self.subTest(mode=mode, attrs=attrs), mock.patch.object(
                    Path, "lstat", return_value=types.SimpleNamespace(st_mode=mode, st_file_attributes=attrs)):
                with self.assertRaises(safety.BuildSafetyError):
                    safety.checked_path(self.project)

    def test_parent_reparse_point_is_rejected(self):
        original = Path.lstat
        def patched(path, *args, **kwargs):
            if path == self.project:
                return types.SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=0x400)
            return original(path, *args, **kwargs)
        with mock.patch.object(Path, "lstat", patched):
            with self.assertRaises(safety.BuildSafetyError):
                safety.checked_path(self.project / "absent/output.exe")

    def test_manifest_schema_validation(self):
        invalid = [[], {}, {"schema_version": True, "files": self.entries}]
        for field, value in (("size", True), ("size", 0), ("sha256", "x"), ("source_url", "http://insecure.invalid")):
            manifest = self.manifest()
            manifest["files"]["ffmpeg.exe"][field] = value
            invalid.append(manifest)
        manifest = self.manifest()
        manifest["files"]["../ffmpeg.exe"] = manifest["files"].pop("ffmpeg.exe")
        invalid.append(manifest)
        for manifest in invalid:
            with self.subTest(manifest=manifest):
                path = self.write(self.base / "manifest.json", json.dumps(manifest).encode())
                with self.assertRaises(safety.BuildSafetyError):
                    safety.load_manifest(path)

    def test_invalid_json_main_fails_closed(self):
        with mock.patch.object(safety, "load_manifest", side_effect=ValueError("bad JSON")), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(safety.main(["preflight"]), 1)

    def test_project_validation_is_read_only(self):
        self.project_fixture()
        before = self.snapshot()
        safety.validate_project(self.project, self.entries)
        self.assertEqual(before, self.snapshot())

    def test_staging_leftovers_block_without_deleting(self):
        self.project_fixture()
        self.write(self.project / safety.STAGING_NAME / "leftover.txt")
        before = self.snapshot()
        with self.assertRaises(safety.BuildSafetyError):
            safety.validate_project(self.project, self.entries)
        self.assertEqual(before, self.snapshot())

    def test_old_output_unresolved_binary_blocks_without_deleting(self):
        self.project_fixture()
        self.write(self.project / safety.OUTPUT_NAME / "app$.exe")
        before = self.snapshot()
        with self.assertRaises(safety.BuildSafetyError):
            safety.validate_project(self.project, self.entries)
        self.assertEqual(before, self.snapshot())

    def test_valid_package(self):
        safety.verify_package(self.package_fixture(), self.entries)

    def test_preflight_requires_both_server_notes_without_legacy_fallback(self):
        self.project_fixture()
        self.write(self.base / "server/changelog.txt", b"Technical notes")
        for name in safety.PLAYER_PATCH_NOTES:
            with self.subTest(name=name):
                self.write(self.project / name, b"Stale client copy")
                path = self.base / "server/docs" / name
                saved = path.read_bytes()
                path.unlink()
                before = self.snapshot()
                with self.assertRaisesRegex(safety.BuildSafetyError, "Player patch notes are missing"):
                    safety.validate_project(self.project, self.entries)
                self.assertEqual(before, self.snapshot())
                self.write(path, saved)

    def test_source_and_package_notes_reject_empty_or_invalid_utf8_read_only(self):
        self.project_fixture()
        package = self.package_fixture()
        for name in safety.PLAYER_PATCH_NOTES:
            for invalid in (b"", b" \r\n\t", b"\xff"):
                for root in (self.base / "server/docs", package):
                    with self.subTest(name=name, invalid=invalid, root=root):
                        path = root / name
                        saved = path.read_bytes()
                        self.write(path, invalid)
                        before = self.snapshot()
                        with self.assertRaises(safety.BuildSafetyError):
                            safety.validate_player_notes(root)
                        self.assertEqual(before, self.snapshot())
                        self.write(path, saved)

    def test_final_package_requires_both_languages(self):
        package = self.package_fixture()
        for name in safety.PLAYER_PATCH_NOTES:
            with self.subTest(name=name):
                path = package / name
                saved = path.read_bytes()
                path.unlink()
                with self.assertRaisesRegex(safety.BuildSafetyError, "Player patch notes are missing"):
                    safety.verify_package(package, self.entries)
                self.write(path, saved)

    def test_final_package_rejects_game_changelog_but_keeps_dependency_licenses(self):
        package = self.package_fixture()
        self.write(package / "third_party/ffmpeg/changelog.txt", b"Dependency history")
        safety.verify_package(package, self.entries)
        unwanted = self.write(package / "CHANGELOG.TXT", b"Technical game history")
        before = self.snapshot()
        with self.assertRaisesRegex(safety.BuildSafetyError, "technical game changelog"):
            safety.verify_package(package, self.entries)
        self.assertEqual(before, self.snapshot())
        self.assertTrue(unwanted.is_file())

    def test_old_output_changelog_does_not_block_new_clean_build(self):
        self.project_fixture()
        self.write(self.project / safety.OUTPUT_NAME / "changelog.txt", b"Previous build")
        before = self.snapshot()
        safety.validate_project(self.project, self.entries)
        self.assertEqual(before, self.snapshot())

    def test_copy_rejects_compiler_game_changelog_without_modifying_originals(self):
        self.project_fixture()
        self.write(self.project / safety.STAGING_NAME / "sounds.dat")
        original = self.write(self.project / safety.DIST_NAME / "changelog.txt", b"Technical history")
        with self.assertRaisesRegex(safety.BuildSafetyError, "technical game changelog"):
            safety.copy_inputs(self.project, self.entries)
        self.assertEqual(original.read_bytes(), b"Technical history")
        self.assertFalse((self.project / safety.STAGING_NAME / "changelog.txt").exists())

    def test_missing_encrypted_assets_blocks_package(self):
        package = self.package_fixture()
        (package / "sounds.dat").unlink()
        with self.assertRaises(safety.BuildSafetyError):
            safety.verify_package(package, self.entries)

    def test_raw_assets_block_package(self):
        package = self.package_fixture()
        self.write(package / "data/private.ogg")
        with self.assertRaises(safety.BuildSafetyError):
            safety.verify_package(package, self.entries)

    def test_invalid_package_preserves_previous_output(self):
        package = self.package_fixture()
        self.write(self.project / safety.OUTPUT_NAME / "previous.txt")
        (package / "oalinst.exe").unlink()
        before = self.snapshot()
        with self.assertRaises(safety.BuildSafetyError):
            safety.publish(self.project, self.entries)
        self.assertEqual(before, self.snapshot())

    def test_publish_touches_only_generated_fixture_directories(self):
        package = self.package_fixture()
        self.write(self.project / safety.OUTPUT_NAME / "previous.txt")
        self.write(self.project / safety.DIST_NAME / "beyond_tournament.exe")
        sentinel = self.write(self.base / "untouched.txt")
        source_before = (self.project / "ffmpeg.exe").read_bytes()
        safety.publish(self.project, self.entries)
        self.assertFalse(package.exists())
        self.assertFalse((self.project / safety.DIST_NAME).exists())
        self.assertTrue((self.project / safety.OUTPUT_NAME / "sounds.dat").is_file())
        self.assertEqual(sentinel.read_bytes(), b"fixture")
        self.assertEqual((self.project / "ffmpeg.exe").read_bytes(), source_before)

    def test_publish_refuses_output_link(self):
        self.package_fixture()
        outside = self.base / "outside"
        sentinel = self.write(outside / "keep.txt")
        link = self.project / safety.OUTPUT_NAME
        try:
            link.symlink_to(outside, target_is_directory=True)
        except OSError:
            self.skipTest("Creating symlinks requires Windows developer mode or privilege")
        self.addCleanup(link.unlink)
        with self.assertRaises(safety.BuildSafetyError):
            safety.publish(self.project, self.entries)
        self.assertEqual(sentinel.read_bytes(), b"fixture")

    @unittest.skipUnless(os.name == "nt", "Windows junction test")
    def test_publish_refuses_windows_directory_junction(self):
        self.package_fixture()
        outside = self.base / "outside-junction-target"
        sentinel = self.write(outside / "keep.txt")
        link = self.project / safety.OUTPUT_NAME
        cmd = str(Path(os.environ["SystemRoot"]) / "System32/cmd.exe")
        result = subprocess.run([cmd, "/d", "/c", "mklink", "/J", str(link), str(outside)], capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        # Only unlink the junction fixture itself, never traverse its target.
        self.addCleanup(os.rmdir, link)
        with self.assertRaises(safety.BuildSafetyError):
            safety.publish(self.project, self.entries)
        self.assertEqual(sentinel.read_bytes(), b"fixture")

    def test_preflight_checks_runtime_without_importing_it(self):
        self.project_fixture()
        root = self.base / "Python/Lib/site-packages"
        self.write(root / "yt_dlp/__init__.py", b"raise RuntimeError('do not import')")
        self.write(root.parent.parent / "Scripts/entry.py", b"raise RuntimeError('do not run')")
        before = self.snapshot()
        with mock.patch.object(safety, "PROJECT", self.project), mock.patch.object(
                safety, "load_manifest", return_value=self.entries), mock.patch.object(
                safety, "runtime_site_dirs", return_value=[root]), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(safety.main(["preflight"]), 0)
        self.assertEqual(before, self.snapshot())

    def test_preflight_skips_unresolved_runtime_binary_without_deleting(self):
        self.project_fixture()
        root = self.base / "Python/Lib/site-packages"
        self.write(root / "yt_dlp/__init__.py")
        self.write(root.parent.parent / "Scripts/pip$.exe")
        before = self.snapshot()
        with mock.patch.object(safety, "PROJECT", self.project), mock.patch.object(
                safety, "load_manifest", return_value=self.entries), mock.patch.object(
                safety, "runtime_site_dirs", return_value=[root]), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(safety.main(["preflight"]), 0)
        self.assertEqual(before, self.snapshot())

    def test_runtime_discovery_and_copy_do_not_import_package(self):
        root = self.base / "site-packages"
        source = self.write(root / "yt_dlp/__init__.py", b"raise RuntimeError('must never execute')")
        self.assertEqual(safety.yt_dlp_source([root]), source.parent)
        (self.project / safety.STAGING_NAME).mkdir()
        safety.copy_runtime(self.project, [root])
        self.assertEqual((self.project / safety.STAGING_NAME / "yt_dlp/__init__.py").read_bytes(), source.read_bytes())

    def test_runtime_copy_requires_staging(self):
        root = self.base / "site-packages"
        self.write(root / "yt_dlp/__init__.py")
        with self.assertRaises(safety.BuildSafetyError):
            safety.copy_runtime(self.project, [root])

    def test_runtime_copy_excludes_unresolved_files_without_deleting(self):
        root = self.base / "site-packages"
        self.write(root / "yt_dlp/__init__.py")
        self.write(root / "yt_dlp/tool$.exe")
        (self.project / safety.STAGING_NAME).mkdir()
        safety.copy_runtime(self.project, [root])
        self.assertTrue((root / "yt_dlp/tool$.exe").is_file())
        self.assertTrue((self.project / safety.STAGING_NAME / "yt_dlp/__init__.py").is_file())
        self.assertFalse((self.project / safety.STAGING_NAME / "yt_dlp/tool$.exe").exists())

    def test_input_scan_does_not_inspect_dollar_files_or_descend_into_dollar_dirs(self):
        kept = self.write(self.project / "assets/good.ogg")
        self.write(self.project / "assets/old$.exe", b"must not inspect")
        self.write(self.project / "assets/old$/bad.blocked", b"must not inspect")
        seen = []
        with mock.patch.object(safety, "inspect_file", side_effect=lambda path: seen.append(path)):
            safety.scan_tree(self.project / "assets", skip_dollar=True)
        self.assertEqual(seen, [kept])

    def test_included_autoplay_and_quarantine_still_block_input_scan(self):
        for name, contents in (("bad.exe", b" ".join(safety.AUTOPLAY_MARKERS)), ("sample.blocked", b"x")):
            with self.subTest(name=name):
                path = self.write(self.project / name, contents)
                with self.assertRaises(safety.BuildSafetyError):
                    safety.scan_tree(self.project, skip_dollar=True)
                path.unlink()

    @unittest.skipUnless(os.name == "nt", "Windows junction exclusion test")
    def test_excluded_junction_is_not_followed_or_copied(self):
        source = self.project / "assets"
        self.write(source / "keep.txt")
        outside = self.base / "excluded-target"
        self.write(outside / "sample.blocked")
        link = source / "old$"
        cmd = str(Path(os.environ["SystemRoot"]) / "System32/cmd.exe")
        result = subprocess.run([cmd, "/d", "/c", "mklink", "/J", str(link), str(outside)], capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.addCleanup(os.rmdir, link)
        destination = self.base / "filtered"
        safety.copy_tree_filtered(source, destination)
        self.assertEqual([path.name for path in destination.iterdir()], ["keep.txt"])
        self.assertTrue((outside / "sample.blocked").exists())

    def test_project_validation_skips_root_and_asset_dollar_names_read_only(self):
        self.project_fixture()
        for name in ("ffmpeg$.exe", "oalinst$.exe", "extra$.dll", "data/old$.ogg", "data/archive$/sample.blocked",
                     "dlls_windows/extra$.dll", "urlextract/archive$/untrusted.exe"):
            self.write(self.project / name, b" ".join(safety.AUTOPLAY_MARKERS))
        before = self.snapshot()
        safety.validate_project(self.project, self.entries)
        self.assertEqual(before, self.snapshot())

    def test_dollar_original_is_not_a_fallback_for_missing_approved_helper(self):
        original = self.project / "ffmpeg.exe"
        original.rename(self.project / "ffmpeg$.exe")
        with self.assertRaises(safety.BuildSafetyError):
            safety.verify_helpers(self.project, self.entries)

    def test_final_package_rejects_dollar_file_and_empty_directory(self):
        package = self.package_fixture()
        unwanted = self.write(package / "notes$.txt")
        with self.assertRaises(safety.BuildSafetyError):
            safety.verify_package(package, self.entries)
        unwanted.unlink()
        (package / "empty$").mkdir()
        with self.assertRaises(safety.BuildSafetyError):
            safety.verify_package(package, self.entries)

    def test_all_package_copies_filter_dollar_names_and_preserve_sources(self):
        self.project_fixture()
        self.write(self.project / "opus.dll")
        self.write(self.project / "third_party/ffmpeg/LICENSE.txt")
        self.write(self.project / "tools/build_binary_manifest.json", json.dumps(self.manifest()).encode())
        self.write(self.project / safety.DIST_NAME / "beyond_tournament.exe")
        self.write(self.project / safety.STAGING_NAME / "sounds.dat")
        self.write(self.base / "server/changelog.txt", b"Technical server history")
        self.write(self.project / "changelog.txt", b"Legacy client history")
        for name in safety.PLAYER_PATCH_NOTES:
            self.write(self.project / name, b"Stale client notes")
            self.write(self.project / safety.DIST_NAME / name, b"Stale compiler notes")
        runtime = self.base / "site-packages"
        self.write(runtime / "yt_dlp/__init__.py", b"raise RuntimeError('do not import')")
        self.write(runtime / "yt_dlp/old$.exe")
        for name in ("ffmpeg$.exe", "profile$.mhr", "openal$.dll", "dlls_windows/bad$.dll",
                     "dlls_windows/old$/bad.exe", "urlextract/old$.exe", "third_party/old$/readme.txt"):
            self.write(self.project / name, b" ".join(safety.AUTOPLAY_MARKERS))
        before = self.snapshot()
        safety.copy_inputs(self.project, self.entries)
        safety.copy_runtime(self.project, [runtime])
        package = self.project / safety.STAGING_NAME
        safety.verify_package(package, self.entries)
        self.assertFalse((package / "changelog.txt").exists())
        for name in safety.PLAYER_PATCH_NOTES:
            self.assertEqual((package / name).read_bytes(), (self.base / "server/docs" / name).read_bytes())
        self.assertFalse(any("$" in str(path.relative_to(package)) for path in package.rglob("*")))
        for relative, contents in before.items():
            self.assertEqual((self.base / relative).read_bytes(), contents)

    def test_copy_failure_propagates_and_leaves_source(self):
        source = self.write(self.project / "readme.txt")
        with mock.patch.object(safety.shutil, "copy2", side_effect=PermissionError("fixture")):
            with self.assertRaises(PermissionError):
                safety.copy_checked_file(source, self.base / "copy.txt")
        self.assertEqual(source.read_bytes(), b"fixture")

    def test_cyal_plugin_does_not_select_dollar_dlls(self):
        root = self.base / "cyal"
        kept = self.write(root / "openal.dll")
        self.write(root / "openal$.dll")
        fake_plugin = types.ModuleType("nuitka.plugins.PluginBase")
        fake_plugin.NuitkaPluginBase = object
        plugin_spec = importlib.util.spec_from_file_location("fixture_cyal_plugin", CLIENT / "CyalPlugin.py")
        plugin = importlib.util.module_from_spec(plugin_spec)
        with mock.patch.dict(sys.modules, {"nuitka": types.ModuleType("nuitka"),
                                          "nuitka.plugins": types.ModuleType("nuitka.plugins"),
                                          "nuitka.plugins.PluginBase": fake_plugin}):
            plugin_spec.loader.exec_module(plugin)
        module = types.SimpleNamespace(getCompileTimeDirectory=lambda: str(root))
        self.assertEqual([Path(path) for path in plugin.get_libraries(module)], [kept])

    def test_batch_checks_before_writes_and_before_success(self):
        batch = (CLIENT / "build.bat").read_text(encoding="utf-8")
        self.assertLess(batch.index("build_safety.py preflight"), batch.index("goto checked"))
        self.assertLess(batch.index("goto checked"), batch.index("copy /Y"))
        self.assertLess(batch.index("build_safety.py publish"), batch.index("echo build complete!"))
        self.assertNotIn("rmdir", batch.lower())
        self.assertNotIn("xcopy", batch.lower())
        self.assertIn("build_safety.py copy-inputs", batch)
        lines = batch.splitlines()
        for index, line in enumerate(lines):
            if line.startswith(("copy /Y", "xcopy ", "ren ")):
                self.assertEqual(lines[index + 1], "if errorlevel 1 goto failed")

    @unittest.skipUnless(os.name == "nt", "Windows batch test")
    def test_batch_failure_waits_for_enter_and_returns_failure(self):
        # Invalid arguments stop before Python, the packer, or the compiler.
        # Run a disposable copy: these tests never build or change game data.
        batch = self.write(self.project / "build.bat", (CLIENT / "build.bat").read_bytes())
        cmd = str(Path(os.environ["SystemRoot"]) / "System32/cmd.exe")
        env = os.environ.copy()
        env.pop("BT_BUILD_NO_PAUSE", None)
        with subprocess.Popen([cmd, "/d", "/c", batch.name, "--invalid-test-option"], cwd=self.project,
                              env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT) as process:
            try:
                with self.assertRaises(subprocess.TimeoutExpired):
                    process.wait(timeout=0.5)
                output, _ = process.communicate(input=b"\r\n", timeout=5)
                self.assertEqual(process.returncode, 1)
                self.assertIn(b"BUILD FAILED.", output)
                self.assertIn(b"Press Enter to close this build window.", output)
                self.assertNotIn(b"checking build inputs", output)
                self.assertEqual(set(self.project.iterdir()),
                                 {batch, *(self.project / name for name in safety.HELPERS)})
            finally:
                if process.poll() is None:
                    process.kill()
                    process.communicate()

    @unittest.skipUnless(os.name == "nt", "Windows batch test")
    def test_batch_automated_failure_does_not_wait_or_report_success(self):
        self.write(self.project / "build.bat", (CLIENT / "build.bat").read_bytes())
        cmd = str(Path(os.environ["SystemRoot"]) / "System32/cmd.exe")
        env = dict(os.environ, BT_BUILD_NO_PAUSE="1")
        with subprocess.Popen([cmd, "/d", "/c", "build.bat", "--invalid-test-option"], cwd=self.project,
                              env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT) as process:
            try:
                self.assertEqual(process.wait(timeout=5), 1)
                output, _ = process.communicate(timeout=5)
                self.assertIn(b"BUILD FAILED.", output)
                self.assertNotIn(b"Press Enter", output)
                self.assertNotIn(b"build complete!", output)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.communicate()


if __name__ == "__main__":
    unittest.main()
