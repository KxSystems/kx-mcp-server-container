"""Outbound identity-propagation config + the credential it produces.

`OutboundConfig` is a backend's outbound fragment — constructed by the extension from *its own* env
prefix (`KDBAI_OUTBOUND_*`, `KXI_OUTBOUND_*`, …); the container owns the schema + the strategies, the
extension owns *which* strategy and *which* endpoint. Frozen, so it can key a per-principal connection
cache.
"""

from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, SecretStr

# RFC 8693 / OAuth constants (the wire shape `rfc_8693` sends).
GRANT_TOKEN_EXCHANGE = "urn:ietf:params:oauth:grant-type:token-exchange"
GRANT_CLIENT_CREDENTIALS = "client_credentials"
TOKEN_TYPE_ACCESS = "urn:ietf:params:oauth:token-type:access_token"


class OutboundConfig(BaseModel):
    """How a backend propagates the inbound principal outbound (frozen).

    The public contract for outbound identity propagation: a backend builds this from its own
    env-prefixed fragment and hands it to :func:`exchange`. The per-field descriptions are the
    canonical reference for what each knob does and which strategy consults it.
    """

    model_config = ConfigDict(frozen=True)

    strategy: str = Field(
        default="passthrough",
        description=(
            "Which outbound strategy to run. Resolved by name from the register_outbound_strategy "
            "registry at exchange() time (a bare str, not an enum, so custom drivers register "
            "arbitrary names — mirrors the inbound KX_MCP_AUTH mode). Built-ins: passthrough / "
            "rfc_8693 / service_account. An unregistered name raises ValueError at exchange()."
        ),
    )
    token_url: Optional[str] = Field(
        default=None,
        description=(
            "OIDC/STS token endpoint. Required by rfc_8693 and service_account; unused by passthrough."
        ),
    )
    audience: Optional[str] = Field(
        default=None,
        description=(
            "Target backend audience. rfc_8693 requests a token for it; passthrough's guard refuses "
            "unless the inbound token's aud already includes it; service_account may request it."
        ),
    )
    resource: Optional[str] = Field(
        default=None,
        description="Optional RFC 8707 resource URI — an alternative to audience for rfc_8693.",
    )
    client_id: Optional[str] = Field(
        default=None,
        description=(
            "Client identity for authenticating at the token endpoint (rfc_8693 / service_account)."
        ),
    )
    client_secret: Optional[SecretStr] = Field(
        default=None,
        description="Client secret paired with client_id. SecretStr — kept out of logs and reprs.",
    )
    client_auth: Literal["basic", "post"] = Field(
        default="post",
        description=(
            "How credentials reach the token endpoint (RFC 6749 §2.3): 'post' = in the request body "
            "(client_secret_post), 'basic' = Authorization: Basic header (client_secret_basic). "
            "Default 'post' matches the supported backend IdPs. Only "
            "consulted when client_id is set."
        ),
    )
    scopes: Optional[list[str]] = Field(
        default=None,
        description=(
            "Optional requested scopes, space-joined onto the token request "
            "(rfc_8693 / service_account)."
        ),
    )
    subject_token_type: str = Field(
        default=TOKEN_TYPE_ACCESS,
        description=(
            "RFC 8693 subject_token_type for the inbound bearer. Defaults to an OAuth access token."
        ),
    )
    verify: bool = Field(
        default=True,
        description=(
            "TLS verification for the token call exchange() makes when it builds its own client "
            "(ignored if the caller passes its own client). Set False for e.g. a self-signed dev "
            "Keycloak — without the backend importing httpx."
        ),
    )
    timeout: float = Field(
        default=30.0,
        description=(
            "Request timeout (seconds) for that same token call; ignored when the caller passes a "
            "client."
        ),
    )


class OutboundCredential(BaseModel):
    """The product of an exchange: the credential to present to the backend, plus audit metadata."""

    access_token: str = Field(
        description=(
            "The credential to present to the backend (forwarded as-is by passthrough; minted by "
            "rfc_8693 / service_account)."
        ),
    )
    token_type: str = Field(
        default="Bearer",
        description=(
            "OAuth token type from the token endpoint; 'Bearer' for passthrough and the usual "
            "default otherwise."
        ),
    )
    expires_in: Optional[int] = Field(
        default=None,
        description=(
            "Lifetime in seconds as reported by the token endpoint; None for passthrough (the "
            "forwarded token carries its own expiry)."
        ),
    )
    strategy: str = Field(
        default="",
        description="The strategy that produced this credential — recorded in the audit chain.",
    )
    claims: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Best-effort, *unverified* decode of access_token (sub / aud / jti / scope), for the "
            "audit chain ONLY — never an authorization input. {} for opaque (non-JWT) tokens."
        ),
    )
