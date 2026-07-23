# Writing a new backend

How a team adds a backend to the kx-mcp container: the contract a bundle must satisfy, a
step-by-step tutorial following the kdb-x bundle's pattern, and recipes for wiring the three auth
seams. This guide is the working reference for the extension contract; its normative rationale is
maintained in the project's internal design docs.

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
namespace. Five obligations come with it:

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
`tests/deterministic/unit/test_packaging_deps.py` guards this — it will fail your MR if an import
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

async def list_widgets_impl(kind: str, config=None) -> dict:
    conn = get_connection(config)
    ...                                  # the logic, testable without MCP

@tool                                    # attaches metadata; discovery registers it per instance
async def list_widgets(kind: str, ctx: Context) -> dict:
    """List ACME widgets of the given kind."""
    return await list_widgets_impl(kind, config=config_from_ctx(ctx))
```

Three things this shape buys: the **standalone decorator** keeps the module import-safe (nothing
binds to an instance at import time — that's what makes multi-mount work); the **`*_impl` split**
gives tests (and the `@authorize` decorator) a direct target; **bare names** leave namespacing to
the container. Because tools, resources, and prompts all carry their own decorator type, they live
together in the one `addins/` folder.

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
