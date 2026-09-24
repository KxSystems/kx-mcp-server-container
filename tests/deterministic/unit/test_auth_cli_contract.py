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


@pytest.mark.parametrize(
    "scope_claim",
    [
        ["kdbx.read", 1, None],  # list with non-string members
        [["nested"]],  # list with an UNHASHABLE member
        12345,  # wrong type entirely
        {"scope": "kdbx.read"},  # wrong type, object
    ],
    ids=["non-string-members", "unhashable-member", "wrong-type-int", "wrong-type-object"],
)
@pytest.mark.parametrize("required", [None, ["kdbx.read"]], ids=["no-required", "required"])
def test_core_and_container_agree_on_hostile_scope_claims(keypair, mint, scope_claim, required):
    """The drift guard's blind spot, now closed.

    The parametrizations above only ever fed a *well-formed* scope claim, so `kx-auth-core` was free
    to drift from `JWTVerifier` on every malformed shape without this file noticing. That matters
    because `_extract_scopes` deliberately mirrors FastMCP: hardening it to drop non-string members
    and to name a malformed claim in its reason must not change the accept/reject verdict, and a
    future "just reject the hostile token" change here would silently make `kx auth introspect`
    disagree with what the container enforces. Neither side may raise, either — the unhashable case
    used to escape as a bare TypeError.
    """
    _, pub = keypair
    token = mint(extra_claims={"scope": scope_claim})
    kw = {"required_scopes": required} if required else {}
    settings = _settings(pub, **kw)
    assert verify_token(token, settings).valid == _container_accepts(settings, token)
