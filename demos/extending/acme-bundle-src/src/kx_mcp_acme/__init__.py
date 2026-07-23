"""kx-mcp-acme — a minimal example backend bundle for the `demos/extending` demo.

Gives the consumer project a real backend to mount without any heavy dependency (no pykx, no
licence, no external server): a config fragment (`settings.py`), a connection lifecycle
(`utils/acme.py`, an in-process **stub** where a real client goes), an eager pre-flight, and a
couple of tools. See ../../../docs/extending.md for the full contract and the `addins/` discovery
pass a larger backend would use.
"""

from .server import build_server

__all__ = ["build_server"]
