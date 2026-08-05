"""A second, independent bundle for the live multi-backend composition test (X.9).

Re-exports the ``kx_mcp_example`` fixture's ``build_server`` under a distinct package name. Mounting
the *same* bundle code at two different namespaces is the sharpest instance-safety proof available:
each call to ``build_server()`` returns a fresh ``FastMCP`` with fresh tool closures (no module
globals), so identical bare tool names (``echo``, ``whoami``, ...) must stay distinct once namespaced
by the container — no collision, both independently reachable.
"""

from kx_mcp_example.server import build_server

__all__ = ["build_server"]
