# Deploying the kx-mcp container

How an operator stands up the KX MCP composition container in a real environment: what to install,
what to point it at, how to secure it, and how to know it's healthy. Configuration of the auth
seams themselves is in the [auth guide](auth.md); extending the container with a new backend is in
the [extender guide](extending.md).

**What you're deploying.** One Python process — the container — which mounts the backend bundles
you select (`kdbx`, `kdbai`) into a single MCP surface and serves it over streamable-HTTP
(or STDIO for single-user bundling). The backends themselves (KDB-X and KDB.AI) are *not* part of
the deployment — the container connects out to wherever they already
run. The process is deliberately host-native: co-located with a kdb-x install it reuses its
license, and localhost backends just work.

## Quickstart — three ways to run it

### 1. From a checkout (the portable path)

The repo *is* a [uv](https://docs.astral.sh/uv/) workspace with every package as source, so a clone
runs the container with no index or credentials:

```bash
uv sync
uv run kx-mcp --bundles kdbx          # or: just run
```

For a local smoke target, start the sample KDB-X host first: `q examples/host.q` (listens on
`:5010`, loads the SQL + AI modules, seeds a canonical `trades` table). The kdb-x bundle needs a
PyKX license at import — with a kdb-x install on the machine (its `q` on `PATH`) it resolves
automatically; see the prerequisites table below for the standalone case. The KDB.AI bundle needs no
license.

### 2. From the published wheels (no checkout) — `uvx` &nbsp;·&nbsp; _KX-internal today_

> **Internal use only, for now.** The packages are published to the **internal KX Nexus** (requires
> KX VPN + read-only Nexus credentials); they are **not** on PyPI or any public index — external
> wheel distribution is not yet committed. An external reader should use path 1 (checkout) or path 3
> (build the wheels from source).

With Nexus access, `uvx` runs the container in an ephemeral environment from one command:

```bash
export UV_INDEX_KXI_NEXUS_USERNAME=<nexus-ro-user>
export UV_INDEX_KXI_NEXUS_PASSWORD=<nexus-ro-password>

uvx --index kxi-nexus=https://nexus.kxi-dev.kx.com/repository/kxi/simple \
    --from kx-mcp-core --with kx-mcp-kdbx \
    kx-mcp --bundles kdbx
```

`--from kx-mcp-core` names the package that owns the `kx-mcp` command (uvx can't infer it from the
executable name); each `--with` adds a backend bundle wheel (`kx-mcp-kdbx`/`-kdbai`, repeat
for several) matching the `--bundles` list. Pin per release tag once you depend on a version:
`--from kx-mcp-core==<X.Y.Z> --with kx-mcp-kdbx==<X.Y.Z>` (all packages version in lockstep).

### 3. Assemble your own server (downstream repo)

When you want your own repo that composes the container with your own glue or a private bundle,
[`demos/extending/`](../demos/extending/README.md) is the runnable reference: a ~6-line
`server.py` over `make_parent` + `try_mount_bundle`, a `pyproject.toml` that consumes the `kx-*`
wheels from a local index, and a health probe. Because the wheels aren't on a public index, it
**builds them from this repo's source** (`uv build --all-packages --wheel`) and consumes them from
`./dist` — so it runs with nothing but a checkout, and you swap the one index line for a publishing
index if/when one exists.

## Per-backend prerequisites

| Backend | Bundle | Needs on the container host | Needs reachable |
| --- | --- | --- | --- |
| KDB-X (plain kdb+) | `kx-mcp-kdbx` | **PyKX license** — auto-resolved from a co-located kdb-x install (`q` on `PATH`); otherwise set `QLIC` to the license dir | A KDB-X process with the **SQL module initialized** through its compatibility interface (`.s`), default `127.0.0.1:5010`; AI libs (`.ai`) optional — without them the similarity-search tools are disabled, not broken |
| KDB.AI | `kx-mcp-kdbai` | Nothing licensed (the `kdbai-client` SDK; qipc mode uses PyKX unlicensed path via the workspace pin) | A KDB.AI server, default `127.0.0.1:8082`, qipc or REST (`KDBAI_DB_MODE`) |

Each bundle runs an **eager pre-flight** at startup (connectivity → interfaces → optional
features), logging `SUCCESS`/`ERROR` per check, and refuses to serve a broken backend. Per-backend
setup detail (modules, TLS, embeddings) lives in each bundle's README:
[kdbx](../packages/kx-mcp-kdbx/README.md) · [kdbai](../packages/kx-mcp-kdbai/README.md).

## The production topology

```
                   ┌──────────────────────── container host ───────────────────────┐
MCP clients ──TLS──▶  kx-mcp  ── inbound KX_MCP_AUTH=jwks ─── validated principal   │
(Claude Code, …)   │     │        + KX_MCP_AUTH_RESOURCE_URL (RFC 9728 discovery)   │
        ▲          │     ├── kdbx bundle ── qIPC + identity assertion ──▶ KDB-X     │
        │          │     └── kdbai bundle ── passthrough / svc-account ──▶ KDB.AI   │
   IdP (Keycloak / │                                                               │
   Entra ID, JWKS) └────────────────────────────────────────────────────────────────┘
```

A hardened deployment is three decisions, each detailed in the [auth guide](auth.md):

1. **Inbound**: `KX_MCP_AUTH=jwks` against your IdP, and `KX_MCP_AUTH_RESOURCE_URL` set to the
   public URL clients use — that single variable is what lets MCP clients discover the IdP and log
   in themselves.
2. **Outbound, per backend**: `passthrough` where the backend enforces per-user ACLs off the same
   issuer; `service_account` where the backend authorizes the workload; `KDBX_DB_ASSERT_IDENTITY`
   for plain kdb+.
3. **Authorization**: backend data gates are authoritative by default (route-only); add
   `KX_MCP_AUTHZ` capability grants only where a tool-class concern is distinct.

**Transport & network.** Default transport is `streamable-http` on `KX_MCP_HOST:KX_MCP_PORT`
(`127.0.0.1:8000`). Bind `0.0.0.0` only behind TLS termination (a reverse proxy is the expected
shape — the container itself doesn't terminate TLS), and keep `KX_MCP_AUTH_RESOURCE_URL` equal to
the *client-facing* URL, not the internal bind address. Backend-leg TLS: `KDBX_DB_TLS` /
`KDBAI_DB_QIPC_TLS` (both need `KX_SSL_CA_CERT_FILE` at the PyKX layer), `https` in
`KDBAI_DB_REST_PROTOCOL` for the REST leg (`KDBAI_DB_SSL_VERIFY` governs the outbound OIDC
token call, on by default).

**Secrets.** Prefer file-backed secrets where offered (`KDBX_DB_PASSWORD_FILE` wins over
`KDBX_DB_PASSWORD` — mountable as a K8s/Docker secret); client secrets ride in env vars — keep them
out of unit files readable by other users, shell history, and committed `.env` files (this repo
gitignores root-level `.env.*` for that reason).

## Configuration reference

Container serving (`KX_MCP_*`, read by the `kx-mcp` launcher):

| Variable | Default | Meaning |
| --- | --- | --- |
| `KX_MCP_BUNDLES` | — | **Required.** Comma-separated bundles to mount: `kdbx,kdbai` |
| `KX_MCP_TRANSPORT` | `streamable-http` | `stdio` for single-user bundling |
| `KX_MCP_HOST` / `KX_MCP_PORT` | `127.0.0.1` / `8000` | HTTP bind (ignored for stdio) |
| `KX_MCP_NAME` | `kx-mcp` | Server instance name |
| `KX_MCP_LOG_LEVEL` | `INFO` | The container's own logs (audit line, mount warnings) |

Backend connection fragments (each owned by its bundle — full tables in the bundle READMEs):

| Backend | Key variables (defaults) |
| --- | --- |
| kdbx | `KDBX_DB_HOST` (`127.0.0.1`) · `KDBX_DB_PORT` (`5010`) · `KDBX_DB_USERNAME`/`_PASSWORD`/`_PASSWORD_FILE` · `KDBX_DB_TLS` (`false`) · `KDBX_DB_TIMEOUT` (`1`) · `KDBX_DB_RETRY` (`2`) · `KDBX_DB_ASSERT_IDENTITY` (`false`) · vector-search: `KDBX_DB_EMBEDDING_CSV_PATH`, `KDBX_DB_METRIC` (`CS`), `KDBX_DB_K` (`5`) |
| kdbai | `KDBAI_DB_HOST` (`127.0.0.1`) · `KDBAI_DB_PORT` (`8082`) · `KDBAI_DB_MODE` (`qipc`\|`rest`) · `KDBAI_DB_DATABASE_NAME` (`default`) · `KDBAI_DB_OUTBOUND_STRATEGY` + OIDC detail (see [auth guide](auth.md)) · hybrid-search weights `KDBAI_DB_VECTOR_WEIGHT`/`_SPARSE_WEIGHT` (`0.7`/`0.3`) |

Auth variables (`KX_MCP_AUTH*`, `KX_MCP_AUTHZ*`, outbound strategies): the
[auth guide](auth.md) is the single reference — not duplicated here.

## Verifying a deployment

1. **Startup log**: every mounted bundle's pre-flight lines read `SUCCESS`; the FastMCP banner
   shows your transport/URL.
2. **Handshake**: point any MCP client at `http://<host>:<port>/mcp` and list tools — you should
   see the namespaced surface (`kdbx_run_sql_query`, `kdbai_list_tables`, …).
3. **Auth path** (when inbound auth is on): `kx auth login --server <url>` completes discovery + login, an
   authenticated call succeeds, and an unauthenticated one gets a clean `401` — each dispatch
   emitting one `kx_mcp.audit` line (`subject / action / target / outcome`).

## Troubleshooting

| Symptom | Cause / fix |
| --- | --- |
| Bundle exits at startup: connectivity `ERROR` | The backend isn't reachable from the container host — check `*_DB_HOST`/`_PORT`, network path, and that the backend is actually up. The container refuses to serve a broken backend by design. |
| kdbx pre-flight: SQL interface `ERROR` | The target q process hasn't initialized the SQL module's compatibility interface — run `.s.init[]` on the host. See `examples/host.q` for the canonical bring-up. |
| kdbx: `import pykx` fails at startup | No resolvable license: put the kdb-x `q` on `PATH` (co-located install) or set `QLIC` to the directory holding `kc.lic`. |
| Similarity-search tools missing from `list_tools` | AI libs (`.ai`) not loaded on the q host — deliberate degradation, not a fault; load `kx.ai` to enable them. |
| qIPC connect *times out* on macOS at `:5000` | AirPlay owns `:5000` on macOS and swallows connections — this is why the kdb-x default is `:5010`. Don't deploy a backend on `:5000` on a Mac. |
| `401` on every authed call / client can't log in | Work the inbound checklist in the [auth guide](auth.md#troubleshooting) — issuer/audience mismatch and a missing/wrong `KX_MCP_AUTH_RESOURCE_URL` cover most cases. |
| `uvx` fails resolving `kx-mcp-core` (401) | Wrong/missing Nexus credentials — set `UV_INDEX_KXI_NEXUS_{USERNAME,PASSWORD}` (read-only pair) or a `.netrc` entry. |
| Audit lines don't appear | You're embedding `make_parent` in custom glue without calling `configure_logging()` — the launcher does this for you; custom entry points must too. |
