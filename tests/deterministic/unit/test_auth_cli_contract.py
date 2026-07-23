"""Drift guard: the lean `kx_auth_core.verify_token` and the container's FastMCP `JWTVerifier` agree.

`kx auth introspect` validates with joserfc directly (no fastmcp) while the container serves with
FastMCP's `JWTVerifier`. They are *meant* to be one implementation; this pins that — the same token +
keys must produce the same accept/reject verdict from both. If FastMCP's verifier semantics ever
shift, this fails before the CLI silently disagrees with what the container enforces.
"""

from __future__ import annotations

import asyncio

import pytest

from kx_auth_core import AuthSettings, verify_token
from kx_mcp_core.auth import build_auth_provider

# These must stay in sync with ISSUER/AUDIENCE in the repo-root conftest.py (the values
# the shared `mint` fixture encodes). They are plain string constants, not fixtures, so
# they cannot be injected automatically — a deliberate duplication of two stable values.
ISSUER = "https://issuer.test"
AUDIENCE = "kx-mcp"


def _settings(pub: str, **kw) -> AuthSettings:
    return AuthSettings(mode="static", public_key=pub, issuer=ISSUER, audience=AUDIENCE, **kw)


def _container_accepts(settings: AuthSettings, token: str) -> bool:
    provider = build_auth_provider(settings)

    async def go():
        return await provider.verify_token(token)

    return asyncio.run(go()) is not None


@pytest.mark.parametrize(
    "make_token_kwargs",
    [
        {},  # valid
        {"exp_delta": -10},  # expired
        {"aud": "someone-else"},  # audience mismatch
        {"iss": "https://evil.test"},  # issuer mismatch
    ],
    ids=["valid", "expired", "wrong-audience", "wrong-issuer"],
)
def test_core_and_container_agree(keypair, mint, make_token_kwargs):
    _, pub = keypair
    token = mint(**make_token_kwargs)
    settings = _settings(pub)
    assert verify_token(token, settings).valid == _container_accepts(settings, token)


def test_core_and_container_agree_on_bad_signature(keypair, mint, other_priv):
    _, pub = keypair
    token = mint(priv=other_priv)
    settings = _settings(pub)
    assert verify_token(token, settings).valid == _container_accepts(settings, token)


def test_core_and_container_agree_on_required_scopes(keypair, mint):
    _, pub = keypair
    token = mint(scope="kdbx.read")
    settings = _settings(pub, required_scopes=["kdbx.write"])
    assert verify_token(token, settings).valid == _container_accepts(settings, token)
