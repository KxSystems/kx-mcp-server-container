"""`kx auth introspect` end to end through the argparse entry point.

The required regression: a valid token exits 0, a bad/expired/forbidden token exits non-0 (the
specific codes), and `--json` emits the structured envelope an agent branches on. Invoked in-process
via `cli.main`, asserting the `SystemExit` code; the pub key is written to `tmp_path` (mirrors
`tests/test_auth_integration.py`).
"""

from __future__ import annotations

import json

import pytest

from kx_auth_cli import cli

# Must match the values the shared `mint` fixture encodes (repo-root conftest.py).
ISSUER = "https://issuer.test"
AUDIENCE = "kx-mcp"


def _run(argv: list[str]) -> int:
    with pytest.raises(SystemExit) as exc:
        cli.main(argv)
    code = exc.value.code
    return code if isinstance(code, int) else 1


@pytest.fixture
def pub_path(keypair, tmp_path):
    _, pub = keypair
    p = tmp_path / "public.pem"
    p.write_text(pub)
    return str(p)


def _args(token: str, pub_path: str, *extra: str) -> list[str]:
    return [
        "auth", "introspect", token,
        "--public-key-path", pub_path,
        "--issuer", ISSUER, "--audience", AUDIENCE,
        *extra,
    ]


def test_valid_token_exits_0(mint, pub_path):
    assert _run(_args(mint(), pub_path)) == 0


def test_expired_token_exits_3(mint, pub_path):
    assert _run(_args(mint(exp_delta=-10), pub_path)) == 3


def test_wrong_audience_exits_4(mint, pub_path):
    assert _run(_args(mint(aud="other"), pub_path)) == 4


def test_bad_signature_exits_1(mint, pub_path, other_priv):
    assert _run(_args(mint(priv=other_priv), pub_path)) == 1


def test_no_token_is_usage_error_2(pub_path, monkeypatch):
    monkeypatch.delenv("KX_AUTH_TOKEN", raising=False)
    assert _run(["auth", "introspect", "--public-key-path", pub_path]) == 2


def test_token_from_env(mint, pub_path, monkeypatch):
    monkeypatch.setenv("KX_AUTH_TOKEN", mint())
    assert _run(["auth", "introspect", "--public-key-path", pub_path,
                 "--issuer", ISSUER, "--audience", AUDIENCE]) == 0


def test_json_envelope_on_valid(mint, pub_path, capsys):
    assert _run(_args(mint(), pub_path, "--json")) == 0
    envelope = json.loads(capsys.readouterr().out)
    assert envelope["status"] == "ok" and envelope["valid"] is True
    assert envelope["client_id"] == "alice"
    assert envelope["claims"]["iss"] == ISSUER


def test_json_envelope_on_failure_has_reason(mint, pub_path, capsys):
    assert _run(_args(mint(exp_delta=-10), pub_path, "--json")) == 3
    envelope = json.loads(capsys.readouterr().out)
    assert envelope["status"] == "auth_required" and envelope["valid"] is False
    assert "reason" in envelope


def test_no_command_shows_help_exits_2():
    assert _run(["auth"]) == 2
