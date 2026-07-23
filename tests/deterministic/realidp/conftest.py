"""realidp/ — shared IdP fixtures registered at the root of the harness.

This conftest is the fixture anchor for the whole ``realidp/`` tree. It:

1. Fails fast (``_require_idp``, autouse) when the configured IdP is unreachable.
   Session-scoped + autouse so it fires once before the first realidp test;
   because realidp tests are deselected by default (``addopts = -m 'not realidp'``),
   this never fires during a normal ``just test`` run.

2. Provides ``container_url`` — one MCP container (``--bundles example``) for the whole
   realidp session; sufficient for all inbound-auth assertions.

3. Pulls in the bearer token fixtures from ``realidp.idp.fixtures.tokens`` so they are
   visible to every public lane (``idp/``, ``kdbai/``, ``kdbx/``).

Layout:
  idp/       — shared IdP layer: providers, token fixtures, personas, inbound-auth tests
  kdbai/     — kdbai backend lane: ACL tests, kdbai-db guard, kdbai container fixtures
  kdbx/      — kdb-x identity-ferry lane against a throwaway q process

  setup/     — local infra (docker-compose, keycloak_setup.py, seed.py, entra/)
  envs/      — .env.*.example templates (gitignored live files sourced by just recipes)
  _spawn.py  — _free_port, _wait_until_listening, _spawn_container
"""

from __future__ import annotations

import os

import pytest
import requests

from realidp._spawn import _spawn_container

# Import session-scoped token fixtures so pytest registers them for the whole tree.
# (pytest_plugins is not allowed in non-root conftests — direct import is the
# established pattern here.)
from realidp.idp.fixtures.tokens import (  # noqa: F401
    alice_token,
    bob_token,
    charlie_token,
    make_token,
    personas,
    root_token,
    token_provider,
)

# ---------------------------------------------------------------------------
# Path constants (for fail-fast messages)
# ---------------------------------------------------------------------------

_KC_ENV_FILE = "tests/deterministic/realidp/envs/.env.keycloak"
_KC_COMPOSE = "tests/deterministic/realidp/setup/keycloak/docker-compose.yaml"
# Minimum env vars required for the Entra path (domain + passwords are per-persona,
# validated lazily by EntraTokenProvider._resolve at token-acquisition time).
_ENTRA_REQUIRED = {"ENTRA_TENANT_ID", "ENTRA_CLIENT_ID"}


# ---------------------------------------------------------------------------
# Fail-fast guard
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session", autouse=True)
def _require_idp() -> None:
    """Fail fast if the IdP env is not configured when realidp tests actually run.

    Session-scoped and autouse so it fires once before the first realidp test in
    the session. Because realidp tests are deselected by default (``addopts``),
    the normal ``just test`` run never triggers this check.
    """
    from realidp.idp.providers.factory import provider_name
    provider = provider_name()

    if provider == "entra":
        missing = _ENTRA_REQUIRED - set(os.environ)
        if missing:
            pytest.exit(
                f"AUTH_PROVIDER=entra but missing env vars: {missing}",
                returncode=1,
            )
        return

    if provider == "keycloak":
        kc_base = os.environ.get("KC_BASE", "")
        if not kc_base:
            pytest.exit(
                "Keycloak env vars not set — did you source the env file?\n"
                f"  Run from the repo root:\n"
                f"    source {_KC_ENV_FILE}\n"
                f"    uv run pytest -m 'realidp and not kdbai and not kdbx' -v\n"
                f"  Or use: just test-keycloak  (sources the env automatically)",
                returncode=1,
            )
        try:
            r = requests.get(f"{kc_base}/health/ready", timeout=5)
            if r.status_code != 200:
                pytest.exit(
                    f"Keycloak health check failed (HTTP {r.status_code}).\n"
                    f"  Is Keycloak running at {kc_base}?\n"
                    f"  docker compose -f {_KC_COMPOSE} up -d",
                    returncode=1,
                )
        except Exception as exc:
            pytest.exit(
                f"Keycloak unreachable at {kc_base}: {exc}\n"
                f"  docker compose -f {_KC_COMPOSE} up -d",
                returncode=1,
            )


# ---------------------------------------------------------------------------
# Session-scoped container — one process for the whole real-IdP session
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def container_url() -> str:
    """Session-scoped MCP container for all real-IdP inbound-auth tests.

    Mounts the ``example`` bundle (no backend DB required). One container serves
    the whole realidp session; Keycloak startup is ~30 s so per-test containers
    would be impractically slow.

    Requires (sourced by ``just test-keycloak`` from ``envs/.env.keycloak``):
        KC_BASE, KC_REALM, KC_CLIENT_ID / KX_MCP_AUTH_AUDIENCE
    """
    url, proc = _spawn_container(bundles="example")
    yield url
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except Exception:
        proc.kill()
