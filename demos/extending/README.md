# extending — assemble your own MCP server from the container wheels

A self-contained **example consumer project**: how a downstream repo stands up its own MCP server
from the container's packages — pulling `kx-mcp-core` (the assembly seam + the `kx-mcp` launcher) and
`kx-auth-core` (transitively) as **wheels**, while keeping its own backend bundle (here a minimal
**Acme** bundle) as a **local source project** it can iterate on.

It is deliberately minimal — the Acme bundle has no real backend (**no PyKX, no kdb licence, no
external server**), so the whole thing builds and runs anywhere. This is not a walkthrough (no
`manual.md`/`agent.md`); it is just enough to **build, launch, and prove MCP-server health**.

> **Where the wheels come from.** The KX packages are **not published to a public index** — external
> wheel/PyPI distribution is not yet committed. So this demo builds the wheels **from the public repo
> source** (below) and consumes them from a local index. That makes it runnable with nothing but a
> checkout. When a publishing index exists, you swap the one `[[tool.uv.index]]` line and nothing else
> changes.

## Layout

| File | Role |
|---|---|
| `pyproject.toml` | The consumer project: `kx-*` deps from a local wheel index + the Acme source override. |
| `acme-bundle-src/` | The `kx_mcp_acme` bundle as a local **source project** — a minimal instance of the [extension contract](../../docs/extending.md) (see below). |
| `server.py` | The ~6-line assembly glue: `make_parent` + `try_mount_bundle(acme)`. The artifact this teaches. |
| `check_health.py` | Launches `server.py` and proves it answers an MCP `initialize` handshake. |
| `.env.example` | The Acme `ACME_DB_*` config fragment + optional inbound auth. |

The **Acme bundle** (`acme-bundle-src/`) is a minimal instance of the extension contract. Its
internals — the `ACME_DB_*` config fragment, the eager pre-flight, the cached connection, and the
`addins/` discovery pass (tools `echo`/`whoami`/`list_widgets`/`enrich_widget`) — are the exact pattern the
[extender guide](../../docs/extending.md) walks through step by step; read it there rather than here.

This demo's own connection is an **in-process stub**, so it needs no PyKX, licence, or server; in a
real downstream repo, `acme-bundle-src/` would be your own bundle's checkout wrapping a real backend
instead. The `list_widgets` tool echoes back the `host:port` it "connected" to, so you can watch
`ACME_DB_*` flow config → connection → tool.

## Build the wheels from source

The KX packages aren't on a public index, so build them from a checkout of the public repo. From the
**repo root** (the directory this `demos/` folder lives under):

```bash
uv build --all-packages --wheel      # -> ./dist/*.whl  (kx_mcp_core, kx_auth_core, …)
```

This produces the same wheels the release pipeline would publish. Built from an **untagged** checkout
they carry the `0.0.0` fallback version; built from a **release tag** (`vX.Y.Z`) they carry that
version.

> **Already have a publishing index?** Skip the build and point the index below at it instead
> (and add credentials per your index's docs). The rest of this project stays identical either way,
> which is what the seam is for.

## Build the consumer

```bash
cd demos/extending
uv sync
```

This resolves `kx-mcp-core` + `kx-auth-core` from the local wheel index (`../../dist`, a uv *flat*
index) and builds `kx-mcp-acme` from the linked source (`acme-bundle-src`, an editable path source).
PyPI stays the default index for everything else (`fastmcp`/`pydantic`/…). No credentials, no VPN.

## Launch

```bash
uv run python server.py
# zero-code equivalent, straight from the kx-mcp-core wheel:
uv run kx-mcp --bundles acme --transport streamable-http --host 127.0.0.1 --port 8000
```

## Health

```bash
uv run python check_health.py
```

It launches `server.py`, waits for it to bind, and completes an MCP `initialize` + `list_tools`
handshake. Expected output:

```
[health] OK — MCP handshake succeeded; the server is healthy.
[health] acme bundle mounted: 4 acme_* tool(s): ['acme_echo', 'acme_enrich_widget', 'acme_list_widgets', 'acme_whoami']
```

`whoami` reads `current_principal()` — with `KX_MCP_AUTH` unset it returns `anonymous`; turn inbound
auth on (see `.env.example`) and it returns the validated caller. `list_widgets` reads the config +
cached connection from the request context. `enrich_widget` instruments *itself* — see
[Observability](#observability-metrics--tracing) below. A real backend that couldn't reach its data store would
instead fail its pre-flight, be disabled by `try_mount_bundle`, and the container would still come up
healthy but bare — the "never crash the container" invariant.

## Signalling failure (the required contract)

Two of the four Acme tools show the two halves of the
[failure contract](../../docs/extending.md#signalling-failure-required) — a failed tool must say so
at the *protocol* level (`isError: true`), not only inside its payload:

```bash
uv run kx-mcp --bundles acme --transport streamable-http --port 8000
```

- `acme_list_widgets` uses **`tool_result`**: its payload follows the `status` vocabulary, so one
  wrapper call flags a failure and passes a success straight through.
- `acme_enrich_widget` uses **`error_result`**: its success payload has no `status` field, so there
  is nothing to inspect and the failure path sets the flag directly.

Ask either for a kind that does not exist (`nope`) and the result comes back flagged, with the valid
kinds named in the message — a recovery hint, so an agent gets a next step rather than a dead end:

```
acme_list_widgets(gadget)  is_error=False  {'status': 'success', 'endpoint': ..., 'rows': [...]}
acme_list_widgets(nope)    is_error=True   {'status': 'error', 'message': "unknown widget kind 'nope'. Valid kinds: doohickey, gadget."}
```

With `KX_MCP_METRICS=prometheus` those land in `kx_mcp_dispatches_total{outcome="ok"}` and
`{outcome="error"}` respectively — the reason the flag is required rather than advisory. Both tools
also declare `annotations={"readOnlyHint": True}`, which lets a host auto-approve a read instead of
prompting for it.

## Observability (metrics & tracing)

**Off by default here**, exactly as in a default container: this demo's `.env.example` sets neither
`KX_MCP_METRICS` nor `KX_MCP_TRACING`. The `acme_enrich_widget` tool still carries its own
instrumentation — a module-level counter and a nested span, both from `kx_mcp_core` — and runs
identically with observability off: the counter increments in-process with nothing scraping it, and
`span()` yields a no-op span when no tracer provider is installed. Inert, not broken, and no
`if enabled:` guard anywhere in the tool.

Note what the bundle does **not** import or declare: neither `prometheus_client` nor `opentelemetry`
appears in `acme-bundle-src/pyproject.toml`. `metric` and the collector classes come from
`kx_mcp_core.observability`, and `span` from `kx_mcp_core`, so which telemetry libraries the container
uses stays the container's choice.

To actually watch it, turn the seams on for a run (the container serves `/metrics` on its own port —
no second listener):

```bash
# The launcher wires the scrape route for you (it owns the transport, which the route needs):
KX_MCP_METRICS=prometheus uv run kx-mcp --bundles acme --transport streamable-http --port 8000
# in another shell, after calling the tool once:
curl -s http://127.0.0.1:8000/metrics | grep acme_widgets_enriched
```

> The hand-written [`server.py`](server.py) works the same way — it reads `ObservabilitySettings()`,
> passes it to `make_parent`, and calls `mount_metrics_route(app, obs, TRANSPORT)`, mirroring the
> repo-root [`server.py`](../../server.py). Glue owns **both** halves: with either call missing,
> `KX_MCP_METRICS` is silently inert and `/metrics` 404s with no warning. The launcher does both for
> you because it owns the transport the route needs.

`acme_widgets_enriched_total{kind="gadget"}` appears alongside the container's own
`kx_mcp_dispatches_total` — one scrape, one registry.

Tracing needs one more thing metrics doesn't: the OpenTelemetry SDK + OTLP exporter are an optional
extra on `kx-mcp-core` (`docs/observability.md`), and this consumer's `pyproject.toml` already asks
for it (`kx-mcp-core[tracing]`) so `uv sync` installs it. With that in place:

```bash
KX_MCP_TRACING=otlp KX_MCP_TRACING_OTLP_ENDPOINT=http://collector:4317 uv run python server.py
```

and the tool's `acme.widget.enrich` span arrives in the same trace as the container's
`mcp.tool_invoke` span for that call, nested beneath it — with FastMCP's own `tools/call` / `delegate`
spans in between, so it is a descendant rather than a direct child. Without the extra installed,
`KX_MCP_TRACING=otlp` is non-fatal (the container logs a warning and serves without traces), which is
also how you'd know if you removed `[tracing]` from `pyproject.toml`.

How this works, and the rules for doing it in your own bundle:
[extender guide § Observability for bundle authors](../../docs/extending.md#observability-for-bundle-authors).
Operator-side configuration: the [observability guide](../../docs/observability.md).

## Where to go next

- The assembly seam and the `try_mount_bundle` graceful-degradation contract: [../../README.md](../../README.md).
- Writing a real backend bundle (config fragment, eager pre-flight, cached connections, `addins/`
  discovery): the [extender guide](../../docs/extending.md) — this `acme` bundle is its worked example
  and now demonstrates the `addins/` discovery pattern directly.
- Adding your own metrics and spans from inside a bundle: the extender guide's
  [Observability for bundle authors](../../docs/extending.md#observability-for-bundle-authors)
  section — `acme_enrich_widget` is its worked example.
- Deploying the assembled server: the [deployment guide](../../docs/deployment.md).
