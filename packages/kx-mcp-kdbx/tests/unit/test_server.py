"""Tests for McpServer."""
import logging
import pytest
from kx_mcp_kdbx.server import McpServer


@pytest.fixture
def mock_config(mocker):
    """Mock config to be used in tests.

    Mount-only: the bundle's AppSettings carries DB config only (no serving config) — the container
    owns transport/host/port.
    """
    config = mocker.Mock()
    config.db.host = "localhost"
    config.db.port = 5001
    config.db.username = "user"
    config.db.password.get_secret_value.return_value = "test-pass"
    config.db.timeout = 5
    config.db.retry = 2
    config.db.tls = False
    return config


@pytest.fixture
def mock_dependencies(mocker):
    """Mock all external dependencies."""
    mocks = {
        'fastmcp': mocker.patch('kx_mcp_kdbx.server.FastMCP'),
        'addins': mocker.patch('kx_mcp_kdbx.server.register_addins'),
        'pykx_connection': mocker.patch('pykx.SyncQConnection'),
    }

    # Set default return values (tools + prompts + resources, now one combined list)
    mocks['addins'].return_value = ["run_sql_query", "similarity_search", "table_analysis"]

    return mocks


class TestMcpServer:
    """Test server initialization and KDB-X connection (mount-only: no serving/port checks)."""

    def test_initialization_success(self, mock_config, mock_dependencies):
        """Ensures server properly initializes with config values"""
        from kx_mcp_kdbx.server import SERVER_NAME

        server = McpServer(mock_config)

        assert server.db_config == mock_config.db

        # Mount-only: the container owns transport/host/port; the bundle constructs FastMCP with
        # only the module-level SERVER_NAME constant (no serving config to source the name from).
        mock_dependencies['fastmcp'].assert_called_once_with(SERVER_NAME)

    def test_registration_failure(self, mock_config, mock_dependencies, caplog):
        """Validates server fails gracefully with proper error logging when component registration fails"""
        error_msg = "Add-in registration failed"
        mock_dependencies['addins'].side_effect = Exception(error_msg)

        with pytest.raises(Exception, match=error_msg):
            McpServer(mock_config)

        # Verify error is logged
        assert "Failed to register add-ins" in caplog.text
        assert error_msg in caplog.text

    def test_registration_success_logging(self, mock_config, mock_dependencies, caplog):
        """Ensures successful component registration is properly logged for debugging and monitoring"""
        with caplog.at_level(logging.INFO):
            McpServer(mock_config)

        # Verify success logging (using default fixture values — one combined add-ins list)
        assert "Successfully registered 3 add-ins" in caplog.text

    def test_kdbx_connection_success(self, mock_config, mock_dependencies, mocker):
        """Test successful KDB-X connection"""
        mock_client = mock_dependencies['pykx_connection'].return_value

        # Mock the query responses
        mock_version_result = mocker.Mock()
        mock_version_result.py.return_value = b'0.1.2'  # Return bytes that can be decoded

        mock_sql_check_result = mocker.Mock()
        mock_sql_check_result.py.return_value = True

        mock_ai_check_result = mocker.Mock()
        mock_ai_check_result.py.return_value = False

        # Set up the call method to return different results based on the query
        mock_client.side_effect = [mock_version_result, mock_sql_check_result, mock_ai_check_result]

        McpServer(mock_config)

        mock_dependencies['pykx_connection'].assert_called_once_with(
            host="localhost",
            port=5001,
            username="user",
            password="test-pass",
            timeout=5,
            tls=False,
        )
        mock_client.close.assert_called_once()

    def test_kdbx_connection_sql_interface_missing(self, mock_config, mock_dependencies, mocker):
        """Test KDB-X connection when SQL interface is not loaded"""
        mock_client = mock_dependencies['pykx_connection'].return_value

        # Mock the query responses
        mock_version_result = mocker.Mock()
        mock_version_result.py.return_value = b'0.1.2'  # Return bytes that can be decoded

        mock_sql_check_result = mocker.Mock()
        mock_sql_check_result.py.return_value = False  # SQL interface not loaded

        # Set up the call method to return different results based on the query
        mock_client.side_effect = [mock_version_result, mock_sql_check_result]

        with pytest.raises(SystemExit):
            McpServer(mock_config)

        mock_dependencies['pykx_connection'].assert_called_once()

    def test_kdbx_connection_auth_failure(self, mock_config, mock_dependencies):
        """Test KDB-X connection with authentication failure"""
        from pykx.exceptions import QError
        mock_dependencies['pykx_connection'].side_effect = QError("invalid username/password")

        with pytest.raises(SystemExit):
            McpServer(mock_config)

    def test_kdbx_connection_refused(self, mock_config, mock_dependencies):
        """Ensures server fails fast when KDB-X is unreachable to avoid runtime errors"""
        from pykx.exceptions import QError
        mock_dependencies['pykx_connection'].side_effect = QError("Connection refused")

        with pytest.raises(SystemExit):
            McpServer(mock_config)


def test_importing_the_bundle_registers_the_kdbx_rbac_authz_adapter():
    """Loading the kdb-x bundle must register the `kdbx_rbac` capability-check authz adapter.

    The SQL tool's `@authorize` routes a `query`/`kdbx:sql` capability check to `decide(strategy=
    "kdbx_rbac")` when `KX_MCP_AUTHZ=kdbx_rbac`. Since the tool no longer imports `authz_kx_rbac`
    directly, `server.py` imports it for its self-registration side effect — importing this test's
    `kx_mcp_kdbx.server` is enough to make the adapter available. Regression guard for that import."""
    from kx_auth_core.authz import authz_adapters

    assert "kdbx_rbac" in authz_adapters()
