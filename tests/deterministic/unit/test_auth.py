"""Inbound-auth verifier seam: unset / static / jwks / oidc_proxy, plus the audit scaffolding.

Covers:
- Provider building: unset (None), static (local public key), jwks (remote JWKS endpoint),
  oidc_proxy (OAuth-Proxy front door for any OIDC issuer).
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


def test_entra_canonical_env_wins_over_azure_alias(monkeypatch):
    """Both names exported: the canonical KX_MCP_AUTH_* value wins (AliasChoices is first-match-wins
    and KX_MCP_AUTH_CLIENT_ID is listed first). Companion to test_entra_reads_azure_env_aliases,
    which only covers the alias-ONLY case. Pinned so a future reorder of the AliasChoices tuple
    can't silently flip which app registration the OAuth proxy brokers for."""
    monkeypatch.setenv("KX_MCP_AUTH", "entra")
    monkeypatch.setenv("KX_MCP_AUTH_CLIENT_ID", "canonical-client")
    monkeypatch.setenv("AZURE_CLIENT_ID", "azure-client")
    monkeypatch.setenv("KX_MCP_AUTH_CLIENT_SECRET", "canonical-secret")
    monkeypatch.setenv("AZURE_CLIENT_SECRET", "azure-secret")
    monkeypatch.setenv("KX_MCP_AUTH_TENANT_ID", "canonical-tenant")
    monkeypatch.setenv("AZURE_TENANT_ID", "azure-tenant")

    settings = AuthSettings()

    assert (settings.client_id, settings.client_secret, settings.tenant_id) == (
        "canonical-client",
        "canonical-secret",
        "canonical-tenant",
    )


# --- oidc_proxy (OIDCProxy — the provider-agnostic OAuth-Proxy front door) ----------------------
# The proxy is FastMCP's; what we own is the config mapping and the fail-fast, so that is what
# these assert.

_OIDC = dict(
    mode="oidc_proxy",
    issuer="https://idp.test/realms/quants",
    client_id="app-client-id",
    client_secret="app-secret",
    resource_url="http://localhost:8000",
    audience="kx-mcp-aud",
)


@pytest.fixture
def fake_oidc_proxy(monkeypatch):
    """Capture the builder's kwargs. The real class fetches discovery in its constructor, so it
    can't be built without a live issuer."""
    import fastmcp.server.auth.oidc_proxy as oidc_mod

    captured = {}

    class FakeOIDCProxy:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(oidc_mod, "OIDCProxy", FakeOIDCProxy)
    return captured


def test_oidc_proxy_builds_proxy_with_app_config(fake_oidc_proxy):
    """Happy path: the pre-registered app config + claim knobs reach OIDCProxy unchanged."""
    provider = build_auth_provider(AuthSettings(**_OIDC, required_scopes=["openid"]))

    assert provider is not None
    assert fake_oidc_proxy == {
        "config_url": "https://idp.test/realms/quants/.well-known/openid-configuration",
        "strict": True,
        "client_id": "app-client-id",
        "client_secret": "app-secret",
        "audience": "kx-mcp-aud",
        "algorithm": "RS256",
        "required_scopes": ["openid"],
        "base_url": "http://localhost:8000",
        "jwt_signing_key": None,
    }


def test_oidc_proxy_derives_config_url_from_issuer(fake_oidc_proxy):
    """Discovery precedence, step 1: the standard well-known path is derived from the issuer."""
    build_auth_provider(AuthSettings(**{**_OIDC, "issuer": "https://idp.test/realms/quants/"}))
    # The trailing slash on the issuer must not produce a double slash in the derived URL.
    assert (
        fake_oidc_proxy["config_url"]
        == "https://idp.test/realms/quants/.well-known/openid-configuration"
    )


def test_oidc_proxy_explicit_config_url_wins_over_issuer(fake_oidc_proxy):
    """Discovery precedence, step 2: an explicit config URL wins — for issuers not serving
    metadata at the standard path (Entra v2.0, some Auth0/Okta setups)."""
    build_auth_provider(
        AuthSettings(**_OIDC, config_url="https://idp.test/other/.well-known/openid-configuration")
    )
    assert fake_oidc_proxy["config_url"] == "https://idp.test/other/.well-known/openid-configuration"


def test_oidc_proxy_config_url_alone_is_enough(fake_oidc_proxy):
    """The issuer is only a means of deriving the discovery URL — naming it directly suffices."""
    cfg = {k: v for k, v in _OIDC.items() if k != "issuer"}
    build_auth_provider(AuthSettings(**cfg, config_url="https://idp.test/.well-known/openid-configuration"))
    assert fake_oidc_proxy["config_url"] == "https://idp.test/.well-known/openid-configuration"


@pytest.mark.parametrize("drop", ["issuer", "client_id", "resource_url"])
def test_oidc_proxy_missing_config_raises(drop):
    """Each missing required field is a clear operator error, not a stack trace at request time."""
    cfg = {k: v for k, v in _OIDC.items() if k != drop}
    with pytest.raises(ValueError, match="KX_MCP_AUTH=oidc_proxy requires"):
        build_auth_provider(AuthSettings(**cfg))


def test_oidc_proxy_public_client_requires_a_signing_key():
    """A public/PKCE client may omit the secret, but must then supply the signing key — FastMCP
    otherwise has nothing to derive the token and store keys from."""
    cfg = {k: v for k, v in _OIDC.items() if k != "client_secret"}
    with pytest.raises(ValueError, match="KX_MCP_AUTH_JWT_SIGNING_KEY"):
        build_auth_provider(AuthSettings(**cfg))


def test_oidc_proxy_public_client_with_signing_key_builds(fake_oidc_proxy):
    cfg = {k: v for k, v in _OIDC.items() if k != "client_secret"}
    build_auth_provider(AuthSettings(**cfg, jwt_signing_key="a-stable-signing-key"))
    assert fake_oidc_proxy["client_secret"] is None
    assert fake_oidc_proxy["jwt_signing_key"] == "a-stable-signing-key"


def test_oidc_proxy_without_audience_warns_but_builds(fake_oidc_proxy, caplog):
    """An unvalidated 'aud' accepts any token the issuer minted, but the flow works without it —
    so warn rather than refuse to start."""
    cfg = {k: v for k, v in _OIDC.items() if k != "audience"}
    with caplog.at_level(logging.WARNING, logger="kx_mcp_core.auth.providers"):
        build_auth_provider(AuthSettings(**cfg))
    assert "aud" in caplog.text
    assert fake_oidc_proxy["audience"] is None


def test_oidc_proxy_strict_discovery_is_opt_out(fake_oidc_proxy):
    """Lenient discovery documents (a missing subject_types_supported, say) need an escape hatch."""
    build_auth_provider(AuthSettings(**_OIDC, oidc_strict=False))
    assert fake_oidc_proxy["strict"] is False


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
