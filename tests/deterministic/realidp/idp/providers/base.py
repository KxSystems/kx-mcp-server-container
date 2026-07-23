"""Swappable IdP token provider seam.

``AUTH_PROVIDER=keycloak|entra`` selects the concrete implementation.
The seam is deliberately thin: callers ask for a bearer string by persona ID
and present it as ``Authorization: Bearer <token>`` to the container under test.
"""

from __future__ import annotations

from abc import ABC, abstractmethod


class TokenProvider(ABC):
    """Acquire bearer tokens for test personas from a real Identity Provider."""

    @abstractmethod
    def get_token(self, persona_id: str) -> str:
        """Return a valid bearer token string for the given persona.

        Implementations may cache tokens for the session lifetime; callers
        should not assume tokens are fresh on every call.
        """

    def cleanup(self) -> None:
        """Called at session teardown. Override if the provider holds resources."""
