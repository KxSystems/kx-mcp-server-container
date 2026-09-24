"""build_server() registration + pre-flight behaviour for the kdb.ai bundle."""

import asyncio

import pytest

from kx_mcp_kdbai.server import build_server, McpServer
from kx_mcp_kdbai.settings import AppSettings, KDBAIConfig


def _settings(**db):
    # Mount-only: the bundle owns no serving config; AppSettings is just the db connection.
    return AppSettings(db=KDBAIConfig(**db))


def test_build_server_registers_full_surface(mocker):
    """All 11 tools + 1 resource + 1 prompt register; the connection pre-flight is mocked."""
    mocker.patch("kdbai_client.Session", return_value=mocker.Mock())

    mcp = build_server(_settings())

    tools = {t.name for t in asyncio.run(mcp.list_tools())}
    resources = {str(r.uri) for r in asyncio.run(mcp.list_resources())}
    prompts = {p.name for p in asyncio.run(mcp.list_prompts())}

    # Bare names (the container adds the `kdbai` namespace at mount time).
    assert tools == {
        "list_databases", "database_info", "all_databases_info",
        "list_tables", "table_info",
        "query_data", "similarity_search", "hybrid_search",
        "session_info", "system_info", "process_info",
    }
    assert len(tools) == 11
    assert resources == {"file://guidance/kdbai-operations"}
    assert prompts == {"table_analysis"}


def test_build_server_attaches_instance_config(mocker):
    """The per-instance config is stashed on the server object for ctx-based routing."""
    mocker.patch("kdbai_client.Session", return_value=mocker.Mock())
    cfg = _settings(host="kdbai-eu", port=8083)
    mcp = build_server(cfg)
    assert mcp._kdbai_config.host == "kdbai-eu"
    assert mcp._kdbai_config.port == 8083


def test_preflight_exits_when_backend_unreachable(mocker):
    """A KDBAIException during the connectivity pre-flight is a clean sys.exit(1), not a crash."""
    import kdbai_client
    mocker.patch("kdbai_client.Session", side_effect=kdbai_client.KDBAIException("failed to open a session"))
    with pytest.raises(SystemExit) as exc:
        McpServer(_settings())
    assert exc.value.code == 1


def test_authentication_error_names_backend_environment_variables(mocker, caplog):
    """Authentication diagnostics name the bundle's public KDBAI_DB_* settings."""
    import kdbai_client
    mocker.patch(
        "kdbai_client.Session",
        side_effect=kdbai_client.KDBAIException("authentication error"),
    )

    with pytest.raises(SystemExit):
        McpServer(_settings())

    assert "KDBAI_DB_USERNAME and KDBAI_DB_PASSWORD" in caplog.text


@pytest.fixture(autouse=True)
def _inbound_auth_for_passthrough(monkeypatch):
    """`passthrough` is refused at startup without inbound auth, so give these tests a mode.

    Not a workaround: passthrough forwards the *validated* inbound principal's bearer, so with
    KX_MCP_AUTH unset there is never a principal and every call would silently open an anonymous
    session. Every shipped passthrough config (both live demos, the realidp lane) already sets this;
    only the tests did not, which is precisely why the gap went unnoticed.
    """
    monkeypatch.setenv("KX_MCP_AUTH", "jwks")


def test_passthrough_preflight_is_socket_probe_not_authed_open(mocker):
    """passthrough has no principal at startup, so the pre-flight must NOT open an (anonymous) Session
    against an OAuth-enforcing server — it does a tokenless socket reachability probe instead.

    Regression for a gap found live: against a real OAuth KDB.AI an anonymous open is *rejected*, so the
    old behaviour exited the container on startup. (Verified live 2026-06-18: alice's bearer forwarded
    through passthrough authenticated as alice; the server ACL then filtered her vs bob.)
    """
    probe = mocker.patch("socket.create_connection")
    session = mocker.patch("kdbai_client.Session", return_value=mocker.Mock())
    McpServer(_settings(outbound_strategy="passthrough"))
    probe.assert_called_once()        # reachability probe ran
    session.assert_not_called()       # and we did NOT try an anonymous authed open


def test_passthrough_without_inbound_auth_exits_at_startup(mocker, monkeypatch):
    """REGRESSION (see mcp-container/adversarial-review-2026-08.md § kdb.ai backend).

    The whole point of `passthrough` is that the *validated* inbound principal's bearer becomes the
    KDB.AI credential. With `KX_MCP_AUTH` unset there is never a validated principal and never a
    bearer, so every call would open an ANONYMOUS session — and if the KDB.AI server permits
    anonymous access (a real, supported posture) that silently defeats the configuration while
    looking identical in the logs to a legitimately anonymous deployment. Nothing checked it.

    A clean startup exit, per the graceful-degradation invariant: `try_mount_bundle` catches the
    SystemExit, disables just this backend with a warning, and the container keeps serving the rest.
    """
    monkeypatch.setenv("KX_MCP_AUTH", "unset")
    probe = mocker.patch("socket.create_connection")

    with pytest.raises(SystemExit) as exc:
        McpServer(_settings(outbound_strategy="passthrough"))

    assert exc.value.code == 1
    probe.assert_not_called()  # refused before we even probe reachability


def test_passthrough_preflight_exits_when_socket_unreachable(mocker):
    """A genuine connection failure (refused / timeout) under passthrough is still a clean exit(1)."""
    mocker.patch("socket.create_connection", side_effect=ConnectionRefusedError("refused"))
    with pytest.raises(SystemExit) as exc:
        McpServer(_settings(outbound_strategy="passthrough"))
    assert exc.value.code == 1
