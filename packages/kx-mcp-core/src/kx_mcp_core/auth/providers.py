"""The pluggable inbound-auth verifier seam.

``build_auth_provider(settings)`` turns a resolved :class:`AuthSettings` into a FastMCP
``AuthProvider`` (or ``None`` for the no-auth / single-principal posture). Modes are registered in a
small registry rather than hard-coded into a conditional, so a new mode — e.g. a future
``proxy_headers`` — slots in with ``register_auth_mode("proxy_headers", builder)`` without touching
``make_parent`` or this dispatch.

FastMCP 3.x ships the verifiers themselves (``JWTVerifier`` does RS256 against a local public key or
a remote JWKS), so the container only owns mode selection + config, not crypto.

A bare ``JWTVerifier`` *validates* bearers but advertises **nothing** for client-driven discovery —
it is a ``TokenVerifier``, which serves no routes, so ``/.well-known/oauth-protected-resource`` 404s
and the ``401`` carries no ``resource_metadata`` pointer. To advertise the authorization server (RFC
9728 Protected Resource Metadata), the verifier must be wrapped in FastMCP's ``RemoteAuthProvider``.
``_build_jwks`` does that whenever ``KX_MCP_AUTH_RESOURCE_URL`` (the container's public URL) is set;
unset keeps the bare verifier (no discovery), which is correct for the stdio / no-public-URL posture.
"""

from __future__ import annotations

import logging
from typing import Callable, Optional

from fastmcp.server.auth import AuthProvider, RemoteAuthProvider
from fastmcp.server.auth.providers.jwt import JWTVerifier
from pydantic import AnyHttpUrl
from kx_auth_core import AuthSettings, resolve_public_key

from .audit import audit_authentication_denials

logger = logging.getLogger(__name__)

# A builder turns resolved settings into a provider (or None for "no inbound auth").
AuthProviderBuilder = Callable[[AuthSettings], Optional[AuthProvider]]

_BUILDERS: dict[str, AuthProviderBuilder] = {}


def register_auth_mode(mode: str, builder: AuthProviderBuilder) -> None:
    """Register the builder for a ``KX_MCP_AUTH`` mode. The extension point for new modes."""
    _BUILDERS[mode] = builder


def auth_modes() -> list[str]:
    """The modes currently registered (for error messages / introspection)."""
    return sorted(_BUILDERS)


def build_auth_provider(settings: Optional[AuthSettings] = None) -> Optional[AuthProvider]:
    """Resolve ``KX_MCP_AUTH`` to a FastMCP ``AuthProvider`` (or ``None`` when auth is off).

    Reads :class:`AuthSettings` from the environment when none is passed. Raises ``ValueError`` on an
    unknown mode or a mode missing its required config — a clear operator error, not a stack trace
    deep in the request path.
    """
    settings = settings or AuthSettings()
    try:
        builder = _BUILDERS[settings.mode]
    except KeyError:
        raise ValueError(
            f"unknown KX_MCP_AUTH mode {settings.mode!r}; known modes: {auth_modes()}"
        ) from None

    provider = builder(settings)
    if provider is None:
        logger.info("inbound auth disabled (KX_MCP_AUTH=unset) — single-principal posture")
    else:
        logger.info(
            "inbound auth enabled: mode=%s issuer=%s audience=%s",
            settings.mode,
            settings.issuer,
            settings.audience,
        )
        # Every rejected bearer becomes an audit line — the one access decision the dispatch
        # middleware can never see, because a 401 ends the request before dispatch.
        audit_authentication_denials(provider, settings)
    return provider


# --- built-in mode builders ---------------------------------------------------------------------


def _build_unset(settings: AuthSettings) -> None:
    """No inbound auth — the bundling / single-principal posture."""
    return None


def _build_static(settings: AuthSettings) -> AuthProvider:
    """RS256 verified against a local public key — offline dev / no IdP reachable."""
    return JWTVerifier(
        public_key=resolve_public_key(settings),
        issuer=settings.issuer,
        audience=settings.audience,
        algorithm=settings.algorithm,
        required_scopes=settings.required_scopes,
    )


def _build_jwks(settings: AuthSettings) -> AuthProvider:
    """RS256 verified against a remote JWKS endpoint — the production direction (Keycloak/OIDC).

    When ``KX_MCP_AUTH_RESOURCE_URL`` is set, the verifier is wrapped in a ``RemoteAuthProvider`` so
    the container advertises RFC 9728 Protected Resource Metadata (the issuer as authorization
    server) — letting an MCP client / ``kx auth login`` *discover* the IdP from the server. Unset →
    a bare verifier that validates only (no discovery), the stdio / no-public-URL posture.
    """
    if not settings.jwks_uri:
        raise ValueError("KX_MCP_AUTH=jwks requires KX_MCP_AUTH_JWKS_URI (the issuer's JWKS endpoint)")
    verifier = JWTVerifier(
        jwks_uri=settings.jwks_uri,
        issuer=settings.issuer,
        audience=settings.audience,
        algorithm=settings.algorithm,
        required_scopes=settings.required_scopes,
    )
    if not settings.resource_url:
        return verifier
    if not settings.issuer:
        raise ValueError(
            "KX_MCP_AUTH_RESOURCE_URL requires KX_MCP_AUTH_ISSUER (the authorization server advertised "
            "for discovery)"
        )
    logger.info(
        "inbound auth advertising discovery: resource=%s authorization_server=%s",
        settings.resource_url,
        settings.issuer,
    )
    return RemoteAuthProvider(
        token_verifier=verifier,
        authorization_servers=[AnyHttpUrl(settings.issuer)],
        base_url=settings.resource_url,
    )


def _build_oidc_proxy(settings: AuthSettings) -> AuthProvider:
    """Any OIDC issuer via FastMCP's ``OIDCProxy`` — the provider-agnostic OAuth-Proxy front door.

    Use this (not ``jwks``) when the container must **log a client in** against an issuer whose
    Dynamic Client Registration is closed: ``jwks`` advertises the issuer and expects the client to
    register there itself, which a locked-down Keycloak/Auth0/Okta realm refuses. ``OIDCProxy``
    presents a DCR facade to the client and brokers with the issuer through **one pre-registered
    app**, reading the upstream endpoints from its discovery document. ``entra`` is this mode pinned
    to Microsoft. Validation underneath is the same ``JWTVerifier``, so everything downstream of
    ``current_principal`` is unchanged; when a client already *holds* a token, prefer ``jwks``.

    Two deployment consequences: discovery is fetched in the constructor (an unreachable issuer is
    a startup failure, not a request-time one), and the proxy keeps durable state — an encrypted
    on-disk store keyed off ``client_secret``, so restart- and worker-safe unconfigured. Detail:
    ``docs/auth.md``.
    """
    from fastmcp.server.auth.oidc_proxy import OIDCProxy

    config_url = settings.config_url
    if not config_url and settings.issuer:
        config_url = f"{settings.issuer.rstrip('/')}/.well-known/openid-configuration"

    missing = [
        name
        for name, value in (
            ("KX_MCP_AUTH_ISSUER or KX_MCP_AUTH_CONFIG_URL (the OIDC discovery document)", config_url),
            ("KX_MCP_AUTH_CLIENT_ID (the pre-registered app)", settings.client_id),
            ("KX_MCP_AUTH_RESOURCE_URL (the container's public base_url)", settings.resource_url),
        )
        if not value
    ]
    if missing:
        raise ValueError(f"KX_MCP_AUTH=oidc_proxy requires: {', '.join(missing)}")
    if not settings.client_secret and not settings.jwt_signing_key:
        raise ValueError(
            "KX_MCP_AUTH=oidc_proxy requires KX_MCP_AUTH_CLIENT_SECRET, or "
            "KX_MCP_AUTH_JWT_SIGNING_KEY for a public/PKCE client with no secret to derive from"
        )
    if not settings.audience:
        logger.warning(
            "KX_MCP_AUTH=oidc_proxy without KX_MCP_AUTH_AUDIENCE — the token 'aud' claim will NOT "
            "be validated. Set it (and a matching audience mapper at the issuer) to restrict tokens "
            "to this container."
        )

    logger.info(
        "inbound auth via OIDC proxy: config_url=%s client_id=%s base_url=%s scopes=%s",
        config_url,
        settings.client_id,
        settings.resource_url,
        settings.required_scopes,
    )
    # The `missing` check above guarantees these are set; assert narrows str | None -> str.
    assert config_url and settings.client_id and settings.resource_url
    return OIDCProxy(
        config_url=config_url,
        strict=settings.oidc_strict,
        client_id=settings.client_id,
        client_secret=settings.client_secret,
        audience=settings.audience,
        algorithm=settings.algorithm,
        required_scopes=settings.required_scopes,
        base_url=settings.resource_url,
        jwt_signing_key=settings.jwt_signing_key,
    )


def _build_entra(settings: AuthSettings) -> AuthProvider:
    """Microsoft Entra via FastMCP's ``AzureProvider`` — the OAuth-Proxy front door.

    Use this (not ``jwks``) when the container itself must **log a client in** against Entra: an MCP
    client like Claude Code drives the full discover → register → authorize flow, but Entra has no
    open Dynamic Client Registration, so a bare ``RemoteAuthProvider`` (what ``jwks`` advertises)
    can't complete it. ``AzureProvider`` is an ``OAuthProxy`` that presents a DCR-compatible facade
    to the client while brokering with Entra through **one pre-registered app** (client id + secret +
    tenant). It validates inbound tokens with the same ``JWTVerifier`` underneath, so everything
    downstream of ``current_principal`` (the ferry, q ``promote``, outbound) is unchanged.

    When a client already *holds* an Entra token, prefer ``jwks`` (validate-only) — no app/secret
    needed. ``AzureProvider`` is imported lazily so its optional deps load only for this mode.
    """
    from fastmcp.server.auth.providers.azure import AzureProvider

    missing = [
        name
        for name, value in (
            ("KX_MCP_AUTH_CLIENT_ID / AZURE_CLIENT_ID", settings.client_id),
            ("KX_MCP_AUTH_CLIENT_SECRET / AZURE_CLIENT_SECRET", settings.client_secret),
            ("KX_MCP_AUTH_TENANT_ID / AZURE_TENANT_ID", settings.tenant_id),
            ("KX_MCP_AUTH_RESOURCE_URL (the container's public base_url)", settings.resource_url),
        )
        if not value
    ]
    if missing:
        raise ValueError(f"KX_MCP_AUTH=entra requires: {', '.join(missing)}")
    if not settings.required_scopes:
        raise ValueError(
            "KX_MCP_AUTH=entra requires KX_MCP_AUTH_REQUIRED_SCOPES (the app's exposed scope "
            "name(s), unprefixed — e.g. 'access')"
        )

    logger.info(
        "inbound auth via Entra OAuth proxy: tenant=%s client_id=%s base_url=%s scopes=%s",
        settings.tenant_id,
        settings.client_id,
        settings.resource_url,
        settings.required_scopes,
    )
    # The `missing` check above guarantees these are set in entra mode; assert narrows str | None → str.
    assert settings.client_id and settings.tenant_id and settings.resource_url
    return AzureProvider(
        client_id=settings.client_id,
        client_secret=settings.client_secret,
        tenant_id=settings.tenant_id,
        required_scopes=settings.required_scopes,
        base_url=settings.resource_url,
        identifier_uri=settings.identifier_uri,
    )


register_auth_mode("unset", _build_unset)
register_auth_mode("static", _build_static)
register_auth_mode("jwks", _build_jwks)
register_auth_mode("oidc_proxy", _build_oidc_proxy)
register_auth_mode("entra", _build_entra)
