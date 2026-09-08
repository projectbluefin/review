"""Import-root contract: image/ is the single import root (#417).

Modules under image/tui and image/harness must import their siblings by
package-qualified name (tui.*, harness.*) — never by bare module name and
never via a repo-rooted image.* path. Bare-name imports only resolve when
image/tui itself sits on sys.path, which loads the same file under a second
module identity: isinstance() checks, module-level state, and monkeypatches
then silently split across the two copies. This guard fails if any sibling
import regresses to a bare or repo-rooted spelling, if image/tui loses its
__init__.py, or if a test puts image/tui back on sys.path.
"""

from __future__ import annotations

import ast
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
IMAGE = ROOT / "image"
TUI = IMAGE / "tui"
HARNESS = IMAGE / "harness"

SIBLINGS = {p.stem for p in TUI.glob("*.py")} | {p.stem for p in HARNESS.glob("*.py")}
SIBLINGS.discard("__init__")


def _imports(path: Path):
    tree = ast.parse(path.read_text(), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                yield node.module


class ImportRootContractTests(unittest.TestCase):
    def test_tui_is_a_real_package(self) -> None:
        self.assertTrue(
            (TUI / "__init__.py").is_file(),
            "image/tui/__init__.py must exist so tui.* is one package, "
            "not an accidental namespace",
        )

    def test_no_bare_sibling_imports(self) -> None:
        offenders = []
        for module in sorted(TUI.glob("*.py")) + sorted(HARNESS.glob("*.py")):
            for name in _imports(module):
                top = name.split(".")[0]
                if top in SIBLINGS:
                    offenders.append(f"{module.name}: bare sibling import '{name}'")
                if top == "image":
                    offenders.append(f"{module.name}: repo-rooted import '{name}'")
        self.assertEqual(
            offenders,
            [],
            "image/ is the only import root; spell siblings tui.*/harness.* — "
            + "; ".join(offenders),
        )

    def test_tests_use_the_image_root_only(self) -> None:
        offenders = []
        for test in sorted(ROOT.glob("tests/*.py")):
            if test.name == "import_root_contract.py":
                continue  # this guard's own source necessarily names the patterns
            source = test.read_text()
            for line in source.splitlines():
                if "sys.path" in line and ('"image" / "tui"' in line or "'image' / 'tui'" in line or "TUI_DIR)" in line and "parent" not in line):
                    offenders.append(f"{test.name}: puts image/tui on sys.path")
            for name in _imports(test):
                if name.split(".")[0] in SIBLINGS:
                    offenders.append(f"{test.name}: bare sibling import '{name}'")
        self.assertEqual(
            offenders,
            [],
            "tests must import via tui.*/harness.* off the image/ root — "
            + "; ".join(offenders),
        )

    def test_launcher_pythonpath_has_no_tui_half(self) -> None:
        launcher = (IMAGE / "bin" / "bluefin-review").read_text()
        self.assertNotIn(
            "${root}/tui",
            launcher,
            "bluefin-review must not put the tui dir on PYTHONPATH; "
            "the image root alone makes tui.*/harness.* importable",
        )

    def test_container_smoke_uses_the_image_root(self) -> None:
        containerfile = (ROOT / "image" / "Containerfile").read_text()
        self.assertNotIn(
            "PYTHONPATH=/opt/bluefin/tui:/opt/bluefin",
            containerfile,
            "the Containerfile smoke test must not restore the tui import root",
        )
        self.assertIn(
            'import tui.$(basename "$tui_module" .py)',
            containerfile,
            "the Containerfile smoke test must import package-qualified modules",
        )


if __name__ == "__main__":
    unittest.main()
