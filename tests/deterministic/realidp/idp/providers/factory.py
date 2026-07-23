"""TokenProvider factory — select the provider for the current session.

Reads ``AUTH_PROVIDER`` (default ``keycloak``) and returns the appropriate
:class:`TokenProvider` instance. This collapses the two formerly-inline
``if/elif`` provider-selection blocks (``tokens.py::token_provider`` and
``conftest.py::_require_idp``) into a single authority.

Supported values:
    keycloak  — password-grant against a local Keycloak container
    entra     — OAuth2 against Microsoft Entra ID (ROPC/password by default;
                client_credentials and external_token also supported per-persona)
"""

from __future__ import annotations

import os

from realidp.idp.providers.base import TokenProvider


def select_provider(personas: dict) -> TokenProvider:
    """Return a :class:`TokenProvider` for the ``AUTH_PROVIDER`` in the environment."""
    provider_name = os.environ.get("AUTH_PROVIDER", "keycloak")
    if provider_name == "keycloak":
        from realidp.idp.providers.keycloak import KeycloakTokenProvider
        return KeycloakTokenProvider(personas=personas)
    elif provider_name == "entra":
        from realidp.idp.providers.entra import EntraTokenProvider
        return EntraTokenProvider(personas=personas)
    else:
        raise ValueError(
            f"Unknown AUTH_PROVIDER: {provider_name!r}. "
            "Supported values: 'keycloak', 'entra'."
        )


def provider_name() -> str:
    """Return the current ``AUTH_PROVIDER`` value (defaulting to ``'keycloak'``)."""
    return os.environ.get("AUTH_PROVIDER", "keycloak")
