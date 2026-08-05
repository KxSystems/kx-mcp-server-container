# Testing — KX MCP Container

One doc, three parts: **(1)** a concern-based map for "is X tested?", **(2)** the structure guide
for "how is the suite built, how do I run it?", **(3)** the row-by-row coverage tracker.

---

# Part 1 — What's tested, at a glance

The container has three jobs: inbound authentication, the Subject/Action/Resource (S/A/R)
authorization seam, and outbound identity propagation to each backend:

| Concern | What it proves | Primary tests |
|---|---|---|
| **INBOUND** | the `KX_MCP_AUTH` verifier validates every bearer before any backend runs (issuer/audience/scope/expiry/signature/kid), sets the principal contextvar, audits the dispatch | `unit/test_auth.py`, `integration/test_auth_integration.py`, `realidp/idp/` → [Part 3 § Inbound auth](#inbound--inbound-auth-verifier-seam) |
| **AUTHZ (S/A/R)** | a dispatch is allowed/denied by principal × action; the decision + adapter are audited | `unit/test_authorize.py`, `integration/test_authz_integration.py`, kdbx `test_authz_kx_rbac.py`, `realidp/kdbai/` → [Part 3 § Authorization](#authz--sar-seam--per-backend-authz) |
| **OUTBOUND** | the validated identity is propagated to the backend (`passthrough`/`rfc_8693`/`service_account`; identity assertion to plain kdb+) | kx-auth-core `test_outbound.py`, `integration/test_outbound_integration.py`, `realidp/{kdbai,kdbx}/` → [Part 3 § Outbound propagation](#outbound--oauth-backend-identity-propagation) / [§ Identity assertion](#outbound--identity-assertion-to-plain-kdb-x) |
| **Cross-cutting** | container shape, logging, packaging-deps, CLI contract | → [Part 3 § Container shape](#container-shape--extension-contract) / [§ Cross-cutting](#cross-cutting--structural) |

Part 3 groups the same rows by *concern* rather than by when the work landed. Details:

**INBOUND** — validate the bearer before any backend runs
- verifier modes (`unset` / `static` / `jwks`), principal contextvar, audit middleware
- RFC 9728 discovery; real-IdP acceptance/rejection (Keycloak/Entra, `realidp/idp/`)

**AUTHZ (S/A/R)** — allow/deny a dispatch by principal × action
- the capability-check decorator (`@authorize`, PEP-1) + the `static` and `kdbx_rbac` adapters
- audit line's `decision` / `adapter` fields; KDB.AI OAuth ACL (`realidp/kdbai/`)

**OUTBOUND** — propagate identity out to backends
- token-exchange seam: `passthrough` / `rfc_8693` / `service_account`
- identity assertion to plain kdb+ (ferry-shape dict, q-side promotion) — live-proven end-to-end
  by `realidp/kdbx/` (real q + real Keycloak)
- KDB.AI live propagation (`realidp/kdbai/`)

The inbound flow, which Part 2 details most heavily:

```
Client --[Bearer JWT]--> [ Parent: KX_MCP_AUTH verifier ] --> [ Mounted bundle: kdbx / kdbai / example ]
                                        |
                               rejects with 401 if invalid
                               sets AccessToken contextvar if valid
                                        |
                               AuditMiddleware logs: subject / action / outcome
```

Inbound breaks into three orthogonal sub-concerns:

| Concern | What it means | Where tested |
|---|---|---|
| **Verifier logic** | JWT checks (issuer, audience, scope, expiry, signature, kid) | `deterministic/unit/test_auth.py` — in-process |
| **Transport wiring** | FastMCP HTTP enforces the verifier; principal contextvar reaches mounted tools | `deterministic/integration/test_auth_integration.py` — real subprocess |
| **CLI contract** | `kx auth introspect` (joserfc) agrees with the container's `JWTVerifier` | `deterministic/unit/test_auth_cli_contract.py` — in-process |

---

# Part 2 — How the suite works

## Test tiers

Everything under `tests/deterministic/` is an exact pass/fail assertion. That includes the
real-IdP tests — their *assertion kind* is still deterministic even though they need live infra.

### Sub-tiers

| Sub-tier | Infra | CI? | Key files |
|---|---|---|---|
| **unit** | in-process, no subprocess | yes | `unit/test_auth.py`, `test_auth_cli_contract.py`, `test_authorize.py`, `test_audit_shape.py`, `test_composition.py`, `test_launcher.py`, `test_logging.py`, `test_packaging_deps.py` |
| **integration** | real subprocess, license-free | yes | `integration/test_auth_integration.py`, `test_authz_integration.py`, `test_audit_logging.py`, `test_outbound_integration.py`, `test_stdio_smoke.py` |
| **realidp** | live Keycloak/Entra; kdbai lane also needs registry-gated `kdbai-db`; kdbx lane also spawns its own throwaway real `q` process | **no** (`-m 'not realidp'`) | `realidp/idp/test_inbound_auth.py` (inbound auth, provider-agnostic), `realidp/kdbai/test_kdbai_acl.py` (OAuth ACL, KA), `realidp/kdbx/test_kdbx_ferry.py` (ferry live, real q) |

---

## Shared fixtures

**`conftest.py` (repo root)** — crypto fixtures used by every test tree:

| Fixture | Scope | What it gives |
|---|---|---|
| `keypair` | session | RSA-2048 private+public PEM pair, generated once per run |
| `mint` | function | `mint(**kwargs) -> JWT string` — accepts `exp_delta`, `priv`, `scope`, `client_id`, `aud`, `iss` |
| `other_priv` | function | A second private key the verifier does NOT trust (bad-signature cases) |
| `jwks_uri` | function | In-process `ThreadingHTTPServer` serving the keypair's public key as a JWKS; no external IdP needed |
| `mock_sts` | function | In-process OAuth token endpoint — yields `(token_url, captured)` where `captured` holds posted form fields |

Constants `KID = "test-key-1"`, `ISSUER = "https://issuer.test"`, `AUDIENCE = "kx-mcp"` are module-level.

**`tests/conftest.py`** — subprocess spawn harness:

| Fixture | Scope | What it gives |
|---|---|---|
| `spawn_container` | function | `start(bundles="example", **env_overrides) -> (url, proc)` — spawns the launcher as a real `streamable-http` child on a free port; terminates on teardown. `bundles=` selects the bundle. |

---

## Unit + integration tests

### `deterministic/unit/test_auth.py`

In-process verifier tests — no HTTP, no subprocess.

| Section | What it proves |
|---|---|
| **unset mode** | No provider built; tool call succeeds without a token; `current_principal()` returns None |
| **static mode** | Valid token accepted; expired/bad-sig/wrong-issuer/wrong-audience/missing-scope all rejected; PEM loaded from path |
| **jwks mode** | Same acceptance/rejection rules on the JWKS code path; kid mismatch rejected |
| **pluggable seam** | Unknown mode raises `ValueError`; new modes register via `register_auth_mode` without touching dispatch |
| **config binding** | `KX_MCP_AUTH` is the bare mode var; empty/whitespace collapses safely to `unset` |
| **audit middleware** | All three action kinds (`tool_invoke`, `resource_read`, `prompt_get`) emit correct audit records; `outcome=error` covered; `audit=False` suppresses middleware |

### `deterministic/integration/test_auth_integration.py`

Real subprocess over HTTP — catches auth wiring and contextvar propagation bugs that in-process tests can't.

| Test | What it proves |
|---|---|
| `test_valid_bearer_reaches_mounted_tool` | Happy path: valid static bearer → tool reachable |
| `test_authenticated_principal_visible_in_mounted_tool` | **Core assumption for outbound propagation and authorization** — `AccessToken` contextvar crosses the mount boundary; `example_whoami` returns `"alice"` not `"anonymous"` |
| `test_missing_bearer_is_rejected_401` | No bearer → 401 before any backend runs |
| `test_expired_bearer_is_rejected_401` | Expired token → 401 |
| `test_wrong_audience_is_rejected_401` | Wrong `aud` claim → 401 |
| `test_missing_required_scope_is_rejected` | Token lacks required scope → 4xx |
| `test_jwks_valid_bearer_reaches_mounted_tool` | JWKS mode end-to-end: subprocess fetches the in-process JWKS server, validates, tool runs |
| `test_unset_auth_no_bearer_works` | `KX_MCP_AUTH=""` → tool runs without any bearer (single-principal / bundling posture) |

### `deterministic/unit/test_auth_cli_contract.py`

Runs the same token through both `kx_auth_core.verify_token` (joserfc, used by `kx auth introspect`) and FastMCP's `JWTVerifier` (used by the container). If their semantics diverge, this fails before the CLI silently disagrees with what the container enforces. The underlying `verify_token` unit suite lives in `packages/kx-auth-core/tests/test_verify.py` (static + jwks accept/reject paths); the CLI-facing exit-code/`--json` contract for `kx auth introspect` is `packages/kx-auth-cli/tests/test_introspect_cli.py`.

### `deterministic/unit/test_logging.py` / `test_packaging_deps.py`

Two structural regressions, not auth logic: `test_logging.py` locks in the `configure_logging` brand filter (admits the `kx_mcp*` container and bundle-sibling logger trees, rejects third-party loggers, idempotent). `test_packaging_deps.py` AST-scans every workspace package's `src/` and asserts each first-party cross-package import is declared in that package's `pyproject.toml` — the uv workspace masks a missing declaration in dev; a Nexus-installed wheel would `ImportError`.

### Outbound + identity-assertion coverage (package-level and integration)

- **`kx-auth-core/tests/test_outbound.py`** — all four outbound strategies, the pluggable registry, audit chain, claim decoding.
- **`kx-auth-core/tests/test_assertion.py`** — identity-assertion projection (`project_principal`/`project_from_claims`): ferry-shape wire dict, `exp` coercion, raw `claims` preserved for q-side promotion. fastmcp-free and pykx-free.
- **`kx-mcp-kdbx/tests/unit/utils/test_kdbx.py`** — the kdbx-side bind/cache wiring: `assert_identity` on/off, per-principal cache keys, claims ferried as `CharVector` not symbol (19 functions covering this plus the base connection-cache behavior).
- **`kx-auth-cli/tests/test_assert_cli.py`** — `kx auth assert` end-to-end: project-only by default, optional qIPC `--connect` bind behind the `[qipc]` extra.
- **`kx-mcp-kdbai/tests/unit/`** — `utils/test_kdbai_auth.py` (`ServiceAccountTokenManager`, `build_conn_options` qipc/REST/anonymous paths, passthrough per-principal cache — 15 functions), plus `test_kdbai_server.py`/`test_kdbai_instance_safety.py`/`test_kdbai_cli.py` (pre-flight, instance-safety, CLI parsing) and per-primitive registration tests under `tools/`, `prompts/`, `resources/`.
- **`kx-mcp-kdbx/tests/unit/`** — mirrors the kdbx `src/` layout: `addins/` (one file per tool/resource/prompt, e.g. `test_kdbx_run_sql_query.py`, `test_kdbx_sim_search.py`), `utils/` (`test_kdbx.py` above, `test_embeddings*.py`, `test_format_utils.py`), plus `test_server.py`/`test_settings.py`/`test_instance_safety.py`. (The `register_components` add-in scanner itself is tested at the container level — `deterministic/unit/test_discovery.py`, not here.)
- **`integration/test_outbound_integration.py`** — `current_principal()` crossing the mount boundary into a real `exchange()` call ([Part 3 rows 3.1b/3.2b/3.3](#outbound--oauth-backend-identity-propagation)).

### Authorization coverage — the S/A/R seam (the `@authorize` capability check, PEP-1 + per-backend adapters)

- **`kx-auth-core/tests/test_authz.py`** — the adapter registry itself: `register_authz_adapter`, boolean-first `AuthzRequest`/`AuthzDecision`, fail-closed on adapter exception, route-only (allow) when no adapter is configured.
- **`deterministic/unit/test_authorize.py`** — the `@authorize` decorator against the bundled `static` file-based adapter: route-only when authz is unset, group-based allow/deny, namespace derived from the resource prefix, malformed/empty policy files.
- **`integration/test_authz_integration.py`** — the seam over a real subprocess: granted vs. ungranted principal, the audit line's `decision`/`adapter` fields, an Entra `roles` claim as the group source.
- **`kx-mcp-kdbx/tests/unit/utils/test_authz_kx_rbac.py`** — the concrete `kdbx_rbac` adapter (`KX_MCP_AUTHZ=kdbx_rbac`): q-side allow/deny reaching the Python adapter, denial reason propagation, fail-closed on infrastructure error.
- **`deterministic/unit/test_audit_shape.py`** — the dispatch-audit line's shape is uniform across three simulated extensions (kdbx/kdbai/acme namespaces), and carries `decision`+`adapter` when a PEP-1-gated tool is allowed/denied. Complements `test_audit_logging.py`'s over-the-wire subject-population proof.

See [Part 3 § Authorization](#authz--sar-seam--per-backend-authz) (rows 5.1–5.5) for the row-by-row mapping.

---

## Real-IdP harness

**Location:** `tests/deterministic/realidp/`
Marked `@pytest.mark.realidp`. Excluded from the default run (`addopts = "-m 'not realidp'"`).
Organized into per-backend lanes; each is self-contained with its own conftest, fixtures, and tests.
Shared items at the root: `_spawn.py` (free-port + wait-until-listening) and `providers/base.py` (`TokenProvider` ABC).

**Session scope:** Keycloak startup is ~30 s; sharing one container per session is the right trade-off.

---

### `idp/` lane — provider-agnostic inbound auth

`just test-keycloak` (Keycloak) / `just test-entra` (Entra ID) — one Keycloak stack, selected via `AUTH_PROVIDER`. Lives at `deterministic/realidp/idp/` (`personas.yaml`, `fixtures/`, `providers/{keycloak.py,entra.py}`, `helpers.py`); setup scripts live at `deterministic/realidp/setup/{keycloak,entra}/`.

Real OIDC tokens via the password-grant flow; container with `KX_MCP_AUTH=jwks` pointed at live JWKS:

| Test | Row | What it proves |
|---|---|---|
| `test_valid_token_accepted` | 2.38 | Real token accepted; live JWKS fetch + RS256 validation |
| `test_principal_visible_in_mounted_tool` | 2.39 | `AccessToken` contextvar populated from a real OIDC token; `example_whoami` returns non-anonymous |
| `test_tampered_token_rejected` | 2.40 | Corrupted signature → 401 |
| `test_discovery_advertised` | 2.42a | Live RFC 9728 Protected Resource Metadata endpoint reachable, carries an `authorization_servers` entry |
| `test_wrong_issuer_token_rejected` | 2.41 | charlie's `iss=.../realms/risk` token rejected by quants-configured container |

### `kdbai/` lane — OAuth ACL persona harness

`just test-kdbai` — Keycloak + Postgres + registry-gated `kdbai-db`. Rows KA.1–KA.9.

**Personas:** alice (quants/[trader,viewer]), bob (quants/[viewer]), charlie (risk/[viewer]), root (manager/[admin]).

**OAuth ACL tests** — container in `passthrough` mode; seeded grants (`quants/trader → DB-read on db_read`; `quants/viewer → table-read on db_read_isolated/T1`):

| Test | Row | What it proves |
|---|---|---|
| `test_kdbai_bundle_mounts` | KA.1 | Bundle mounts against real OAuth kdbai-db; passthrough pre-flight passes; tools advertised |
| `test_kdbai_acl_list_tables_differentiates_personas` | KA.2 | **Headline.** alice (trader) sees T1; bob (viewer) sees []. Committed version of the 2026-06-18 live validation. |
| `test_kdbai_acl_query_data_permitted_and_denied` | KA.3 | alice succeeds; bob gets `status:error`. ACL denial → structured error (not HTTP error). |
| `test_kdbai_acl_table_info_permitted_and_denied` | KA.4 | alice gets schema; bob `status:error`. No embedding provider needed (pure metadata). |
| `test_kdbai_acl_list_databases_system_admin_only` | KA.5 | root (system_admin) sees database list and queries a quants-granted table it has no direct grant for; alice denied. Uses a **second container** (`kdbai_manager_container_url`) on the manager-realm JWKS — the main quants-only container would 401 root's manager-realm token. |
| `test_kdbai_acl_table_scoped_grant_no_bleed` | KA.6 | alice queries T1 (granted); T2 → error. Table-scoped grant does not bleed. |
| `test_kdbai_no_bearer_rejected` | KA.7 | No bearer → 401 at inbound gate. Does not require a live kdbai-db. |

**Deferred gaps:**
- **AU-W / AU-D** — write/delete ACL untestable; kdbai bundle is read-only. Blocked on adding write tools.
- **Search ACL** — `similarity_search` / `hybrid_search` embed client-side via an external embedding provider. Blocked on that dependency in the test lane.

---

### `kdbx/` lane — ferry live (real q + real Keycloak)

Setup + full walkthrough: [`realidp/kdbx/README.md`](../deterministic/realidp/kdbx/README.md).

`just test-kdbx` — the same live Keycloak `quants` realm `idp`/`kdbai` reuse; no separate managed
backend to provision — this lane spawns its own **throwaway real `q` process**
(`kdbx_ferry_host.q`, loaded with `kx.auth`) for the session. Requires a kdb-x install (`q` on
`PATH` or `~/.kx/bin/q`) with a valid license, in addition to the Keycloak stack.

**Personas:** alice (quants/[trader,viewer]), bob (quants/[viewer]) — the same personas `idp`/
`kdbai` use. Keycloak's `groups-mapper` already puts group membership in a top-level `groups`
claim, matching `kx.auth`'s default claim search — no `setClaims` call needed in the host q file.

| Test | What it proves |
|---|---|
| `test_kdbx_ferry_alice_in_trader_group_allowed` | Full chain, no mocking: container validates alice's real bearer, ferries the principal via `.kx.auth.bind` on the service-account qIPC handle, `.s.e` wrapper calls `.kx.auth.authorize[`read;`trades]` — the `trader` group grant allows it, real trade rows come back. |
| `test_kdbx_ferry_bob_without_trader_group_denied` | bob is bound (the service-account connection may assert any identity — the bind-gate gates the *caller*, not the asserted principal) but ungranted; `.kx.auth.authorize` refuses, surfaced as a structured `permission_denied` envelope, not a raw q error. |
| `test_kdbx_ferry_unbound_connection_denied_by_default` | A second raw qIPC connection (same service-account creds, `.kx.auth.bind` never called) is denied by default — proves the q module's own default-deny holds independent of the container's plumbing. |

**Gotcha this lane's `conftest.py` works around:** pytest's default `testpaths` collects every
package's unit tests, and importing `kx_mcp_kdbx.server` at collection time (from unrelated files)
sets `PYKX_LICENSED=true` and triggers `import pykx` in the pytest process *itself* — which mutates
its own `os.environ` (overwrites `QHOME`, sets `QPATH`) before any kdbx fixture ever runs. That
contamination reaches the spawned container subprocess as a spurious "no valid q license" failure
even though the license is genuinely there. `just test-kdbx` scopes collection to
`tests/deterministic/realidp/kdbx` (not just `-m kdbx`) to avoid triggering it at the source;
`_spawn.py`'s `_PYKX_KEYS_TO_STRIP` also strips `QHOME`/`QPATH`/`PYKX_DIR`/`PYKX_EXECUTABLE`/
`PYKX_UNDER_PYTHON` from every realidp container spawn as defense in depth.

---

## File map

```
conftest.py                                ← repo-root: crypto fixtures (keypair, mint, jwks_uri, mock_sts)
tests/
  conftest.py                              ← spawn harness (spawn_container)
  docs/
    TESTING.md                             ← this file (what's tested / how it works / coverage tracker)
  deterministic/
    unit/
      test_auth.py                         ← in-process verifier + audit tests
      test_auth_cli_contract.py            ← CLI ↔ container verifier drift guard
      test_authorize.py                    ← @authorize decorator + bundled `static` adapter
      test_audit_shape.py                  ← uniform dispatch-audit shape across backends
      test_composition.py                  ← assembly + mount/namespace unit tests
      test_launcher.py                     ← launcher env + config unit tests
      test_logging.py                      ← configure_logging brand filter + idempotency
      test_packaging_deps.py               ← AST-scan: first-party imports declared per package
    integration/
      conftest.py                          ← auto-marks @pytest.mark.integration
      test_auth_integration.py             ← over-the-wire subprocess integration tests
      test_audit_logging.py                ← audit-line visibility + KX_MCP_LOG_LEVEL suppression
                                             + authenticated-subject-over-the-wire
      test_authz_integration.py            ← S/A/R seam over the wire, audit decision/adapter fields
      test_stdio_smoke.py                  ← STDIO transport smoke test
      test_outbound_integration.py         ← outbound container-wiring: rows 3.1b/3.2b/3.3
    realidp/
      _spawn.py                            ← _free_port, _wait_until_listening
      envs/
        .env.keycloak / .env.kdbai         ← gitignored local config
      setup/
        keycloak/{docker-compose.yaml, keycloak_setup.py, keycloak_config.json, seed.py}
        entra/                              ← entra_setup.py + first-time provisioning
      idp/                                 ← inbound-auth harness, provider-agnostic
        personas.yaml / helpers.py
        fixtures/                          ← session-scoped persona token fixtures
        providers/{base.py, keycloak.py, entra.py}   ← TokenProvider ABC + implementations
        test_inbound_auth.py               ← @pytest.mark.realidp tests 2.38–2.42
      kdbai/                               ← OAuth ACL harness (registry-gated kdbai-db)
        conftest.py                        ← _require_idp, _spawn_container, kdbai_container_url,
                                             kdbai_manager_container_url (KA.5 second container)
        helpers.py
        test_kdbai_acl.py                  ← @pytest.mark.realidp @pytest.mark.kdbai tests KA.1–KA.9
        README.md
      kdbx/                                ← ferry live harness (spawns its own real q process)
        conftest.py                        ← kdbx_ferry_host (real q subprocess), kdbx_ferry_container_url
        kdbx_ferry_host.q                  ← kx.auth + trader×trades×read/write grant, default-deny
        test_kdbx_ferry.py                 ← @pytest.mark.realidp @pytest.mark.kdbx: alice/bob/unbound
        README.md
  fixtures/kx-mcp-example/                ← shared license-free example bundle
```

---

## Running the suite

```bash
# All deterministic tests (unit + integration, no real IdP) — default CI run
just test

# Unit tests only (fastest feedback)
uv run pytest -m "not integration and not realidp"

# Subprocess integration tests only
uv run pytest -m integration

# Inbound auth against real Keycloak (Docker required, no kdbai-db image needed)
docker compose -f tests/deterministic/realidp/setup/keycloak/docker-compose.yaml up -d
uv run python tests/deterministic/realidp/setup/keycloak/keycloak_setup.py \
    tests/deterministic/realidp/setup/keycloak/keycloak_config.json
cp tests/deterministic/realidp/envs/.env.keycloak.example \
   tests/deterministic/realidp/envs/.env.keycloak   # gitignored; defaults match this stack
source tests/deterministic/realidp/envs/.env.keycloak
just test-keycloak

# Ferry live — real q + the same Keycloak stack above (no kdbai-db needed)
# Requires a kdb-x install (`q` on PATH or ~/.kx/bin/q) with a valid license
just test-kdbx

# KDB.AI OAuth ACL (requires docker login registry.gitlab.com + KDBX license)
# All steps from the repo root — see realidp/kdbai/README.md for the full walkthrough.
# 1. mkdir -p tests/deterministic/realidp/setup/keycloak/kdbai-data \
#             tests/deterministic/realidp/setup/keycloak/acl-data
#    chmod 777 tests/deterministic/realidp/setup/keycloak/kdbai-data \
#              tests/deterministic/realidp/setup/keycloak/acl-data
# 2. KDB_LICENSE_B64=$(base64 ~/.kx/kc.lic) docker compose \
#      -f tests/deterministic/realidp/setup/keycloak/docker-compose.yaml --profile backends up -d
#    # Note: use ~/.kx/kc.lic (KDBX license) — a ~/q/kc.lic path is stale/KXAI and won't work
# 3. uv run python tests/deterministic/realidp/setup/keycloak/keycloak_setup.py \
#        tests/deterministic/realidp/setup/keycloak/keycloak_config.json
# 4. uv run python tests/deterministic/realidp/setup/keycloak/seed.py
# 5. cp tests/deterministic/realidp/envs/.env.kdbai.example \
#       tests/deterministic/realidp/envs/.env.kdbai
just test-kdbai

```

---

## Key invariants

- **`KID`, `ISSUER`, `AUDIENCE`** must stay in sync between `conftest.py` and each test file — they are stable string constants, not injectable fixtures.
- **`mint` always uses `kid="test-key-1"`**. Tests needing a different kid must encode inline with `jwt.encode(..., headers={"kid": "..."})`.
- **No pytest-asyncio** — tests wrap coroutines in `asyncio.run()` manually. Mixing asyncio.run with pytest-asyncio causes event-loop reuse issues.
- **`realidp` tests never run in CI** — enforced by `addopts = "-m 'not realidp'"` in `pyproject.toml`.

---

# Part 3 — Coverage tracker

**This is a planning/tracking artifact, not a description of how the suite is structured** (Part 2
is that description). The runnable test suite is organized by feature/concern (`test_auth.py`,
`test_composition.py`, …), split by container `tests/` vs per-package `packages/*/tests/`.
Sections below group rows by feature area and can be re-sectioned freely as scope shifts. Nothing
in the suite depends on this file.

Tracks every test that exists or should exist, by feature area. For each test:
- **Status:** `existing` (already in the suite) · `needed` (gap) · `deferred` (deliberate — not yet in scope)
- **Priority:** `critical` · `important` · `minor` — against the three aims: catch bugs, prove core functionality, anchor regressions

Keep this file updated as tests are written: flip `needed` to `existing` and note the function name.

**Markers:** real-IdP tests (Keycloak now, Entra ID later) carry `@pytest.mark.realidp` and are
excluded from the default/CI run — they are manual/local (`just test-keycloak`). This is a functional
gate (what infra the test needs), not a section marker.

---

## Container shape + extension contract

> Scope: assembly seam, bundle mount/namespace, STDIO bundling, launcher env parsing.
> All tests are in the container-level `tests/` suite using the license-free `example` fixture.

| # | Test | File | Function | Status | Priority | Notes |
|---|---|---|---|---|---|---|
| 1.1 | Container prefix `KX_MCP_BUNDLES` / `KX_MCP_TRANSPORT` bind correctly | deterministic/unit/test_launcher.py | `test_settings_parse_under_kx_mcp_env` | existing | — | |
| 1.2 | Bundle auto-registers under namespaced prefix; no cross-bundle collision | deterministic/unit/test_composition.py | `test_single_bundle_is_namespaced` / `test_multi_backend_recomposition_without_collision` | existing | — | |
| 1.3 | STDIO spawn in-process: `example_echo` reachable via FastMCP `Client` | deterministic/integration/test_stdio_smoke.py | `test_stdio_smoke_in_process` | existing | — | |
| 1.4 | STDIO spawn as real subprocess: `example_echo` reachable via `StdioTransport` | deterministic/integration/test_stdio_smoke.py | `test_stdio_smoke_subprocess` | existing | — | |

---

## INBOUND · Inbound auth verifier seam

> Scope: `kx_mcp_core/auth/` — settings, providers, principal, audit middleware.
> Unit tests in `tests/deterministic/unit/test_auth.py`; over-the-wire integration in
> `tests/deterministic/integration/test_auth_integration.py`;
> real-IdP (Keycloak/Entra) tests are `@pytest.mark.realidp`, manual/local only.

### Settings & config binding

| # | Test | File | Function | Status | Priority | Notes |
|---|---|---|---|---|---|---|
| 2.1 | `KX_MCP_AUTH` bare var binds to `mode`; `KX_MCP_AUTH_*` detail binds correctly | deterministic/unit/test_auth.py | `test_mode_binds_to_bare_kx_mcp_auth_env` | existing | — | Also proves case normalisation (`JWKS` → `jwks`) and comma-separated scope parsing |
| 2.2 | Empty `KX_MCP_AUTH` collapses to `unset` | deterministic/unit/test_auth.py | `test_empty_mode_collapses_to_unset` | existing | — | |
| 2.3 | Whitespace-only `KX_MCP_AUTH` collapses to `unset` | deterministic/unit/test_auth.py | `test_whitespace_mode_collapses_to_unset` | existing | — | |

### Providers / verifier seam (unit — verifier-level, no transport)

| # | Test | File | Function | Status | Priority | Notes |
|---|---|---|---|---|---|---|
| 2.4 | `unset` mode builds `None` provider | deterministic/unit/test_auth.py | `test_unset_builds_no_provider` | existing | — | |
| 2.5 | Unknown mode raises a clear `ValueError` | deterministic/unit/test_auth.py | `test_unknown_mode_raises` | existing | — | |
| 2.6 | Custom mode registers via `register_auth_mode` without touching dispatch | deterministic/unit/test_auth.py | `test_custom_mode_is_pluggable` | existing | — | Shape a future `proxy_headers` mode will follow |
| 2.7 | `static`: valid RS256 token accepted; claims round-trip | deterministic/unit/test_auth.py | `test_static_accepts_valid_rejects_expired_and_bad_signature` | existing | — | |
| 2.8 | `static`: expired token rejected | deterministic/unit/test_auth.py | `test_static_accepts_valid_rejects_expired_and_bad_signature` | existing | — | |
| 2.9 | `static`: bad signature (wrong key) rejected | deterministic/unit/test_auth.py | `test_static_accepts_valid_rejects_expired_and_bad_signature` | existing | — | |
| 2.10 | `static`: missing public key raises a clear error | deterministic/unit/test_auth.py | `test_static_requires_a_public_key` | existing | — | |
| 2.11 | `static`: public key loaded from file path (`KX_MCP_AUTH_PUBLIC_KEY_PATH`) | deterministic/unit/test_auth.py | `test_static_loads_public_key_from_file_path` | existing | — | |
| 2.12 | `static`: wrong issuer rejected | deterministic/unit/test_auth.py | `test_static_wrong_issuer_rejected` | existing | — | |
| 2.13 | `static`: wrong audience rejected | deterministic/unit/test_auth.py | `test_static_wrong_audience_rejected` | existing | — | |
| 2.14 | `static`: token missing a required scope rejected | deterministic/unit/test_auth.py | `test_static_missing_required_scope_rejected` | existing | — | |
| 2.15 | `jwks`: valid RS256 token accepted | deterministic/unit/test_auth.py | `test_jwks_validates_minted_token_rejects_expired_and_bad_signature` | existing | — | Against in-process JWKS server |
| 2.16 | `jwks`: expired token rejected | deterministic/unit/test_auth.py | `test_jwks_validates_minted_token_rejects_expired_and_bad_signature` | existing | — | |
| 2.17 | `jwks`: bad signature (key absent from JWKS) rejected | deterministic/unit/test_auth.py | `test_jwks_validates_minted_token_rejects_expired_and_bad_signature` | existing | — | |
| 2.18 | `jwks`: missing URI raises a clear error | deterministic/unit/test_auth.py | `test_jwks_requires_a_uri` | existing | — | |
| 2.19 | `jwks`: token with mismatched `kid` (not in JWKS) rejected | deterministic/unit/test_auth.py | `test_jwks_kid_mismatch_rejected` | existing | — | |
| 2.20 | `jwks`: wrong issuer rejected | deterministic/unit/test_auth.py | `test_jwks_wrong_issuer_rejected` | existing | — | |
| 2.21 | `jwks`: wrong audience rejected | deterministic/unit/test_auth.py | `test_jwks_wrong_audience_rejected` | existing | — | |

### Principal accessor

| # | Test | File | Function | Status | Priority | Notes |
|---|---|---|---|---|---|---|
| 2.22 | `unset` mode: tool call succeeds with no token; `current_principal()` returns `None` | deterministic/unit/test_auth.py | `test_unset_is_no_auth_and_no_principal` | existing | — | Bare FastMCP server, no parent/child composition |
| 2.23 | **Principal crosses mount boundary** — parent has auth enabled, mounted bundle tool reads `current_principal()` and sees the authenticated principal | deterministic/integration/test_auth_integration.py | `test_authenticated_principal_visible_in_mounted_tool` | existing | — | Combined with 2.32 — over-the-wire test required because in-process clients bypass JWT validation |

### Audit middleware

| # | Test | File | Function | Status | Priority | Notes |
|---|---|---|---|---|---|---|
| 2.24 | `tool_invoke`: audit log emits `subject=anonymous action=tool_invoke target=… outcome=ok` | deterministic/unit/test_auth.py | `test_audit_logs_subject_action_outcome` | existing | — | `unset` mode, anonymous subject |
| 2.25 | `tool_invoke`: audit log emits authenticated subject (`subject=alice`) when a token is present | deterministic/unit/test_auth.py | `test_audit_logs_authenticated_subject` | existing | — | Patches `current_principal` in the audit module; full JWT→subject wiring is proven by 2.23/2.32 |
| 2.26 | `tool_invoke`: audit log emits `outcome=error` when the tool raises | deterministic/unit/test_auth.py | `test_audit_logs_error_outcome` | existing | — | |
| 2.27 | `resource_read`: audit log emits the correct action and target | deterministic/unit/test_auth.py | `test_audit_logs_resource_read_action` | existing | — | |
| 2.28 | `prompt_get`: audit log emits the correct action and target | deterministic/unit/test_auth.py | `test_audit_logs_prompt_get_action` | existing | — | |
| 2.29 | `make_parent(audit=False)` suppresses audit middleware | deterministic/unit/test_auth.py | `test_make_parent_audit_false_suppresses_middleware` | existing | — | |

### Over-the-wire integration (real subprocess, `streamable-http`)

| # | Test | File | Function | Status | Priority | Notes |
|---|---|---|---|---|---|---|
| 2.30 | `static`: valid bearer reaches mounted tool, tool returns result | deterministic/integration/test_auth_integration.py | `test_valid_bearer_reaches_mounted_tool` | existing | — | |
| 2.31 | `static`: no bearer → 401 before any backend runs | deterministic/integration/test_auth_integration.py | `test_missing_bearer_is_rejected_401` | existing | — | |
| 2.32 | `static`: authenticated principal is visible inside mounted tool | deterministic/integration/test_auth_integration.py | `test_authenticated_principal_visible_in_mounted_tool` | existing | — | Combined with 2.23; calls `example_whoami` which returns `current_principal().client_id` |
| 2.33 | `jwks`: valid bearer reaches mounted tool over `streamable-http` | deterministic/integration/test_auth_integration.py | `test_jwks_valid_bearer_reaches_mounted_tool` | existing | — | Subprocess fetches JWKS from in-process `jwks_uri` fixture server on 127.0.0.1 |
| 2.34 | `static`: expired bearer → 401 | deterministic/integration/test_auth_integration.py | `test_expired_bearer_is_rejected_401` | existing | — | |
| 2.35 | `static`: bearer with wrong audience → 401 | deterministic/integration/test_auth_integration.py | `test_wrong_audience_is_rejected_401` | existing | — | |
| 2.36 | `static`: token missing required scope → rejected | deterministic/integration/test_auth_integration.py | `test_missing_required_scope_is_rejected` | existing | — | Asserts 401 or 403 (FastMCP maps scope failure to 4xx) |
| 2.37 | `unset`: no auth configured, tool call works without any bearer | deterministic/integration/test_auth_integration.py | `test_unset_auth_no_bearer_works` | existing | — | Passes `KX_MCP_AUTH=""` to neutralise any ambient env var |

### Real-IdP integration (Keycloak now, Entra ID later — `@pytest.mark.realidp`, manual/local)

> Provider abstraction + personas ported from the kdb.ai OAuth harness. Asserts **authentication
> only** here (real token accepted/rejected by the container), but structured authz-ready so the
> authorization suite (ACL grants, permitted/denied) drops in without rework.

> This lane lives at `deterministic/realidp/idp/` (provider-agnostic Keycloak/Entra harness —
> `idp/personas.yaml`, `idp/fixtures/`, `idp/providers/`, `idp/helpers.py`). The kdbai lane holds
> only the OAuth-ACL tests (KA.1–KA.9 below); setup scripts live at
> `deterministic/realidp/setup/{keycloak,entra}/`.

| # | Test | File | Function | Status | Priority | Notes |
|---|---|---|---|---|---|---|
| 2.38 | Real IdP token (password-grant) accepted by container in `jwks` mode | deterministic/realidp/idp/test_inbound_auth.py | `test_valid_token_accepted` | existing | important | Token acquired via `KeycloakTokenProvider` (now) / `EntraTokenProvider` (future); container fetches live JWKS to verify |
| 2.39 | Real IdP token propagates authenticated principal into mounted tool | deterministic/realidp/idp/test_inbound_auth.py | `test_principal_visible_in_mounted_tool` | existing | important | `example_whoami` returns non-anonymous; proves `AccessToken` contextvar from real OIDC token |
| 2.40 | Tampered real token (signature byte corrupted) rejected with 401 | deterministic/realidp/idp/test_inbound_auth.py | `test_tampered_token_rejected` | existing | important | Proves live JWKS fetch + RS256 signature check, not just format validation |
| 2.41 | Real token from wrong realm/tenant rejected by container | deterministic/realidp/idp/test_inbound_auth.py | `test_wrong_issuer_token_rejected` | existing | important | charlie's token has `iss=.../realms/risk`; Entra equivalent: second-tenant token. Issuer mismatch → 401. Provider seam (`AUTH_PROVIDER=keycloak\|entra`) proven structurally by `TokenProvider` ABC + conftest switching |
| 2.42a | Real IdP: container advertises RFC 9728 Protected Resource Metadata over the wire | deterministic/realidp/idp/test_inbound_auth.py | `test_discovery_advertised` | existing | important | Live `/.well-known/oauth-protected-resource/mcp`; `_spawn_container` sets `KX_MCP_AUTH_RESOURCE_URL` for all realidp sessions |

### Demo deliverables — inbound auth

> These are not automated tests but operator/agent-facing demo artifacts that complement the tests
> above — the tests prove the path mechanically, these demos prove it end-to-end for a human or
> an agent to follow.

| # | Deliverable | Location | Status | Notes |
|---|---|---|---|---|
| 2.42 | `manual.md` — operator runs with `KX_MCP_AUTH=jwks`, mints token against local mock-oauth2-server, calls tool, sees principal in audit log | recorded walkthrough (combined with 2.43) | existing | Satisfies both the manual and agent-facing demo requirements from one walkthrough. |
| 2.43 | `agent.md` — MCP client configured with a real bearer; tool returns with validated principal visible in response | recorded walkthrough (combined with 2.42) | existing | Same walkthrough as 2.42 — covers both the manual operator path and the agent-driven path. |
| 2.44 | Launcher surfaces audit line at INFO by default; `KX_MCP_LOG_LEVEL=WARNING` suppresses it; over the wire the line carries a real authenticated subject | `deterministic/integration/test_audit_logging.py` | `test_launcher_surfaces_audit_line_by_default` / `test_log_level_suppresses_audit_line` / `test_audit_line_carries_authenticated_subject_over_the_wire` | existing | The brand-filter/log-level mechanics behind this are unit-tested separately in `deterministic/unit/test_logging.py`. |

---

## OUTBOUND · OAuth-backend identity propagation

> Three layers, all shipped:
> - **Seam mechanics** — `test_outbound.py` (kx-auth-core) covers the strategies in-process
>   against a stdlib mock STS: `passthrough`, `rfc_8693`, `service_account`, the pluggable
>   registry, audit chain, and claim decoding.
> - **Container wiring** (rows 3.1b/3.2b/3.3) — `current_principal()` crossing the mount boundary
>   into a real `exchange()` on a dispatched tool (`test_outbound_integration.py`).
> - **Live backend** — KDB.AI passthrough and `service_account` are proven against a live IdP and
>   OAuth KDB.AI deployment (KA.1–KA.9).

| # | Test | File | Function | Status | Priority | Notes |
|---|---|---|---|---|---|---|
| 3.1a | `passthrough`: seam mechanics — bearer forwarded unchanged when audience matches; refused on mismatch | `packages/kx-auth-core/tests/test_outbound.py` | `test_passthrough_forwards_when_audience_matches`, `test_passthrough_refuses_on_audience_mismatch` | **shipped** | critical | In-process mock STS. Seam logic proven; container-wiring row is 3.1b below. |
| 3.1b | `passthrough`: `current_principal()` bearer forwarded through `exchange()` on a real dispatched tool call (cross-mount-boundary wiring) | `tests/deterministic/integration/test_outbound_integration.py` | `test_passthrough_wires_principal_through_tool` | existing | critical | |
| 3.2a | `rfc_8693`: seam mechanics — wire shape (grant type, subject token, audience, client-auth), token injection, basic/post auth modes | `packages/kx-auth-core/tests/test_outbound.py` | `test_rfc_8693_exchanges_and_injects_product_token` / `test_rfc_8693_basic_client_auth_uses_authorization_header` | **shipped** | critical | In-process mock STS. |
| 3.2b | `rfc_8693`: token exchange wired under composition — inbound principal exchanged for audience-scoped token before backend call | `tests/deterministic/integration/test_outbound_integration.py` | `test_rfc_8693_wires_exchange_through_tool` | existing | critical | |
| 3.2c | `service_account`: client-credentials grant, no subject token sent | `packages/kx-auth-core/tests/test_outbound.py` | `test_service_account_uses_client_credentials` | **shipped** | critical | KDB.AI delegates to this strategy; its token-manager lifecycle is covered by `test_kdbai_auth.py`. |
| 3.3 | Audit record shows inbound→exchange→outbound chain for one call | `tests/deterministic/integration/test_outbound_integration.py` | `test_exchange_audit_chain` | existing | important | |
| 3.4 | KDB.AI extension reaches backend with threaded principal's token | `deterministic/realidp/kdbai/test_kdbai_acl.py` | `test_kdbai_acl_list_tables_differentiates_personas` / `test_kdbai_acl_query_data_permitted_and_denied` (KA.2/KA.3) | **existing** | critical | Proven live against a real OAuth `kdbai-db` (`just test-kdbai`): the inbound bearer is forwarded via `passthrough` as the qipc credential and kdbai-db's ACL enforces on the propagated identity. See the KA.1–KA.9 sub-section. |
| 3.7 | `kx auth exchange --subject <token> --audience <aud>` returns audience-scoped token from mock STS | `packages/kx-auth-cli/tests/test_exchange_cli.py` | `test_rfc_8693_returns_audience_scoped_token`, `test_passthrough_audience_mismatch_is_denied_4`, `test_rfc_8693_missing_token_url_is_error_1`, `test_missing_subject_for_rfc_8693_is_usage_2`, `test_expired_cache_subject_is_auth_required_3`, `test_subject_falls_back_to_login_cache` | existing | critical | *(shipped in `5f6371a`)* Reuses the shared `exchange` seam. |
| 3.8 | `kx auth login` device-code happy path + endpoint-keyed token cache | `packages/kx-auth-cli/tests/test_login_cli.py` + `test_cache.py` | `test_device_code_happy_path_caches_token`, `test_dcr_registers_a_client_when_no_client_id`, `test_explicit_client_id_skips_dcr`, `test_access_denied_is_denied_4`, `test_no_device_support_is_error_1` | existing | critical | *(shipped in `5f6371a`)* CLI coverage for the outbound exchange seam. |
| 3.9 | KDB.AI `service_account`: client-credentials token minted against a **live** IdP is accepted by a real backend; result is independent of which human called | `deterministic/realidp/kdbai/test_kdbai_acl.py` | `test_kdbai_service_account_query_succeeds` / `test_kdbai_service_account_result_independent_of_caller` (KA.8/KA.9) | **existing** | important | Proven live against real Keycloak + OAuth `kdbai-db` (`just test-kdbai`): a `kdbai-service-worker` client (dedicated `quants` service client, `serviceAccountsEnabled`, its own `quants/service → db_read` grant independent of any human persona's) mints a client-credentials token kdbai-db accepts; alice/bob get an identical result, proving neither human's claims reach the backend. Verified independently outside pytest too — the minted token decodes to `groups:["service"]`/`tenant:"quants"`, and kdbai-db's `/api/v2/admin/grants` shows the `service` grant as a distinct entry from `trader`'s. |
## OUTBOUND · Identity assertion to plain kdb-x

> Coverage is split across a fastmcp-free projection layer (`kx-auth-core`) that projects the
> validated principal into a q-friendly dict, and the kdbx-side bind/cache wiring (`kx-mcp-kdbx`)
> that ferries it over qIPC and binds it via `.kx.auth.bind`; the q-side module tests live in-repo
> alongside `modules/kx/auth/init.q` per the `q` skill's conventions, not as a
> `test_kx_auth_module.py` pytest file (no pykx-free way to exercise `.kx.auth.bind` in-process).
>
> **Live-q gap closed.** This section previously noted no automated test exercised the q-side
> `.kx.auth.bind`/`authorize`/default-deny against a real q process — proven only by a manual
> walkthrough against a live q process and live Keycloak. That gap is closed:
> `deterministic/realidp/kdbx/` spawns a real `q` process and drives it through a real live
> Keycloak (`quants` realm) end-to-end. Everything else below stays mocked/in-process — this is
> the one row proving the real chain.

| # | Test | File | Function | Status | Priority | Notes |
|---|---|---|---|---|---|---|
| 4.1 | Principal claims project to the documented ferry-shape q-dict (`sub`/`scopes`/`aud`/`iss`/`act`/`exp`/raw `claims`) | `packages/kx-auth-core/tests/test_assertion.py` | `test_project_principal_core_keys_always_present`, `test_project_from_claims_extracts_standard_names`, `test_both_entry_points_agree`, `test_groups_not_promoted_in_ferry_shape`, `test_raw_claims_preserved_for_q_side_promotion` (11 functions total) | existing | critical | fastmcp-free + pykx-free projection; promotion (`groups`/`tenant` from claims, `exp` canon) is q-side per the contract, not tested here. |
| 4.2 | Bind writes the ferry-shape dict onto a cached connection, re-keyed per principal | `packages/kx-mcp-kdbx/tests/unit/utils/test_kdbx.py` | `test_assert_identity_on_binds_ferry_shape`, `test_distinct_principals_get_distinct_cache_keys` | existing | critical | `assert_identity` config flag gates the bind call. Connection + PyKX are mocked — no real q. |
| 4.3 | `assert_identity=False` (default) never reads or binds; high-cardinality claim values ferry as `CharVector`, not symbol | `packages/kx-mcp-kdbx/tests/unit/utils/test_kdbx.py` | `test_assert_identity_off_does_not_read_or_bind`, `test_assert_identity_on_without_principal_leaves_unbound`, `test_claims_string_values_charvec_not_symbol`, `test_raw_claims_ferried_for_q_side_promotion`, `test_assert_identity_defaults_false` | existing | critical | Guards a real PyKX gotcha: Python strings convert to q symbols by default, so high-cardinality claim values must be wrapped as `CharVector` to avoid interning them. |
| 4.4 | `kx auth assert` exercises the projection + optional qIPC `--connect` bind end-to-end from a shell | `packages/kx-auth-cli/tests/test_assert_cli.py` | `test_project_only_emits_wire_dict`, `test_connect_binds_and_confirms`, `test_probe_denial_maps_to_exit_4`, `test_connect_without_pykx_is_error`, `test_assert_cmd_import_is_fastmcp_free` (8 functions total) | existing | critical | `--connect` is behind the `kx-auth-cli[qipc]` extra so the base CLI stays pykx-free. `--connect` uses a fake pykx stub in tests — no real q. |
| 4.5 | Ferry live: real q loads `kx.auth`, `.kx.auth.bind` promotes an inbound identity, `.kx.auth.authorize`/`setPolicy` enforce a group×table×action grant (allow + default-deny) | `deterministic/realidp/kdbx/test_kdbx_ferry.py` | `test_kdbx_ferry_alice_in_trader_group_allowed`, `test_kdbx_ferry_bob_without_trader_group_denied`, `test_kdbx_ferry_unbound_connection_denied_by_default` | **existing** | critical | `just test-kdbx` — real live Keycloak `quants` realm (alice has `trader`, bob doesn't) + a throwaway real `q` process spawned by the test. Closes the gap above. |

---

## AUTHZ · S/A/R seam + per-backend authz

> The capability-check decorator (`@authorize`, PEP-1) and q-side `kdbx_rbac` adapter have shipped.
> **This is where the kdb.ai-style authorization suite (personas with grant levels,
> `assert_permitted`/`assert_denied`, ACL grant fixtures) becomes directly relevant** — the provider
> abstraction from the inbound-auth suite grew into it (KA.1–KA.9 below).

| # | Test | File | Function | Status | Priority | Notes |
|---|---|---|---|---|---|---|
| 5.1 | S/A/R seam allows/denies a dispatch by principal × action; records outcome + adapter name | `packages/kx-auth-core/tests/test_authz.py`, `tests/deterministic/unit/test_authorize.py` | `test_adapter_bool_true_allows_and_stamps_adapter`, `test_adapter_bool_false_denies`, `test_route_only_allows_when_authz_unset`, `test_static_denies_when_group_missing` (registry: 8 fns; decorator: 12 fns) | existing | critical | Registry (`register_authz_adapter`, boolean-first `AuthzRequest`/`AuthzDecision`) in kx-auth-core; the `@authorize` decorator + its bundled `static` file-based adapter in kx-mcp-core. |
| 5.1a | S/A/R seam wired end-to-end over the wire: granted/ungranted principal, Entra `roles` claim as the group source | `tests/deterministic/integration/test_authz_integration.py` | `test_granted_principal_is_allowed`, `test_ungranted_principal_is_denied_with_a_clean_error`, `test_entra_roles_claim_grants_pep1`, `test_entra_roles_claim_denies_pep1` | existing | critical | Real subprocess; denial surfaces as a clean structured error, not a crash. |
| 5.1b | `kdbx_rbac` adapter: q-side allow/deny decision reaches the Python adapter, denial reason propagates, infra errors fail closed | `packages/kx-mcp-kdbx/tests/unit/utils/test_authz_kx_rbac.py` | `test_allow_routes_through_decide_and_stamps_adapter`, `test_q_denial_becomes_deny_with_reason`, `test_infrastructure_error_fails_closed_via_decide` (8 functions total) | existing | critical | The concrete PEP-1 adapter selected via `KX_MCP_AUTHZ=kdbx_rbac`; talks to `.kx.auth.authorize` over the cached PyKX connection — **mocked**, no real q (see row 4.5, the same live-q gap). |
| 5.2 | SQL write-keyword blocklist (`INSERT`/`DROP`/etc.) still rejects on a SELECT-only tool (no-regression floor) | `packages/kx-mcp-kdbx/tests/unit/addins/test_kdbx_run_sql_query.py` | (13 functions covering the blocklist + `@authorize` route-only/deny paths) | existing | critical | Confirmed not regressed by the `@authorize` decoration landing on `run_query_impl`. |
| 5.3 | KDB.AI: two personas with different ACL grants get different results | `deterministic/realidp/kdbai/test_kdbai_acl.py` | `test_kdbai_acl_list_tables_differentiates_personas` (KA.2 headline) | **existing** | critical | Shipped as KA.1–KA.9 in the OAuth-ACL sub-section below. `@pytest.mark.realidp @pytest.mark.kdbai`, `just test-kdbai`. **AU-W/AU-D gap:** kdbai bundle is read-only (no create/insert/update/delete/drop tool); write/delete rows blocked until write tools are added to the bundle. |
| 5.5 | Audit record answers who/what/backend/decision for one call per backend | `tests/deterministic/unit/test_audit_shape.py` | `test_dispatch_audit_shape_uniform_across_backends`, `test_dispatch_audit_shape_carries_allow_decision_and_adapter`, `test_dispatch_audit_shape_carries_deny_decision_and_adapter` | existing | important | Three fake bundles stand in for kdbx/kdbai/acme (middleware is backend-agnostic); "which backend" is inferred from the `target` namespace prefix, not a structured field — flagged as a follow-up, not a gap in this row. |
| 5.6 | The data gate's (PEP-2) `obligations` scope-down consumed by a real tool; the capability check and data gate (PEP-1/PEP-2) don't double-gate the same request | — | — | **needed** | minor | `obligations` passes through the `AuthzDecision` today (`test_authz.py`) but no backend consumes it to filter output. No production backend needs this yet — tracked as a reference-implementation gap, not a blocker. |

### KDB.AI OAuth ACL integration (`@pytest.mark.realidp` + `@pytest.mark.kdbai`, manual/local — requires registry-gated `kdbai-db` image + Keycloak)

> Proves the full passthrough chain: inbound OIDC bearer → container (`KX_MCP_AUTH=jwks`) → kdbai
> tool (`KDBAI_DB_OUTBOUND_STRATEGY=passthrough`) → OAuth `kdbai-db` ACL check on the propagated
> `tenant`/`groups` claims. KA.8/KA.9 exercise `service_account` instead — see below.
>
> **Setup:** `docker compose --profile backends up -d` (needs `docker login registry.gitlab.com` +
> `KDB_LICENSE_B64`), `uv run keycloak_setup.py keycloak_config.json`, `uv run python seed.py`
> (one-time database + grant creation), fill `envs/.env.kdbai` from `.env.kdbai.example`. Run with
> `just test-kdbai`. All tests are dual-marked `realidp` + `kdbai`.
>
> **Read-only surface — AU-W/AU-D gap.** The kdbai bundle exposes only read tools
> (`kdbai_list_tables`, `kdbai_query_data`, `kdbai_table_info`, `kdbai_similarity_search`, etc.) —
> no create/insert/update/delete/drop. KA tests cover the read path (AU-R); write/delete rows are a
> documented gap blocked on adding write tools to the bundle.

| # | Test | File | Function | Status | Priority | Notes |
|---|---|---|---|---|---|---|
| KA.1 | kdbai bundle mounts against real OAuth kdbai-db — pre-flight passes, kdbai tools advertised | `deterministic/realidp/kdbai/test_kdbai_acl.py` | `test_kdbai_bundle_mounts` | existing | critical | `kdbai_list_tables`, `kdbai_query_data`, `kdbai_list_databases`, `kdbai_table_info` advertised; passthrough pre-flight (socket probe) passing proves the container started against an OAuth kdbai-db |
| KA.2 | Two-persona DB-read differentiation: alice (trader) sees T1; bob (viewer) sees [] | `deterministic/realidp/kdbai/test_kdbai_acl.py` | `test_kdbai_acl_list_tables_differentiates_personas` | existing | critical | **Row 5.3 headline.** Committed version of the 2026-06-18 live validation. Grant: `quants/trader → DB-level read on db_read`. alice (quants/[trader,viewer]) sees T1; bob (quants/[viewer]) sees []. Purely by propagated identity. |
| KA.3 | `kdbai_query_data`: alice (trader) succeeds; bob (viewer) gets `status:error` | `deterministic/realidp/kdbai/test_kdbai_acl.py` | `test_kdbai_acl_query_data_permitted_and_denied` | existing | critical | ACL denial surfaces as `{"status":"error","message":"..."}` in the tool result — not an HTTP error. Proves structured denial semantics. |
| KA.4 | `kdbai_table_info`: alice (trader) gets schema + stats; bob (viewer) gets `status:error` | `deterministic/realidp/kdbai/test_kdbai_acl.py` | `test_kdbai_acl_table_info_permitted_and_denied` | existing | important | Exercises table-level read without an embedding provider (pure metadata endpoint). Analog of AU-R-06 `getTable`. |
| KA.5 | `kdbai_list_databases` is system_admin-only: root sees list; alice (trader) is denied | `deterministic/realidp/kdbai/test_kdbai_acl.py` | `test_kdbai_acl_list_databases_system_admin_only` | existing | important | root is manager/[admin] (system_admin, bypasses ACL); alice has a DB-read grant but `listDatabases` is admin-only. **Uses a second container** (`kdbai_manager_container_url`) pointed at the manager-realm JWKS — the main `kdbai_container_url` is quants-only, so root's manager-realm token would 401 at its inbound gate; alice goes to the quants container. Both share one kdbai-db. |
| KA.6 | Table-scoped grant on `db_read_isolated`: alice can query T1 but not T2 (no grant bleed) | `deterministic/realidp/kdbai/test_kdbai_acl.py` | `test_kdbai_acl_table_scoped_grant_no_bleed` | existing | critical | Grant: `quants/viewer → table-level read on T1 only`. alice (viewer) can query T1; T2 returns `status:error` even though alice has a grant for a different table in the same database. Analog of AU-R-11. |
| KA.7 | No bearer → 401 at the inbound gate (before any kdbai-db call) | `deterministic/realidp/kdbai/test_kdbai_acl.py` | `test_kdbai_no_bearer_rejected` | existing | important | HTTP-layer auth check independent of token content. Works even when kdbai-db is temporarily unavailable. |
| KA.8 | `service_account`: a real client-credentials grant against live Keycloak mints a token kdbai-db accepts | `deterministic/realidp/kdbai/test_kdbai_acl.py` | `test_kdbai_service_account_query_succeeds` | existing | important | `kdbai_service_account_container_url` fixture: `KDBAI_DB_OUTBOUND_STRATEGY=service_account`, machine client `kdbai-service-worker` (dedicated `quants` service client, `serviceAccountsEnabled`, own `quants/service → db_read` grant — distinct from alice's `trader` grant, so this can't pass by accidentally inheriting hers). Row 3.9. |
| KA.9 | `service_account`: alice and bob get an *identical* result — the backend never sees either human's claims | `deterministic/realidp/kdbai/test_kdbai_acl.py` | `test_kdbai_service_account_result_independent_of_caller` | existing | critical | The distinguishing assertion vs. passthrough (KA.2/KA.3): under passthrough alice/bob differ by their own claims; under service_account only the container's machine identity reaches kdbai-db, so both callers must see the same result. Row 3.9. **Entra:** no service client provisioned yet — `kdbai_service_account_container_url` skips under `AUTH_PROVIDER=entra`. |

---

## Planned — additional CLI coverage

> Not yet decomposed. Only the kx auth CLI track is tracked here; full test rows will be added when this work is scheduled.

| # | Test | File | Function | Status | Notes |
|---|---|---|---|---|---|
| 9.1 | `kx auth check --resource <…>` wraps the authorization adapter's decision API; correct allow/deny from shell | packages/kx-auth-cli/tests/ | — | deferred | Planned `kx auth` CLI activity |

---

## Cross-cutting / structural

| # | Test | File | Function | Status | Priority | Notes |
|---|---|---|---|---|---|---|
| X.1 | `try_mount_bundle`: failed `build_server()` disables only that backend; parent still starts and serves other bundles | deterministic/unit/test_composition.py | `test_try_mount_skips_backend_that_exits_and_keeps_the_rest` / `test_try_mount_skips_backend_that_raises` | existing | — | The in-process proof; the over-the-wire counterpart is now X.8. |
| X.2 | Unreachable backend (no backend reachable): parent starts bare, returns empty tool list | deterministic/unit/test_composition.py | `test_try_mount_all_backends_down_yields_a_bare_but_live_parent` | existing | — | |
| X.3 | Multi-issuer / multi-realm: container accepts tokens from realm A and realm B simultaneously | — | — | **needed** | important | `JWTVerifier` takes one `issuer` + one `jwks_uri`; no multi-issuer whitelist yet. 2.41 proves the rejection; this row tracks the future multi-issuer feature. Real-IdP test exists for the rejection side (charlie/risk rejected by quants-configured container). |
| X.4 | `configure_logging`'s brand filter admits `kx_mcp*` (dotted children and underscore bundle siblings alike), rejects third-party loggers; idempotent; sets root level from `KX_MCP_LOG_LEVEL` | `deterministic/unit/test_logging.py` | `test_brand_filter_admits_brand_rejects_others`, `test_configure_logging_surfaces_sibling_and_audit_but_filters_third_party`, `test_configure_logging_sets_root_level_so_brand_info_is_created`, `test_configure_logging_is_idempotent` | existing | — | Regression for the rule that logging configuration belongs in the entry point (the launcher), not a library seam — a logger with no configured handler silently drops its records. |
| X.5 | Every first-party cross-package import in a workspace member's `src/` is declared in its own `pyproject.toml` | `deterministic/unit/test_packaging_deps.py` | `test_first_party_imports_are_declared` | existing | — | AST-scan guard against the uv-workspace-masks-undeclared-deps gotcha; a published wheel only carries declared `Requires-Dist`. |
| X.6 | `kx auth introspect` (joserfc) agrees with the container's `JWTVerifier` on accept/reject for the same token, across static and jwks modes | `packages/kx-auth-core/tests/test_verify.py`, `deterministic/unit/test_auth_cli_contract.py` | `test_static_valid_token_is_ok`, `test_jwks_bad_signature_is_error`, `test_issuer_mismatch_is_denied` (14 functions in `test_verify.py`) | existing | — | `test_verify.py` is the underlying `kx_auth_core.verify_token` unit suite; `test_auth_cli_contract.py` (already listed under inbound auth above) is the drift guard that runs both verifiers on the same token. |
| X.7 | `kx auth introspect` CLI: exit-code contract (0/1/3/4), `--json` envelope, token-from-arg/env/stdin | `packages/kx-auth-cli/tests/test_introspect_cli.py` | `test_valid_token_exits_0`, `test_expired_token_exits_3`, `test_wrong_audience_exits_4`, `test_json_envelope_on_valid`, `test_token_from_env` (9 functions total) | existing | — | The CLI-facing counterpart to X.6. |
| X.8 | Graceful degradation over the wire: real container with one unreachable backend comes up bare-but-live, serves the healthy bundle | `deterministic/integration/test_composition_integration.py` | `test_failing_bundle_degrades_gracefully_over_the_wire` | existing | important | Over-the-wire counterpart to X.1/X.2, which only prove this in-process. License-free (`KX_MCP_AUTH=unset`): spawns `--bundles example,failing` where the `kx-mcp-failing` fixture's `build_server()` `sys.exit(1)`s; asserts `example_echo` is live/callable, no `failing_*` tools, and a session-less raw JSON-RPC POST answers 400 rather than hanging. |
| X.9 | Live multi-backend composition: two real backends mounted on one container, no namespace collision, both reachable, audit attributes each dispatch to the right target prefix | `deterministic/integration/test_composition_integration.py` | `test_two_backends_compose_no_collision_over_the_wire` | existing | important | License-free: `example` + `example2` (the same fixture code re-exported under a second package, `--bundles example,example2`) — proves fresh `build_server()` closures avoid collision under two namespaces; both tools independently callable; audit lines carry `target=example_echo`/`target=example2_echo` distinctly. This is one codebase mounted twice, not two distinct `addins/` packages — it does not exercise the `sys.modules` collision path between two *different* `addins/` packages; that's covered by `deterministic/unit/test_discovery.py::test_two_sibling_addins_packages_do_not_collide`. |

---

## How to use this file

1. When you write a test, change its row from `needed` → `existing` and fill in the `Function` column.
2. When new work is scheduled, move its rows from `deferred` → `needed` and assign priorities.
3. When a test is deliberately removed or replaced, note why in the `Notes` column rather than deleting the row.
4. `critical` items block that work from being considered done; `important` items should land in the same PR if feasible; `minor` items can follow.
5. The sections above are a **planning device only** — the shipped suite is organized by feature, not by these sections.
