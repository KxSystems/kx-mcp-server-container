"""The capability-check seam over the wire — allows/denies a dispatch by principal × action and
audits it.

Spawns the launcher as a real ``streamable-http`` child with ``KX_MCP_AUTH=static`` +
``KX_MCP_AUTHZ=static`` and a YAML capability policy, then drives it with an HTTP client. This is the
bug class in-process tests can't catch — the two things that depend on FastMCP composition internals:

1. an ``@authorize`` deny raised inside a *mounted* tool surfaces to the client as a clean tool error
   (not a crash, not a 500), and
2. the decision the decorator records on a contextvar inside the child crosses the mount boundary so
   the parent ``AuditMiddleware`` folds ``decision=`` / ``adapter=`` into the one dispatch audit line.

Uses the license-free ``example`` bundle's ``example_privileged`` tool (decorated
``@authorize(action="write", resource="example:thing")``). Fixtures ``keypair`` / ``mint`` come from
the repo-root ``conftest.py``; ``spawn_container`` from ``tests/conftest.py``.
"""

from __future__ import annotations

import asyncio

import pytest
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

ISSUER = "https://issuer.test"
AUDIENCE = "kx-mcp"


@pytest.fixture
def authz_url(tmp_path, keypair, spawn_container):
    """A spawned container in static inbound-auth + static authz mode, granting example:write to
    the `traders` group. Returns (url, proc) so a test can read the proc's captured audit log."""
    _, pub = keypair
    pub_path = tmp_path / "public.pem"
    pub_path.write_text(pub)
    policy = tmp_path / "capability-policy.yaml"
    policy.write_text("example:\n  write: [traders]\n")

    return spawn_container(
        "example",
        KX_MCP_AUTH="static",
        KX_MCP_AUTH_PUBLIC_KEY_PATH=str(pub_path),
        KX_MCP_AUTH_ISSUER=ISSUER,
        KX_MCP_AUTH_AUDIENCE=AUDIENCE,
        KX_MCP_AUTHZ="static",
        KX_MCP_AUTHZ_POLICY_FILE=str(policy),
        KX_MCP_LOG_LEVEL="INFO",
    )


def _call_privileged(url: str, token: str):
    async def go():
        async with Client(StreamableHttpTransport(url, auth=token)) as client:
            return await client.call_tool("example_privileged", {})

    return asyncio.run(go())


def test_granted_principal_is_allowed(authz_url, mint):
    """alice ∈ traders -> the gated tool runs and returns its result."""
    url, _ = authz_url
    alice = mint(client_id="alice", extra_claims={"groups": ["traders"]})
    result = _call_privileged(url, alice)
    assert result.data == "did the privileged thing"


def test_ungranted_principal_is_denied_with_a_clean_error(authz_url, mint):
    """bob ∉ traders -> a clean tool error crosses the mount boundary (not a crash / 500)."""
    url, _ = authz_url
    bob = mint(client_id="bob", extra_claims={"groups": ["viewers"]})
    with pytest.raises(Exception) as ei:  # fastmcp surfaces the tool error client-side
        _call_privileged(url, bob)
    assert "not authorized" in str(ei.value).lower()


def test_audit_line_carries_decision_and_adapter(authz_url, mint):
    """The decision crosses into the parent audit line: decision=allow/deny + adapter=static."""
    url, proc = authz_url
    _call_privileged(url, mint(client_id="alice", extra_claims={"groups": ["traders"]}))
    try:
        _call_privileged(url, mint(client_id="bob", extra_claims={"groups": ["viewers"]}))
    except Exception:
        pass  # the deny is asserted elsewhere; here we only want both audit lines emitted

    proc.terminate()
    try:
        proc.wait(timeout=10)
    except Exception:
        proc.kill()
    logs = proc.stdout.read() if proc.stdout else ""

    assert "decision=allow adapter=static" in logs, logs
    assert "decision=deny adapter=static" in logs, logs
    assert "outcome=denied" in logs, logs


# --- Entra + the capability check: the claims data path ---------------------------------------
# Proves an Entra-shaped token's claims flow inbound -> current_principal -> @authorize -> decide.
# Uses static validation with a locally-signed but Entra-SHAPED token (Entra issuer + app-`roles`
# claim). Faithful because the entra (AzureProvider) mode validates with the SAME JWTVerifier, so
# AccessToken.claims is populated identically — only the interactive login proxy differs (not here).

ENTRA_ISSUER = "https://login.microsoftonline.com/24ef354a-9b41-40f4-9da3-f993dbf4f10e/v2.0"


@pytest.fixture
def entra_authz_url(tmp_path, keypair, spawn_container):
    """A container that validates an Entra-style issuer and gates example:write on the Entra
    app-`roles` claim (KX_MCP_AUTHZ_GROUPS_CLAIM=roles), granting the role `Trader.Write`."""
    _, pub = keypair
    pub_path = tmp_path / "public.pem"
    pub_path.write_text(pub)
    policy = tmp_path / "capability-policy.yaml"
    policy.write_text("example:\n  write: [Trader.Write]\n")  # an Entra app-role value, not a group

    return spawn_container(
        "example",
        KX_MCP_AUTH="static",
        KX_MCP_AUTH_PUBLIC_KEY_PATH=str(pub_path),
        KX_MCP_AUTH_ISSUER=ENTRA_ISSUER,
        KX_MCP_AUTH_AUDIENCE=AUDIENCE,
        KX_MCP_AUTHZ="static",
        KX_MCP_AUTHZ_POLICY_FILE=str(policy),
        KX_MCP_AUTHZ_GROUPS_CLAIM="roles",  # Entra app roles, not Keycloak `groups`
        KX_MCP_LOG_LEVEL="INFO",
    )


def test_entra_roles_claim_grants_pep1(entra_authz_url, mint):
    """An Entra token carrying app-role `Trader.Write` is allowed — the `roles` claim reached the
    capability check."""
    url, _ = entra_authz_url
    token = mint(client_id="alice", iss=ENTRA_ISSUER, extra_claims={"roles": ["Trader.Write"]})
    assert _call_privileged(url, token).data == "did the privileged thing"


def test_entra_roles_claim_denies_pep1(entra_authz_url, mint):
    """An Entra token with a different app-role is denied — the capability check decided off the
    propagated `roles`."""
    url, _ = entra_authz_url
    token = mint(client_id="bob", iss=ENTRA_ISSUER, extra_claims={"roles": ["Viewer.Read"]})
    with pytest.raises(Exception) as ei:
        _call_privileged(url, token)
    assert "not authorized" in str(ei.value).lower()
