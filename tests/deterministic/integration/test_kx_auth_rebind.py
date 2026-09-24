"""q-level regression for kx.auth's per-handle principal replacement semantics.

Runs ``kx_auth_rebind.q`` in a real ``q`` process and asserts that ``bind[]`` replaces a handle's
principal **wholesale**. The module used to store principals as the values of a dict, and a dict whose
values are conforming dicts *is* a keyed table to q — so the store-join was a column-wise upsert:

* a re-bind with a narrower key set kept the previous principal's extra fields, so a stale ``tenant``
  outlived a token refresh and a policy could authorise on stale identity. Reachable on the ordinary
  path, since the container caches connections on ``(sub, iss)`` and re-binds on every call;
* binding a narrower principal while another handle held a wider one signalled ``'mismatch``;
* separately, ``promote[]`` signalled ``'type`` on a valid all-atoms principal (a uniform typed value
  list cannot take a symbol-vector index-assign).

**Self-skipping**: needs a kdb-x install (a ``q`` binary) and a license. When either is absent — as in
the license-free CI integration lane — the test skips rather than fails, so it costs nothing there and
gives a real regression wherever q + a license exist (local dev, the licensed CI lane).
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[3]
Q_SCRIPT = Path(__file__).with_name("kx_auth_rebind.q")


def _q_binary() -> str | None:
    """The ``q`` on PATH, else a co-located kdb-x install under ~/.kx (the repo's dev convention)."""
    found = shutil.which("q")
    if found:
        return found
    fallback = Path.home() / ".kx" / "bin" / "q"
    return str(fallback) if fallback.exists() else None


def test_kx_auth_rebind_replaces_principal_wholesale():
    q = _q_binary()
    if not q:
        pytest.skip("no `q` binary — the kx.auth q-level regression needs a kdb-x install")

    env = dict(os.environ)
    # License resolves relative to the q binary (QHOME -> kc.lic); fill in the co-located kdb-x
    # install's paths when unset so a local dev run works without extra setup. Absent/invalid -> the
    # license probe below skips.
    kx = Path.home() / ".kx"
    if kx.exists():
        env.setdefault("QHOME", str(kx / "q"))
        env.setdefault("QLIC", str(kx))

    proc = subprocess.run(
        [q, str(Q_SCRIPT), "-q"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
    )
    out = proc.stdout + proc.stderr
    if "license" in out.lower() and ("error" in out.lower() or "no license" in out.lower()):
        pytest.skip(f"no kdb-x license available — skipping q-level regression ({out.strip()[:120]})")

    assert "ALL PASS" in out, f"kx.auth rebind regression failed:\n{out}"
    assert proc.returncode == 0, f"q exited {proc.returncode}:\n{out}"
