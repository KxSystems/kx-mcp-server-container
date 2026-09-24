"""TOML reader that works at the declared Python floor — tier-neutral.

``tomllib`` entered the standard library in **Python 3.11**, but this repo declares
``requires-python = ">=3.10,<3.14"``. Two metadata tests (``test_lock_hygiene`` and
``test_packaging_deps``) parse ``pyproject.toml``/``uv.lock``, and importing ``tomllib`` directly
made them ``ModuleNotFoundError`` at *collection* time on 3.10 — which took the whole suite down,
not just those two modules.

That went unnoticed for the life of the declaration because nothing ran the floor: local dev pins
3.12 and CI pinned 3.13. It surfaced alongside KXI-72920 (a genuine 3.10-only product bug in
``kdbai/utils/filters.py``) when the floor was finally exercised. The ``test:floor`` CI job now runs
the suite at the floor so the claim stays honest.

``tomli`` is the upstream project ``tomllib`` was derived from and its ``load``/``loads`` API is
identical, so aliasing is a true drop-in rather than a shim with its own behaviour. It is declared as
a marker-gated dev dependency (``tomli; python_version < "3.11"``), so 3.11+ environments never
install it.

**Delete this module** when the floor moves to >=3.11: re-inline ``import tomllib`` in both call
sites and drop the ``tomli`` dev dependency.
"""

from __future__ import annotations

import sys

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - exercised only on the 3.10 floor (the `test:floor` CI job)
    import tomli as tomllib

__all__ = ["tomllib"]
