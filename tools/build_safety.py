"""Read-only build checks, plus promotion of a fully checked staging directory.

Run with Python -I -S: no game imports, sitecustomize, or candidate EXE execution.
This is a packaging integrity guard, not an antivirus or a clean-host guarantee.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import sys
import sysconfig

PROJECT = Path(__file__).resolve().parents[1]
MANIFEST = PROJECT / "tools" / "build_binary_manifest.json"
STAGING_NAME = "Beyond Tournament.pending"
OUTPUT_NAME = "Beyond Tournament"
DIST_NAME = "beyond_tournament.dist"
HELPERS = frozenset({"ffmpeg.exe", "oalinst.exe"})
PLAYER_PATCH_NOTES = ("player_patch_notes.txt", "player_patch_notes_th.txt")
EXECUTABLE_SUFFIXES = frozenset({".exe", ".dll", ".pyd", ".com", ".scr", ".cpl", ".msi"})
BLOCKED_HASHES = frozenset({
    "b003c197eee574a1a0b1038b364fccbbbdd6245d0f7d77b75ff4067e7658d769",
})
BLOCKED_SAMPLE_SIZE = 1_797_464
AUTOPLAY_MARKERS = tuple(value.encode("utf-16le") for value in ("AutoPlay Application", "ams_launch.exe"))


class BuildSafetyError(RuntimeError):
    pass


def checked_path(path: Path) -> Path:
    """Reject symlinks/junctions BEFORE resolving or walking a path."""
    path = Path(os.path.abspath(path))
    for candidate in (path, *path.parents):
        try:
            info = candidate.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise BuildSafetyError(f"Link/reparse point is not allowed in build inputs: {candidate}")
    return path


def inspect_file(path: Path) -> str | None:
    path = checked_path(path)
    info = path.stat()
    if not stat.S_ISREG(info.st_mode):
        raise BuildSafetyError(f"Not a regular build input: {path}")
    suffix = path.suffix.lower()
    if suffix == ".blocked" or "$" in path.name:
        raise BuildSafetyError(f"Unresolved/quarantined binary must not enter a build: {path}")
    if suffix not in EXECUTABLE_SUFFIXES and info.st_size != BLOCKED_SAMPLE_SIZE:
        return None
    digest = hashlib.sha256()
    seen = [False] * len(AUTOPLAY_MARKERS)
    tail = b""
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
            window = tail + chunk
            for index, marker in enumerate(AUTOPLAY_MARKERS):
                seen[index] |= marker in window
            tail = window[-max(map(len, AUTOPLAY_MARKERS)):]
    value = digest.hexdigest()
    if value in BLOCKED_HASHES or all(seen):
        raise BuildSafetyError(f"Known replacement/AutoPlay wrapper detected: {path}")
    return value


def scan_tree(root: Path, *, skip_dollar: bool = False) -> None:
    root = checked_path(root)
    if not root.is_dir():
        raise BuildSafetyError(f"Required input directory is missing: {root}")
    def walk_error(error: OSError) -> None:
        raise error
    for directory, names, files in os.walk(root, followlinks=False, onerror=walk_error):
        if skip_dollar:
            # Prune before inspecting/traversing excluded directories, including links.
            names[:] = [name for name in names if "$" not in name]
        for name in names:
            if "$" in name:
                raise BuildSafetyError(f"Excluded directory must not enter a package: {Path(directory) / name}")
            checked_path(Path(directory) / name)
        for name in files:
            if skip_dollar and "$" in name:
                continue
            inspect_file(Path(directory) / name)


def load_manifest(path: Path = MANIFEST) -> dict:
    manifest = json.loads(checked_path(path).read_text(encoding="utf-8"))
    if (not isinstance(manifest, dict) or type(manifest.get("schema_version")) is not int
            or manifest["schema_version"] != 1):
        raise BuildSafetyError("Unsupported or invalid binary manifest")
    entries = manifest.get("files")
    if not isinstance(entries, dict) or set(entries) != HELPERS:
        raise BuildSafetyError("Binary manifest must pin exactly ffmpeg.exe and oalinst.exe")
    for name, entry in entries.items():
        if (not isinstance(entry, dict)
                or not isinstance(entry.get("size"), int) or isinstance(entry.get("size"), bool)
                or entry["size"] <= 0
                or not isinstance(entry.get("sha256"), str)
                or not re.fullmatch(r"[0-9a-f]{64}", entry["sha256"])
                or not str(entry.get("source_url", "")).startswith("https://")):
            raise BuildSafetyError(f"Invalid trusted binary manifest entry: {name}")
    return entries


def verify_helpers(root: Path, entries: dict) -> None:
    for name in sorted(HELPERS):
        path = checked_path(root / name)
        if not path.is_file():
            raise BuildSafetyError(f"Required verified helper is missing: {path}. Do not rename a $ file blindly.")
        expected = entries[name]
        if path.stat().st_size != expected["size"] or inspect_file(path) != expected["sha256"]:
            raise BuildSafetyError(f"Helper does not match its approved SHA-256/size: {path}")
        with path.open("rb") as handle:
            header = handle.read(64)
            offset = int.from_bytes(header[60:64], "little")
            if len(header) != 64 or header[:2] != b"MZ" or not 64 <= offset <= expected["size"] - 4:
                raise BuildSafetyError(f"Helper is not a Windows PE executable: {path}")
            handle.seek(offset)
            if handle.read(4) != b"PE\0\0":
                raise BuildSafetyError(f"Helper is not a Windows PE executable: {path}")


def runtime_site_dirs() -> list[Path]:
    """Locate isolated-mode package roots without importing third-party packages."""
    executable = checked_path(Path(sys.executable))
    base_site = checked_path(Path(sysconfig.get_path("purelib")))
    possible_env = executable.parent.parent
    config = possible_env / "pyvenv.cfg"
    if executable.parent.name.lower() == "scripts" and config.is_file():
        settings = {}
        for line in checked_path(config).read_text(encoding="utf-8").splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                settings[key.strip().lower()] = value.strip().lower()
        roots = [possible_env / "Lib" / "site-packages"]
        if settings.get("include-system-site-packages") == "true":
            roots.append(base_site)
        return list(dict.fromkeys(checked_path(root) for root in roots))
    return [base_site]


def yt_dlp_source(site_dirs: list[Path]) -> Path:
    for root in site_dirs:
        package = checked_path(root / "yt_dlp")
        if (package / "__init__.py").is_file():
            return package
    raise BuildSafetyError("yt_dlp is missing from the selected isolated Python environment")


def copy_runtime(project: Path, site_dirs: list[Path]) -> None:
    source = yt_dlp_source(site_dirs)
    staging = checked_path(project / STAGING_NAME)
    if not staging.is_dir():
        raise BuildSafetyError("Create the staging directory before copying runtime packages")
    target = checked_path(staging / "yt_dlp")
    copy_tree_filtered(source, target)


def copy_checked_file(source: Path, target: Path) -> str:
    source, target = Path(source), checked_path(Path(target))
    inspect_file(source)
    if "$" in target.name:
        raise BuildSafetyError(f"Excluded destination name: {target}")
    return shutil.copy2(source, target)


def copy_tree_filtered(source: Path, target: Path) -> None:
    """Only copy inspected non-$ inputs; never modify or rename the source."""
    source, target = checked_path(source), checked_path(target)
    scan_tree(source, skip_dollar=True)
    if target.exists():
        scan_tree(target)
    shutil.copytree(source, target, dirs_exist_ok=True,
                    ignore=lambda _directory, names: [name for name in names if "$" in name],
                    copy_function=copy_checked_file)


def validate_player_notes(root: Path) -> None:
    """Require both public note files; never substitute the technical changelog."""
    for name in PLAYER_PATCH_NOTES:
        path = checked_path(root / name)
        if not path.is_file():
            raise BuildSafetyError(f"Player patch notes are missing: {path}")
        inspect_file(path)
        try:
            text = path.read_text(encoding="utf-8-sig")
        except UnicodeError as error:
            raise BuildSafetyError(f"Player patch notes must be UTF-8: {path}") from error
        if not text.strip():
            raise BuildSafetyError(f"Player patch notes are empty: {path}")


def reject_technical_changelog(package: Path) -> None:
    # Only the game's root document is private build input. Do not strip
    # third-party documentation or licenses inside dependency directories.
    for path in package.iterdir():
        if path.name.casefold() == "changelog.txt":
            raise BuildSafetyError(f"Do not package the technical game changelog: {path}")


def copy_inputs(project: Path, entries: dict) -> None:
    staging = checked_path(project / STAGING_NAME)
    if not staging.is_dir():
        raise BuildSafetyError("Create the staging directory before copying build inputs")
    scan_tree(staging)
    reject_technical_changelog(staging)
    notes = checked_path(project.parent / "server" / "docs")
    validate_player_notes(notes)
    verify_helpers(project, entries)
    copy_tree_filtered(project / "dlls_windows", staging)
    for pattern in ("*.mhr", "*.dll"):
        files = [path for path in project.glob(pattern) if "$" not in path.name]
        if not files:
            raise BuildSafetyError(f"Required non-$ inputs are missing: {pattern}")
        for path in files:
            copy_checked_file(path, staging / path.name)
    for name in ("default_keyconfig.json", *sorted(HELPERS)):
        copy_checked_file(project / name, staging / name)
    # The compiled check remains strict: unexpected $ files in compiler output
    # stop the build, rather than being shipped or automatically deleted.
    scan_tree(project / DIST_NAME)
    reject_technical_changelog(project / DIST_NAME)
    copy_tree_filtered(project / DIST_NAME, staging)
    game = checked_path(staging / "beyond_tournament.exe")
    renamed = checked_path(staging / "Beyond Tournament.exe")
    if renamed.exists():
        raise BuildSafetyError(f"Unexpected existing game executable: {renamed}")
    game.rename(renamed)
    for name in ("urlextract", "third_party"):
        copy_tree_filtered(project / name, staging / name)
    copy_checked_file(project / "tools/build_binary_manifest.json",
                      staging / "third_party/build_binary_manifest.json")
    # Copy last so stale compiler/runtime copies cannot override Server notes.
    for name in PLAYER_PATCH_NOTES:
        copy_checked_file(notes / name, staging / name)
    reject_technical_changelog(staging)


def validate_project(project: Path, entries: dict) -> None:
    project = checked_path(project)
    verify_helpers(project, entries)
    for path in project.iterdir():
        if "$" in path.name:
            continue
        checked_path(path)
        if path.is_file():
            inspect_file(path)
    for name in ("data", "dlls_windows", "urlextract", "libs", "third_party"):
        scan_tree(project / name, skip_dollar=True)
    for name in ("beyond_tournament.py", "CyalPlugin.py", "default_keyconfig.json", "tools/pack_data.py"):
        if not checked_path(project / name).is_file():
            raise BuildSafetyError(f"Required project file is missing: {name}")
    if any(not any("$" not in path.name for path in project.glob(pattern))
           for pattern in ("*.mhr", "*.dll")):
        raise BuildSafetyError("Project HRTF profiles or runtime DLL files are missing")
    validate_player_notes(checked_path(project.parent / "server" / "docs"))
    # Check old generated trees before any build operation can replace them.
    for name in (OUTPUT_NAME, DIST_NAME):
        old = checked_path(project / name)
        if old.exists():
            scan_tree(old)
    pending = checked_path(project / STAGING_NAME)
    if pending.exists():
        raise BuildSafetyError(f"Previous staging directory exists; review it before retrying: {pending}")


def verify_package(package: Path, entries: dict) -> None:
    scan_tree(package)
    reject_technical_changelog(package)
    validate_player_notes(package)
    verify_helpers(package, entries)
    for name in ("Beyond Tournament.exe", "sounds.dat", "default_keyconfig.json", "openal.dll", "opus.dll", "third_party/ffmpeg/LICENSE.txt"):
        path = checked_path(package / name)
        if not path.is_file() or path.stat().st_size == 0:
            raise BuildSafetyError(f"Package file is missing or empty: {path}")
    for name in ("yt_dlp", "urlextract"):
        directory = checked_path(package / name)
        if not directory.is_dir() or not any(directory.iterdir()):
            raise BuildSafetyError(f"Runtime package is missing or empty: {directory}")
    if (package / "data").exists():
        raise BuildSafetyError("Do not ship raw data; retain the encrypted sounds.dat workflow")


def publish(project: Path, entries: dict) -> None:
    """Keep the previous output until staging passes. Never operate outside project."""
    project = checked_path(project)
    staging = checked_path(project / STAGING_NAME)
    output = checked_path(project / OUTPUT_NAME)
    dist = checked_path(project / DIST_NAME)
    verify_package(staging, entries)
    for path in (staging, output, dist):
        if path.parent != project or path == project:
            raise BuildSafetyError(f"Refusing an unexpected generated path: {path}")
    for old in (output, dist):
        if old.exists():
            scan_tree(old)
    # These are the same generated directories replaced by the previous batch.
    # Never use the generated output as storage for personal files/settings.
    # Sources, $ candidates, and external paths are not removed.
    if output.exists():
        shutil.rmtree(output)
    staging.rename(output)
    if dist.exists():
        shutil.rmtree(dist)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("preflight", "copy-inputs", "copy-runtime", "compiled", "package", "publish"))
    args = parser.parse_args(argv)
    try:
        entries = load_manifest()
        if args.action == "preflight":
            print("[INFO] Input files and directories with $ in their names are excluded; originals are left untouched.", flush=True)
            validate_project(PROJECT, entries)
            inspect_file(Path(sys.executable))
            for root in runtime_site_dirs():
                scan_tree(root, skip_dollar=True)
                scripts = root.parent.parent / "Scripts"
                if scripts.is_dir():
                    scan_tree(scripts, skip_dollar=True)
            yt_dlp_source(runtime_site_dirs())
        elif args.action == "copy-inputs":
            copy_inputs(PROJECT, entries)
        elif args.action == "copy-runtime":
            copy_runtime(PROJECT, runtime_site_dirs())
        elif args.action == "compiled":
            scan_tree(PROJECT / DIST_NAME)
            verify_helpers(PROJECT, entries)
            if not (PROJECT / DIST_NAME / "beyond_tournament.exe").is_file():
                raise BuildSafetyError("Nuitka did not produce the expected executable")
        elif args.action == "package":
            verify_package(PROJECT / STAGING_NAME, entries)
        elif args.action == "publish":
            publish(PROJECT, entries)
        print(f"[PASS] Build {args.action} checks passed. This is not a full antivirus scan.")
        return 0
    except (BuildSafetyError, OSError, ValueError) as error:
        print(f"[BUILD BLOCKED] {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
