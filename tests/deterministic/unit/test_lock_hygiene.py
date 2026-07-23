"""Guard: everything under public/ resolves its packages from public sources.

uv records, per package, the exact registry it was resolved from — in uv.lock
at install time, and in ``[[tool.uv.index]]`` / ``[tool.uv.sources]`` at
resolution time. A dev who re-locks with the internal Nexus index configured in
their shell (a ``UV_INDEX`` env var or ``--index`` flag) silently bakes internal
URLs into the lock — invisible in-house, where credentials exist, but
``uv run`` / ``uv sync`` from a clean public checkout then 401s on download.
This has bitten before: kdbai-client and pykx shipped Nexus-pinned while both
exist on PyPI at the exact same versions, breaking the README quickstart for
external users.

These tests self-discover every uv.lock and pyproject.toml under public/ (the
workspace root plus any standalone demo project), so new projects are covered
without touching this file. Local non-network registries (e.g. the extending
demo's ``../../dist`` flat index of self-built wheels) are externally
reproducible and allowed; any *network* registry other than public PyPI fails.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
PUBLIC_INDEX = "https://pypi.org/simple"
INTERNAL_HOST = "kxi-dev.kx.com"


def _discover(name: str) -> list[Path]:
    return sorted(p for p in REPO_ROOT.rglob(name) if ".venv" not in p.parts)


LOCKS = _discover("uv.lock")
PYPROJECTS = _discover("pyproject.toml")


def _rel(path: Path) -> str:
    return str(path.relative_to(REPO_ROOT))


def test_the_workspace_lock_is_discovered() -> None:
    # Meta-guard: if discovery silently broke, the other tests would pass vacuously.
    assert REPO_ROOT / "uv.lock" in LOCKS


@pytest.mark.parametrize("lock", LOCKS, ids=_rel)
def test_all_network_registry_sources_are_public_pypi(lock: Path) -> None:
    data = tomllib.loads(lock.read_text())
    offenders = sorted(
        f"{pkg['name']}=={pkg.get('version', '?')} <- {registry}"
        for pkg in data.get("package", [])
        if (registry := pkg.get("source", {}).get("registry", ""))
        and registry.startswith(("http://", "https://"))
        and registry != PUBLIC_INDEX
    )
    assert not offenders, (
        f"{_rel(lock)} pins package(s) to a non-public network registry: "
        f"{offenders}. A clean public checkout has no credentials for it, so "
        f"the README quickstart (`uv run kx-mcp ...`) fails at download. "
        f'Re-lock without the internal index configured, e.g. `uv lock '
        f'--upgrade-package "<name>==<same-version>"` in a shell with no '
        f"UV_INDEX/--index pointing at Nexus."
    )


@pytest.mark.parametrize("lock", LOCKS, ids=_rel)
def test_no_internal_nexus_host_in_lock(lock: Path) -> None:
    hits = [
        f"line {i}: {line.strip()}"
        for i, line in enumerate(lock.read_text().splitlines(), start=1)
        if INTERNAL_HOST in line
    ]
    assert not hits, (
        f"{_rel(lock)} references the internal Nexus host ({INTERNAL_HOST}) — "
        f"unreachable without KX credentials, so external users cannot "
        f"install. First hits: {hits[:5]}"
    )


@pytest.mark.parametrize("pyproject", PYPROJECTS, ids=_rel)
def test_no_internal_host_in_pyproject(pyproject: Path) -> None:
    # Catches [[tool.uv.index]] entries and direct-URL deps a lock check misses.
    hits = [
        f"line {i}: {line.strip()}"
        for i, line in enumerate(pyproject.read_text().splitlines(), start=1)
        if INTERNAL_HOST in line
    ]
    assert not hits, (
        f"{_rel(pyproject)} references the internal Nexus host "
        f"({INTERNAL_HOST}); external users cannot resolve against it. "
        f"Hits: {hits[:5]}"
    )
