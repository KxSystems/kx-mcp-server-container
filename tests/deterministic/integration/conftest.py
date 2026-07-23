"""Auto-apply the ``integration`` marker to every test in this subtree.

Tests under ``tests/deterministic/integration/`` spawn a real subprocess over
the wire (streamable-http or stdio). They are deterministic and license-free
(in-process JWKS + ``example`` bundle), so they run in CI by default — the
``integration`` marker is for *local fast-feedback selection*, not CI exclusion.

Run only integration tests::

    just test-integration
    # or: uv run pytest -m integration

Skip integration tests (unit subset only)::

    just test-unit
    # or: uv run pytest -m "not integration and not realidp"
"""

from __future__ import annotations

import pytest


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Stamp every item in this directory tree with the ``integration`` marker."""
    for item in items:
        if str(item.fspath).startswith(str(__file__).removesuffix("/conftest.py")):
            item.add_marker(pytest.mark.integration)
