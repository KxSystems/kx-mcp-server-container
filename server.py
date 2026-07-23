"""Headline artifact: assemble a kx-mcp composition server with ~10 lines of glue.

"FastMCP is the container." Each backend is a bundle exposing build_server() -> FastMCP; the
parent mounts them under namespaces. No container runtime, no discovery/activation machinery.

This is the hand-written glue path. The equivalent zero-code path is the launcher:

    uv run kx-mcp --bundles kdbx,kdbai

Run this file:  uv run python server.py

Graceful degradation: each backend is mounted via `try_mount_bundle`, so a backend whose eager
pre-flight fails (its `build_server()` calls `sys.exit(1)` when its database is unreachable) is
disabled with a warning while the container keeps serving the backends that did come up — the
"never crash the container" invariant. With no backend reachable, the parent still starts (bare).

Inbound auth is resolved from KX_MCP_AUTH and passed to the parent, guarding every mounted backend;
it defaults to off (the single-principal bundling posture). The subject/action/resource authorization
seam and outbound identity propagation are deferred control-plane slots that attach in kx-mcp-core
(make_parent).
"""

from kx_mcp_core import build_auth_provider, configure_logging, make_parent, try_mount_bundle
from kx_mcp_kdbx import build_server as kdbx
from kx_mcp_kdbai import build_server as kdbai

configure_logging()  # surface the audit line + mount warnings (same config as the kx-mcp launcher)

app = make_parent("kx-mcp", auth=build_auth_provider())  # KX_MCP_AUTH: unset (default) / static / jwks
try_mount_bundle(app, kdbx, namespace="kdbx")  # -> kdbx_run_sql_query, kdbx_similarity_search, ...
try_mount_bundle(app, kdbai, namespace="kdbai")  # -> kdbai_* tools/prompts/resources

if __name__ == "__main__":
    app.run(transport="streamable-http", host="0.0.0.0", port=8000)
