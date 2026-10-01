# kx-mcp-core

The assembly layer of the **KX MCP composition container**: a [FastMCP](https://gofastmcp.com) 3.x
parent that mounts pluggable backend bundles (KDB-X, KDB.AI, or your own)
into one served MCP surface, and the home of the container's control plane — inbound authentication,
the authorization seam, and per-dispatch audit.

Install it when you are **assembling an MCP server** from KX backend bundles:

```bash
uv add kx-mcp-core kx-mcp-kdbx        # from PyPI
```

> All the packages version in lockstep; pin them together (`kx-mcp-core==0.5.0`) when you depend on
> a version. See the
> [deployment guide](https://github.com/KxSystems/kx-mcp-server-container/blob/main/docs/deployment.md#quickstart--three-ways-to-run-it).

## What's in the box

- **`make_parent(name, auth=None)`** — builds the parent server; inbound auth
  (`KX_MCP_AUTH`: `unset`/`static`/`jwks`/`oidc_proxy`/`entra`) and the audit middleware attach here.
- **`mount_bundle` / `try_mount_bundle`** — mount a bundle's `build_server()` under a namespace;
  the `try_` variant disables an unreachable backend instead of crashing the container.
- **The `kx-mcp` launcher** — the zero-code path: `kx-mcp --bundles kdbx,kdbai` (a bundle named
  `<name>` is the importable package `kx_mcp_<name>`).
- **`kx_mcp_core.auth`** — `current_principal()` (the validated inbound principal, visible to
  mounted tools), the `@authorize` PEP-1 capability decorator, `AuditMiddleware`, and re-exports of
  the shared `kx-auth-core` seams so bundle imports stay stable.

## Minimal glue

```python
from kx_mcp_core import make_parent, load_build_server, try_mount_bundle

app = make_parent("kx-mcp")
try_mount_bundle(app, load_build_server("kx_mcp_kdbx"), namespace="kdbx")
app.run(transport="streamable-http", host="127.0.0.1", port=8000)
```

Or no glue at all:

```bash
uvx --from kx-mcp-core --with kx-mcp-kdbx kx-mcp --bundles kdbx
```

## Documentation

The full docs live in the [`kx-mcp-server-container`](https://github.com/KxSystems/kx-mcp-server-container)
repository, under `docs/`: the [deployment guide](https://github.com/KxSystems/kx-mcp-server-container/blob/main/docs/deployment.md),
the [auth reference](https://github.com/KxSystems/kx-mcp-server-container/blob/main/docs/auth.md)
(inbound modes, outbound strategies, authorization), and the
[extender guide](https://github.com/KxSystems/kx-mcp-server-container/blob/main/docs/extending.md)
for writing your own bundle — plus a reference downstream build
([`demos/extending/`](https://github.com/KxSystems/kx-mcp-server-container/tree/main/demos/extending))
showing a standalone project that assembles a server from these wheels.

All workspace packages version in lockstep from release tags — pin `kx-mcp-core` and your bundles
to the same version.
