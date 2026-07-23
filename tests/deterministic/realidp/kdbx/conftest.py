"""kdbx ferry lane — session-level fixtures.

Unlike ``kdbai/`` (which connects to an already-running managed backend), kdbx's backend
is plain kdb+ — there is no pre-existing service to provision, so this lane **spawns its own
throwaway `q` process** loaded with ``kdbx_ferry_host.q`` (the ``kx.auth`` module + a
`` `trader ``-group grant against the real live Keycloak `quants` realm this lane's tests reuse
from ``idp``/``kdbai``).

The shared root ``realidp/conftest.py`` already provides the autouse ``_require_idp`` guard and
the ``alice_token``/``bob_token`` fixtures (inherited automatically — no import needed here).

Provides:
  kdbx_ferry_host          — session-scoped (host, port, svc_user, svc_password) for a real `q`
                              process; skips if no `q` binary/license (mirrors `_q_binary()` in
                              tests/deterministic/integration/test_kx_auth_assertion_gate.py).
  kdbx_ferry_container_url — session-scoped MCP container (--bundles kdbx, KX_MCP_AUTH=jwks,
                              KDBX_DB_ASSERT_IDENTITY=true) pointed at the ferry host.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import socket
import subprocess
import tempfile
import time
from pathlib import Path

import pytest

from realidp._spawn import _spawn_container

# kdbx/ -> realidp -> deterministic -> tests -> public
_PUBLIC_ROOT = Path(__file__).resolve().parents[4]
_HOST_Q = Path(__file__).with_name("kdbx_ferry_host.q")

# Test-only service-account credential for the throwaway q process — matches the admin grant
# (`.ferry.svcUser`) in kdbx_ferry_host.q. Local, transient, never shared with any real system.
_SVC_USER = "kxmcp"
_SVC_PASSWORD = "kdbx-ferry-test-svc-pw"


def _q_binary() -> str | None:
    """The ``q`` on PATH, else a co-located kdb-x install under ~/.kx (the repo's dev convention)."""
    found = shutil.which("q")
    if found:
        return found
    fallback = Path.home() / ".kx" / "bin" / "q"
    return str(fallback) if fallback.exists() else None


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def kdbx_ferry_host():
    """Spawn a real `q` process loaded with kdbx_ferry_host.q for the whole kdbx session.

    Yields (host, port, svc_user, svc_password). Skips cleanly if no `q` binary is found or the
    process fails to license-start; fails loudly if it starts but never becomes ready (a real bug,
    not an environment gap).

    Readiness is polled via a real qIPC call checking `.kx.auth.bind` is defined — mirroring the
    container's own pre-flight — rather than a bare socket check, because q opens its listening
    port on `-p` *before* the script body (which loads kx.auth) finishes running; a socket-only
    check would race that.
    """
    q = _q_binary()
    if not q:
        pytest.skip("no `q` binary — the kdbx ferry realidp lane needs a kdb-x install")

    env = dict(os.environ)
    # `import pykx` (already triggered by test collection elsewhere in this session — a
    # process-wide side effect, same class of contamination `_spawn.py` strips PYKX_LICENSED/
    # PYKX_Q_LOADED_MARKER for) overrides QHOME to PyKX's own bundled q lib dir and, to
    # compensate, sets QPATH to `<original QHOME>/mod` — poisoning both for a fresh q process.
    # For a co-located kdb-x install, QHOME=~/.kx resolves both the license (~/.kx/kc.lic,
    # matching the documented "QHOME -> kc.lic" convention) and the module path (~/.kx/mod,
    # where `just install-modules` symlinks kx.auth) — set explicitly rather than relying on
    # ambient/pykx-mutated env.
    kx = Path.home() / ".kx"
    if kx.exists():
        env["QHOME"] = str(kx)
        env["QPATH"] = str(kx / "mod")

    port = _free_port()
    userpass_fd, userpass_path = tempfile.mkstemp(prefix="kdbx-ferry-userpass-")
    md5 = hashlib.md5(_SVC_PASSWORD.encode()).hexdigest()
    with os.fdopen(userpass_fd, "w") as f:
        f.write(f"{_SVC_USER}:{md5}\n")
    os.chmod(userpass_path, 0o600)

    proc = subprocess.Popen(
        [q, str(_HOST_Q), "-U", userpass_path, "-p", str(port)],
        cwd=_PUBLIC_ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )

    import pykx as kx_mod  # local import: only the kdbx lane needs a real (licensed) pykx

    deadline = time.time() + 20.0
    ready = False
    last_exc: Exception | None = None
    out = ""
    while time.time() < deadline:
        if proc.poll() is not None:
            out = proc.stdout.read() if proc.stdout else ""
            break
        try:
            probe = kx_mod.SyncQConnection(
                host="127.0.0.1", port=port, username=_SVC_USER, password=_SVC_PASSWORD, timeout=2,
            )
            try:
                bind_available = probe("@[{.kx.auth.bind;1b};(::);{0b}]").py()
            finally:
                probe.close()
            if bind_available:
                ready = True
                break
        except Exception as exc:  # noqa: BLE001 — q not accepting connections yet, keep polling
            last_exc = exc
            time.sleep(0.25)

    if not ready:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        os.unlink(userpass_path)
        if out:
            pytest.fail(f"kdbx_ferry_host exited early ({proc.returncode}):\n{out}")
        pytest.fail(f"kdbx_ferry_host never became ready on :{port}: {last_exc}")

    yield "127.0.0.1", port, _SVC_USER, _SVC_PASSWORD

    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
    os.unlink(userpass_path)


@pytest.fixture(scope="session")
def kdbx_ferry_container_url(kdbx_ferry_host) -> str:
    """Session-scoped MCP container: --bundles kdbx, jwks inbound (real Keycloak), assert_identity
    outbound (ferries the validated principal onto the q handle via .kx.auth.bind).

    Requires (sourced by `just test-kdbx` from envs/.env.keycloak): KC_BASE, KC_REALM,
    KX_MCP_AUTH_AUDIENCE — the same env `just test-keycloak` already uses.
    """
    host, port, svc_user, svc_password = kdbx_ferry_host
    extra_env = {
        "KDBX_DB_HOST": host,
        "KDBX_DB_PORT": str(port),
        "KDBX_DB_USERNAME": svc_user,
        "KDBX_DB_PASSWORD": svc_password,
        "KDBX_DB_ASSERT_IDENTITY": "true",
    }
    # The container's own PyKX import (the kdbx bundle force-sets PYKX_LICENSED=true and makes
    # real embedded-q calls) unconditionally overwrites its own QHOME to PyKX's bundled lib dir
    # regardless of what's inherited — fighting that is pointless. What actually matters is QLIC,
    # which PyKX's import does NOT touch, so the ambient value (already correctly the real KDBX
    # license location — NOT ~/.kx/kc.lic, which is a KXAI license, the wrong type) flows through
    # _spawn_container's base env (a plain `os.environ` copy) unmodified. Nothing to override here.
    url, proc = _spawn_container(bundles="kdbx", extra_env=extra_env)
    yield url
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
