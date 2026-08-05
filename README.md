# KX MCP Server

A **composition MCP server** for KX products: a [FastMCP](https://gofastmcp.com) 3.x parent that mounts
one or more **pluggable backend extensions**, each behind a uniform contract, into a single served
surface. KDB-X and KDB.AI are the first shipped backends; downstream teams can compose additional
extensions through the same contract.

The server is the **control plane** — it is the single place where inbound authentication, the
authorization seam, and outbound identity propagation attach (see
[authentication & authorization](docs/auth.md)). Inbound authentication (`KX_MCP_AUTH` —
`unset` / `static` / `jwks` / `entra`), outbound identity propagation, and the authorization seam
are all **wired (opt-in)** — the capability check (`@authorize`, PEP-1) and the per-backend data
gates (PEP-2). Each **backend** is mounted as a namespaced bundle, so its tools, resources, and prompts
appear under a prefix (e.g. `kdbx_run_sql_query`, `tables://kdbx/all`) with no cross-backend
collisions.

## Where to start

| You want to… | Go to |
| --- | --- |
| **Connect** to a kx-mcp server someone already runs | [MCP client configuration](#mcp-client-configuration) — point your client at the URL; with auth on, the server advertises its IdP and the client logs you in |
| **Run it yourself** | [Quickstart](#quickstart) — `uv run` from a checkout (the portable path), or one `uvx` command from the wheels (KX-internal); then the [deployment guide](docs/deployment.md) for a production setup |
| **See it in action** | The live Claude Code demos — the whole auth flow end-to-end against a real backend: [`claude-code-live-kdbx`](demos/claude-code-live-kdbx/) (kdb-x, two RBAC sets), [`claude-code-live-kdbai`](demos/claude-code-live-kdbai/) (KDB.AI, RFC 9728 discovery + DCR), [`claude-code-live-kdbai-entra`](demos/claude-code-live-kdbai-entra/) (via Entra ID) |
| **Migrate** from the standalone kdb-x / KDB.AI MCP server | [Coming from the standalone servers](#coming-from-the-standalone-kdb-x-or-kdbai-server) — same tools, now one launch command, and you can run both backends in one server |
| **Assemble your own server** from the packages | [`demos/extending/`](demos/extending/) — the runnable downstream reference (builds the wheels from source) |
| **Extend** it with a new backend | The [extender guide](docs/extending.md) — contract + tutorial |

The user guides live under [`docs/`](docs/): [deployment](docs/deployment.md) ·
[authentication & authorization](docs/auth.md) · [writing a backend](docs/extending.md).

## Table of contents

- [Where to start](#where-to-start)
- [Quickstart](#quickstart)
- [How it works](#how-it-works)
- [Available backends](#available-backends)
- [Coming from the standalone kdb-x or KDB.AI server](#coming-from-the-standalone-kdb-x-or-kdbai-server)
- [Enabling, disabling & configuring backends](#enabling-disabling--configuring-backends)
- [Transports & deployment](#transports--deployment)
- [Backend capabilities](#backend-capabilities)
- [MCP client configuration](#mcp-client-configuration)
- [Adding a backend](#adding-a-backend)
- [Security considerations](#security-considerations)
- [Testing](#testing)
- [Troubleshooting](#troubleshooting)
- [Design & strategy](#design--strategy)
- [Useful resources](#useful-resources)

## Quickstart

> **Prerequisites:** a clone of this repository ·
> [uv](https://docs.astral.sh/uv/getting-started/installation/) (fetches a suitable Python itself) ·
> a local [KDB-X install](https://developer.kx.com/products/kdb-x/install) with a valid license and
> `q` on your `PATH`. Full detail (license flags, OS support, AI libs):
> [KDB-X backend README](packages/kx-mcp-kdbx/README.md#prerequisites).

1. **Bring up a KDB-X service.** For local testing, [examples/host.q](examples/host.q) loads the SQL + AI
   interfaces, seeds annotated `instruments` and `trades` tables, and listens on `:5010`:

   ```bash
   q examples/host.q
   ```

2. **Run the composition server with the kdb-x backend** (the first run downloads all dependencies —
   give it a few minutes):

   ```bash
   uv run kx-mcp --bundles kdbx
   ```

3. **Point your MCP client at `http://127.0.0.1:8000/mcp`** (see
   [MCP client configuration](#mcp-client-configuration)) and confirm the `kdbx_*` tools, the
   `tables://kdbx/all` resource, and the `kdbx_table_analysis` prompt appear. More checks:
   [deployment guide § verifying](docs/deployment.md#verifying-a-deployment).

**Config defaults.** Out of the box the container serves `streamable-http` on `127.0.0.1:8000`, and the
kdb-x backend connects to `127.0.0.1:5010`. Override the backend endpoint with an env var:

```bash
KDBX_DB_HOST=kdb-prod KDBX_DB_PORT=5011 uv run kx-mcp --bundles kdbx
```

The kdb-x backend is **mount-only** — it runs only through the container, so there is no standalone
`mcp-server` process; old muscle memory (`uv run mcp-server`) becomes `uv run kx-mcp --bundles kdbx`.

See the [backend README](packages/kx-mcp-kdbx/README.md#configuration) for the full `KDBX_DB_*` set
(TLS, timeout, retry, embeddings, metric, k) and the licence/AI-libs setup.

### Semantic KDB-X metadata and function discovery

The kdb-x backend can use the optional
[aimeta](https://github.com/KxSystems/aimeta) module on the target host to give agents semantic
schema context—not just column names and q types. Annotated hosts expose descriptions,
`semanticType` vocabularies, reference resolvers, explicit `foreignRef` joins, public q function
signatures, examples, and declared table dependencies. Hosts without usable aimeta metadata remain
fully supported: discovery falls back cleanly to native `tables[]`/`meta` introspection.

The agent-facing output is the versioned **KDB-X MCP metadata contract v1**, available as JSON
Schema at `schema://kdbx/metadata/v1`. Agents can discover all visible metadata or use the
token-efficient single-item surfaces:

- `tables://kdbx/all` and `tables://kdbx/{table}`
- `functions://kdbx/all` and `functions://kdbx/{function}`
- `kdbx_get_table_metadata` with a bounded live preview

Metadata is filtered through the same optional KDB-X data-entitlement gate as table discovery;
references and function dependencies cannot reveal filtered tables. The administrative
`kdbx_refresh_metadata` tool reloads recompiled annotations without restarting the container. It is
route-only when `KX_MCP_AUTHZ` is unset (the default); when capability authz is configured, callers
must have the `admin` capability for `kdbx:metadata`. See the
[KDB-X backend metadata guide](packages/kx-mcp-kdbx/README.md#semantic-metadata-with-aimeta) and
[authorization guide](docs/auth.md#authorization-kx_mcp_authz).

### Run from the published wheels (no checkout)

> **KX-internal.** This path pulls from the internal Nexus and needs KX credentials — external users,
> run from a checkout (above).

The same server, straight from the internal Nexus via [uvx](https://docs.astral.sh/uv/guides/tools/)
— nothing to clone:

```bash
export UV_INDEX_KXI_NEXUS_USERNAME=<nexus-ro-user>
export UV_INDEX_KXI_NEXUS_PASSWORD=<nexus-ro-password>

uvx --index kxi-nexus=https://nexus.kxi-dev.kx.com/repository/kxi/simple \
    --from kx-mcp-core --with kx-mcp-kdbx \
    kx-mcp --bundles kdbx
```

`--from kx-mcp-core` names the package that owns the `kx-mcp` command; each `--with` adds a backend
bundle wheel matching the `--bundles` list. The kdb-x licence resolves from a co-located kdb-x
install exactly as above. Details (credentials, pinning, prerequisites):
[deployment guide § quickstart](docs/deployment.md#quickstart--three-ways-to-run-it).

## How it works

Each backend already *is* a FastMCP server, exposed as a package with a `build_server() -> FastMCP`
function (a **bundle**). "The container" is the thin `kx-mcp-core` assembly seam plus a few lines of
glue that mount the bundles you want:

```python
from kx_mcp_core import make_parent
from kx_mcp_kdbx import build_server as kdbx

app = make_parent("kx-mcp")
app.mount(kdbx(), namespace="kdbx")     # -> kdbx_* tools, tables://kdbx/...
app.run(transport="streamable-http", host="0.0.0.0", port=8000)
```

That glue lives in [server.py](server.py). The equivalent **zero-code** path is the launcher
(`kx-mcp --bundles kdbx`), built from the same seam. There is no container runtime and no
discovery/activation machinery — assembly *is* selection.

The repository is a [uv](https://docs.astral.sh/uv/) workspace of **five installable packages**:

```
packages/kx-mcp-core/   # the container: make_parent + mount + the kx-mcp launcher
packages/kx-auth-core/  # shared, fastmcp-free auth library (config + token verify + outbound/authz seams)
packages/kx-auth-cli/   # the `kx auth` CLI (agent-facing: introspect / login / exchange / assert)
packages/kx-mcp-kdbx/   # the KDB-X backend bundle (+ its own README)
packages/kx-mcp-kdbai/  # the KDB.AI backend bundle (+ its own README)
server.py               # headline glue
examples/host.q         # sample KDB-X host on :5010 for local testing
```

This project doesn't just assemble a server — it **produces modules**. All five packages are published as
versioned wheels to the internal Nexus (KX-internal today), so a downstream project can `uv`/`pip`-install just the pieces
it needs (the container + auth core, plus whichever backend bundles) and assemble its own server in a
few lines, rather than working from this checkout. See [`demos/extending`](demos/extending) for a
worked external-consumer build.

## Available backends

| Backend | Namespace | Status | Details |
| --- | --- | --- | --- |
| KDB-X | `kdbx` | ✅ Available | [packages/kx-mcp-kdbx/README.md](packages/kx-mcp-kdbx/README.md) |
| KDB.AI | `kdbai` | ✅ Available | [packages/kx-mcp-kdbai/README.md](packages/kx-mcp-kdbai/README.md) |

Each backend owns its own prerequisites, dependencies, and configuration — see its README. A backend's
heavy dependencies (e.g. PyKX for kdb-x) are pulled in **only** when that bundle is installed.

## Coming from the standalone kdb-x or KDB.AI server

The **`kdbx`** and **`kdbai`** backends *are* the servers you already know — the OSS
`kdb-x-mcp-server` and `kdbai-mcp-server`, re-shipped as bundles that mount into one container.
Everything you ran before still works; deploying and activating it is simpler.

**Same tools.** The capabilities are unchanged (no regression) — kdb-x still gives you
`run_sql_query`, `similarity_search`, `hybrid_search`, the table-schema resource, and the
table-analysis prompt; KDB.AI still gives you the database/table/query tools plus `similarity_search`
/ `hybrid_search` and session info. They're just namespaced now (`kdbx_*`, `kdbai_*`) so several
backends can coexist. Full inventory: [Backend capabilities](#backend-capabilities).

**What changes — one launch command.** Standalone serving is retired for both backends; you run them
through the container instead of a per-server process:

| Standalone (before) | Container (now) |
| --- | --- |
| the kdb-x MCP server | `uv run kx-mcp --bundles kdbx` |
| the KDB.AI MCP server | `uv run kx-mcp --bundles kdbai` |

Transports are the same choices — add `--transport stdio` for the single-user / Claude-Desktop
posture, or take the default `streamable-http`. See the [Quickstart](#quickstart) to run one now.

**What you gain**

- **One server, both backends.** `uv run kx-mcp --bundles kdbx,kdbai` serves them from a single
  process; their tools appear side by side as `kdbx_*` and `kdbai_*`. No more one process per server.
- **Deploy without a checkout.** A single `uvx` command launches straight from the published wheels
  (KX-internal) — see [Run from the published wheels](#run-from-the-published-wheels-no-checkout).
- **Single-user stays zero-config.** With `KX_MCP_AUTH` unset (the default), the single-principal
  STDIO posture needs no auth setup — same as the standalone servers had. Inbound auth,
  authorization, and outbound identity propagation are there when you want them, entirely opt-in
  (the [auth guide](docs/auth.md)).
- **Only the deps you use.** Installing a bundle pulls its heavy dependencies (PyKX for kdb-x) only
  when that backend is selected.

**Configuration.** Point each backend at its service with its own env prefix — `KDBX_DB_HOST` /
`KDBX_DB_PORT` / … for kdb-x, `KDBAI_DB_*` for KDB.AI (full sets in the
[kdb-x](packages/kx-mcp-kdbx/README.md#configuration) and [KDB.AI](packages/kx-mcp-kdbai/README.md)
backend READMEs). The container itself owns transport, host, and port under `KX_MCP_*`; the backends
no longer carry any serving config of their own.

## Enabling, disabling & configuring backends

**Which backends run** is decided at assembly, two equivalent ways:

- **Launcher (zero-code):** `uv run kx-mcp --bundles kdbx` (or set `KX_MCP_BUNDLES=kdbx`). Add more by
  listing them: `--bundles kdbx,kdbai`.
- **Glue (a few lines):** edit [server.py](server.py) to `mount()` the bundles you want.

**Configuration is split by ownership:**

| Scope | Prefix | Owns |
| --- | --- | --- |
| Container | `KX_MCP_*` | which bundles, transport, host, port, name, and the inbound-auth mode |
| Extension | `KDBX_DB_*` (kdb-x), `KDBAI_DB_*` (kdb.ai), … | that backend's connection & behaviour |

This keeps the container CLI simple — point a client at a local or remote HTTP URL; the backend's
richer configuration lives in its own env-prefixed settings, not on the container command line.

Container launcher flags / env vars:

| Flag | Env var | Default |
| --- | --- | --- |
| `--bundles` | `KX_MCP_BUNDLES` | _(required)_ |
| `--transport` | `KX_MCP_TRANSPORT` | `streamable-http` |
| `--host` | `KX_MCP_HOST` | `127.0.0.1` |
| `--port` | `KX_MCP_PORT` | `8000` |
| `--name` | `KX_MCP_NAME` | `kx-mcp` |
| _(env only)_ | `KX_MCP_AUTH` | `unset` |

**Inbound authentication** (`KX_MCP_AUTH`, env only — keeps the container CLI simple): `unset` (no
auth — the single-principal bundling posture), `static` (RS256 against a local public-key PEM —
`KX_MCP_AUTH_PUBLIC_KEY` or `KX_MCP_AUTH_PUBLIC_KEY_PATH`), `jwks` (RS256 against a remote JWKS
endpoint — `KX_MCP_AUTH_JWKS_URI`; Keycloak / Auth0 / any OIDC issuer), or `entra` (Microsoft Entra
via an OAuth-proxy front door — `KX_MCP_AUTH_CLIENT_ID` / `_CLIENT_SECRET` / `_TENANT_ID`). `static`
and `jwks` also take `KX_MCP_AUTH_ISSUER`, `KX_MCP_AUTH_AUDIENCE`, and optional
`KX_MCP_AUTH_REQUIRED_SCOPES`. When set, parent auth guards every mounted backend (unauthenticated
calls get a spec-compliant `401`) and the validated principal is exposed to backend tools. The
authorization seam is **wired (opt-in)** — the capability check (`@authorize`) and the per-backend
data gates; see [authentication & authorization](docs/auth.md).

## Transports & deployment

> Full operator walkthrough — topology, TLS, secrets, verification, troubleshooting:
> [docs/deployment.md](docs/deployment.md).

Two transports, matching the two deployment topologies:

- **`streamable-http`** (default) — a shared, user-managed server. This is the primary topology and the
  one the auth control plane targets. The server binds `127.0.0.1:8000` by default; do not
  expose it directly — front it with an HTTPS proxy (e.g. [Envoy](https://www.envoyproxy.io/),
  [Nginx](https://nginx.org/)) if remote access is needed.
- **`stdio`** — the single-principal / bundling path, where the client manages the server lifecycle on
  the same host.

The deprecated SSE transport is not supported.

### Zero-config STDIO bundling

In the single-principal posture an MCP client (Claude Desktop / Code) spawns the container as a
STDIO child and owns its lifecycle. "Zero-config" here means **no auth to configure** — inbound and
outbound identity collapse to the developer who launched it — *not* "no CLI args": the client config
names the bundle, exactly as the launcher always has. The spawn recipe is:

```jsonc
// the client config's launch command
uv run kx-mcp --bundles kdbx --transport stdio
```

The container itself starts with no further configuration, but the **backends** still have
environmental prerequisites. An unreachable or unlicensed backend is **disabled with a warning while
the parent keeps serving** (the `try_mount_bundle` "never crash the container" invariant) — so the
spawn never crashes, it may just come up bare:

| Assumption | Required by | Symptom if missing | Resolution |
|---|---|---|---|
| A kdb license | the **`kdbx` bundle's PyKX** (forces `PYKX_LICENSED=true`) | `import pykx` fails → `kdbx` disabled | with a kdb-x install it's auto-resolved relative to the `q` binary (QHOME → `kc.lic`); only set `QLIC` for a standalone PyKX (e.g. CI) |
| Reachable KDB-X + initialized SQL module on `:5010` | `kdbx` tools (qIPC) | pre-flight fails → `kdbx` disabled, parent serves bare | start a KDB-X host (e.g. `q examples/host.q`) |
| Reachable KDB.AI endpoint | `kdbai` tools | pre-flight fails → `kdbai` disabled | configure `KDBAI_DB_*` |

### Running with inbound auth

> The complete auth reference — every inbound mode, outbound strategy, identity assertion, and the
> authorization seam: [docs/auth.md](docs/auth.md).

The shared, multi-principal topology: an HTTP server that **validates a bearer on every request** and
(for backends that support it) **propagates the caller's identity** outbound. There is no special
launch mode — it is the stock launcher plus the `KX_MCP_AUTH*` config (env-only; see the
[auth guide](docs/auth.md#inbound-authentication-kx_mcp_auth)). For RS256 against a live OIDC issuer (Keycloak / Auth0 / Entra),
set `jwks` and point it at the issuer's JWKS:

```bash
export KX_MCP_AUTH=jwks
export KX_MCP_AUTH_JWKS_URI="https://<issuer>/.well-known/jwks.json"   # the issuer's JWKS endpoint
export KX_MCP_AUTH_ISSUER="https://<issuer>"                            # expected iss claim
export KX_MCP_AUTH_AUDIENCE="<audience>"                                # expected aud claim
export KX_MCP_AUTH_RESOURCE_URL="http://127.0.0.1:8000"                 # this server's public URL → advertise discovery

uv run kx-mcp --bundles <backend> --transport streamable-http --host 127.0.0.1 --port 8000
```

A bearer that fails validation is rejected with `401`; a valid one is exposed to the mounted backends
as the request principal. An OAuth-capable extension can then pass it through to its data plane or
exchange it for a backend-scoped credential; KDB.AI implements both supported postures.

> **Client-driven discovery.** Setting `KX_MCP_AUTH_RESOURCE_URL` (this server's public URL) makes the
> container advertise OAuth 2.0 Protected Resource Metadata ([RFC 9728](https://datatracker.ietf.org/doc/html/rfc9728)):
> the `401` carries a `resource_metadata` pointer and `/.well-known/oauth-protected-resource/<path>`
> names the issuer as the `authorization_server`. An MCP client (Claude Code) or `kx auth login` then
> *discovers* the IdP from the server and runs the login itself — no pre-minted bearer. The advertised
> URL must equal the one clients connect to (set it to the public address behind a proxy). Leave it
> unset (the default) for the STDIO / no-public-URL posture: the container then validates bearers
> without advertising discovery.

## Backend capabilities

The tools, resources, and prompts each backend contributes. Names are shown **namespaced**, as the
container exposes them.

### KDB-X backend (`kdbx`)

See [packages/kx-mcp-kdbx/README.md](packages/kx-mcp-kdbx/README.md) for setup and configuration.

**Tools**

| Name | Purpose | Params | Returns |
| --- | --- | --- | --- |
| `kdbx_run_sql_query` | Execute a SQL `SELECT` against KDB-X (write keywords blocked; max 1000 rows) | `query` | JSON query results |
| `kdbx_similarity_search` | Dense-vector similarity search on a table _(needs KDB-X ≥ 0.1.2 + AI libs)_ | `table_name`, `query`, `n?` | Search results |
| `kdbx_hybrid_search` | Hybrid dense + sparse (BM25) search on a table _(needs AI libs)_ | `table_name`, `query`, `n?` | Search results |
| `kdbx_get_table_metadata` | Fetch one table under metadata contract v1 | `table`, `preview_rows?` (0–100) | Semantic schema + live state |
| `kdbx_refresh_metadata` | Reload recompiled aimeta metadata _(admin capability)_ | — | Refresh status + detected tier |

**Resources**

| Name | URI | Purpose |
| --- | --- | --- |
| `kdbx_describe_tables` | `tables://kdbx/all` | Contract-v1 JSON for all visible tables, semantic references, and live samples |
| `kdbx_describe_table` | `tables://kdbx/{table}` | Token-efficient metadata lookup for one table |
| `kdbx_functions` | `functions://kdbx/all` | Public aimeta-documented functions and table dependencies |
| `kdbx_function` | `functions://kdbx/{function}` | Token-efficient metadata lookup for one qualified function |
| `kdbx_metadata_schema` | `schema://kdbx/metadata/v1` | Machine-readable JSON Schema for the agent-facing metadata contract |
| `kdbx_sql_query_guidance` | `file://kdbx/guidance/kdbx-sql-queries` | SQL `SELECT` syntax guidance and examples |

**Prompts**

| Name | Purpose | Params |
| --- | --- | --- |
| `kdbx_table_analysis` | Detailed analysis prompt for a table | `table_name`, `analysis_type?` (`statistical`/`data_quality`), `sample_size?` |

### KDB.AI backend (`kdbai`)

See [packages/kx-mcp-kdbai/README.md](packages/kx-mcp-kdbai/README.md) for setup and configuration.

**Tools**

| Name | Purpose |
| --- | --- |
| `kdbai_list_databases` / `kdbai_database_info` / `kdbai_all_databases_info` | Enumerate databases and their info |
| `kdbai_list_tables` / `kdbai_table_info` | List tables; schema + statistics + indexes for a table |
| `kdbai_query_data` | Structured query (filter / sort / group / aggregate / limit) |
| `kdbai_similarity_search` | Dense-vector similarity search against a named vector index |
| `kdbai_hybrid_search` | Hybrid dense + sparse (BM25) search |
| `kdbai_session_info` / `kdbai_system_info` / `kdbai_process_info` | KDB.AI session / system / process info |

**Resources**

| Name | URI | Purpose |
| --- | --- | --- |
| `kdbai_operations_guidance` | `file://kdbai/guidance/kdbai-operations` | Query / search / hybrid syntax + filter examples |

**Prompts**

| Name | Purpose | Params |
| --- | --- | --- |
| `kdbai_table_analysis` | Detailed analysis prompt for a table | `table_name`, `analysis_type?` (`overview`/`content`/`quality`/`search`), `sample_size?` |

## MCP client configuration

The server works with any MCP-compatible client. Configuration guides:

- [Claude Desktop](mcp-clients/claude-desktop.md) — macOS and Windows
- [GitHub Copilot in VSCode](mcp-clients/github-copilot-vscode.md) — macOS, Linux, Windows, WSL

For other clients, see the [official MCP clients list](https://modelcontextprotocol.io/clients).

## Adding a backend

> Step-by-step tutorial (contract, config fragment, pre-flight, discovery, auth seams, tests):
> [docs/extending.md](docs/extending.md).

A backend is a uv-workspace package `packages/kx-mcp-<name>/` exposing `build_server() -> FastMCP`. To
add one:

1. Create the package and implement `build_server()`, returning a FastMCP server with its primitives
   registered under **bare** names (the container adds the namespace prefix at mount time).
2. Make it **instance-safe** — config, connection, and feature state are owned per `build_server()`
   call, never module globals (so it can be mounted more than once). See the
   [extension contract](docs/extending.md#the-contract).
3. Mount it: add `--bundles <name>` to the launcher, or `app.mount(<name>(), namespace="<name>")` in
   [server.py](server.py).

Within a backend, tools/resources/prompts are auto-discovered: the prescribed layout is a single
`addins/` folder, scanned in one pass via the shared `register_components` helper in `kx-mcp-core`,
with inert copy-me templates named `*.py.template` — start a new primitive from one (see the
[extender guide](docs/extending.md#5-a-tool-addins)). The kdb-x and kdb.ai bundles both use it. Folder
layout is bundle-internal either way — the
extension contract only requires `build_server()`, bare names, and instance-safety. The minimal
worked example is the composition test fixture
[tests/fixtures/kx-mcp-example](tests/fixtures/kx-mcp-example).

## Security considerations

- Run the MCP client, the MCP server, and the backend on the same trusted/internal network to start.
- Encrypt the **backend** connection with TLS where supported (kdb-x: `KDBX_DB_TLS=true` — see the
  [backend README](packages/kx-mcp-kdbx/README.md#configuration)).
- Encrypt the **client** connection by fronting `streamable-http` with an HTTPS proxy; `stdio` needs no
  proxy (same-host pipes).
- **Inbound authentication** is available via `KX_MCP_AUTH` (`static` / `jwks` — see
  [Enabling, disabling & configuring backends](#enabling-disabling--configuring-backends)); it
  defaults to `unset` (no auth — the single-principal bundling posture). Bearer tokens are validated
  in cleartext unless the `streamable-http` endpoint is fronted by an HTTPS proxy (above). Outbound
  identity propagation and the **authorization** seam are **wired (opt-in)** — the capability check
  (`@authorize`) plus the per-backend data gates — see [authentication & authorization](docs/auth.md).

## Testing

```bash
uv run pytest            # or: just test
uv run coverage run && uv run coverage report   # or: just coverage
```

For interactive development/debugging of MCP primitives, the
[MCP Inspector](https://modelcontextprotocol.io/legacy/tools/inspector) and
[Postman](https://learning.postman.com/docs/postman-ai-agent-builder/mcp-requests/create/) are useful.

## Troubleshooting

- **A backend fails to start / is absent from the mounted surface** — most backend startup failures
  (e.g. an unreachable database or missing license) surface as a clear logged error and disable only
  that bundle; the parent continues serving other healthy bundles. See the backend's troubleshooting:
  [KDB-X](packages/kx-mcp-kdbx/README.md#troubleshooting).
- **Container port in use** — change it with `--port` / `KX_MCP_PORT`.
- **No bundles selected** — pass `--bundles` or set `KX_MCP_BUNDLES`.
- **Client-specific issues** — see the [Claude Desktop](mcp-clients/claude-desktop.md#troubleshooting)
  and [GitHub Copilot](mcp-clients/github-copilot-vscode.md#troubleshooting) guides.

## Design & strategy

The container is the seam for KX's agentic auth work — inbound authn, the authorization seam, and
outbound identity propagation. The user-facing reference for all of this is the guide set under
[`docs/`](docs/): [deployment](docs/deployment.md) · [authentication & authorization](docs/auth.md) ·
[writing a backend](docs/extending.md). The deeper strategy and design specs (container shape,
token-exchange, identity-assertion, authorization, and the milestone backlog) are maintained in the
project's internal design docs.

## Useful resources

- [KDB-X documentation](https://docs.kx.com/public-preview/kdb-x/home.htm)
- [KX Forum](https://forum.kx.com/) — community support
- [KX Slack](http://kx.com/slack) — support & feedback
