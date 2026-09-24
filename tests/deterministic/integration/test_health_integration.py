"""The ``/health`` route over the wire, against the real launcher process.

What an orchestrator actually does: a plain HTTP GET from outside the process, no MCP client and no
credentials. The one place both operator-facing properties are proven together — the probe answers
200 while the MCP endpoint enforces auth, and needs no bearer to do it. Routing itself is pinned
in ``unit/test_health.py``.

Uses the license-free ``example`` bundle. Fixture ``keypair`` comes from the repo-root
``conftest.py``; ``spawn_container`` from ``tests/conftest.py``.
"""

from __future__ import annotations

import httpx
import pytest

ISSUER = "https://issuer.test"
AUDIENCE = "kx-mcp"

_MCP_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
}
_TOOLS_LIST = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}


def _health_url(mcp_url: str) -> str:
    return f"{mcp_url.rsplit('/mcp', 1)[0]}/health"


@pytest.mark.integration
def test_health_answers_without_auth_configured(spawn_container):
    """The bundling posture (``KX_MCP_AUTH`` unset): ``curl -f /health`` → 200."""
    url, _ = spawn_container()

    resp = httpx.get(_health_url(url), timeout=10)

    assert resp.status_code == 200, resp.text
    assert resp.text == "OK"


@pytest.mark.integration
def test_health_answers_unauthenticated_while_mcp_is_protected(tmp_path, keypair, spawn_container):
    """Auth is enforced on ``/mcp`` and the probe still needs no bearer — same live process.

    A kubelet has no token, so a probe that 401s marks a healthy pod dead and restarts it in a loop.
    """
    _priv, pub = keypair
    pub_path = tmp_path / "public.pem"
    pub_path.write_text(pub)

    url, _ = spawn_container(
        KX_MCP_AUTH="static",
        KX_MCP_AUTH_PUBLIC_KEY_PATH=str(pub_path),
        KX_MCP_AUTH_ISSUER=ISSUER,
        KX_MCP_AUTH_AUDIENCE=AUDIENCE,
    )

    health = httpx.get(_health_url(url), timeout=10)
    mcp = httpx.post(url, headers=_MCP_HEADERS, json=_TOOLS_LIST, timeout=10)

    assert health.status_code == 200, health.text
    assert health.text == "OK"
    assert mcp.status_code == 401, mcp.text
