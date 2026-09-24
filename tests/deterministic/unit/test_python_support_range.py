"""Guard: the declared Python range is uniform, and the floor is actually *exercised*.

KXI-72920 was a Python 3.10 crash (`time.fromisoformat` only learned to parse a bare 'Z' in 3.11)
that sat undetected because **nothing ran the declared floor**: `requires-python` said `>=3.10`,
local dev pinned 3.12, CI pinned 3.13. The same blind spot hid a second defect — two metadata tests
imported `tomllib` (stdlib only from 3.11), so the suite could not even be *collected* at its own
floor. A declared floor nobody runs is documentation, not support.

So there are two separate things to pin, and only both together close that gap:

1. **The declaration is consistent** — every package, the workspace root, and the shipped demos
   agree on one range, and mypy's `python_version` / Ruff's `target-version` track the floor. A
   split toolchain is how "supported" quietly stops meaning anything.
2. **The declaration is exercised** — the CI job that runs the suite at the floor really is pinned
   to the floor.

Check 2 reads `.gitlab-ci.yml`, which lives at the *internal* repo root, outside `public/`. In the
published public tree `public/` is the root and that file is absent, so it skips there rather than
failing — the internal repo is where CI is defined and where the drift can happen.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

# This module runs at the floor too, so it must not import `tomllib` directly (3.11+ only).
from _toml_compat import tomllib

PUBLIC_ROOT = Path(__file__).resolve().parents[3]
PACKAGES_DIR = PUBLIC_ROOT / "packages"

FLOOR = "3.10"
CEILING_EXCLUSIVE = "3.14"
EXPECTED_CLAUSES = {f">={FLOOR}", f"<{CEILING_EXCLUSIVE}"}
# The CI job whose whole purpose is running the suite at the floor.
FLOOR_JOB = "test:floor"


def _requires_python(pyproject: Path) -> str:
    return tomllib.loads(pyproject.read_text())["project"]["requires-python"]


def _clauses(spec: str) -> set[str]:
    """`">=3.10,<3.14"` -> `{">=3.10", "<3.14"}` (whitespace/order insensitive)."""
    return {clause.replace(" ", "") for clause in spec.split(",")}


def _declaring_pyprojects() -> dict[str, Path]:
    """Every pyproject under public/ that declares a range: the root, the packages, the demos."""
    found = {"<workspace root>": PUBLIC_ROOT / "pyproject.toml"}
    for pyproject in sorted(PACKAGES_DIR.glob("*/pyproject.toml")):
        found[tomllib.loads(pyproject.read_text())["project"]["name"]] = pyproject
    for pyproject in sorted((PUBLIC_ROOT / "demos").rglob("pyproject.toml")):
        found[str(pyproject.relative_to(PUBLIC_ROOT))] = pyproject
    return found


PYPROJECTS = _declaring_pyprojects()


def test_declaration_sites_were_found() -> None:
    """Sanity: an empty/mis-rooted glob must not let the parametrised checks pass vacuously."""
    assert "kx-mcp-kdbai" in PYPROJECTS, sorted(PYPROJECTS)
    assert len(PYPROJECTS) >= 6, sorted(PYPROJECTS)


@pytest.mark.parametrize("label", sorted(PYPROJECTS))
def test_declared_range_is_uniform(label: str) -> None:
    actual = _clauses(_requires_python(PYPROJECTS[label]))
    assert actual == EXPECTED_CLAUSES, (
        f"{label} declares requires-python {_requires_python(PYPROJECTS[label])!r}; "
        f"expected clauses {sorted(EXPECTED_CLAUSES)}. Every package, the workspace root, and the "
        "shipped demos must agree on one range."
    )


def test_static_analysis_targets_track_the_floor() -> None:
    """mypy and Ruff must analyse *at* the floor, or the gate stops covering the oldest support."""
    config = tomllib.loads((PUBLIC_ROOT / "pyproject.toml").read_text())["tool"]
    assert config["mypy"]["python_version"] == FLOOR
    assert config["ruff"]["target-version"] == f"py{FLOOR.replace('.', '')}"


def test_ci_runs_the_suite_at_the_declared_floor() -> None:
    """The floor is only 'supported' if a job actually runs it — the KXI-72920 root cause."""
    ci_file = PUBLIC_ROOT.parent / ".gitlab-ci.yml"
    if not ci_file.exists():
        pytest.skip("no .gitlab-ci.yml (published public tree — CI is defined internally)")

    # The GitLab config may use non-standard tags (!reference); we only need plain scalars here.
    ci = yaml.safe_load(ci_file.read_text().replace("!reference", "#!reference"))
    assert FLOOR_JOB in ci, (
        f"the {FLOOR_JOB!r} job is gone. It is the only thing that runs the suite at the declared "
        f"floor ({FLOOR}); without it requires-python is an unverified claim again (KXI-72920)."
    )
    pinned = ci[FLOOR_JOB]["variables"]["UV_PYTHON"]
    assert pinned == f"python{FLOOR}", (
        f"{FLOOR_JOB} pins UV_PYTHON={pinned!r} but the declared floor is {FLOOR}. "
        "Move them together, or CI stops exercising the oldest Python we claim to support."
    )
