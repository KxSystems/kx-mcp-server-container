"""The shared, fastmcp-free JWT verifier — one implementation, two front doors.

``verify_token(token, settings)`` decodes and validates a bearer against the same ``KX_MCP_AUTH``
config the container enforces, using **joserfc** (the exact JOSE library FastMCP's ``JWTVerifier``
uses under the hood) so the CLI's verdict matches what the container would do. The serving path
(``kx_mcp_core.auth.providers``) keeps FastMCP's ``JWTVerifier`` — this is the *same* validation
logic, mirrored; the repo's ``test_auth_cli_contract`` pins the two in agreement.

Unlike the FastMCP verifier (which collapses every failure to ``None``), this returns a
**categorised** :class:`VerifyResult` so the ``kx auth introspect`` CLI can map to its stable
exit-code contract:

* ``OK``            — valid, accepted                                  (exit 0 / HTTP 200)
* ``ERROR``         — malformed token, bad signature, unreachable/bad JWKS  (exit 1 / HTTP 400/500)
* ``AUTH_REQUIRED`` — well-signed but expired; the agent should re-login   (exit 3 / HTTP 401)
* ``DENIED``        — well-signed but not acceptable here: issuer / audience / required-scope
                      mismatch                                          (exit 4 / HTTP 403)

The static-vs-denied split mirrors HTTP 401/403: a structurally broken token is an ``ERROR``; a
genuine, properly-signed token that simply isn't authorised for this resource is ``DENIED``.
"""

from __future__ import annotations

import base64
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import httpx
from joserfc import jwk, jwt
from joserfc.errors import JoseError

from .settings import AuthSettings

# Verdict categories (stable; the CLI maps these to exit codes).
OK = "ok"
ERROR = "error"
AUTH_REQUIRED = "auth_required"
DENIED = "denied"


@dataclass(frozen=True)
class VerifyResult:
    """The categorised outcome of verifying a bearer. ``valid`` is true only for :data:`OK`."""

    category: str
    valid: bool
    claims: Optional[dict] = None
    client_id: Optional[str] = None
    scopes: Optional[list] = None
    reason: Optional[str] = None

    @classmethod
    def ok(cls, claims: dict, client_id: str, scopes: list) -> "VerifyResult":
        return cls(OK, True, claims=claims, client_id=client_id, scopes=scopes)

    @classmethod
    def fail(cls, category: str, reason: str) -> "VerifyResult":
        return cls(category, False, reason=reason)


def resolve_public_key(settings: AuthSettings) -> str:
    """Resolve the static-mode public-key PEM (inline or by path). Shared with the container's
    ``_build_static`` so both read the key identically."""
    if settings.public_key:
        return settings.public_key
    if settings.public_key_path:
        return Path(settings.public_key_path).read_text()
    raise ValueError(
        "KX_MCP_AUTH=static requires KX_MCP_AUTH_PUBLIC_KEY or KX_MCP_AUTH_PUBLIC_KEY_PATH "
        "(an RS256 public-key PEM)"
    )


def _import_key(key: str | bytes | dict, algorithm: str):
    """Import key material for the algorithm family (mirrors FastMCP's ``_import_key_for_algorithm``)."""
    if algorithm.startswith("HS"):
        return jwk.import_key(key, "oct")
    if algorithm.startswith(("RS", "PS")):
        return jwk.import_key(key, "RSA")
    if algorithm.startswith("ES"):
        return jwk.import_key(key, "EC")
    raise ValueError(f"Unsupported algorithm: {algorithm}.")


def _jwk_to_pem(key_data: dict) -> str:
    kty = key_data.get("kty")
    if kty == "RSA":
        return jwk.import_key(key_data, "RSA").as_pem().decode("utf-8")
    if kty == "EC":
        return jwk.import_key(key_data, "EC").as_pem().decode("utf-8")
    raise ValueError(f"Unsupported JWK key type: {kty!r}")


def _decode_header(token: str) -> dict:
    """Decode a JWT's header segment without verifying — used only to read ``kid`` for JWKS lookup."""
    try:
        header_b64 = token.split(".")[0]
        padded = header_b64 + "=" * (-len(header_b64) % 4)
        return json.loads(base64.urlsafe_b64decode(padded))
    except Exception as exc:  # malformed token — caller surfaces as ERROR
        raise ValueError(f"could not decode token header: {exc}") from exc


def _resolve_jwks_key(settings: AuthSettings, token: str) -> str:
    """Fetch the JWKS and return the PEM for the token's ``kid`` (or the sole key)."""
    if not settings.jwks_uri:
        raise ValueError("KX_MCP_AUTH=jwks requires KX_MCP_AUTH_JWKS_URI (the issuer's JWKS endpoint)")
    kid = _decode_header(token).get("kid")
    resp = httpx.get(settings.jwks_uri, timeout=10.0)
    resp.raise_for_status()
    keys = {}
    for key_data in resp.json().get("keys", []):
        keys[key_data.get("kid") or "_default"] = _jwk_to_pem(key_data)
    if kid:
        if kid not in keys:
            raise ValueError(f"key ID {kid!r} not found in JWKS")
        return keys[kid]
    if len(keys) == 1:
        return next(iter(keys.values()))
    if not keys:
        raise ValueError("no keys found in JWKS")
    raise ValueError("multiple keys in JWKS but no key ID (kid) in token")


def _extract_scopes(claims: dict) -> list:
    """Scopes from the standard ``scope`` claim, falling back to ``scp`` (mirrors FastMCP)."""
    for claim in ("scope", "scp"):
        value = claims.get(claim)
        if isinstance(value, str):
            return value.split()
        if isinstance(value, list):
            return value
    return []


def _audience_ok(expected: Any, actual: Any) -> bool:
    expected_list = expected if isinstance(expected, list) else [expected]
    actual_list = actual if isinstance(actual, list) else [actual]
    return any(e in actual_list for e in expected_list)


def verify_token(token: str, settings: AuthSettings) -> VerifyResult:
    """Verify ``token`` against ``settings`` and return a categorised :class:`VerifyResult`.

    Signature failures / malformed tokens / JWKS problems are :data:`ERROR`; a well-signed but
    expired token is :data:`AUTH_REQUIRED`; a well-signed token failing issuer / audience /
    required-scope checks is :data:`DENIED`. The validation order mirrors FastMCP's ``JWTVerifier``.
    """
    if settings.mode not in ("static", "jwks"):
        return VerifyResult.fail(
            ERROR, f"introspect requires KX_MCP_AUTH=static or jwks (got {settings.mode!r})"
        )

    # 1. Resolve the verification key, then verify the signature (joserfc raises on bad sig/format).
    try:
        if settings.mode == "static":
            verification_key = resolve_public_key(settings)
        else:
            verification_key = _resolve_jwks_key(settings, token)
        key = _import_key(verification_key, settings.algorithm)
        claims = jwt.decode(token, key, algorithms=[settings.algorithm]).claims
    except JoseError as exc:
        return VerifyResult.fail(ERROR, f"invalid signature or token format: {exc}")
    except httpx.HTTPError as exc:
        return VerifyResult.fail(ERROR, f"could not fetch JWKS: {exc}")
    except (ValueError, KeyError, TypeError) as exc:
        return VerifyResult.fail(ERROR, str(exc))

    client_id = str(
        claims.get("client_id") or claims.get("azp") or claims.get("sub") or "unknown"
    )

    # 2. Expiry — a real credential, just stale: the agent should re-login.
    exp = claims.get("exp")
    if exp is not None and exp < time.time():
        return VerifyResult.fail(AUTH_REQUIRED, "token expired")

    # 3. Issuer / audience / required scopes — well-signed but not acceptable here → DENIED.
    if settings.issuer and claims.get("iss") != settings.issuer:
        return VerifyResult.fail(
            DENIED, f"issuer mismatch (got {claims.get('iss')!r}, expected {settings.issuer!r})"
        )
    if settings.audience and not _audience_ok(settings.audience, claims.get("aud")):
        return VerifyResult.fail(
            DENIED, f"audience mismatch (got {claims.get('aud')!r}, expected {settings.audience!r})"
        )

    scopes = _extract_scopes(claims)
    if settings.required_scopes:
        missing = set(settings.required_scopes) - set(scopes)
        if missing:
            return VerifyResult.fail(DENIED, f"missing required scopes: {sorted(missing)}")

    return VerifyResult.ok(claims=claims, client_id=client_id, scopes=scopes)
