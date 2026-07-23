"""The Acme bundle's server — a small but complete instance of the extension contract.

``build_server(config=None) -> FastMCP``: build the config fragment, run an eager pre-flight, attach
per-instance state to the server object (never a module global), then discover and register the
bare-named add-ins. The add-ins in ``addins/`` show the two seams that matter:
  - ``whoami`` reads ``current_principal()`` — the container-validated inbound principal reaching a
    mounted tool across the namespace boundary.
  - ``list_widgets`` reads its config + connection from the request ``Context`` and queries the
    (stub) backend — the config → cached-connection → tool path a real backend follows.

Tools, resources, and prompts all live in the single ``addins/`` folder and are registered in one
native-discovery pass via ``register_addins`` (the shared ``register_components`` helper in
``kx-mcp-core``) — exactly the pattern the shipping kdbx and kdbai bundles use, and the worked
example for ``../../../../docs/extending.md`` steps 5–6. For a bundle with only a couple of tools,
bound ``@mcp.tool()`` decorators inside ``build_server()`` are an equally valid minimal style; the
contract is identical either way.
"""

from __future__ import annotations

from fastmcp import FastMCP

from .addins import register_addins
from .settings import AcmeConfig
from .utils.acme import preflight


def build_server(config: AcmeConfig | None = None) -> FastMCP:
    """Build the Acme FastMCP server (config fragment + eager pre-flight + add-in discovery)."""
    cfg = config or AcmeConfig()
    preflight(cfg)                 # reachability/interface checks; a real backend sys.exit(1)s on failure
    mcp = FastMCP("acme")
    mcp._acme_config = cfg         # type: ignore[attr-defined]  # per-instance stash, set BEFORE discovery (read via getattr)
    register_addins(mcp)           # scan addins/, register tools/resources/prompts onto this instance
    return mcp
