#!/usr/bin/env python
"""Print a Keycloak bearer for a demo persona, for `claude mcp add --header`.

Mints a token via the local realidp Keycloak (password grant), reusing the test harness's
provider factory + personas.yaml so there is one source of persona truth (the same alice/bob
personas the idp/kdbai/kdbx lanes all use).

Usage (Keycloak must be up + .env.keycloak sourced — see manual.md):
    uv run python demos/claude-code-live-kdbx/mint_token.py P-Q-ALICE
    ALICE=$(uv run python demos/claude-code-live-kdbx/mint_token.py P-Q-ALICE)
    claude mcp add --transport http kx-kdbx http://127.0.0.1:8000/mcp --header "Authorization: Bearer $ALICE"

Personas: P-Q-ALICE (viewer+trader — allowed), P-Q-BOB (viewer — capability check OK, data gate denied).
The provider reads KC_BASE / KC_CLIENT_ID / AUTH_PROVIDER from the environment (source
.env.keycloak first — the generic realidp Keycloak fixture, whose clients all carry the
groups mapper the policy keys on).
"""
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
# The realidp harness lives under tests/deterministic; put it on the path so `realidp.*` imports.
sys.path.insert(0, str(REPO_ROOT / "tests" / "deterministic"))

import yaml  # noqa: E402  (imported after the sys.path.insert above, deliberately)
from realidp.idp.providers.factory import select_provider  # noqa: E402

PERSONAS = REPO_ROOT / "tests" / "deterministic" / "realidp" / "idp" / "personas.yaml"


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: mint_token.py <PERSONA_ID>   e.g. P-Q-ALICE | P-Q-BOB", file=sys.stderr)
        return 2
    personas = yaml.safe_load(PERSONAS.read_text())["personas"]
    provider = select_provider(personas)
    # No trailing newline — keeps `$(...)` capture clean for the Authorization header.
    sys.stdout.write(provider.get_token(argv[1]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
