# Writing a new backend

How a team adds a backend to the kx-mcp container: the contract a bundle must satisfy, a
step-by-step tutorial following the kdb-x bundle's pattern, and recipes for wiring the three auth
seams.

> **Which pattern to copy.** This guide prescribes the **`addins/` discovery pattern** (steps 5–6),
> shared by the kdbx and kdbai bundles via the `register_components` helper in `kx-mcp-core`. The runnable
> [`demos/extending`](../demos/extending/README.md) bundle uses this same `addins/` pattern, so it
> doubles as the worked example for steps 5–6. For a bundle with only a couple of tools, the minimal
> **bound-decorator** style (bound `@mcp.tool()` inside `build_server()`) is an equally valid option
> and needs no discovery machinery — same contract either way.

## The contract

A backend bundle is an installed Python package named `kx_mcp_<name>` exposing one module-level
function:

```python
def build_server(config=None) -> FastMCP: ...
```

That's the whole integration surface. The launcher resolves `kx-mcp --bundles acme` to the package
`kx_mcp_acme`, calls its `build_server()`, and mounts the returned server under the `acme`
namespace. Seven obligations come with it:

1. **`build_server() -> FastMCP`** — build, configure, and return the server; never serve it
   yourself (the container owns transport/host/port).
2. **Bare primitive names** — register `run_sql_query`, not `acme_run_sql_query`; the container
   adds the namespace prefix at `mount()`.
3. **Instance-safety** — the bundle must be safely mountable more than once (two mounts, two
   backends, different hosts). Per-instance state (config, feature flags) lives on the server
   object; tools read it from the request `Context`, never module globals.
4. **Eager pre-flight, lazy connection** — verify the backend is reachable (and which optional
   interfaces exist) inside `build_server()`, log `SUCCESS`/`ERROR` per check, and fail hard rather
   than serve broken; the *working* connection is established lazily on first tool call and cached.
5. **An env-prefixed config fragment** — the bundle owns its own prefix (`ACME_DB_*`), read via
   pydantic-settings; the container never reaches into it.
6. **A bounded pre-flight** — obligation 4's health check must also *finish*: bound it with your
   own configurable timeout (`ACME_DB_TIMEOUT`, in the fragment above). Backends mount
   sequentially, so a backend that accepts the connection and never answers wedges the whole
   container's startup and every backend requested after yours never mounts.
   `KX_MCP_MOUNT_TIMEOUT` is only an opt-in container-side backstop — a synchronous
   `build_server()` cannot be cancelled in Python, so it *abandons* the startup thread rather than
   stopping it. Only you can bound the blocking call where it actually is.
7. **Signal failure at the protocol level** — a tool that fails must return a result carrying
   `isError: true` (or raise). See [Signalling failure](#signalling-failure-required) — this is
   *required*, not advisory: a failure that only says so inside its payload reaches the host as a
   success and is counted as one.

**The 73-line proof:** [`tests/fixtures/kx-mcp-example/`](../tests/fixtures/kx-mcp-example/) is a
dependency-free bundle exercising the whole contract — including all three auth seams — in one
file. Read it first; it's the floor of what "a bundle" means. It is a *contract validator*, not a
starter (a real backend wants the structure below).

## Tutorial — a new bundle, the kdbx way

Worked example: a backend named `acme`. Alongside each step, the kdb-x equivalent to crib from.

### 1. Package skeleton

```
packages/kx-mcp-acme/
├── pyproject.toml
├── README.md
├── src/kx_mcp_acme/
│   ├── __init__.py          # re-export build_server
│   ├── server.py            # build_server + pre-flight
│   ├── settings.py          # the ACME_DB_* config fragment
│   ├── addins/              # tools/resources/prompts, one folder
│   │   ├── __init__.py      # register_addins(mcp) — one line, delegates to kx-mcp-core
│   │   ├── acme_widgets.py  # an add-in module (standalone @tool)
│   │   └── tool.py.template # copy-me starters (also resource/prompt), inert to discovery
│   └── utils/
│       └── acme.py          # connection lifecycle (config_from_ctx, get_connection)
└── tests/
```

In this repo, add the package under `packages/` and it joins the uv workspace automatically
(`members = ["packages/*"]`). An out-of-repo bundle is the same shape — it just depends on the
container wheels instead. The runnable reference is [`demos/extending/`](../demos/extending/README.md):
a standalone project that mounts its own `acme` bundle over `kx-mcp-core`, consuming the `kx-*` wheels
built from this repo's source (no published index needed). It is a **minimal, no-backend instance of
this tutorial** — its connection is an in-process stub, so it runs anywhere with no PyKX/licence/server;
the steps below are the general (real-backend) shape it follows.

`pyproject.toml`: depend on `fastmcp>=3,<4` plus whatever SDK reaches your backend — and **declare
every first-party package you import** (`kx-mcp-core` if you use `@authorize` or
`current_principal`, `kx-auth-core` if you use `exchange`). The uv workspace masks a missing
declaration; the published wheel does not, and the consumer `ImportError`s at runtime.
`tests/deterministic/unit/test_packaging_deps.py` guards this — it will fail your PR if an import
is undeclared.

### 2. The config fragment (`settings.py`)

A frozen pydantic-settings model under your own prefix — frozen because it becomes a cache key:

```python
class AcmeConfig(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ACME_DB_", frozen=True, extra="ignore")

    host: str = "127.0.0.1"
    port: int = 9000
    timeout: int = 1
    retry: int = 2
```

Crib: [`packages/kx-mcp-kdbx/src/kx_mcp_kdbx/settings.py`](../packages/kx-mcp-kdbx/src/kx_mcp_kdbx/settings.py).
Don't add serving config (transport/host/port) — the container owns that; the kdbx bundle's
`KDBX_MCP_*` serving prefix was deliberately retired.

### 3. `build_server()` with the eager pre-flight (`server.py`)

```python
SERVER_NAME = "ACME MCP server"

def build_server(config: AcmeConfig | None = None) -> FastMCP:
    cfg = config or AcmeConfig()
    _preflight(cfg)                      # reachability + interface checks; sys.exit(1) on hard failure
    mcp = FastMCP(SERVER_NAME)
    mcp._acme_config = cfg  # type: ignore[attr-defined]  # instance state on the server object, read via getattr
    register_addins(mcp)                 # set the config BEFORE discovery — tools read it per call
    return mcp
```

The pre-flight opens a throwaway connection, verifies each interface your tools need, and logs one
`SUCCESS`/`ERROR` line per check. Optional features degrade instead of failing: tag the dependent
tools (e.g. `tags={"requires-ai-libs"}`) and hide them per-instance with
`mcp.disable(tags={...})` when the probe says the feature is absent — exactly what kdbx does for
its similarity-search tools
([`server.py`](../packages/kx-mcp-kdbx/src/kx_mcp_kdbx/server.py)).

### 4. Connection lifecycle (`utils/acme.py`)

Two small functions, mirroring
[`utils/kdbx.py`](../packages/kx-mcp-kdbx/src/kx_mcp_kdbx/utils/kdbx.py):

```python
def config_from_ctx(ctx: Context) -> AcmeConfig:
    return getattr(ctx.fastmcp, "_acme_config")   # each mount's own config, read per call

@lru_cache
def get_connection(config: AcmeConfig):        # keyed on the frozen config:
    return connect(config.host, config.port)   # distinct mounts -> distinct cached connections
```

Tools call `get_connection(config_from_ctx(ctx))` — never open ad-hoc connections, and never cache
one in a module global (that silently shares a single connection across every mount). Add
reconnect-on-closed handling in `get_connection` as kdbx does.

### 5. A tool (`addins/`)

Copy a template — kdbx ships inert copy-me files
([`addins/tool.py.template`](../packages/kx-mcp-kdbx/src/kx_mcp_kdbx/addins/tool.py.template), and
`resource.py.template` / `prompt.py.template`) — drop the `.template` suffix to activate one.
The pattern:

```python
from fastmcp.tools import tool          # the standalone decorator, NOT a bound @mcp.tool

def list_widgets_impl(kind: str, config=None) -> dict:
    conn = get_connection(config)
    ...                                  # the logic, testable without MCP

@tool(annotations={"readOnlyHint": True})  # attaches metadata; discovery registers it per instance
async def list_widgets(kind: str, ctx: Context) -> dict:
    """List ACME widgets of the given kind."""
    return tool_result(list_widgets_impl(kind, config=config_from_ctx(ctx)))
```

**Note which half is `async`.** The registered primitive is `async def`; the `*_impl` is a plain
`def` because it does not await anything. That split is deliberate and it is worth getting right:

- **`async` on the primitive is not a claim about the body.** It is how FastMCP is told *where to
  dispatch*: a coroutine is awaited inline on the event loop, while a plain `def` is handed to
  `anyio.to_thread.run_sync` and runs in a **worker thread**. Keep your registered primitives `async`
  unless you have specifically established that a worker thread is safe for your backend client.
- **For kdb-x it is not safe**, which is why this matters beyond style. Licensed q refuses to *open* a
  socket on any thread but the main one (`nosocket: Cannot open or use a socket on a thread other
  than main`), and the connections carry no lock — q IPC has no message ids, so two threads sharing a
  handle can each be handed the other's reply. Single-threaded dispatch is what makes both of those
  non-issues.
- **`async def` on an impl that never awaits is a lie.** It says the function yields when it does not,
  and it forces every caller and every test into an async context for nothing. Declare it `def`. Use
  `async def` when there is a real `await` — an async HTTP client, or `anyio.to_thread.run_sync`
  around genuinely CPU-bound work, as the embedding providers do.

A CI guard enforces both halves: registered primitives must be `async`, and nothing else in `addins/`
may be `async` without awaiting.

The consequence for callers is that a blocking backend call holds the event loop for its duration, so
one slow query delays other in-flight tools and the `/metrics` scrape. That is a known and accepted
trade for kdb-x, because q does not multiplex qIPC requests — moving the call off the loop was tried
and reverted, since it bought loop isolation but no concurrency at real cost in complexity.

This shape earns its keep in a few ways. The **standalone decorator** keeps the module import-safe —
nothing binds to an instance at import time, which is what makes multi-mount work in the first
place. The **`*_impl` split** gives tests, and the `@authorize` decorator, something to target
directly. And **bare names** leave all the namespacing to the container. Tools, resources, and
prompts each carry their own decorator type, so they can all live together in the one `addins/`
folder. `tool_result` is the required failure contract — see
[Signalling failure](#signalling-failure-required) — and `annotations` tells hosts this is a read,
so they can auto-approve it instead of prompting.

### 6. Discovery (`addins/__init__.py`)

The discovery helper is shared: `register_components` lives in `kx-mcp-core`
([`kx_mcp_core/discovery.py`](../packages/kx-mcp-core/src/kx_mcp_core/discovery.py) — it composes
FastMCP's native `discover_files` / `extract_components` and dispatches
`add_tool`/`add_resource`/`add_prompt` off each component's concrete type). Your `register_addins`
is a one-liner that delegates to it, scanning the folder it lives in:

```python
from pathlib import Path
from kx_mcp_core import register_components   # the shared helper — no per-bundle copy

def register_addins(mcp):
    return register_components(mcp, Path(__file__).parent)
```

`*.py.template` files are never globbed, so templates and disabled modules are inert with no
bespoke filter. `build_server()` sets the per-instance config on the server object **before**
calling `register_addins`, so discovered tools can read it from the request Context.

> **Which to use:** `addins/` + discovery earns its keep on a growing surface — it's what the
> shipping kdbx and kdbai bundles use, and what the runnable
> [`demos/extending`](../demos/extending/README.md) bundle demonstrates (config fragment +
> pre-flight + cached connection + three add-in tools). For a couple of tools, you can skip it
> entirely: bound `@mcp.tool()` decorators inside `build_server()` satisfy the contract with no
> discovery machinery. Same contract either way — bare names, instance-safety, `build_server()`;
> the folder layout is bundle-internal.

### 7. Mount and run

```bash
uv sync                                  # make kx_mcp_acme importable
uv run kx-mcp --bundles acme             # convention: acme -> package kx_mcp_acme
```

List tools from any MCP client: your primitives appear namespaced (`acme_list_widgets`). Mounting
alongside others is just `--bundles kdbx,acme`. A failed pre-flight logs its `ERROR` and the
launcher's `try_mount_bundle` disables that backend rather than crashing the container.

### 8. Tests

Unit tests live in your package (`packages/kx-mcp-acme/tests/`), mirror `src/`, target the
`*_impl` functions and the discovery/registration path with the backend SDK mocked — see
[`packages/kx-mcp-kdbx/tests/`](../packages/kx-mcp-kdbx/tests/) for the idiom. Two container-level
assertions are already generic: the mount/namespace path (crib
`tests/deterministic/unit/` cases against the example fixture) and the packaging-deps AST guard,
which picks up your package automatically. For registration/transport/auth behaviour, pair the
mocks with an integration test that spawns the real container over the wire.

## Signalling failure (required)

**A tool that fails must say so at the protocol level.** MCP carries a dedicated flag for this —
`isError` on the tool result — and it is the only failure signal anything outside your tool can see.
A payload that reports its own failure (`{"status": "error", ...}`) is *not* a substitute:

- **The host and the model** treat `isError` as "this call failed, try something else". A payload
  field is advisory — a model usually reads it, nothing in the protocol says it must, and a host
  rendering only `structuredContent` will show a success.
- **The container's [observability](observability.md) seams** see the dispatch, not your payload
  conventions. Without the flag, a failed call increments
  `kx_mcp_dispatches_total{outcome="ok"}` and the `error` series reads zero during an outage.

Use `tool_result` from `kx-mcp-core`, which sets the flag while **keeping** your structured payload:

```python
from kx_mcp_core import tool_result

async def list_widgets_impl(kind: str, config=None) -> dict:
    try:
        ...
    except WidgetError as exc:
        # Put a recovery hint in the message: "use list_kinds to see valid values" turns a dead
        # end into a next step. Return the payload — the wrapper below flags it.
        return {"status": "error", "message": f"{exc}. Call list_kinds for valid kinds."}
    return {"status": "success", "widgets": [...]}

@tool(annotations={"readOnlyHint": True})
async def list_widgets(kind: str, ctx: Context) -> dict:
    """List ACME widgets of the given kind."""
    return tool_result(await list_widgets_impl(kind, config=config_from_ctx(ctx)))
```

`tool_result` marks the result `isError: true` when the payload's `status` is `error` or
`permission_denied`, and passes a success straight through. If your payloads carry no `status`
field, call `error_result(payload)` directly on the failure path instead.

**Wrap at the `@tool` wrapper, not inside the `*_impl`.** The `*_impl` keeps returning a plain
dict, so it stays directly testable and any pinned response-document contract is untouched. Adopting
this is one line per tool.

**Raise only when there is nothing structured to say.** Raising `ToolError` also sets `isError`, but
it **discards `structuredContent` entirely** — the client gets text only, so every discriminating
field you put in the payload (`error_type`, remediation details, an entitled-subset list) is lost,
and fastmcp logs the expected condition at `ERROR` level. Raise for a bare refusal (that is what
`@authorize` does); return an error result whenever you have detail worth carrying.

**Keep the `-> dict` annotation.** FastMCP derives the tool's advertised `outputSchema` from it;
widening it to `Any` to accommodate `ToolResult` drops the schema from your tool's surface
altogether. `ToolResult` is a transport envelope FastMCP unwraps, not part of your declared data
shape.

### Denials are failures too

An authorization refusal is a failure, and gets the same `isError: true` — but it should also be
*distinguishable* from a fault, so a policy deny never inflates the error rate an operator alerts
on. The refusing layer records that by calling `stamp_authz_decision`, and the container reads it
back after the dispatch: `@authorize` does it for you, and a backend-native (PEP-2) refusal your
tool translates itself should record it. kdbx's `record_denial` in
[`utils/denial.py`](../packages/kx-mcp-kdbx/src/kx_mcp_kdbx/utils/denial.py) is the worked example.

Use `stamp_authz_decision` rather than writing a contextvar yourself. The decision has to survive
from your tool body out to the parent middleware, and those two are not always on the same thread —
a `ContextVar.set` made in a worker thread is invisible to the dispatching task, so a hand-rolled
stamp is silently lost and your denial is reported as a plain error. The helper owns that detail.

> **Your tool must be `async def` for that stamp to survive.** A *sync* tool body runs in a worker
> thread, which gets a **copy** of the caller's context — so a `contextvar` set inside it never
> reaches the parent middleware, and the denial is silently recorded as a plain `error`. Same root
> cause as the thread-hop span orphaning below. Verified both ways: the identical tool records
> `denied` as `async def` and `error` as `def`.

### What clients see

Worth knowing before you change a tool's failure path, because it is observable:

| | success | `isError: true` |
| --- | --- | --- |
| MCP host (Claude) | normal result | rendered and reasoned about as a failure |
| fastmcp Python `Client` | returns the result | **raises `ToolError`** unless called with `raise_on_error=False` |
| `CallToolResult.data` | the payload | **`None`** — read `.structured_content` instead |

## Tool annotations

Declare `annotations` on **every** tool. They are behavioural hints a host reads *before* invoking, and
unset does not mean "unknown" in practice — it means the host assumes the worst and puts an approval
prompt in front of what may be a plain read.

```python
@tool(annotations={"readOnlyHint": True})              # a read
@tool(annotations={"readOnlyHint": True, "idempotentHint": True})   # a read that is safe to retry
@tool(annotations={"destructiveHint": True})           # a write that can lose or overwrite data
```

| Hint | Declare it when | Effect of leaving it unset |
| --- | --- | --- |
| `readOnlyHint` | The tool does not modify state. Most query/metadata tools. | The host treats a read as a possible write and prompts. |
| `idempotentHint` | Repeating the call with the same arguments is safe. | An agent may avoid a legitimate retry. |
| `destructiveHint` | The tool can lose or overwrite data. Declare it *instead of* `readOnlyHint`. | A destructive call may be auto-approved where the host allows writes. |

This is not one of the six [extension-contract](../../mcp-container/mcp-container-design.md)
obligations — nothing breaks without it, and the container cannot verify a claim about your tool's
behaviour. It is a strong convention: all 16 shipped tools declare theirs, and a tool that omits them
is a worse citizen in every host, which is why the copy-me `tool.py.template` files start with
`readOnlyHint` set and tell you to change it if your tool writes.

## Wiring the auth seams

All three seams cross the mount boundary — a mounted bundle's tools see the container's inbound
principal with no plumbing on your part.

**Who is calling** (inbound, [auth guide § inbound](auth.md#inbound-authentication-kx_mcp_auth)):

```python
from kx_mcp_core.auth import current_principal

principal = current_principal()          # validated AccessToken, or None when auth is off
sub = principal.claims.get("sub") if principal else "anonymous"
```

**Capability gating** (PEP-1, [auth guide § authorization](auth.md#authorization-kx_mcp_authz)) —
decorate the `_impl`, selectively:

```python
from kx_mcp_core.auth import authorize

@authorize(action="write", resource="acme:widgets")
async def create_widget_impl(...): ...
```

Decorate only where a tool-class concern (write/admin/publish) is distinct from your backend's own
data gate; read-path tools whose data access the backend already authorizes should stay
undecorated (route-only). If your backend has a native decision engine, register an adapter for it
(mirror [`utils/authz_kx_rbac.py`](../packages/kx-mcp-kdbx/src/kx_mcp_kdbx/utils/authz_kx_rbac.py)
— ctx-resolved, self-registering at import, fail-closed on error).

**Who the backend sees** (outbound,
[auth guide § outbound](auth.md#outbound-identity-per-backend)) — delegate to the shared exchange
seam rather than hand-rolling token requests:

```python
from kx_auth_core import OutboundConfig, exchange

cred = await exchange(outbound_config, subject_token=principal.token if principal else None)
# cred.access_token -> however your SDK carries a bearer
```

Give your config fragment the standard suffixes (`ACME_DB_OUTBOUND_STRATEGY`, `_TOKEN_URL`,
`_CLIENT_ID`, `_CLIENT_SECRET`, `_SCOPES`, …) so operators configure every backend the same way —
kdbai's [`utils/kdbai_auth.py`](../packages/kx-mcp-kdbai/src/kx_mcp_kdbai/utils/kdbai_auth.py) is
the reference. For `passthrough`, key your connection cache per principal, not just per config.
## Observability for bundle authors

The [metrics and tracing seams](observability.md) attach as **parent** middleware, so every dispatch
is already counted, timed, and spanned — with your namespace already applied — before your tool body
runs. Instrument further only to say something about the *inside* of a tool that the boundary can't
see. Both additions need **zero plumbing**: no registration call into `kx-mcp-core`, no base class.

Everything you need comes from `kx_mcp_core`, so **your bundle imports neither `prometheus_client` nor
`opentelemetry`, and declares nothing beyond `kx-mcp-core`.** Which metrics or tracing library the
container uses is the container's choice to change — it has changed once already — and that choice
shouldn't reach into your dependency list.

> **The shipped kdb-x and KDB.AI bundles are instrumented through this same surface** — see
> [`observability.md` § Backend instrumentation](observability.md#backend-instrumentation) for what
> they emit. Neither declares a telemetry dependency, and neither needed an addition to
> `kx-mcp-core` to do it; if you find yourself wanting one, that is worth raising rather than
> working around. The worked example below is the smallest version of the pattern.

Their shape is worth copying at scale: each bundle keeps its collectors and call wrappers in one
`utils/observe.py` and instruments the **connection and client layer**, so the add-ins stay clean
and a new add-in is instrumented for free by reusing the connection helper it already has to use.
Per-tool `with span(...)` blocks are for what only that tool knows.

**A metric of your own.** Module-level collectors are the one documented exception to the
instance-safety rule: the registry is process-global by design, so the per-backend dimension rides on
*labels*, not separate instances. Anything registered anywhere in the process appears in the same
`/metrics` scrape — no route work:

```python
from kx_mcp_core.observability import Counter, metric

# The name here is the name that appears in the scrape, and the only place you write it.
WIDGETS_ENRICHED = metric(Counter, "acme_widgets_enriched_total", "Widgets enriched.", ["kind"])

def enrich_widget_impl(kind: str, config: AcmeConfig | None = None) -> dict:
    WIDGETS_ENRICHED.labels(kind=kind).inc()
```

`metric` takes the collector class, so `Gauge`, `Histogram`, `Summary`, `Info` and `Enum` work the
same way (all re-exported alongside `Counter`), and constructor keywords like `buckets` or `unit`
pass straight through. Its job is to make *defining* a metric idempotent: constructing a collector
registers it as a side effect, and a duplicate name raises, so a module imported twice would crash
without it. It raises instead if the name is genuinely taken by a *different* collector — two bundles
colliding, which you want to hear about at definition rather than at some later `.labels()` call.

**A span of your own.** The middleware uses `start_as_current_span`, so your dispatch span is already
*current* when `*_impl` runs — a child span is just a nested `with`, and `contextvars` carries the
parent across `await` for you:

```python
from kx_mcp_core import span

def enrich_widget_impl(kind: str, config: AcmeConfig | None = None) -> dict:
    with span("acme.widget.enrich", {"acme.widget.kind": kind}) as current:
        rows = fetch(kind)
        current.set_attribute("acme.widget.count", len(rows))
```

That is all the nesting takes: the span lands in the **same trace**, beneath the dispatch span, with no
extra work — though not as its *direct* child, since FastMCP's own protocol and mount-delegation spans
(`tools/call …`, `delegate …`) sit in between. Filter on your own `acme.*` prefix, not on parentage.

**Enabled-ness and installed-ness are different questions, and you have to handle neither.**

- *Off* is handled by the libraries. With no tracer provider installed `span()` yields a no-op span
  that discards everything, and a counter with nothing scraping it is just an in-process increment. So
  a tool never checks whether observability is on — the two snippets above are the whole story, and
  they behave identically with `KX_MCP_METRICS` and `KX_MCP_TRACING` unset.
- *Absent* is handled by the container. `kx-mcp-core` declares what it imports, so the libraries behind
  `metric` and `span` are there whenever your bundle is installed. (`kx-mcp-core[tracing]` adds the
  OpenTelemetry **SDK and OTLP exporter** — what the operator needs to actually *export* spans. Your
  code does not change either way; without it the container logs a warning at startup and serves
  untraced, and your `span()` calls are the same no-ops as when tracing is simply switched off.)

  The distinction matters because the first is a *runtime* state that flips per deployment, while the
  second is a *packaging* fact. Conflating them is how you end up writing `try: import …` guards around
  telemetry that cannot actually be missing.

> **The one real gotcha — a raw thread hop silently orphans your span.** `asyncio.to_thread` and
> `asyncio.create_task` propagate `contextvars`, so spans nest correctly across them. A bare
> `concurrent.futures.ThreadPoolExecutor.submit()` does **not**: anything traced after that hop
> becomes its own disconnected root trace instead of a child of the dispatch span — no error, no
> warning, nothing to notice until a trace looks wrong.
>
> The fix is one call — submit `contextvars.copy_context().run` instead of your callable directly:
>
> ```python
> executor.submit(contextvars.copy_context().run, asyncio.run, coro)
> ```
>
> The worked example is `_run_coro` in kdbai's
> [`utils/kdbai_auth.py`](../packages/kx-mcp-kdbai/src/kx_mcp_kdbai/utils/kdbai_auth.py), which hops
> to a worker thread to mint a service-account token from sync code. Its regression test removes the
> copy and asserts the span orphans, because nothing else about the code changes shape when it does.

Worth knowing:

- **Namespace span names and attributes by backend** (`acme.*` / `kdbx.*`), never `mcp.*` — that
  prefix is the boundary middleware's own vocabulary. Same for metric names: they are process-global,
  so prefix yours with your bundle's name.
- **Same disclosure posture as the audit line.** Action, target, outcome, verdict are fine on a span;
  raw query text, token claims, and anything off `current_principal()` are not — a span export is a
  wire egress.
- **Record an exception once per span.** The middleware records on the dispatch span (and turns
  OpenTelemetry's own `record_exception` *off* there so it doesn't double-record its own span);
  `span()` leaves those defaults on, so an exception escaping your `with` is recorded on **your** span
  and re-raised. One event per span, no duplicates. If you are *swallowing* an error instead of
  re-raising, pass `record_exception=False` and record it yourself with whatever context you have.
- **Bound label values by construction.** `tool_name` is safe because the tool inventory is finite; a
  username, `jti`, table name, or query text is an unbounded-cardinality leak — that is the audit
  line's job, not Prometheus's.
- **A series appears only after first use** — a tool nobody has called yet has no series. Expected.
- **Collection is transport-independent**: counters still increment under `stdio`, only the scrape
  route is skipped — so a unit test can assert on the collector with no HTTP at all.
- **Instrument the `*_impl`, not the registered `@tool` wrapper** — that is what tests call directly
  and what `@authorize` decorates.
- **Writing your own glue instead of using the `kx-mcp` launcher?** Then you own both halves:
  `make_parent(..., observability=ObservabilitySettings())` *and* `mount_metrics_route(app, obs,
  transport)`. Miss either and `KX_MCP_METRICS` is silently inert. See
  [`demos/extending/server.py`](../demos/extending/server.py).

The runnable version of both snippets is the `acme_enrich_widget` tool in
[`demos/extending`](../demos/extending/).

## Do / don't

| ✅ Do | ❌ Don't |
| --- | --- |
| Attach per-instance state to the server object (`mcp._acme_config`) and read it via `ctx.fastmcp` | Keep config, connections, or feature flags in module globals — it silently couples every mount |
| Register bare names (`list_widgets`) | Pre-prefix names (`acme_list_widgets`) — the mount namespace does that |
| Pre-flight eagerly, log `SUCCESS`/`ERROR` per check, exit hard on broken | Serve a backend you couldn't reach, or let a failure surface as a mid-tool stack trace |
| Degrade optional features via tags + `mcp.disable(tags=...)` | Register-time `if` around tools with no per-instance story |
| One cached connection per frozen config (and per principal under passthrough) | Ad-hoc connections inside tools |
| Declare `kx-mcp-core` / `kx-auth-core` in `[project.dependencies]` when you import them | Rely on the workspace making siblings importable — the published wheel won't |
| Keep the `*_impl` split so tests and `@authorize` target the logic directly | Test only through the MCP layer |
| Return failures through `tool_result` / `error_result` so they carry `isError: true` | Report a failure only inside the payload — the host and the metrics both read it as a success |
| Declare `annotations` on every tool (`readOnlyHint` for reads) | Leave them unset — hosts then assume the worst and prompt for a plain read |
