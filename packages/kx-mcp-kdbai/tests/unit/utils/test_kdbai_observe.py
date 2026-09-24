"""Tests for the KDB.AI backend's instrumentation seam."""

import asyncio

import pytest
from opentelemetry import trace as trace_api
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from prometheus_client import REGISTRY

from kx_mcp_core import span
from kx_mcp_kdbai.utils import observe
from kx_mcp_kdbai.utils.kdbai_auth import _run_coro


@pytest.fixture
def spans(monkeypatch):
    """Install a real global TracerProvider, which is what `span()` resolves through.

    `kx_mcp_core.span` calls `trace.get_tracer` against the **global** provider, so an injected
    tracer is not enough. OpenTelemetry guards `set_tracer_provider` with a one-shot `Once`, so the
    two module globals are monkeypatched (and thus restored) rather than set directly — otherwise
    the provider leaks into every later test in the session.
    """
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(trace_api, "_TRACER_PROVIDER_SET_ONCE", trace_api.Once())
    monkeypatch.setattr(trace_api, "_TRACER_PROVIDER", None)
    trace_api.set_tracer_provider(provider)
    yield exporter


def _sample(name, labels=None):
    return REGISTRY.get_sample_value(name, labels or {}) or 0.0


class TestCall:
    """`call()` — the kdbai_client SDK seam."""

    def test_returns_the_result_and_passes_arguments_through(self, mocker):
        fn = mocker.Mock(return_value="rows")

        assert observe.call("query", fn, 1, limit=10) == "rows"

        fn.assert_called_once_with(1, limit=10)

    def test_names_the_span_after_the_op(self, spans, mocker):
        observe.call("search", mocker.Mock(return_value=None))

        assert [s.name for s in spans.get_finished_spans()] == ["kdbai.search"]

    def test_counts_a_successful_call(self, mocker):
        labels = {"op": "list_databases", "outcome": "ok"}
        before = _sample("kdbai_sdk_calls_total", labels)

        observe.call("list_databases", mocker.Mock(return_value=[]))

        assert _sample("kdbai_sdk_calls_total", labels) - before == 1

    def test_a_failing_call_propagates_and_is_counted_as_an_error(self, mocker):
        labels = {"op": "query", "outcome": "error"}
        before = _sample("kdbai_sdk_calls_total", labels)

        with pytest.raises(RuntimeError, match="boom"):
            observe.call("query", mocker.Mock(side_effect=RuntimeError("boom")))

        assert _sample("kdbai_sdk_calls_total", labels) - before == 1

    def test_a_raising_collector_does_not_chain_onto_the_callers_error(self, mocker):
        mocker.patch.object(
            observe.SDK_CALLS, "labels", side_effect=ValueError("collector exploded")
        )

        with pytest.raises(RuntimeError, match="the real failure"):
            observe.call("query", mocker.Mock(side_effect=RuntimeError("the real failure")))


class TestSessionsGauge:
    def test_publishes_the_cache_size(self):
        observe.record_sessions(4)

        assert _sample("kdbai_cached_sessions") == 4

    def test_a_cleared_cache_reads_zero(self):
        observe.record_sessions(3)
        observe.record_sessions(0)

        assert _sample("kdbai_cached_sessions") == 0


class TestTokenMint:
    def test_traces_and_counts_a_successful_mint(self, spans):
        before = _sample("kdbai_token_mints_total", {"outcome": "ok"})

        with observe.token_mint():
            pass

        assert [s.name for s in spans.get_finished_spans()] == ["kdbai.token_mint"]
        assert _sample("kdbai_token_mints_total", {"outcome": "ok"}) - before == 1

    def test_a_failed_mint_is_counted_as_an_error(self):
        before = _sample("kdbai_token_mints_total", {"outcome": "error"})

        with pytest.raises(RuntimeError):
            with observe.token_mint():
                raise RuntimeError("token endpoint down")

        assert _sample("kdbai_token_mints_total", {"outcome": "error"}) - before == 1


class TestRunCoroTraceContext:
    """`_run_coro` hops to a worker thread; the trace context must survive the hop.

    `ThreadPoolExecutor.submit` starts its callable in a *fresh* context, so without an explicit
    `contextvars.copy_context()` the token-mint span silently exports as its own disconnected root
    trace rather than a child of the caller's span — no error, nothing to notice.
    """

    def test_a_span_inside_the_hop_is_a_child_of_the_callers_span(self, spans):
        async def inner():
            with span("kdbai.token_mint"):
                pass

        async def scenario():
            # Inside a running loop, so _run_coro takes the ThreadPoolExecutor path.
            with span("mcp.tool_invoke"):
                _run_coro(inner())

        asyncio.run(scenario())

        finished = {s.name: s for s in spans.get_finished_spans()}
        mint, dispatch = finished["kdbai.token_mint"], finished["mcp.tool_invoke"]
        assert mint.parent is not None, "the mint span orphaned into its own root trace"
        assert mint.parent.span_id == dispatch.context.span_id
        assert mint.context.trace_id == dispatch.context.trace_id

    def test_the_coroutines_result_still_reaches_the_caller(self):
        async def inner():
            return "token"

        async def scenario():
            return _run_coro(inner())

        assert asyncio.run(scenario()) == "token"

    def test_outside_a_running_loop_it_runs_in_place(self):
        async def inner():
            return "token"

        assert _run_coro(inner()) == "token"


class TestEmbedding:
    def test_names_the_span_by_kind_and_labels_the_provider(self, spans):
        with observe.embedding("openai", "dense"):
            pass

        span_ = spans.get_finished_spans()[0]
        assert span_.name == "kdbai.embed.dense"
        assert span_.attributes["kdbai.embed.provider"] == "openai"

    def test_does_not_land_on_the_sdk_metrics(self):
        labels = {"op": "embed.dense", "outcome": "ok"}
        before = _sample("kdbai_sdk_calls_total", labels)

        with observe.embedding("openai", "dense"):
            pass

        assert _sample("kdbai_sdk_calls_total", labels) == before


class TestResultRows:
    def test_records_a_returned_row_count(self):
        before = _sample("kdbai_result_rows_count")

        observe.record_result_rows(12)

        assert _sample("kdbai_result_rows_count") - before == 1


class TestConnects:
    def test_records_a_failed_connect(self):
        before = _sample("kdbai_connects_total", {"outcome": "failed"})

        observe.record_connect("failed")

        assert _sample("kdbai_connects_total", {"outcome": "failed"}) - before == 1
