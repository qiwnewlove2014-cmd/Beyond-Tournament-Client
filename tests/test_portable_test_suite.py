"""The suite must behave the same on every machine and every shell.

Every check here exists because of a real failure on a developer machine:

* ``python -m unittest discover -s tests`` was run from Git Bash, which exports
  ``NoDefaultCurrentDirectoryInExePath=1``.  That makes ``cmd.exe`` stop
  searching the working directory, so a test that spawned ``cmd /c build.bat``
  by bare name failed with "'build.bat' is not recognized" while the same test
  passed everywhere else.
* A Windows console carries an ANSI codepage (cp874 on a Thai machine, cp1252
  on a Western one), so text file I/O without an explicit ``encoding=`` reads
  and writes different bytes per machine.

These are cheap to keep fixed and expensive to re-diagnose, so they are pinned
rather than described.  Nothing here runs a command or reads game data.
"""

import ast
from pathlib import Path
import unittest

TESTS = Path(__file__).resolve().parent
TOOLS = TESTS.parent / "tools"
# The publishable copy of the room library lives next to the game (cinema-room/)
# and is read from these machines too, so it is held to the same rules. It may
# not exist (the copy is the owner's to keep or drop), and a missing directory
# is simply not scanned.
PUBLISHED = TESTS.parent.parent / "cinema-room"
SCANNED_DIRECTORIES = (TESTS, TOOLS, PUBLISHED)

SUBPROCESS_SPAWNERS = frozenset({"run", "Popen", "call", "check_call", "check_output"})
UNSAFE_MODULE_CALLS = frozenset({"system", "popen"})


def mode_of(node):
    """File mode of an ``open`` family call, or None when it is not literal.

    ``open(path, mode)`` and ``module.open(path, mode)`` take the mode second;
    ``Path.open(mode)`` — the only form with a single argument — takes it first.
    """
    for keyword in node.keywords:
        if keyword.arg == "mode" and isinstance(keyword.value, ast.Constant):
            return keyword.value.value
    index = 1 if len(node.args) > 1 else 0
    if node.args and isinstance(node.args[index], ast.Constant):
        return node.args[index].value
    return None


def has_encoding(node):
    return any(keyword.arg == "encoding" for keyword in node.keywords)


def opens_a_file(node):
    """True when this call reads a file, rather than a test helper named open()."""
    function = node.func
    if isinstance(function, ast.Name):
        return function.id == "open"
    if not isinstance(function, ast.Attribute) or function.attr != "open":
        return False
    receiver = function.value
    if isinstance(receiver, ast.Name) and receiver.id in {"self", "cls"} and len(node.args) >= 2:
        return False
    return True


def text_io_without_encoding(source):
    """Text file reads/writes that inherit the machine's default encoding."""
    offenders = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call) or has_encoding(node):
            continue
        function = node.func
        name = function.attr if isinstance(function, ast.Attribute) else getattr(function, "id", "")
        if name in {"read_text", "write_text"}:
            offenders.append(name)
        elif name == "open" and opens_a_file(node):
            mode = mode_of(node)
            if mode is None or "b" not in str(mode):
                offenders.append(f"open(mode={mode!r})")
    return offenders


def shell_dependencies(source):
    """Commands that resolve through the shell, PATH or the working directory."""
    offenders = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        owner = node.func.value
        module = owner.id if isinstance(owner, ast.Name) else getattr(owner, "attr", "")
        name = node.func.attr
        if module == "os" and name in UNSAFE_MODULE_CALLS:
            offenders.append(f"os.{name}()")
        if module != "subprocess" or name not in SUBPROCESS_SPAWNERS:
            continue
        if any(keyword.arg == "shell" and getattr(keyword.value, "value", False) for keyword in node.keywords):
            offenders.append(f"subprocess.{name}(shell=True)")
        if node.args and isinstance(node.args[0], ast.List) and node.args[0].elts:
            first = node.args[0].elts[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                if not Path(first.value).is_absolute():
                    offenders.append(f"subprocess.{name}([{first.value!r}, ...])")
    return offenders


def shown(path):
    """A short name for a scanned file, for a readable failure message.

    Scanned files can live outside the client tree (the publishable copy), and
    ``relative_to`` would raise rather than shorten those.
    """
    try:
        return path.relative_to(TESTS.parent)
    except ValueError:
        return path


class PortableSuiteTests(unittest.TestCase):
    def test_no_text_file_io_without_an_explicit_encoding(self):
        offenders = []
        for path in self.sources():
            for offence in text_io_without_encoding(path.read_text(encoding="utf-8")):
                offenders.append(f"{shown(path)}: {offence}")
        self.assertEqual(
            offenders, [],
            "name the encoding (encoding=\"utf-8\") so the bytes do not depend on the "
            "machine's ANSI codepage",
        )

    def test_no_command_resolved_through_path_shell_or_working_directory(self):
        offenders = []
        for path in self.sources():
            for offence in shell_dependencies(path.read_text(encoding="utf-8")):
                offenders.append(f"{shown(path)}: {offence}")
        self.assertEqual(
            offenders, [],
            "spawn an absolute program path and pass argv as a list: a bare batch name is "
            "not found when the shell disables the current-directory search, and shell "
            "strings break on paths holding spaces or non-ASCII characters",
        )

    def test_the_guard_rejects_the_patterns_it_claims_to(self):
        self.assertEqual(text_io_without_encoding("open('a.txt', 'r')\n"), ["open(mode='r')"])
        self.assertEqual(text_io_without_encoding("path.read_text()\n"), ["read_text"])
        self.assertEqual(text_io_without_encoding("path.write_text('x')\n"), ["write_text"])
        self.assertEqual(text_io_without_encoding("open('a.bin', 'rb')\n"), [])
        self.assertEqual(text_io_without_encoding("open('a.txt', encoding='utf-8')\n"), [])
        self.assertEqual(text_io_without_encoding("path.read_text(encoding='utf-8')\n"), [])
        self.assertEqual(text_io_without_encoding("path.open('r')\n"), ["open(mode='r')"])
        self.assertEqual(text_io_without_encoding("wave.open(path, 'rb')\n"), [])
        self.assertEqual(text_io_without_encoding("self.open('event', 'en')\n"), [])
        self.assertEqual(text_io_without_encoding("self.path.open('r')\n"), ["open(mode='r')"])

        self.assertEqual(shell_dependencies("os.system('x')\n"), ["os.system()"])
        self.assertEqual(shell_dependencies("subprocess.run('x', shell=True)\n"),
                         ["subprocess.run(shell=True)"])
        self.assertEqual(shell_dependencies("subprocess.Popen(['tool.bat'])\n"),
                         ["subprocess.Popen(['tool.bat', ...])"])
        self.assertEqual(shell_dependencies("subprocess.Popen([cmd, '--check'])\n"), [])
        self.assertEqual(shell_dependencies("subprocess.Popen([sys.executable, '-c', body])\n"), [])
        self.assertEqual(shell_dependencies("helper.open('a', 'r')\n"), [])

    def sources(self):
        return sorted(
            path
            for directory in SCANNED_DIRECTORIES
            if directory.is_dir()
            for path in directory.rglob("*.py")
        )


if __name__ == "__main__":
    unittest.main()
