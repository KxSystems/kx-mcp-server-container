# Configuring observability — metrics & tracing

The reference for making a kx-mcp container **observable**: **metrics** (how many dispatches, how
long, what outcome) and **tracing** (one span per dispatch, exported to a collector). Both seams are
opt-in and independently configured, so you can adopt them incrementally — start with nothing, and
turn on one at a time.

If you are deploying rather than configuring observability specifically, start with the
[deployment guide](deployment.md); for securing the container see
[authentication & authorization](auth.md).

## The zero-setup default

With nothing configured, the container emits **no metrics and no spans** — nothing scraped, no
exporter, no extra HTTP route, no middleware attached, and the OpenTelemetry SDK is never even
imported:

```bash
uv run kx-mcp --bundles kdbx --transport stdio
```

No `KX_MCP_METRICS`, no `KX_MCP_TRACING`. A default deployment behaves exactly as it did before this
feature existed, including under `stdio`. Everything below is what you add when you want telemetry.

> **Scope: HTTP transports only for the scrape endpoint.** The Prometheus endpoint is a route on the
> container's *own* ASGI app, so it exists only under `--transport streamable-http` (or `http`).
> Under `stdio` there is no network listener at all: the container logs a clear warning and skips the
> route rather than failing. The dispatch counters are still collected in-process, and **tracing works
> under every transport** (it pushes to a collector rather than being scraped).

> **One asymmetry worth knowing, since it is deliberate.** Tracing's SDK is genuinely lazy — nothing
> `opentelemetry`-shaped is imported until `KX_MCP_TRACING` turns the seam on. The metrics library is
> not: `prometheus-client` is a hard dependency of `kx-mcp-core`, and importing the package registers
> the two dispatch collectors on the process-global registry whatever `KX_MCP_METRICS` says. Nothing is
> collected (the middleware is not attached) and nothing is exposed (no route is mounted), so the
> observable behaviour is unchanged — but the import and the registration do happen.
>
> That is a decision, not an oversight. Prometheus's registry *is* process-global, so the collectors
> are process-scoped by nature; and `prometheus-client` is pure Python with no transitive tail worth
> deferring, unlike the OTLP exporter's gRPC/protobuf weight, which is the whole reason the `tracing`
> extra exists. The trigger to revisit it would be a deployment that needs a `prometheus-client`-free
> install; because bundles reach metrics through `kx_mcp_core.observability` rather than importing the
> library themselves, moving it behind an extra later would not break them.

## The two seams at a glance

```
                       ┌── metrics ──▶ GET /metrics  (scraped by Prometheus)
MCP client ──▶ [ dispatch: tool / resource / prompt ]
                       └── tracing ──▶ span ──(OTLP)──▶ collector (Tempo / Jaeger / …)
```

| Seam | Selector variable | Default | Modes |
| --- | --- | --- | --- |
| Metrics | `KX_MCP_METRICS` | `""` (off) | `""` · `prometheus` |
| Tracing | `KX_MCP_TRACING` | `""` (off) | `""` · `otlp` |

The selector is the **bare** variable (`KX_MCP_METRICS`, not `KX_MCP_METRICS_MODE`); mode-specific
detail lives under the matching prefix (`KX_MCP_METRICS_*`, `KX_MCP_TRACING_*`) — the same convention
`KX_MCP_AUTH` and `KX_MCP_AUTHZ` follow. Empty and whitespace values collapse to off. An
**unrecognised** mode (the typo `KX_MCP_METRICS=prometheous`) is refused at startup rather than
silently leaving the seam off.

---

## Settings

| Variable | Default | Meaning |
| --- | --- | --- |
| `KX_MCP_METRICS` | `""` | `""` = off. `prometheus` = collect counters/histograms **and** serve the scrape endpoint. |
| `KX_MCP_METRICS_PATH` | `/metrics` | Where the scrape endpoint is mounted. A missing leading `/` is added for you. |
| `KX_MCP_TRACING` | `""` | `""` = off. `otlp` = export one span per dispatch over OTLP. |
| `KX_MCP_TRACING_OTLP_ENDPOINT` | _(SDK default)_ | Collector address spans are pushed to, e.g. `http://collector:4317`. Unset falls back to the OTel SDK's own default (`localhost:4317`) and honours the standard `OTEL_*` env vars. |
| `KX_MCP_TRACING_SERVICE_NAME` | `kx-mcp` | The `service.name` resource attribute stamped on every span. |

---

## Metrics (`KX_MCP_METRICS=prometheus`)

```bash
export KX_MCP_METRICS=prometheus
# export KX_MCP_METRICS_PATH=/internal/metrics    # optional

uv run kx-mcp --bundles kdbx,kdbai --transport streamable-http --host 127.0.0.1 --port 8000
```

Then scrape it:

```bash
curl -s http://127.0.0.1:8000/metrics | grep kx_mcp
```

### What is collected

| Metric | Type | Labels | Meaning |
| --- | --- | --- | --- |
| `kx_mcp_dispatches_total` | counter | `tool_name`, `outcome` | One increment per tool call, resource read, or prompt get. |
| `kx_mcp_dispatch_duration_seconds` | histogram | `tool_name` | Wall-clock duration of the dispatch, recorded for **every** outcome including failures. |

`tool_name` is the dispatch target as the client asked for it — the **namespaced** tool/prompt name
(`kdbx_run_sql_query`, `kdbai_query_data`) or the resource URI (`tables://kdbx/all`). Because the
middleware attaches to the parent and the namespace prefix is applied at `mount()`, metrics split per
backend with no backend-specific configuration.

The duration histogram uses `prometheus-client`'s standard bucket boundaries (`0.005 … 10.0`, plus
`+Inf`), so `histogram_quantile()` behaves exactly as it would against any other Prometheus
histogram.

The scrape also carries `prometheus-client`'s own `python_*` collectors and — **on Linux only** —
its `process_*` collectors, since the metrics live in its default registry. See
[§ Process and runtime metrics](#process-and-runtime-metrics) for the full list and for the
platform caveat, which is a silent one.

`outcome` is the same three-way split the [audit line](auth.md#audit) records, read from the same
decision, so a metric and an audit line never disagree:

| `outcome` | When |
| --- | --- |
| `ok` | The dispatch succeeded. |
| `denied` | An authorization refusal — a capability check (`@authorize`, PEP-1), or a backend data gate (PEP-2) whether it raised or returned a structured `permission_denied`. |
| `error` | Any other failure: an exception escaping the dispatch, **or** a result the tool marked `isError: true`. |

A useful consequence: `rate(kx_mcp_dispatches_total{outcome="denied"}[5m])` is an authorization-refusal
rate per tool, and the `error` series is a genuine fault signal that a policy deny will never inflate.

### What `outcome` can and cannot see

Middleware observes the *dispatch*, not a backend's payload conventions. It classifies from exactly
two signals: whether the dispatch raised, and the protocol-level `isError` flag on the result (plus
the authz decision, which promotes a failure to `denied`). It deliberately does **not** inspect
payload fields — `{"status": "error"}` is a kdbx/KDB.AI spelling, not a protocol fact, and a
third-party bundle may use its own.

So a tool that fails *without* setting `isError` is recorded as `ok`. That is why setting the flag is
a **required** part of the extension contract rather than a suggestion — see
[extending.md § Signalling failure](extending.md#signalling-failure-required). The shipped kdb-x and
KDB.AI backends comply, and a CI guard fails a new tool that does not, so `outcome="error"` is
trustworthy for every tool this container serves. If you mount a **third-party** bundle, that
guarantee is only as good as that bundle's compliance: an under-reporting bundle shows up as a tool
whose `error` series stays empty while its callers see failures.

> **Label cardinality — worth a look if you serve templated resources.** Tool and prompt names come
> from a fixed set, so their label values are bounded. **Resource URIs are not**: a
> [resource template](extending.md) read is labelled with the *concrete* URI, so a template like
> `tables://kdbx/{name}` yields one series per distinct value. That is fine for a handful of tables and
> a problem for a high-cardinality parameter (an id, a timestamp). If you expose such a template, drop
> or rewrite the label at the scrape config (Prometheus `metric_relabel_configs`) rather than letting
> the series count grow without bound.

> **The scrape endpoint is unauthenticated — protect it at the network layer.** `/metrics` carries no
> authentication check, even on a container with `KX_MCP_AUTH` configured for everything else: the
> route is added through FastMCP's `custom_route`, which sits outside the configured `AuthProvider`'s
> scope. Anyone who can reach the container's port can read the full tool inventory, per-tool call
> volumes, denial rates, and latency profiles, with no token. That is the standard posture for
> Prometheus-style scrape endpoints — conventionally kept private by a firewall rule, a private subnet,
> or a security group rather than by an application-layer check — but it does mean the protection has
> to cover the whole port: there is no way to bind `/metrics` to a different address from the main
> listener. Ensure it, like the rest of the container's HTTP port, is not reachable from untrusted
> networks.

### Under `stdio`

Requesting metrics with `--transport stdio` logs a warning and skips the route:

```
metrics requested (KX_MCP_METRICS=prometheus) but transport 'stdio' serves no HTTP endpoint —
the /metrics scrape route is not mounted; dispatch metrics are still collected in-process.
Use --transport streamable-http (or http) to expose them.
```

The container starts and serves normally — degrade with a warning, never crash, the same posture
`try_mount_bundle` takes for a backend that cannot come up.

---

## Tracing (`KX_MCP_TRACING=otlp`)

**Tracing needs an optional extra.** The OpenTelemetry SDK and OTLP exporter are not part of a
default install — they ship as the `tracing` extra, so a deployment that never traces does not carry
the exporter's gRPC/protobuf tail:

```bash
pip install 'kx-mcp-core[tracing]'
# uvx: uvx --from 'kx-mcp-core[tracing]' --with kx-mcp-kdbx kx-mcp --bundles kdbx
# from a checkout, the workspace dev install already includes it: uv sync
```

Setting `KX_MCP_TRACING=otlp` without the extra is **not** fatal: the container logs a warning naming
the extra and serves without traces, exactly as it does for any other tracing setup failure.

```bash
export KX_MCP_TRACING=otlp
export KX_MCP_TRACING_OTLP_ENDPOINT=http://collector:4317
export KX_MCP_TRACING_SERVICE_NAME=kx-mcp-prod

uv run kx-mcp --bundles kdbx --transport streamable-http --host 127.0.0.1 --port 8000
```

One span per dispatch, named `mcp.tool_invoke` / `mcp.resource_read` / `mcp.prompt_get`, carrying:

| Attribute | Meaning |
| --- | --- |
| `mcp.action` | `tool_invoke` · `resource_read` · `prompt_get` |
| `mcp.target` | The namespaced tool/prompt name, or the resource URI |
| `mcp.outcome` | `ok` · `denied` · `error` (same vocabulary as the metrics counter) |
| `mcp.authz.decision` | `allow` / `deny` — only when the dispatch ran a capability check |
| `mcp.authz.adapter` | Which authz adapter decided (e.g. `static`, `kdbx_rbac`) |

A failed dispatch records the exception as a span event and sets the span status to `ERROR`; the span
is always closed, on every exit path. A *denied* dispatch is also marked `ERROR` (with reason
`authorization denied`) so it does not read as a clean success in a trace UI.

Unlike metrics, tracing needs no HTTP route, so it works under **every** transport including `stdio`.

> **You will also see FastMCP's own spans.** FastMCP 3.x is itself OpenTelemetry-instrumented, and its
> instrumentation activates as soon as a `TracerProvider` is installed — so a trace contains its spans
> (`tools/list`, `tools/call <name>`, `delegate <name>`) *alongside* the container's `mcp.*` span for
> the same dispatch. That is useful rather than redundant: FastMCP's spans show the protocol and
> mount-delegation layers, while the `mcp.*` span is the one carrying the container's outcome and
> authorization attributes. Filter on `name = "mcp.*"` (or on the presence of `mcp.target`) when you
> want only the container's view. Note FastMCP's own error spans record the exception event twice; the
> container's `mcp.*` spans record it once.

**The OpenTelemetry SDK is imported lazily** — only when `KX_MCP_TRACING=otlp` actually turns the seam
on. A deployment with tracing off never pays for the SDK, exporter, or gRPC import (the same deferral
`KX_MCP_AUTHZ=static` uses for its YAML policy adapter). If exporter setup fails — a bad
configuration, a missing optional dependency — the container logs a warning and serves **without**
traces rather than refusing to start. Building the exporter never contacts the collector, so an
unreachable endpoint is not a setup failure: at startup a best-effort probe (a short TCP connect on a
background thread, so it never delays startup) logs `OTLP endpoint … is unreachable … serving without
traces until it becomes reachable`, and the exporter's own warnings appear as spans are dropped.
Tracing never blocks serving; telemetry is never worth an outage.

---

## Process and runtime metrics

Beyond the per-dispatch metrics, the scrape carries process- and runtime-level series. These need no
extra configuration — they ride the same `KX_MCP_METRICS=prometheus` switch, because they are cheap
and a second toggle would buy an operator nothing.

### What `prometheus-client` provides, and the platform trap

Because the collectors live in `prometheus-client`'s default registry, its own auto-registered
collectors are already in the scrape:

| Family | Source | Note |
| --- | --- | --- |
| `python_info` | `PlatformCollector` | Interpreter version. Every platform. |
| `python_gc_objects_collected_total`, `python_gc_collections_total`, `python_gc_objects_uncollectable_total` | `GCCollector` | Per generation. Every platform. |
| `process_resident_memory_bytes`, `process_virtual_memory_bytes` | `ProcessCollector` | **Linux only.** |
| `process_cpu_seconds_total`, `process_start_time_seconds` | `ProcessCollector` | **Linux only.** |
| `process_open_fds`, `process_max_fds` | `ProcessCollector` | **Linux only.** Worth watching: every qIPC handle and HTTP connection is an fd. |

> **`ProcessCollector` fails silently off Linux.** It reads `/proc`, so on macOS its constructor
> raises, the error is swallowed, and it then yields **no metric families at all** — the collector is
> registered and permanently empty. So `process_resident_memory_bytes` is present in a Linux
> container and simply absent on a developer's Mac, with no warning either way. If you are looking
> for memory on a laptop and finding nothing, this is why, and the seam is not broken.
>
> We deliberately do **not** add `psutil` to paper over this. The production target is a Linux
> container where the free collectors already work, so a dependency would buy dev-machine
> convenience only.

### What the container adds

| Metric | Type | Labels | Meaning |
| --- | --- | --- | --- |
| `kx_mcp_dispatches_in_progress` | gauge | `tool_name` | Dispatches currently in flight. |
| `kx_mcp_event_loop_lag_seconds` | histogram | — | How late a scheduled event-loop wake-up arrived. |
| `kx_mcp_threads` | gauge | — | Live threads in the process. |
| `kx_mcp_build_info` | info | `version` | Version of the running container. |

**`kx_mcp_dispatches_in_progress`** is the concurrency signal a counter and a histogram cannot give
you between them: they say how many dispatches happened and how long each took, never how many were
in flight at once, so a pile-up is invisible. It is sampled at the same middleware seam as the other
two, and decremented in a `finally`, so it returns to zero on every exit path including failures.

**`kx_mcp_build_info`** reports the `kx-mcp-core` version. A local build reads `0.0.0` — that is
`hatch-vcs`'s fallback when no git tag is reachable, not a fault; CI stamps the real version.

**`kx_mcp_threads`** is coarse on purpose. It covers the worker pool behind sync tool bodies, the
threads `asyncio.to_thread` uses for embedding-model inference, and the executor behind outbound
token minting, without needing to know which pool grew.

### Reading event-loop lag

Lag is the overshoot of a scheduled wake-up: the sampler asks to be woken in one second and records
how much later it actually was. Anything holding the loop — a synchronous backend round-trip, model
inference on the wrong thread, a long GC pause — appears here as time the loop could not hand back,
which is exactly the delay a *concurrent* request waits.

Measured on the observability demo stack, the healthy/contended split is clean:

| State | Where the ticks land |
| --- | --- |
| Idle | `le="0.005"` — sub-5ms |
| Under sustained tool load | mostly `0.005 < lag ≤ 0.05`, mean ~4ms |

So `histogram_quantile(0.9, sum by (le) (rate(kx_mcp_event_loop_lag_seconds_bucket[5m])))` climbing
above a few tens of milliseconds means requests are queueing behind loop-bound work.

**What it is not.** The sampler ticks once a second, so it measures the *duty cycle* of blocking
rather than catching any individual block. A single 25ms stall between ticks is invisible; sustained
blocking shows up reliably because ticks keep landing inside it. Treat lag as a stall-and-saturation
detector, not a per-request measurement — for per-request cost, use
`kx_mcp_dispatch_duration_seconds` against the backend duration histograms.

**Why lag and not GIL contention.** There is no cheap, stable way to measure GIL wait time from
inside CPython, and for an asyncio server it is the wrong question: what degrades this container is
blocking work on the loop thread, whoever holds the GIL. Lag measures that directly.

### The signal these three make together

This container is a **single process with a single event loop**, and its backend calls are
synchronous: a qIPC round-trip or a KDB.AI SDK call runs on the loop thread without yielding. So
concurrent requests largely serialize behind each other.

That shows up as a specific, recognisable shape:

- `kx_mcp_dispatches_in_progress` rises with offered load,
- `kx_mcp_dispatch_duration_seconds` rises *with it* — each request's wall-clock includes waiting for
  the ones ahead of it,
- while the backend's own duration (`kdbx_qipc_duration_seconds`) stays flat, because the database is
  not the slow part,
- and `kx_mcp_event_loop_lag_seconds` shifts up out of its idle bucket.

Four requests in flight each taking four times as long as one, with a flat backend histogram, is
queueing rather than a slow backend — and no amount of database tuning will move it. Measured on the
demo stack: six concurrent copies of one 25ms query took 4.1x a single one, and a `/metrics` scrape
issued during that window took 3.1x its idle latency, because the scrape shares the loop.

## Backend instrumentation

The boundary middleware above times the *dispatch*. The shipped backends additionally instrument
their own outbound calls, which is what makes the interesting subtraction possible: **dispatch
duration minus backend duration is the container's own overhead.** Without it, a slow tool call
tells you nothing about whether the database, an embedding provider, or the container is at fault.

Nothing here needs enabling separately — these emit under the same `KX_MCP_METRICS` /
`KX_MCP_TRACING` switches, and are inert when those are off.

### The cardinality rule

**Unbounded dimensions go on spans, never on metric labels.** Every label below is drawn from a
fixed vocabulary decided in our own source. Table names, database names, index names, SQL and search
text, and the principal are deliberately *absent* from labels: they are client-supplied, so as label
values they let a caller mint unbounded series — a Prometheus cardinality explosion is a memory
problem for whoever runs the scrape, not just untidy data. On a span the same values are free,
because a trace is per-request and sampled.

This is why there is no `table` label anywhere below, even though "slowest table" is an obvious thing
to want. Ask that question of traces.

### kdb-x

| Metric | Type | Labels | Meaning |
| --- | --- | --- | --- |
| `kdbx_qipc_calls_total` | counter | `op`, `outcome` | One increment per qIPC round-trip. |
| `kdbx_qipc_duration_seconds` | histogram | `op` | Duration of a qIPC round-trip. |
| `kdbx_connects_total` | counter | `outcome` | Connection establishment attempts (`ok` / `failed`), including the startup pre-flight and reopening a handle found dead. |
| `kdbx_reconnects_total` | counter | — | A cached handle was found dead and re-established; a reopen that fails is counted as `kdbx_connects_total{outcome="failed"}` instead. |
| `kdbx_sql_result_rows` | histogram | — | Rows *matched* by a SQL query, before the response cap. |
| `kdbx_sql_truncated_total` | counter | — | Responses capped at `MAX_ROWS_RETURNED` (1000). |
| `kdbx_authz_consults_total` | counter | `adapter`, `action`, `decision` | q-side authorization consults. |
| `kdbx_embed_duration_seconds` | histogram | `provider`, `kind` | An embedding-provider call. |

`op` is the round-trip's role, not its q expression: `connect`, `probe`, `bind`, `authorize`,
`entitled`, `preflight` (the startup checks), `sql`, `tables`, `meta`, `rowcounts`, `partitioned`, `preview`, `search`,
`hybrid_search`, `aimeta.probe`, `aimeta.fetch`, `aimeta.reload`. Spans are named `kdbx.<op>`.

`decision` is `allow`, `deny`, or **`partial`** — the last being a scope-down, where the data gate
entitled a strict subset of the tables a query referenced. That series is the one worth alerting on:
it means agents are routinely asking for more than they are entitled to see.

Two of these answer questions nothing else in the stack does. `kdbx_sql_truncated_total` says agents
are hitting the 1000-row cap, which is a product signal rather than an operational one. And reading
`tables://kdbx/all` issues **one `preview` round-trip per visible non-empty table** (for the first 20
tables), plus one batched `meta` for all the tables aimeta does not annotate, three fixed ones
(`tables`, `rowcounts`, `partitioned`) and the connection `probe`. A three-table, un-annotated
document is therefore **eight** round-trips: 3 + 1 + 3 + 1. It costs more when:

- a table's columns are missing from the batched `meta`, which adds one `meta` for that table;
- a preview hits float infinities, which adds a second `preview` for that table;
- the aimeta cache is cold, which adds `aimeta.probe`, plus `aimeta.fetch` where aimeta is loaded;
- identity assertion or the data gate is on, which adds their `bind` / `entitled` calls.

The dispatch histogram reports all of this as one aggregate number;
`kdbx_qipc_calls_total{op="preview"}` climbing much faster than the dispatch count is that fan-out.

### KDB.AI

| Metric | Type | Labels | Meaning |
| --- | --- | --- | --- |
| `kdbai_sdk_calls_total` | counter | `op`, `outcome` | One increment per `kdbai_client` call. |
| `kdbai_sdk_duration_seconds` | histogram | `op` | Duration of a `kdbai_client` call. |
| `kdbai_connects_total` | counter | `outcome` | Session establishment attempts. |
| `kdbai_cached_sessions` | gauge | — | Sessions currently cached. |
| `kdbai_token_mints_total` | counter | `outcome` | Service-account tokens minted. |
| `kdbai_token_mint_duration_seconds` | histogram | — | Duration of a mint against the token endpoint. |
| `kdbai_result_rows` | histogram | — | Rows returned by a query or search. |
| `kdbai_embed_duration_seconds` | histogram | `provider`, `kind` | An embedding-provider call. |

`op` values: `connect`, `resolve_table`, `list_databases`, `databases_info`, `database_info`,
`list_tables`, `table_info`, `query`, `search`, `hybrid_search`, `session_info`, `system_info`,
`process_info`. Spans are named `kdbai.<op>`, plus `kdbai.token_mint`.

**`kdbai_cached_sessions` is the one to watch under `passthrough`.** That strategy partitions the
session cache per principal, so the gauge tracks distinct callers — and a cache that only ever grows
is how a per-principal cache becomes a leak. Under `service_account` or `static` it sits at 1 per
configured backend and is uninteresting.

`kdbai_token_mint_duration_seconds` makes IdP latency visible on the request path. A mint is cached
until 60s before expiry, so a mint rate near the dispatch rate means the cache is not working.

### Embedding calls are timed separately, on purpose

`*_embed_duration_seconds` covers the dense/sparse embedding call a vector search makes before it
reaches the database. It may be a remote API (OpenAI) or local model inference, it is usually the
highest-variance leg of a search, and folding it into the search's own time makes a slow embedding
provider look like a slow database. Both backends time it at their provider factory, so every
add-in gets it without asking.

---

## Where the seams attach

Both are parent middleware, attached in `make_parent` exactly as inbound auth and the audit line are:

```python
from kx_mcp_core import ObservabilitySettings, make_parent, mount_metrics_route

settings = ObservabilitySettings()                       # reads KX_MCP_METRICS / KX_MCP_TRACING
app = make_parent("kx-mcp", observability=settings)
# ... mount bundles ...
mount_metrics_route(app, settings, "streamable-http")    # no-op unless metrics are on + HTTP
app.run(transport="streamable-http", host="0.0.0.0", port=8000)
```

The `kx-mcp` launcher does all of this for you; the snippet is only needed for hand-written glue (see
[server.py](../server.py), or [demos/extending/server.py](../demos/extending/server.py) for the
single-backend consumer shape). Three things worth knowing:

- **One attach point covers every backend.** Middleware on the parent sees each mounted bundle's
  dispatches, already namespaced — so adding a backend needs no observability work.
- **The scrape route is mounted separately** from `make_parent`, because it depends on the transport,
  which only the entry point knows.
- **Glue owns both halves.** `make_parent(observability=…)` alone collects nothing to scrape; the
  `mount_metrics_route` call alone has no middleware feeding it. Omit either and `KX_MCP_METRICS` is
  silently inert — the symptom is a 404 on `/metrics` with no warning, because as far as the container
  knows you never asked for metrics.

**Instrumenting inside a backend?** Bundles reach metrics and tracing through
`kx_mcp_core.observability` (`metric`, `span`, and the collector classes) rather than importing
`prometheus_client` or `opentelemetry` directly — see
[extending.md § Observability for bundle authors](extending.md#observability-for-bundle-authors).

Everything above is proven against a real launcher subprocess and a real kdb+ backend, not just
in-process, by `tests/deterministic/integration/test_observability_metrics_e2e.py`.

## Troubleshooting

| Symptom | Likely cause |
| --- | --- |
| `GET /metrics` returns 404 | Metrics are off (`KX_MCP_METRICS` unset), or the path was changed with `KX_MCP_METRICS_PATH`. Check the startup log for `metrics endpoint mounted at …`. |
| Startup warns "serves no HTTP endpoint" | Metrics requested under `stdio`. Switch to `--transport streamable-http`, or drop `KX_MCP_METRICS` for the stdio posture. |
| Container refuses to start on a mode error | An unrecognised `KX_MCP_METRICS` / `KX_MCP_TRACING` value — the message lists the accepted modes. This is deliberate: a typo must not silently disable telemetry. |
| `/metrics` is served but a tool never appears | Counters are created on first use, so a tool that has never been dispatched has no series yet. Call it once. |
| No spans reach the collector | Check `KX_MCP_TRACING_OTLP_ENDPOINT` is reachable *from the container*, and look at startup for the `OTLP endpoint … is unreachable` warning (a best-effort TCP check; it cannot tell that a reachable port is not an OTLP collector) and for the exporter's own warnings. Spans are batched, so allow a few seconds. |
| Startup warns `the OpenTelemetry tracing extra is not installed` | Tracing was requested but the extra is absent. Install it (`pip install 'kx-mcp-core[tracing]'`) and restart. The container keeps serving without traces meanwhile — and attaches no tracing middleware at all, so there is no per-dispatch cost either. This message is distinct from the generic `setup failed` one, which means the extra *is* present but something else broke (typically the endpoint). |
| `/metrics` 404s from hand-written glue even with `KX_MCP_METRICS=prometheus` | The glue built the parent itself and skipped one of the two required calls — see *Where the seams attach*. The `kx-mcp` launcher does both. |
| Metrics/tracing add no `denied` series | The authorization seam may be route-only — see the [auth guide](auth.md#authorization-kx_mcp_authz). Route-only dispatches record `ok`. |
| A tool is clearly failing but its `error` series stays empty | The tool is not setting `isError: true` on its failures, so the dispatch is classified `ok` — see [What `outcome` can and cannot see](#what-outcome-can-and-cannot-see). Expected only for a non-compliant third-party bundle; the shipped backends are guarded in CI. |
| A denial is counted `error` rather than `denied` | The refusing layer did not stamp the authz decision. If it is your own tool: a **sync** (`def`, not `async def`) tool body runs in a worker thread with a copied context, so a `contextvar` set inside it never reaches the middleware. Make the tool `async def`. |
