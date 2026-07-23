"""Inbound-auth over the wire — the bug class that verifier-level tests can't catch.

Spawns the launcher as a real ``streamable-http`` child process and drives it with an HTTP client.
Tests verify:
- valid bearer reaches a mounted tool and surfaces the authenticated principal
- principal set by parent auth is readable inside a mounted bundle's tool (2.23 / 2.32)
- missing, expired, wrong-audience, and scope-insufficient bearers are rejected with 4xx
- jwks mode works over the wire (subprocess fetches the JWKS URI from an in-process server)
- unset mode (KX_MCP_AUTH="") runs with no auth and no bearer required

All tests use the license-free ``example`` bundle (no PyKX / no kdb-x process needed).

Fixtures ``keypair``, ``mint``, ``jwks_uri`` come from the repo-root ``conftest.py``;
``spawn_container`` comes from ``tests/conftest.py``.
"""

from __future__ import annotations

import asyncio

import pytest
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

ISSUER = "https://issuer.test"
AUDIENCE = "kx-mcp"

# keypair / mint / jwks_uri / spawn_container — injected from conftest files


# ---------------------------------------------------------------------------
# static mode: happy path
# ---------------------------------------------------------------------------


def test_valid_bearer_reaches_mounted_tool(tmp_path, keypair, mint, spawn_container):
    """Valid RS256 bearer accepted; tool result returned (2.30)."""
    _, pub = keypair
    pub_path = tmp_path / "public.pem"
    pub_path.write_text(pub)

    url, _ = spawn_container(
        KX_MCP_AUTH="static",
        KX_MCP_AUTH_PUBLIC_KEY_PATH=str(pub_path),
        KX_MCP_AUTH_ISSUER=ISSUER,
        KX_MCP_AUTH_AUDIENCE=AUDIENCE,
    )

    async def go() -> str:
        async with Client(StreamableHttpTransport(url, auth=mint())) as client:
            names = {t.name for t in await client.list_tools()}
            assert "example_echo" in names
            return (await client.call_tool("example_echo", {"text": "hello"})).data

    assert asyncio.run(go()) == "hello"


def test_authenticated_principal_visible_in_mounted_tool(tmp_path, keypair, mint, spawn_container):
    """Parent's validated principal is readable inside a mounted bundle's tool (2.23 / 2.32).

    This is the core assumption: after the parent validates the bearer the resulting
    AccessToken contextvar must cross the namespace mount boundary so backend tools can see who
    is calling. Verifier-level tests can't catch this because the contextvar is only populated
    by the HTTP auth layer, not by in-process clients.
    """
    _, pub = keypair
    pub_path = tmp_path / "public.pem"
    pub_path.write_text(pub)

    url, _ = spawn_container(
        KX_MCP_AUTH="static",
        KX_MCP_AUTH_PUBLIC_KEY_PATH=str(pub_path),
        KX_MCP_AUTH_ISSUER=ISSUER,
        KX_MCP_AUTH_AUDIENCE=AUDIENCE,
    )

    async def go() -> str:
        # example_whoami returns current_principal().client_id or "anonymous"
        async with Client(StreamableHttpTransport(url, auth=mint())) as client:
            return (await client.call_tool("example_whoami", {})).data

    assert asyncio.run(go()) == "alice"


# ---------------------------------------------------------------------------
# static mode: rejection cases
# ---------------------------------------------------------------------------


def test_missing_bearer_is_rejected_401(tmp_path, keypair, spawn_container):
    """No bearer at all → 401 before any backend runs (2.31)."""
    _, pub = keypair
    pub_path = tmp_path / "public.pem"
    pub_path.write_text(pub)

    url, _ = spawn_container(
        KX_MCP_AUTH="static",
        KX_MCP_AUTH_PUBLIC_KEY_PATH=str(pub_path),
        KX_MCP_AUTH_ISSUER=ISSUER,
        KX_MCP_AUTH_AUDIENCE=AUDIENCE,
    )

    async def go():
        async with Client(StreamableHttpTransport(url)) as client:
            await client.call_tool("example_echo", {"text": "hello"})

    with pytest.raises(Exception) as exc_info:
        asyncio.run(go())
    assert "401" in str(exc_info.value) or "Unauthorized" in str(exc_info.value)


def test_expired_bearer_is_rejected_401(tmp_path, keypair, mint, spawn_container):
    """Expired token → 401 (2.34)."""
    _, pub = keypair
    pub_path = tmp_path / "public.pem"
    pub_path.write_text(pub)

    url, _ = spawn_container(
        KX_MCP_AUTH="static",
        KX_MCP_AUTH_PUBLIC_KEY_PATH=str(pub_path),
        KX_MCP_AUTH_ISSUER=ISSUER,
        KX_MCP_AUTH_AUDIENCE=AUDIENCE,
    )

    async def go():
        async with Client(StreamableHttpTransport(url, auth=mint(exp_delta=-10))) as client:
            await client.call_tool("example_echo", {"text": "hello"})

    with pytest.raises(Exception) as exc_info:
        asyncio.run(go())
    assert "401" in str(exc_info.value) or "Unauthorized" in str(exc_info.value)


def test_wrong_audience_is_rejected_401(tmp_path, keypair, mint, spawn_container):
    """Token with wrong audience → 401 (2.35)."""
    _, pub = keypair
    pub_path = tmp_path / "public.pem"
    pub_path.write_text(pub)

    url, _ = spawn_container(
        KX_MCP_AUTH="static",
        KX_MCP_AUTH_PUBLIC_KEY_PATH=str(pub_path),
        KX_MCP_AUTH_ISSUER=ISSUER,
        KX_MCP_AUTH_AUDIENCE=AUDIENCE,
    )

    async def go():
        async with Client(StreamableHttpTransport(url, auth=mint(aud="wrong-audience"))) as client:
            await client.call_tool("example_echo", {"text": "hello"})

    with pytest.raises(Exception) as exc_info:
        asyncio.run(go())
    assert "401" in str(exc_info.value) or "Unauthorized" in str(exc_info.value)


def test_missing_required_scope_is_rejected(tmp_path, keypair, mint, spawn_container):
    """Token lacking a required scope is rejected (2.36).

    Spawns the container with ``KX_MCP_AUTH_REQUIRED_SCOPES=kdbx.admin``; the minted token
    only has ``kdbx.read``. The verifier returns None → FastMCP returns a 4xx before the tool runs.
    """
    _, pub = keypair
    pub_path = tmp_path / "public.pem"
    pub_path.write_text(pub)

    url, _ = spawn_container(
        KX_MCP_AUTH="static",
        KX_MCP_AUTH_PUBLIC_KEY_PATH=str(pub_path),
        KX_MCP_AUTH_ISSUER=ISSUER,
        KX_MCP_AUTH_AUDIENCE=AUDIENCE,
        KX_MCP_AUTH_REQUIRED_SCOPES="kdbx.admin",
    )

    async def go():
        # mint() defaults to scope="kdbx.read" — does not satisfy kdbx.admin
        async with Client(StreamableHttpTransport(url, auth=mint())) as client:
            await client.call_tool("example_echo", {"text": "hello"})

    with pytest.raises(Exception) as exc_info:
        asyncio.run(go())
    err = str(exc_info.value)
    assert "401" in err or "403" in err or "Unauthorized" in err or "Forbidden" in err


# ---------------------------------------------------------------------------
# jwks mode: over-the-wire
# ---------------------------------------------------------------------------


def test_jwks_valid_bearer_reaches_mounted_tool(keypair, mint, jwks_uri, spawn_container):
    """jwks mode end-to-end: subprocess fetches the in-process JWKS endpoint and validates (2.33).

    This exercises the JWKS fetch path that static mode skips — a different code path in
    FastMCP's JWTVerifier that can only be tested with an actual HTTP exchange.
    The ``jwks_uri`` fixture serves the test keypair from a localhost ThreadingHTTPServer,
    reachable from the subprocess because both live on 127.0.0.1.
    """
    url, _ = spawn_container(
        KX_MCP_AUTH="jwks",
        KX_MCP_AUTH_JWKS_URI=jwks_uri,
        KX_MCP_AUTH_ISSUER=ISSUER,
        KX_MCP_AUTH_AUDIENCE=AUDIENCE,
    )

    async def go() -> str:
        async with Client(StreamableHttpTransport(url, auth=mint())) as client:
            return (await client.call_tool("example_echo", {"text": "jwks-ok"})).data

    assert asyncio.run(go()) == "jwks-ok"


# ---------------------------------------------------------------------------
# jwks discovery: RFC 9728 Protected Resource Metadata advertised over the wire
# ---------------------------------------------------------------------------


def test_jwks_resource_url_advertises_discovery_over_the_wire(jwks_uri, spawn_container):
    """With KX_MCP_AUTH_RESOURCE_URL set, the container serves PRM and points its 401 at it (RFC 9728).

    This is the discovery an MCP client (Claude Code) / ``kx auth login`` needs: the server advertises
    its authorization server, so the client never hand-configures the IdP. A bare verifier (no
    resource_url) serves none of this — the catch verifier-level tests can't make (the bug that the
    container 401'd with no ``resource_metadata`` pointer and 404'd ``/.well-known/...``).
    """
    import httpx

    # The resource_url only sets the advertised ``resource`` value; the PRM route is served at the
    # real (random) port regardless, so a placeholder is fine for asserting the discovery wiring.
    url, _ = spawn_container(
        KX_MCP_AUTH="jwks",
        KX_MCP_AUTH_JWKS_URI=jwks_uri,
        KX_MCP_AUTH_ISSUER=ISSUER,
        KX_MCP_AUTH_AUDIENCE=AUDIENCE,
        KX_MCP_AUTH_RESOURCE_URL="http://127.0.0.1:8000",
    )
    origin = url.rsplit("/mcp", 1)[0]

    # PRM endpoint (path-aware form) advertises the issuer as the authorization server.
    meta = httpx.get(f"{origin}/.well-known/oauth-protected-resource/mcp")
    assert meta.status_code == 200, meta.text
    assert [s.rstrip("/") for s in meta.json()["authorization_servers"]] == [ISSUER]

    # An unauthenticated call is 401 and points at that metadata (WWW-Authenticate resource_metadata).
    resp = httpx.post(
        url,
        headers={"Accept": "application/json, text/event-stream", "Content-Type": "application/json"},
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
    )
    assert resp.status_code == 401
    assert "resource_metadata" in resp.headers.get("www-authenticate", "")


# ---------------------------------------------------------------------------
# unset mode: bundling posture (no auth, no bearer required)
# ---------------------------------------------------------------------------


def test_unset_auth_no_bearer_works(spawn_container):
    """KX_MCP_AUTH=unset (the bundling posture): tool call succeeds with no bearer (2.37).

    Passing KX_MCP_AUTH="" explicitly ensures no ambient env var overrides the test,
    regardless of what the test runner's environment has set.
    """
    url, _ = spawn_container(KX_MCP_AUTH="")

    async def go() -> str:
        async with Client(StreamableHttpTransport(url)) as client:
            return (await client.call_tool("example_echo", {"text": "anon"})).data

    assert asyncio.run(go()) == "anon"
