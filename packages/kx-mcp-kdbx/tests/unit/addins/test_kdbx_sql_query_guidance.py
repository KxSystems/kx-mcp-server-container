"""Tests for kdbx_sql_query_guidance.py."""

from unittest.mock import mock_open

from kx_mcp_kdbx.addins.kdbx_sql_query_guidance import kdbx_sql_query_guidance_impl


class TestKdbxSqlQueryGuidanceImpl:
    """Test kdbx_sql_query_guidance_impl function."""

    def test_guidance_file_read_success(self, mocker):
        """Test successful reading of guidance file."""
        # Arrange
        expected_content = "KDB SQL Query Guide content"
        mocker.patch("builtins.open", mock_open(read_data=expected_content))

        # Act
        result = kdbx_sql_query_guidance_impl()

        # Assert
        assert result == expected_content

    def test_guidance_file_encoding_verification(self, mocker):
        """Test file is opened with UTF-8 encoding."""
        # Arrange
        mock_file = mocker.patch("builtins.open", mock_open(read_data="content"))

        # Act
        kdbx_sql_query_guidance_impl()

        # Assert — the module resolves its co-located data file via __file__, then opens it.
        from kx_mcp_kdbx.addins.kdbx_sql_query_guidance import _GUIDANCE_PATH

        mock_file.assert_called_once_with(_GUIDANCE_PATH, 'r', encoding='utf-8')