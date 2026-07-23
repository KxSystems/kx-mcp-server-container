"""Shared container-spawn helpers for the real-backend harness.

These are the only helpers shared across the public per-backend lanes (kdbai/, kdbx/, …).
Each lane's conftest imports from here instead of duplicating.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

# tests/fixtures/kx-mcp-example/src — two parents up from realidp/, then into tests/fixtures/
_FIXTURE_SRC = str(Path(__file__).parents[2] / "fixtures" / "kx-mcp-example" / "src")

# Keys injected by the pytest process that must not leak into a fresh subprocess.
# kx_mcp_kdbx.server sets PYKX_LICENSED=true; pykx adds PYKX_Q_LOADED_MARKER when it starts, and
# also unconditionally OVERWRITES QHOME (to its own bundled q lib dir under site-packages) and
# QPATH (to "<original QHOME>/mod" — which may not be where kx modules are actually installed),
# plus sets PYKX_DIR/PYKX_EXECUTABLE/PYKX_UNDER_PYTHON. All are process-local side effects of any
# `import pykx` happening anywhere in this pytest session (e.g. during collection of unrelated
# test files) — in a fresh subprocess they cause either a "licensed/unlicensed conflict" (the
# original kdbai_client case) or, for a PyKX-embedded bundle like kdbx, a spurious
# "no valid q license" failure even though the license is genuinely there (confirmed empirically:
# a clean env resolves it fine; the inherited/contaminated one doesn't).
_PYKX_KEYS_TO_STRIP = {
    "PYKX_LICENSED",
    "PYKX_Q_LOADED_MARKER",
    "QHOME",
    "QPATH",
    "PYKX_DIR",
    "PYKX_EXECUTABLE",
    "PYKX_UNDER_PYTHON",
}


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_until_listening(port: int, proc: subprocess.Popen, timeout: float = 20.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            out = proc.stdout.read() if proc.stdout else ""
            raise RuntimeError(f"container exited early ({proc.returncode}):\n{out}")
        with socket.socket() as s:
            s.settimeout(0.25)
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.2)
    raise RuntimeError("container did not start listening in time")


def _spawn_container(bundles: str = "example", extra_env: dict | None = None) -> tuple[str, subprocess.Popen]:
    """Spawn the MCP container with ``KX_MCP_AUTH=jwks`` → live IdP JWKS URI.

    The container is started as a subprocess and returned alongside its URL so that
    callers can manage its lifecycle (session-scoped fixtures call ``proc.terminate()``
    in their teardown).

    IdP config is read from the environment, sourced by the ``just test-*`` recipes
    via the ``envs/.env.*`` files:
      - Keycloak: derives JWKS URI + issuer from ``KC_BASE`` / ``KC_REALM``.
      - Entra:    reads ``KX_MCP_AUTH_JWKS_URI`` / ``KX_MCP_AUTH_ISSUER`` /
                  ``KX_MCP_AUTH_AUDIENCE`` directly from env (env file or overrides);
                  falls back to v2 endpoint defaults derived from ``ENTRA_TENANT_ID``.

    ``KX_MCP_AUTH_RESOURCE_URL`` is set for all spawns so the container wraps in
    ``RemoteAuthProvider`` and serves RFC 9728 Protected Resource Metadata — needed
    for the ``test_discovery_advertised`` test and representative of the HTTP-deployed
    posture.

    Returns ``(url, proc)``.
    """
    from realidp.idp.providers.factory import provider_name

    port = _free_port()

    if provider_name() == "entra":
        # Read JWKS/issuer/audience from env (written by entra_setup.py / .env.entra).
        # Default to v2 endpoint derived from ENTRA_TENANT_ID — overridable for v1
        # (sts.windows.net) by setting KX_MCP_AUTH_ISSUER explicitly in .env.entra.
        tenant = os.environ["ENTRA_TENANT_ID"]
        jwks_uri = os.environ.get(
            "KX_MCP_AUTH_JWKS_URI",
            f"https://login.microsoftonline.com/{tenant}/discovery/v2.0/keys",
        )
        issuer = os.environ.get(
            "KX_MCP_AUTH_ISSUER",
            f"https://login.microsoftonline.com/{tenant}/v2.0",
        )
        audience = os.environ.get(
            "KX_MCP_AUTH_AUDIENCE",
            f"api://{os.environ['ENTRA_CLIENT_ID']}",
        )
    else:
        # Keycloak — derive from KC_BASE + KC_REALM (existing behaviour)
        kc_base = os.environ.get("KC_BASE", "http://localhost:8080")
        kc_realm = os.environ.get("KC_REALM", "quants")
        audience = os.environ.get("KX_MCP_AUTH_AUDIENCE", os.environ.get("KC_CLIENT_ID", "kx-mcp"))
        jwks_uri = f"{kc_base}/realms/{kc_realm}/protocol/openid-connect/certs"
        issuer = f"{kc_base}/realms/{kc_realm}"

    base_env = {k: v for k, v in os.environ.items() if k not in _PYKX_KEYS_TO_STRIP}
    env = {
        **base_env,
        "PYTHONPATH": os.pathsep.join([_FIXTURE_SRC, os.environ.get("PYTHONPATH", "")]),
        "KX_MCP_AUTH": "jwks",
        "KX_MCP_AUTH_JWKS_URI": jwks_uri,
        "KX_MCP_AUTH_ISSUER": issuer,
        "KX_MCP_AUTH_AUDIENCE": audience,
        # Always set RESOURCE_URL so the container advertises RFC 9728 PRM — the
        # test_discovery_advertised assertion and the HTTP-deployed posture both require it.
        "KX_MCP_AUTH_RESOURCE_URL": f"http://127.0.0.1:{port}",
        **(extra_env or {}),
    }

    proc = subprocess.Popen(
        [
            sys.executable, "-m", "kx_mcp_core.launcher",
            "--bundles", bundles,
            "--transport", "streamable-http",
            "--host", "127.0.0.1",
            "--port", str(port),
        ],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )

    try:
        _wait_until_listening(port, proc)
    except RuntimeError:
        proc.kill()
        raise

    return f"http://127.0.0.1:{port}/mcp", proc
