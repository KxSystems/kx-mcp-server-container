# Inbound-auth lane (2.38–2.42, provider-agnostic)

Tests the container's inbound `KX_MCP_AUTH=jwks` gate against a **live** IdP — real JWKS
fetch, real RS256 signature check, real issuer/audience validation. Provider-agnostic via the
`TokenProvider` seam (`AUTH_PROVIDER=keycloak|entra`): the same assertions run unchanged against
either IdP.

| Marker | Command | IdP |
|---|---|---|
| `@pytest.mark.realidp` | `just test-keycloak` | Keycloak (local) |
| `@pytest.mark.realidp` | `just test-entra` | Microsoft Entra ID (public) |

## Quick start

**Keycloak:**
```bash
docker compose -f tests/deterministic/realidp/setup/keycloak/docker-compose.yaml up -d
uv run python tests/deterministic/realidp/setup/keycloak/keycloak_setup.py \
    tests/deterministic/realidp/setup/keycloak/keycloak_config.json
source tests/deterministic/realidp/envs/.env.keycloak
just test-keycloak
```

**Entra ID:** one-time manual app registration bootstrap required first — see
[`setup/entra/README.md`](../setup/entra/README.md#app-registration-one-time-manual) — then:
```bash
source tests/deterministic/realidp/envs/.env.entra
uv run python tests/deterministic/realidp/setup/entra/entra_setup.py
# Writes ENTRA_DOMAIN, group Object IDs, and KX_MCP_AUTH_* back into .env.entra
source tests/deterministic/realidp/envs/.env.entra
just test-entra
```

## What each test asserts

| Test | Row | What it proves |
|---|---|---|
| `test_valid_token_accepted` | 2.38 | Happy path — live JWKS fetch, RS256 signature check, issuer/audience validation, tool call succeeds |
| `test_principal_visible_in_mounted_tool` | 2.39 | The validated principal (real OIDC claims, not a mock `AccessToken`) crosses the mount boundary into a backend tool |
| `test_tampered_token_rejected` | 2.40 | A corrupted signature byte → 401; proves the signature check is real, not a formality |
| `test_discovery_advertised` | 2.42 | Container advertises RFC 9728 Protected Resource Metadata at `/.well-known/oauth-protected-resource/mcp` |
| `test_wrong_issuer_token_rejected` | 2.41 | A token from the wrong realm/tenant → 401 — see below, **skipped** under single-tenant Entra |

Expected output once a lane is provisioned and `just test-entra` (or `-keycloak`) runs:

```
test_valid_token_accepted          PASSED   (alice's ROPC token accepted)
test_principal_visible_in_…        PASSED   (real user principal, non-anonymous)
test_tampered_token_rejected       PASSED   (corrupted signature → 401)
test_discovery_advertised          PASSED   (RFC 9728 PRM endpoint live)
test_wrong_issuer_token_rejected   SKIPPED  (single-tenant Entra — see below; always active under Keycloak)
```

## Wrong-issuer persona (test 2.41)

`test_wrong_issuer_token_rejected` requires a token from a different issuer boundary:

- **Keycloak** — charlie's token has `iss = .../realms/risk`; the container is configured for
  `.../realms/quants`. Always active (both realms are local).
- **Entra ID** — the equivalent is a token from a *second tenant*. With a single-tenant setup
  the test is **automatically skipped** — this is expected and correct, not a failure. To
  activate it, provision a second Entra tenant and set these vars in `.env.entra`:
  ```
  ENTRA_TENANT_ID_RISK=<second tenant Directory ID>
  ENTRA_DOMAIN_RISK=<second tenant domain>
  ENTRA_PASSWORD_CHARLIE=<charlie's password in the second tenant>
  ```
  Add the corresponding `charlie@${ENTRA_DOMAIN_RISK}` user + group in the second tenant. The
  skip auto-lifts when `ENTRA_TENANT_ID_RISK` is set — no code change needed.

In both cases: a valid token from the wrong IdP boundary must not grant access — the real-world
version of the single-issuer limitation tracked in `TESTING.md` (row X.3).

## Directory

```
idp/
  providers/
    base.py          — TokenProvider ABC
    factory.py       — select_provider(personas) — AUTH_PROVIDER dispatch
    keycloak.py       — password-grant token acquisition
    entra.py          — password / client_credentials / external_token flows
  fixtures/tokens.py — session-scoped token fixtures per persona
  personas.yaml      — alice, bob, charlie, root
  helpers.py         — assert_authenticated, assert_principal_visible, assert_rejected_401, assert_discovery_advertised
  test_inbound_auth.py — @pytest.mark.realidp tests 2.38–2.42 (provider-agnostic)
  README.md          — this file
```
