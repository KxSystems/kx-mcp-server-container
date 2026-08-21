"""Unit tests for the lean shared verifier: the categorised verdict on each token condition.

Static and jwks modes are exercised against the in-process keypair / JWKS fixtures (repo-root
conftest); no fastmcp and no external IdP. The categories map to the CLI's exit-code contract.
"""

from __future__ import annotations

from contextlib import contextmanager

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


# --- the JWKS key-selection ladder (_resolve_jwks_key) ------------------------------------------
#
# Every case above uses the shared `mint`/`jwks_uri` fixtures, which always sign with `kid=KID`
# and always serve exactly that one matching key — so only the "kid present and found" branch of
# `_resolve_jwks_key`'s selection ladder is exercised. The other four branches (kid present but not
# in the JWKS; kid absent + sole key; kid absent + no keys; kid absent + multiple keys) need a
# token with no `kid` header and/or a JWKS response the shared fixtures can't produce, so this
# section builds its own throwaway JWKS server and no-kid token per case.
#
# These all fail closed already (mapped to ERROR via verify_token's `except (ValueError, ...)`
# clause) — no auth-bypass risk. The value here is pinning *which* key gets selected, since that is
# exactly the kind of divergence-from-FastMCP's-JWTVerifier this shared verifier must avoid.


@contextmanager
def _serve_jwks(keys: list[dict]):
    """A throwaway in-process JWKS endpoint serving `keys` verbatim. Same shape as the shared
    `jwks_uri` fixture (daemon-thread `ThreadingHTTPServer`) — including its `httpd.shutdown()` on
    exit, which is the part that actually matters: without it every call leaks a bound listening
    socket and a running `serve_forever()` thread for the rest of the process."""
    import json
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    body = json.dumps({"keys": keys}).encode()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 (http.server API)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{httpd.server_port}/jwks"
    finally:
        httpd.shutdown()


def _jwk_for(pub_pem: str, kid: str) -> dict:
    import json

    import jwt as _pyjwt
    from cryptography.hazmat.primitives import serialization

    pub_key = serialization.load_pem_public_key(pub_pem.encode())
    jwk = json.loads(_pyjwt.algorithms.RSAAlgorithm.to_jwk(pub_key))
    jwk.update({"kid": kid, "use": "sig", "alg": "RS256"})
    return jwk


def _mint_without_kid(priv: str) -> str:
    """Like the shared `mint` fixture, but with NO `kid` in the header — `mint` always sets
    `kid=KID`, which none of the kid-ABSENT ladder branches below can be reached with."""
    import time

    import jwt as _pyjwt

    now = int(time.time())
    payload = {
        "iss": ISSUER, "aud": AUDIENCE, "sub": "alice", "client_id": "alice",
        "scope": "kdbx.read", "iat": now, "exp": now + 3600,
    }
    return _pyjwt.encode(payload, priv, algorithm="RS256")  # no headers -> no kid


def test_jwks_kid_present_but_not_in_jwks_is_error(mint):
    """Token carries a `kid` (the shared `mint` fixture always sets one); the JWKS serves a
    DIFFERENT kid only -> 'key ID ... not found in JWKS', not a bad-signature error."""
    with _serve_jwks([_jwk_for(_gen_unrelated_pub(), "some-other-kid")]) as uri:
        result = verify_token(mint(), _jwks(uri))
    assert result.category == ERROR
    assert "not found in JWKS" in result.reason


def test_jwks_no_kid_sole_key_is_ok(keypair):
    """No `kid` in the token + exactly ONE key in the JWKS -> falls back to that sole key."""
    priv, pub = keypair
    with _serve_jwks([_jwk_for(pub, "irrelevant-kid")]) as uri:
        result = verify_token(_mint_without_kid(priv), _jwks(uri))
    assert result.category == OK and result.valid


def test_jwks_no_kid_empty_keys_is_error(keypair):
    """No `kid` in the token, JWKS serves ZERO keys -> 'no keys found in JWKS'."""
    priv, _ = keypair
    with _serve_jwks([]) as uri:
        result = verify_token(_mint_without_kid(priv), _jwks(uri))
    assert result.category == ERROR
    assert "no keys found in JWKS" in result.reason


def test_jwks_no_kid_multiple_keys_is_error(keypair):
    """No `kid` in the token, JWKS serves MULTIPLE keys -> ambiguous, 'no key ID (kid) in token'."""
    priv, pub = keypair
    with _serve_jwks([_jwk_for(pub, "key-1"), _jwk_for(_gen_unrelated_pub(), "key-2")]) as uri:
        result = verify_token(_mint_without_kid(priv), _jwks(uri))
    assert result.category == ERROR
    assert "multiple keys in JWKS" in result.reason


def _gen_unrelated_pub() -> str:
    """A public key PEM with no corresponding private key available to the test — stands in for
    'some other tenant's key' in the not-found/multiple-keys cases, where content never matters."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return (
        key.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )
