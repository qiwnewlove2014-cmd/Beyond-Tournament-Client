"""Copy dynamic runtime packages and validate a compiled Client package."""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_DIR = PROJECT_DIR / "Beyond Tournament"


class PackageValidationError(RuntimeError):
    pass


def _files_below(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    return [path for path in root.rglob("*") if path.is_file()]


def _yt_dlp_source() -> Path:
    import yt_dlp

    return Path(yt_dlp.__file__).resolve().parent


def copy_yt_dlp(output_dir: Path, source_dir: Path | None = None) -> None:
    source = (source_dir or _yt_dlp_source()).resolve()
    target = output_dir / "yt_dlp"
    if not source.is_dir() or not (source / "__init__.py").is_file():
        raise PackageValidationError(f"yt-dlp source package is invalid: {source}")

    print(f"[PACKAGE] Copying yt-dlp from {source}")
    shutil.copytree(source, target, dirs_exist_ok=True, copy_function=shutil.copy2)


def _compare_tree(
    label: str,
    source: Path,
    target: Path,
    failures: list[str],
) -> None:
    source_files = _files_below(source)
    if not source_files:
        failures.append(f"{label}: source tree is missing or empty: {source}")
        return

    mismatches: list[str] = []
    for source_file in source_files:
        relative = source_file.relative_to(source)
        target_file = target / relative
        if not target_file.is_file():
            mismatches.append(f"missing {relative}")
        elif target_file.stat().st_size != source_file.stat().st_size:
            mismatches.append(f"size mismatch {relative}")

    if mismatches:
        preview = "; ".join(mismatches[:10])
        if len(mismatches) > 10:
            preview += f"; and {len(mismatches) - 10} more"
        failures.append(f"{label}: {preview}")
    else:
        print(
            f"        [PASS] {label}: "
            f"{len(source_files)} files match the source tree"
        )


def _require_file(
    label: str,
    path: Path,
    failures: list[str],
    minimum_size: int = 1,
) -> None:
    if not path.is_file():
        failures.append(f"{label}: missing {path}")
    elif path.stat().st_size < minimum_size:
        failures.append(f"{label}: file is unexpectedly small: {path}")
    else:
        print(f"        [PASS] {label}: {path.name}")


def _require_glob(
    label: str,
    root: Path,
    pattern: str,
    failures: list[str],
) -> None:
    matches = [path for path in root.glob(pattern) if path.is_file()]
    if not matches:
        failures.append(f"{label}: no files match {root / pattern}")
    else:
        print(f"        [PASS] {label}: {len(matches)} files")


def _require_tree(
    label: str,
    root: Path,
    minimum_files: int,
    failures: list[str],
) -> None:
    files = _files_below(root)
    if len(files) < minimum_files:
        failures.append(
            f"{label}: expected at least {minimum_files} files under {root}, "
            f"found {len(files)}"
        )
    else:
        print(f"        [PASS] {label}: {len(files)} files")


def verify_package(
    output_dir: Path,
    project_dir: Path = PROJECT_DIR,
    yt_dlp_source: Path | None = None,
) -> None:
    output = output_dir.resolve()
    project = project_dir.resolve()
    failures: list[str] = []

    print(f"[CHECK] Compiled package: {output}")
    if not output.is_dir():
        raise PackageValidationError(f"Compiled package directory is missing: {output}")

    _require_file(
        "Compiled game executable",
        output / "Beyond Tournament.exe",
        failures,
        minimum_size=1_000_000,
    )
    for name in (
        "default_keyconfig.json",
        "changelog.txt",
        "ffmpeg.exe",
        "openal.dll",
        "openal32.dll",
        "enet.pyd",
        "opus.dll",
        "opusenc.dll",
        "opusfile.dll",
        "_tkinter.pyd",
        "tk86t.dll",
        "tcl86t.dll",
        "win32gui.pyd",
    ):
        _require_file(f"Required runtime file {name}", output / name, failures)

    _require_glob("Cyal native audio modules", output / "cyal", "*.pyd", failures)
    _require_glob("Pygame native modules", output / "pygame", "*.pyd", failures)
    _require_glob(
        "Cryptography native module",
        output / "cryptography",
        "**/*.pyd",
        failures,
    )
    _require_file(
        "Screen-reader package",
        output / "accessible_output2" / "lib" / "nvdaControllerClient64.dll",
        failures,
    )
    _require_glob(
        "PyWin32 runtime",
        output,
        "pywintypes*.dll",
        failures,
    )
    _require_tree("Tkinter scripts", output / "tk", 20, failures)
    _require_tree("Tcl scripts", output / "tcl", 100, failures)

    _compare_tree("Game data", project / "data", output / "data", failures)
    _compare_tree(
        "Windows runtime DLL bundle",
        project / "dlls_windows",
        output,
        failures,
    )
    _compare_tree(
        "URL extraction data",
        project / "urlextract",
        output / "urlextract",
        failures,
    )
    _compare_tree(
        "yt-dlp runtime package",
        (yt_dlp_source or _yt_dlp_source()).resolve(),
        output / "yt_dlp",
        failures,
    )

    for source_file in project.glob("*.mhr"):
        _require_file(
            f"HRTF profile {source_file.name}",
            output / source_file.name,
            failures,
            minimum_size=source_file.stat().st_size,
        )
    for source_file in project.glob("*.dll"):
        _require_file(
            f"Project runtime DLL {source_file.name}",
            output / source_file.name,
            failures,
            minimum_size=source_file.stat().st_size,
        )

    if failures:
        details = "\n".join(f"  - {failure}" for failure in failures)
        raise PackageValidationError(
            f"Compiled package validation failed with {len(failures)} problem(s):\n"
            f"{details}"
        )

    print("[SUCCESS] Compiled package contains every required runtime component.")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help="Compiled package directory (default: project/Beyond Tournament)",
    )
    parser.add_argument(
        "--verify-only",
        action="store_true",
        help="Validate without copying yt-dlp first.",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    output = args.output.resolve()
    expected_output = DEFAULT_OUTPUT_DIR.resolve()
    if output != expected_output:
        print(
            f"[FAILED] Refusing an unexpected package path: {output}\n"
            f"         Expected: {expected_output}",
            file=sys.stderr,
        )
        return 1

    try:
        if not args.verify_only:
            copy_yt_dlp(output)
        verify_package(output)
    except (OSError, PackageValidationError) as error:
        print(f"[FAILED] {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
