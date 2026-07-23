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

## Table of contents

- [Supported environments](#supported-environments)
- [Prerequisites](#prerequisites)
- [KDB-X setup](#kdb-x-setup)
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
  without a valid license `import pykx` fails and the server exits. Set `QLIC` to your license
  directory (containing a valid `kc.lic`). A valid license has not expired and carries the feature
  flags `pykx`/`py` and `embedq`/`eq` — see the
  [PyKX licensing notes](https://code.kx.com/pykx/4.0/help/troubleshooting.html#accessing-a-license-valid-for-kdb-x-python).
  KDB-X can be installed via the [KDB-X public preview](https://developer.kx.com/products/kdb-x/install).
- **A reachable KDB-X service** on a host and port the MCP server can reach, with the **SQL module**
  initialized (and, for search, the AI libraries — see [setup](#kdb-x-setup)). The bundle's default is
  **`127.0.0.1:5010`** (note: on macOS, `:5000` is taken by Control Center / AirPlay Receiver, where a
  qIPC connect *times out* — hence `:5010`).
- **[UV](https://docs.astral.sh/uv/getting-started/installation/)** to run the server.

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
`.ai`, seeds a `trades` table, listens on `:5010`):

```bash
q examples/host.q
```

## Using the AI search tools

The `kdbx_similarity_search` and `kdbx_hybrid_search` tools require:

- **KDB-X version 0.1.2 or greater**, and
- the AI libraries loaded: `.ai:use\`kx.ai`

When the AI libraries are not present, the backend starts cleanly with only the SQL tool registered
(feature-gated at registration time).

## Configuration

The backend owns the **`KDBX_DB_*`** environment prefix. The container CLI does not carry backend
config; configure the backend via env vars or `.env`. Transport/host/port are **not** the backend's
concern — they belong to the container (`KX_MCP_*`), since the backend is mount-only.

| Setting | Env var | Default | Notes |
| --- | --- | --- | --- |
| Host | `KDBX_DB_HOST` | `127.0.0.1` | KDB-X hostname or IP |
| Port | `KDBX_DB_PORT` | `5010` | qIPC port |
| Username | `KDBX_DB_USERNAME` | _(empty)_ | |
| Password | `KDBX_DB_PASSWORD` | _(empty)_ | |
| TLS | `KDBX_DB_TLS` | `false` | See TLS note below |
| Timeout | `KDBX_DB_TIMEOUT` | `1` | Connect timeout (seconds) |
| Retry | `KDBX_DB_RETRY` | `2` | Connect retry attempts |
| Embeddings CSV | `KDBX_DB_EMBEDDING_CSV_PATH` | _(packaged `utils/embeddings.csv`)_ | Per-table embedding config |
| Distance metric | `KDBX_DB_METRIC` | `CS` | `CS`, `L2`, `IP` |
| Default `k` | `KDBX_DB_K` | `5` | Default neighbours returned |
| Identity assertion | `KDBX_DB_ASSERT_IDENTITY` | `false` | Opt-in identity propagation — see below |
| Capability check (PEP-1) | `KX_MCP_AUTHZ=kdbx_rbac` _(container env)_ | _(unset = route-only)_ | A container-side capability gate via the `@authorize(action="query", resource="kdbx:sql")` decorator on the SQL tool. Setting the container's `KX_MCP_AUTHZ` to `kdbx_rbac` routes the check to q `.kx.auth` over a *capability* grant set, distinct from the q-side data gate (PEP-2). Requires `KDBX_DB_ASSERT_IDENTITY` (a principal must be bound for q `require[]`). Unset leaves the tool route-only. |
| Data gate (PEP-2) | `KDBX_DB_DATA_GATE` | `false` | A container-side *explicit consult* of the q data gate: before a query runs, the tool asks `.kx.auth.entitled[action;tables]` (one round-trip, on the bound per-principal handle) for the tables the query references and acts on the verdict — allow, structured `permission_denied`, or **scope-down**: the table listing is filtered to the entitled subset; a partially-entitled SQL query gets a denial naming the entitled tables so the agent re-scopes (SQL is never rewritten). Requires `KDBX_DB_ASSERT_IDENTITY=true` (config-validated) and a `kx.auth` module that ships `entitled` (pre-flight-checked). Off = data gating (if any) happens only via a host-side `.s.e` wrap, not this seam. |
| Password file | `KDBX_DB_PASSWORD_FILE` | _(empty)_ | Read the service-account password from a file (overrides `KDBX_DB_PASSWORD`) — for K8s/Docker mounted secrets |

Resolution order: env vars > `.env` file > defaults.

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
   .kx.auth.configure[(`kxmcp;"service-account-pw")];          / the creds the container connects with
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
   The eager pre-flight verifies `.kx.auth.bind` is defined and **exits clean at startup** if not.
2. **Service-account credentials** (`KDBX_DB_USERNAME` / `KDBX_DB_PASSWORD`, or `KDBX_DB_PASSWORD_FILE`
   for a mounted secret) are the highest-trust secret here — holding them lets the container assert any
   principal, so the kdb+ process trusts the container fully.
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

## Troubleshooting

- **`Failed to import pykx`** — no valid license. Check `QLIC` points to a directory containing a valid
  `kc.lic` with the required feature flags; update to the latest [KDB-X](https://developer.kx.com/products/kdb-x/install)
  if it has expired.
- **Connection error / pre-flight exit** — the KDB-X service is not reachable on the configured
  host:port (default `127.0.0.1:5010`). The server logs a clear connectivity error and exits rather
  than serving a broken backend.
- **SQL interface not loaded** — run `` .s.init[] `` on the host.
- **Missing AI search tools** — confirm KDB-X ≥ 0.1.2 and `.ai:use\`kx.ai`; check the server logs for
  registration messages.
