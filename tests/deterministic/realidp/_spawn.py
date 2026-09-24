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

from _pykx_env import clean_env

# tests/fixtures/kx-mcp-example/src — two parents up from realidp/, then into tests/fixtures/
_FIXTURE_SRC = str(Path(__file__).parents[2] / "fixtures" / "kx-mcp-example" / "src")


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


def _require_port_free(port: int) -> int:
    """A caller-pinned port must be *ours*. ``_wait_until_listening`` only waits for *something*
    to accept a connection, so a squatter already on ``port`` would silently be driven by the
    whole test instead of the container we think we spawned."""
    with socket.socket() as s:
        s.settimeout(0.25)
        if s.connect_ex(("127.0.0.1", port)) == 0:
            raise RuntimeError(
                f"127.0.0.1:{port} is already in use. This port is pinned (not auto-selected) "
                f"because it is baked into a pre-registered redirect URI at the IdP — free it, or "
                f"pick a different port and update the matching client config, then re-provision."
            )
    return port


def _spawn_container(
    bundles: str = "example",
    extra_env: dict | None = None,
    *,
    port: int | None = None,
) -> tuple[str, subprocess.Popen]:
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

    ``extra_env`` is merged last, so a caller can already override ``KX_MCP_AUTH`` and every
    ``KX_MCP_AUTH_*`` key below — that is the seam a non-``jwks`` mode (e.g. ``oidc_proxy``, see
    ``_spawn_oidc_proxy_container``) uses. ``port`` pins the listen port instead of picking a free
    one; needed only by a mode whose advertised ``base_url`` must be knowable *before* spawn
    because it is pre-registered at the IdP.

    Returns ``(url, proc)``.
    """
    from realidp.idp.providers.factory import provider_name

    port = _free_port() if port is None else _require_port_free(port)

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

    base_env = clean_env()
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


def _spawn_oidc_proxy_container(
    *, port: int, bundles: str = "example", fastmcp_home: str | None = None
) -> tuple[str, subprocess.Popen]:
    """Spawn the container in ``KX_MCP_AUTH=oidc_proxy`` mode on a **fixed** port (Keycloak only).

    Only what differs from the ``jwks`` spawn needs to be named — the rest (issuer, audience,
    ``RESOURCE_URL``) is inherited via ``extra_env`` overriding the ``jwks`` defaults:
      - ``KX_MCP_AUTH_ISSUER``   → ``_build_oidc_proxy`` derives ``config_url`` from it.
      - ``KX_MCP_AUTH_AUDIENCE`` → the ``aud`` the ``JWTVerifier`` requires on the **upstream**
        Keycloak token (the mode's contract, not the container-minted one the client receives).
      - ``KX_MCP_AUTH_RESOURCE_URL`` → ``http://127.0.0.1:{port}``, which is *why* the port must be
        fixed: it is the ``base_url`` FastMCP advertises and the prefix of the upstream redirect
        URI ``{base_url}/auth/callback`` pre-registered at Keycloak.

    ``KX_MCP_AUTH_REQUIRED_SCOPES`` is deliberately left unset — see the realidp README for why.
    ``fastmcp_home`` pins ``FASTMCP_HOME`` so the proxy's Fernet-encrypted OAuth-state store lives
    in a throwaway directory per test run rather than the developer's real data dir.
    """
    extra_env: dict = {
        "KX_MCP_AUTH": "oidc_proxy",
        "KX_MCP_AUTH_CLIENT_ID": os.environ.get("KC_PROXY_CLIENT_ID", "kx-mcp-proxy"),
        "KX_MCP_AUTH_CLIENT_SECRET": os.environ.get("KC_PROXY_CLIENT_SECRET", "kx-mcp-proxy-secret"),
    }
    if fastmcp_home:
        extra_env["FASTMCP_HOME"] = fastmcp_home
    return _spawn_container(bundles=bundles, extra_env=extra_env, port=port)
