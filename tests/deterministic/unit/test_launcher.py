"""Proves the thin zero-code launcher assembles the same server from config."""

import asyncio
import importlib
import threading
import time

import pytest
from fastmcp import FastMCP

from kx_mcp_core import launcher
from kx_mcp_core.auth import begin_authz_dispatch, end_authz_dispatch
from kx_mcp_core.launcher import _env_flag, _parse_bundles, build_app, main
from kx_mcp_example import build_server as example

# The package attribute `kx_mcp_core.auth.authorize` is the *function* (re-exported), shadowing the
# submodule (see test_authorize.py) — so fetch the real module object to reset its cached settings.
authorize_mod = importlib.import_module("kx_mcp_core.auth.authorize")


@pytest.fixture(autouse=True)
def _reset_authz():
    """Reset the cached authz settings between tests so env/state never leaks across cases.

    The decision slot is opened/closed through the dispatch API rather than by poking a contextvar:
    the raw `current_authz_decision` var is gone, because a `.set()` inside a worker thread never
    reached the dispatching task (see `AuthzSlot`).
    """
    authorize_mod._SETTINGS = None
    token = begin_authz_dispatch()
    yield
    end_authz_dispatch(token)
    authorize_mod._SETTINGS = None


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


def test_observability_resolves_from_env_and_attaches_middleware(monkeypatch):
    """The launcher resolves KX_MCP_METRICS/_TRACING at startup, like KX_MCP_AUTH(Z) already are."""
    monkeypatch.setenv("KX_MCP_METRICS", "prometheus")

    app = build_app(["example"], name="t")

    assert "MetricsMiddleware" in {type(m).__name__ for m in app.middleware}


def test_observability_off_by_default_in_build_app(monkeypatch):
    monkeypatch.delenv("KX_MCP_METRICS", raising=False)
    monkeypatch.delenv("KX_MCP_TRACING", raising=False)

    app = build_app(["example"], name="t")

    kinds = {type(m).__name__ for m in app.middleware}
    assert "MetricsMiddleware" not in kinds and "TracingMiddleware" not in kinds


def test_main_mounts_metrics_route_under_http_transport(monkeypatch):
    """main() wires the scrape route once the transport is known (HTTP -> mounted)."""
    monkeypatch.setenv("KX_MCP_BUNDLES", "example")
    monkeypatch.setenv("KX_MCP_METRICS", "prometheus")
    monkeypatch.setattr(FastMCP, "run", lambda self, **kwargs: None)

    captured = {}
    real_mount = launcher.mount_metrics_route

    def spy(app, settings, transport):
        captured["transport"] = transport
        captured["mounted"] = real_mount(app, settings, transport)
        captured["paths"] = [getattr(r, "path", None) for r in app._additional_http_routes]

    monkeypatch.setattr(launcher, "mount_metrics_route", spy)

    main(["--transport", "streamable-http"])

    assert captured["transport"] == "streamable-http"
    assert captured["mounted"] is True
    assert "/metrics" in captured["paths"]


def test_main_skips_metrics_route_under_stdio(monkeypatch):
    """The stdio guard runs on the real launcher path: no route, no crash."""
    monkeypatch.setenv("KX_MCP_BUNDLES", "example")
    monkeypatch.setenv("KX_MCP_METRICS", "prometheus")
    monkeypatch.setattr(FastMCP, "run", lambda self, **kwargs: None)

    captured = {}
    real_mount = launcher.mount_metrics_route

    def spy(app, settings, transport):
        captured["mounted"] = real_mount(app, settings, transport)
        captured["paths"] = [getattr(r, "path", None) for r in app._additional_http_routes]

    monkeypatch.setattr(launcher, "mount_metrics_route", spy)

    main(["--transport", "stdio"])  # must not raise

    assert captured["mounted"] is False
    # Not an empty list: the parent always carries the container's own /health route.
    assert "/metrics" not in captured["paths"]


def test_main_rejects_a_bad_observability_mode(monkeypatch):
    """A typo'd selector fails loudly at startup rather than silently disabling telemetry."""
    monkeypatch.setenv("KX_MCP_BUNDLES", "example")
    monkeypatch.setenv("KX_MCP_METRICS", "prometheous")
    monkeypatch.setattr(FastMCP, "run", lambda self, **kwargs: None)

    with pytest.raises(Exception) as exc:
        main(["--transport", "stdio"])
    assert "KX_MCP_METRICS" in str(exc.value)


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


def test_mount_timeout_bounds_startup_and_keeps_serving_the_healthy_bundles(monkeypatch):
    """REGRESSION (see mcp-container/adversarial-review-2026-08.md § container/assembly seam).

    Nothing in the chain bounded bundle startup, so a `build_server()` that hangs wedged the
    container's startup FOREVER and every bundle requested after it never mounted — including ones
    that would have come up instantly.

    The timeout is **opt-in** (`KX_MCP_MOUNT_TIMEOUT`), which is the deliberate design decision, not
    a half-measure: a synchronous call cannot be cancelled in Python, so the only way to stop waiting
    is to run it on a worker thread and *abandon* that thread, which then holds whatever it allocated
    (for kdb-x, an embedded-PyKX/qIPC handle) for the life of the process. Putting every backend's
    pre-flight on a worker thread by default, to fix a case most deployments never hit, is the wrong
    trade — so the container's own obligation is the matching extension-contract amendment
    (`build_server()` must bound its own pre-flight, where a hang can be cancelled properly), and
    this is the operator's backstop when a bundle does not honour it.

    Asserts what the old version of this test did not: that `healthy2` — requested *after* the hung
    bundle — actually mounts. A "fix" that merely aborted all of build_app would have passed that.
    """
    import threading

    release = threading.Event()

    def _hangs(*_a, **_k):
        release.wait(30)  # bounded so the abandoned thread cannot outlive the test session
        raise AssertionError("unreachable: released only during teardown")

    _stub_bundles(monkeypatch, healthy1=example, hang=_hangs, healthy2=example)

    result = {}

    def _run():
        result["app"] = build_app(
            ["healthy1", "hang", "healthy2"], name="t", mount_timeout=0.5
        )

    runner = threading.Thread(target=_run, daemon=True)
    runner.start()
    runner.join(timeout=15)

    try:
        assert not runner.is_alive(), "build_app must not block forever on one hung bundle"
        names = {t.name for t in asyncio.run(result["app"].list_tools())}
        assert "healthy1_echo" in names
        assert "healthy2_echo" in names, "a bundle requested AFTER the hung one must still mount"
    finally:
        release.set()  # let the abandoned worker unwind instead of lingering for 30s


def test_mount_timeout_is_off_by_default(monkeypatch):
    """No timeout unless asked for: the default path must not put build_server() on a worker thread.

    Pinned because that is the whole reason the timeout is opt-in — PyKX's embedded q and a qIPC
    handle opened on a thread that is then abandoned are exactly what the default must avoid.
    """
    monkeypatch.delenv("KX_MCP_MOUNT_TIMEOUT", raising=False)
    calling_threads = []

    def _records_thread(*_a, **_k):
        calling_threads.append(threading.current_thread())
        return example()

    _stub_bundles(monkeypatch, ex=_records_thread)
    build_app(["ex"], name="t")

    assert calling_threads == [threading.main_thread()]


def test_a_timed_out_bundle_never_mounts_late_once_its_hang_clears(monkeypatch):
    """A bundle we have already given up on must not appear later, on the abandoned thread.

    The abandoned thread used to run the MOUNT as well as the build, so once its hang cleared it
    quietly added the backend the operator had just been told was unavailable — `list_tools()` grew
    a namespace after startup, and the late `mount_bundle` raced both the parent's namespace
    bookkeeping and build_app's mounted-count guard. Confirmed live before the fix (a 0.3s timeout
    logged "serving without it", then served all four `slow_*` tools ~1s later).

    The worker now resolves and *builds* only; `mount` happens on the calling thread. That leaves
    exactly the one cost `_mount_forgiving` documents — the thread runs on holding what it allocated
    — and no surprise mount.
    """
    release = threading.Event()

    def _hangs_then_succeeds():
        release.wait(30)  # bounded so the abandoned thread cannot outlive the test session
        return example()

    _stub_bundles(monkeypatch, slow=_hangs_then_succeeds, ok=example)

    app = build_app(["slow", "ok"], name="t", mount_timeout=0.3)
    before = {t.name for t in asyncio.run(app.list_tools())}
    assert "ok_echo" in before
    assert not any(n.startswith("slow_") for n in before)

    release.set()  # the hung pre-flight finally completes, on the abandoned thread
    for _ in range(50):  # give it every chance to mount late
        if not any(t.name.startswith("slow_") for t in asyncio.run(app.list_tools())):
            time.sleep(0.02)
            continue
        break

    after = {t.name for t in asyncio.run(app.list_tools())}
    assert after == before, f"a timed-out bundle mounted late: {sorted(after - before)}"


def test_bad_bundle_name_does_not_crash_healthy_bundles_alongside_it():
    """REGRESSION (see mcp-container/adversarial-review-2026-08.md § container/assembly seam).

    load_build_server's importlib.import_module call sits OUTSIDE try_mount_bundle's
    try/except in build_app's loop — a malformed bundle name (e.g. a stray comma producing an
    empty short name) must not crash the whole launcher; the healthy bundle requested alongside
    it must still mount."""
    app = build_app(["example", ""], name="t")
    names = {t.name for t in asyncio.run(app.list_tools())}
    assert "example_echo" in names


def test_bundle_resolution_failure_does_not_crash_healthy_bundles_mounted_alongside_it(monkeypatch):
    """REGRESSION (see mcp-container/adversarial-review-2026-08.md § container/assembly seam).

    load_build_server's resolution step (an arbitrary import-time failure — e.g. a bundle's own
    module-level config construction raising, matching kdb-x's `app_settings = AppSettings()`
    pattern) sits OUTSIDE try_mount_bundle's try/except in build_app's loop. ANY resolution
    failure, for any reason, currently crashes the whole launcher uncaught — even when a healthy
    bundle was requested alongside it."""

    def _bad_resolution(package):
        raise RuntimeError("bundle's own module-level config construction failed")

    monkeypatch.setattr(
        launcher, "load_build_server",
        lambda package: _bad_resolution(package) if package == "kx_mcp_broken" else example,
    )

    app = build_app(["example", "broken"], name="t")
    names = {t.name for t in asyncio.run(app.list_tools())}
    assert "example_echo" in names


@pytest.mark.parametrize("bad_port", ["999999", "0", "-1", "65536", "notanumber"])
def test_out_of_range_port_fails_at_the_cli_not_deep_in_uvicorn(monkeypatch, bad_port):
    """REGRESSION (see mcp-container/adversarial-review-2026-08.md § container/assembly seam).

    `--port` had only argparse's `type=int`, so `999999` was accepted and failed far later from
    inside uvicorn/starlette's socket bind — an `OverflowError` that never mentions `--port`. It is
    now a usage error: argparse names the flag, the value and the valid range, and exits 2.

    Asserted as SystemExit(2) alone, not `(SystemExit, OverflowError)`: the original test allowed the
    OverflowError only so the red version could reach an informative assertion, and leaving it would
    let a future regression back into uvicorn unnoticed. `FastMCP.run` is patched so a test that
    somehow gets past validation cannot start serving (the sibling authz test already did this).
    """
    monkeypatch.setenv("KX_MCP_BUNDLES", "example")
    monkeypatch.setattr(FastMCP, "run", lambda self, **kwargs: None)

    with pytest.raises(SystemExit) as exc:
        main(["--port", bad_port])
    assert exc.value.code == 2


def test_a_non_numeric_port_env_var_is_a_usage_error_not_a_traceback(monkeypatch):
    """`int(os.environ.get("KX_MCP_PORT", ...))` used to run at parser-CONSTRUCTION time, so a
    non-numeric value was an uncaught ValueError traceback out of main() — and it fired even for
    `--help` or `--transport stdio`, where the port is never used at all."""
    monkeypatch.setenv("KX_MCP_BUNDLES", "example")
    monkeypatch.setenv("KX_MCP_PORT", "not-a-port")
    monkeypatch.setattr(FastMCP, "run", lambda self, **kwargs: None)

    with pytest.raises(SystemExit) as exc:
        main([])
    assert exc.value.code == 2


def test_a_stray_comma_in_the_bundle_list_is_ignored(monkeypatch):
    """The bad-bundle-name case through the REAL entry point.

    `_parse_bundles` did not drop empty entries, so `--bundles example,` yielded `["example", ""]`
    and the empty name became `load_build_server("kx_mcp_")` -> ModuleNotFoundError, crashing the
    whole launcher and taking the healthy `example` down with it. `","` alone was worse: `["", ""]`
    is truthy, so it passed the "no bundles selected" guard entirely.

    Exercised via `main()`/`_parse_bundles` rather than `build_app([..., ""])`, because the stray
    comma is a CLI/env-level accident and that is the boundary where it has to be handled.
    """
    assert _parse_bundles("example,") == ["example"]
    assert _parse_bundles("a,,b") == ["a", "b"]
    assert _parse_bundles(",") == []
    assert _parse_bundles("  ,  ") == []

    captured = {}
    monkeypatch.setenv("KX_MCP_BUNDLES", "example,")
    monkeypatch.setattr(FastMCP, "run", lambda self, **kwargs: None)
    def _spy(bundles, **_kwargs):
        captured["bundles"] = list(bundles)
        return FastMCP("t")

    monkeypatch.setattr(launcher, "build_app", _spy)

    main(["--transport", "stdio"])
    assert captured["bundles"] == ["example"]


def test_a_comma_only_bundle_list_is_a_usage_error(monkeypatch):
    """`","` parsed to `["", ""]` — truthy, so it slipped past the guard that exists to catch
    exactly this."""
    monkeypatch.setenv("KX_MCP_BUNDLES", ",")
    with pytest.raises(SystemExit) as exc:
        main(["--transport", "stdio"])
    assert exc.value.code == 2


def test_unregistered_authz_strategy_fails_at_startup_not_first_request(monkeypatch):
    """REGRESSION (see mcp-container/adversarial-review-2026-08.md § container/assembly seam).

    configure_authz() only validates a policy FILE for mode="static" — a named strategy that
    no bundle has registered an adapter for (e.g. a --bundles typo, or that bundle failing to
    mount) is accepted silently at startup and only surfaces as a confusing per-request
    ValueError later. Must fail loudly at startup instead."""
    monkeypatch.setenv("KX_MCP_AUTHZ", "never_registered_strategy")
    monkeypatch.setenv("KX_MCP_BUNDLES", "example")
    monkeypatch.setattr(FastMCP, "run", lambda self, **kwargs: None)

    with pytest.raises((ValueError, SystemExit)):
        main([])


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

    def _spy(bundles, name="kx-mcp", exit_on_mount_failure=False, **kwargs):
        captured["strict"] = exit_on_mount_failure
        captured["kwargs"] = kwargs
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
