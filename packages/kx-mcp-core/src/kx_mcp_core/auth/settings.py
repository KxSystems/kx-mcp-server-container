"""Container inbound-auth configuration (the ``KX_MCP_AUTH`` family).

The model itself now lives in the lean, fastmcp-free :mod:`kx_auth_core` package so the same config
contract is shared by the container *and* the client-side ``kx auth`` CLI. This module re-exports it
to keep the established ``kx_mcp_core.auth.AuthSettings`` import path stable.
"""

from __future__ import annotations

from kx_auth_core import AuthSettings

__all__ = ["AuthSettings"]
