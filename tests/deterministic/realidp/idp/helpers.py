"""Assertion helpers for the real-IdP inbound-authentication tests (2.38–2.41).

These helpers are provider-agnostic — they work against any IdP and any backend bundle,
as long as the container is running with ``KX_MCP_AUTH=jwks``.

KA.* ACL helpers (kdbai-specific) live in ``realidp.kdbai.helpers``.
"""

from __future__ import annotations

import asyncio

import pytest
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport


def assert_authenticated(container_url: str, token: str) -> None:
    """Assert that ``token`` is accepted by the container and a tool call succeeds."""

    async def _go() -> str:
        async with Client(StreamableHttpTransport(container_url, auth=token)) as c:
            return (await c.call_tool("example_echo", {"text": "realidp-ok"})).data

    result = asyncio.run(_go())
    assert result == "realidp-ok", f"unexpected tool result: {result!r}"


def assert_principal_visible(container_url: str, token: str) -> str:
    """Assert that token is accepted AND returns a non-anonymous principal from whoami."""

    async def _go() -> str:
        async with Client(StreamableHttpTransport(container_url, auth=token)) as c:
            return (await c.call_tool("example_whoami", {})).data

    principal = asyncio.run(_go())
    assert principal != "anonymous", "principal was anonymous — token not propagated to tool"
    return principal


def assert_discovery_advertised(container_url: str) -> None:
    """Assert that the container advertises RFC 9728 Protected Resource Metadata (2.42).

    Requires the container to have been spawned with ``KX_MCP_AUTH_RESOURCE_URL`` set
    (done by ``_spawn_container`` for all realidp sessions). Fetches the well-known PRM
    document and checks that at least one ``authorization_servers`` entry is present.

    Mirrors the over-the-wire assertion in
    ``tests/deterministic/integration/test_auth_integration.py``
    ``test_jwks_resource_url_advertises_discovery_over_the_wire``.
    """
    import httpx

    origin = container_url.rsplit("/mcp", 1)[0]
    resp = httpx.get(f"{origin}/.well-known/oauth-protected-resource/mcp")
    assert resp.status_code == 200, (
        f"Expected 200 from PRM endpoint, got {resp.status_code}: {resp.text}"
    )
    body = resp.json()
    assert body.get("authorization_servers"), (
        f"No 'authorization_servers' in PRM response: {body}"
    )


def assert_rejected_401(
    container_url: str,
    token: str | None = None,
    *,
    tool: str = "example_echo",
    params: dict | None = None,
) -> None:
    """Assert that ``token`` (or no token) is rejected with HTTP 401.

    ``tool`` and ``params`` let callers use a tool name that exists on the running bundle
    (e.g. ``tool="kdbai_list_tables"`` for the kdbai ACL lane, which has no ``example_echo``).
    The auth check fires before tool dispatch so the tool name doesn't affect the result.
    """
    async def _go() -> None:
        transport_kwargs: dict = {}
        if token is not None:
            transport_kwargs["auth"] = token
        async with Client(StreamableHttpTransport(container_url, **transport_kwargs)) as c:
            await c.call_tool(tool, params or {})

    with pytest.raises(Exception) as exc_info:
        asyncio.run(_go())
    err = str(exc_info.value)
    assert "401" in err or "Unauthorized" in err, (
        f"Expected 401 Unauthorized but got: {err!r}"
    )
