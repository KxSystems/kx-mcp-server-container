"""Proves the thin zero-code launcher assembles the same server from config."""

import asyncio

import pytest
from fastmcp import FastMCP

from kx_mcp_core.launcher import _parse_bundles, build_app, main


def test_build_app_mounts_named_bundles():
    app = build_app(["example"], name="t")
    names = {t.name for t in asyncio.run(app.list_tools())}
    assert "example_echo" in names


def test_settings_parse_under_kx_mcp_env(monkeypatch):
    """The container launcher binds its config off the KX_MCP_* prefix (bundles + transport).

    Asserts the container-prefix contract: KX_MCP_BUNDLES selects the bundle and KX_MCP_TRANSPORT
    drives run(). FastMCP.run is monkeypatched to capture kwargs so no socket/stdio is opened.
    (The kdb-x bundle is mount-only — it has no serving prefix; the container owns transport/host/
    port via KX_MCP_*. The backend's only prefix is KDBX_DB_*, an extension-owned namespace.)
    """
    monkeypatch.setenv("KX_MCP_BUNDLES", "example")
    monkeypatch.setenv("KX_MCP_TRANSPORT", "stdio")

    assert _parse_bundles("example") == ["example"]

    captured = {}
    monkeypatch.setattr(FastMCP, "run", lambda self, **kwargs: captured.update(kwargs))

    main([])  # no flags — resolves bundles + transport entirely from KX_MCP_* env

    assert captured["transport"] == "stdio"  # stdio path takes no host/port
    assert "host" not in captured and "port" not in captured


def test_parse_bundles():
    assert _parse_bundles("kdbx,example") == ["kdbx", "example"]
    assert _parse_bundles(" kdbx , example ") == ["kdbx", "example"]
    assert _parse_bundles(None) == []
    assert _parse_bundles("") == []


def test_main_errors_when_no_bundles_selected(monkeypatch):
    monkeypatch.delenv("KX_MCP_BUNDLES", raising=False)
    with pytest.raises(SystemExit):
        main(["--transport", "stdio"])


def test_sse_transport_is_rejected(monkeypatch):
    """The deprecated HTTP+SSE transport is not offered — argparse rejects it as an invalid choice.

    SSE was superseded by streamable-http in the MCP spec; the launcher only accepts
    stdio / streamable-http / http, so `--transport sse` fails fast rather than starting a
    deprecated server. (Matches the "SSE transport is not supported" statement in the README.)
    """
    monkeypatch.setenv("KX_MCP_BUNDLES", "example")
    with pytest.raises(SystemExit):
        main(["--transport", "sse"])
