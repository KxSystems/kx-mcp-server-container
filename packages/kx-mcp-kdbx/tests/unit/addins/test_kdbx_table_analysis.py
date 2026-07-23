"""Tests for kdbx_table_analysis.py."""

import pytest

from kx_mcp_kdbx.addins.kdbx_table_analysis import table_deep_dive_prompt_impl


class TestTableDeepDivePromptImpl:
    """Test table_deep_dive_prompt_impl function."""

    @pytest.mark.anyio
    async def test_prompt_generation_success(self):
        """Test successful prompt generation."""
        # Act
        result = await table_deep_dive_prompt_impl("test_table", "statistical", 100)

        # Assert
        assert isinstance(result, str)
        assert "table: test_table" in result
        assert "Analysis type: Statistical" in result
        assert "LIMIT 100" in result