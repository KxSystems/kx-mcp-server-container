"""Process- and runtime-level metrics: the in-flight gauge, event-loop lag, threads, build info.

The registry is process-global and outlives every test, so assertions here are **deltas** around a
before/after read, never absolute values — the same rule the rest of the metrics tests follow.
"""

import asyncio
import time

import pytest
from fastmcp import Client, FastMCP
from prometheus_client import REGISTRY

from kx_mcp_core import make_parent, mount_bundle
from kx_mcp_core.observability import ObservabilitySettings
from kx_mcp_core.observability import runtime as runtime_mod
from kx_mcp_core.observability import tracing as tracing_mod


@pytest.fixture(autouse=True)
def _reset_runtime():
    """Cancel the sampler between tests so one test's task never samples another's loop."""
    runtime_mod.reset_runtime_metrics_for_tests()
    yield
    runtime_mod.reset_runtime_metrics_for_tests()


@pytest.fixture(autouse=True)
def _tracing_already_initialised(monkeypatch):
    """Skip real exporter setup; mirrors the fixture in test_observability.py."""
    monkeypatch.setattr(tracing_mod, "_initialised", True)


def _sample(name, labels=None):
    return REGISTRY.get_sample_value(name, labels or {}) or 0.0


def _metrics_parent(backend):
    parent = make_parent(
        "runtime-metrics-test", observability=ObservabilitySettings(metrics="prometheus")
    )
    mount_bundle(parent, lambda: backend, namespace="rt")
    return parent


class TestInFlightGauge:
    """`kx_mcp_dispatches_in_progress` — the concurrency signal count+duration cannot express."""

    def test_a_tool_sees_itself_in_flight(self):
        """Read the gauge from inside the dispatch it is counting.

        Simpler and less racy than driving a second concurrent request: if the gauge is 1 while the
        body runs, the increment happened before `call_next` and the label matches the target.
        """
        backend = FastMCP("rt-backend")

        @backend.tool
        async def peek() -> float:
            return _sample("kx_mcp_dispatches_in_progress", {"tool_name": "rt_peek"})

        async def scenario():
            async with Client(_metrics_parent(backend)) as client:
                return (await client.call_tool("rt_peek", {})).data

        assert asyncio.run(scenario()) == 1.0

    def test_the_gauge_returns_to_baseline_after_a_successful_dispatch(self):
        backend = FastMCP("rt-backend")

        @backend.tool
        async def ok() -> str:
            return "fine"

        labels = {"tool_name": "rt_ok"}
        before = _sample("kx_mcp_dispatches_in_progress", labels)

        async def scenario():
            async with Client(_metrics_parent(backend)) as client:
                await client.call_tool("rt_ok", {})

        asyncio.run(scenario())

        assert _sample("kx_mcp_dispatches_in_progress", labels) == before

    def test_the_gauge_returns_to_baseline_after_a_raising_dispatch(self):
        """The decrement lives in a `finally`; without it the gauge drifts up forever.

        A gauge that only decrements on the happy path reads as a permanent pile-up after the first
        error, which is worse than having no gauge at all.
        """
        backend = FastMCP("rt-backend")

        @backend.tool
        async def boom() -> str:
            raise RuntimeError("boom")

        labels = {"tool_name": "rt_boom"}
        before = _sample("kx_mcp_dispatches_in_progress", labels)

        async def scenario():
            async with Client(_metrics_parent(backend)) as client:
                await client.call_tool("rt_boom", {}, raise_on_error=False)

        asyncio.run(scenario())

        assert _sample("kx_mcp_dispatches_in_progress", labels) == before


class TestEnsureMonitor:
    def test_no_loop_means_no_monitor(self):
        assert runtime_mod.ensure_monitor() is False

    def test_starts_on_a_running_loop(self):
        async def scenario():
            assert runtime_mod.ensure_monitor() is True
            return runtime_mod._task

        task = asyncio.run(scenario())
        assert task is not None

    def test_is_idempotent_on_the_same_loop(self):
        async def scenario():
            runtime_mod.ensure_monitor()
            first = runtime_mod._task
            runtime_mod.ensure_monitor()
            return first is runtime_mod._task

        assert asyncio.run(scenario()) is True

    def test_a_second_loop_gets_its_own_monitor(self):
        """Otherwise the sampler stays bound to a dead loop and silently stops reporting."""

        async def scenario():
            runtime_mod.ensure_monitor()
            return runtime_mod._task

        first = asyncio.run(scenario())
        second = asyncio.run(scenario())
        assert first is not second


class TestEventLoopLag:
    def test_blocking_the_loop_is_recorded_as_lag(self, monkeypatch):
        """The claim that makes this metric worth shipping.

        A synchronous `time.sleep` on the loop thread is exactly what a blocking qIPC round-trip
        does to this container, so if lag does not move here it would not move in production either.
        """
        monkeypatch.setattr(runtime_mod, "SAMPLE_INTERVAL_SECONDS", 0.02)
        before = _sample("kx_mcp_event_loop_lag_seconds_sum")

        async def scenario():
            runtime_mod.ensure_monitor()
            await asyncio.sleep(0.05)  # let a few clean ticks happen first
            time.sleep(0.3)  # block the loop, as a sync backend call would
            await asyncio.sleep(0.05)  # give the sampler a chance to notice

        asyncio.run(scenario())

        observed = _sample("kx_mcp_event_loop_lag_seconds_sum") - before
        assert observed >= 0.2, f"a 0.3s block produced only {observed:.3f}s of measured lag"

    def test_an_idle_loop_reports_near_zero_lag(self, monkeypatch):
        """Guards against a metric that always looks alarming and so gets ignored."""
        monkeypatch.setattr(runtime_mod, "SAMPLE_INTERVAL_SECONDS", 0.02)
        before = _sample("kx_mcp_event_loop_lag_seconds_sum")

        async def scenario():
            runtime_mod.ensure_monitor()
            await asyncio.sleep(0.2)

        asyncio.run(scenario())

        observed = _sample("kx_mcp_event_loop_lag_seconds_sum") - before
        assert observed < 0.1, f"an idle loop reported {observed:.3f}s of lag"

    def test_the_sampler_publishes_a_thread_count(self, monkeypatch):
        monkeypatch.setattr(runtime_mod, "SAMPLE_INTERVAL_SECONDS", 0.02)

        async def scenario():
            runtime_mod.ensure_monitor()
            await asyncio.sleep(0.06)

        asyncio.run(scenario())

        assert _sample("kx_mcp_threads") >= 1


class TestBuildInfo:
    def test_make_parent_publishes_a_version_when_metrics_are_on(self):
        make_parent("bi-test", observability=ObservabilitySettings(metrics="prometheus"))

        families = {m.name: m for m in REGISTRY.collect()}
        assert "kx_mcp_build" in families
        versions = {
            s.labels.get("version") for m in [families["kx_mcp_build"]] for s in m.samples
        }
        assert versions and all(v for v in versions), f"no version label: {versions}"
