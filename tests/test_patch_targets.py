"""A patch target must be read where it points, or the patch reaches nothing.

Moving code between modules is exactly when a test goes quiet: `mock.patch` raises only
when the attribute is gone from the module it names, and when the name survives there for
some other reason (a re-export, an unused import, a second reader) the patch installs a
Mock that nothing consults -- a green test that no longer tests anything.

This reads every literal `patch("...")` target in tests/ and tools/, resolves it to the
module that owns the name, and fails when that module never reads it, naming the modules
that do read it so the fix is one string. A target outside the client tree (stdlib, a
third-party package) is left alone: there is no module here to have moved it out of.

What this cannot see, and does not pretend to: a target that names a re-export
(`libs.package.Name`) while the code under test binds the name with
`from libs.package.module import Name` itself. That patch is installed in the package
namespace and read from the submodule, so the string has to name the module the code
under test actually lives in -- which is the rule the failure message points at.
"""
import ast
import os
from pathlib import Path
import tempfile
import unittest

CLIENT = Path(__file__).resolve().parents[1]
SCANNED = ("tests", "tools")
# A scan that finds (almost) nothing is broken, not clean: the suite patches hundreds.
MIN_TARGETS = 200


_TARGETS = {}
_NAMES = {}


def source_files(root, folder, recursive=False):
    directory = Path(root) / folder
    if not directory.is_dir():
        return []
    if recursive:
        return sorted(path for path in directory.rglob("*.py") if "__pycache__" not in path.parts)
    return [directory / name for name in sorted(os.listdir(directory))
            if name.endswith(".py")]


def literal_patch_targets(root, folder):
    """Every `patch("libs.x.y")` / `mock.patch("libs.x.y")` string in one folder."""
    key = (str(root), folder)
    if key in _TARGETS:
        return _TARGETS[key]
    found = []
    for path in source_files(root, folder):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not (isinstance(func, ast.Attribute) and func.attr == "patch"):
                continue
            if not node.args:
                continue
            first = node.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                found.append((path, node.lineno, first.value))
    _TARGETS[key] = found
    return found


def resolve_target(root, target):
    """The module a dotted patch target names, and the attribute path inside it.

    Longest prefix first, so `libs.jukebox.player.speak` resolves to the module that owns
    the name rather than to the package that merely contains the module. Returns
    `(None, [])` when no prefix of the target is a module in this tree.
    """
    parts = target.split(".")
    for cut in range(len(parts), 0, -1):
        dotted = parts[:cut]
        module = Path(root).joinpath(*dotted).with_suffix(".py")
        if module.is_file():
            return module, parts[cut:]
        package = Path(root).joinpath(*dotted) / "__init__.py"
        if package.is_file():
            return package, parts[cut:]
    return None, []


def names_read(path):
    """Names a module can answer for: everything it loads, plus what it binds itself."""
    key = str(path)
    if key in _NAMES:
        return _NAMES[key]
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            names.add(node.id)
    for stmt in tree.body:
        if isinstance(stmt, (ast.FunctionDef, ast.ClassDef, ast.AsyncFunctionDef)):
            names.add(stmt.name)
        elif isinstance(stmt, ast.Assign):
            for target in stmt.targets:
                for node in ast.walk(target):
                    if isinstance(node, ast.Name):
                        names.add(node.id)
        elif isinstance(stmt, (ast.Import, ast.ImportFrom)):
            for alias in stmt.names:
                names.add((alias.asname or alias.name).split(".")[0])
    _NAMES[key] = names
    return names


def modules_reading(root, name, near=None, limit=6):
    """Where a name IS read: the fix hint when a target points somewhere it is not.

    ``near`` is the module the target names, and modules that sit beside it come first:
    code usually moved to a neighbour, so the right answer is usually the first line.
    """
    found = []
    for folder in ("libs", "tests", "tools"):
        for path in source_files(root, folder, recursive=True):
            try:
                if name in names_read(path):
                    found.append(path)
            except SyntaxError:
                continue
    if near is not None:
        neighbour = Path(near).parent
        found.sort(key=lambda path: (path.parent != neighbour, str(path)))
    return [str(path.relative_to(Path(root))).replace(os.sep, "/") for path in found[:limit]]


def silent_targets(root, folders=SCANNED):
    """Patch targets that name a module in this tree which never reads the name."""
    quiet = []
    for folder in folders:
        for path, line, target in literal_patch_targets(root, folder):
            module, attrs = resolve_target(root, target)
            if module is None or not attrs:
                continue          # not ours, or a whole module being patched
            if attrs[0] not in names_read(module):
                quiet.append((
                    str(path.relative_to(Path(root))).replace(os.sep, "/"), line,
                    target, str(module.relative_to(Path(root))).replace(os.sep, "/"),
                    attrs[0],
                ))
    return quiet


class PatchTargetTests(unittest.TestCase):
    def test_every_patch_target_is_read_where_it_points(self):
        quiet = silent_targets(CLIENT)
        if quiet:
            lines = []
            for where, line, target, module, name in quiet:
                readers = modules_reading(CLIENT, name, near=CLIENT / module)
                hint = ", ".join(readers) if readers else "no module in the client tree"
                lines.append(
                    f"{where}:{line} patches {target}, but {module} never reads "
                    f"`{name}` -- the patch reaches nothing.\n"
                    f"    `{name}` is read by: {hint}\n"
                    f"    Patch the module that owns the code under test."
                )
            self.fail("silent patch targets:\n\n" + "\n\n".join(lines))

    def test_the_scan_reaches_the_suite(self):
        # Guards the guard: a scanner that finds nothing would pass the test above.
        total = sum(len(literal_patch_targets(CLIENT, folder)) for folder in SCANNED)
        self.assertGreaterEqual(
            total, MIN_TARGETS,
            f"only {total} patch targets found in {SCANNED}: the scan is broken, "
            f"not the suite")

    def test_a_target_whose_name_moved_is_caught(self):
        """The rule against a fixture: the name lives next door, not here."""
        with tempfile.TemporaryDirectory(prefix="bt-patch-target-") as temp:
            root = Path(temp)
            (root / "libs").mkdir()
            (root / "libs" / "__init__.py").write_text("", encoding="utf-8")
            (root / "libs" / "old.py").write_text(
                "def work():\n    return 1\n", encoding="utf-8")
            (root / "libs" / "new.py").write_text(
                "speak = work\n", encoding="utf-8")
            (root / "tests").mkdir()
            (root / "tests" / "test_moved.py").write_text(
                "from unittest import mock\n"
                "\n"
                "with mock.patch(\"libs.new.speak\"):\n"
                "    pass\n"
                "\n"
                "with mock.patch(\"libs.old.speak\"):\n"
                "    pass\n"
                "\n"
                "with mock.patch(\"pyogg.OpusEncoder\"):\n"
                "    pass\n",
                encoding="utf-8")

            quiet = silent_targets(root, ("tests",))
            readers = modules_reading(root, "speak")

        self.assertEqual(
            [(line, target) for _, line, target, _, _ in quiet],
            [(6, "libs.old.speak")],
            "the moved name must be the only one reported, and an outside target none")
        self.assertEqual(readers, ["libs/new.py"])


if __name__ == "__main__":
    unittest.main()
