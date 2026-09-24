"""The container-owned ``GET /health`` liveness route (attached in ``make_parent``).

Covers: served by default (200 ``OK``, GET-only); ``health=False`` opts out; reachable with no
bearer in the same app where ``/mcp`` 401s; a mounted bundle's own ``/health`` cannot displace the
parent's; probe traffic emits no audit line.

Driven against the real ASGI app from ``http_app()`` with its lifespan entered, so the routing and
middleware order under test are the ones served in production. Proven over the wire in
``integration/test_health_integration.py``.

Fixture ``keypair`` comes from the repo-root ``conftest.py``.
"""

from __future__ import annotations

import asyncio
import json
from typing import Awaitable, Callable, TypeVar

import httpx
import pytest
from fastmcp import FastMCP
from starlette.responses import PlainTextResponse

from kx_mcp_core import make_parent
from kx_mcp_core.assembly import HEALTH_PATH
from kx_mcp_core.auth import AuthSettings, build_auth_provider

ISSUER = "https://issuer.test"
AUDIENCE = "kx-mcp"

_BASE = "http://kx-mcp.test"
_MCP_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
}
_TOOLS_LIST = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}

T = TypeVar("T")


def _serve(parent: FastMCP, exercise: Callable[[httpx.AsyncClient], Awaitable[T]]) -> T:
    """Run ``exercise`` against the parent's real streamable-http ASGI app.

    The lifespan is entered because the MCP endpoint's session manager starts there — without it a
    ``/mcp`` request raises instead of answering, so an auth assertion would prove nothing.
    """

    async def _run() -> T:
        app = parent.http_app()
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app)
            async with httpx.AsyncClient(transport=transport, base_url=_BASE) as client:
                return await exercise(client)

    return asyncio.run(_run())


def _static_auth(pub_path):
    return build_auth_provider(
        AuthSettings(
            mode="static",
            public_key_path=str(pub_path),
            issuer=ISSUER,
            audience=AUDIENCE,
        )
    )


def test_health_is_served_by_default():
    resp = _serve(make_parent("test"), lambda c: c.get(HEALTH_PATH))
    assert resp.status_code == 200
    assert resp.text == "OK"


def test_health_path_is_the_conventional_one():
    """Pinned: probe configs (k8s manifests, Compose healthchecks) hard-code this path."""
    assert HEALTH_PATH == "/health"


def test_health_opt_out_leaves_no_route():
    resp = _serve(make_parent("test", health=False), lambda c: c.get(HEALTH_PATH))
    assert resp.status_code == 404


def test_health_needs_no_bearer_while_mcp_does(tmp_path, keypair):
    """Auth is active, ``/mcp`` 401s, and the probe still gets its 200.

    A regression here — an auth mode that rejects app-wide rather than on the MCP route — would
    silently restart every healthy pod in a loop.
    """
    _priv, pub = keypair
    pub_path = tmp_path / "pub.pem"
    pub_path.write_text(pub)

    async def _both(client: httpx.AsyncClient):
        health = await client.get(HEALTH_PATH)
        mcp = await client.post("/mcp", headers=_MCP_HEADERS, content=json.dumps(_TOOLS_LIST))
        return health, mcp

    health, mcp = _serve(make_parent("test", auth=_static_auth(pub_path)), _both)

    assert health.status_code == 200
    assert health.text == "OK"
    assert mcp.status_code == 401


def test_health_is_not_audited(caplog):
    """Audit records MCP dispatch, not HTTP routes; probe traffic must not flood it."""
    with caplog.at_level("INFO", logger="kx_mcp.audit"):
        resp = _serve(make_parent("test"), lambda c: c.get(HEALTH_PATH))
    assert resp.status_code == 200
    assert not [r for r in caplog.records if r.name == "kx_mcp.audit"]


def test_parent_health_wins_over_a_mounted_bundles_own():
    """A bundle carrying a legacy ``/health`` cannot answer for the pod.

    FastMCP forwards a mounted child's ``custom_route`` up to the parent app, parent routes first.
    Without that ordering, a bundle's private view of its own state would decide the composition's
    liveness.
    """
    child = FastMCP("legacy-bundle")

    @child.custom_route(HEALTH_PATH, methods=["GET"])
    async def child_health(_request):  # pragma: no cover - must never be reached
        return PlainTextResponse("BUNDLE")

    parent = make_parent("test")
    parent.mount(child, namespace="legacy")

    resp = _serve(parent, lambda c: c.get(HEALTH_PATH))
    assert resp.status_code == 200
    assert resp.text == "OK"


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE"])
def test_health_is_get_only(method):
    """Probes are GETs; anything else is a misconfiguration, so 405 rather than 200."""
    resp = _serve(make_parent("test"), lambda c: c.request(method, HEALTH_PATH))
    assert resp.status_code == 405
