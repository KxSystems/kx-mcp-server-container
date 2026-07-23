"""Prompt: table analysis generates a prompt and registers a bare name."""

import asyncio

from kx_mcp_kdbai.addins import kdbai_table_analysis as mod


def test_overview_prompt_mentions_table():
    p = asyncio.run(mod.kdbai_table_analysis_prompt_impl("docs", "overview", 10))
    assert "docs" in p
    assert "Table Overview" in p
    assert "Analysis type: overview" in p


def test_invalid_type_defaults_to_overview():
    p = asyncio.run(mod.kdbai_table_analysis_prompt_impl("docs", "bogus", 5))
    assert "Analysis type: overview" in p

# The bare-name registration is covered by test_kdbai_addins_init.py (native discovery of the
# standalone @prompt component).
