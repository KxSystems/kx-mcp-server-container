# KDB.AI OAuth ACL lane (KA.1–KA.9)

Tests the kdbai-db ACL enforcing off the propagated `tenant`/`groups` claims — the container in
`passthrough` mode threads the inbound bearer directly to kdbai-db as the qipc connection
credential. KA.1–KA.7 are **IdP-agnostic**: the same assertions run under either Keycloak or
Microsoft Entra ID; the provider is selected by `AUTH_PROVIDER` in the env file. KA.8/KA.9 exercise
the `service_account` outbound strategy instead of passthrough (the container's own
client-credentials machine identity, not the caller's) — **Keycloak-only for now**; no Entra-side
service client has been provisioned, so `kdbai_service_account_container_url` skips under
`AUTH_PROVIDER=entra`.

| Marker | Command | IdP | Infra |
|---|---|---|---|
| `@pytest.mark.realidp` + `@pytest.mark.kdbai` | `just test-kdbai` | Keycloak (local) | Keycloak + Postgres + registry-gated (portal.dl.kx.com) `kdbai-db` + `seed.py` |
| `@pytest.mark.realidp` + `@pytest.mark.kdbai` | `just test-kdbai-entra` | Entra ID (public) | registry-gated (portal.dl.kx.com) `kdbai-db` only + `seed.py` |

Inbound-auth tests (2.38–2.41) live at `idp/test_inbound_auth.py` and run under
`just test-keycloak` / `just test-entra`.

---

## Quick start — Keycloak

**Prerequisites:** `docker login portal.dl.kx.com -u <portal-email> -p <bearer-token>` (the
`kdbai-db` image is registry-gated there — sign in at portal.dl.kx.com, generate a bearer token
under your username → Token Management; see
[the KDB.AI docs](https://code.kx.com/kdbai/latest/gettingStarted/kdb-ai-server-setup.html#get-the-kdbai-docker-image))
and a KDB-X license (`KDB_LICENSE_B64`).

All commands below run from the **repo root** (the workspace root — where the `.justfile` with
`just test-kdbai` and friends lives).

```bash
cd /path/to/kx-mcp-server-container   # if you aren't already there

# 1. Create data directories (gitignored — kdbai-db runs as 'nobody')
mkdir -p tests/deterministic/realidp/setup/keycloak/kdbai-data \
         tests/deterministic/realidp/setup/keycloak/acl-data
chmod 777 tests/deterministic/realidp/setup/keycloak/kdbai-data \
          tests/deterministic/realidp/setup/keycloak/acl-data

# 2. Start Keycloak + Postgres + kdbai-db (profile-gated)
KDB_LICENSE_B64=$(base64 < ~/.kx/kc.lic) \
  docker compose -f tests/deterministic/realidp/setup/keycloak/docker-compose.yaml \
  --profile backends up -d

# 3. Provision Keycloak (idempotent)
uv run python tests/deterministic/realidp/setup/keycloak/keycloak_setup.py \
    tests/deterministic/realidp/setup/keycloak/keycloak_config.json

# 4. Seed databases, tables, and grants
uv run python tests/deterministic/realidp/setup/keycloak/seed.py

# 5. Configure env
cp tests/deterministic/realidp/envs/.env.kdbai.example \
   tests/deterministic/realidp/envs/.env.kdbai
# Fill in KC_BASE, KC_CLIENT_ID (default kdbai-service), KDBAI_DB_HOST/PORT

# 6. Run
just test-kdbai
```

---

## Quick start — Entra ID

**Prerequisites:** `docker login portal.dl.kx.com -u <portal-email> -p <bearer-token>` (the
`kdbai-db` image is registry-gated there — see
[the KDB.AI docs](https://code.kx.com/kdbai/latest/gettingStarted/kdb-ai-server-setup.html#get-the-kdbai-docker-image)),
a KDB-X license, and a completed `entra_setup.py` run (see
[`setup/entra/README.md`](../setup/entra/README.md#quick-start) for first-time setup) — that run
is what populates `envs/.env.entra`, which step 2 below sources. **Before running it, you must
manually fill in `ENTRA_TENANT_ID`/`ENTRA_CLIENT_ID`/`ENTRA_CLIENT_SECRET` and the three
`ENTRA_PASSWORD_*` vars** in `.env.entra` — `entra_setup.py` only writes *back* the derived
values (domain, group IDs, `KX_MCP_AUTH_*`), it doesn't invent the inputs. Skipping this fails
partway through with `ERROR: ENTRA_PASSWORD_ALICE is not set` (or similar). If you haven't done
this yet, go do it first; otherwise step 2 below will fail with "file not found" (no file to
source at all).

All commands below run from the **repo root**, same as the Keycloak lane above.

```bash
cd /path/to/kx-mcp-server-container   # if you aren't already there

# 1. Create data directories
mkdir -p tests/deterministic/realidp/setup/entra/kdbai-data \
         tests/deterministic/realidp/setup/entra/acl-data
chmod 777 tests/deterministic/realidp/setup/entra/kdbai-data \
          tests/deterministic/realidp/setup/entra/acl-data

# 2. Start kdbai-db (no local IdP — Entra is the public IdP)
cd tests/deterministic/realidp/setup/entra
set -a; source ../../envs/.env.entra; set +a
KDB_LICENSE_B64=$(base64 < ~/.kx/kc.lic) docker compose up -d
cd -

# 3. Seed databases, tables, and grants (root authenticates via Entra ROPC)
source tests/deterministic/realidp/envs/.env.entra
uv run python tests/deterministic/realidp/setup/entra/seed.py

# 4. Run
just test-kdbai-entra
```

The Entra compose uses **bridged networking** (explicit `ports:` map). Entra's issuer and JWKS
are public URLs (`login.microsoftonline.com`) reachable from inside the container, so no
`network_mode: host` is needed — unlike the Keycloak lane. This is the CI-portable shape.

---

## What `seed.py` creates

Both seeds create the same logical schema:

- `db_read` database + `T1` table — grant: DB-level read for the **trader** group/role
- `db_read_isolated` database + `T1` + `T2` tables — grant: table-level read on `T1` only for the **viewer** group/role
- `db_read` database — grant: DB-level read for the **service** group/role (Keycloak only, KA.8/KA.9) —
  a grant of its own, distinct from `trader`'s, so the service_account tests can't pass by
  accidentally inheriting a human's ACL

The grant keys differ by provider:

| Provider | tenant key | groups key |
|---|---|---|
| Keycloak | Keycloak realm name (`quants`) | group name string (`trader`, `viewer`, `service`) |
| Entra | tenant UUID (`ENTRA_TENANT_ID`) | group Object-ID GUID (`ENTRA_GROUP_QUANTS_TRADER`, …) |

Seeds are idempotent — re-running is safe. ACL grants are persisted in the `acl-data/` volume.

---

## Personas

Defined in `idp/personas.yaml` (shared with the inbound-auth suite).

### Keycloak

| Persona | Realm | Groups | Password |
|---|---|---|---|
| alice | quants | [trader, viewer] | alice123 |
| bob | quants | [viewer] | bob123 |
| charlie | risk | [viewer] | charlie123 |
| root | manager | [admin] | root123 |

KA.5 uses a second MCP container pointed at the manager-realm JWKS so root's token passes the
inbound gate.

**Not a human persona — a machine identity (KA.8/KA.9):** `kdbai-service-worker` is a second,
confidential `quants` client (defined in `keycloak_config.json`'s `service_client` block,
distinct from the human `kdbai-service` client) with `serviceAccountsEnabled`. Keycloak
auto-creates its service-account user (`service-account-kdbai-service-worker`), which
`keycloak_setup.py` adds to the `service` group. Its client_credentials token carries
`groups:["service"]` / `tenant:"quants"` — plus an *extra* audience mapper (`audience_override`
in the config) so its `aud` also includes `kdbai-service`, the audience kdbai-db's
`OAUTH_CLIENT_ID` actually trusts (its own client_id, `kdbai-service-worker`, would otherwise be
rejected).

### Entra ID

| Persona | Tenant | Groups (Object IDs) | Notes |
|---|---|---|---|
| alice | `ENTRA_TENANT_ID` | `ENTRA_GROUP_QUANTS_TRADER` + `ENTRA_GROUP_QUANTS_VIEWER` | sees `T1` in `db_read` |
| bob | `ENTRA_TENANT_ID` | `ENTRA_GROUP_QUANTS_VIEWER` | sees `[]` (KA.2 headline) |
| root | `ENTRA_TENANT_ID` | `ENTRA_GROUP_MANAGER_ADMIN` | system_admin (bypasses ACL) |
| charlie | second tenant | — | wrong-issuer persona; **skipped** unless `ENTRA_TENANT_ID_RISK` is set |

Under Entra, alice/bob differ purely by `groups` Object IDs (same `tid`). KA.5's manager
container collapses to the standard container (root's token has the same issuer as alice's).

Grant differentiation for both providers: alice (trader role) sees `T1` in `db_read`; bob
(viewer only) sees `[]`.

---

## Issuer-matching

**Keycloak (local):** kdbai-db runs with `network_mode: host` on Linux so the container sees
`localhost:8080` as the Keycloak issuer — matching the URL carried in tokens. For macOS/colima,
replace with explicit port-maps and set `OAUTH_ISSUERS` to `host.docker.internal`.

**Entra ID (public):** no issuer-matching issue — `login.microsoftonline.com` is reachable from
inside a bridged container without any special networking.

---

## Write/delete gap (AU-W/AU-D)

The kdbai bundle exposes only read tools — no create/insert/update/delete/drop. Write and delete
ACL rows (AU-W / AU-D) are a documented gap blocked on adding write tools to the kdbai bundle.

---

## Why session-scoped fixtures?

Keycloak startup is ~30 s; kdbai-db startup is ~10–15 s. One session-scoped container shared
across all KA tests is the right trade-off over per-test isolation here.
