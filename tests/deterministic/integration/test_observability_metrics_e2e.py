"""Observability metrics over a real wire, against a real kdb+ backend — not mocked, not in-process.

`unit/test_observability.py` and `unit/test_runtime_metrics.py` cover outcome classification, route
mounting, and the runtime sampler exhaustively, but entirely via in-process `make_parent` +
`httpx.ASGITransport`. `kx-mcp-kdbx/tests/unit/utils/test_observe.py` covers the kdbx-specific
collectors with a mocked connection. None of that proves the seam works the way an operator actually
runs it: a real launcher subprocess, a real HTTP wire, a real qIPC round-trip, scraped over a real
`GET /metrics`.

That gap already cost a shipped bug: BACKLOG.md (§ "Signal tool failures at the protocol level")
records that the `isError` tool-result contract was discovered by running this seam against a live
container — dispatches were counted `outcome="ok"` while the tool had actually failed. Mocked tests
agreed with the bug. This file brackets real dispatches with real scrapes so that class of drift is
caught by CI rather than by a human noticing during a manual pass.

**Self-skipping**: T2-T4 need a `q` binary and a kdb-x license, same convention as
``test_kdbx_sql_blocklist_bypass_e2e.py``. T1 needs neither — it uses the license-free `example`
bundle, so this file stays partially useful on a machine with no kdb-x install.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import socket
import subprocess
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from prometheus_client.parser import text_string_to_metric_families

from _pykx_env import stripped_from_os_environ

pytestmark = pytest.mark.integration

_HOST_Q = Path(__file__).with_name("kdbx_sql_blocklist_host.q")


# --- q host (module-scoped, shared by T2-T4) --------------------------------------------------


def _q_binary() -> str | None:
    """The ``q`` on PATH, else a co-located kdb-x install under ~/.kx (the repo's dev convention)."""
    found = shutil.which("q")
    if found:
        return found
    fallback = Path.home() / ".kx" / "bin" / "q"
    return str(fallback) if fallback.exists() else None


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def sql_host():
    """Spawn a real, plain `q` process loaded with kdbx_sql_blocklist_host.q for the whole module.

    Same fixture as ``test_kdbx_sql_blocklist_bypass_e2e.py::sql_host`` — reused verbatim rather
    than duplicated with a variant name, since that file's host script (`.s.init[]` + a live
    `trades` table, no auth) is exactly what a SQL-query observability test also needs.
    """
    q = _q_binary()
    if not q:
        pytest.skip("no `q` binary — the observability e2e tests need a kdb-x install")

    env = dict(os.environ)
    kx = Path.home() / ".kx"
    if kx.exists():
        env["QHOME"] = str(kx)
        env["QPATH"] = str(kx / "mod")

    port = _free_port()
    proc = subprocess.Popen(
        [q, str(_HOST_Q), "-p", str(port)],
        cwd=Path(__file__).resolve().parents[3],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )

    import pykx as kx_mod  # local import: only this module needs a real (licensed) pykx

    deadline = time.time() + 20.0
    ready = False
    last_exc: Exception | None = None
    out = ""
    while time.time() < deadline:
        if proc.poll() is not None:
            out = proc.stdout.read() if proc.stdout else ""
            break
        try:
            probe = kx_mod.SyncQConnection(host="127.0.0.1", port=port, timeout=2)
            try:
                trades_live = probe("`trades in tables[]").py()
            finally:
                probe.close()
            if trades_live:
                ready = True
                break
        except Exception as exc:  # noqa: BLE001 — q not accepting connections yet, keep polling
            last_exc = exc
            time.sleep(0.25)

    if not ready:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        if "license" in out.lower() and ("error" in out.lower() or "no license" in out.lower()):
            pytest.skip(f"no kdb-x license available — skipping observability e2e ({out.strip()[:120]})")
        if out:
            pytest.fail(f"sql_host exited early ({proc.returncode}):\n{out}")
        pytest.fail(f"sql_host never became ready on :{port}: {last_exc}")

    yield "127.0.0.1", port

    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()


@pytest.fixture(scope="module")
def metrics_container(sql_host, spawn_container_module):
    """One kdbx container, metrics on, shared by T2-T4 (each brackets its own delta-scrape).

    ``spawn_container_module``'s argv hardcodes transport/host/port, so metrics can only be
    turned on via env — see ``ObservabilitySettings``/``KX_MCP_METRICS`` in
    ``kx_mcp_core.observability``. ``stripped_from_os_environ()`` is load-bearing: `sql_host`
    already imported pykx in this pytest process, which contaminates QHOME/PYKX_* env vars that
    would otherwise leak into this child's own `import pykx` (see ``_pykx_env``'s docstring).
    """
    host, port = sql_host
    with stripped_from_os_environ():
        url, _ = spawn_container_module(
            "kdbx",
            KX_MCP_METRICS="prometheus",
            KX_MCP_AUTH="unset",
            KDBX_DB_HOST=host,
            KDBX_DB_PORT=str(port),
            KDBX_DB_TIMEOUT="10",  # the 1s default is too tight for a shared dev box
        )
    return url


# --- scrape + wire-call helpers ----------------------------------------------------------------


def _metrics_url(mcp_url: str) -> str:
    return f"{mcp_url.rsplit('/mcp', 1)[0]}/metrics"


def _health_url(mcp_url: str) -> str:
    return f"{mcp_url.rsplit('/mcp', 1)[0]}/health"


def _samples(text: str) -> dict[tuple[str, frozenset], float]:
    """Parse Prometheus exposition text into ``{(name, labels): value}`` via the reference parser.

    Unlike the unit tier's hand-rolled ``_families`` (label *names* only, no values), this test
    needs actual numbers — including `_count`/`_sum` suffixes and info-metric shape — from text
    produced by a subprocess. `prometheus_client.parser` is the format's own reference
    implementation, so it is trusted here rather than re-derived.
    """
    out: dict[tuple[str, frozenset], float] = {}
    for family in text_string_to_metric_families(text):
        for sample in family.samples:
            out[(sample.name, frozenset(sample.labels.items()))] = sample.value
    return out


def _scrape(mcp_url: str) -> dict[tuple[str, frozenset], float]:
    resp = httpx.get(_metrics_url(mcp_url), timeout=10)
    assert resp.status_code == 200, resp.text
    return _samples(resp.text)


def _value(samples: dict, name: str, **labels: str) -> float:
    """0.0 when the series has never been minted — an absent series IS a zero baseline."""
    return samples.get((name, frozenset(labels.items())), 0.0)


def _delta(before: dict, after: dict, name: str, **labels: str) -> float:
    return _value(after, name, **labels) - _value(before, name, **labels)


def _present(samples: dict, prefix: str) -> bool:
    return any(name.startswith(prefix) for name, _ in samples)


def _call(url: str, tool: str, args: dict) -> tuple[bool, Any]:
    """Call a tool over the wire; return (is_error, payload).

    ``raise_on_error=False`` + reading ``.structured_content`` on an error result are both
    required: a failed dispatch carries ``isError: true``, fastmcp's ``Client`` raises on that by
    default, and ``.data`` is not populated on an error-flagged result — see
    ``docs/extending.md`` § Signalling failure.
    """

    async def go():
        async with Client(StreamableHttpTransport(url)) as client:
            return await client.call_tool(tool, args, raise_on_error=False)

    outcome = asyncio.run(go())
    payload = outcome.structured_content if outcome.is_error else outcome.data
    if isinstance(payload, dict) and "license" in str(payload.get("message", "")).lower():
        pytest.skip(f"embedded-pykx license contention under the full suite — {payload['message']}")
    return outcome.is_error, payload


def _read(url: str, uri: str) -> str:
    async def go():
        async with Client(StreamableHttpTransport(url)) as client:
            result = await client.read_resource(uri)
            return result[0].text

    return asyncio.run(go())


# --- T1: license-free, proves the real launcher argv path mounts the route ---------------------


def test_metrics_route_absent_when_disabled(spawn_container):
    """Zero-config posture, over the real launcher — not an in-process ``mount_metrics_route`` call.

    Uses the license-free `example` bundle. Checks `/health` alongside `/metrics` so a 404 means
    "the route is missing", not "the process never came up".
    """
    url, _ = spawn_container()

    metrics = httpx.get(_metrics_url(url), timeout=10)
    health = httpx.get(_health_url(url), timeout=10)

    assert metrics.status_code == 404, metrics.text
    assert health.status_code == 200, health.text


# --- T2-T4: real kdbx + real q, sharing one metrics-on container --------------------------------


def test_metrics_route_serves_prometheus_when_enabled(metrics_container):
    resp = httpx.get(_metrics_url(metrics_container), timeout=10)

    assert resp.status_code == 200, resp.text
    assert "text/plain" in resp.headers["content-type"]
    assert "version=1.0.0" in resp.headers["content-type"]
    # Parses cleanly under the reference parser — a real shape check, not just a substring grep.
    families = list(text_string_to_metric_families(resp.text))
    assert families


def test_dispatch_outcomes_counted_over_the_wire(metrics_container):
    """REGRESSION for the isError contract (BACKLOG.md, "Signal tool failures at the protocol
    level"): a tool that fails must be counted `outcome="error"`, not `outcome="ok"`, over a real
    transport against a real backend — this is exactly the gap mocked tests could not see.
    """
    url = metrics_container
    before = _scrape(url)

    is_error, ok_payload = _call(url, "kdbx_run_sql_query", {"query": "SELECT * FROM trades"})
    assert is_error is False, ok_payload

    is_error, err_payload = _call(url, "kdbx_run_sql_query", {"query": "SELECT * FROM nosuchtable"})
    assert is_error is True, err_payload

    after = _scrape(url)

    assert _delta(before, after, "kx_mcp_dispatches_total",
                  tool_name="kdbx_run_sql_query", outcome="ok") == 1
    assert _delta(before, after, "kx_mcp_dispatches_total",
                  tool_name="kdbx_run_sql_query", outcome="error") == 1
    assert _delta(before, after, "kx_mcp_dispatch_duration_seconds_count",
                  tool_name="kdbx_run_sql_query") == 2
    assert _delta(before, after, "kdbx_qipc_calls_total", op="sql", outcome="ok") == 1

    # The in-flight gauge's only non-trivial half: it must return to 0 on the FAILURE path too,
    # not just the success path (a `finally`-vs-`except` decrement bug would leave this pinned).
    assert _value(after, "kx_mcp_dispatches_in_progress", tool_name="kdbx_run_sql_query") == 0

    # The runtime sampler (`ensure_monitor`) is only ever started from inside the dispatch
    # middleware, so "it actually starts in a real process" is only provable at this tier.
    assert _present(after, "kx_mcp_event_loop_lag_seconds")
    assert _present(after, "kx_mcp_threads")
    assert _present(after, "kx_mcp_build")


def test_resource_read_fans_out_to_multiple_qipc_calls(metrics_container):
    """Reading `tables://kdbx/all` is documented (docs/observability.md) to fan out into several
    qIPC round-trips per visible table, which the single dispatch counter flattens into one
    number. Asserted relationally (fan-out > dispatch count), not as an exact ratio, so it survives
    a table being added to the host `.q` or the preview logic changing.
    """
    url = metrics_container
    before = _scrape(url)

    body = _read(url, "tables://kdbx/all")
    assert body

    after = _scrape(url)

    dispatch_delta = _delta(before, after, "kx_mcp_dispatches_total",
                             tool_name="tables://kdbx/all", outcome="ok")
    assert dispatch_delta == 1

    fanout_delta = sum(
        _delta(before, after, "kdbx_qipc_calls_total", op=op, outcome="ok")
        for op in ("tables", "meta", "rowcounts", "partitioned", "preview")
    )
    assert fanout_delta > dispatch_delta, (
        f"expected the resource read's qIPC fan-out ({fanout_delta}) to exceed its single "
        f"dispatch count ({dispatch_delta})"
    )


def test_documented_runtime_series_are_live_on_the_first_scrape(spawn_container):
    """REGRESSION: `kx_mcp_threads` was set only by the sampler, which starts on the first
    dispatch, so a fresh container's first scrape read 0. The class of bug is any documented runtime
    series that depends on something starting later, so check every one of them, before any tool call.

    The list is `docs/observability.md` § Process and runtime metrics. `kx_mcp_dispatches_in_progress`
    (no series until a dispatch) and `kx_mcp_event_loop_lag_seconds` (ticks once a second, from the
    first dispatch) are legitimately empty here. The `process_*` family is Linux-only.
    """
    url, _ = spawn_container(KX_MCP_METRICS="prometheus")

    samples = _scrape(url)

    assert _value(samples, "kx_mcp_threads") >= 1
    build = [v for (name, labels), v in samples.items() if name == "kx_mcp_build_info"]
    assert build == [1.0]
    assert any(name == "kx_mcp_build_info" and dict(labels).get("version") for name, labels in samples)
    assert _present(samples, "python_info")
    if _present(samples, "process_"):  # ProcessCollector yields nothing off Linux
        for name in ("process_resident_memory_bytes", "process_open_fds", "process_start_time_seconds"):
            assert _value(samples, name) > 0, name
