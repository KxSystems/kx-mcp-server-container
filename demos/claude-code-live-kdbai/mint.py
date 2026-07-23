# /// script
# requires-python = ">=3.10"
# dependencies = ["httpx", "pyyaml"]
# ///
"""Mint a Keycloak access token for a demo persona and print it to stdout.

Usage (from the repo root):
    uv run demos/claude-code-live-kdbai/mint.py alice
    uv run demos/claude-code-live-kdbai/mint.py bob
    uv run demos/claude-code-live-kdbai/mint.py root

Tokens live for 900 seconds.  Re-mint if you get a 401 after a pause.

Composes in shell substitution:
    TOKEN=$(uv run demos/claude-code-live-kdbai/mint.py alice)
    curl -H "Authorization: Bearer $TOKEN" ...

Environment:
    KC_BASE        Keycloak base URL  (default: http://localhost:8080)
    KC_CLIENT_ID   OAuth client id    (default: kdbai-service)
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import httpx
import yaml

KC_BASE = os.environ.get("KC_BASE", "http://localhost:8080")
KC_CLIENT_ID = os.environ.get("KC_CLIENT_ID", "kdbai-service")

# Persona creds live in the harness file — single source of truth, no duplication.
_PERSONAS_FILE = (
    Path(__file__).parent.parent.parent
    / "tests/deterministic/realidp/idp/personas.yaml"
)

# Friendly name → personas.yaml key
_PERSONA_KEYS: dict[str, str] = {
    "alice": "P-Q-ALICE",
    "bob": "P-Q-BOB",
    "root": "P-ROOT",
}


def _load_persona(name: str) -> dict:
    key = _PERSONA_KEYS.get(name.lower())
    if not key:
        known = ", ".join(sorted(_PERSONA_KEYS))
        print(f"ERROR: unknown persona '{name}'.  Known: {known}", file=sys.stderr)
        sys.exit(2)
    data = yaml.safe_load(_PERSONAS_FILE.read_text())
    persona = data["personas"].get(key)
    if not persona:
        print(f"ERROR: key '{key}' not found in personas.yaml", file=sys.stderr)
        sys.exit(1)
    return persona


def mint(name: str) -> str:
    persona = _load_persona(name)
    tenant = persona["tenant"]
    username = persona["username"]
    password = persona["password"]

    url = f"{KC_BASE}/realms/{tenant}/protocol/openid-connect/token"
    resp = httpx.post(
        url,
        data={
            "grant_type": "password",
            "client_id": KC_CLIENT_ID,
            "username": username,
            "password": password,
            "scope": "openid",
        },
        timeout=10,
    )
    if resp.status_code != 200:
        print(
            f"ERROR: Keycloak returned {resp.status_code} for {username}@{tenant}:\n{resp.text}",
            file=sys.stderr,
        )
        sys.exit(1)
    return resp.json()["access_token"]


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} <alice|bob|root>", file=sys.stderr)
        sys.exit(2)
    print(mint(sys.argv[1]))
