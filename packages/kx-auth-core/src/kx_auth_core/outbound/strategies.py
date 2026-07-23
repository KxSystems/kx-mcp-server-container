"""The pluggable outbound token-exchange seam.

`exchange(config, subject_token)` resolves `config.strategy` from a registry and runs it, returning
an `OutboundCredential` for the backend. A registry (not a hard-coded conditional) so a `custom`
driver — e.g. Microsoft Entra OBO — slots in via `register_outbound_strategy` without touching
dispatch. Mirrors the inbound verifier seam in `auth/providers.py`. Built-in strategies: `passthrough`
(forward the inbound bearer when its audience matches), `rfc_8693` (RFC 8693 token exchange), and
`service_account` (`client_credentials` — the container's own workload identity).

Each exchange emits one `kx_mcp.audit` line so the inbound→exchange→outbound chain is traceable.
"""

from __future__ import annotations

import logging
from typing import Optional, Protocol

import httpx

from .claims import decode_claims_unverified
from .config import (
    GRANT_CLIENT_CREDENTIALS,
    GRANT_TOKEN_EXCHANGE,
    OutboundConfig,
    OutboundCredential,
)

logger = logging.getLogger("kx_mcp.audit")


class OutboundStrategy(Protocol):
    """A custom outbound driver. Registered via ``register_outbound_strategy`` and dispatched by
    ``exchange``. Receives the backend's ``OutboundConfig`` and the inbound bearer (``None`` for
    workload strategies like ``service_account``), plus a keyword-only pooled ``httpx.AsyncClient``
    to reuse. Returns the credential to present to the backend. Raise ``ValueError`` on missing
    config, ``PermissionError`` to refuse (e.g. an audience guard).
    """

    async def __call__(
        self, config: OutboundConfig, subject_token: Optional[str], *, client: httpx.AsyncClient
    ) -> OutboundCredential: ...


_STRATEGIES: dict[str, OutboundStrategy] = {}


def register_outbound_strategy(name: str, strategy: OutboundStrategy) -> None:
    """Register an outbound strategy. The extension point for `custom` drivers."""
    _STRATEGIES[name] = strategy


def outbound_strategies() -> list[str]:
    """The names of all registered strategies (built-ins + custom), sorted. Mirrors ``auth_modes()``."""
    return sorted(_STRATEGIES)


async def exchange(
    config: OutboundConfig,
    subject_token: Optional[str] = None,
    *,
    client: Optional[httpx.AsyncClient] = None,
) -> OutboundCredential:
    """Run the configured outbound strategy and return the backend credential.

    Pass an `httpx.AsyncClient` to reuse a pooled client; otherwise one is created and closed per
    call. Raises `ValueError` on an unknown strategy or missing required config; `PermissionError`
    when `passthrough`'s audience guard refuses.

    Emits one `kx_mcp.audit` record per call (the inbound→exchange→outbound chain), on success and
    failure alike.
    """
    try:
        strategy = _STRATEGIES[config.strategy]
    except KeyError:
        raise ValueError(
            f"unknown outbound strategy {config.strategy!r}; known: {outbound_strategies()}"
        ) from None

    owns_client = client is None
    client = client or httpx.AsyncClient(verify=config.verify, timeout=config.timeout)
    try:
        credential = await strategy(config, subject_token, client=client)
    except Exception as exc:
        _audit(config, subject_token, credential=None, error=exc)
        raise
    finally:
        if owns_client:
            await client.aclose()

    _audit(config, subject_token, credential=credential, error=None)
    return credential


# --- built-in strategies -----------------------------------------------------------------------


async def _passthrough(
    config: OutboundConfig, subject_token: Optional[str], *, client: httpx.AsyncClient
) -> OutboundCredential:
    """Forward the inbound bearer unchanged — only when its audience matches the configured backend.

    ``audience`` is **required**: forwarding a bearer without checking it was minted for this backend
    is a confused-deputy risk, so an unset audience is *refused* rather than blindly forwarded.
    """
    if not subject_token:
        raise ValueError("passthrough requires an inbound subject token (none present)")
    if not config.audience:
        raise ValueError(
            "passthrough requires `audience` — refusing to forward a bearer without verifying it "
            "was minted for this backend (confused-deputy guard)"
        )
    claims = decode_claims_unverified(subject_token)
    if not _audience_includes(claims.get("aud"), config.audience):
        raise PermissionError(
            f"passthrough refused: inbound audience {claims.get('aud')!r} "
            f"does not include {config.audience!r}"
        )
    return OutboundCredential(access_token=subject_token, strategy="passthrough", claims=claims)


async def _rfc_8693(
    config: OutboundConfig, subject_token: Optional[str], *, client: httpx.AsyncClient
) -> OutboundCredential:
    """RFC 8693 token exchange: swap the inbound bearer for a backend-audience token."""
    if not subject_token:
        raise ValueError("rfc_8693 requires an inbound subject token (none present)")
    if not config.token_url:
        raise ValueError("rfc_8693 requires token_url")
    data = {
        "grant_type": GRANT_TOKEN_EXCHANGE,
        "subject_token": subject_token,
        "subject_token_type": config.subject_token_type,
    }
    if config.audience:
        data["audience"] = config.audience
    if config.resource:
        data["resource"] = config.resource
    if config.scopes:
        data["scope"] = " ".join(config.scopes)
    return await _post_for_token(config, data, client, strategy="rfc_8693")


async def _service_account(
    config: OutboundConfig, subject_token: Optional[str], *, client: httpx.AsyncClient
) -> OutboundCredential:
    """OAuth client_credentials — the container's own workload identity, not the caller's."""
    if not config.token_url:
        raise ValueError("service_account requires token_url")
    data = {"grant_type": GRANT_CLIENT_CREDENTIALS}
    if config.audience:
        data["audience"] = config.audience
    if config.scopes:
        data["scope"] = " ".join(config.scopes)
    return await _post_for_token(config, data, client, strategy="service_account")


async def _post_for_token(
    config: OutboundConfig, data: dict[str, str], client: httpx.AsyncClient, *, strategy: str
) -> OutboundCredential:
    url = config.token_url
    assert url is not None  # callers guard this; narrows Optional[str] for the type checker
    headers = {"Accept": "application/json"}
    body = dict(data)  # copy — don't mutate the caller's dict when adding post-body creds
    auth: Optional[tuple[str, str]] = None
    if config.client_id:
        secret = config.client_secret.get_secret_value() if config.client_secret else ""
        if config.client_auth == "basic":
            auth = (config.client_id, secret)  # client_secret_basic — Authorization header
        else:
            body["client_id"] = config.client_id  # client_secret_post — credentials in the body
            body["client_secret"] = secret
    if auth is not None:
        response = await client.post(url, data=body, headers=headers, auth=auth)
    else:
        response = await client.post(url, data=body, headers=headers)
    response.raise_for_status()
    body = response.json()
    token = body.get("access_token")
    if not token:
        raise ValueError(f"{strategy}: token endpoint returned no access_token")
    return OutboundCredential(
        access_token=token,
        token_type=body.get("token_type", "Bearer"),
        expires_in=body.get("expires_in"),
        strategy=strategy,
        claims=decode_claims_unverified(token),
    )


def _audience_includes(aud: object, wanted: str) -> bool:
    if aud is None:
        return False
    audiences = aud if isinstance(aud, list) else [aud]
    return wanted in audiences


def _audit(
    config: OutboundConfig,
    subject_token: Optional[str],
    *,
    credential: Optional[OutboundCredential],
    error: Optional[Exception],
) -> None:
    subject = decode_claims_unverified(subject_token).get("sub") or "anonymous"
    if error is not None:
        logger.info(
            "audit exchange strategy=%s subject=%s -> audience=%s outcome=error error=%s",
            config.strategy, subject, config.audience, type(error).__name__,
        )
        return
    assert credential is not None
    claims = credential.claims
    logger.info(
        "audit exchange strategy=%s subject=%s -> audience=%s jti=%s scopes=%s outcome=ok",
        credential.strategy or config.strategy,
        subject,
        config.audience or claims.get("aud"),
        claims.get("jti"),
        claims.get("scope"),
    )


register_outbound_strategy("passthrough", _passthrough)
register_outbound_strategy("rfc_8693", _rfc_8693)
register_outbound_strategy("service_account", _service_account)
