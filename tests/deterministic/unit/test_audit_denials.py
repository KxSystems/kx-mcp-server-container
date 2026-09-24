"""A rejected bearer is an access decision, so it gets an audit line like every other.

Until now the container's regressions asserted only ``verify_token(...) is None`` — nothing asserted
that anyone was *told*. ``audit_authentication_denials`` (installed by ``build_auth_provider`` on
every mode) turns each rejection into one line on ``kx_mcp.audit``::

    audit subject=anonymous action=authenticate target=<mode> outcome=denied error=invalid_token
          reason=<why> claimed_iss=… claimed_sub=… claimed_azp=…

Raised by the first external bundle (OneTick): failed attempts are what an investigation needs, and
their log file already receives this logger, so nothing changes on their side.
"""

from __future__ import annotations

import asyncio
import logging

import pytest
from fastmcp.server.auth.providers.jwt import JWTVerifier

from kx_mcp_core.auth import AuthSettings, build_auth_provider
from kx_mcp_core.auth.audit import audit_authentication_denials

ISSUER = "https://issuer.test"
AUDIENCE = "kx-mcp"


def _static(pub: str, **over):
    return build_auth_provider(
        AuthSettings(mode="static", public_key=pub, issuer=ISSUER, audience=AUDIENCE, **over)
    )


def _verify(provider, token):
    return asyncio.run(provider.verify_token(token))


def _denials(caplog) -> list[dict[str, str]]:
    """Every ``action=authenticate`` audit line, parsed to a field dict."""
    parsed = []
    for record in caplog.records:
        if record.name != "kx_mcp.audit" or "action=authenticate" not in record.message:
            continue
        fields: dict[str, str] = {}
        for token in record.message[len("audit ") :].split():
            key, _, value = token.partition("=")
            fields[key] = value
        parsed.append(fields)
    return parsed


@pytest.fixture(autouse=True)
def _capture_audit(caplog):
    with caplog.at_level(logging.INFO, logger="kx_mcp.audit"):
        yield


# --- what is and is not audited -------------------------------------------------------------------


def test_valid_bearer_emits_no_authenticate_line(keypair, mint, caplog):
    _, pub = keypair
    assert _verify(_static(pub), mint()) is not None
    assert _denials(caplog) == []


def test_rejected_bearer_emits_one_denied_line_with_the_full_shape(keypair, mint, caplog):
    _, pub = keypair
    token = mint(exp_delta=-10, client_id="svc-1")

    assert _verify(_static(pub), token) is None

    lines = _denials(caplog)
    assert len(lines) == 1, lines
    fields = lines[0]
    assert fields["subject"] == "anonymous"  # nothing was proven, so nobody is authenticated
    assert fields["action"] == "authenticate"
    assert fields["target"] == "static"
    assert fields["outcome"] == "denied"
    assert fields["error"] == "invalid_token"
    assert fields["reason"] == "expired"
    # what the token *claimed*, marked as such — the forensic trail for "who kept trying"
    assert fields["claimed_iss"] == ISSUER
    assert fields["claimed_sub"] == "svc-1"
    assert fields["claimed_azp"] == "svc-1"


def test_the_bearer_itself_is_never_logged(keypair, mint, caplog):
    _, pub = keypair
    token = mint(exp_delta=-10)
    _verify(_static(pub), token)
    assert token not in caplog.text
    assert token.split(".")[2] not in caplog.text  # nor its signature segment


# --- the reason diagnosis -------------------------------------------------------------------------


def test_reason_expired(keypair, mint, caplog):
    _, pub = keypair
    _verify(_static(pub), mint(exp_delta=-10))
    assert _denials(caplog)[0]["reason"] == "expired"


def test_reason_issuer(keypair, mint, caplog):
    _, pub = keypair
    _verify(_static(pub), mint(iss="https://evil.test"))
    fields = _denials(caplog)[0]
    assert fields["reason"] == "issuer"
    assert fields["claimed_iss"] == "https://evil.test"


def test_reason_audience(keypair, mint, caplog):
    _, pub = keypair
    _verify(_static(pub), mint(aud="wrong-audience"))
    assert _denials(caplog)[0]["reason"] == "audience"


def test_reason_scope(keypair, mint, caplog):
    _, pub = keypair
    _verify(_static(pub, required_scopes=["kdbx.admin"]), mint(scope="kdbx.read"))
    assert _denials(caplog)[0]["reason"] == "scope"


def test_reason_signature_or_key_when_everything_checkable_matches(keypair, mint, other_priv, caplog):
    """A token signed by an untrusted key has the right iss/aud/exp/scope — the one failure the
    unverified claims cannot explain, so it is named for what it must be."""
    _, pub = keypair
    _verify(_static(pub), mint(priv=other_priv))
    assert _denials(caplog)[0]["reason"] == "signature_or_key"


def test_reason_malformed_for_a_non_jwt(keypair, caplog):
    _, pub = keypair
    _verify(_static(pub), "not-a-jwt-at-all")
    fields = _denials(caplog)[0]
    assert fields["reason"] == "malformed"
    assert "claimed_sub" not in fields  # nothing decodable, so nothing claimed


def test_expiry_wins_over_the_other_checks(keypair, mint, caplog):
    """An expired token from the wrong issuer is reported as expired: the most common, most
    benign cause first, so a stale-token flurry is not misread as an attack."""
    _, pub = keypair
    _verify(_static(pub), mint(exp_delta=-10, iss="https://evil.test"))
    assert _denials(caplog)[0]["reason"] == "expired"


# --- every mode -----------------------------------------------------------------------------------


def test_jwks_denials_are_audited_with_the_mode_as_target(mint, jwks_uri, caplog):
    provider = build_auth_provider(
        AuthSettings(mode="jwks", jwks_uri=jwks_uri, issuer=ISSUER, audience=AUDIENCE)
    )
    _verify(provider, mint(aud="wrong-audience"))
    fields = _denials(caplog)[0]
    assert fields["target"] == "jwks"
    assert fields["reason"] == "audience"


def test_proxy_modes_do_not_compare_the_token_against_upstream_settings(mint, caplog):
    """oidc_proxy / entra verify a token the proxy itself issued, whose iss/aud are the proxy's own.
    Comparing those against KX_MCP_AUTH_ISSUER would call every denial an issuer mismatch."""

    class _RejectsEverything:
        async def verify_token(self, token):
            return None

    settings = AuthSettings(mode="oidc_proxy", issuer=ISSUER, audience=AUDIENCE)
    provider = audit_authentication_denials(_RejectsEverything(), settings)

    _verify(provider, mint(iss="https://proxy.example/mcp"))  # a proxy-issued shape, valid exp

    fields = _denials(caplog)[0]
    assert fields["target"] == "oidc_proxy"
    assert fields["reason"] == "signature_or_key"  # not "issuer"


def test_wrapping_keeps_the_provider_object_and_its_type(keypair):
    """Downstream isinstance checks (bare verifier vs RemoteAuthProvider) must keep working."""
    _, pub = keypair
    provider = _static(pub)
    assert isinstance(provider, JWTVerifier)


def test_a_provider_without_verify_token_is_left_alone():
    """The oidc_proxy unit tests substitute a bare fake for the proxy; nothing to observe there."""
    double = object()
    assert audit_authentication_denials(double, AuthSettings(mode="oidc_proxy")) is double


def test_oversized_or_spaced_claimed_values_are_dropped_not_logged(keypair, mint, caplog):
    """The claimed_* text is attacker-chosen; it must not dictate the log line's size or shape."""
    _, pub = keypair
    _verify(_static(pub), mint(exp_delta=-10, client_id="x" * 129, extra_claims={"iss": "has a space"}))
    fields = _denials(caplog)[0]
    assert fields["reason"] == "expired"
    assert "claimed_sub" not in fields and "claimed_azp" not in fields and "claimed_iss" not in fields


def test_wrapping_twice_logs_once(keypair, mint, caplog):
    _, pub = keypair
    provider = _static(pub)
    audit_authentication_denials(provider, AuthSettings(mode="static", public_key=pub, issuer=ISSUER))
    _verify(provider, mint(exp_delta=-10))
    assert len(_denials(caplog)) == 1
