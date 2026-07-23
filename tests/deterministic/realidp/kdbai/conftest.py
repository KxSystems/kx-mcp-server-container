"""KDB.AI OAuth ACL lane fixtures.

Provides the kdbai-db guard and kdbai-specific container fixtures. The shared
IdP fixtures (``_require_idp``, ``container_url``, ``alice_token``, etc.) are
registered at the parent ``realidp/conftest.py`` and inherited automatically.

Fixtures defined here:
  ``_require_kdbai_db``       — fail-fast guard: kdbai-db socket reachable?
  ``kdbai_container_url``     — session-scoped container with ``--bundles kdbai``
                                + passthrough, quants realm
  ``kdbai_manager_container_url`` — same but with the manager realm JWKS (KA.5);
                                under Entra (single tenant → single issuer) this
                                collapses to ``kdbai_container_url``
  ``kdbai_service_account_container_url`` — same kdbai-db, but ``KDBAI_DB_OUTBOUND_STRATEGY=
                                service_account``: the container mints its own client-credentials
                                token (kdbai-service-worker) instead of forwarding the caller's
                                bearer. Inbound auth is unchanged (still quants-realm jwks), so
                                alice/bob still need a valid human bearer to reach the tool at all —
                                only what reaches kdbai-db differs.

Stack quick-start (from the repo root):
  KDB_LICENSE_B64=$(base64 < ~/.kx/kc.lic) \\
    docker compose -f tests/deterministic/realidp/setup/docker-compose.yaml \\
    --profile backends up -d
  uv run python tests/deterministic/realidp/setup/keycloak_setup.py \\
    tests/deterministic/realidp/setup/keycloak_config.json
  uv run python tests/deterministic/realidp/setup/seed.py
  just test-kdbai
"""

from __future__ import annotations

import os
import subprocess

import pytest

from realidp._spawn import _spawn_container
from realidp.idp.providers.factory import provider_name

# ---------------------------------------------------------------------------
# Path constants (for fail-fast messages)
# ---------------------------------------------------------------------------

_KDBAI_ENV_FILE = "tests/deterministic/realidp/envs/.env.kdbai"
_SETUP_COMPOSE = "tests/deterministic/realidp/setup/keycloak/docker-compose.yaml"
_SETUP_KC = "tests/deterministic/realidp/setup/keycloak/keycloak_setup.py"
_SETUP_SEED = "tests/deterministic/realidp/setup/keycloak/seed.py"

# Entra lane (AUTH_PROVIDER=entra) — bridged kdbai-db, no local IdP service.
_ENTRA_ENV_FILE = "tests/deterministic/realidp/envs/.env.entra"
_ENTRA_COMPOSE = "tests/deterministic/realidp/setup/entra/docker-compose.yaml"
_ENTRA_SEED = "tests/deterministic/realidp/setup/entra/seed.py"

_KDBAI_DEFAULT_HOST = "127.0.0.1"
_KDBAI_DEFAULT_PORT = 8082

# The quants-realm machine identity provisioned by keycloak_setup.py's `service_client` block —
# a client_credentials grant, distinct from the human kdbai-service client (see keycloak_config.json).
_SERVICE_ACCOUNT_CLIENT_ID = "kdbai-service-worker"
_SERVICE_ACCOUNT_CLIENT_SECRET = "kdbai-service-worker-secret"
# Must match kdbai-db's OAUTH_CLIENT_ID (docker-compose) — kdbai-service-worker's own client_id
# is not what kdbai-db trusts, hence the audience_override mapper provisioned alongside it.
_SERVICE_ACCOUNT_AUDIENCE = "kdbai-service"


# ---------------------------------------------------------------------------
# kdbai-db guard
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def _require_kdbai_db() -> None:
    """Fail fast if kdbai-db is not reachable at the configured host:port.

    Used as an explicit dependency of ``kdbai_container_url`` so it only fires
    when the KA.* ACL tests are actually selected — never during ``just test``
    or ``just test-keycloak``.
    """
    import socket

    host = os.environ.get("KDBAI_DB_HOST", _KDBAI_DEFAULT_HOST)
    port = int(os.environ.get("KDBAI_DB_PORT", str(_KDBAI_DEFAULT_PORT)))
    try:
        with socket.create_connection((host, port), timeout=3):
            pass
    except OSError as exc:
        if provider_name() == "entra":
            pytest.exit(
                f"kdbai-db not reachable at {host}:{port}: {exc}\n"
                f"  1. Start kdbai-db (Entra trust — no local IdP service):\n"
                f"     KDB_LICENSE_B64=$(base64 < ~/.kx/kc.lic) \\\n"
                f"       docker compose -f {_ENTRA_COMPOSE} up -d\n"
                f"     (entra_setup.py must already have provisioned the tenant)\n"
                f"  2. Seed databases:      uv run python {_ENTRA_SEED}\n"
                f"  3. Run: just test-kdbai-entra  (sources {_ENTRA_ENV_FILE})",
                returncode=1,
            )
        pytest.exit(
            f"kdbai-db not reachable at {host}:{port}: {exc}\n"
            f"  1. Start the stack:\n"
            f"     KDB_LICENSE_B64=$(base64 < ~/.kx/kc.lic) \\\n"
            f"       docker compose -f {_SETUP_COMPOSE} --profile backends up -d\n"
            f"  2. Provision Keycloak:  uv run python {_SETUP_KC} "
            f"tests/deterministic/realidp/setup/keycloak/keycloak_config.json\n"
            f"  3. Seed databases:      uv run python {_SETUP_SEED}\n"
            f"  4. Run: just test-kdbai  (sources {_KDBAI_ENV_FILE})",
            returncode=1,
        )


# ---------------------------------------------------------------------------
# kdbai container fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def kdbai_container_url(_require_kdbai_db) -> str:
    """Session-scoped MCP container for the kdbai OAuth ACL lane (KA.* tests).

    Spawns one container with ``--bundles kdbai`` + ``KX_MCP_AUTH=jwks`` pointing
    at the ``kdbai-service`` Keycloak client. Uses ``KDBAI_DB_OUTBOUND_STRATEGY=passthrough``
    so each inbound bearer is forwarded as the kdbai-db qipc connection credential,
    letting kdbai-db enforce ACL against the propagated identity.

    Requires (sourced by ``just test-kdbai`` from ``envs/.env.kdbai``):
        KC_BASE, KC_REALM, KX_MCP_AUTH_AUDIENCE
        KDBAI_DB_HOST, KDBAI_DB_PORT (defaults 127.0.0.1:8082)
    """
    host = os.environ.get("KDBAI_DB_HOST", _KDBAI_DEFAULT_HOST)
    port = os.environ.get("KDBAI_DB_PORT", str(_KDBAI_DEFAULT_PORT))
    url, proc = _spawn_container(
        bundles="kdbai",
        extra_env={
            "KDBAI_DB_HOST": host,
            "KDBAI_DB_PORT": port,
            "KDBAI_DB_OUTBOUND_STRATEGY": "passthrough",
            "KDBAI_DB_MODE": os.environ.get("KDBAI_DB_MODE", "qipc"),
        },
    )
    yield url
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()


@pytest.fixture(scope="session")
def kdbai_service_account_container_url(_require_kdbai_db) -> str:
    """Session-scoped MCP container using the ``service_account`` outbound strategy.

    Inbound auth is unchanged from ``kdbai_container_url`` (quants-realm jwks) — a valid human
    bearer is still required to reach the tool at all. What differs is what reaches kdbai-db:
    the container mints its own client-credentials token (``kdbai-service-worker``, a machine
    identity distinct from any human persona — see ``keycloak_config.json``'s ``service_client``
    block) instead of forwarding the caller's bearer. So a query's *result* should be identical
    regardless of which human called it — the backend never sees alice's or bob's claims.

    Requires the quants-realm ``service_client`` to have been provisioned by
    ``keycloak_setup.py`` and its grant seeded by ``seed.py`` (see module docstring).

    Keycloak-only for now — no Entra-side service client has been provisioned
    (``entra_setup.py`` has no ``service_client`` equivalent yet).
    """
    if provider_name() == "entra":
        pytest.skip("service_account outbound strategy is not yet provisioned under Entra")

    kc_base = os.environ.get("KC_BASE", "http://localhost:8080")
    kc_realm = os.environ.get("KC_REALM", "quants")
    host = os.environ.get("KDBAI_DB_HOST", _KDBAI_DEFAULT_HOST)
    port = os.environ.get("KDBAI_DB_PORT", str(_KDBAI_DEFAULT_PORT))
    url, proc = _spawn_container(
        bundles="kdbai",
        extra_env={
            "KDBAI_DB_HOST": host,
            "KDBAI_DB_PORT": port,
            "KDBAI_DB_OUTBOUND_STRATEGY": "service_account",
            "KDBAI_DB_MODE": os.environ.get("KDBAI_DB_MODE", "qipc"),
            "KDBAI_DB_TOKEN_URL": f"{kc_base}/realms/{kc_realm}/protocol/openid-connect/token",
            "KDBAI_DB_CLIENT_ID": _SERVICE_ACCOUNT_CLIENT_ID,
            "KDBAI_DB_CLIENT_SECRET": _SERVICE_ACCOUNT_CLIENT_SECRET,
            "KDBAI_DB_AUDIENCE": _SERVICE_ACCOUNT_AUDIENCE,
        },
    )
    yield url
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()


@pytest.fixture(scope="session")
def kdbai_manager_container_url(request, _require_kdbai_db) -> str:
    """Session-scoped MCP container for manager-realm tokens (KA.5 system_admin path).

    Under **Keycloak**, the main ``kdbai_container_url`` uses the quants realm JWKS,
    so root's manager-realm token is rejected at the inbound gate. This fixture spawns
    a second container whose JWKS URI and issuer point at the manager realm, letting
    root's token through. kdbai-db's ``ACL_SYSTEM_ADMIN_TENANT=manager`` then grants
    root system_admin access. Both containers connect to the same kdbai-db instance —
    only the inbound-auth realm differs.

    Under **Entra** (single tenant), root and alice share the *same* issuer — root is
    system_admin purely via its ``manager-admin`` group Object ID (kdbai-db's
    ``ACL_SYSTEM_ADMIN_GROUP``), not a separate realm. So there is no second issuer to
    accept and this fixture collapses to ``kdbai_container_url`` (no extra process).
    """
    if provider_name() == "entra":
        yield request.getfixturevalue("kdbai_container_url")
        return

    kc_base = os.environ.get("KC_BASE", "http://localhost:8080")
    host = os.environ.get("KDBAI_DB_HOST", _KDBAI_DEFAULT_HOST)
    port = os.environ.get("KDBAI_DB_PORT", str(_KDBAI_DEFAULT_PORT))
    manager_jwks_uri = f"{kc_base}/realms/manager/protocol/openid-connect/certs"
    manager_issuer = f"{kc_base}/realms/manager"
    url, proc = _spawn_container(
        bundles="kdbai",
        extra_env={
            "KX_MCP_AUTH_JWKS_URI": manager_jwks_uri,
            "KX_MCP_AUTH_ISSUER": manager_issuer,
            "KDBAI_DB_HOST": host,
            "KDBAI_DB_PORT": port,
            "KDBAI_DB_OUTBOUND_STRATEGY": "passthrough",
            "KDBAI_DB_MODE": os.environ.get("KDBAI_DB_MODE", "qipc"),
        },
    )
    yield url
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
