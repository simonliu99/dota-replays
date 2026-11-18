"""The application and its tests run on the Python standard library alone.

The case reads each source file's import statements, including imports inside
functions, rather than the modules a run happened to load, so its verdict does
not depend on what the host's python3 has installed or on test order.
"""

import ast
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCE_GLOBS = ("*.py", "tests/*.py", "scripts/*.py", "scripts/lib/*.py")
# Python files without a .py suffix.
SCRIPTS = ("tests/record-opendota",)
# Standard library packages newer than the interpreter the suite may run on:
# compression arrived in Python 3.14, and scripts/probe_replay_formats.py
# imports it only when present.
NEWER_STDLIB = {"compression"}


def python_sources() -> list[Path]:
    """Return the repository's Python sources."""
    sources = [path for pattern in SOURCE_GLOBS for path in ROOT.glob(pattern)]
    return sorted(sources + [ROOT / name for name in SCRIPTS])


def local_modules(sources: list[Path]) -> set[str]:
    """Return the module names the repository itself provides."""
    return {path.stem for path in sources} | {"tests"}


def imported_names(path: Path) -> set[str]:
    """Return the top-level module names a source file imports."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module.split(".")[0])
    return names


class TestStandardLibraryOnly(unittest.TestCase):
    def test_no_source_imports_a_module_outside_the_standard_library(self):
        sources = python_sources()
        # The repository holds at least the eight application modules.
        self.assertGreaterEqual(len(sources), 8)
        allowed = set(sys.stdlib_module_names) | NEWER_STDLIB | local_modules(sources)

        outside = {
            str(path.relative_to(ROOT)): sorted(imported_names(path) - allowed)
            for path in sources
        }

        self.assertEqual({name: found for name, found in outside.items() if found}, {})


if __name__ == "__main__":
    unittest.main()
