"""Tests for the kdb-x backend's instrumentation seam."""

import pytest
from opentelemetry import trace as trace_api
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from prometheus_client import REGISTRY

from kx_mcp_kdbx.utils import observe


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


class TestQ:
    """`q()` — the qIPC round-trip seam."""

    def test_returns_the_connection_result_and_passes_args_through(self, mocker):
        conn = mocker.Mock(return_value="rows")

        assert observe.q(conn, "sql", "select from t", 7) == "rows"

        conn.assert_called_once_with("select from t", 7)

    def test_names_the_span_after_the_op(self, spans, mocker):
        observe.q(mocker.Mock(return_value=None), "bind", ".kx.auth.bind", {})

        assert [s.name for s in spans.get_finished_spans()] == ["kdbx.bind"]

    def test_counts_a_successful_call(self, mocker):
        before = _sample("kdbx_qipc_calls_total", {"op": "probe", "outcome": "ok"})

        observe.q(mocker.Mock(return_value=None), "probe", "")

        after = _sample("kdbx_qipc_calls_total", {"op": "probe", "outcome": "ok"})
        assert after - before == 1

    def test_records_a_duration_observation(self, mocker):
        before = _sample("kdbx_qipc_duration_seconds_count", {"op": "meta"})

        observe.q(mocker.Mock(return_value=None), "meta", "meta t")

        after = _sample("kdbx_qipc_duration_seconds_count", {"op": "meta"})
        assert after - before == 1

    def test_a_failing_call_propagates_and_is_counted_as_an_error(self, mocker):
        conn = mocker.Mock(side_effect=RuntimeError("boom"))
        before = _sample("kdbx_qipc_calls_total", {"op": "sql", "outcome": "error"})

        with pytest.raises(RuntimeError, match="boom"):
            observe.q(conn, "sql", "bad")

        after = _sample("kdbx_qipc_calls_total", {"op": "sql", "outcome": "error"})
        assert after - before == 1

    def test_a_failing_call_still_records_its_duration(self, mocker):
        conn = mocker.Mock(side_effect=RuntimeError("boom"))
        before = _sample("kdbx_qipc_duration_seconds_count", {"op": "entitled"})

        with pytest.raises(RuntimeError):
            observe.q(conn, "entitled", ".kx.auth.entitled")

        after = _sample("kdbx_qipc_duration_seconds_count", {"op": "entitled"})
        assert after - before == 1

    def test_a_raising_collector_does_not_chain_onto_the_callers_error(self, mocker):
        """The tool's real exception must reach the caller unchained.

        Mirrors the container-side rule: telemetry is never the reason a dispatch fails, and a
        collector raising inside `finally` would attach itself as the visible `__context__`.
        """
        mocker.patch.object(
            observe.QIPC_CALLS, "labels", side_effect=ValueError("collector exploded")
        )
        conn = mocker.Mock(side_effect=RuntimeError("the real failure"))

        with pytest.raises(RuntimeError, match="the real failure"):
            observe.q(conn, "sql", "bad")


class TestTimed:
    """`timed()` — the seam for calls that are not a plain `conn(expr, ...)`."""

    def test_traces_and_counts_a_block(self, spans):
        before = _sample("kdbx_qipc_calls_total", {"op": "tables", "outcome": "ok"})

        with observe.timed("tables"):
            pass

        assert [s.name for s in spans.get_finished_spans()] == ["kdbx.tables"]
        after = _sample("kdbx_qipc_calls_total", {"op": "tables", "outcome": "ok"})
        assert after - before == 1

    def test_attributes_are_set_at_span_start(self, spans):
        with observe.timed("connect", {"server.address": "localhost", "server.port": 5010}):
            pass

        span = spans.get_finished_spans()[0]
        assert span.attributes["server.address"] == "localhost"
        assert span.attributes["server.port"] == 5010


class TestEmbedding:
    """`embedding()` — separates provider latency from the search round-trip."""

    def test_names_the_span_by_kind_and_labels_the_provider(self, spans):
        with observe.embedding("openai", "dense"):
            pass

        span = spans.get_finished_spans()[0]
        assert span.name == "kdbx.embed.dense"
        assert span.attributes["kdbx.embed.provider"] == "openai"

    def test_records_duration_per_provider_and_kind(self):
        labels = {"provider": "sentence_transformers", "kind": "sparse"}
        before = _sample("kdbx_embed_duration_seconds_count", labels)

        with observe.embedding("sentence_transformers", "sparse"):
            pass

        assert _sample("kdbx_embed_duration_seconds_count", labels) - before == 1

    def test_does_not_land_on_the_qipc_metrics(self):
        """An embed is not a qIPC call; conflating them is the defect this split exists to avoid."""
        before = _sample("kdbx_qipc_calls_total", {"op": "embed.dense", "outcome": "ok"})

        with observe.embedding("openai", "dense"):
            pass

        assert _sample("kdbx_qipc_calls_total", {"op": "embed.dense", "outcome": "ok"}) == before


class TestSqlResult:
    def test_records_the_true_row_count(self):
        before = _sample("kdbx_sql_result_rows_count")

        observe.record_sql_result(42, cap=1000)

        assert _sample("kdbx_sql_result_rows_count") - before == 1

    def test_a_capped_result_counts_as_truncated(self):
        before = _sample("kdbx_sql_truncated_total")

        observe.record_sql_result(5000, cap=1000)

        assert _sample("kdbx_sql_truncated_total") - before == 1

    def test_a_result_at_the_cap_is_not_truncated(self):
        before = _sample("kdbx_sql_truncated_total")

        observe.record_sql_result(1000, cap=1000)

        assert _sample("kdbx_sql_truncated_total") == before


class TestAuthzAndConnects:
    def test_records_an_authz_decision(self):
        labels = {"adapter": "kdbx_entitlements", "action": "read", "decision": "partial"}
        before = _sample("kdbx_authz_consults_total", labels)

        observe.record_authz("kdbx_entitlements", "read", "partial")

        assert _sample("kdbx_authz_consults_total", labels) - before == 1

    def test_records_a_failed_connect(self):
        before = _sample("kdbx_connects_total", {"outcome": "failed"})

        observe.record_connect("failed")

        assert _sample("kdbx_connects_total", {"outcome": "failed"}) - before == 1

    def test_records_a_reconnect(self):
        before = _sample("kdbx_reconnects_total")

        observe.record_reconnect()

        assert _sample("kdbx_reconnects_total") - before == 1
