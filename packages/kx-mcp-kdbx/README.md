# kx-mcp-kdbx — KDB-X backend

The KDB-X backend extension (bundle) for the [KX MCP composition container](../../README.md). It
exposes SQL query, schema-discovery, and vector/hybrid-search capability over qIPC via
[PyKX](https://code.kx.com/pykx/), and is packaged as a standard container bundle: a package exposing
`build_server() -> FastMCP`, mounted under the `kdbx` namespace.

It is **mount-only**: the composition container (`kx-mcp-core`) owns transport/host/port, so the
bundle has no standalone server of its own — `build_server()` just returns a configured FastMCP to
`mount()`. Run it via the container: `uv run kx-mcp --bundles kdbx`.

> The bundle connects to a **KDB-X** service listening on a host and port reachable from the MCP
> server. It relies on the KDB-X **SQL module** through its compatibility interface (`.s`); see
> [setup](#kdb-x-setup).

> **Semantic metadata is a first-class backend capability.** When the target host loads the
> optional [aimeta](https://github.com/KxSystems/aimeta) module, agents receive annotated table,
> column, reference, and public-function semantics through the versioned
> `schema://kdbx/metadata/v1` contract. Without aimeta, the same resources degrade cleanly to native
> table/column discovery. Prefer `tables://kdbx/{table}` or `kdbx_get_table_metadata` and
> `functions://kdbx/{function}` for token-efficient lookups. The administrative
> `kdbx_refresh_metadata` tool is route-only under the default unset authz posture; configured
> capability authz must grant `admin` on the `kdbx` namespace (`kdbx:metadata` is the recorded
> resource convention). See [Semantic metadata with aimeta](#semantic-metadata-with-aimeta).

## Table of contents

- [Supported environments](#supported-environments)
- [Prerequisites](#prerequisites)
- [KDB-X setup](#kdb-x-setup)
- [Semantic metadata with aimeta](#semantic-metadata-with-aimeta)
- [Using the AI search tools](#using-the-ai-search-tools)
- [Configuration](#configuration)
- [Configure embeddings](#configure-embeddings)
- [Running the backend](#running-the-backend)
- [Troubleshooting](#troubleshooting)

## Supported environments

| **Primary OS** | **KDB-X**       | **MCP Server** | **UV/NPX** |
| -------------- | --------------- | -------------- | ---------- |
| **Mac**        | ✅ Local        | ✅ Local       | ✅ Local   |
| **Linux**      | ✅ Local        | ✅ Local       | ✅ Local   |
| **WSL**        | ✅ Local        | ✅ Local       | ✅ Local   |
| **Windows**    | ⚠️ WSL / Remote | ✅ Local       | ✅ Local   |

- **KDB-X**: Mac/Linux/WSL only (no native Windows support — run it under WSL or connect to a remote Linux host). The MCP server can still run natively on Windows and connect to a remote/WSL KDB-X.

## Prerequisites

- **A valid PyKX / KDB-X license.** The backend runs PyKX in **licensed mode** (`PYKX_LICENSED=true`);
  without a valid license `import pykx` fails and the bundle is disabled. Set `QLIC` to your license
  directory (containing a valid `kc.lic`). A valid license has not expired and carries the feature
  flags `pykx`/`py` and `embedq`/`eq` — see the
  [PyKX licensing notes](https://code.kx.com/pykx/4.0/help/troubleshooting.html#accessing-a-license-valid-for-kdb-x-python).
  KDB-X can be installed via the [KDB-X public preview](https://developer.kx.com/products/kdb-x/install).
- **A reachable KDB-X service** on a host and port the MCP server can reach, with the **SQL module**
  initialized (and, for search, the AI libraries — see [setup](#kdb-x-setup)). The bundle's default is
  **`127.0.0.1:5010`** (note: on macOS, `:5000` is taken by Control Center / AirPlay Receiver, where a
  qIPC connect *times out* — hence `:5010`).
- **[UV](https://docs.astral.sh/uv/getting-started/installation/)** to run the server.

The license, qIPC reachability, and initialized `.s` interface are mount-time hard requirements: a
failure disables the `kdbx` bundle while the parent continues serving healthy bundles. `.ai` is a
feature gate—without it the bundle mounts but omits its search tools. Embedding/model configuration
is a call-time prerequisite for those search tools. The sample hosts in `examples/` demonstrate a
complete setup; they are not runtime dependencies.

## KDB-X setup

Start a KDB-X service and load the SQL interface (and, for search, the AI libraries):

```bash
q -p 5010
```

```q
.ai:use`kx.ai     // AI libraries (optional — enables similarity/hybrid search)
.s.init[]         // populate the .s SQL interface used by kdbx_run_sql_query
```

The SQL module uses a special compatibility integration: the installer places `s.k_` on the q
runtime path, preserving `.s.init[]` instead of requiring the normal module-framework load syntax.

A ready-made host for smoke testing lives at [examples/host.q](../../examples/host.q) (loads `.s` +
`.ai`, seeds annotated `instruments` and `trades` tables, listens on `:5010`):

```bash
q examples/host.q
```

## Semantic metadata with aimeta

[aimeta](https://github.com/KxSystems/aimeta) is optional. When the target host loads it, the
backend reads its schema-v2 document over the existing qIPC connection and enriches discovery with
descriptions, semantic types, foreign references, reference vocabularies, public function
signatures, and declared table dependencies. Without aimeta—or when its document is invalid—the
backend stays available and falls back to native `tables[]`/`meta` introspection.

The agent-facing shape is the backend's own stable **metadata contract v1**, not raw aimeta. Every
response points to `schema://kdbx/metadata/v1` and reports a richness `tier`, `annotationStatus`, and
`source`. Tier 1 is native/degraded, Tier 2 is a valid basic aimeta document, and Tier 3 contains
useful semantic annotations. Use `tables://kdbx/{table}` and `functions://kdbx/{function}` for
token-efficient lookups.

First install aimeta and its runtime modules on the host by following its canonical
[installation guide](https://github.com/KxSystems/aimeta/blob/main/docs/install.md). The module must
resolve as `kx/aimeta` under `$QPATH` (normally `~/.kx/mod/kx/aimeta`). Then enable it after defining
the host's tables and functions:

```q
aimeta:use`kx.aimeta;
aimeta[`init][];
```

The module and annotations are host-owned; `kx-mcp-kdbx` neither bundles nor requires aimeta.

## Using the AI search tools

The `kdbx_similarity_search` and `kdbx_hybrid_search` tools require:

- **KDB-X version 0.1.2 or greater**, and
- the AI libraries loaded: `.ai:use\`kx.ai`

When the AI libraries are not present, the backend starts cleanly with only the SQL tool registered
(feature-gated at registration time). When they are present, each search call still needs a
table-specific embedding/model row and the corresponding table data expected by that configuration.

## Configuration

The backend owns the **`KDBX_DB_*`** environment prefix. The container CLI does not carry backend
config; configure the backend via env vars or `.env`. Transport/host/port are **not** the backend's
concern — they belong to the container (`KX_MCP_*`), since the backend is mount-only.

| Setting | Env var | Default | Notes |
| --- | --- | --- | --- |
| Host | `KDBX_DB_HOST` | `127.0.0.1` | KDB-X hostname or IP |
| Port | `KDBX_DB_PORT` | `5010` | qIPC port |
| Username | `KDBX_DB_USERNAME` | _(empty)_ | |
| Password | `KDBX_DB_PASSWORD` | _(empty)_ | Backend connection credential; use this or `KDBX_DB_PASSWORD_FILE`, not both |
| TLS | `KDBX_DB_TLS` | `false` | See TLS note below |
| Timeout | `KDBX_DB_TIMEOUT` | `1` | Connect timeout (seconds) |
| Retry | `KDBX_DB_RETRY` | `2` | Connect retry attempts |
| Embeddings CSV | `KDBX_DB_EMBEDDING_CSV_PATH` | _(packaged `utils/embeddings.csv`)_ | Per-table embedding config |
| Distance metric | `KDBX_DB_METRIC` | `CS` | `CS`, `L2`, `IP` |
| Default `k` | `KDBX_DB_K` | `5` | Default neighbours returned |
| aimeta cache TTL | `KDBX_DB_AIMETA_CACHE_TTL` | `300` | Seconds to cache metadata (including absence); `0` disables caching |
| Identity assertion | `KDBX_DB_ASSERT_IDENTITY` | `false` | Opt-in identity propagation — see below |
| Capability check (PEP-1) | `KX_MCP_AUTHZ=kdbx_rbac` _(container env)_ | _(unset = route-only)_ | Container-side capability gates on the SQL tool (`query` / `kdbx:sql`) and metadata refresh (`admin` / `kdbx:metadata`). Setting `KX_MCP_AUTHZ=kdbx_rbac` routes both decorators to q `.kx.auth` over a *capability* grant set, distinct from the q-side data gate (PEP-2). Requires `KDBX_DB_ASSERT_IDENTITY` so a principal is bound. Unset leaves both tools route-only. |
| Data gate (PEP-2) | `KDBX_DB_DATA_GATE` | `false` | A container-side *explicit consult* of the q data gate: before a query runs, the tool asks `.kx.auth.entitled[action;tables]` (one round-trip, on the bound per-principal handle) for the tables the query references and acts on the verdict — allow, structured `permission_denied`, or **scope-down**: the table listing is filtered to the entitled subset; a partially-entitled SQL query gets a denial naming the entitled tables so the agent re-scopes (SQL is never rewritten). Requires `KDBX_DB_ASSERT_IDENTITY=true` (config-validated) and a `kx.auth` module that ships `entitled` (pre-flight-checked). Off = data gating (if any) happens only via a host-side `.s.e` wrap, not this seam. |
| Password file | `KDBX_DB_PASSWORD_FILE` | _(empty)_ | Read the service-account password from a file (overrides `KDBX_DB_PASSWORD`) — for K8s/Docker mounted secrets |

Resolution order: env vars > `.env` file > defaults.

These defaults are a local-development posture: loopback, plaintext qIPC, empty connection
credentials, identity assertion off, and data/capability gates off. With assertion off the
username/password identify the single backend connection. With assertion on they identify the
trusted service account permitted to assert callers. The built-in Python `static` capability
adapter is independent of `kx.auth`; only `KX_MCP_AUTHZ=kdbx_rbac` and
`KDBX_DB_DATA_GATE=true` require the q authorization module and an asserted principal.

**TLS.** Enable with `KDBX_DB_TLS=true`. This requires your KDB-X database to be
set up for TLS (see the [kdb+ SSL/TLS guide](https://code.kx.com/q/kb/ssl/)). For self-signed certs,
point `KX_SSL_CA_CERT_FILE` at the CA cert; for local development you can bypass verification with
`KX_SSL_VERIFY_SERVER=NO`.

## Identity assertion (multi-principal)

In the multi-principal posture, the container can propagate the **validated inbound principal** to
plain kdb+ so q-side permission functions can gate on *who is calling*. qIPC has no bearer concept, so
identity is **asserted**, not exchanged: the container **ferries** the structured fields + raw claims,
connects as a trusted **service account**, and calls `.kx.auth.bind[principal]`; q's `.kx.auth.promote`
then extracts `groups`/`tenant` and types the principal. No JWT, no crypto, no OAuth in q. Promotion is
q-side so the qIPC and HTTP paths share it. See the [auth guide](../../docs/auth.md) § plain kdb-x
for the full walkthrough.

**Enable it:** set `KDBX_DB_ASSERT_IDENTITY=true` (default off). Off behaves exactly as the
single-principal posture (no bind), so a vanilla kdb+ is unaffected. When on:

1. **Load the `kx.auth` KDB-X module** on your KDB-X process. It's a `use`-loaded module
   ([`modules/kx/auth/`](../../modules/kx/auth/)); install it onto the q module path once with
   `just install-modules` (symlinks it into `~/.kx/mod/kx/auth`), then on the host:
   ```q
   .kx.auth:use`kx.auth;                                       / MUST bind to the global `.kx.auth`
   .kx.auth.configure[(`kxmcp;"service-account-pw")];          / dev/reference verifier; see below
   .kx.auth.setClaims[(enlist `groups)!enlist "realm_access.roles"]; / where to read groups (host owns it)
   .kx.auth.setPolicy[myGrantFn];                              / required — must grant the svc login `assert on `identity (bind is gated by this default-deny policy)
   .kx.auth.activate[];                                        / wire .z.pw / .z.po / .z.pc (qIPC)
   / .kx.auth.activateHttp[];                                  / OPTIONAL: wire .z.ph / .z.pp (thin HTTP)
   ```
   It exposes `bind` / `current` / `valid` / `require` (default-deny on an unbound/expired handle),
   a **data-level authorization seam** (`setPolicy[fn]` installs a `(principal;action;resource) -> 1b`
   decision function, `authorize[action;resource]` enforces it, default-deny until set), and `setClaims`
   to point promotion at the right claim path (default search `groups`→`realm_access.roles`→`roles`).
   `bind` itself consults that same policy — the connecting login needs an `` `assert `` grant on
   `` `identity `` or every assertion is refused (see the
   [module README](../../modules/kx/auth/README.md)).
   The eager pre-flight verifies `.kx.auth.bind` is defined and disables this bundle if not.
2. **Configure one credential source on each side.** On the MCP side, set
   `KDBX_DB_USERNAME` plus either `KDBX_DB_PASSWORD` or `KDBX_DB_PASSWORD_FILE` (the file wins; a
   mounted secret is preferred in production). On q, either use `.kx.auth.configure` as shown for a
   self-contained development/reference host, or keep the platform's existing login verifier
   (`-U` password file or host-defined `.z.pw`). These are alternatives, not cumulative copies of
   the secret. Production deployments should normally preserve platform q authentication and mount
   the MCP-side password as a secret.
   The service-account credentials are the highest-trust secret here—holding them lets the
   container attempt identity assertion, while the policy's explicit `` `assert ``/`` `identity ``
   grant determines whether q accepts it.
3. **TLS:** strongly recommended (the service-account credential + asserted principal otherwise cross
   qIPC in cleartext). The pre-flight only **warns** when TLS is off (keeps the dev/loopback path
   open); treat TLS as required for any production deployment.

Permission-check functions read `.kx.auth.current[]` (or call `.kx.auth.require[]` / `.kx.auth.authorize`);
q's `promote` gives the principal a first-class `groups` symbol vector (extracted from the configured
claim path), so a policy gates on group membership — key on the promoted fields, not raw `claims`. A
q-side denial surfaces to the agent as a structured `permission_denied`. To exercise the ferry + bind
handshake from a shell without the container, use `kx auth assert`.

## Configure embeddings

To use similarity search you configure an embedding model per table. Two providers ship ready to use
(OpenAI and SentenceTransformers); add your own as needed:

1. **Add the provider dependency** to this package's `pyproject.toml` (the optional embedding extras).
2. **Set any required API keys** (e.g. `OPENAI_API_KEY`).
3. **Register a provider** — `src/kx_mcp_kdbx/utils/embeddings.py` defines the `EmbeddingProvider`
   base class; subclass it and decorate with `@register_provider`. Use the OpenAI /
   SentenceTransformers implementations as templates.
4. **Map tables to models** in `src/kx_mcp_kdbx/utils/embeddings.csv` — the provider name there must
   match a registered provider.

## Running the backend

The backend is **mount-only** — run it through the container (there is no standalone `mcp-server`):

```bash
# Run the kdb-x backend via the container:
uv run kx-mcp --bundles kdbx

# Point it at a different KDB-X endpoint with the backend's own env prefix:
KDBX_DB_HOST=kdb-eu KDBX_DB_PORT=5011 uv run kx-mcp --bundles kdbx
```

The bundle registers **bare** primitive names (`run_sql_query`, …); the `kdbx_` qualifier is supplied
by the container's `mount(namespace="kdbx")`.

`kdbx_refresh_metadata` asks aimeta to reload a recompiled document and refreshes this mounted
backend's cache. It carries `@authorize(action="admin", resource="kdbx:metadata")`: authz unset keeps
the single-principal route-only posture, while configured static or kdb-x RBAC must grant the
`admin` capability explicitly.

> **Metadata contract maintenance:** contract v1 is a public interface. Any projection change must
> update the packaged JSON Schema, resource/tool examples and descriptions, contract tests, and the
> changelog in the same change. An incompatible shape requires a new contract version and URI.

## Troubleshooting

- **`Failed to import pykx`** — no valid license. Check `QLIC` points to a directory containing a valid
  `kc.lic` with the required feature flags; update to the latest [KDB-X](https://developer.kx.com/products/kdb-x/install)
  if it has expired.
- **Connection error / bundle disabled** — the KDB-X service is not reachable on the configured
  host:port (default `127.0.0.1:5010`). The bundle logs a clear connectivity error and is not
  mounted; the parent continues serving other healthy bundles.
- **SQL interface not loaded** — run `` .s.init[] `` on the host.
- **Missing AI search tools** — confirm KDB-X ≥ 0.1.2 and `.ai:use\`kx.ai`; check the server logs for
  registration messages.
- **Tier 1 metadata** — install/load `kx.aimeta`, annotate the host source, and compile it. The server
  log's `annotationStatus` distinguishes missing, empty, invalid, and failed metadata.
