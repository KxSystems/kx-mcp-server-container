"""Keycloak TokenProvider — acquires tokens via the OAuth password-grant flow.

Requires ``requests`` (a dev dependency via the test suite — not added to the
container's runtime deps).

Environment variables consumed (set by sourcing ``envs/.env.keycloak``):
    KC_BASE          — Keycloak base URL, e.g. http://localhost:8080
    KC_REALM         — default realm (overridden per-persona via ``tenant`` key)
    KC_CLIENT_ID     — OAuth client registered in Keycloak
    KC_CLIENT_SECRET — client secret; set to empty string (or omit) for public clients
"""

from __future__ import annotations

import os

import requests

from realidp.idp.providers.base import TokenProvider


class KeycloakTokenProvider(TokenProvider):
    def __init__(self, personas: dict) -> None:
        self._base = os.environ["KC_BASE"]
        self._default_realm = os.environ.get("KC_REALM", "quants")
        self._client_id = os.environ.get("KC_CLIENT_ID", "kdbai-service")
        # Empty string = public client (omit client_secret from the POST).
        self._client_secret = os.environ.get("KC_CLIENT_SECRET", "")
        self._personas = personas
        self._cache: dict[str, str] = {}

    def get_token(self, persona_id: str) -> str:
        if persona_id in self._cache:
            return self._cache[persona_id]

        persona = self._personas[persona_id]
        realm = persona.get("tenant", self._default_realm)
        url = f"{self._base}/realms/{realm}/protocol/openid-connect/token"

        data: dict = {
            "grant_type": "password",
            "client_id": self._client_id,
            "username": persona["username"],
            "password": persona["password"],
            "scope": "openid",
        }
        # Omit client_secret for public clients (Keycloak rejects the param for
        # confidential clients when the value is wrong, so only include it when set).
        if self._client_secret:
            data["client_secret"] = self._client_secret

        resp = requests.post(url, data=data, timeout=10)
        resp.raise_for_status()
        token: str = resp.json()["access_token"]
        self._cache[persona_id] = token
        return token
