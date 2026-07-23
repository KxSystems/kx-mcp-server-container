# Claude Code → live kdb-x, two RBAC sets (operator path)

Claude Code → the jwks-protected container (`KDBX_DB_ASSERT_IDENTITY=true` +
`KX_MCP_AUTHZ=kdbx_rbac`) → a local kdb-x host carrying the `kx.auth` module — with **two
semantic RBAC sets enforced over one `.kx.auth` engine**:

- **The capability check (PEP-1, container-side):** the SQL tool carries `@authorize(action="query",
  resource="kdbx:sql")`; with `KX_MCP_AUTHZ=kdbx_rbac` the decorator calls
  `.kx.auth.authorize[\`query;\`kdbx:sql]` before the query — *may this subject invoke the SQL tool at all?*
- **The data gate (PEP-2, q-side):** `.s.e` wraps the query with `.kx.auth.authorize[\`read;\`trades]`
  — *for this data, what is permitted?*

The grants **diverge** so the two layers are visible: **alice** (`viewer`+`trader`) clears both → rows;
**bob** (`viewer`) clears the capability check (he *may* use the SQL tool) but the data gate denies the
`trades` data → a clean `permission_denied`. An `INSERT` is rejected by the SQL write-keyword blocklist
regardless.

> **NB — two denial shapes.** A capability-check deny raises `AuthorizationDenied` (a clean tool
> error — *"not authorized: query on kdbx:sql"*); a data-gate deny returns the structured
> `permission_denied` envelope. The capability check is the `@authorize` decorator over the
> `kx_auth_core.authz` `kdbx_rbac` adapter; the data gate is the q-side gate.

All commands below run from the **repo root** (the workspace root):

```bash
cd /path/to/kx-mcp-server-container   # if you aren't already there
```

## Prerequisites

- `q` (KDB-X), `uv`, `just`, and Claude Code on PATH.
- The `kx.auth` module on the q module path: `just install-modules`.
- A running realidp Keycloak (the `quants` realm) + a filled-in `tests/deterministic/realidp/envs/.env.keycloak`
  (the same harness `just test-kdbx` uses; the file is gitignored — create it from the template).
  Every client the fixture provisions carries the `groups` mapper the policy keys on (generalized
  onto all provisioned clients, not just kdbai-service's), so no kdb-x-specific fixture or
  kdbai-fixture borrow is needed. Bring it up with:
  ```bash
  cp tests/deterministic/realidp/envs/.env.keycloak.example \
     tests/deterministic/realidp/envs/.env.keycloak
  # Fill in KC_BASE, KC_REALM, KC_CLIENT_ID, KX_MCP_AUTH_AUDIENCE (the defaults match this demo)
  docker compose -f tests/deterministic/realidp/setup/keycloak/docker-compose.yaml up -d
  uv run python tests/deterministic/realidp/setup/keycloak/keycloak_setup.py \
      tests/deterministic/realidp/setup/keycloak/keycloak_config.json
  ```

## Step 1 — start the kdb-x host (data + kx.auth + the two grant sets)

> **Every step below runs in its own terminal** (the host, the container, and the `claude`/mint
> commands all need separate terminals since the first two block in the foreground) — **`cd` to the
> repo root in each new terminal before running anything**, or relative paths like `demos/claude-code-live-kdbx/host.q`
> will silently resolve against the wrong directory (`q` will report `No such file or directory` and
> drop you into a bare session with no `kx.auth`/`trades`/grants loaded).

The service account authenticates via a standard kdb+ `-U` user:md5hash file (the secret is not in
`host.q`). Generate it from the same password the container will use, then start the host on `:5010`:

```bash
cd /path/to/kx-mcp-server-container   # this terminal too — see the note above
just install-modules
SVC_USERPASS=$(mktemp); printf 'kxmcp:%s\n' \
  "$(printf 's3cret-svc-pw' | { md5sum 2>/dev/null || md5; } | awk '{print $1}')" > "$SVC_USERPASS"
q demos/claude-code-live-kdbx/host.q -U "$SVC_USERPASS"
# => "claude-code-live-kdbx host ready: identity assertion ON, TWO RBAC sets"
```

## Step 2 — start the container as a discovery-advertising HTTP server

```bash
set -a; source tests/deterministic/realidp/envs/.env.keycloak; source demos/claude-code-live-kdbx/kdbx.env; set +a
uv run kx-mcp --bundles kdbx --transport streamable-http --host 127.0.0.1 --port 8000
# Logs should show: jwks inbound, KDBX_DB_ASSERT_IDENTITY on, KX_MCP_AUTHZ=kdbx_rbac, kdb-x reachable.
```

Verify discovery is advertised (RFC 9728 protected-resource metadata):

```bash
curl -s http://127.0.0.1:8000/.well-known/oauth-protected-resource/mcp | python -m json.tool
```

## Step 3 — register Claude Code, as alice

Steps 1 and 2 each run in the foreground of their own terminal, so do this in a **new terminal**
— `cd` to the repo root there too, and source `.env.keycloak` again (`mint_token.py` needs `KC_BASE` /
`KC_REALM` / `KC_CLIENT_ID`; it doesn't need `kdbx.env`'s `KDBX_*` vars):

```bash
set -a; source tests/deterministic/realidp/envs/.env.keycloak; set +a
```

Mint alice's bearer and add the server with a token-injection header (this stands in for an
interactive device-code login, keeping the demo self-contained):

```bash
ALICE=$(uv run python demos/claude-code-live-kdbx/mint_token.py P-Q-ALICE)
claude mcp add --transport http kx-kdbx http://127.0.0.1:8000/mcp \
  --header "Authorization: Bearer $ALICE"
claude mcp list   # kx-kdbx connected; /mcp lists kdbx_* tools
```

> Persona tokens last ~15 min (`access_token_lifespan_seconds: 900`). Re-mint and
> `claude mcp remove kx-kdbx` + re-add when one expires.

## Step 4 — let Claude Code drive it (alice), then swap to bob

Hand Claude Code the scenario in [`agent.md`](agent.md). As **alice** the `SELECT … FROM trades`
returns rows; an `INSERT` is rejected by the blocklist. Then swap identity to **bob** and re-run:

```bash
claude mcp remove kx-kdbx
BOB=$(uv run python demos/claude-code-live-kdbx/mint_token.py P-Q-BOB)
claude mcp add --transport http kx-kdbx http://127.0.0.1:8000/mcp \
  --header "Authorization: Bearer $BOB"
```

As **bob** the same `SELECT` returns `permission_denied` — *"…not permitted read on trades"* — even
though bob passed the capability check. On the container/host terminals you can see **both** decisions:
the PEP-1 `query`/`kdbx:sql` consult (allow) and the PEP-2 `read`/`trades` consult (deny).

## Teardown

```bash
claude mcp remove kx-kdbx
# Ctrl-C the container and the q host; rm -f "$SVC_USERPASS"
```

## Seeing PEP-1 deny (the capability gate has teeth)

The headline above shows bob denied at the *data* layer. To see the *capability* layer deny — before
any data round-trip — narrow the capability grant in [`host.q`](host.q) to traders only:

```q
.demo.capGrants:([] grp:enlist `trader; act:enlist `query; res:enlist `$"kdbx:sql");
.demo.grants:.demo.dataGrants,.demo.capGrants;  / rebuild the combined table the check reads
```

Restart the host and re-run as **bob**: now `.kx.auth.authorize[\`query;\`kdbx:sql]` denies him at
PEP-1, the `@authorize` decorator raises `AuthorizationDenied` — *"not authorized: query on
kdbx:sql"* — and the query never reaches the data gate. Two independent semantic RBAC sets, one
`.kx.auth` engine.

## Wrinkles

- **Both settings are required for the two-set demo.** `KDBX_DB_ASSERT_IDENTITY` binds the principal
  (so the q gate can see it); `KX_MCP_AUTHZ=kdbx_rbac` routes the SQL tool's `@authorize` capability
  check to the q engine. Both default off/unset — existing single-principal deployments are unchanged.
- **Fixed `(action;resource)`.** The demo gates on a fixed `(\`read;\`trades)` (PEP-2) and
  `(\`query;\`kdbx:sql)` (PEP-1) to keep the lesson on the two seams, not on SQL parsing. The PEP-1
  vocabulary `query`/`kdbx:sql` is the documented convention the `@authorize` decorator and the q
  capability grant set share.
- **Groups source.** Tokens carry a top-level `groups` claim (`viewer`/`trader`); `kx.auth`'s default
  search finds it, so no `setClaims` override is needed. If your IdP puts groups elsewhere, set the
  path with `.kx.auth.setClaims[(enlist \`groups)!enlist "your.claim.path"]`.
