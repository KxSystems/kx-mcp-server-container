"""Unit tests for the lean shared verifier: the categorised verdict on each token condition.

Static and jwks modes are exercised against the in-process keypair / JWKS fixtures (repo-root
conftest); no fastmcp and no external IdP. The categories map to the CLI's exit-code contract.
"""

from __future__ import annotations

from kx_auth_core import (
    AUTH_REQUIRED,
    DENIED,
    ERROR,
    OK,
    AuthSettings,
    verify_token,
)

# Must match the values the shared `mint` / `jwks_uri` fixtures encode (repo-root conftest.py).
ISSUER = "https://issuer.test"
AUDIENCE = "kx-mcp"


def _static(pub: str, **kw) -> AuthSettings:
    return AuthSettings(mode="static", public_key=pub, issuer=ISSUER, audience=AUDIENCE, **kw)


def test_static_valid_token_is_ok(keypair, mint):
    _, pub = keypair
    result = verify_token(mint(), _static(pub))
    assert result.category == OK and result.valid
    assert result.client_id == "alice"
    assert result.scopes == ["kdbx.read"]
    assert result.claims["iss"] == ISSUER


def test_static_bad_signature_is_error(keypair, mint, other_priv):
    _, pub = keypair
    result = verify_token(mint(priv=other_priv), _static(pub))
    assert result.category == ERROR and not result.valid


def test_static_expired_is_auth_required(keypair, mint):
    _, pub = keypair
    result = verify_token(mint(exp_delta=-10), _static(pub))
    assert result.category == AUTH_REQUIRED and not result.valid


def test_malformed_token_is_error(keypair):
    _, pub = keypair
    result = verify_token("not-a-jwt", _static(pub))
    assert result.category == ERROR and not result.valid


def test_issuer_mismatch_is_denied(keypair, mint):
    _, pub = keypair
    result = verify_token(mint(iss="https://evil.test"), _static(pub))
    assert result.category == DENIED and "issuer" in result.reason


def test_audience_mismatch_is_denied(keypair, mint):
    _, pub = keypair
    result = verify_token(mint(aud="someone-else"), _static(pub))
    assert result.category == DENIED and "audience" in result.reason


def test_missing_required_scope_is_denied(keypair, mint):
    _, pub = keypair
    result = verify_token(mint(scope="kdbx.read"), _static(pub, required_scopes=["kdbx.write"]))
    assert result.category == DENIED and "scope" in result.reason


def test_required_scope_present_is_ok(keypair, mint):
    _, pub = keypair
    result = verify_token(
        mint(scope="kdbx.read kdbx.write"), _static(pub, required_scopes=["kdbx.write"])
    )
    assert result.category == OK and result.valid


def test_static_without_key_is_error(keypair, mint):
    """A misconfigured static mode (no key material) surfaces as ERROR, not a stack trace."""
    result = verify_token(mint(), AuthSettings(mode="static", issuer=ISSUER, audience=AUDIENCE))
    assert result.category == ERROR and "PUBLIC_KEY" in result.reason


def test_unset_mode_is_error(keypair, mint):
    result = verify_token(mint(), AuthSettings(mode="unset"))
    assert result.category == ERROR


# --- jwks (against the in-process JWKS endpoint) -----------------------------------------------


def _jwks(uri: str, **kw) -> AuthSettings:
    return AuthSettings(mode="jwks", jwks_uri=uri, issuer=ISSUER, audience=AUDIENCE, **kw)


def test_jwks_valid_token_is_ok(mint, jwks_uri):
    result = verify_token(mint(), _jwks(jwks_uri))
    assert result.category == OK and result.valid and result.client_id == "alice"


def test_jwks_expired_is_auth_required(mint, jwks_uri):
    result = verify_token(mint(exp_delta=-10), _jwks(jwks_uri))
    assert result.category == AUTH_REQUIRED


def test_jwks_bad_signature_is_error(mint, jwks_uri, other_priv):
    """Signed with the right kid but a key absent from the JWKS — signature can't verify."""
    result = verify_token(mint(priv=other_priv), _jwks(jwks_uri))
    assert result.category == ERROR


def test_jwks_unreachable_endpoint_is_error(mint):
    result = verify_token(mint(), _jwks("http://127.0.0.1:1/jwks"))
    assert result.category == ERROR and "JWKS" in result.reason
