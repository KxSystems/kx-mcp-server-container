"""kx-mcp-example — a trivial backend bundle.

Exists only to prove that two independent bundles compose under distinct namespaces with no
collisions, and to do so without any heavy backend dependency (no pykx, no license).
"""

from .server import build_server

__all__ = ["build_server"]
