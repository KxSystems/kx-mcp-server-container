# Inbound-auth lane (2.38–2.42 provider-agnostic, plus 2.51–2.53 Keycloak-only)

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

Under `just test-keycloak` only, three more tests run — the `oidc_proxy` code-flow lane (§ below),
skipped wholesale under `just test-entra`:

```
test_authorization_code_flow_yields_a_working_bearer            PASSED
test_issued_bearer_is_the_containers_not_keycloaks               PASSED
test_password_grant_keycloak_token_is_rejected_in_proxy_mode      PASSED
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

## `oidc_proxy` authorization-code flow (2.51–2.53, Keycloak-only)

`test_inbound_auth.py` above mints tokens by **password grant (ROPC)** — that never reaches
`KX_MCP_AUTH=oidc_proxy`, because in that mode `/token` hands the client a bearer the *container*
mints, while the real Keycloak token stays server-side. Only a real browser-shape
authorization-code flow exercises it, so `test_oidc_proxy_code_flow.py` drives one for real:
DCR at the container → `/authorize` → the container's own consent page → a real Keycloak login
form → `/auth/callback` → `/token` (`oidc_proxy_driver.py`).

**Why Keycloak-only:** the driver scrapes Keycloak's rendered login form; there is no Entra
equivalent in this harness. The module is skipped wholesale under `AUTH_PROVIDER=entra`.

**No extra step — the Quick start above already provisions it.** Step 2
(`keycloak_setup.py keycloak_config.json`) now also creates the `kx-mcp-proxy` confidential
client (`tenants.quants.proxy_client` in `keycloak_config.json`) —
`directAccessGrantsEnabled: false` (deliberately: this client *cannot* password-grant, so the
code flow is the only way through it) — with a redirect URI exact-matching the fixed port the
container spawns on. `just test-keycloak` (step 4) already collects
`test_oidc_proxy_code_flow.py` alongside `test_inbound_auth.py`, so running the Quick start runs
both.

The one thing to know if you ever change it: the port is **fixed**, not auto-selected
(`KC_PROXY_PORT`, default `8765`) — the upstream redirect URI
`{KX_MCP_AUTH_RESOURCE_URL}/auth/callback` is pre-registered byte-exact at Keycloak, so it must be
knowable before the container spawns. Changing `KC_PROXY_PORT` means editing
`proxy_client.redirect_uris` in `keycloak_config.json` and re-running `keycloak_setup.py` — safe
to re-run any time, even with no port change, since `ensure_client` now reconciles a drifted
`redirectUris`/`secret` back to the declared config instead of leaving a stale client in place.

| Test | Row | What it proves |
|---|---|---|
| `test_authorization_code_flow_yields_a_working_bearer` | 2.51 | The full flow, against a live Keycloak, yields a bearer the container accepts on a real tool call |
| `test_issued_bearer_is_the_containers_not_keycloaks` | 2.52 | The bearer is container-minted (`alg=HS256`, `iss`/`aud` name the container) — not Keycloak's `RS256` token |
| `test_password_grant_keycloak_token_is_rejected_in_proxy_mode` | 2.53 | The same `alice` token that passes 2.38 under `jwks` is rejected here — the mode's defining behaviour |

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
  oidc_proxy_driver.py — drives a real authorization-code flow against KX_MCP_AUTH=oidc_proxy
  test_oidc_proxy_code_flow.py — @pytest.mark.realidp tests 2.51–2.53 (Keycloak-only)
  README.md          — this file
```
