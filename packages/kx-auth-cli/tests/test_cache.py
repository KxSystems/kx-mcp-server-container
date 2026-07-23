"""The client-side token cache: endpoint-keyed round-trip, path override, file mode."""

from __future__ import annotations

import json
import os
import stat

import pytest

from kx_auth_cli import cache


@pytest.fixture
def cache_file(tmp_path, monkeypatch):
    path = tmp_path / "creds.json"
    monkeypatch.setenv("KX_AUTH_CACHE", str(path))
    return path


def test_path_honours_env_override(cache_file):
    assert cache.cache_path() == cache_file


def test_load_missing_is_empty(cache_file):
    assert cache.load() == {}
    assert cache.get("https://mcp.example") is None


def test_save_then_get_round_trips(cache_file):
    cred = {"access_token": "tok-1", "token_type": "Bearer"}
    cache.save("https://mcp.example", cred)
    assert cache.get("https://mcp.example") == cred
    # persisted as JSON keyed by endpoint
    on_disk = json.loads(cache_file.read_text())
    assert on_disk["https://mcp.example"]["access_token"] == "tok-1"


def test_save_is_keyed_by_endpoint(cache_file):
    cache.save("https://a.example", {"access_token": "a"})
    cache.save("https://b.example", {"access_token": "b"})
    assert cache.get("https://a.example")["access_token"] == "a"
    assert cache.get("https://b.example")["access_token"] == "b"  # the first is not clobbered


def test_file_mode_is_0600(cache_file):
    cache.save("https://mcp.example", {"access_token": "x"})
    mode = stat.S_IMODE(os.stat(cache_file).st_mode)
    assert mode == 0o600


def test_corrupt_cache_reads_as_empty(cache_file):
    cache_file.write_text("not json{")
    assert cache.load() == {}
