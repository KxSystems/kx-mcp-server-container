"""Shared guard against pykx's process-wide env contamination — tier-neutral.

`import pykx` unconditionally overwrites ``QHOME`` (to PyKX's own bundled q lib dir under
site-packages) and ``QPATH`` (to ``"<original QHOME>/mod"`` — which may not be where kx modules are
actually installed), and sets ``PYKX_Q_LOADED_MARKER``/``PYKX_LICENSED``/``PYKX_DIR``/
``PYKX_EXECUTABLE``/``PYKX_UNDER_PYTHON``. These are process-local side effects of ANY ``import pykx``
happening anywhere in a pytest session (e.g. a readiness probe, or an unrelated test file patching
``pykx.SyncQConnection``). Inherited as-is by a spawned subprocess that does its own ``import pykx``
(expecting to bootstrap itself from a clean slate), this reproducibly breaks the child: either a
"licensed/unlicensed conflict", or — for a PyKX-embedded bundle like kdbx — a spurious connectivity
failure (a bare ``.pykx.util.isw``, a PyKX-internal helper misfiring because the inherited
``PYKX_Q_LOADED_MARKER`` falsely claims q is already loaded in that process) even though the license
and target are both genuinely fine.

Used by both the realidp harness (``tests/deterministic/realidp/_spawn.py``) and any deterministic
test that imports pykx in-process (e.g. for a readiness probe) before spawning a pykx-backed
container child — shared here instead of duplicated so the fix doesn't have to be rediscovered per
call site. Lives at the ``tests/`` root (not under either tier) because both tiers need it.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Iterator, Mapping, Optional

PYKX_KEYS_TO_STRIP = frozenset({
    "PYKX_LICENSED",
    "PYKX_Q_LOADED_MARKER",
    "QHOME",
    "QPATH",
    "PYKX_DIR",
    "PYKX_EXECUTABLE",
    "PYKX_UNDER_PYTHON",
})


def clean_env(base: Optional[Mapping[str, str]] = None) -> dict[str, str]:
    """A copy of `base` (default ``os.environ``) with the pykx-contaminated keys removed.

    Use this when building a subprocess's env dict directly (the realidp harness's shape).
    """
    env = dict(os.environ if base is None else base)
    for key in PYKX_KEYS_TO_STRIP:
        env.pop(key, None)
    return env


@contextmanager
def stripped_from_os_environ() -> Iterator[None]:
    """Temporarily remove the pykx-contaminated keys from the REAL ``os.environ``, restoring
    whatever was there on exit — including re-adding a key that didn't exist before.

    For a fixture/helper that reads ``os.environ`` directly and takes no custom base env (e.g.
    ``tests/conftest.py``'s ``spawn_container``, which builds its child's env as
    ``{**os.environ, **env_overrides}`` with no way to supply a different base): wrap the call in
    this context manager instead of mutating the process env permanently.
    """
    saved = {key: os.environ.pop(key) for key in PYKX_KEYS_TO_STRIP if key in os.environ}
    try:
        yield
    finally:
        os.environ.update(saved)
