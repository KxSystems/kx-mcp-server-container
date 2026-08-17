# kdbx ferry live lane

Proves the kdbx "ferry" identity-assertion strategy — `.kx.auth.bind` promoting an inbound
identity onto a q connection, `.kx.auth.authorize`/`setPolicy` enforcing a group×resource×action
grant — end-to-end against a **real** `q` process and a **real** live Keycloak IdP. No mocking:

    MCP client --(Bearer Keycloak-token)--> container [KX_MCP_AUTH=jwks, quants realm]
        --> kdbx tool --(assert_identity: .kx.auth.bind)--> real q
            --> .kx.auth.authorize[`read;`data.trades] (`trader` group grant, default-deny)

Unlike `kdbai/` (which connects to an already-running managed backend), kdbx's backend is
plain kdb+ — there's no pre-existing service to provision, so **this lane spawns its own
throwaway `q` process** for the session (`kdbx_ferry_host.q`, loaded with `kx.auth`).

Reuses the same live Keycloak `quants` realm and alice/bob personas `idp`/`kdbai` already use —
alice has the `trader` group, bob doesn't. No new provisioning needed.

## Quick start

**Prerequisites:** the same local Keycloak stack `test-keycloak` uses, plus a kdb-x install
(`q` on `PATH`, or `~/.kx/bin/q`) with a valid license.

All commands below run from the **repo root** (the workspace root).

```bash
cd /path/to/kx-mcp-server-container   # if you aren't already there

# 1. Start Keycloak + Postgres (no kdbai-db profile needed for this lane)
docker compose -f tests/deterministic/realidp/setup/keycloak/docker-compose.yaml up -d

# 2. Provision Keycloak (idempotent) — same realm/personas idp/ and kdbai/ use
uv run python tests/deterministic/realidp/setup/keycloak/keycloak_setup.py \
    tests/deterministic/realidp/setup/keycloak/keycloak_config.json

# 3. Configure env — same file test-keycloak uses, no new template needed
cp tests/deterministic/realidp/envs/.env.keycloak.example \
   tests/deterministic/realidp/envs/.env.keycloak
# Fill in KC_BASE, KC_REALM, KC_CLIENT_ID, KX_MCP_AUTH_AUDIENCE

# 4. Install the kx.auth module onto the q runtime's module path (idempotent symlink)
just install-modules

# 5. Run
just test-kdbx
```

`just test-kdbx` is equivalent to:

```bash
set -a; source tests/deterministic/realidp/envs/.env.keycloak; set +a
uv run pytest tests/deterministic/realidp/kdbx -m kdbx -v
```

**Note the explicit path** (`tests/deterministic/realidp/kdbx`, not just `-m kdbx`) — see
[Gotcha](#gotcha--why-collection-is-scoped-to-this-directory) below for why that matters.

## What each test asserts

| Test | What it proves |
|---|---|
| `test_kdbx_ferry_alice_in_trader_group_allowed` | Full chain, no mocking: the container validates alice's real Keycloak bearer, ferries her principal via `.kx.auth.bind` on the service-account qIPC handle, and `.s.e`'s wrapper calls `.kx.auth.authorize[`read;`data.trades]` — the `trader` group grant allows it, real trade rows come back. |
| `test_kdbx_ferry_bob_without_trader_group_denied` | bob's principal *is* bound (the service-account connection is trusted to assert any identity — the bind-gate gates the caller, not the asserted principal) but he lacks `trader`; `.kx.auth.authorize` refuses. The q-side `'denied: ...` signal is translated into a structured `{"status":"error","error_type":"permission_denied",...}` envelope, not a raw stack trace. |
| `test_kdbx_ferry_unbound_connection_denied_by_default` | A second raw qIPC connection, opened directly against the ferry host with the same service-account creds but where `.kx.auth.bind` is **never called**, is denied by default. Proves the q module's own default-deny holds on its own terms — not merely because the container happens to always bind before querying. |

## How it's wired

`kdbx_ferry_host` (session-scoped fixture) spawns a real `q` process running
[`kdbx_ferry_host.q`](kdbx_ferry_host.q):

- `.s.init[]` + an inlined `trades` table (kept local rather than loading the broader
  `examples/host.q` smoke-test host).
- `.kx.auth:use`kx.auth` — loads the identity-assertion module from `~/.kx/mod/kx/auth`
  (symlinked to [`modules/kx/auth/`](../../../../modules/kx/auth/) by `just install-modules`).
- `setPolicy`: the real Keycloak `trader` group may `read`/`write` `data.trades` (default-deny); the
  service-account login (`kxmcp`, matching `KDBX_DB_USERNAME`) may `assert` `kx.identity` (the
  `bind[]` caller-gate).
- `.s.e` is wrapped so every SQL query is gated by `.kx.auth.authorize[`read;`data.trades]`.

`kdbx_ferry_container_url` (session-scoped, depends on the fixture above) spawns the MCP
container itself via the shared `_spawn_container` helper: `--bundles kdbx`, `KX_MCP_AUTH=jwks`
pointed at the real Keycloak JWKS, `KDBX_DB_HOST`/`PORT`/`USERNAME`/`PASSWORD` pointed at the
spawned q process, `KDBX_DB_ASSERT_IDENTITY=true`.

`alice_token`/`bob_token` are inherited for free from the shared root `realidp/conftest.py` — no
import needed in this lane.

## Gotcha — why collection is scoped to this directory

`just test-kdbx` runs `pytest tests/deterministic/realidp/kdbx -m kdbx`, not just `pytest -m kdbx`.
pytest's default `testpaths` collects every package's unit tests regardless of `-m` filtering,
and several kdbx unit test files import `kx_mcp_kdbx.server` at module level — which sets
`os.environ["PYKX_LICENSED"] = "true"` before `import pykx`. That import mutates the **pytest
process's own** environment (overwrites `QHOME`, sets a stale `QPATH`) as a side effect, before
any fixture in this lane ever runs — and that contamination leaks into the container subprocess
this lane spawns, surfacing as a spurious "no valid q license" failure even though the license is
genuinely there. Scoping collection to this directory avoids triggering the import at all.
`realidp/_spawn.py`'s `_PYKX_KEYS_TO_STRIP` also strips `QHOME`/`QPATH`/`PYKX_DIR`/
`PYKX_EXECUTABLE`/`PYKX_UNDER_PYTHON` from every realidp container spawn as defense in depth.

## Directory

```
kdbx/
  conftest.py         ← kdbx_ferry_host (real q subprocess), kdbx_ferry_container_url
  kdbx_ferry_host.q    ← kx.auth + trader×trades×read/write grant, default-deny
  test_kdbx_ferry.py   ← @pytest.mark.realidp @pytest.mark.kdbx: alice/bob/unbound
  README.md            ← this file
```
