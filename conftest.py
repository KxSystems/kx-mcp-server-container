"""Shared auth-test fixtures (RSA keypair, token minting, an in-process JWKS server).

A repo-root conftest so the same helpers serve every test tree — `kx-auth-core`, `kx-auth`, and the
container-level `tests/` — instead of being re-declared per module. Tokens are **minted with PyJWT**
while the lean core verifies with **joserfc**: deliberate cross-library coverage. The JWKS server is
a dependency-free in-process stand-in for a mock-oauth2-server (mirrors `tests/test_auth.py`).
"""

from __future__ import annotations

import json
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

KID = "test-key-1"
ISSUER = "https://issuer.test"
AUDIENCE = "kx-mcp"

_MOCK_STS_SECRET = "test-secret-key-at-least-32-bytes-long!"


def _gen_rsa() -> tuple[str, str]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    priv = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    pub = (
        key.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )
    return priv, pub


@pytest.fixture(scope="session")
def keypair() -> tuple[str, str]:
    """A session RSA keypair as (private PEM, public PEM)."""
    return _gen_rsa()


@pytest.fixture
def other_priv() -> str:
    """A second private key the verifier does NOT trust — for bad-signature cases."""
    priv, _ = _gen_rsa()
    return priv


@pytest.fixture
def mint(keypair):
    """Factory: mint an RS256 bearer. `exp_delta=-10` → expired; pass `priv=other_priv` → bad sig."""
    default_priv, _ = keypair

    def _mint(
        *,
        priv: str | None = None,
        exp_delta: int = 3600,
        scope: str = "kdbx.read",
        client_id: str = "alice",
        aud: str = AUDIENCE,
        iss: str = ISSUER,
        extra_claims: dict | None = None,
    ) -> str:
        now = int(time.time())
        payload = {
            "iss": iss,
            "aud": aud,
            "sub": client_id,
            "client_id": client_id,
            "scope": scope,
            "iat": now,
            "exp": now + exp_delta,
        }
        if extra_claims:
            payload.update(extra_claims)  # e.g. {"groups": [...]} for the capability-check tests
        return jwt.encode(payload, priv or default_priv, algorithm="RS256", headers={"kid": KID})

    return _mint


@pytest.fixture
def jwks_uri(keypair):
    """An in-process JWKS endpoint serving the keypair's public key (no external IdP needed)."""
    _, pub = keypair
    pub_key = serialization.load_pem_public_key(pub.encode())
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(pub_key))
    jwk.update({"kid": KID, "use": "sig", "alg": "RS256"})
    body = json.dumps({"keys": [jwk]}).encode()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 (http.server API)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):  # silence per-request logging
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}/jwks"
    finally:
        httpd.shutdown()


@pytest.fixture
def mock_sts():
    """An in-process OAuth token endpoint shared across all test trees.

    Captures each POST body (form fields + Authorization header) and returns a minted HS256 JWT
    echoing the requested audience. Yields ``(token_url, captured)`` where ``captured`` is a list
    of dicts — one per request — with a ``_authorization`` key for the Authorization header value.
    """
    captured: list[dict] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            body = self.rfile.read(int(self.headers.get("Content-Length", 0))).decode()
            form = {k: v[0] for k, v in urllib.parse.parse_qs(body).items()}
            captured.append({**form, "_authorization": self.headers.get("Authorization", "")})
            aud = form.get("audience", "backend")
            token = jwt.encode(
                {"aud": aud, "sub": "svc-account", "jti": "exch-jti-1", "scope": "kdbx.read"},
                _MOCK_STS_SECRET,
                algorithm="HS256",
            )
            payload = json.dumps(
                {"access_token": token, "token_type": "Bearer", "expires_in": 300}
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *_args):
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}/token", captured
    finally:
        httpd.shutdown()
