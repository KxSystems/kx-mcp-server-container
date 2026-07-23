"""Inbound-auth verifier seam: unset / static / jwks, plus the audit scaffolding.

Covers:
- Provider building: unset (None), static (local public key), jwks (remote JWKS endpoint).
- Claim validation: issuer, audience, scope, expiry, bad signature, kid mismatch.
- Config binding: KX_MCP_AUTH env var, case normalisation, empty/whitespace → unset.
- Principal accessor: current_principal() returns None in unset mode; crosses mount boundary
  (proven over-the-wire in test_auth_integration).
- Audit middleware: subject=anonymous/authenticated, outcome=ok/error, all three action kinds
  (tool_invoke, resource_read, prompt_get), audit=False suppression.

Fixtures ``keypair``, ``mint``, ``other_priv``, ``jwks_uri`` and constants ``KID``/``ISSUER``/
``AUDIENCE`` come from the repo-root ``conftest.py``, shared with ``kx-auth-core``/``kx-auth-cli``.
No subprocess or HTTP server needed — all tests are in-process.
"""

from __future__ import annotations

import asyncio
import logging
import time

import jwt
import pytest
from fastmcp import Client, FastMCP

from kx_mcp_core import make_parent
from kx_mcp_core.auth import (
    AuthSettings,
    build_auth_provider,
    current_principal,
    register_auth_mode,
)
from kx_mcp_core.launcher import build_app

# KID / ISSUER / AUDIENCE / keypair / mint / other_priv / jwks_uri — injected from root conftest.py
ISSUER = "https://issuer.test"
AUDIENCE = "kx-mcp"


# --- unset -------------------------------------------------------------------------------------


def test_unset_builds_no_provider():
    assert build_auth_provider(AuthSettings(mode="unset")) is None


def test_unset_is_no_auth_and_no_principal():
    """A tool call succeeds with no token, and the tool sees no principal (anonymous)."""
    srv = FastMCP("noauth")

    @srv.tool
    def whoami() -> str:
        principal = current_principal()
        return "anonymous" if principal is None else principal.client_id

    async def go() -> str:
        async with Client(srv) as client:
            return (await client.call_tool("whoami", {})).data

    assert asyncio.run(go()) == "anonymous"


# --- static (RS256 against a local public key) -------------------------------------------------


def test_static_accepts_valid_rejects_expired_and_bad_signature(keypair, mint, other_priv):
    _, pub = keypair
    provider = build_auth_provider(
        AuthSettings(mode="static", public_key=pub, issuer=ISSUER, audience=AUDIENCE)
    )

    async def go():
        good = await provider.verify_token(mint())
        expired = await provider.verify_token(mint(exp_delta=-10))
        bad_sig = await provider.verify_token(mint(priv=other_priv))
        return good, expired, bad_sig

    good, expired, bad_sig = asyncio.run(go())
    assert good is not None and good.client_id == "alice" and "kdbx.read" in good.scopes
    assert expired is None
    assert bad_sig is None


def test_static_requires_a_public_key():
    with pytest.raises(ValueError, match="PUBLIC_KEY"):
        build_auth_provider(AuthSettings(mode="static", issuer=ISSUER))


def test_static_loads_public_key_from_file_path(tmp_path, keypair, mint):
    """KX_MCP_AUTH_PUBLIC_KEY_PATH is a supported alternative to an inline KX_MCP_AUTH_PUBLIC_KEY."""
    _, pub = keypair
    pub_path = tmp_path / "public.pem"
    pub_path.write_text(pub)

    provider = build_auth_provider(
        AuthSettings(mode="static", public_key_path=str(pub_path), issuer=ISSUER, audience=AUDIENCE)
    )

    async def go():
        return await provider.verify_token(mint())

    result = asyncio.run(go())
    assert result is not None and result.client_id == "alice"


def test_static_wrong_issuer_rejected(keypair, mint):
    """Token with a different issuer than the verifier expects is rejected (auth-bypass guard)."""
    _, pub = keypair
    provider = build_auth_provider(
        AuthSettings(mode="static", public_key=pub, issuer=ISSUER, audience=AUDIENCE)
    )

    async def go():
        return await provider.verify_token(mint(iss="https://evil.test"))

    assert asyncio.run(go()) is None


def test_static_wrong_audience_rejected(keypair, mint):
    """Token with the wrong audience is rejected (auth-bypass guard)."""
    _, pub = keypair
    provider = build_auth_provider(
        AuthSettings(mode="static", public_key=pub, issuer=ISSUER, audience=AUDIENCE)
    )

    async def go():
        return await provider.verify_token(mint(aud="wrong-audience"))

    assert asyncio.run(go()) is None


def test_static_missing_required_scope_rejected(keypair, mint):
    """Token lacking a required scope is rejected even if the signature and claims are valid."""
    _, pub = keypair
    provider = build_auth_provider(
        AuthSettings(
            mode="static",
            public_key=pub,
            issuer=ISSUER,
            audience=AUDIENCE,
            required_scopes=["kdbx.admin"],
        )
    )

    async def go():
        # mint() defaults to scope="kdbx.read" — does not include kdbx.admin
        return await provider.verify_token(mint(scope="kdbx.read"))

    assert asyncio.run(go()) is None


# --- jwks (RS256 against a remote JWKS endpoint) -----------------------------------------------
# jwks_uri fixture comes from root conftest.py


def test_jwks_validates_minted_token_rejects_expired_and_bad_signature(mint, other_priv, jwks_uri):
    provider = build_auth_provider(
        AuthSettings(mode="jwks", jwks_uri=jwks_uri, issuer=ISSUER, audience=AUDIENCE)
    )

    async def go():
        good = await provider.verify_token(mint())
        expired = await provider.verify_token(mint(exp_delta=-10))
        bad_sig = await provider.verify_token(mint(priv=other_priv))
        return good, expired, bad_sig

    good, expired, bad_sig = asyncio.run(go())
    assert good is not None and good.client_id == "alice"
    assert expired is None
    assert bad_sig is None


def test_jwks_requires_a_uri():
    with pytest.raises(ValueError, match="JWKS_URI"):
        build_auth_provider(AuthSettings(mode="jwks", issuer=ISSUER))


def test_jwks_kid_mismatch_rejected(keypair, jwks_uri):
    """Token with a kid absent from the JWKS is rejected — the common key-rotation misconfiguration.

    The ``mint`` fixture always uses ``kid=KID`` ("test-key-1"), so we mint inline here with a
    different ``kid`` header. The JWKS endpoint (from root conftest) only advertises "test-key-1".
    """
    priv, _ = keypair
    now = int(time.time())
    token = jwt.encode(
        {
            "iss": ISSUER, "aud": AUDIENCE, "sub": "alice", "client_id": "alice",
            "scope": "kdbx.read", "iat": now, "exp": now + 3600,
        },
        priv,
        algorithm="RS256",
        headers={"kid": "unknown-kid"},  # not in the JWKS
    )
    provider = build_auth_provider(
        AuthSettings(mode="jwks", jwks_uri=jwks_uri, issuer=ISSUER, audience=AUDIENCE)
    )

    async def go():
        return await provider.verify_token(token)

    assert asyncio.run(go()) is None


def test_jwks_wrong_issuer_rejected(mint, jwks_uri):
    """Same auth-bypass guard as static mode, exercised on the jwks code path."""
    provider = build_auth_provider(
        AuthSettings(mode="jwks", jwks_uri=jwks_uri, issuer=ISSUER, audience=AUDIENCE)
    )

    async def go():
        return await provider.verify_token(mint(iss="https://evil.test"))

    assert asyncio.run(go()) is None


def test_jwks_wrong_audience_rejected(mint, jwks_uri):
    """Same auth-bypass guard as static mode, exercised on the jwks code path."""
    provider = build_auth_provider(
        AuthSettings(mode="jwks", jwks_uri=jwks_uri, issuer=ISSUER, audience=AUDIENCE)
    )

    async def go():
        return await provider.verify_token(mint(aud="wrong-audience"))

    assert asyncio.run(go()) is None


# --- jwks discovery advertisement (RFC 9728 Protected Resource Metadata) -----------------------
# KX_MCP_AUTH_RESOURCE_URL set => wrap the verifier in a RemoteAuthProvider that serves the PRM
# routes a client (Claude Code / `kx auth login`) discovers the IdP against.


def test_jwks_without_resource_url_is_bare_verifier(jwks_uri):
    """No KX_MCP_AUTH_RESOURCE_URL => bare JWTVerifier, discovery off (backward-compatible default)."""
    from fastmcp.server.auth.providers.jwt import JWTVerifier

    provider = build_auth_provider(
        AuthSettings(mode="jwks", jwks_uri=jwks_uri, issuer=ISSUER, audience=AUDIENCE)
    )
    assert isinstance(provider, JWTVerifier)


def test_jwks_with_resource_url_advertises_protected_resource_metadata(jwks_uri):
    """KX_MCP_AUTH_RESOURCE_URL set => RemoteAuthProvider serving PRM with the issuer as the AS."""
    from fastmcp.server.auth import RemoteAuthProvider

    provider = build_auth_provider(
        AuthSettings(
            mode="jwks",
            jwks_uri=jwks_uri,
            issuer=ISSUER,
            audience=AUDIENCE,
            resource_url="http://127.0.0.1:8000",
        )
    )
    assert isinstance(provider, RemoteAuthProvider)
    # AnyHttpUrl normalises a host-only URL with a trailing slash; issuers with a path are unchanged.
    assert [str(u).rstrip("/") for u in provider.authorization_servers] == [ISSUER]
    # It still validates tokens (delegates to the wrapped verifier).
    routes = provider.get_routes("/mcp")
    paths = [getattr(r, "path", "") for r in routes]
    assert any("/.well-known/oauth-protected-resource" in p for p in paths), paths


def test_jwks_resource_url_without_issuer_raises(jwks_uri):
    """Can't advertise an authorization server we don't know — a clear operator error."""
    with pytest.raises(ValueError, match="KX_MCP_AUTH_ISSUER"):
        build_auth_provider(
            AuthSettings(mode="jwks", jwks_uri=jwks_uri, resource_url="http://127.0.0.1:8000")
        )


# --- entra (AzureProvider OAuth-Proxy front door) ----------------------------------------------
# Used when the container must *log a client in* against Entra (no open DCR). Validation underneath
# is the same JWTVerifier, so downstream of current_principal is unchanged; we assert the builder
# wires the pre-registered app config through and fails clearly when it's incomplete.

_ENTRA = dict(
    mode="entra",
    client_id="app-client-id",
    client_secret="app-secret",
    tenant_id="tenant-id",
    required_scopes=["access"],
    resource_url="http://localhost:8000",
)


def test_entra_builds_azure_provider(monkeypatch):
    """Happy path: builder constructs an AzureProvider with the pre-registered app config."""
    import fastmcp.server.auth.providers.azure as azure_mod

    captured = {}

    class FakeAzureProvider:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(azure_mod, "AzureProvider", FakeAzureProvider)

    provider = build_auth_provider(AuthSettings(**_ENTRA, identifier_uri="api://app-client-id"))

    assert isinstance(provider, FakeAzureProvider)
    assert captured == {
        "client_id": "app-client-id",
        "client_secret": "app-secret",
        "tenant_id": "tenant-id",
        "required_scopes": ["access"],
        "base_url": "http://localhost:8000",
        "identifier_uri": "api://app-client-id",
    }


@pytest.mark.parametrize("drop", ["client_id", "client_secret", "tenant_id", "resource_url"])
def test_entra_missing_app_config_raises(drop):
    """Each missing app credential / public URL is a clear operator error, not a deep stack trace."""
    cfg = {k: v for k, v in _ENTRA.items() if k != drop}
    with pytest.raises(ValueError, match="KX_MCP_AUTH=entra requires"):
        build_auth_provider(AuthSettings(**cfg))


def test_entra_missing_required_scopes_raises():
    cfg = {k: v for k, v in _ENTRA.items() if k != "required_scopes"}
    with pytest.raises(ValueError, match="REQUIRED_SCOPES"):
        build_auth_provider(AuthSettings(**cfg))


def test_entra_reads_azure_env_aliases(monkeypatch):
    """The AZURE_* env names an existing app registration uses are accepted, not just KX_MCP_AUTH_*."""
    monkeypatch.setenv("KX_MCP_AUTH", "entra")
    monkeypatch.setenv("AZURE_CLIENT_ID", "env-client")
    monkeypatch.setenv("AZURE_CLIENT_SECRET", "env-secret")
    monkeypatch.setenv("AZURE_TENANT_ID", "env-tenant")
    settings = AuthSettings()
    assert (settings.client_id, settings.client_secret, settings.tenant_id) == (
        "env-client",
        "env-secret",
        "env-tenant",
    )


# --- the pluggable seam ------------------------------------------------------------------------


def test_unknown_mode_raises():
    with pytest.raises(ValueError, match="unknown KX_MCP_AUTH mode"):
        build_auth_provider(AuthSettings(mode="nope"))


def test_custom_mode_is_pluggable():
    """A new mode (the shape a future proxy_headers mode would take) registers without touching
    the dispatch."""
    seen = {}

    def builder(settings):
        seen["mode"] = settings.mode
        return None

    register_auth_mode("custom_test_mode", builder)
    build_auth_provider(AuthSettings(mode="custom_test_mode"))
    assert seen["mode"] == "custom_test_mode"


# --- config binding ----------------------------------------------------------------------------


def test_mode_binds_to_bare_kx_mcp_auth_env(monkeypatch):
    """The mode is the bare KX_MCP_AUTH var; detail binds under KX_MCP_AUTH_*."""
    monkeypatch.setenv("KX_MCP_AUTH", "JWKS")  # also proves case-normalisation
    monkeypatch.setenv("KX_MCP_AUTH_JWKS_URI", "https://issuer.test/jwks")
    monkeypatch.setenv("KX_MCP_AUTH_REQUIRED_SCOPES", "kdbx.read, kdbx.write")
    settings = AuthSettings()
    assert settings.mode == "jwks"
    assert settings.jwks_uri == "https://issuer.test/jwks"
    assert settings.required_scopes == ["kdbx.read", "kdbx.write"]


def test_empty_mode_collapses_to_unset():
    """Empty string KX_MCP_AUTH normalises to 'unset' — misconfiguration is safe."""
    assert AuthSettings(mode="").mode == "unset"


def test_whitespace_mode_collapses_to_unset(monkeypatch):
    """Whitespace-only KX_MCP_AUTH (env var set but blank) normalises to 'unset'."""
    monkeypatch.setenv("KX_MCP_AUTH", "  ")
    assert AuthSettings().mode == "unset"


# --- audit scaffolding -------------------------------------------------------------------------


def test_audit_logs_subject_action_outcome(caplog):
    app = build_app(["example"], name="audit", auth_settings=AuthSettings(mode="unset"))

    async def go():
        async with Client(app) as client:
            await client.call_tool("example_echo", {"text": "x"})

    with caplog.at_level(logging.INFO, logger="kx_mcp.audit"):
        asyncio.run(go())

    lines = [r.message for r in caplog.records if r.name == "kx_mcp.audit"]
    assert any(
        "subject=anonymous" in m and "action=tool_invoke" in m
        and "target=example_echo" in m and "outcome=ok" in m
        for m in lines
    ), lines


def test_audit_logs_authenticated_subject(caplog, monkeypatch):
    """Audit emits the principal's client_id (not 'anonymous') when a token is present.

    Patches ``current_principal`` in the audit module to inject a fake principal — this tests
    the middleware's subject-reading logic in isolation. Full inbound wiring (JWT → contextvar
    → audit subject) is proven over-the-wire in ``test_auth_integration``.
    """

    class _FakePrincipal:
        client_id = "alice"

    monkeypatch.setattr(
        "kx_mcp_core.auth.audit.current_principal", lambda: _FakePrincipal()
    )
    parent = make_parent("test")

    @parent.tool()
    def echo(text: str) -> str:
        return text

    async def go():
        async with Client(parent) as c:
            await c.call_tool("echo", {"text": "hi"})

    with caplog.at_level(logging.INFO, logger="kx_mcp.audit"):
        asyncio.run(go())

    lines = [r.message for r in caplog.records if r.name == "kx_mcp.audit"]
    assert any(
        "subject=alice" in m and "action=tool_invoke" in m and "outcome=ok" in m
        for m in lines
    ), lines


def test_audit_logs_error_outcome(caplog):
    """Audit emits ``outcome=error`` when the tool raises — the error branch in _audit is covered."""
    parent = make_parent("test")

    @parent.tool()
    def bad_tool() -> str:
        raise RuntimeError("intentional failure")

    async def go():
        async with Client(parent) as c:
            try:
                await c.call_tool("bad_tool", {})
            except Exception:
                pass

    with caplog.at_level(logging.INFO, logger="kx_mcp.audit"):
        asyncio.run(go())

    lines = [r.message for r in caplog.records if r.name == "kx_mcp.audit"]
    assert any(
        "action=tool_invoke" in m and "outcome=error" in m
        for m in lines
    ), lines


def test_audit_logs_resource_read_action(caplog):
    """Audit ``on_read_resource`` fires and emits ``action=resource_read`` — not just tools."""
    parent = make_parent("test")

    @parent.resource("test://status")
    async def status_resource() -> str:
        return "ok"

    async def go():
        async with Client(parent) as c:
            await c.read_resource("test://status")

    with caplog.at_level(logging.INFO, logger="kx_mcp.audit"):
        asyncio.run(go())

    lines = [r.message for r in caplog.records if r.name == "kx_mcp.audit"]
    assert any(
        "action=resource_read" in m and "outcome=ok" in m
        for m in lines
    ), lines


def test_audit_logs_prompt_get_action(caplog):
    """Audit ``on_get_prompt`` fires and emits ``action=prompt_get`` — not just tools."""
    parent = make_parent("test")

    @parent.prompt()
    async def greet(name: str = "world") -> str:
        return f"Hello, {name}!"

    async def go():
        async with Client(parent) as c:
            await c.get_prompt("greet", {"name": "test"})

    with caplog.at_level(logging.INFO, logger="kx_mcp.audit"):
        asyncio.run(go())

    lines = [r.message for r in caplog.records if r.name == "kx_mcp.audit"]
    assert any(
        "action=prompt_get" in m and "outcome=ok" in m
        for m in lines
    ), lines


def test_make_parent_audit_false_suppresses_middleware(caplog):
    """``make_parent(audit=False)`` omits the AuditMiddleware entirely."""
    parent = make_parent("test", audit=False)

    @parent.tool()
    def echo(text: str) -> str:
        return text

    async def go():
        async with Client(parent) as c:
            await c.call_tool("echo", {"text": "hi"})

    with caplog.at_level(logging.INFO, logger="kx_mcp.audit"):
        asyncio.run(go())

    audit_records = [r for r in caplog.records if r.name == "kx_mcp.audit"]
    assert audit_records == [], f"Expected no audit records, got: {audit_records}"
