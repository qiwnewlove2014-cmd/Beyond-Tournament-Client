"""Compatibility entry point for the Beyond Tournament setup console.

The real bootstrapper is PowerShell because it must discover or install Python
before a project virtual environment exists. Keep this wrapper so existing
contributors who run ``python install_libs.py`` are sent through the same safe
setup path as ``install_libs.bat``.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def main() -> int:
    project_dir = Path(__file__).resolve().parent
    setup_script = project_dir / "tools" / "setup_dev_environment.ps1"
    command = [
        "powershell.exe",
        "-NoLogo",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(setup_script),
        *sys.argv[1:],
    ]
    print("[INFO] Opening the unified Beyond Tournament setup console...")
    return subprocess.call(command, cwd=project_dir)


if __name__ == "__main__":
    raise SystemExit(main())
