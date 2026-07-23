"""Session-scoped bearer token fixtures for the real-IdP harness.

Each fixture acquires a token once per test session via the configured
``TokenProvider`` and caches it. The ``make_token`` factory is the primary
interface; the per-persona shortcuts (``alice_token`` etc.) are convenience
wrappers for the common cases.

These fixtures are pulled into the ``tests/deterministic/realidp/conftest.py`` scope —
import them there rather than pulling them directly into test files.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

_PERSONAS_FILE = Path(__file__).parent.parent / "personas.yaml"


@pytest.fixture(scope="session")
def personas() -> dict:
    with open(_PERSONAS_FILE) as f:
        return yaml.safe_load(f)["personas"]


@pytest.fixture(scope="session")
def token_provider(personas):
    """Select and initialise the TokenProvider for this session."""
    from realidp.idp.providers.factory import select_provider
    provider = select_provider(personas)
    yield provider
    provider.cleanup()


@pytest.fixture(scope="session")
def make_token(token_provider):
    """Factory: ``make_token("P-Q-ALICE")`` → bearer token string."""
    def _make(persona_id: str) -> str:
        return token_provider.get_token(persona_id)
    return _make


@pytest.fixture(scope="session")
def root_token(make_token) -> str:
    return make_token("P-ROOT")


@pytest.fixture(scope="session")
def alice_token(make_token) -> str:
    return make_token("P-Q-ALICE")


@pytest.fixture(scope="session")
def bob_token(make_token) -> str:
    return make_token("P-Q-BOB")


@pytest.fixture(scope="session")
def charlie_token(make_token) -> str:
    """Token from the ``risk`` realm / 2nd Entra tenant — different issuer from ``quants``.

    Under ``AUTH_PROVIDER=entra``, a second tenant is required to produce a token
    with a different ``iss`` claim. Skip gracefully when single-tenant (the common
    case) — the skip auto-lifts once ``ENTRA_TENANT_ID_RISK`` is populated.
    """
    import os

    from realidp.idp.providers.factory import provider_name

    if provider_name() == "entra" and not os.environ.get("ENTRA_TENANT_ID_RISK"):
        pytest.skip(
            "test_wrong_issuer_token_rejected requires a 2nd Entra tenant; "
            "set ENTRA_TENANT_ID_RISK (+ ENTRA_DOMAIN_RISK, ENTRA_PASSWORD_CHARLIE) "
            "in .env.entra — see tests/deterministic/realidp/setup/entra/README.md"
        )
    return make_token("P-R-CHARLIE")
