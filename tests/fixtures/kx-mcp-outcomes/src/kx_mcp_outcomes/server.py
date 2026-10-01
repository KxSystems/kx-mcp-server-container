"""The outcomes bundle's entry point: one tool per ``ok`` / ``error`` / ``denied`` classification."""

from __future__ import annotations

from fastmcp import FastMCP

from kx_mcp_core import error_result, tool_result
from kx_mcp_core.auth import authorize


def build_server() -> FastMCP:
    mcp = FastMCP("outcomes")

    @mcp.tool()
    async def ok() -> dict:
        """Succeeds."""
        return tool_result({"status": "ok"})

    @mcp.tool()
    async def failed() -> dict:
        """Returns a failure: ``isError: true``, no exception."""
        return error_result({"status": "error", "message": "no_such_table"})

    @mcp.tool()
    async def raises() -> dict:
        """Raises, as a tool with a bug would."""
        raise RuntimeError("kaboom")

    @mcp.tool()
    @authorize(action="write", resource="outcomes:thing")
    async def denied() -> dict:
        """Gated on ``outcomes:write``; the test's policy grants it to no one."""
        return tool_result({"status": "ok"})

    return mcp
