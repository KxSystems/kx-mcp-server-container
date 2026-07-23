"""`kx auth exchange` end to end through the argparse entry point.

The required regression: `exchange` returns an audience-scoped token from a mock STS (exit 0 +
the `--json` envelope). Plus the strategy/exit-code matrix and the `login` → `exchange` cache chain.
The `mock_sts` fixture is shared from the repo-root `conftest.py`.
"""

from __future__ import annotations

import json
import time

import jwt
import pytest

from kx_auth_cli import cache, cli

_HS256_KEY = "test-secret-key-at-least-32-bytes-long!"


def _jwt(**claims) -> str:
    return jwt.encode(claims, _HS256_KEY, algorithm="HS256")


def _run(argv: list[str]) -> int:
    with pytest.raises(SystemExit) as exc:
        cli.main(argv)
    code = exc.value.code
    return code if isinstance(code, int) else 1


@pytest.fixture(autouse=True)
def _no_ambient_token(monkeypatch, tmp_path):
    """Keep the environment from leaking a subject token or a real cache into these tests."""
    monkeypatch.delenv("KX_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("KX_AUTH_CLIENT_SECRET", raising=False)
    monkeypatch.setenv("KX_AUTH_CACHE", str(tmp_path / "creds.json"))


# --- the required regression: rfc_8693 → audience-scoped token ---------------------------------


def test_rfc_8693_returns_audience_scoped_token(mock_sts, capsys):
    token_url, captured = mock_sts
    subject = _jwt(sub="alice", aud="mcp")
    code = _run([
        "auth", "exchange", "--subject", subject, "--audience", "kdbai",
        "--token-url", token_url, "--client-id", "mcp-container", "--client-secret", "s3cret",
        "--scope", "kdbx.read", "--json",
    ])
    assert code == 0
    envelope = json.loads(capsys.readouterr().out)
    assert envelope["status"] == "ok"
    assert envelope["strategy"] == "rfc_8693"
    assert envelope["claims"]["aud"] == "kdbai"  # the minted token is scoped to the backend
    assert captured[0]["subject_token"] == subject
    assert captured[0]["audience"] == "kdbai"


# --- the strategy / exit-code matrix ----------------------------------------------------------


def test_passthrough_audience_mismatch_is_denied_4(capsys):
    subject = _jwt(sub="alice", aud="mcp")
    code = _run([
        "auth", "exchange", "--strategy", "passthrough", "--subject", subject,
        "--audience", "kdbai", "--json",
    ])
    assert code == 4
    assert json.loads(capsys.readouterr().out)["status"] == "denied"


def test_rfc_8693_missing_token_url_is_error_1(capsys):
    subject = _jwt(sub="alice", aud="mcp")
    code = _run(["auth", "exchange", "--subject", subject, "--audience", "kdbai", "--json"])
    assert code == 1
    assert json.loads(capsys.readouterr().out)["status"] == "error"


def test_service_account_needs_no_subject_0(mock_sts):
    token_url, captured = mock_sts
    code = _run([
        "auth", "exchange", "--strategy", "service_account", "--audience", "backend-api",
        "--token-url", token_url, "--client-id", "mcp-container", "--client-secret", "s3cret",
    ])
    assert code == 0
    assert "subject_token" not in captured[0]


def test_missing_subject_for_rfc_8693_is_usage_2():
    # no --subject, no $KX_AUTH_TOKEN, stdin empty under pytest → usage error before any network call
    assert _run(["auth", "exchange", "--audience", "kdbai", "--token-url", "http://x/token"]) == 2


# --- the login -> exchange cache chain --------------------------------------------------------


def test_subject_falls_back_to_login_cache(mock_sts, capsys):
    token_url, captured = mock_sts
    server = "https://mcp.example"
    cached_token = _jwt(sub="alice", aud="mcp")
    cache.save(server, {"access_token": cached_token, "token_type": "Bearer"})

    code = _run([
        "auth", "exchange", "--server", server, "--audience", "kdbai",
        "--token-url", token_url, "--json",
    ])
    assert code == 0
    # the cached login token was used as the exchange subject
    assert captured[0]["subject_token"] == cached_token


def test_expired_cache_subject_is_auth_required_3(mock_sts, capsys):
    token_url, captured = mock_sts
    server = "https://mcp.example"
    cache.save(server, {
        "access_token": _jwt(sub="alice", aud="mcp"),
        "expires_at": int(time.time()) - 60,
    })

    code = _run([
        "auth", "exchange", "--server", server, "--audience", "kdbai",
        "--token-url", token_url, "--json",
    ])
    assert code == 3  # auth-required: re-login, not a usage error
    assert json.loads(capsys.readouterr().out)["status"] == "auth_required"
    assert not captured  # the expired token was never sent to the STS


# --- client-secret env fallback ---------------------------------------------------------------


def test_client_secret_falls_back_to_env(mock_sts, monkeypatch):
    token_url, captured = mock_sts
    monkeypatch.setenv("KX_AUTH_CLIENT_SECRET", "env-s3cret")
    code = _run([
        "auth", "exchange", "--strategy", "service_account", "--audience", "backend-api",
        "--token-url", token_url, "--client-id", "mcp-container",
    ])
    assert code == 0
    assert captured[0]["client_secret"] == "env-s3cret"
