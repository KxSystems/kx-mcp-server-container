"""Tests for KDB-X connection utilities."""

import logging
import pytest
from kx_mcp_kdbx.utils.kdbx import (
    MalformedPrincipal,
    _is_dead_handle,
    cleanup_kdb_connection,
    get_kdb_connection,
    kdb_sync_connection,
)
from kx_mcp_kdbx.utils.denial import is_denial
from kx_mcp_kdbx.settings import KDBConfig
from pydantic import SecretStr, ValidationError
from pykx.exceptions import QError


class TestGetKdbConnection:
    """Test cases for get_kdb_connection function."""

    def test_get_kdb_connection_success(self, mocker):
        """Test successful KDB connection retrieval."""
        # Arrange
        mock_conn = mocker.Mock()
        mock_conn.return_value = None  # Successful connection test
        mocker.patch("kx_mcp_kdbx.utils.kdbx.kdb_sync_connection", return_value=mock_conn)

        # Act
        result = get_kdb_connection()

        # Assert
        assert result == mock_conn
        mock_conn.assert_called_once_with('')

    def test_get_kdb_connection_closed_connection_reinitialize(self, mocker, caplog):
        """Test KDB connection reinitialize when connection is closed."""
        # Arrange
        caplog.set_level(logging.INFO)
        mock_conn = mocker.Mock()
        mock_conn.side_effect = [
            Exception("Attempted to use a closed IPC connection"),
            None  # Second call succeeds
        ]
        mocker.patch("kx_mcp_kdbx.utils.kdbx.cleanup_kdb_connection")
        mocker.patch("kx_mcp_kdbx.utils.kdbx.kdb_sync_connection", return_value=mock_conn)

        # Act
        result = get_kdb_connection()

        # Assert
        assert result == mock_conn
        assert "KDB-X connection was closed. Reinitializing..." in caplog.text
        assert mock_conn.call_count == 2

    def test_get_kdb_connection_other_error(self, mocker, caplog):
        # Arrange
        """Test KDB connection with other types of errors."""
        caplog.set_level(logging.ERROR)
        mock_conn = mocker.Mock()
        mock_conn.side_effect = Exception("Some other error")
        mocker.patch("kx_mcp_kdbx.utils.kdbx.kdb_sync_connection", return_value=mock_conn)

        # Act
        with pytest.raises(Exception, match="Some other error"):
            get_kdb_connection()

        # Assert
        assert "Error in creating KDBX connection: Some other error" in caplog.text


class TestKdbSyncConnection:
    """Test cases for kdb_sync_connection function."""

    def test_kdb_sync_connection_success(self, mocker, caplog):
        """Test successful KDB connection on first attempt."""
        # Arrange
        caplog.set_level(logging.INFO)
        config = KDBConfig(host="localhost", port=5000, username="test", password=SecretStr("pass"))
        mock_conn = mocker.Mock()
        mock_kx = mocker.patch("kx_mcp_kdbx.utils.kdbx.kx")
        mock_kx.SyncQConnection.return_value = mock_conn

        # Act
        result = kdb_sync_connection(config)

        # Assert
        assert result == mock_conn
        assert "Connecting to KDB at localhost:5000" in caplog.text
        assert "Connected to Q/KDB-X" in caplog.text

    def test_kdb_sync_connection_retry_then_success(self, mocker, caplog):
        """Test KDB connection succeeds after retry."""
        # Arrange
        caplog.set_level(logging.INFO)
        cleanup_kdb_connection()  # Clear cache

        config = KDBConfig(host="localhost", port=5000, username="test", password=SecretStr("pass"), retry=3)
        mock_conn = mocker.Mock()
        mock_kx = mocker.patch("kx_mcp_kdbx.utils.kdbx.kx")

        call_count = 0
        def sync_q_connection_side_effect(*args, **kwargs):
            # mock needs to accept all args/kwargs, but will discard
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise ConnectionError("Connection failed")
            return mock_conn

        mock_kx.SyncQConnection.side_effect = sync_q_connection_side_effect

        # Act
        result = kdb_sync_connection(config)

        # Assert
        assert result == mock_conn
        assert "KDB-X connectivity attempt 1/4 failed: Connection failed" in caplog.text  # retry=3: 4 attempts
        assert call_count == 2

    def test_kdb_sync_connection_all_attempts_fail(self, mocker, caplog):
        """Test KDB connection fails after the first attempt and all `retry` retries."""
        # Arrange
        caplog.set_level(logging.INFO)
        cleanup_kdb_connection()  # Clear cache

        config = KDBConfig(host="localhost", port=5000, username="test", password=SecretStr("pass"), retry=2)
        mock_kx = mocker.patch("kx_mcp_kdbx.utils.kdbx.kx")
        mock_kx.SyncQConnection.side_effect = ConnectionError("Connection failed")

        # Act
        with pytest.raises(Exception):
            kdb_sync_connection(config)

        # Assert
        assert "Failed to connect to KDB" in caplog.text
        assert "KDB-X connectivity attempt 1/3 failed: Connection failed" in caplog.text
        assert "KDB-X connectivity attempt 3/3 failed: Connection failed" in caplog.text
        assert mock_kx.SyncQConnection.call_count == 3

    def test_kdb_sync_connection_uses_default_config(self, mocker, caplog):
        """Test KDB connection uses default config when none provided."""
        # Arrange
        caplog.set_level(logging.INFO)
        default_config = KDBConfig(host="default_host", port=6000, username="user", password=SecretStr("pass"))
        mock_conn = mocker.Mock()
        mock_kx = mocker.patch("kx_mcp_kdbx.utils.kdbx.kx")
        mock_kx.SyncQConnection.return_value = mock_conn
        mocker.patch("kx_mcp_kdbx.utils.kdbx.db_config", default_config)

        # Act
        result = kdb_sync_connection()

        # Assert
        assert result == mock_conn
        assert "Connecting to KDB at default_host:6000" in caplog.text

    def test_evicted_connections_are_closed(self, mocker):
        """REGRESSION (see mcp-container/adversarial-review-2026-08.md § kdb-x backend).

        kdb_sync_connection's @lru_cache(maxsize=128) has no eviction hook — the 129th distinct
        config evicts the oldest cached connection from the CACHE, but nothing calls .close() on
        it, leaking the underlying qIPC socket until GC happens to reclaim the object."""
        cleanup_kdb_connection()
        created = []

        def _new_conn(*a, **k):
            m = mocker.Mock()
            created.append(m)
            return m

        mock_kx = mocker.patch("kx_mcp_kdbx.utils.kdbx.kx")
        mock_kx.SyncQConnection.side_effect = _new_conn

        for i in range(129):
            cfg = KDBConfig(host="localhost", port=5000, username=f"u{i}", password=SecretStr("pw"))
            kdb_sync_connection(cfg, None)

        assert kdb_sync_connection.cache_info().currsize == 128
        created[0].close.assert_called_once()
        cleanup_kdb_connection()


class _FakePrincipal:
    """Stand-in for the validated AccessToken the connection layer reads."""
    subject = "alice"
    client_id = "alice-client"
    scopes = ["kdbx.read"]
    expires_at = 1893456000
    resource = "kx-mcp"
    claims = {"iss": "https://issuer.test"}


class _FakeBob(_FakePrincipal):
    subject = "bob"
    client_id = "bob-client"


class _FakeFastmcpPrincipal:
    """How fastmcp's JWTVerifier actually shapes the token: `subject` left None, `client_id` folded
    down to azp, the real user identity only in the `sub` claim. Two such users on one OAuth client
    must still get distinct handles (keyed on the sub claim, not the shared client_id)."""
    subject = None
    client_id = "kx-mcp"
    scopes = ["profile"]
    expires_at = 1893456000
    resource = "kx-mcp"
    claims = {
        "sub": "uuid-alice",
        "iss": "https://issuer.test",
        "realm_access": {"roles": ["traders", "offline_access"]},
    }


class TestIdentityAssertion:
    """Project + bind the inbound principal in the connection layer (opt-in)."""

    def test_assert_identity_off_does_not_read_or_bind(self, mocker):
        """Flag off → no principal read, no bind."""
        cfg = KDBConfig(assert_identity=False)
        mock_conn = mocker.Mock()
        mocker.patch("kx_mcp_kdbx.utils.kdbx.kdb_sync_connection", return_value=mock_conn)
        spy_principal = mocker.patch("kx_mcp_kdbx.utils.kdbx._current_principal")

        get_kdb_connection(cfg)

        spy_principal.assert_not_called()
        bind_calls = [c for c in mock_conn.call_args_list if c.args and c.args[0] == ".kx.auth.bind"]
        assert bind_calls == []

    def test_assert_identity_on_binds_ferry_shape(self, mocker):
        """Flag on + principal present → `.kx.auth.bind` called with the ferry dict (no groups: q
        promotes that from claims). Structured fields + raw claims ride; groups is absent here."""
        cfg = KDBConfig(assert_identity=True, username="svc", password="pw")
        mock_conn = mocker.Mock()
        mocker.patch("kx_mcp_kdbx.utils.kdbx.kdb_sync_connection", return_value=mock_conn)
        mocker.patch("kx_mcp_kdbx.utils.kdbx._current_principal", return_value=_FakePrincipal())

        get_kdb_connection(cfg)

        bind_calls = [c for c in mock_conn.call_args_list if c.args and c.args[0] == ".kx.auth.bind"]
        assert len(bind_calls) == 1
        wire = bind_calls[0].args[1]
        assert wire["sub"] == "alice"
        assert wire["scopes"] == ["kdbx.read"]
        assert wire["aud"] == "kx-mcp"
        assert wire["iss"] == "https://issuer.test"
        assert "groups" not in wire  # promotion is q-side now
        assert "claims" in wire

    def test_assert_identity_on_without_principal_leaves_unbound(self, mocker):
        """Flag on + no principal → opened unbound (no bind); q-side default-deny refuses queries."""
        cfg = KDBConfig(assert_identity=True, username="svc", password="pw")
        mock_conn = mocker.Mock()
        mocker.patch("kx_mcp_kdbx.utils.kdbx.kdb_sync_connection", return_value=mock_conn)
        mocker.patch("kx_mcp_kdbx.utils.kdbx._current_principal", return_value=None)

        get_kdb_connection(cfg)

        bind_calls = [c for c in mock_conn.call_args_list if c.args and c.args[0] == ".kx.auth.bind"]
        assert bind_calls == []

    def test_distinct_principals_get_distinct_cache_keys(self, mocker):
        """Each principal partitions the connection cache → its own handle."""
        cfg = KDBConfig(assert_identity=True, username="svc", password="pw")
        mock_conn = mocker.Mock()
        mock_sync = mocker.patch("kx_mcp_kdbx.utils.kdbx.kdb_sync_connection", return_value=mock_conn)

        mocker.patch("kx_mcp_kdbx.utils.kdbx._current_principal", return_value=_FakePrincipal())
        get_kdb_connection(cfg)
        mocker.patch("kx_mcp_kdbx.utils.kdbx._current_principal", return_value=_FakeBob())
        get_kdb_connection(cfg)

        keys = [c.args[1] for c in mock_sync.call_args_list]  # (config, principal_key)
        assert keys[0] == ("alice", "https://issuer.test")
        assert keys[1] == ("bob", "https://issuer.test")
        assert keys[0] != keys[1]

    def test_same_principal_reuses_cache_key(self, mocker):
        cfg = KDBConfig(assert_identity=True, username="svc", password="pw")
        mock_conn = mocker.Mock()
        mock_sync = mocker.patch("kx_mcp_kdbx.utils.kdbx.kdb_sync_connection", return_value=mock_conn)
        mocker.patch("kx_mcp_kdbx.utils.kdbx._current_principal", return_value=_FakePrincipal())

        get_kdb_connection(cfg)
        get_kdb_connection(cfg)

        keys = [c.args[1] for c in mock_sync.call_args_list]
        assert keys[0] == keys[1] == ("alice", "https://issuer.test")

    def test_subject_falls_back_to_sub_claim_when_fastmcp_leaves_it_none(self, mocker):
        """Regression: fastmcp leaves AccessToken.subject None and folds client_id to azp, so the
        key + bound sub must come from the `sub` claim — else every user on one client collapses to
        a single handle and the wrong identity is asserted."""
        cfg = KDBConfig(assert_identity=True, username="svc", password="pw")
        mock_conn = mocker.Mock()
        mock_sync = mocker.patch("kx_mcp_kdbx.utils.kdbx.kdb_sync_connection", return_value=mock_conn)
        mocker.patch("kx_mcp_kdbx.utils.kdbx._current_principal", return_value=_FakeFastmcpPrincipal())

        get_kdb_connection(cfg)

        assert mock_sync.call_args_list[0].args[1] == ("uuid-alice", "https://issuer.test")
        wire = [c for c in mock_conn.call_args_list if c.args and c.args[0] == ".kx.auth.bind"][0].args[1]
        assert wire["sub"] == "uuid-alice"

    def test_raw_claims_ferried_for_q_side_promotion(self, mocker):
        """Groups are NOT extracted Python-side; the raw claims (realm_access.roles) ride along so q's
        promote can extract them. The ferry carries claims, not a promoted `groups` key."""
        cfg = KDBConfig(assert_identity=True, username="svc", password="pw")
        mock_conn = mocker.Mock()
        mocker.patch("kx_mcp_kdbx.utils.kdbx.kdb_sync_connection", return_value=mock_conn)
        mocker.patch("kx_mcp_kdbx.utils.kdbx._current_principal", return_value=_FakeFastmcpPrincipal())

        get_kdb_connection(cfg)

        wire = [c for c in mock_conn.call_args_list if c.args and c.args[0] == ".kx.auth.bind"][0].args[1]
        assert "groups" not in wire  # q promotes from claims, not the container
        # the realm_access.roles claim survives so q can extract groups from it
        roles = wire["claims"]["realm_access"]["roles"]
        assert [str(r) for r in roles] == ["traders", "offline_access"]

    def test_claims_string_values_charvec_not_symbol(self, mocker):
        """Raw claim string values are ferried as q char vectors (10h), so high-cardinality values
        like `jti` never intern as symbols. PyKX would map a Python str -> q symbol otherwise."""
        import pykx as kx

        cfg = KDBConfig(assert_identity=True, username="svc", password="pw")
        mock_conn = mocker.Mock()
        mocker.patch("kx_mcp_kdbx.utils.kdbx.kdb_sync_connection", return_value=mock_conn)
        mocker.patch("kx_mcp_kdbx.utils.kdbx._current_principal", return_value=_FakeFastmcpPrincipal())

        get_kdb_connection(cfg)

        wire = [c for c in mock_conn.call_args_list if c.args and c.args[0] == ".kx.auth.bind"][0].args[1]
        # the `sub` claim value (a string) is wrapped as a CharVector, not left as a Python str
        assert isinstance(wire["claims"]["sub"], kx.CharVector)

    def test_deeply_nested_claims_does_not_let_a_raw_recursion_error_escape(self, mocker):
        """REGRESSION (see mcp-container/adversarial-review-2026-08.md § kdb-x backend).

        _charvec recurses with no depth guard. get_kdb_connection/_bind_principal must not let a
        raw RecursionError escape on a pathologically deep claims value — the tool-level broad
        excepts that currently absorb this are incidental, not a guarantee this function itself
        makes."""
        cfg = KDBConfig(assert_identity=True, username="svc", password="pw")
        mock_conn = mocker.Mock()
        mocker.patch("kx_mcp_kdbx.utils.kdbx.kdb_sync_connection", return_value=mock_conn)

        deep = {}
        node = deep
        for _ in range(3000):
            node["nested"] = {}
            node = node["nested"]

        class _DeepPrincipal(_FakePrincipal):
            claims = {"sub": "alice", "deep": deep}

        mocker.patch("kx_mcp_kdbx.utils.kdbx._current_principal", return_value=_DeepPrincipal())

        with pytest.raises(MalformedPrincipal) as exc:
            get_kdb_connection(cfg)
        # A denial, not an infra fault: the message carries q's `denied:` prefix, so denial.py maps
        # it to the same structured permission_denied a q-side refusal produces. Asserted rather
        # than a bare `except Exception`, which would also have swallowed a typo in this test.
        assert str(exc.value).startswith("denied:")
        assert is_denial(exc.value)

    @pytest.mark.parametrize("bad_sub", [12345, 12.5, {"nested": "object"}, ["a", "b"], True])
    def test_a_non_string_sub_claim_is_refused_as_a_denial_not_bound_silently(self, mocker, bad_sub):
        """REGRESSION (see mcp-container/adversarial-review-2026-08.md § kdb-x backend).

        Nothing type-checked `sub`. A JSON-number `sub` ferried to q without complaint and only bit
        the first policy check that compared it against a symbol — a raw q `'type` error, which
        `is_denial` correctly does NOT treat as a denial, so the tool fell through to a bare
        `{"status": "error"}` with no `error_type`: an infrastructure-shaped error for what is
        actually a malformed token. A dict/list `sub` failed even earlier and more obscurely, as
        `TypeError: unhashable type` from the connection cache key.

        Refused Python-side rather than in q on purpose: the `kx.auth` q module is canonical in the
        `kx-auth` repo, not this one, so hardening `promote` belongs there (recorded as an ask).
        """
        cfg = KDBConfig(assert_identity=True, username="svc", password="pw")
        sync = mocker.patch("kx_mcp_kdbx.utils.kdbx.kdb_sync_connection")

        class _BadSubPrincipal(_FakePrincipal):
            # `subject = None` mirrors production: fastmcp's JWTVerifier leaves AccessToken.subject
            # unset, so the `sub` CLAIM is what the ferry actually reads. Leaving the fixture's
            # string `subject` in place would shadow the malformed claim and the test would pass
            # while proving nothing.
            subject = None
            claims = {"sub": bad_sub, "iss": "https://issuer.test"}

        mocker.patch("kx_mcp_kdbx.utils.kdbx._current_principal", return_value=_BadSubPrincipal())

        with pytest.raises(MalformedPrincipal) as exc:
            get_kdb_connection(cfg)
        assert is_denial(exc.value)
        assert "sub" in str(exc.value)
        # Rejected BEFORE the cache key is built, so no connection is opened or cached for it.
        sync.assert_not_called()


class TestKDBConfigM4:
    """Identity-assertion config additions on the frozen KDBConfig."""

    def test_assert_identity_defaults_false(self):
        assert KDBConfig(_env_file=None).assert_identity is False

    def test_password_file_overrides_password(self, tmp_path):
        secret = tmp_path / "kdbx_pw"
        secret.write_text("file-secret\n")
        cfg = KDBConfig(_env_file=None, password_file=str(secret))
        assert cfg.password.get_secret_value() == "file-secret"

    def test_missing_password_file_is_ignored(self, tmp_path):
        cfg = KDBConfig(_env_file=None, password_file=str(tmp_path / "nope"), password=SecretStr("env-pw"))
        assert cfg.password.get_secret_value() == "env-pw"


class TestDeadHandleClassification:
    """Which probe errors mean "reopen the handle" and which mean "q said no".

    The real-host tests in `tests/integration/test_kdbx_connection_lifecycle.py` cover a peer that
    went away over plain TCP. The TLS path's QError texts and a failed socket write are not cheap to
    provoke against a real host, so the classification itself is pinned here.
    """

    @pytest.mark.parametrize("exc", [
        RuntimeError("Attempted to use a closed IPC connection"),
        RuntimeError("KDB-X Python attempted to process a message containing less than the "
                     "expected number of bytes, connection closed."),
        RuntimeError("Failed to send query on IPC socket: '[Errno 32] Broken pipe'"),
        ConnectionResetError(54, "Connection reset by peer"),
        QError("snd handle 5"),  # the TLS path, where q owns the handle
        QError("write to handle 5. OS reports: Broken pipe"),
    ])
    def test_dead_handle(self, exc):
        assert _is_dead_handle(exc, conn=None)

    @pytest.mark.parametrize("exc", [
        QError("denied"),
        QError("type"),
        QError("Query timed out"),  # slow, not dead: reopening would resend a slow query
        QError("hop. OS reports: Connection refused"),  # a failed *open*, not a dead handle
        ValueError("cannot send"),
    ])
    def test_live_handle(self, exc):
        assert not _is_dead_handle(exc, conn=None)


class TestRetryConfig:
    """`KDBX_DB_RETRY` counts retries, so it cannot be negative."""

    def test_negative_retry_is_rejected(self):
        with pytest.raises(ValidationError):
            KDBConfig(_env_file=None, retry=-1)


class TestCleanupKdbConnection:
    """Test cases for cleanup_kdb_connection function."""

    def test_cleanup_kdb_connection(self, mocker, caplog):
        """Test KDB connection cache cleanup."""
        # Arrange
        caplog.set_level(logging.INFO)
        mock_cache_clear = mocker.patch("kx_mcp_kdbx.utils.kdbx.kdb_sync_connection.cache_clear")

        # Act
        cleanup_kdb_connection()

        # Assert
        mock_cache_clear.assert_called_once()
        assert "KDBX connection cache cleared" in caplog.text