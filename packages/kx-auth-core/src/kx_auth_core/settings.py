"""Container inbound-auth configuration (the ``KX_MCP_AUTH`` family) — the shared config contract.

The *mode* lives in the bare ``KX_MCP_AUTH`` env var (``unset`` / ``static`` / ``jwks`` /
``oidc_proxy`` / ``entra``; later ``proxy_headers`` — see the verifier seam in
:mod:`kx_mcp_core.auth.providers`); the mode-specific detail lives under the ``KX_MCP_AUTH_*``
prefix. This is the container's config fragment — distinct from each extension's own prefix
(``KDBX_DB_*``, ``KDBAI_DB_*``).

This model lives in the lean ``kx-auth-core`` package (no fastmcp) so **both** the container and the
client-side ``kx auth`` CLI bind to the *same* config: the CLI validates a bearer against the same
keys/issuer/audience the container enforces. ``kx_mcp_core.auth.AuthSettings`` re-exports it.
"""

from __future__ import annotations

from typing import List, Optional

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class AuthSettings(BaseSettings):
    """Resolved inbound-auth config. Read from the environment by default; constructible by name
    in tests/glue (``AuthSettings(mode="static", public_key=...)``)."""

    model_config = SettingsConfigDict(
        env_prefix="KX_MCP_AUTH_",
        populate_by_name=True,
        extra="ignore",
    )

    # The mode selector is the bare KX_MCP_AUTH var (not KX_MCP_AUTH_MODE) — see the module docstring.
    mode: str = Field(default="unset", validation_alias="KX_MCP_AUTH")

    # static (RS256 against a local public key) — supply the PEM inline or by path.
    public_key: Optional[str] = None
    public_key_path: Optional[str] = None

    # jwks (RS256 against a remote JWKS endpoint — Keycloak / Auth0 / any OIDC issuer).
    jwks_uri: Optional[str] = None

    # The container's own public base URL, as clients reach it (e.g. https://mcp.example or
    # http://127.0.0.1:8000). When set, the jwks provider advertises RFC 9728 Protected Resource
    # Metadata (the issuer as authorization_server) so an MCP client / `kx auth login` can *discover*
    # the IdP from the server. Unset → the verifier validates only (no discovery) — correct for the
    # stdio / no-public-URL posture. Must equal the URL the client actually connects to (proxy-aware).
    resource_url: Optional[str] = None

    # Shared claim-validation knobs for static + jwks.
    issuer: Optional[str] = None
    audience: Optional[str] = None
    algorithm: str = "RS256"
    required_scopes: Optional[List[str]] = None

    # entra (FastMCP's AzureProvider — the OAuth-Proxy front door for clients that must *log in*
    # against Microsoft Entra, which has no open DCR). These name the **one app you pre-register**;
    # the proxy brokers the flow on its behalf. `base_url` is the container's public URL (reuse
    # `resource_url`). We accept both the canonical `KX_MCP_AUTH_*` names and the `AZURE_*` names
    # the Azure tooling / FastMCP docs use, so an existing Azure-app `.env` works unchanged.
    client_id: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("KX_MCP_AUTH_CLIENT_ID", "AZURE_CLIENT_ID"),
    )
    client_secret: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("KX_MCP_AUTH_CLIENT_SECRET", "AZURE_CLIENT_SECRET"),
    )
    tenant_id: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("KX_MCP_AUTH_TENANT_ID", "AZURE_TENANT_ID"),
    )
    # Application ID URI exposed by the app registration; defaults to api://{client_id} in Entra,
    # so leave unset unless you configured a custom one.
    identifier_uri: Optional[str] = None

    # oidc_proxy (FastMCP's OIDCProxy — `entra` generalised to any OIDC issuer whose DCR is
    # closed). Reuses `client_id`/`client_secret` (the one app you pre-register), `resource_url`
    # (base_url), `audience`, `algorithm` and `required_scopes`.

    # The discovery document. Derived as `{issuer}/.well-known/openid-configuration` when unset;
    # set it for issuers that serve it elsewhere (Entra v2.0, some Auth0/Okta setups).
    config_url: Optional[str] = None
    # Signs container-issued tokens and keys the encrypted OAuth-state store. Unset → both are
    # derived from `client_secret`. Required for a public/PKCE client with no secret.
    jwt_signing_key: Optional[str] = None
    # False accepts a discovery document missing an OIDC-required field (usually
    # `subject_types_supported`).
    oidc_strict: bool = True

    @field_validator("mode", mode="before")
    @classmethod
    def _normalise_mode(cls, value: object) -> str:
        """Empty / whitespace / unset all collapse to ``unset`` (the no-auth bundling posture)."""
        if isinstance(value, str) and value.strip():
            return value.strip().lower()
        return "unset"

    @field_validator("required_scopes", mode="before")
    @classmethod
    def _split_scopes(cls, value: object) -> object:
        """Accept a comma- or space-separated string from the environment (not just JSON)."""
        if isinstance(value, str):
            parts = value.replace(",", " ").split()
            return parts or None
        return value
