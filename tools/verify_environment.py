"""Non-destructive health check for the Beyond Tournament development setup."""

from __future__ import annotations

import importlib
import os
import platform
import sys
from pathlib import Path


SUPPORTED_PYTHON = {(3, 11), (3, 12)}
IMPORT_CHECKS = (
    ("pygame", "game input and window support"),
    ("cyal", "OpenAL spatial audio bindings"),
    ("pyogg", "Ogg and Opus audio support"),
    ("enet", "multiplayer networking"),
    ("accessible_output2", "screen-reader output"),
    ("requests", "HTTP support"),
    ("cryptography", "encrypted settings"),
    ("psutil", "process monitoring"),
    ("pyperclip", "clipboard support"),
    ("semver", "version comparison"),
    ("urlextract", "URL extraction"),
    ("linkpreview", "link previews"),
    ("yt_dlp", "music downloads"),
    ("nuitka", "client compilation"),
    ("zstandard", "Nuitka compression support"),
    ("audioop", "voice-chat audio conversion"),
)
REQUIRED_PROJECT_PATHS = (
    "beyond_tournament.py",
    "CyalPlugin.py",
    "data",
    "dlls_windows",
    "ffmpeg.exe",
    "openal.dll",
)


def main() -> int:
    os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
    project_dir = Path(__file__).resolve().parent.parent
    failures: list[str] = []

    print("[CHECK] Python runtime")
    version = sys.version_info[:2]
    print(f"        Executable: {sys.executable}")
    print(f"        Version:    {platform.python_version()}")
    print(f"        Platform:   {platform.system()} {platform.machine()}")
    if version not in SUPPORTED_PYTHON:
        failures.append(
            f"Python {version[0]}.{version[1]} is not in the tested 3.11-3.12 range."
        )
    if platform.system() != "Windows" or platform.architecture()[0] != "64bit":
        failures.append("The automated build environment requires 64-bit Windows.")

    print("[CHECK] Required Python modules")
    for module_name, purpose in IMPORT_CHECKS:
        try:
            importlib.import_module(module_name)
        except Exception as exc:  # Report optional DLL/import failures precisely.
            failures.append(f"{module_name}: {type(exc).__name__}: {exc}")
            print(f"        [FAIL] {module_name} - {purpose}")
        else:
            print(f"        [PASS] {module_name} - {purpose}")

    print("[CHECK] Required project files")
    for relative_path in REQUIRED_PROJECT_PATHS:
        path = project_dir / relative_path
        if path.exists():
            print(f"        [PASS] {relative_path}")
        else:
            failures.append(f"Missing project file: {relative_path}")
            print(f"        [FAIL] {relative_path}")

    if failures:
        print("\n[FAILED] Environment health check found these problems:")
        for index, failure in enumerate(failures, start=1):
            print(f"        {index}. {failure}")
        return 1

    print("\n[SUCCESS] Python, libraries, accessibility support, and build files are ready.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
