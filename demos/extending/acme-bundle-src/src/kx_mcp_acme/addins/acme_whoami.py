"""An add-in that reads the inbound principal.

``current_principal()`` returns the container-validated ``AccessToken`` that crosses the mount
namespace boundary into a backend tool (or ``None`` when ``KX_MCP_AUTH`` is unset). This is the
inbound-identity seam a real backend reads before propagating identity outbound.
"""

from kx_mcp_core.auth import current_principal
from fastmcp.tools import tool


@tool
def whoami() -> str:
    """Return the authenticated principal's client_id, or 'anonymous' when auth is unset."""
    principal = current_principal()
    return "anonymous" if principal is None else principal.client_id
