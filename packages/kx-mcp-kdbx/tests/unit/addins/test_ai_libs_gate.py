"""The AI-libs gate: AI tools are discovered unconditionally then HIDDEN per-instance.

Pre-refactor, the AI tools (similarity_search, hybrid_search) were conditionally *registered*
based on a global flag. Now they are always registered during native discovery, tagged
``requires-ai-libs``, and McpServer hides them with ``mcp.disable(tags={"requires-ai-libs"})``
when the per-instance pre-flight reports AI libs unavailable.

This pins the two things that matter:
  1. ``disable(tags=...)`` removes the tagged tools from ``list_tools()`` — i.e. they are not
     merely blocked at call time, they are not advertised (the no-regression floor: never offer
     a tool we can't fulfil).
  2. The gate is instance-safe: two backends with different AI-libs status, in one process, each
     expose the correct surface.
"""

import asyncio

from fastmcp import FastMCP

from kx_mcp_kdbx.addins import register_addins

AI_TOOLS = {"similarity_search", "hybrid_search"}
AI_LIBS_TAG = "requires-ai-libs"


def _server_with_tools() -> FastMCP:
    mcp = FastMCP("kdbx-test")
    mcp._kdbx_config = None
    register_addins(mcp)
    return mcp


def _gate(mcp: FastMCP, ai_available: bool) -> None:
    """Mirror McpServer._gate_ai_tools — disable the tagged tools when AI libs are absent."""
    if not ai_available:
        mcp.disable(tags={AI_LIBS_TAG})


def test_ai_tools_present_when_ai_libs_available():
    mcp = _server_with_tools()
    _gate(mcp, ai_available=True)

    names = {t.name for t in asyncio.run(mcp.list_tools())}
    assert AI_TOOLS <= names
    assert "run_sql_query" in names


def test_ai_tools_absent_from_listing_when_ai_libs_unavailable():
    mcp = _server_with_tools()
    _gate(mcp, ai_available=False)

    names = {t.name for t in asyncio.run(mcp.list_tools())}
    # disable(tags=...) hides them from listing entirely, not just at call time.
    assert names.isdisjoint(AI_TOOLS)
    # The non-AI tool is unaffected.
    assert "run_sql_query" in names


def test_ai_tools_carry_the_gate_tag():
    """The gate relies on the tag being present on exactly the two AI tools."""
    mcp = _server_with_tools()
    tagged = {t.name for t in asyncio.run(mcp.list_tools()) if AI_LIBS_TAG in (t.tags or set())}
    assert tagged == AI_TOOLS


def test_gate_is_instance_safe_across_two_backends():
    """Two backends in one process: one with AI libs, one without — each exposes its own surface."""
    with_ai = _server_with_tools()
    without_ai = _server_with_tools()

    _gate(with_ai, ai_available=True)
    _gate(without_ai, ai_available=False)

    names_with = {t.name for t in asyncio.run(with_ai.list_tools())}
    names_without = {t.name for t in asyncio.run(without_ai.list_tools())}

    assert AI_TOOLS <= names_with
    assert names_without.isdisjoint(AI_TOOLS)
    # The disable transform on one backend did not leak to the other.
    assert "run_sql_query" in names_with and "run_sql_query" in names_without
