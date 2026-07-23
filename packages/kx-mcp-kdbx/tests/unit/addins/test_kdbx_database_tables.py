"""Tests for kdbx_database_tables.py."""

import pytest
from mcp.types import TextContent
from unittest.mock import Mock

from kx_mcp_kdbx.addins.kdbx_database_tables import (
    kdbx_describe_table_impl,
    kdbx_describe_tables_impl,
)


class TestKdbxDescribeTableImpl:
    """Test kdbx_describe_table_impl function."""

    @pytest.mark.parametrize(
        "is_partitioned,expected_query_count",
        [
            (False, 3),  # non-partitioned: count, meta, sublist query
            (True, 3),   # partitioned: count, meta, .Q.ind query
        ]
    )
    @pytest.mark.anyio
    async def test_describe_table_success(self, mocker, is_partitioned, expected_query_count):
        """Test successful table description for partitioned and non-partitioned tables."""
        # Arrange
        table_name = "test_table"
        mock_conn = Mock()
        mock_conn.py.return_value = 5  # total_records
        mock_conn.meta.return_value.py.return_value = {"col1": "int", "col2": "string"}

        if is_partitioned:
            mock_conn.Q.pt.py.return_value = [table_name]  # table is partitioned
        else:
            mock_conn.Q.pt.py.return_value = ["other_table"]  # table not in partitioned tables

        mock_conn.side_effect = [
            mock_conn,  # count query
            mock_conn,  # meta query
            Mock(py=lambda: [
                {"col1": 1, "col2": "test1"},
                {"col1": 2, "col2": "test2"},
                {"col1": 3, "col2": "test3"},
                {"col1": 4, "col2": "test4"},
                {"col1": 5, "col2": "test5"}
            ])  # preview query - 5 rows of data
        ]

        mocker.patch("kx_mcp_kdbx.addins.kdbx_database_tables.get_kdb_connection", return_value=mock_conn)
        mocker.patch("kx_mcp_kdbx.addins.kdbx_database_tables.format_data_for_display", side_effect=["schema_formatted", "data_formatted"])

        # Act
        result = await kdbx_describe_table_impl(table_name)

        # Assert
        assert len(result) == 1
        assert isinstance(result[0], TextContent)
        assert f"TABLE ANALYSIS: {table_name}" in result[0].text
        assert "Schema Information:" in result[0].text
        assert f"Data Preview ({expected_query_count} records):" in result[0].text

    @pytest.mark.parametrize(
        "table_name,total_records,expected_text",
        [
            ("empty_table", 0, "Table is empty - no data to preview"),
        ]
    )
    @pytest.mark.anyio
    async def test_describe_table_empty(self, mocker, table_name, total_records, expected_text):
        """Test table description for empty table."""
        # Arrange
        mock_conn = Mock()
        mock_conn.py.return_value = total_records
        mock_conn.meta.return_value.py.return_value = {"col1": "int"}
        mock_conn.Q.pt.py.return_value = []
        mock_conn.side_effect = [mock_conn, mock_conn]  # count and meta queries only

        mocker.patch("kx_mcp_kdbx.addins.kdbx_database_tables.get_kdb_connection", return_value=mock_conn)
        mocker.patch("kx_mcp_kdbx.addins.kdbx_database_tables.format_data_for_display", return_value="schema_formatted")

        # Act
        result = await kdbx_describe_table_impl(table_name)

        # Assert
        assert len(result) == 1
        assert f"TABLE ANALYSIS: {table_name}" in result[0].text
        assert expected_text in result[0].text

    @pytest.mark.parametrize(
        "table_name,error_message",
        [
            ("test_table", "Connection failed"),
            ("bad_table", "Table not found"),
        ]
    )
    @pytest.mark.anyio
    async def test_describe_table_errors(self, mocker, table_name, error_message):
        """Test error handling when operations fail."""
        # Arrange
        mocker.patch("kx_mcp_kdbx.addins.kdbx_database_tables.get_kdb_connection", side_effect=Exception(error_message))
        mock_logger = mocker.patch("kx_mcp_kdbx.addins.kdbx_database_tables.logger")

        # Act
        result = await kdbx_describe_table_impl(table_name)

        # Assert
        assert len(result) == 1
        assert f"TABLE ANALYSIS FAILED: {table_name}" in result[0].text
        assert error_message in result[0].text
        mock_logger.error.assert_called_once_with(f"Failed to analyze table '{table_name}': {error_message}")


class TestKdbxDescribeTablesImpl:
    """Test kdbx_describe_tables_impl function."""

    @pytest.mark.parametrize(
        "table_list,expected_table_count,expected_content",
        [
            (["table1", "table2"], 2, "Found 2 table(s)"),
            (["single_table"], 1, "Found 1 table(s)"),
            ([], 0, "Database is empty - no tables found"),
        ]
    )
    @pytest.mark.anyio
    async def test_describe_tables_scenarios(self, mocker, table_list, expected_table_count, expected_content):
        """Test database overview with various table configurations."""
        # Arrange
        mock_conn = Mock()
        mock_conn.tables.return_value.py.return_value = table_list
        mocker.patch("kx_mcp_kdbx.addins.kdbx_database_tables.get_kdb_connection", return_value=mock_conn)

        if table_list:  # Only mock describe_table if we have tables
            mock_describe_table = mocker.patch("kx_mcp_kdbx.addins.kdbx_database_tables.kdbx_describe_table_impl")
            mock_describe_table.side_effect = [
                [TextContent(type="text", text=f"{table} analysis")] for table in table_list
            ]

        # Act
        result = await kdbx_describe_tables_impl()

        # Assert
        assert len(result) == 1
        assert isinstance(result[0], TextContent)
        assert expected_content in result[0].text

        if table_list:
            assert "DATABASE SCHEMA OVERVIEW" in result[0].text
            for table in table_list:
                assert f"{table} analysis" in result[0].text

    @pytest.mark.anyio
    async def test_describe_tables_connection_error(self, mocker):
        """Test error handling when database connection fails."""
        # Arrange
        mocker.patch("kx_mcp_kdbx.addins.kdbx_database_tables.get_kdb_connection", side_effect=Exception("DB connection error"))
        mock_logger = mocker.patch("kx_mcp_kdbx.addins.kdbx_database_tables.logger")

        # Act
        result = await kdbx_describe_tables_impl()

        # Assert
        assert len(result) == 1
        assert "DATABASE ANALYSIS ERROR" in result[0].text
        assert "DB connection error" in result[0].text
        mock_logger.error.assert_called_once_with("Database schema analysis failed: DB connection error")

class TestDescribeTablesDataGate:
    """The data gate scopes the table listing down to the entitled subset.

    This is the direct application of allow-with-obligations: the resource *filters* its output
    (unlike the SQL tool, which surfaces scope-down as denial-with-guidance because rewriting a
    query is a non-goal). A full deny yields a clean denial message, never a stack trace.
    """

    @staticmethod
    def _conn(mocker, tables):
        mock_conn = Mock()
        mock_conn.tables.return_value.py.return_value = list(tables)
        mocker.patch(
            "kx_mcp_kdbx.addins.kdbx_database_tables.get_kdb_connection", return_value=mock_conn
        )
        return mock_conn

    @staticmethod
    def _config():
        cfg = Mock()
        cfg.data_gate = True
        return cfg

    @pytest.mark.anyio
    async def test_scope_down_filters_the_listing(self, mocker):
        from kx_auth_core.authz import AuthzDecision
        self._conn(mocker, ["trades", "accounts"])
        mocker.patch(
            "kx_mcp_kdbx.addins.kdbx_database_tables.consult_data_gate",
            return_value=AuthzDecision(
                allowed=True, adapter="kdbx_entitlements",
                obligations={"entitled": ["trades"], "denied": ["accounts"]}),
        )
        describe = mocker.patch(
            "kx_mcp_kdbx.addins.kdbx_database_tables.kdbx_describe_table_impl",
            return_value=[TextContent(type="text", text="TABLE DETAIL")],
        )

        result = await kdbx_describe_tables_impl(config=self._config())

        text = result[0].text
        assert "Found 1 table(s)" in text
        assert "1 table(s) hidden by entitlements" in text
        # Only the entitled table was described.
        described = [c.args[0] for c in describe.call_args_list]
        assert described == ["trades"]

    @pytest.mark.anyio
    async def test_full_deny_yields_clean_denial(self, mocker):
        from kx_auth_core.authz import AuthzDecision
        self._conn(mocker, ["trades"])
        mocker.patch(
            "kx_mcp_kdbx.addins.kdbx_database_tables.consult_data_gate",
            return_value=AuthzDecision(
                allowed=False, adapter="kdbx_entitlements",
                reason="bob not permitted read on trades"),
        )

        result = await kdbx_describe_tables_impl(config=self._config())

        assert "DATABASE ACCESS DENIED" in result[0].text
        assert "bob not permitted read on trades" in result[0].text

    @pytest.mark.anyio
    async def test_gate_off_lists_everything_without_consulting(self, mocker):
        self._conn(mocker, ["trades", "accounts"])
        consult = mocker.patch("kx_mcp_kdbx.addins.kdbx_database_tables.consult_data_gate")
        mocker.patch(
            "kx_mcp_kdbx.addins.kdbx_database_tables.kdbx_describe_table_impl",
            return_value=[TextContent(type="text", text="TABLE DETAIL")],
        )
        cfg = Mock()
        cfg.data_gate = False

        result = await kdbx_describe_tables_impl(config=cfg)

        assert "Found 2 table(s)" in result[0].text
        consult.assert_not_called()
