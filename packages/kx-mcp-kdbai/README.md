# kx-mcp-kdbai — KDB.AI backend

The KDB.AI backend extension (bundle) for the [KX MCP composition container](../../README.md). It
exposes database/table introspection, structured query, and vector + hybrid search over a KDB.AI
service via the [`kdbai-client`](https://pypi.org/project/kdbai-client/) SDK, and is packaged as a
standard container bundle: a package exposing `build_server() -> FastMCP`, mounted under the `kdbai`
namespace.

It is **mount-only**: the composition container (`kx-mcp-core`) owns transport/host/port, so the
bundle has no standalone server of its own — `build_server()` just returns a configured FastMCP to
`mount()`. Run it via the container: `uv run kx-mcp --bundles kdbai`.

> The bundle connects to a **KDB.AI** service. Unlike the kdb-x backend, it does **not** require a
> PyKX license — `kdbai-client` owns connectivity (qIPC or REST).

## Table of contents

- [Prerequisites](#prerequisites)
- [KDB.AI setup](#kdbai-setup)
- [Configuration](#configuration)
- [Configure embeddings](#configure-embeddings)
- [Capabilities](#capabilities)
- [Running the backend](#running-the-backend)
- [Troubleshooting](#troubleshooting)

## Prerequisites

- **A reachable KDB.AI Server** on a host and port the MCP server can reach. The bundle's default is
  **`127.0.0.1:8082`** in **`qipc`** mode; selecting `rest` without an explicit port uses
  **`127.0.0.1:8081`**. See the
  [KDB.AI documentation](https://code.kx.com/kdbai/latest/) to install / run a server.
- **[UV](https://docs.astral.sh/uv/getting-started/installation/)** to run the server.
- **`kdbai-client>=2.0.0`** (pulled in automatically with this bundle). The MCP host needs no
  `QLIC` / PyKX license; the KDB.AI Server deployment owns its own licensing.

## KDB.AI setup

Start (or point at) a KDB.AI Server, then configure the endpoint via `KDBAI_DB_*` (below). Run the
container with this bundle to perform its eager mount-time pre-flight. Static credentials and
`service_account` open a real SDK session; `passthrough` has no caller token at startup and therefore
checks socket reachability only. A failed pre-flight disables this bundle while the parent container
continues serving any healthy bundles:

```bash
uv run kx-mcp --bundles kdbai   # connects to 127.0.0.1:8082 (qipc) by default
```

## Configuration

The backend owns the **`KDBAI_DB_*`** environment prefix. The container CLI does not carry backend
config; configure the backend via env vars or `.env`. Transport/host/port/logging are the container's
concern (the **`KX_MCP_*`** prefix), not this bundle's — the mount-only refactor retired the bundle's
former `KDBAI_MCP_*` serving prefix.

| Setting | Env var | Default | Notes |
| --- | --- | --- | --- |
| Host | `KDBAI_DB_HOST` | `127.0.0.1` | KDB.AI server hostname or IP |
| Port | `KDBAI_DB_PORT` | mode-sensitive | `8082` for qipc, `8081` for REST; an explicit value always wins |
| Username | `KDBAI_DB_USERNAME` | _(empty)_ | |
| Password | `KDBAI_DB_PASSWORD` | _(empty)_ | set when auth is enabled |
| Mode | `KDBAI_DB_MODE` | `qipc` | `qipc` (fast binary) or `rest` (HTTP API) |
| REST protocol | `KDBAI_DB_REST_PROTOCOL` | `http` | `http`/`https`; REST mode only |
| QIPC TLS | `KDBAI_DB_QIPC_TLS` | `false` | TLS for qipc mode; see TLS note |
| Database | `KDBAI_DB_DATABASE_NAME` | `default` | default database for operations |
| Retry | `KDBAI_DB_RETRY` | `2` | connect retry attempts |
| Default `k` | `KDBAI_DB_K` | `5` | default neighbours returned from searches |
| Vector weight | `KDBAI_DB_VECTOR_WEIGHT` | `0.7` | dense weight in hybrid search |
| Sparse weight | `KDBAI_DB_SPARSE_WEIGHT` | `0.3` | sparse (BM25) weight in hybrid search |
| Embeddings CSV | `KDBAI_DB_EMBEDDING_CSV_PATH` | _(packaged `utils/embeddings.csv`)_ | per-table embedding config |

Resolution order: env vars > `.env` file > defaults.

These defaults are a local-development posture: qipc, loopback, plaintext backend transport, empty
static credentials, and no outbound identity strategy. Set transport security and an authentication
strategy explicitly for production.

**TLS (qipc mode).** Enable with `KDBAI_DB_QIPC_TLS=true`. Point `KX_SSL_CA_CERT_FILE` at the CA cert
your TLS proxy uses; for local development you can bypass verification with `KX_SSL_VERIFY_SERVER=NO`.

### Outbound auth — OAuth-protected KDB.AI (`KDBAI_DB_OUTBOUND_*`)

For a KDB.AI server running `AUTH_TYPE=oauth`, set `KDBAI_DB_OUTBOUND_STRATEGY` to one of:

- **`service_account`** — the bundle obtains its own workload-identity bearer (OIDC
  **client-credentials**) through the shared `kx_auth_core.exchange` seam (the same outbound
  implementation shared by OAuth-capable extensions). One identity for the container.
- **`passthrough`** — the bundle forwards the **inbound caller's** validated bearer as the connection
  credential, so the KDB.AI server's ACL enforces on the *end user's* `tenant`/`groups`. Each principal
  gets its own cached `Session` (keyed on the `sub` claim), so users on one OAuth client never share a
  connection.

Both strategies work in **qipc** (bearer as the connection password) and **rest** (bearer handed to
the SDK's file-backed `JWTTokenManager` via a temp `external_token` oauth config). Leave
`KDBAI_DB_OUTBOUND_STRATEGY` empty (default) for the static username/password path (`AUTH_TYPE=static`
/ no auth).

| Setting | Env var | Default | Notes |
|---|---|---|---|
| Strategy | `KDBAI_DB_OUTBOUND_STRATEGY` | _(empty = off)_ | `service_account` (container identity) or `passthrough` (end-user identity) |
| Token URL | `KDBAI_DB_TOKEN_URL` | _(empty)_ | OIDC token endpoint of the IdP (`service_account` only) |
| Client ID | `KDBAI_DB_CLIENT_ID` | _(empty)_ | client-credentials client id, distinct from `KDBAI_DB_USERNAME` (`service_account` only) |
| Client secret | `KDBAI_DB_CLIENT_SECRET` | _(empty)_ | client-credentials secret (`service_account` only) |
| Audience | `KDBAI_DB_AUDIENCE` | _(empty)_ | Keycloak: the token audience (= the server's `OAUTH_CLIENT_ID`) |
| Scopes | `KDBAI_DB_SCOPES` | _(empty)_ | Entra: the resource default scope, e.g. `api://<client-id>/.default` |
| TLS verify | `KDBAI_DB_SSL_VERIFY` | `true` | verify TLS on the outbound token call |

> **Server side (not this bundle):** the KDB.AI server must be configured for OAuth — `AUTH_TYPE=oauth`,
> `OAUTH_CLIENT_ID` (matching the token `aud`), `OAUTH_ISSUERS`, and the tenant/groups claim vars. See
> the KDB.AI [OAuth 2.0](https://code.kx.com/kdbai/latest/use/set-up-oauth2-authentication.html) /
> [Entra ID](https://code.kx.com/kdbai/latest/use/entra_id_oauth_setup_guide.html) setup guides.

### Validating outbound identity against a live KDB.AI

**Validated live 2026-06-18** and committed in `tests/deterministic/realidp/kdbai/` (`tests/docs/TESTING.md`
rows KA.1–KA.9, `just test-kdbai`). Against a real OAuth KDB.AI (`kdbai-db 2.0.0-rc.2`, Community
Edition) with Keycloak, confirmed the following. That server build is the tested configuration, not
a declared minimum server version:

- **qipc-on-pykx-4 data path** — a full create+insert+query round-trip on `pykx 4.0.0b5` +
  `kdbai-client 2.0.0` over qipc (resolves the dependency-override risk in the root CLAUDE.md gotcha —
  the path works, not just imports).
- **`passthrough` end-to-end through the container** — with `KDBAI_DB_OUTBOUND_STRATEGY=passthrough`
  + `KX_MCP_AUTH=jwks`, the inbound bearer is forwarded as the qipc credential and the server
  authenticates the **end user**: granting the `trader` group read on a database, `kdbai_list_tables`
  returned `['T1']` for **alice** (trader) and `[]` for **bob** (viewer) — different results purely by
  propagated identity. No bearer → `401` at the container's inbound gate.
- **passthrough pre-flight fix** — the bundle starts in passthrough mode against an OAuth server via
  a tokenless socket reachability probe; the old anonymous open was rejected → exited. Regression:
  `tests/unit/test_kdbai_server.py::test_passthrough_preflight_is_socket_probe_not_authed_open`.

To run the committed lane:

```bash
cd tests/deterministic/realidp/setup/keycloak
docker login registry.gitlab.com
mkdir -p kdbai-data acl-data && chmod 777 kdbai-data acl-data   # gitignored; kdbai-db runs as 'nobody'
KDB_LICENSE_B64=$(base64 ~/.kx/kc.lic) docker compose --profile backends up -d   # KDBX license (not KXAI)
uv run keycloak_setup.py keycloak_config.json
uv run seed.py                                                 # creates db_read/T1 + grants
cd -                                                           # back to repo root
cp tests/deterministic/realidp/envs/.env.kdbai.example \
   tests/deterministic/realidp/envs/.env.kdbai              # fill in values
just test-kdbai
```

Needs `docker login registry.gitlab.com` + a kdb-x license. Uses `network_mode: host` (Linux-ism;
worked under colima). For a portable setup, replace with explicit port-maps + `host.docker.internal`.

For the **agent-driven demo** (Claude Code as the MCP client, two personas, ACL contrast) and the
**native discovery proof** (RFC 9728 → DCR → auth-code browser flow with no pre-injected token),
see [`demos/claude-code-live-kdbai/`](../../demos/claude-code-live-kdbai/).

## Configure embeddings

The similarity / hybrid search tools embed the query text using a provider configured per
database+table in `src/kx_mcp_kdbai/utils/embeddings.csv`. Two providers ship ready to use (OpenAI and
SentenceTransformers); add your own by subclassing `EmbeddingProvider` in
`src/kx_mcp_kdbai/utils/embeddings.py` and decorating it with `@register_provider`, then map your
database/table rows in the CSV. Set any required API keys (e.g. `OPENAI_API_KEY`) in the environment.

These are call-time prerequisites, not registration-time feature gates: the search tools remain
visible. Similarity search additionally needs an accessible database/table and a matching dense
vector index. Hybrid search needs both dense and sparse indexes plus the configured sparse
tokenizer/model. Missing configuration produces a tool error to fix; it does not disable the bundle.

## Capabilities

Names are shown **bare** (as registered by the bundle); under the container they are namespaced
`kdbai_*` (e.g. `kdbai_query_data`).

**Tools**

| Bare name | Purpose |
| --- | --- |
| `list_databases` | List all databases |
| `database_info` | Info for one database (incl. its tables) |
| `all_databases_info` | Info for all databases |
| `list_tables` | List tables in a database |
| `table_info` | Schema + statistics + indexes for a table |
| `query_data` | Structured query (filter / sort / group / aggregate / limit); strips embedding columns |
| `similarity_search` | Dense-vector similarity search against a named vector index |
| `hybrid_search` | Dense + sparse (BM25) search; weights from `KDBAI_DB_VECTOR_WEIGHT` / `_SPARSE_WEIGHT` |
| `session_info` | KDB.AI session information |
| `system_info` | KDB.AI system information |
| `process_info` | KDB.AI process information |

**Resources**

| Bare URI | Purpose |
| --- | --- |
| `file://guidance/kdbai-operations` | Query / search / hybrid syntax + filter examples |

**Prompts**

| Bare name | Purpose | Params |
| --- | --- | --- |
| `table_analysis` | Detailed analysis prompt for a table | `table_name`, `analysis_type?` (`overview`/`content`/`quality`/`search`), `sample_size?` |

## Running the backend

The bundle is **mount-only** — run it through the container:

```bash
uv run kx-mcp --bundles kdbai            # or: --bundles kdbx,kdbai
```

Configure the backend with `KDBAI_DB_*` env vars (or `.env`) as above. The bundle registers **bare**
primitive names; the `kdbai_` qualifier is supplied by the container's `mount(namespace="kdbai")`.

## Troubleshooting

- **Connection error / bundle disabled** — the KDB.AI Server is not reachable on the configured
  host:port (qipc default `127.0.0.1:8082`; REST default `127.0.0.1:8081`). The bundle logs a clear
  connectivity error and is not mounted; the parent continues serving other healthy bundles.
- **Authentication error** — auth is enabled on the server; set `KDBAI_DB_PASSWORD` (and
  `KDBAI_DB_USERNAME`).
- **qipc + TLS** — set `KX_SSL_CA_CERT_FILE` (or `KX_SSL_VERIFY_SERVER=NO` for local dev).
- **First passthrough call fails authentication/authorization** — startup checked reachability only;
  verify the inbound bearer is valid for KDB.AI and that its principal has the necessary server ACL.
- **Search returns no embeddings / provider errors** — confirm the table has a row in
  `embeddings.csv`, the provider's API key is set, and the required dense (and, for hybrid, sparse)
  indexes exist.
