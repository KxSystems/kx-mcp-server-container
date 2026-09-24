"""Real authorization-code flow against ``KX_MCP_AUTH=oidc_proxy`` (2.51–2.53).

Keycloak-only: the driver scrapes Keycloak's rendered login form, which has no Entra equivalent
in this harness. The existing realidp lane mints tokens by password grant (see
``idp/providers/keycloak.py``), which never reaches the proxy — in this mode ``/token`` hands the
client a bearer the *container* minted, and the Keycloak token stays server-side. Driving the real
browser-shape flow (``idp/oidc_proxy_driver.py``) is the only way to exercise that path.

Requires ``keycloak_setup.py`` to have provisioned the ``kx-mcp-proxy`` confidential client (see
``setup/keycloak/keycloak_config.json``'s ``tenants.quants.proxy_client``) with a redirect URI
exact-matching ``KC_PROXY_PORT`` (default 8765) — see ``realidp/idp/README.md``.
"""

from __future__ import annotations

import os

import pytest

from realidp._spawn import _spawn_oidc_proxy_container
from realidp.idp.helpers import assert_authenticated, assert_principal_visible, assert_rejected_401
from realidp.idp.oidc_proxy_driver import run_authorization_code_flow
from realidp.idp.providers.factory import provider_name

_PORT = int(os.environ.get("KC_PROXY_PORT", "8765"))
_BASE_URL = f"http://127.0.0.1:{_PORT}"

pytestmark = [
    pytest.mark.realidp,
    pytest.mark.skipif(
        provider_name() != "keycloak",
        reason=(
            "the oidc_proxy code-flow lane is Keycloak-only: it needs a pre-provisioned "
            "confidential client with a port-exact redirect URI and a scrapeable login form"
        ),
    ),
]


@pytest.fixture(scope="module")
def oidc_proxy_container_url(tmp_path_factory):
    """A container in ``oidc_proxy`` mode on the fixed port, for this module only.

    Deliberately not in ``realidp/conftest.py`` — the ``kdbai``/``kdbx`` lanes must not pay for a
    second container, nor race on the pinned port.
    """
    home = tmp_path_factory.mktemp("fastmcp-oauth-proxy-home")
    url, proc = _spawn_oidc_proxy_container(port=_PORT, fastmcp_home=str(home))
    yield url
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except Exception:
        proc.kill()


@pytest.fixture(scope="module")
def flow_result(oidc_proxy_container_url, personas):
    alice = personas["P-Q-ALICE"]
    return run_authorization_code_flow(
        base_url=_BASE_URL, username=alice["username"], password=alice["password"]
    )


@pytest.mark.realidp
def test_authorization_code_flow_yields_a_working_bearer(oidc_proxy_container_url, flow_result):
    """DCR at the container -> /authorize -> consent -> real Keycloak login -> /auth/callback ->
    /token yields a bearer the container accepts on a real tool call (2.51).

    Nothing is mocked, and no password grant is used anywhere in this test: it is the only realidp
    path that exercises the proxy end to end.
    """
    assert flow_result.token_response.get("token_type") == "Bearer"
    assert_authenticated(oidc_proxy_container_url, flow_result.access_token)
    principal = assert_principal_visible(oidc_proxy_container_url, flow_result.access_token)
    assert principal != "anonymous"


@pytest.mark.realidp
def test_issued_bearer_is_the_containers_not_keycloaks(flow_result):
    """The bearer the client receives is minted by the container, not forwarded from Keycloak
    (2.52) — the proxy keeps the upstream token server-side.
    """
    kc_base = os.environ.get("KC_BASE", "http://localhost:8080").rstrip("/")
    kc_realm = os.environ.get("KC_REALM", "quants")
    realm_issuer = f"{kc_base}/realms/{kc_realm}"

    assert flow_result.header["alg"] == "HS256"  # Keycloak's tokens are RS256
    assert flow_result.claims["iss"].rstrip("/") == _BASE_URL
    assert flow_result.claims["iss"].rstrip("/") != realm_issuer
    aud = flow_result.claims["aud"]
    aud_list = [aud] if isinstance(aud, str) else aud
    assert f"{_BASE_URL}/mcp" in aud_list
    assert flow_result.claims.get("jti")


@pytest.mark.realidp
def test_password_grant_keycloak_token_is_rejected_in_proxy_mode(oidc_proxy_container_url, alice_token):
    """A direct password-grant Keycloak token — the one that passes 2.38 under ``jwks`` — is
    rejected here (2.53). This is the mode's defining behaviour: the container only honours
    bearers it minted itself, so a token it never issued has no ``jti`` mapping to an upstream
    token and is rejected outright.
    """
    assert_rejected_401(oidc_proxy_container_url, alice_token)
