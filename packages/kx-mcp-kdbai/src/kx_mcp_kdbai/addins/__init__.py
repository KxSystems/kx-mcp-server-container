from pathlib import Path
from typing import List

from fastmcp import FastMCP

from kx_mcp_core import register_components


def register_addins(mcp: FastMCP) -> List[str]:
    """Discover and register every add-in module in this package onto ``mcp``.

    Tools, resources, and prompts all live in this single ``addins/`` folder, scanned in one
    pass. Uses FastMCP 3.x native filesystem discovery via the shared ``register_components``
    helper in ``kx-mcp-core`` — modules carry standalone ``@tool`` / ``@resource`` / ``@prompt``
    decorators, and the helper dispatches the add-method off each discovered component's concrete
    type (``Tool`` / ``Resource`` / ``ResourceTemplate`` / ``Prompt``), so a mixed folder is
    handled correctly.

    Registration is instance-safe: components are added onto the passed-in instance, never a
    module global.

    Copy-me templates (``*.py.template``) are inert to discovery: ``discover_files`` globs only
    ``*.py``, so they are never imported or registered. Start a new add-in from one.
    """
    return register_components(mcp, Path(__file__).parent)
