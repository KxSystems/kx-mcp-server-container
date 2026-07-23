"""Tests for the addins package's native FastMCP filesystem discovery.

Tools, resources, and prompts now live in one ``addins/`` folder scanned in a single pass by
FastMCP 3.x native discovery via the shared ``kx_mcp_core.register_components`` helper, which
dispatches the add-method off each component's concrete type. These tests pin the package-level
``register_addins()`` entry point against a real FastMCP instance.
"""

import asyncio

from fastmcp import FastMCP

from kx_mcp_kdbai.addins import register_addins

_EXPECTED_TOOLS = {
    "list_databases", "database_info", "all_databases_info",
    "list_tables", "table_info",
    "query_data", "similarity_search", "hybrid_search",
    "session_info", "system_info", "process_info",
}


def _new_server() -> FastMCP:
    mcp = FastMCP("kdbai-test")
    # Per-instance context the tools read at call time (instance-safety contract).
    mcp._kdbai_config = None
    return mcp


def test_register_addins_discovers_all_primitive_kinds_in_one_pass():
    """One folder, one scan: all 11 tools, the guidance resource, and the prompt register."""
    mcp = _new_server()
    registered = register_addins(mcp)

    # The registered list carries the bare names across all three primitive kinds.
    assert _EXPECTED_TOOLS <= set(registered)
    assert "table_analysis" in registered

    tools = {t.name for t in asyncio.run(mcp.list_tools())}
    assert tools == _EXPECTED_TOOLS
    # Bare names — no double-prefix (the container adds `kdbai` at mount time).
    assert not any(name.startswith("kdbai_") for name in tools)

    resource_uris = {str(r.uri) for r in asyncio.run(mcp.list_resources())}
    assert resource_uris == {"file://guidance/kdbai-operations"}

    prompts = {p.name for p in asyncio.run(mcp.list_prompts())}
    assert prompts == {"table_analysis"}


def test_py_template_files_are_not_discovered():
    """The copy-me templates are ``*.py.template`` — inert to discovery (``discover_files`` globs
    ``*.py``), so nothing they define is ever registered."""
    mcp = _new_server()
    register_addins(mcp)

    tools = {t.name for t in asyncio.run(mcp.list_tools())}
    resource_uris = {str(r.uri) for r in asyncio.run(mcp.list_resources())}
    prompts = {p.name for p in asyncio.run(mcp.list_prompts())}

    # tool.py.template defines my_tool; resource.py.template an example://static resource;
    # prompt.py.template example_analysis — none of these appear.
    assert "my_tool" not in tools
    assert "example://static" not in resource_uris
    assert "example_analysis" not in prompts


def test_register_addins_is_instance_safe():
    """The same discovered modules register cleanly onto two independent instances."""
    mcp_a = _new_server()
    mcp_b = _new_server()

    reg_a = register_addins(mcp_a)
    reg_b = register_addins(mcp_b)

    assert set(reg_a) == set(reg_b)

    # Distinct instances hold distinct tool objects (no shared module-global registry).
    tools_a = {t.name: t for t in asyncio.run(mcp_a.list_tools())}
    tools_b = {t.name: t for t in asyncio.run(mcp_b.list_tools())}
    assert tools_a["query_data"] is not tools_b["query_data"]
