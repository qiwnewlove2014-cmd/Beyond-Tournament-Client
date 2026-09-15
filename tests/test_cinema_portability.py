"""The game's room core must stay a library somebody could take.

The room maths in ``libs/audio/cinema`` (layout, profiles, placement, router,
channel, listener) is engine-free on purpose: no OpenAL, no third-party import,
no game import. What makes it portable is that it *stays* that way, so it is
checked rather than described — plus the copy published next to the tree
(``cinema-room/``) has to hold exactly these modules, because the published one
is the one other projects will use.

Nothing here imports the published package or plays audio: it reads files, walks
imports, and compares bytes.
"""

import ast
import os
from pathlib import Path
import unittest

TESTS = Path(__file__).resolve().parent
CLIENT = TESTS.parent
GAME = CLIENT / "libs" / "audio" / "cinema"
PUBLISHED = CLIENT.parent / "cinema-room"
PUBLISHED_PACKAGE = PUBLISHED / "cinema_room"

# The six modules that are a library rather than a game feature, and the only
# files in the game allowed to need the engine (its OpenAL transport).
CORE_MODULES = ("layout", "profiles", "placement", "router", "channel", "listener")
ENGINE_FILES = {"bank.py", "speech.py"}
STDLIB_ALLOWED = frozenset({"math", "array", "collections"})


def source(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def imported_names(path):
    """Every module name a file imports, package-relative ones included."""
    found = set()
    for node in ast.walk(ast.parse(source(path))):
        if isinstance(node, ast.Import):
            found.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            found.add(("." * (node.level or 0)) + (node.module or ""))
    return found


class PortableCoreTests(unittest.TestCase):
    """The room maths has to remain takeable."""

    def test_the_core_imports_the_standard_library_and_nothing_else(self):
        siblings = frozenset("." + name for name in CORE_MODULES)
        offenders = []
        for name in CORE_MODULES:
            for module in imported_names(GAME / (name + ".py")):
                if module.startswith("."):
                    if module not in siblings:
                        offenders.append(f"{name}.py imports {module}")
                    continue
                if module not in STDLIB_ALLOWED:
                    offenders.append(f"{name}.py imports {module}")
        self.assertEqual(
            offenders, [],
            "a core module grew a dependency: this half is what another project "
            "can take, so anything it needs has to be handed to it",
        )

    def test_only_the_transport_needs_the_engine(self):
        offenders = sorted(
            path.name for path in GAME.glob("*.py")
            if "cyal" in imported_names(path) and path.name not in ENGINE_FILES
        )
        self.assertEqual(
            offenders, [],
            "OpenAL belongs to bank.py and speech.py; the core must render for "
            "an engine that is not OpenAL at all",
        )

    def test_the_core_never_reaches_for_a_game_module(self):
        offenders = []
        for name in CORE_MODULES:
            for module in imported_names(GAME / (name + ".py")):
                if module.startswith("libs") or module.startswith(".."):
                    offenders.append(f"{name}.py imports {module}")
        self.assertEqual(offenders, [], "the portable half reached into the game")


class PublishedCopyTests(unittest.TestCase):
    """The published library has to match this game's own copy of the core.

    ``cinema-room/`` (next to ``client/``) is the installable library: packaging
    metadata, README, examples, its own tests, and its own modules *including*
    the six core ones. Those six must be the same code as the game's, or the
    published library quietly becomes a fork of the feature it came from.
    """

    def test_the_published_copy_exists(self):
        self.assertTrue(
            PUBLISHED_PACKAGE.is_dir(),
            "the published copy in cinema-room/ is gone; if that was deliberate,"
            " delete this test class with it",
        )

    def test_the_six_core_modules_are_the_same_code(self):
        missing, drifted = [], []
        for name in CORE_MODULES:
            mine = GAME / (name + ".py")
            theirs = PUBLISHED_PACKAGE / (name + ".py")
            if not theirs.is_file():
                missing.append(name)
            elif self.normalised(mine) != self.normalised(theirs):
                drifted.append(name)
        self.assertEqual(missing, [], "modules missing from the published copy")
        self.assertEqual(
            drifted, [],
            "these modules differ between the game and the published library:"
            " take whichever side you changed to the other (the probe in"
            " cinema-room/tools/portability_check.py prints the command)",
        )

    def test_the_published_library_carries_what_a_reader_needs(self):
        for name in ("pyproject.toml", "README.md", "CHANGELOG.md"):
            self.assertTrue((PUBLISHED / name).is_file(), f"{name} is missing")
        for name in ("__init__.py", "rooms.py", "port.py", "player.py", "openal.py"):
            self.assertTrue((PUBLISHED_PACKAGE / name).is_file(),
                            f"cinema_room/{name} is missing from the library")

    def test_the_engine_is_an_extra_not_a_dependency(self):
        import tomllib
        with open(PUBLISHED / "pyproject.toml", "rb") as handle:
            metadata = tomllib.load(handle)
        self.assertEqual(metadata["project"]["name"], "cinema-room")
        self.assertEqual(metadata["project"]["dependencies"], [],
                         "the library must install with no dependencies at all")
        self.assertIn("openal", metadata["project"]["optional-dependencies"])

    @staticmethod
    def normalised(path):
        """Compare code, not line endings (this tree is CRLF in places)."""
        return source(path).replace("\r\n", "\n")


if __name__ == "__main__":
    unittest.main()
