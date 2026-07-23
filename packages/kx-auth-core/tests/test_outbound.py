"""Outbound token-exchange seam: passthrough / rfc_8693 / service_account + the audit chain.

`rfc_8693` and `service_account` are exercised against a **mock STS** — a dependency-free in-process
token endpoint that captures what the seam sent and returns a minted JWT, so we assert both the
request wire shape (grant type, subject token, audience, client auth) and that the product token is
injected. `passthrough` and claim decoding are pure-local. No live IdP, no Docker.
"""

from __future__ import annotations

import asyncio
import logging
import time

import jwt
import pytest

from kx_auth_core import (
    OutboundConfig,
    decode_claims_unverified,
    exchange,
    outbound_strategies,
    register_outbound_strategy,
)
from kx_auth_core.outbound.config import (
    GRANT_CLIENT_CREDENTIALS,
    GRANT_TOKEN_EXCHANGE,
)


_HS256_KEY = "test-secret-key-at-least-32-bytes-long!"  # length only matters to silence a warning


def _jwt(**claims) -> str:
    """A signed-but-irrelevant JWT (the seam never verifies outbound tokens; we only read claims)."""
    return jwt.encode(claims, _HS256_KEY, algorithm="HS256")


# --- passthrough ------------------------------------------------------------------------------


def test_passthrough_forwards_when_audience_matches():
    subject = _jwt(sub="alice", aud=["mcp", "kdbai"])
    config = OutboundConfig(strategy="passthrough", audience="kdbai")
    cred = asyncio.run(exchange(config, subject))
    assert cred.access_token == subject  # forwarded unchanged
    assert cred.strategy == "passthrough"
    assert cred.claims["sub"] == "alice"


def test_passthrough_refuses_on_audience_mismatch():
    subject = _jwt(sub="alice", aud="mcp")
    config = OutboundConfig(strategy="passthrough", audience="kdbai")
    with pytest.raises(PermissionError, match="passthrough refused"):
        asyncio.run(exchange(config, subject))


def test_passthrough_requires_subject_token():
    with pytest.raises(ValueError, match="requires an inbound subject token"):
        asyncio.run(exchange(OutboundConfig(strategy="passthrough"), None))


def test_passthrough_refuses_when_no_audience_configured():
    """No audience = no way to verify the token is for this backend → refuse (confused-deputy guard)."""
    subject = _jwt(sub="alice", aud="mcp")
    with pytest.raises(ValueError, match="requires `audience`"):
        asyncio.run(exchange(OutboundConfig(strategy="passthrough"), subject))


# --- rfc_8693 ---------------------------------------------------------------------------------


def test_rfc_8693_exchanges_and_injects_product_token(mock_sts):
    token_url, captured = mock_sts
    subject = _jwt(sub="alice", aud="mcp")
    config = OutboundConfig(
        strategy="rfc_8693", token_url=token_url, audience="kdbai",
        client_id="mcp-container", client_secret="s3cret", scopes=["kdbx.read"],
    )
    cred = asyncio.run(exchange(config, subject))

    # the product token is injected, scoped to the backend audience
    assert decode_claims_unverified(cred.access_token)["aud"] == "kdbai"
    assert cred.strategy == "rfc_8693"
    assert cred.expires_in == 300

    # the seam sent the RFC 8693 wire shape, with client auth
    sent = captured[0]
    assert sent["grant_type"] == GRANT_TOKEN_EXCHANGE
    assert sent["subject_token"] == subject
    assert sent["audience"] == "kdbai"
    assert sent["scope"] == "kdbx.read"
    # default client_auth="post" → credentials in the body, not an Authorization header
    assert sent["client_id"] == "mcp-container"
    assert sent["client_secret"] == "s3cret"
    assert not sent["_authorization"]


def test_rfc_8693_basic_client_auth_uses_authorization_header(mock_sts):
    token_url, captured = mock_sts
    subject = _jwt(sub="alice", aud="mcp")
    config = OutboundConfig(
        strategy="rfc_8693", token_url=token_url, audience="kdbai",
        client_id="mcp-container", client_secret="s3cret", client_auth="basic",
    )
    asyncio.run(exchange(config, subject))
    sent = captured[0]
    assert sent["_authorization"].startswith("Basic ")  # creds in the header
    assert "client_secret" not in sent                  # ...not the body


def test_rfc_8693_requires_subject_token_and_url():
    with pytest.raises(ValueError, match="requires an inbound subject token"):
        asyncio.run(exchange(OutboundConfig(strategy="rfc_8693", token_url="http://x"), None))
    with pytest.raises(ValueError, match="requires token_url"):
        asyncio.run(exchange(OutboundConfig(strategy="rfc_8693"), _jwt(sub="a")))


# --- service_account --------------------------------------------------------------------------


def test_service_account_uses_client_credentials(mock_sts):
    token_url, captured = mock_sts
    config = OutboundConfig(
        strategy="service_account", token_url=token_url, audience="backend-api",
        client_id="mcp-container", client_secret="s3cret",
    )
    cred = asyncio.run(exchange(config, subject_token=None))  # workload identity — no caller token
    assert cred.strategy == "service_account"
    assert cred.access_token
    assert captured[0]["grant_type"] == GRANT_CLIENT_CREDENTIALS
    assert "subject_token" not in captured[0]


# --- the pluggable seam -----------------------------------------------------------------------


def test_unknown_strategy_raises():
    with pytest.raises(ValueError, match="unknown outbound strategy"):
        asyncio.run(exchange(OutboundConfig(strategy="nope")))


def test_custom_strategy_is_pluggable():
    from kx_auth_core import OutboundCredential

    async def entra_obo(config, subject_token, *, client):
        return OutboundCredential(access_token="obo-token", strategy="custom_entra")

    register_outbound_strategy("custom_entra", entra_obo)
    assert "custom_entra" in outbound_strategies()
    cred = asyncio.run(exchange(OutboundConfig(strategy="custom_entra"), "anything"))
    assert cred.access_token == "obo-token"


# --- audit chain ------------------------------------------------------------------------------


def test_audit_logs_inbound_exchange_outbound_chain(mock_sts, caplog):
    token_url, _ = mock_sts
    config = OutboundConfig(strategy="rfc_8693", token_url=token_url, audience="kdbai")
    subject = _jwt(sub="alice", aud="mcp")
    with caplog.at_level(logging.INFO, logger="kx_mcp.audit"):
        asyncio.run(exchange(config, subject))
    lines = [r.message for r in caplog.records if r.name == "kx_mcp.audit"]
    assert any(
        "strategy=rfc_8693" in m and "subject=alice" in m and "audience=kdbai" in m
        and "jti=exch-jti-1" in m and "scopes=kdbx.read" in m and "outcome=ok" in m
        for m in lines
    ), lines


def test_audit_logs_exchange_failure(caplog):
    config = OutboundConfig(strategy="passthrough", audience="kdbai")
    subject = _jwt(sub="alice", aud="wrong")
    with caplog.at_level(logging.INFO, logger="kx_mcp.audit"):
        with pytest.raises(PermissionError):
            asyncio.run(exchange(config, subject))
    lines = [r.message for r in caplog.records if r.name == "kx_mcp.audit"]
    assert any("outcome=error" in m and "strategy=passthrough" in m and "subject=alice" in m
               for m in lines), lines


# --- claim decoding ---------------------------------------------------------------------------


def test_decode_claims_unverified():
    token = _jwt(sub="bob", aud="kdbai", iat=int(time.time()))
    assert decode_claims_unverified(token)["sub"] == "bob"
    assert decode_claims_unverified("opaque-token") == {}
    assert decode_claims_unverified(None) == {}
