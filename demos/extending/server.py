"""Consumer-side assembly glue: an Acme-only kx-mcp composition server.

This is the artifact this demo teaches — what a downstream repo writes once it has the container's
`kx-mcp-core` (the assembly seam + the `kx-mcp` launcher) and a backend bundle. "FastMCP is the
container": the parent mounts each backend bundle (build_server() -> FastMCP) under a namespace.
Here there is exactly one backend — the example "Acme" bundle — so the surface is Acme-only.

The repo-root server.py mounts several backends; this trims it to the single bundle a consumer wants.

Run this file:  uv run python server.py
Zero-code equivalent (the launcher from the kx-mcp-core wheel):  uv run kx-mcp --bundles acme

Graceful degradation: the bundle is mounted via `try_mount_bundle`, so if a backend's eager
pre-flight fails (build_server() calls sys.exit(1) when the backend is unreachable) that backend is
disabled with a warning and the container still starts — the "never crash the container" invariant.
Acme has no external backend, so it always mounts; a real backend degrades to healthy-but-bare.

Inbound auth is resolved from KX_MCP_AUTH (default off — the single-principal bundling posture).
"""

from kx_mcp_core import build_auth_provider, configure_logging, make_parent, try_mount_bundle
from kx_mcp_acme import build_server as acme

configure_logging()  # surface the audit line + mount warnings (same config as the kx-mcp launcher)

app = make_parent("consumer-acme-mcp", auth=build_auth_provider())  # KX_MCP_AUTH: unset (default) / static / jwks
try_mount_bundle(app, acme, namespace="acme")  # -> acme_* tools/prompts/resources

if __name__ == "__main__":
    app.run(transport="streamable-http", host="127.0.0.1", port=8000)
