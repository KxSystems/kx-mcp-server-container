"""Entra ID TokenProvider — per-persona OAuth2 token acquisition.

Supports three flows per persona, selected by the ``flow:`` key in each
persona's ``entra:`` block in ``personas.yaml`` (default: ``password``):

  password           — ROPC (resource owner password credentials); the default
  client_credentials — service-principal / app-identity flow
  external_token     — pre-acquired token supplied directly (no HTTP call)

Ported from
``similarity-search/kdbai-db-tests/test_oauth/providers/entra.py``, adapted
to return a **bearer string** (``get_token() -> str``) rather than a
kdbai-SDK config file path — our ``TokenProvider`` seam is a thin
``get_token`` contract, not a file writer.

Environment variables (consumed globally):
    ENTRA_TENANT_ID  — Directory (tenant) ID
    ENTRA_CLIENT_ID  — Application (client) ID of ``kdbai-service-mcp-agent``
    ENTRA_SCOPE      — e.g. ``api://<client-id>/access offline_access``
                       (defaults to ``api://{ENTRA_CLIENT_ID}/access offline_access``)

Per-persona keys in the ``entra:`` block (``${VAR}`` expansion supported):
    All flows (optional):
        flow          — ``password`` | ``client_credentials`` | ``external_token``
        tenant_id     — per-persona tenant override, e.g. ``${ENTRA_TENANT_ID_RISK}``
                        (enables the 2nd-tenant charlie persona without code change)
    password flow:
        username      — UPN, e.g. ``alice@${ENTRA_DOMAIN}``
        password      — e.g. ``${ENTRA_PASSWORD_ALICE}``
    client_credentials flow:
        client_id     — SP client ID, e.g. ``${ENTRA_SP_CLIENT_ID}``
        client_secret — SP client secret
    external_token flow:
        access_token  — pre-acquired bearer, e.g. ``${ENTRA_EXT_ACCESS_TOKEN}``
"""

from __future__ import annotations

import os
import re

import requests

from realidp.idp.providers.base import TokenProvider

_TOKEN_URL_TEMPLATE = "https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"

REQUIRED_ENV_VARS = ["ENTRA_TENANT_ID", "ENTRA_CLIENT_ID"]


class EntraTokenProvider(TokenProvider):
    def __init__(self, personas: dict) -> None:
        missing = [v for v in REQUIRED_ENV_VARS if not os.environ.get(v)]
        if missing:
            raise RuntimeError(
                f"EntraTokenProvider: missing required env vars: {missing}\n"
                "  Source tests/deterministic/realidp/envs/.env.entra before running,\n"
                "  or use: just test-entra"
            )
        self._personas = personas
        self._tenant_id = os.environ["ENTRA_TENANT_ID"]
        self._client_id = os.environ["ENTRA_CLIENT_ID"]
        self._scope = os.environ.get(
            "ENTRA_SCOPE", f"api://{self._client_id}/access offline_access"
        )
        self._cache: dict[str, str] = {}

    def _resolve(self, value: str) -> str:
        """Expand ``${VAR}`` references from the environment.

        Raises ``RuntimeError`` loudly if any referenced variable is unset —
        a missing password is a configuration error, not a default.
        """
        def _replace(match: re.Match) -> str:
            var = match.group(1)
            resolved = os.environ.get(var)
            if not resolved:
                raise RuntimeError(
                    f"Env var '{var}' is not set "
                    "(required by persona config — add it to .env.entra)"
                )
            return resolved

        return re.sub(r"\$\{([^}]+)\}", _replace, value) if value else value

    def _token_url(self, cfg: dict) -> str:
        """Return the token endpoint, respecting a per-persona ``tenant_id`` override."""
        tenant_id = self._tenant_id
        if "tenant_id" in cfg:
            tenant_id = self._resolve(cfg["tenant_id"])
        return _TOKEN_URL_TEMPLATE.format(tenant_id=tenant_id)

    def _password_flow(self, cfg: dict, token_url: str) -> str:
        resp = requests.post(
            token_url,
            data={
                "grant_type": "password",
                "client_id": self._client_id,
                "username": self._resolve(cfg["username"]),
                "password": self._resolve(cfg["password"]),
                "scope": self._scope,
            },
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json()["access_token"]

    def _client_credentials_flow(self, cfg: dict, token_url: str) -> str:
        # client_credentials needs /.default — strip "/access" from the resource URI
        # (mirrors similarity-search/kdbai-db-tests/test_oauth/providers/entra.py:76)
        resource = self._scope.split()[0].replace("/access", "")
        cc_scope = f"{resource}/.default"
        resp = requests.post(
            token_url,
            data={
                "grant_type": "client_credentials",
                "client_id": self._resolve(cfg["client_id"]),
                "client_secret": self._resolve(cfg["client_secret"]),
                "scope": cc_scope,
            },
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json()["access_token"]

    def _external_token_flow(self, cfg: dict) -> str:
        return self._resolve(cfg["access_token"])

    def get_token(self, persona_id: str) -> str:
        if persona_id in self._cache:
            return self._cache[persona_id]

        persona = self._personas[persona_id]
        if "entra" not in persona:
            raise RuntimeError(
                f"Persona {persona_id!r} has no 'entra:' block in personas.yaml. "
                "Add one before running with AUTH_PROVIDER=entra."
            )
        cfg = persona["entra"]
        flow = cfg.get("flow", "password")
        token_url = self._token_url(cfg)

        if flow == "client_credentials":
            token = self._client_credentials_flow(cfg, token_url)
        elif flow == "external_token":
            token = self._external_token_flow(cfg)
        else:
            # Default: password (ROPC)
            token = self._password_flow(cfg, token_url)

        self._cache[persona_id] = token
        return token
