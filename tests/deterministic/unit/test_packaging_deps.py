"""Guard: every first-party import is a *declared* dependency.

The uv workspace makes all members editable-importable, so a missing
``[project.dependencies]`` entry for a sibling package is invisible in dev and
CI — every import resolves regardless of what's declared. But a wheel installed
from the index carries only its declared deps (``Requires-Dist`` is derived from
``[project.dependencies]``), so an undeclared sibling import ``ImportError``s at
runtime for a consumer. This has bitten before: an ``@authorize`` import added a
``kx_mcp_core`` dependency to the kdbx bundle without declaring it, and nothing
caught it because the whole test suite runs in-workspace.

This scans each package's ``src/`` for imports of the OTHER workspace members
via AST (so docstring/comment mentions don't count — only real imports) and
asserts each is declared. It is the cheap static guard; a heavier "build the
wheel, install into a clean venv, import it" smoke is the eventual integration
-tier complement (license-gated for the pykx bundles).
"""

from __future__ import annotations

import ast
import re
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
PACKAGES_DIR = REPO_ROOT / "packages"


def _members() -> dict[str, tuple[str, Path]]:
    """Map workspace import-name (underscored) -> (distribution-name, pyproject path)."""
    members: dict[str, tuple[str, Path]] = {}
    for pyproject in PACKAGES_DIR.glob("*/pyproject.toml"):
        data = tomllib.loads(pyproject.read_text())
        dist = data["project"]["name"]
        members[dist.replace("-", "_")] = (dist, pyproject)
    return members


MEMBERS = _members()
IMPORT_NAMES = set(MEMBERS)


def _declared_deps(pyproject: Path) -> set[str]:
    data = tomllib.loads(pyproject.read_text())
    project = data["project"]
    specs = list(project.get("dependencies", []))
    for extra in project.get("optional-dependencies", {}).values():
        specs.extend(extra)
    # Take the leading name token off each PEP 508 spec (strip version/extras/markers).
    return {re.split(r"[<>=!~;\[ ]", spec.strip())[0].lower() for spec in specs}


def _first_party_imports(src_dir: Path) -> set[str]:
    found: set[str] = set()
    for py in src_dir.rglob("*.py"):
        tree = ast.parse(py.read_text(), filename=str(py))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    top = alias.name.split(".")[0]
                    if top in IMPORT_NAMES:
                        found.add(top)
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                top = node.module.split(".")[0]
                if top in IMPORT_NAMES:
                    found.add(top)
    return found


@pytest.mark.parametrize("import_name", sorted(MEMBERS), ids=lambda n: MEMBERS[n][0])
def test_first_party_imports_are_declared(import_name: str) -> None:
    dist, pyproject = MEMBERS[import_name]
    src_dir = pyproject.parent / "src"
    if not src_dir.exists():
        pytest.skip(f"{dist} has no src/ directory")

    imported = _first_party_imports(src_dir)
    imported.discard(import_name)  # a package may import itself
    declared = _declared_deps(pyproject)
    missing = {MEMBERS[i][0] for i in imported} - declared

    assert not missing, (
        f"{dist} imports first-party package(s) {sorted(missing)} but does not "
        f"declare them in [project.dependencies]. A wheel install from the index "
        f"would omit them (Requires-Dist derives from [project.dependencies]) and "
        f"ImportError at runtime. Add them to packages/{pyproject.parent.name}/pyproject.toml."
    )
