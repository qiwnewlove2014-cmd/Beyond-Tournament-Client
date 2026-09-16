"""The game's room core must stay a library somebody could take.

The room maths in ``libs/audio/cinema`` (layout, profiles, placement, router,
channel, listener) is engine-free on purpose: no OpenAL, no third-party import,
no game import. What makes it portable is that it *stays* that way, so it is
checked rather than described.

The copy that used to be published next to the tree (``cinema-room/``) was
deliberately removed: two copies of six modules drift, and the copy that drifts
is the one nobody runs. The half worth keeping is the half that made it
takeable, and that half is checked here against this game's own modules — so
publishing it again later is a copy, not a rewrite.

Nothing here imports a package or plays audio: it reads files and walks imports.
"""

import ast
from pathlib import Path
import unittest

TESTS = Path(__file__).resolve().parent
CLIENT = TESTS.parent
GAME = CLIENT / "libs" / "audio" / "cinema"

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


if __name__ == "__main__":
    unittest.main()
