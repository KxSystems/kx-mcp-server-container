# Deploying the kx-mcp container

How an operator stands up the KX MCP composition container in a real environment — what it takes to
install, what to point it at, how to secure it, and how you'd know afterward that it's healthy.
Configuration of the auth seams themselves is in the [auth guide](auth.md); extending the container
with a new backend is in the [extender guide](extending.md).

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

### 2. From the published wheels (no checkout) — `uvx`

The packages are on PyPI, installable by anyone with no credentials. `uvx` runs the container in an ephemeral environment from one command, straight from PyPI:

```bash
uvx --from kx-mcp-core --with kx-mcp-kdbx kx-mcp --bundles kdbx
```

`--from kx-mcp-core` names the package that owns the `kx-mcp` command (uvx can't infer it from the
executable name); each `--with` adds a backend bundle wheel (`kx-mcp-kdbx`/`-kdbai`, repeat
for several) matching the `--bundles` list.

That unpinned command resolves the latest release, so pin explicitly when you depend on a version
(all packages version in lockstep):

```bash
uvx --from kx-mcp-core==0.5.0 --with kx-mcp-kdbx==0.5.0 kx-mcp --bundles kdbx
```

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
| KDB-X (plain kdb+) | `kx-mcp-kdbx` | **PyKX license** — auto-resolved from a co-located kdb-x install (`q` on `PATH`); otherwise set `QLIC` to the license dir | A KDB-X process with the **SQL module initialized** through its compatibility interface (`.s`), default `127.0.0.1:5010`; AI libs (`.ai`) optional — without them the similarity-search tools are hidden, not broken |
| KDB.AI | `kx-mcp-kdbai` | `kdbai-client>=2.0.0`; no KDB license on the MCP host (the KDB.AI Server deployment owns its license) | A KDB.AI server: qipc default `127.0.0.1:8082`, REST default `127.0.0.1:8081` (`KDBAI_DB_MODE`) |

Requirements fall into three categories. **Mount-time hard requirements** are checked by the eager
pre-flight, and a failure disables just that bundle while the parent keeps serving the healthy ones.
**Feature gates** are narrower — they hide only the affected tools, as `.ai` does for KDB-X's
similarity search. **Call-time prerequisites** are the loosest: the tool stays visible and only
fails, with a useful error, when the data, tables, indexes, embeddings, or a passthrough caller's
credentials it needs turn out to be missing. KDB.AI's static/service-account startup opens an SDK
session; its passthrough startup has no caller bearer yet, so it only checks socket reachability.
Per-backend setup detail lives in:
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
   in themselves. If your IdP restricts dynamic client registration, use `oidc_proxy` instead and
   let the container front the login.
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

**Health probes.** The container serves `GET /health` → `200 OK` on the same port as `/mcp`, for
every composition. It is **unauthenticated** — a kubelet presents no bearer, and a probe behind auth
would restart a healthy pod in a loop. Pass `make_parent(..., health=False)` to opt out.

It is a **liveness** probe: it reports that the process is serving HTTP, not that any backend is
reachable, and a bundle whose pre-flight failed does not turn it red. **Do not wire it to a
`readinessProbe`** — it is green whatever state the backends are in, so it would keep traffic
routed to a pod that cannot serve. There is no readiness endpoint yet.

```yaml
livenessProbe:
  httpGet: { path: /health, port: 8000 }
  periodSeconds: 10
```

For a single-backend deployment set `KX_MCP_EXIT_ON_MOUNT_FAILURE=true` so a failed pre-flight
terminates the process and the orchestrator retries with backoff; without it, a skipped bundle's
tools stay absent until a restart while `/health` stays green. That is only as strong as the
bundle's own pre-flight, which may treat any HTTP response as reachable — a started process does not
by itself prove a *usable* backend. Use the startup log or an MCP `tools/list` to see which backends
came up.

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
| `KX_MCP_EXIT_ON_MOUNT_FAILURE` | `false` | Terminate instead of serving without a backend whose pre-flight failed (see below) |
| `KX_MCP_MOUNT_TIMEOUT` | `0` (off) | Seconds to wait for each backend's startup before serving without it (see below) |
| `KX_MCP_METRICS` / `KX_MCP_TRACING` | _(off)_ | Opt-in Prometheus metrics + OpenTelemetry tracing — see the [observability guide](observability.md) |

Each variable that defaults a launcher flag is validated like the flag itself: an invalid value
(`KX_MCP_TRANSPORT=sse`, a non-numeric `KX_MCP_PORT` or `KX_MCP_MOUNT_TIMEOUT`, an unknown
`KX_MCP_LOG_LEVEL`, a non-boolean `KX_MCP_EXIT_ON_MOUNT_FAILURE`) is a usage error, exit code 2.

**Mount-failure posture.** By default a backend whose eager pre-flight fails is disabled with a
`WARNING` and the container keeps serving the backends that came up — the right behaviour when you
mount several, since one unreachable database shouldn't take the others down. Set
`KX_MCP_EXIT_ON_MOUNT_FAILURE=true` (or pass `--exit-on-mount-failure`) to make the process exit
non-zero instead, so an orchestrator restarts it and the misconfiguration surfaces as a crash loop
rather than a warning line. Recommended when you run **one backend per container**.

**Startup timeout (`KX_MCP_MOUNT_TIMEOUT`, `--mount-timeout`).** Backends mount sequentially, so a
backend that accepts the connection but never answers would otherwise wedge startup indefinitely —
and every backend requested after it never mounts either. Setting this bounds the wait: the container
gives up on that backend and serves the rest.

Prefer a **backend-level** timeout where one exists (`KDBX_DB_TIMEOUT` for kdb-x) — that is the
supported way, and it is why this is off by default. A synchronous startup call cannot be cancelled,
so this option stops the container *waiting* but cannot stop the work: the startup thread is
**abandoned**, and keeps any connection it opened until the process exits. The log line says so when
it happens. A backend given up on this way stays given up on — it can never appear later once its
startup finally completes. Use it as a backstop for a backend you cannot configure, not as the
primary mechanism.

The bound holds only for blocking calls a backend caps by the mount budget the container hands it
(`kx_mcp_core.remaining_mount_budget()`); the kdb-x backend caps its connect this way from this
release. A call that holds the Python interpreter lock (the GIL) and ignores the budget can still
overrun it, because the container cannot even stop waiting until that call returns. The log line
reports the real elapsed time next to the configured bound (`gave up after 32.1s
(KX_MCP_MOUNT_TIMEOUT=3.0s)`), and a backend that finished late is mounted with a warning.

Independently of that flag, requesting bundles and mounting **none** of them always exits non-zero:
a parent with no backends serves no tools, so staying alive would only present a healthy-looking
process with nothing behind it.

The shipped defaults deliberately favor local development: bundle selection is explicit; HTTP binds
to loopback; inbound auth and capability authz are off; backend TLS, identity assertion, and the
KDB-X data gate are off; backend credentials are empty; and KDB.AI uses qipc. Treat these as a
low-friction starting point, not a production security profile.

Backend connection fragments (each owned by its bundle — full tables in the bundle READMEs):

| Backend | Key variables (defaults) |
| --- | --- |
| kdbx | `KDBX_DB_HOST` (`127.0.0.1`) · `KDBX_DB_PORT` (`5010`) · `KDBX_DB_USERNAME`/`_PASSWORD`/`_PASSWORD_FILE` · `KDBX_DB_TLS` (`false`) · `KDBX_DB_TIMEOUT` (`1`) · `KDBX_DB_RETRY` (`2`: retries after a failed connect, so attempts = retry + 1) · `KDBX_DB_ASSERT_IDENTITY` (`false`) · semantic metadata cache: `KDBX_DB_AIMETA_CACHE_TTL` (`300`, `0` disables) · vector-search: `KDBX_DB_EMBEDDING_CSV_PATH`, `KDBX_DB_METRIC` (`CS`), `KDBX_DB_K` (`5`) |
| kdbai | `KDBAI_DB_HOST` (`127.0.0.1`) · `KDBAI_DB_PORT` (mode default: qipc `8082`, REST `8081`; explicit wins) · `KDBAI_DB_MODE` (`qipc`\|`rest`, default `qipc`) · `KDBAI_DB_DATABASE_NAME` (`default`) · `KDBAI_DB_OUTBOUND_STRATEGY` + OIDC detail (see [auth guide](auth.md)) · hybrid-search weights `KDBAI_DB_VECTOR_WEIGHT`/`_SPARSE_WEIGHT` (`0.7`/`0.3`) |

Auth variables (`KX_MCP_AUTH*`, `KX_MCP_AUTHZ*`, outbound strategies): the
[auth guide](auth.md) is the single reference — not duplicated here.

## Verifying a deployment

1. **Liveness**: `curl -f http://<host>:<port>/health` → `200 OK` — no credentials needed even
   with inbound auth on. It proves the process is serving HTTP; steps 2–3 tell you a backend came up.
2. **Startup log**: every mounted bundle's pre-flight lines read `SUCCESS`; the FastMCP banner
   shows your transport/URL.
3. **Handshake**: point any MCP client at `http://<host>:<port>/mcp` and list tools — you should
   see the namespaced surface (`kdbx_run_sql_query`, `kdbai_list_tables`, …).
4. **Auth path** (when inbound auth is on): `kx auth login --server <url>` completes discovery + login, an
   authenticated call succeeds, and an unauthenticated one gets a clean `401` — each dispatch
   emitting one `kx_mcp.audit` line (`subject / action / target / outcome`). `subject` is the token's
   `sub` claim, falling back to its client id and then to `anonymous` — the *same* derivation the
   `@authorize` capability check decides on, so the line always names the identity the decision was
   about. (Before this it recorded only the client id, so on any user token — where `sub` is the
   human and the client id is the app — the record named the app while the decision was about the
   human.)

## Troubleshooting

| Symptom | Cause / fix |
| --- | --- |
| Bundle disabled at startup: connectivity `ERROR` | The backend isn't reachable from the container host — check `*_DB_HOST`/`_PORT`, network path, and that the backend is actually up. The parent continues serving any healthy bundles. |
| kdbx pre-flight: SQL interface `ERROR` | The target q process hasn't initialized the SQL module's compatibility interface — run `.s.init[]` on the host. See `examples/host.q` for the canonical bring-up. |
| kdbx: `import pykx` fails at startup | No resolvable license: put the kdb-x `q` on `PATH` (co-located install) or set `QLIC` to the directory holding `kc.lic`. |
| Similarity-search tools missing from `list_tools` | AI libs (`.ai`) not loaded on the q host — deliberate degradation, not a fault; load `kx.ai` to enable them. |
| KDB.AI search tool is visible but fails | Search prerequisites are call-time: verify the database/table, embedding provider/model, dense index, and—when hybrid—sparse tokenizer/config and sparse index. |
| First KDB.AI passthrough call fails | Startup checked socket reachability only; verify the caller bearer is accepted by KDB.AI and its principal has the required ACL. |
| qIPC connect *times out* on macOS at `:5000` | AirPlay owns `:5000` on macOS and swallows connections — this is why the kdb-x default is `:5010`. Don't deploy a backend on `:5000` on a Mac. |
| `401` on every authed call / client can't log in | Work the inbound checklist in the [auth guide](auth.md#troubleshooting) — issuer/audience mismatch and a missing/wrong `KX_MCP_AUTH_RESOURCE_URL` cover most cases. |
| `uvx`/`uv` reports no solution for `kx-mcp-core`, hinting a pre-release is available | The version you asked for is a pre-release (e.g. `0.5.0b2`) and pre-releases are disabled in that context. Pin it exactly (`kx-mcp-core==0.5.0b2`), pass `--prerelease=allow`, or use a final release. |
| Audit lines don't appear | You're embedding `make_parent` in custom glue without calling `configure_logging()` — the launcher does this for you; custom entry points must too. |
