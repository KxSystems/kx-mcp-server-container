"""Tests for kdbx_run_sql_query.py."""
import pytest
import json
from unittest.mock import Mock
from kx_mcp_kdbx.addins.kdbx_run_sql_query import run_query_impl

import importlib
import kx_auth_core.authz as _authz_registry
from kx_mcp_core.auth import (
    AuthorizationDenied,
    AuthzSettings,
    configure_authz,
    current_authz_decision,
)
from kx_auth_core.authz import AuthzDecision, register_authz_adapter

# `kx_mcp_core.auth.authorize` the attribute is the re-exported *function* (it shadows the submodule),
# so fetch the real module object to snapshot/restore its `_SETTINGS` global between tests.
_authorize_mod = importlib.import_module("kx_mcp_core.auth.authorize")


class TestRunQueryImpl:
    """Test the run_query_impl function."""

    @pytest.mark.anyio
    @pytest.mark.parametrize(
        "row_count,expected_data,expected_message",
        [
            (5, [{'id': 1, 'name': 'Alice'}, {'id': 2, 'name': 'Bob'}], None),
            (0, [], "No rows returned"),
            (1500, [{'id': 1, 'name': 'User1'}], "Showing first 1000 of 1500 rows"),
        ],
    )
    async def test_successful_query_execution(self, mocker, row_count, expected_data, expected_message):
        """Test successful query execution with various result scenarios."""
        # Setup test data (whats returned from kdbx)
        mock_result = {
            'rowCount': row_count,
            'data': Mock(py=lambda: json.dumps(expected_data).encode('utf-8'))
        }

        # Setup mocks
        mock_client = mocker.Mock()
        mock_client.return_value = mock_result
        mock_get_connection = mocker.patch('kx_mcp_kdbx.addins.kdbx_run_sql_query.get_kdb_connection', return_value=mock_client)

        # Execute
        result = await run_query_impl("SELECT * FROM users")

        # Verify result
        assert result['status'] == 'success'
        if row_count == 0:
            assert result['data'] == []
            assert result['message'] == expected_message
        elif row_count > 1000:
            assert len(result['data']) <= 2
            assert expected_message in result['message']
        else:
            assert len(result['data']) <= 2
            if expected_message:
                assert result['message'] == expected_message

        # Verify function calls
        mock_get_connection.assert_called_once()


    @pytest.mark.anyio
    @pytest.mark.parametrize("dangerous_query,keyword", [
        ("INSERT INTO users VALUES (1, 'test')", "INSERT"),
        ("DROP TABLE users", "DROP"),
        ("DELETE FROM users WHERE id=1", "DELETE"),
        ("TRUNCATE TABLE users", "TRUNCATE"),
        ("ALTER TABLE users ADD column test", "ALTER"),
        ("CREATE TABLE test (id int)", "CREATE"),
    ])
    async def test_dangerous_keywords_blocked(self, dangerous_query, keyword):
        """Test that queries with dangerous keywords are blocked."""
        # Execute
        result = await run_query_impl(dangerous_query)

        # Verify result
        assert result['status'] == 'error'
        assert f'Query contains dangerous keyword: {keyword}' in result['message']

    @pytest.mark.anyio
    async def test_select_with_dangerous_keyword_allowed(self, mocker):
        """Test that SELECT queries containing dangerous keywords in strings are allowed."""
        # Setup test data
        expected_result = [{'message': 'INSERT successful'}]
        mock_result = {
            'rowCount': 1,
            'data': Mock(py=lambda: json.dumps(expected_result).encode('utf-8'))
        }

        # Setup mocks
        mock_client = mocker.Mock()
        mock_client.return_value = mock_result
        mock_get_connection = mocker.patch('kx_mcp_kdbx.addins.kdbx_run_sql_query.get_kdb_connection', return_value=mock_client)

        # Execute
        result = await run_query_impl("SELECT 'INSERT successful' as message")

        # Verify result
        assert result['status'] == 'success'
        assert result['data'] == expected_result

        # Verify function calls
        mock_get_connection.assert_called_once()

    @pytest.mark.anyio
    @pytest.mark.parametrize(
        "exception_msg,expected_error_type,expected_msg_contains",
        [
            ("Some error with .s.e in it", "sql_interface_not_loaded", "SQL interface is not loaded"),
            ("Connection timeout", None, "Connection timeout"),
            # A q-side identity-assertion denial surfaces as a clean permission_denied envelope.
            ("denied: no valid principal bound to this handle", "permission_denied", "Access denied by the data layer"),
        ],
    )
    async def test_error_handling(self, mocker, exception_msg, expected_error_type, expected_msg_contains):
        """Test handling of various errors during query execution."""
        # Setup mocks
        mock_client = mocker.Mock()
        mock_client.side_effect = Exception(exception_msg)
        mock_get_connection = mocker.patch('kx_mcp_kdbx.addins.kdbx_run_sql_query.get_kdb_connection', return_value=mock_client)

        # Execute
        result = await run_query_impl("SELECT * FROM users")

        # Verify result
        assert result['status'] == 'error'
        assert expected_msg_contains in result['message']
        if expected_error_type:
            assert result['error_type'] == expected_error_type
            assert 'technical_details' in result
        else:
            assert 'error_type' not in result

        # Verify function calls
        mock_get_connection.assert_called_once()

    @pytest.mark.anyio
    async def test_json_parsing_with_special_characters(self, mocker):
        """Test JSON parsing handles special characters correctly."""
        # Setup test data
        expected_data = [{'name': 'Test "quoted" value', 'desc': 'Line 1\nLine 2'}]
        mock_result = {
            'rowCount': 1,
            'data': Mock(py=lambda: json.dumps(expected_data).encode('utf-8'))
        }

        # Setup mocks
        mock_client = mocker.Mock()
        mock_client.return_value = mock_result
        mock_get_connection = mocker.patch('kx_mcp_kdbx.addins.kdbx_run_sql_query.get_kdb_connection', return_value=mock_client)

        # Execute
        result = await run_query_impl("SELECT * FROM special_chars")

        # Verify result
        assert result['status'] == 'success'
        assert result['data'][0]['name'] == 'Test "quoted" value'
        assert result['data'][0]['desc'] == 'Line 1\nLine 2'

        # Verify function calls
        mock_get_connection.assert_called_once()


class TestSqlToolCapabilityDecorator:
    """The `@authorize(action="query", resource="kdbx:sql")` capability check is wired onto
    `run_query_impl`.

    Drives the decorator with a *fake* authz strategy (no live q) to prove the wiring: an allow lets
    the query run, a deny raises `AuthorizationDenied` *before* any q call, and route-only (`KX_MCP_AUTHZ`
    unset) leaves dispatch unchanged. The real `kdbx_rbac` -> q `.kx.auth` path is proven by
    `test_authz_kx_rbac.py` (adapter level) + the live demo / `realidp/kdbx` lane (end-to-end)."""

    @pytest.fixture(autouse=True)
    def _isolate_authz(self):
        # Snapshot + restore the process-global authz state so a fake strategy never leaks into the
        # other run_query_impl tests (which rely on route-only allow when KX_MCP_AUTHZ is unset).
        saved_adapters = dict(_authz_registry._ADAPTERS)
        saved_settings = _authorize_mod._SETTINGS
        yield
        _authz_registry._ADAPTERS.clear()
        _authz_registry._ADAPTERS.update(saved_adapters)
        _authorize_mod._SETTINGS = saved_settings

    @staticmethod
    def _mock_conn(mocker, rows):
        result = {'rowCount': len(rows), 'data': Mock(py=lambda: json.dumps(rows).encode('utf-8'))}
        conn = mocker.Mock(return_value=result)
        mocker.patch('kx_mcp_kdbx.addins.kdbx_run_sql_query.get_kdb_connection', return_value=conn)
        return conn

    @pytest.mark.anyio
    async def test_allow_lets_query_run(self, mocker):
        register_authz_adapter("faketest", lambda req: True)
        configure_authz(AuthzSettings(mode="faketest"))
        rows = [{'sym': 'AAPL', 'price': 187.45}]
        self._mock_conn(mocker, rows)

        result = await run_query_impl("SELECT * FROM trades")

        assert result['status'] == 'success'
        assert result['data'] == rows

    @pytest.mark.anyio
    async def test_static_policy_allows_real_kdbx_query_without_kx_auth(
        self, mocker, tmp_path
    ):
        """The real static adapter gates the shipped `query` capability entirely in Python."""
        policy = tmp_path / "capability-policy.yaml"
        policy.write_text("kdbx:\n  query: [analyst, admin]\n")
        configure_authz(AuthzSettings(mode="static", policy_file=str(policy)))
        mocker.patch.object(
            _authorize_mod,
            "current_principal",
            return_value=Mock(client_id="kx-mcp", claims={"sub": "alice", "groups": ["analyst"]}),
        )
        rows = [{'sym': 'AAPL', 'price': 187.45}]
        conn = self._mock_conn(mocker, rows)

        result = await run_query_impl("SELECT * FROM trades", config=Mock(data_gate=False))

        assert result['status'] == 'success'
        assert result['data'] == rows
        decision = current_authz_decision.get()
        assert decision is not None
        assert decision.allowed is True
        assert decision.adapter == "static"
        conn.assert_called_once()  # a single .s.e call -> no q-side .kx.auth round-trip

    @pytest.mark.anyio
    async def test_deny_raises_authorization_denied_before_query(self, mocker):
        register_authz_adapter(
            "faketest", lambda req: AuthzDecision(False, reason="bob not permitted")
        )
        configure_authz(AuthzSettings(mode="faketest"))
        conn = self._mock_conn(mocker, [{'sym': 'AAPL'}])

        with pytest.raises(AuthorizationDenied) as ei:
            await run_query_impl("SELECT * FROM trades")

        # The decorator runs before the body, so the message names the capability and the query never ran.
        assert "query on kdbx:sql" in str(ei.value)
        conn.assert_not_called()

    @pytest.mark.anyio
    async def test_route_only_when_authz_unset(self, mocker):
        configure_authz(AuthzSettings(mode=""))  # KX_MCP_AUTHZ unset -> no adapter -> allow
        rows = [{'sym': 'AAPL'}]
        self._mock_conn(mocker, rows)

        result = await run_query_impl("SELECT * FROM trades")

        assert result['status'] == 'success'
        assert result['data'] == rows


class TestSqlToolDataGate:
    """The data gate (KDBX_DB_DATA_GATE) wired into `run_query_impl`.

    Drives the tool with a mocked `consult_data_gate` (the adapter itself is covered by
    `test_authz_kx_entitlements.py`) to pin the tool-side application semantics: deny → structured
    permission_denied and the query NEVER runs; scope-down → the same envelope carrying the entitled
    subset as guidance (SQL is never rewritten — design non-goal); full allow → the query runs
    unchanged; flag off (default) → byte-identical legacy behaviour, no consult at all.
    """

    @staticmethod
    def _gated_conn(mocker, rows, known_tables=("trades", "accounts")):
        """A conn serving the gate's tables[] fetch AND the JSON query path."""
        query_result = {
            'rowCount': len(rows),
            'data': Mock(py=lambda: json.dumps(rows).encode('utf-8')),
        }

        def dispatch(*args):
            if args and args[0] == 'tables[]':
                return Mock(py=Mock(return_value=list(known_tables)))
            return query_result

        conn = mocker.Mock(side_effect=dispatch)
        mocker.patch('kx_mcp_kdbx.addins.kdbx_run_sql_query.get_kdb_connection', return_value=conn)
        return conn

    @staticmethod
    def _config(data_gate=True):
        cfg = Mock()
        cfg.data_gate = data_gate
        return cfg

    @pytest.mark.anyio
    async def test_deny_returns_permission_denied_and_query_never_runs(self, mocker):
        from kx_auth_core.authz import AuthzDecision
        conn = self._gated_conn(mocker, [{'sym': 'AAPL'}])
        consult = mocker.patch(
            'kx_mcp_kdbx.addins.kdbx_run_sql_query.consult_data_gate',
            return_value=AuthzDecision(
                allowed=False, adapter="kdbx_entitlements",
                reason="bob not permitted read on trades"),
        )

        result = await run_query_impl("SELECT * FROM trades", config=self._config())

        assert result['status'] == 'error'
        assert result['error_type'] == 'permission_denied'
        assert "bob not permitted read on trades" in result['message']
        consult.assert_called_once_with("read", ["trades"])
        # Only the tables[] fetch reached q — the .s.e query call never happened.
        assert conn.call_count == 1

    @pytest.mark.anyio
    async def test_scope_down_returns_guidance_and_query_never_runs(self, mocker):
        from kx_auth_core.authz import AuthzDecision
        conn = self._gated_conn(mocker, [{'sym': 'AAPL'}])
        mocker.patch(
            'kx_mcp_kdbx.addins.kdbx_run_sql_query.consult_data_gate',
            return_value=AuthzDecision(
                allowed=True, adapter="kdbx_entitlements",
                reason="scoped down: alice not permitted read on accounts",
                obligations={"entitled": ["trades"], "denied": ["accounts"]}),
        )

        result = await run_query_impl(
            "SELECT * FROM trades JOIN accounts ON trades.sym = accounts.sym",
            config=self._config(),
        )

        assert result['error_type'] == 'permission_denied'
        assert result['entitled_tables'] == ["trades"]
        assert result['denied_tables'] == ["accounts"]
        assert conn.call_count == 1

    @pytest.mark.anyio
    async def test_allow_runs_query_unchanged(self, mocker):
        from kx_auth_core.authz import AuthzDecision
        rows = [{'sym': 'AAPL', 'price': 187.45}]
        conn = self._gated_conn(mocker, rows)
        consult = mocker.patch(
            'kx_mcp_kdbx.addins.kdbx_run_sql_query.consult_data_gate',
            return_value=AuthzDecision(allowed=True, adapter="kdbx_entitlements"),
        )

        result = await run_query_impl("SELECT * FROM trades", config=self._config())

        assert result['status'] == 'success'
        assert result['data'] == rows
        consult.assert_called_once_with("read", ["trades"])
        assert conn.call_count == 2  # tables[] + the query

    @pytest.mark.anyio
    async def test_no_referenced_tables_skips_the_consult(self, mocker):
        conn = self._gated_conn(mocker, [{'x': 2}])
        consult = mocker.patch('kx_mcp_kdbx.addins.kdbx_run_sql_query.consult_data_gate')

        result = await run_query_impl("SELECT 1+1", config=self._config())

        assert result['status'] == 'success'
        consult.assert_not_called()
        assert conn.call_count == 2

    @pytest.mark.anyio
    async def test_gate_off_is_byte_identical_legacy_path(self, mocker):
        rows = [{'sym': 'AAPL'}]
        conn = self._gated_conn(mocker, rows)
        consult = mocker.patch('kx_mcp_kdbx.addins.kdbx_run_sql_query.consult_data_gate')

        result = await run_query_impl("SELECT * FROM trades", config=self._config(data_gate=False))

        assert result['status'] == 'success'
        assert result['data'] == rows
        consult.assert_not_called()
        assert conn.call_count == 1  # no tables[] fetch either
