"""q-level regression for the kx.auth identity-assertion gate.

Runs ``kx_auth_assertion_gate.q`` in a real ``q`` process and asserts the module enforces the
identity-assertion policy: bind[] default-denies until configure[], keys on the caller (assertPolicy),
the two configure() password modes behave (delegate vs enforce), and setAssertPolicy overrides the
default. This proves the module LOGIC; the live ``.z.u``-over-IPC property (a real second connection
is refused at bind) is exercised separately, by a live demo and the kdb-x realidp lane.

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
Q_SCRIPT = Path(__file__).with_name("kx_auth_assertion_gate.q")


def _q_binary() -> str | None:
    """The ``q`` on PATH, else a co-located kdb-x install under ~/.kx (the repo's dev convention)."""
    found = shutil.which("q")
    if found:
        return found
    fallback = Path.home() / ".kx" / "bin" / "q"
    return str(fallback) if fallback.exists() else None


def test_kx_auth_assertion_gate_behaviour():
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
    )
    out = proc.stdout + proc.stderr
    if "license" in out.lower() and ("error" in out.lower() or "no license" in out.lower()):
        pytest.skip(f"no kdb-x license available — skipping q-level regression ({out.strip()[:120]})")

    assert "ALL PASS" in out, f"kx.auth assertion-gate regression failed:\n{out}"
    assert proc.returncode == 0, f"q exited {proc.returncode}:\n{out}"
