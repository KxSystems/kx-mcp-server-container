"""Proves the thin zero-code launcher assembles the same server from config."""

import asyncio

import pytest
from fastmcp import FastMCP

from kx_mcp_core import launcher
from kx_mcp_core.launcher import _env_flag, _parse_bundles, build_app, main
from kx_mcp_example import build_server as example


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


# --- Mount-failure posture -------------------------------------------------------------------
# A bundle whose eager pre-flight fails sys.exit(1)s. By default the launcher disables only that
# backend ("never crash the container"); --exit-on-mount-failure makes the process die instead, and
# mounting *nothing* is always fatal because a bare parent cannot serve a single tool.


def _exits(*_args, **_kwargs):
    """Stand-in for a bundle whose connectivity pre-flight fails and calls sys.exit(1)."""
    raise SystemExit(1)


def _stub_bundles(monkeypatch, **by_name):
    """Resolve `--bundles <name>` to the given stand-ins instead of importing a real package."""
    monkeypatch.setattr(
        launcher,
        "load_build_server",
        lambda package: by_name[package.removeprefix("kx_mcp_")],
    )


def test_build_app_exits_when_strict_and_a_bundle_preflight_fails(monkeypatch):
    """--exit-on-mount-failure: the bundle's SystemExit propagates, exit code intact."""
    _stub_bundles(monkeypatch, ex=example, down=_exits)

    with pytest.raises(SystemExit) as exc:
        build_app(["ex", "down"], name="t", exit_on_mount_failure=True)
    assert exc.value.code == 1


def test_build_app_serves_healthy_backends_when_not_strict(monkeypatch):
    """Default posture is unchanged: a failed backend is dropped, the reachable one still serves."""
    _stub_bundles(monkeypatch, ex=example, down=_exits)

    app = build_app(["ex", "down"], name="t")

    names = {t.name for t in asyncio.run(app.list_tools())}
    assert "ex_echo" in names
    assert not any(n.startswith("down_") for n in names)


def test_build_app_exits_when_no_bundle_mounts(monkeypatch):
    """Reuben's case: kxi is the only bundle, its pre-flight fails — don't serve zero backends.

    Unconditional, no flag: this is the single-backend deployment where "never crash the container"
    would otherwise leave a live process with no tools, a passing liveness check, and no restart.
    """
    _stub_bundles(monkeypatch, kxi=_exits)

    with pytest.raises(SystemExit) as exc:
        build_app(["kxi"], name="t")
    assert exc.value.code == 1


def test_build_app_with_no_bundles_requested_is_not_a_mount_failure():
    """Zero *requested* bundles is a different condition — the parser rejects that, not build_app."""
    assert isinstance(build_app([], name="t"), FastMCP)


def test_exit_on_mount_failure_env_fallback(monkeypatch):
    """KX_MCP_EXIT_ON_MOUNT_FAILURE is the manifest-friendly form of the flag."""
    for truthy in ("1", "true", "TRUE", "yes", "on"):
        monkeypatch.setenv("KX_MCP_EXIT_ON_MOUNT_FAILURE", truthy)
        assert _env_flag("KX_MCP_EXIT_ON_MOUNT_FAILURE") is True
    for falsy in ("0", "false", "no", "off", ""):
        monkeypatch.setenv("KX_MCP_EXIT_ON_MOUNT_FAILURE", falsy)
        assert _env_flag("KX_MCP_EXIT_ON_MOUNT_FAILURE") is False
    monkeypatch.delenv("KX_MCP_EXIT_ON_MOUNT_FAILURE")
    assert _env_flag("KX_MCP_EXIT_ON_MOUNT_FAILURE") is False


def test_main_threads_the_flag_through_to_build_app(monkeypatch):
    """The CLI flag reaches build_app — the wiring that was missing before this change."""
    monkeypatch.setenv("KX_MCP_BUNDLES", "example")
    monkeypatch.delenv("KX_MCP_EXIT_ON_MOUNT_FAILURE", raising=False)
    monkeypatch.setattr(FastMCP, "run", lambda self, **kwargs: None)

    captured = {}

    def _spy(bundles, name="kx-mcp", auth_settings=None, exit_on_mount_failure=False):
        captured["strict"] = exit_on_mount_failure
        return FastMCP(name)

    monkeypatch.setattr(launcher, "build_app", _spy)

    main(["--transport", "stdio"])
    assert captured["strict"] is False

    main(["--transport", "stdio", "--exit-on-mount-failure"])
    assert captured["strict"] is True


def test_sse_transport_is_rejected(monkeypatch):
    """The deprecated HTTP+SSE transport is not offered — argparse rejects it as an invalid choice.

    SSE was superseded by streamable-http in the MCP spec; the launcher only accepts
    stdio / streamable-http / http, so `--transport sse` fails fast rather than starting a
    deprecated server. (Matches the "SSE transport is not supported" statement in the README.)
    """
    monkeypatch.setenv("KX_MCP_BUNDLES", "example")
    with pytest.raises(SystemExit):
        main(["--transport", "sse"])
