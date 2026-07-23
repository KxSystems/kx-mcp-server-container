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
`addins/` discovery pass (tools `echo`/`whoami`/`list_widgets`) — are the exact pattern the
[extender guide](../../docs/extending.md) walks through step by step; read it there rather than here.

What's specific to *this* demo: the connection is an **in-process stub** (so it needs no
PyKX/licence/server), and in a real downstream repo `acme-bundle-src/` would be your own bundle's
checkout wrapping a real backend. The `list_widgets` tool echoes back the `host:port` it "connected"
to, so you can watch `ACME_DB_*` flow config → connection → tool.

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
> (and add credentials per your index's docs). The rest of this project is identical — that is the
> point of the seam.

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
[health] acme bundle mounted: 3 acme_* tool(s): ['acme_echo', 'acme_list_widgets', 'acme_whoami']
```

`whoami` reads `current_principal()` — with `KX_MCP_AUTH` unset it returns `anonymous`; turn inbound
auth on (see `.env.example`) and it returns the validated caller. `list_widgets` reads the config +
cached connection from the request context. A real backend that couldn't reach its data store would
instead fail its pre-flight, be disabled by `try_mount_bundle`, and the container would still come up
healthy but bare — the "never crash the container" invariant.

## Where to go next

- The assembly seam and the `try_mount_bundle` graceful-degradation contract: [../../README.md](../../README.md).
- Writing a real backend bundle (config fragment, eager pre-flight, cached connections, `addins/`
  discovery): the [extender guide](../../docs/extending.md) — this `acme` bundle is its worked example
  and now demonstrates the `addins/` discovery pattern directly.
- Deploying the assembled server: the [deployment guide](../../docs/deployment.md).
