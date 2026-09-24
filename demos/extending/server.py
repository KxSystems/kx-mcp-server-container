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

Observability rides the same seam as the repo-root server.py: KX_MCP_METRICS / KX_MCP_TRACING (both
off by default — see README.md § Observability) attach the metrics + tracing middleware on the
parent, so the mounted acme bundle is observed too.
"""

from typing import Final

from kx_mcp_core import (
    ObservabilitySettings,
    build_auth_provider,
    configure_logging,
    make_parent,
    mount_metrics_route,
    try_mount_bundle,
)
from kx_mcp_acme import build_server as acme

# Final keeps mypy's inference narrow (a plain `str` doesn't satisfy app.run()'s Literal[...] param)
# — this file sits under demos/, which the workspace DOES mypy-check (unlike the repo-root server.py
# this mirrors, which isn't on the checked path and so never hit this).
TRANSPORT: Final = "streamable-http"

configure_logging()  # surface the audit line + mount warnings (same config as the kx-mcp launcher)

obs = ObservabilitySettings()  # KX_MCP_METRICS / KX_MCP_TRACING — both default off
app = make_parent(  # KX_MCP_AUTH: unset (default) / static / jwks
    "consumer-acme-mcp", auth=build_auth_provider(), observability=obs
)
try_mount_bundle(app, acme, namespace="acme")  # -> acme_* tools/prompts/resources

# The /metrics scrape route depends on the transport, so it is mounted here rather than in
# make_parent; a no-op unless KX_MCP_METRICS is on, and a warning-and-skip under stdio.
mount_metrics_route(app, obs, TRANSPORT)

if __name__ == "__main__":
    app.run(transport=TRANSPORT, host="127.0.0.1", port=8000)
