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
# oidc_proxy mode: the container is the authorization server the client sees
# ---------------------------------------------------------------------------

# The advertised base_url can't be the real (random) port — the env is fixed before
# spawn_container picks one. The routes serve at the real port either way, and what these assert is
# *which* server the metadata names.
PROXY_BASE_URL = "http://127.0.0.1:8000"


def test_oidc_proxy_advertises_the_container_as_the_authorization_server(oidc_issuer, spawn_container):
    """The container serves RFC 8414 AS metadata naming *itself* — the capability `jwks` lacks.

    `jwks` advertises the upstream issuer and expects the client to register there, which a
    closed-DCR issuer refuses. Here every endpoint the client is handed is the container's own.
    """
    import httpx

    issuer, _ = oidc_issuer
    url, _ = spawn_container(
        KX_MCP_AUTH="oidc_proxy",
        KX_MCP_AUTH_ISSUER=issuer,
        KX_MCP_AUTH_CLIENT_ID="pre-registered-app",
        KX_MCP_AUTH_CLIENT_SECRET="pre-registered-secret",
        KX_MCP_AUTH_AUDIENCE=AUDIENCE,
        KX_MCP_AUTH_RESOURCE_URL=PROXY_BASE_URL,
    )
    origin = url.rsplit("/mcp", 1)[0]

    meta = httpx.get(f"{origin}/.well-known/oauth-authorization-server")
    assert meta.status_code == 200, meta.text
    body = meta.json()

    # The container names itself, not the upstream issuer — the defining difference from jwks.
    assert body["issuer"].rstrip("/") == PROXY_BASE_URL
    for endpoint in ("authorization_endpoint", "token_endpoint", "registration_endpoint"):
        assert body[endpoint].startswith(PROXY_BASE_URL), (endpoint, body[endpoint])
        assert not body[endpoint].startswith(issuer), f"{endpoint} leaks the upstream issuer"


def test_oidc_proxy_accepts_local_dynamic_client_registration(oidc_issuer, spawn_container):
    """The DCR facade is live: a client registers with the container and gets usable credentials.

    The fixture issuer has no registration endpoint, so only the proxy can be answering.
    """
    import httpx

    issuer, _ = oidc_issuer
    url, _ = spawn_container(
        KX_MCP_AUTH="oidc_proxy",
        KX_MCP_AUTH_ISSUER=issuer,
        KX_MCP_AUTH_CLIENT_ID="pre-registered-app",
        KX_MCP_AUTH_CLIENT_SECRET="pre-registered-secret",
        KX_MCP_AUTH_AUDIENCE=AUDIENCE,
        KX_MCP_AUTH_RESOURCE_URL=PROXY_BASE_URL,
    )
    origin = url.rsplit("/mcp", 1)[0]

    # Follow the advertised registration path rather than hard-coding FastMCP's route.
    advertised = httpx.get(f"{origin}/.well-known/oauth-authorization-server").json()
    register_path = "/" + advertised["registration_endpoint"].split("/", 3)[3]

    resp = httpx.post(
        f"{origin}{register_path}",
        json={
            "client_name": "kx-integration-test-client",
            "redirect_uris": ["http://localhost:9999/callback"],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "client_secret_post",
        },
    )
    assert resp.status_code in (200, 201), resp.text
    registered = resp.json()
    assert registered["client_id"]
    assert registered["redirect_uris"] == ["http://localhost:9999/callback"]


def test_oidc_proxy_still_rejects_an_unauthenticated_call(oidc_issuer, spawn_container):
    """Fronting the login does not weaken the gate: no bearer is still 401, with the PRM pointer."""
    import httpx

    issuer, _ = oidc_issuer
    url, _ = spawn_container(
        KX_MCP_AUTH="oidc_proxy",
        KX_MCP_AUTH_ISSUER=issuer,
        KX_MCP_AUTH_CLIENT_ID="pre-registered-app",
        KX_MCP_AUTH_CLIENT_SECRET="pre-registered-secret",
        KX_MCP_AUTH_AUDIENCE=AUDIENCE,
        KX_MCP_AUTH_RESOURCE_URL=PROXY_BASE_URL,
    )
    origin = url.rsplit("/mcp", 1)[0]

    prm = httpx.get(f"{origin}/.well-known/oauth-protected-resource/mcp")
    assert prm.status_code == 200, prm.text
    # The client is sent to the container, not the upstream issuer, to authorize.
    assert [s.rstrip("/") for s in prm.json()["authorization_servers"]] == [PROXY_BASE_URL]

    resp = httpx.post(
        url,
        headers={"Accept": "application/json, text/event-stream", "Content-Type": "application/json"},
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
    )
    assert resp.status_code == 401
    assert "resource_metadata" in resp.headers.get("www-authenticate", "")


@pytest.mark.parametrize(
    "strict,expect_start",
    [("true", False), ("false", True)],
    ids=["strict-rejects", "lenient-accepts"],
)
def test_oidc_proxy_strict_discovery_switch(oidc_issuer, spawn_container, strict, expect_start):
    """A document missing an OIDC-required field starts only with the strict opt-out. Real issuers
    do omit fields (`subject_types_supported` most often), so both halves are pinned."""
    _, lenient_config_url = oidc_issuer
    env = dict(
        KX_MCP_AUTH="oidc_proxy",
        KX_MCP_AUTH_CONFIG_URL=lenient_config_url,
        KX_MCP_AUTH_CLIENT_ID="pre-registered-app",
        KX_MCP_AUTH_CLIENT_SECRET="pre-registered-secret",
        KX_MCP_AUTH_AUDIENCE=AUDIENCE,
        KX_MCP_AUTH_RESOURCE_URL=PROXY_BASE_URL,
        KX_MCP_AUTH_OIDC_STRICT=strict,
    )
    if expect_start:
        url, _ = spawn_container(**env)
        assert url
    else:
        with pytest.raises(RuntimeError, match="exited"):
            spawn_container(**env)


def test_oidc_proxy_unreachable_issuer_fails_at_startup(spawn_container):
    """Discovery runs in the constructor, so a bad issuer refuses to start rather than failing at
    first login — and the container won't start while the IdP is down."""
    with pytest.raises(RuntimeError) as excinfo:
        spawn_container(
            KX_MCP_AUTH="oidc_proxy",
            KX_MCP_AUTH_ISSUER="http://127.0.0.1:1/realms/nope",
            KX_MCP_AUTH_CLIENT_ID="pre-registered-app",
            KX_MCP_AUTH_CLIENT_SECRET="pre-registered-secret",
            KX_MCP_AUTH_RESOURCE_URL=PROXY_BASE_URL,
        )
    assert "exited" in str(excinfo.value).lower()


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
