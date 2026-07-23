"""Real-IdP authentication tests — provider-agnostic.

All tests are marked ``@pytest.mark.realidp`` and are excluded from the default
``just test`` run. Supported providers: ``keycloak`` (now), ``entra`` (future).

To run against Keycloak:

    docker compose -f tests/deterministic/realidp/setup/keycloak/docker-compose.yaml up -d
    uv run python tests/deterministic/realidp/setup/keycloak/keycloak_setup.py
    source envs/.env.keycloak
    just test-keycloak          # or: uv run pytest -m realidp -v

To run against Entra ID:

    cp tests/deterministic/realidp/envs/.env.entra.example tests/deterministic/realidp/envs/.env.entra
    # Fill in ENTRA_* input vars, then run the provisioner:
    uv run python tests/deterministic/realidp/setup/entra/entra_setup.py
    just test-entra          # sources .env.entra; AUTH_PROVIDER=entra selects the provider

The ``container_url`` fixture (session-scoped) provides a single MCP container for
the whole session — configured for ``KX_MCP_AUTH=jwks`` against the live IdP JWKS
endpoint. The ``*_token`` fixtures acquire real OIDC tokens via the password-grant flow.

TESTING.md rows: 2.38 – 2.41 (path column there is stale — points at the pre-rename
``kdbai/test_realidp_auth.py``, not this file) + ``test_discovery_advertised``, which has no
TESTING.md row yet.
"""

from __future__ import annotations

import pytest

from realidp.idp.helpers import (
    assert_authenticated,
    assert_discovery_advertised,
    assert_principal_visible,
    assert_rejected_401,
)

# container_url, alice_token, charlie_token injected from realidp.conftest / fixtures.tokens


@pytest.mark.realidp
def test_valid_token_accepted(container_url, alice_token):
    """Real IdP token for alice is accepted; tool call succeeds (2.38).

    Provider-agnostic: the assertion (tool reachable) is the same regardless of
    whether the token came from Keycloak or Entra ID. The IdP is selected by
    ``AUTH_PROVIDER`` and the token is acquired via the appropriate ``TokenProvider``.

    This is the real-IdP happy path: the container fetches the live JWKS, validates
    the RS256 signature, checks issuer + audience, and lets the tool run. Nothing is
    mocked.
    """
    assert_authenticated(container_url, alice_token)


@pytest.mark.realidp
def test_principal_visible_in_mounted_tool(container_url, alice_token):
    """Real IdP token propagates the principal into the mounted tool (2.39).

    The ``example_whoami`` tool reads ``current_principal().client_id``. With a real
    OIDC token the exact claim value (azp vs sub vs preferred_username) is an
    implementation detail of the FastMCP ↔ IdP claim mapping — here we just assert
    the principal is non-anonymous: the AccessToken contextvar is populated and
    crosses the mount boundary.
    """
    principal = assert_principal_visible(container_url, alice_token)
    assert principal != "anonymous"


@pytest.mark.realidp
def test_tampered_token_rejected(container_url, alice_token):
    """A real token with a corrupted signature byte is rejected with 401 (2.40).

    Takes alice's valid token, flips a character in the signature segment (the third
    ``.``-delimited part), and asserts the container returns 401. This proves the live
    JWKS fetch + RS256 signature check is real, not a formality.
    """
    parts = alice_token.split(".")
    assert len(parts) == 3, "Expected a three-part JWT"
    sig = parts[2]
    corrupted_sig = sig[:-1] + ("A" if sig[-1] != "A" else "B")
    bad_token = ".".join([parts[0], parts[1], corrupted_sig])
    assert_rejected_401(container_url, bad_token)


@pytest.mark.realidp
def test_discovery_advertised(container_url):
    """Container advertises RFC 9728 Protected Resource Metadata (2.42).

    Provider-agnostic: ``_spawn_container`` sets ``KX_MCP_AUTH_RESOURCE_URL`` for all
    realidp sessions, so the container wraps in ``RemoteAuthProvider`` and serves PRM
    at ``/.well-known/oauth-protected-resource/mcp``. This assertion proves the live
    endpoint is reachable and carries at least one ``authorization_servers`` entry.
    """
    assert_discovery_advertised(container_url)


@pytest.mark.realidp
def test_wrong_issuer_token_rejected(container_url, charlie_token):
    """A real token from the wrong realm/tenant is rejected (2.41).

    For Keycloak: charlie's token has ``iss = .../realms/risk``; the container is
    configured for ``.../realms/quants``. For Entra ID the equivalent is a token
    from a second tenant. Issuer mismatch → 401 in both cases.

    This is the real-world version of the single-issuer limitation documented in
    TESTING.md X.3: a valid token from the wrong IdP boundary must not grant
    access.
    """
    assert_rejected_401(container_url, charlie_token)
