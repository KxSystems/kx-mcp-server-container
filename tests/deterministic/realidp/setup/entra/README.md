# Entra ID — Real-IdP Infra

The provider-agnostic tests live at `tests/deterministic/realidp/idp/test_inbound_auth.py`.
Running them with `AUTH_PROVIDER=entra` exercises the Entra path.

## What is an app registration?

An app registration is the binding between your Entra tenant and this test harness.
It tells Entra that `kdbai-service-mcp-agent` exists and shapes the tokens issued for
it — the `aud` claim (`api://<client-id>`, or the bare client ID under access-token
v2) that the container's `JWTVerifier` accepts, and that group Object IDs should be
embedded in access tokens (`groupMembershipClaims`). The registration also has a
runtime counterpart — a **service principal** (Entra ID → Enterprise applications) —
which is what actually issues sign-ins; `entra_setup.py` creates it explicitly via
Graph, since creating an app registration programmatically doesn't auto-create one
the way the portal does.

`entra_setup.py` reuses one app registration for two roles: provisioning (calling
Graph as an admin identity) and runtime (the audience tokens are validated against).
The Graph permissions below are only needed for the first role and can be removed
once provisioning is complete — see [App registration](#app-registration-one-time-manual).

## App registration (one-time, manual)

You need an Entra tenant where you're **Global Administrator** or **Application
Administrator** — a free one comes with the [Microsoft 365 Developer
Program](https://developer.microsoft.com/en-us/microsoft-365/dev-program).

1. In the Azure portal ([entra.microsoft.com](https://entra.microsoft.com) → **App
   registrations** → **+ New registration**), create an app registration named
   `kdbai-service-mcp-agent` (single tenant; redirect URI can be left blank —
   `entra_setup.py` adds it later).
2. **Certificates & secrets** → **+ New client secret** → copy the secret **Value**
   immediately (it's shown only once). This is the bootstrap credential the script
   uses to authenticate to Graph on every subsequent run.
3. **API permissions** → **+ Add a permission** → **Microsoft Graph** →
   **Application permissions** → add, then **Grant admin consent**:
   - `Application.ReadWrite.All`
   - `Group.ReadWrite.All`
   - `User.ReadWrite.All`
   - `Policy.ReadWrite.ApplicationConfiguration`
   - `Policy.Read.All`
   - `Organization.Read.All`

   These are only needed to run `entra_setup.py`. They can be removed once provisioning
   is complete.
4. **Security Defaults, if this is a new tenant.** New Entra tenants enforce MFA via
   Security Defaults, which blocks the ROPC (password) flow the test personas use to
   get tokens (`AADSTS50076`/`50079` if you skip this). Entra ID → **Overview** →
   **Properties** tab → **Manage security defaults** → **Disabled** (reason: "My
   organization is using Conditional Access"). An established tenant with Conditional
   Access policies already in place may not need this — check with whoever administers it.

Everything else — service principal, API scope/audience, access-token version,
redirect URI, groups claim, **public-client (ROPC) sign-in**, further client secrets,
groups, users, memberships, token lifetime policy — is provisioned by `entra_setup.py`
idempotently. Re-run it safely at any time, including after creating the app manually
above (it fixes up settings on an existing registration, not just at creation).

## Quick start

**One-time, unavoidably manual:** creating the initial app registration, granting it
Graph permissions, and minting a first client secret — the script authenticates *to*
Graph using that secret, so it can't create its own starting credential. Everything
else (service principal, API scope, redirect URI, groups claim, ROPC/public-client
flow, further secrets, groups, users, memberships, token lifetime policy) is scripted
and idempotent. Details: [App registration](#app-registration-one-time-manual).

### First-time setup

```bash
# 1. One-time manual bootstrap in the Azure portal — see "App registration" above.
#    Produces: tenant ID, client ID, an initial client secret, admin-consented
#    Graph permissions, and (if your tenant enforces it) Security Defaults disabled.

# 2. Copy the example env file
cp tests/deterministic/realidp/envs/.env.entra.example \
   tests/deterministic/realidp/envs/.env.entra
```

**3. Stop here and edit `.env.entra` before continuing.** The file you just copied has every
value blank — `entra_setup.py` will fail partway through (after the idempotent app/service-principal/
group steps, which need no input) with an error like `ERROR: ENTRA_PASSWORD_ALICE is not set`
if you skip this. Fill in:

| Variable | Where it comes from |
|---|---|
| `ENTRA_TENANT_ID` | Directory ID from the app registration Overview page |
| `ENTRA_CLIENT_ID` | Application ID of `kdbai-service-mcp-agent` |
| `ENTRA_CLIENT_SECRET` | the secret Value from step 1 (the Azure portal bootstrap) |
| `ENTRA_PASSWORD_ROOT`, `ENTRA_PASSWORD_ALICE`, `ENTRA_PASSWORD_BOB` | pick any values — **8+ chars, upper+lower+digit+symbol** (Entra rejects weaker passwords) |

`ENTRA_PASSWORD_CHARLIE` (further down the file) is only needed if you're also provisioning the
second-tenant wrong-issuer persona — leave it blank otherwise.

```bash
# 4. Source the file and run the provisioner
source tests/deterministic/realidp/envs/.env.entra
uv run python tests/deterministic/realidp/setup/entra/entra_setup.py
# Writes ENTRA_DOMAIN, group Object IDs, and KX_MCP_AUTH_* back into .env.entra.
# Idempotent — if it fails partway (e.g. a password was still blank), fix the file, re-source,
# and re-run; already-provisioned resources are detected and skipped.

# 5. Confirm the exact iss/aud by decoding a real token (important — v1/v2 can differ)
#    The provisioner prints a sample curl + introspect command. Run it, then update
#    KX_MCP_AUTH_ISSUER / KX_MCP_AUTH_AUDIENCE in .env.entra to match the decoded claims.
uv run kx auth introspect <token> --json
```

Provisioning is now done. To run the tests, see [`idp/README.md`](../../idp/README.md#quick-start).

**Returning operators:** skip steps 1-2 — `entra_setup.py` preserves your existing
`.env.entra` values (inputs are read from `os.environ` first, then the existing file).
Just `source` and re-run.

For expected test output and the wrong-issuer/second-tenant persona, see
[`idp/README.md`](../../idp/README.md).

## v1 vs v2 issuer / audience

This is the most common bring-up issue. Entra issues either v1 or v2 tokens depending on
`accessTokenAcceptedVersion` in the app manifest:

| Version | `iss` | `aud` |
|---------|-------|-------|
| v2 (set by `entra_setup.py`) | `https://login.microsoftonline.com/<tid>/v2.0` | `<client-id>` (bare GUID) |
| v1 (legacy default) | `https://sts.windows.net/<tid>/` | `api://<client-id>` |

`entra_setup.py` sets `requestedAccessTokenVersion=2`, so new registrations should produce v2
tokens. **Always confirm by introspecting a real token** and setting `KX_MCP_AUTH_ISSUER` /
`KX_MCP_AUTH_AUDIENCE` in `.env.entra` to match exactly. A mismatch causes a 401 at the
container's `JWTVerifier`.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `AADSTS50076`/`50079` MFA required | Disable Security Defaults — step 4 above |
| `AADSTS7000218` public client not allowed | Re-run `entra_setup.py` — `configure_public_client_flow` fixes this up on every run |
| `AADSTS50126` invalid username or password | Password doesn't meet Entra's complexity rules (8+ chars, upper+lower+digit+symbol), or is stale — re-export the `ENTRA_PASSWORD_*` var and re-run |
| `aud` mismatch / container 401 | Confirm the token's exact `iss`/`aud` — see [v1 vs v2](#v1-vs-v2-issuer--audience) above — and update `KX_MCP_AUTH_*` in `.env.entra` |
| `groups` claim missing from the token | Re-run `entra_setup.py` — `configure_optional_claims` re-applies it |
| Token lifetime policy 403/404 in the script's output | Requires Azure AD Premium P1; safe to ignore — Entra's default lifetime (~60-90 min) applies |
| Graph 404 on group membership, then succeeds on retry | Expected — `add_user_to_group` already retries through Entra's replication lag |
| `ENTRA_CLIENT_SECRET` blank after a re-run | Graph never returns an existing secret's value again; the script only prints one on the run that creates it. Reuse your saved value, or delete the `fastmcp-auto-generated` secret in the portal and re-run to mint a fresh one |
| `kx auth introspect` errors `introspect requires KX_MCP_AUTH=static or jwks (got 'unset')` | You haven't `source`d `.env.entra` in this shell (or it predates this var) — it sets `KX_MCP_AUTH="jwks"` alongside the `KX_MCP_AUTH_*` vars. The test harness itself doesn't need this (`_spawn_container` sets it directly on the subprocess); it's only for running `introspect` by hand |

## Wrong-issuer / second-tenant persona

Provisioning a second tenant to activate the wrong-issuer persona test (2.41) sets
`ENTRA_TENANT_ID_RISK` / `ENTRA_DOMAIN_RISK` / `ENTRA_PASSWORD_CHARLIE` in `.env.entra` —
see [`idp/README.md`](../../idp/README.md) for what that test does and when it's skipped
vs active.

## Entra as IdP for the KDB.AI hop — the outbound lane

The second half of the cross-IdP gate: thread one Entra token agent → container → KDB.AI in
`passthrough`, and prove **alice vs bob get different ACL results purely off the Entra-propagated
`tid`/`groups`**. The KDB.AI passthrough code is IdP-agnostic (already proven under Keycloak), so
this is pure wiring: a kdbai-db that trusts Entra + grants keyed on Entra claims.

**Single-audience passthrough.** Under passthrough the inbound token is forwarded *unchanged*, so
kdbai-db validates the *same* `aud` as the container — its `OAUTH_CLIENT_ID` is the bare-GUID
`ENTRA_CLIENT_ID` of the same `kdbai-service-mcp-agent` app. No separate resource app is needed.

**Bridged networking.** Entra's issuer/JWKS are public URLs, so kdbai-db needs no `network_mode:
host` (the Keycloak Linux-ism) — `setup/entra/docker-compose.yaml` maps ports explicitly, making
this a portable/CI-friendly lane.

**Single tenant.** With one Entra tenant, alice/bob differ purely by `groups` Object ID (same
`tid`). root is system_admin via the `manager-admin` group OID, so the KA.5 "manager container"
collapses to the standard container (the conftest fixture handles this automatically).

### Bring-up (live)

```bash
# 0. (one-time) provision the tenant — see "Quick start" above (entra_setup.py)
docker login registry.gitlab.com

# 1. Bring up kdbai-db trusting Entra (no local IdP service)
cd tests/deterministic/realidp/setup/entra
mkdir -p kdbai-data acl-data && chmod 777 kdbai-data acl-data
set -a; source ../../envs/.env.entra; set +a
KDB_LICENSE_B64=$(base64 < ~/.kx/kc.lic) docker compose up -d
cd -

# 2. Confirm the token shape (the v1/v2 landmine) BEFORE seeding — verify aud=<bare GUID>,
#    tid present, groups carries the quants-trader/viewer Object IDs:
uv run kx auth introspect <alice-token> --json

# 3. Seed databases + Entra-claim-shaped grants (root via Entra ROPC)
source tests/deterministic/realidp/envs/.env.entra
uv run python tests/deterministic/realidp/setup/entra/seed.py

# 4. Run KA.1–KA.7 under Entra
just test-kdbai-entra
```

Expected: alice (trader+viewer OIDs) sees `T1`; bob (viewer OID only) sees `[]` (KA.2 headline);
query/table_info permitted/denied (KA.3/KA.4); root system_admin via the manager-admin OID (KA.5);
table-scoped no-bleed (KA.6); no-bearer → 401 (KA.7). All off Entra-propagated `tid`/`groups`.
