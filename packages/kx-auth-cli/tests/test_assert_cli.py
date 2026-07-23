"""`kx auth assert` end to end through the argparse entry point.

Covers the project-only path (no kdb+, no PyKX), the claims-source matrix, the usage/error/denied
exit codes, and the --connect handshake with PyKX stubbed via sys.modules. Also asserts the base CLI
import graph stays fastmcp-free (the kx-auth-cli charter).
"""

from __future__ import annotations

import json
import sys
import types

import jwt
import pytest

from kx_auth_cli import cli

_HS256_KEY = "test-secret-key-at-least-32-bytes-long!"


def _jwt(**claims) -> str:
    return jwt.encode(claims, _HS256_KEY, algorithm="HS256")


def _run(argv: list[str]) -> int:
    with pytest.raises(SystemExit) as exc:
        cli.main(argv)
    code = exc.value.code
    return code if isinstance(code, int) else 1


@pytest.fixture(autouse=True)
def _no_ambient_token(monkeypatch):
    monkeypatch.delenv("KX_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("KX_AUTH_KDB_PASSWORD", raising=False)


# --- project-only (no --connect): the shared projection, no PyKX --------------------------------


def test_project_only_emits_wire_dict(capsys):
    code = _run([
        "auth", "assert",
        "--principal", json.dumps({"sub": "alice", "scope": "kdbx.read", "aud": "kx-mcp"}),
        "--json",
    ])
    assert code == 0
    env = json.loads(capsys.readouterr().out)
    assert env["status"] == "ok"
    assert env["principal"]["sub"] == "alice"
    assert env["principal"]["scopes"] == ["kdbx.read"]
    assert env["principal"]["aud"] == "kx-mcp"


def test_claims_from_token_decoded_unverified(capsys):
    token = _jwt(sub="bob", scope="kdbx.read", aud="kx-mcp")
    code = _run(["auth", "assert", "--token", token, "--json"])
    assert code == 0
    assert json.loads(capsys.readouterr().out)["principal"]["sub"] == "bob"


def test_no_principal_is_usage_error():
    assert _run(["auth", "assert", "--json"]) == 2


def test_bad_json_is_error(capsys):
    code = _run(["auth", "assert", "--principal", "{not json", "--json"])
    assert code == 1
    assert json.loads(capsys.readouterr().out)["status"] == "error"


# --- --connect handshake with a stubbed PyKX ----------------------------------------------------


class _FakeConn:
    """Records q calls; .kx.auth.valid[] -> True; an optional probe denial is configurable."""

    def __init__(self, *, probe_denied=False):
        self.calls = []
        self._probe_denied = probe_denied

    def __call__(self, expr, *args):
        self.calls.append((expr, args))
        if expr == ".kx.auth.valid[]":
            return types.SimpleNamespace(py=lambda: True)
        if self._probe_denied and expr.startswith("select"):
            raise RuntimeError("denied: principal lacks kdbx.read scope")
        return types.SimpleNamespace(py=lambda: None)


def _install_fake_pykx(monkeypatch, conn):
    fake = types.ModuleType("pykx")
    fake.SyncQConnection = lambda **kwargs: conn
    monkeypatch.setitem(sys.modules, "pykx", fake)


def test_connect_binds_and_confirms(monkeypatch, capsys):
    conn = _FakeConn()
    _install_fake_pykx(monkeypatch, conn)
    code = _run([
        "auth", "assert", "--principal", json.dumps({"sub": "alice", "scope": "kdbx.read"}),
        "--connect", "localhost:5010", "--user", "kxmcp", "--password", "pw", "--json",
    ])
    assert code == 0
    env = json.loads(capsys.readouterr().out)
    assert env["status"] == "ok" and env["bound"] is True and env["valid"] is True
    bind_calls = [c for c in conn.calls if c[0] == ".kx.auth.bind"]
    assert len(bind_calls) == 1 and bind_calls[0][1][0]["sub"] == "alice"


def test_probe_denial_maps_to_exit_4(monkeypatch, capsys):
    conn = _FakeConn(probe_denied=True)
    _install_fake_pykx(monkeypatch, conn)
    code = _run([
        "auth", "assert", "--principal", json.dumps({"sub": "bob"}),
        "--connect", "localhost:5010", "--probe", "select from trades", "--json",
    ])
    assert code == 4
    assert json.loads(capsys.readouterr().out)["status"] == "denied"


def test_connect_without_pykx_is_error(monkeypatch, capsys):
    # Simulate PyKX not installed: importing pykx raises.
    monkeypatch.setitem(sys.modules, "pykx", None)
    code = _run([
        "auth", "assert", "--principal", json.dumps({"sub": "alice"}),
        "--connect", "localhost:5010", "--json",
    ])
    assert code == 1
    assert "PyKX" in json.loads(capsys.readouterr().out)["reason"]


# --- charter: the base CLI import graph stays fastmcp-free --------------------------------------


def test_assert_cmd_import_is_fastmcp_free():
    """Importing the command must not pull fastmcp into the process (the CLI ships light)."""
    sys.modules.pop("fastmcp", None)
    import importlib

    import kx_auth_cli.assert_cmd  # noqa: F401

    importlib.reload(kx_auth_cli.assert_cmd)
    assert "fastmcp" not in sys.modules
