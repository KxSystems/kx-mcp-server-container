"""The opt-in observability seams: Prometheus metrics + OpenTelemetry tracing.

In-process and license-free, in the style of ``test_auth.py`` / ``test_audit_shape.py``: real
``make_parent`` + tiny fake bundles mounted via ``mount_bundle``, driven through a ``fastmcp.Client``.
Valid because the middleware attaches to the *parent* and doesn't know or care what a namespace's
tool actually does — which is the same reason no backend-specific logic is needed for the
multi-backend case.

Covers: both seams default off; the counter's three outcomes (ok / error / denied, the last through a
real ``@authorize`` denial so the audit-order dependency is genuinely exercised); the duration
histogram; per-namespace splitting with two bundles mounted; the stdio guard (warn + skip, no crash);
the route under an HTTP transport; settings normalisation and loud rejection of an unknown mode; and
the tracing middleware's span shape against a real in-memory exporter.

Metrics collectors are process-global by design (Prometheus's registry is), so every assertion here
is a **delta** around the dispatch rather than an absolute value — tests must not depend on suite
ordering.
"""

from __future__ import annotations

import asyncio
import importlib
import itertools
import logging

import pytest
from fastmcp import Client, FastMCP
from prometheus_client import REGISTRY, Counter, Gauge, Histogram

from kx_auth_core.authz import AuthzDecision
from kx_mcp_core import make_parent, mount_bundle, span, tool_result
from kx_mcp_core.auth import (
    AuthzSettings,
    authorize,
    configure_authz,
    begin_authz_dispatch,
    stamp_authz_decision,
)
from kx_mcp_core.observability import (
    METRICS_MODES,
    metric,
    TRACING_MODES,
    ObservabilitySettings,
    TracingMiddleware,
    mount_metrics_route,
)
from kx_mcp_core.observability import metrics as metrics_mod
from kx_mcp_core.observability import tracing as tracing_mod

# The package attribute `kx_mcp_core.auth.authorize` is the *function* (re-exported), shadowing the
# submodule — so fetch the real module object to monkeypatch its `current_principal` lookup
# (same idiom as test_authorize.py / test_audit_shape.py).
authorize_mod = importlib.import_module("kx_mcp_core.auth.authorize")

COUNTER_NAME = "kx_mcp_dispatches_total"
DURATION_COUNT = "kx_mcp_dispatch_duration_seconds_count"


# --- helpers -------------------------------------------------------------------------------------


def _counter(target: str, outcome: str) -> float:
    """The dispatch counter for one (target, outcome) pair — 0.0 before it is first incremented."""
    value = REGISTRY.get_sample_value(COUNTER_NAME, {"tool_name": target, "outcome": outcome})
    return 0.0 if value is None else value


def _duration_count(target: str) -> float:
    """How many observations the duration histogram holds for ``target``."""
    value = REGISTRY.get_sample_value(DURATION_COUNT, {"tool_name": target})
    return 0.0 if value is None else value


_UNIQUE = itertools.count()


def _unique(name: str) -> str:
    """A metric name no other test in this session has used.

    Prometheus's registry is process-global and a collector stays registered for the life of the
    process, so a fixed name in a test that *defines* a metric would collide with itself on a re-run
    within one session (and with any other test choosing the same name).
    """
    prefix, _, suffix = name.rpartition("_")
    return f"{prefix}{next(_UNIQUE)}_{suffix}"


@pytest.fixture
def owned_metrics():
    """Unregister collectors a test created, so it leaves the global registry as it found it.

    Append only collectors the test genuinely *created*: ``REGISTRY.unregister`` raises ``KeyError``
    for one that ``metric`` recovered rather than registered, and unregistering a collector another
    test owns would break it.
    """
    created: list = []
    yield created
    for collector in created:
        try:
            REGISTRY.unregister(collector)
        except KeyError:  # pragma: no cover - defensive; a recovered collector is not ours to drop
            pass


def _metrics_on(**kwargs) -> ObservabilitySettings:
    return ObservabilitySettings(metrics="prometheus", **kwargs)


def _ok_backend(name: str) -> FastMCP:
    """A minimal one-tool bundle standing in for a mounted backend extension."""
    mcp = FastMCP(name)

    @mcp.tool()
    def read() -> str:
        return "ok"

    return mcp


def _boom_backend(name: str) -> FastMCP:
    """A bundle whose tool raises — the `error` outcome."""
    mcp = FastMCP(name)

    @mcp.tool()
    def boom() -> str:
        raise RuntimeError("kaboom")

    return mcp


def _dispatch_tolerating_error(parent: FastMCP, tool_name: str):
    """Call a tool and return the result even when it is flagged `isError: true`.

    fastmcp's Client raises `ToolError` on an error-flagged result by default, which is correct
    client behaviour but would hide the thing under test here: the *server-side* middleware saw a
    returned result, not an exception, and must still classify it as a failure.
    """

    async def go():
        async with Client(parent) as client:
            return await client.call_tool(tool_name, {}, raise_on_error=False)

    return asyncio.run(go())


def _error_result_backend(name: str) -> FastMCP:
    """A bundle whose tools *return* failures instead of raising — this repo's actual convention.

    `broken` is a plain error; `refused` is an authorization denial, which stamps the decision the
    way `record_denial` does before returning the same kind of error result.
    """
    mcp = FastMCP(name)

    @mcp.tool()
    async def broken() -> dict:
        return tool_result({"status": "error", "message": "no_such_table"})

    @mcp.tool()
    async def refused() -> dict:
        stamp_authz_decision(
            AuthzDecision(allowed=False, adapter="kx_auth_qside", reason="denied: nope")
        )
        return tool_result({"status": "permission_denied", "message": "Access denied"})

    return mcp


def _gated_backend(name: str, action: str, resource: str) -> FastMCP:
    """A one-tool bundle whose tool carries an ``@authorize`` capability check."""
    mcp = FastMCP(name)

    @mcp.tool()
    @authorize(action=action, resource=resource)
    async def gated() -> str:
        return "did it"

    return mcp


def _dispatch(parent: FastMCP, tool_name: str, expect_error: bool = False) -> None:
    async def go():
        async with Client(parent) as client:
            await client.call_tool(tool_name, {})

    if expect_error:
        with pytest.raises(Exception):
            asyncio.run(go())
    else:
        asyncio.run(go())


@pytest.fixture(autouse=True)
def _reset_authz():
    """Isolate the module-global authz settings + decision contextvar between tests (mirrors
    test_authorize.py's ``_reset_authz``)."""
    authorize_mod._SETTINGS = None
    begin_authz_dispatch()
    yield
    authorize_mod._SETTINGS = None
    begin_authz_dispatch()


@pytest.fixture(autouse=True)
def _tracing_already_initialised():
    """Pretend tracing is already set up, so no test installs a real exporter.

    ``init_tracing`` is one-shot per process and ends in
    ``trace.set_tracer_provider`` + a ``BatchSpanProcessor`` — a *process-global* side effect plus a
    background export thread that would retry against a nonexistent collector and leak
    ``StatusCode.UNAVAILABLE`` noise across the suite. Flipping the guard on by default makes every
    ``make_parent(observability=...)`` in this module skip that work; the few tests that exercise the setup
    path itself reset the guard and stub the exporter.

    **This is now load-bearing, not just noise suppression.** ``make_parent`` attaches
    ``TracingMiddleware`` only when ``init_tracing`` returns True, and pre-setting the guard is what
    makes it return True early. Without this fixture the attach tests below would depend on a real
    exporter build, and would silently assert "no middleware" instead of the ordering they mean to
    pin. See ``test_tracing_middleware_is_not_attached_when_setup_fails`` for the negative case.
    """
    tracing_mod._initialised = True
    yield
    tracing_mod.reset_tracing_for_tests()


@pytest.fixture
def uninitialised_tracing():
    """Reset the one-shot guard for tests that drive ``init_tracing``'s setup path."""
    tracing_mod.reset_tracing_for_tests()
    yield
    tracing_mod.reset_tracing_for_tests()


@pytest.fixture
def stub_otlp_exporter(monkeypatch):
    """Replace OTLPSpanExporter with an inert stub — exercises the real setup path, no network.

    ``init_tracing`` imports the exporter *inside* the function, so patching the attribute on its
    module is enough; the import resolves at call time.
    """
    from opentelemetry.exporter.otlp.proto.grpc import trace_exporter

    class _StubExporter:
        instances: list = []

        def __init__(self, *args, **kwargs):
            self.kwargs = kwargs
            _StubExporter.instances.append(self)

        def export(self, spans):  # pragma: no cover - never called; nothing is exported here
            return None

        def shutdown(self):
            return None

        def force_flush(self, timeout_millis=None):
            return True

    _StubExporter.instances = []
    monkeypatch.setattr(trace_exporter, "OTLPSpanExporter", _StubExporter)
    return _StubExporter


@pytest.fixture
def alice(monkeypatch):
    """current_principal() returns a fake authenticated principal in group 'traders'."""

    class _FakePrincipal:
        client_id = "alice"
        claims = {"sub": "alice", "groups": ["traders"]}

    monkeypatch.setattr("kx_mcp_core.auth.audit.current_principal", lambda: _FakePrincipal())
    monkeypatch.setattr(authorize_mod, "current_principal", lambda: _FakePrincipal())


# --- the zero-setup default ----------------------------------------------------------------------


def test_both_seams_default_off():
    """Unset env -> both selectors off, so a default deployment is unchanged."""
    settings = ObservabilitySettings()
    assert settings.metrics == ""
    assert settings.tracing == ""
    assert settings.metrics_enabled is False
    assert settings.tracing_enabled is False
    assert settings.enabled is False


def test_default_make_parent_attaches_no_observability_middleware():
    """No settings passed -> only the audit middleware; no metrics/tracing wrapper."""
    parent = make_parent("kx-mcp")
    kinds = {type(m).__name__ for m in parent.middleware}
    assert "MetricsMiddleware" not in kinds
    assert "TracingMiddleware" not in kinds


def test_metrics_off_records_nothing(alice):
    """With the seam off the counter must not move, even though dispatches happen."""
    parent = make_parent("kx-mcp")  # observability=None
    mount_bundle(parent, lambda: _ok_backend("off"), namespace="off")

    before = _counter("off_read", "ok")
    _dispatch(parent, "off_read")
    assert _counter("off_read", "ok") == before


# --- the three outcomes --------------------------------------------------------------------------


def test_counter_records_tool_success(alice):
    parent = make_parent("kx-mcp", observability=_metrics_on())
    mount_bundle(parent, lambda: _ok_backend("m1"), namespace="m1")

    before = _counter("m1_read", "ok")
    _dispatch(parent, "m1_read")
    assert _counter("m1_read", "ok") == before + 1


def test_counter_records_tool_failure_as_error(alice):
    """A raising tool with no authz decision in play is an `error`, not a `denied`."""
    parent = make_parent("kx-mcp", observability=_metrics_on())
    mount_bundle(parent, lambda: _boom_backend("m2"), namespace="m2")

    before_err = _counter("m2_boom", "error")
    before_ok = _counter("m2_boom", "ok")
    _dispatch(parent, "m2_boom", expect_error=True)

    assert _counter("m2_boom", "error") == before_err + 1
    assert _counter("m2_boom", "ok") == before_ok  # not counted as a success


def test_counter_records_authorize_denial_as_denied(alice, tmp_path):
    """A denied @authorize-decorated tool records `denied`, not `error`.

    This is also the regression for the middleware attach order: the decision lives on a contextvar
    that AuditMiddleware resets in its own ``finally``, so metrics only sees it while running *inside*
    audit. If make_parent ever attaches metrics before audit, this flips to `error`.
    """
    policy = tmp_path / "capability-policy.yaml"
    policy.write_text("m3:\n  write: [admins]\n")  # alice is in 'traders' -> denied
    configure_authz(AuthzSettings(mode="static", policy_file=str(policy)))

    parent = make_parent("kx-mcp", observability=_metrics_on())
    mount_bundle(
        parent, lambda: _gated_backend("m3", "write", "m3:thing"), namespace="m3"
    )

    before_denied = _counter("m3_gated", "denied")
    before_error = _counter("m3_gated", "error")
    _dispatch(parent, "m3_gated", expect_error=True)

    assert _counter("m3_gated", "denied") == before_denied + 1
    assert _counter("m3_gated", "error") == before_error


def test_counter_records_authorize_allow_as_ok(alice, tmp_path):
    """The allow side of the same gated tool records `ok`."""
    policy = tmp_path / "capability-policy.yaml"
    policy.write_text("m4:\n  write: [traders]\n")  # alice IS in 'traders'
    configure_authz(AuthzSettings(mode="static", policy_file=str(policy)))

    parent = make_parent("kx-mcp", observability=_metrics_on())
    mount_bundle(
        parent, lambda: _gated_backend("m4", "write", "m4:thing"), namespace="m4"
    )

    before = _counter("m4_gated", "ok")
    _dispatch(parent, "m4_gated")
    assert _counter("m4_gated", "ok") == before + 1


def test_counter_records_a_returned_error_result_as_error(alice):
    """A tool that *returns* `isError: true` is counted `error`, not `ok`.

    The complement of the raise path above. This repo's tools report most failures by returning a
    structured payload rather than raising (a bad table name is an expected condition, not an
    exception), so without this the `error` series stays flat through an outage. The protocol's
    own `isError` flag is the signal — middleware never inspects a backend's payload fields.
    """
    settings = _metrics_on()
    parent = make_parent("kx-mcp", observability=settings)
    parent.mount(_error_result_backend("er"), namespace="er")

    before = _counter("er_broken", "error")
    result = _dispatch_tolerating_error(parent, "er_broken")
    assert result.is_error is True

    assert _counter("er_broken", "error") == before + 1
    assert _counter("er_broken", "ok") == 0.0


def test_returned_denial_outranks_the_error_flag(alice):
    """A denial carries `isError: true` too, and must still be counted `denied`.

    Both signals are present on this dispatch: the result is flagged an error *and* the authz
    decision says deny. The decision wins, so an authorization refusal never dilutes the fault
    signal an operator alerts on.
    """
    settings = _metrics_on()
    parent = make_parent("kx-mcp", observability=settings)
    parent.mount(_error_result_backend("dn"), namespace="dn")

    before = _counter("dn_refused", "denied")
    result = _dispatch_tolerating_error(parent, "dn_refused")
    assert result.is_error is True

    assert _counter("dn_refused", "denied") == before + 1
    assert _counter("dn_refused", "error") == 0.0


def test_tracing_marks_a_returned_error_result(alice, spans):
    """The span agrees with the counter: outcome=error and a non-OK status, without an exception."""
    tracer, exporter = spans
    parent = _traced_parent(tracer)
    mount_bundle(parent, lambda: _error_result_backend("tr"), namespace="tr")

    result = _dispatch_tolerating_error(parent, "tr_broken")
    assert result.is_error is True

    span = exporter.get_finished_spans()[0]
    assert span.attributes["mcp.outcome"] == "error"
    assert span.status.status_code.name == "ERROR"
    # An exception was never raised, so nothing should be recorded as one.
    assert not any(e.name == "exception" for e in span.events)


# --- the duration histogram ----------------------------------------------------------------------


def test_histogram_observes_every_dispatch(alice):
    parent = make_parent("kx-mcp", observability=_metrics_on())
    mount_bundle(parent, lambda: _ok_backend("h1"), namespace="h1")

    before = _duration_count("h1_read")
    _dispatch(parent, "h1_read")
    _dispatch(parent, "h1_read")
    assert _duration_count("h1_read") == before + 2


def test_histogram_observes_failed_dispatch_too(alice):
    """Duration is recorded in a ``finally``, so a slow-then-failing tool still lands in the histogram."""
    parent = make_parent("kx-mcp", observability=_metrics_on())
    mount_bundle(parent, lambda: _boom_backend("h2"), namespace="h2")

    before = _duration_count("h2_boom")
    _dispatch(parent, "h2_boom", expect_error=True)
    assert _duration_count("h2_boom") == before + 1


# --- multi-backend: no special-casing ------------------------------------------------------------


def test_metrics_split_by_namespaced_tool_name_across_two_bundles(alice):
    """Two bundles on one parent -> counters keyed by the already-namespaced target.

    Deliberately no backend-specific logic anywhere: the namespacing comes from ``mount``, and parent
    middleware simply reads ``context.message.name``.
    """
    parent = make_parent("kx-mcp", observability=_metrics_on())
    mount_bundle(parent, lambda: _ok_backend("kdbx"), namespace="kdbx")
    mount_bundle(parent, lambda: _ok_backend("kdbai"), namespace="kdbai")

    before_x = _counter("kdbx_read", "ok")
    before_ai = _counter("kdbai_read", "ok")

    _dispatch(parent, "kdbx_read")
    _dispatch(parent, "kdbai_read")
    _dispatch(parent, "kdbai_read")

    assert _counter("kdbx_read", "ok") == before_x + 1
    assert _counter("kdbai_read", "ok") == before_ai + 2


# --- the scrape route + the stdio guard ----------------------------------------------------------


@pytest.mark.parametrize("transport", ["streamable-http", "http"])
def test_route_mounted_under_http_transports(transport):
    parent = make_parent("kx-mcp", observability=_metrics_on())
    assert mount_metrics_route(parent, _metrics_on(), transport) is True
    paths = [getattr(r, "path", None) for r in parent._additional_http_routes]
    assert "/metrics" in paths


def test_route_honours_custom_path():
    parent = make_parent("kx-mcp", observability=_metrics_on())
    settings = _metrics_on(metrics_path="/internal/metrics")
    assert mount_metrics_route(parent, settings, "streamable-http") is True
    paths = [getattr(r, "path", None) for r in parent._additional_http_routes]
    assert "/internal/metrics" in paths


def test_stdio_transport_warns_and_mounts_no_route(caplog):
    """stdio has no HTTP listener: warn clearly, skip the route, never crash."""
    parent = make_parent("kx-mcp", observability=_metrics_on())

    with caplog.at_level(logging.WARNING, logger="kx_mcp_core.observability.metrics"):
        mounted = mount_metrics_route(parent, _metrics_on(), "stdio")

    assert mounted is False
    # Asserted against the metrics path, not an empty route list: the parent always carries the
    # container's own /health route.
    assert "/metrics" not in [getattr(r, "path", None) for r in parent._additional_http_routes]

    warnings = [r.message for r in caplog.records if r.levelno == logging.WARNING]
    assert any("serves no HTTP endpoint" in m for m in warnings), warnings


def test_stdio_guard_still_collects_metrics_in_process(alice, caplog):
    """The stdio skip is route-only — the middleware keeps counting."""
    settings = _metrics_on()
    parent = make_parent("kx-mcp", observability=settings)
    mount_bundle(parent, lambda: _ok_backend("s1"), namespace="s1")
    mount_metrics_route(parent, settings, "stdio")

    before = _counter("s1_read", "ok")
    _dispatch(parent, "s1_read")
    assert _counter("s1_read", "ok") == before + 1


def test_metrics_off_mounts_no_route():
    parent = make_parent("kx-mcp")
    assert mount_metrics_route(parent, ObservabilitySettings(), "streamable-http") is False
    assert "/metrics" not in [getattr(r, "path", None) for r in parent._additional_http_routes]


def test_metrics_endpoint_serves_prometheus_text(alice):
    """End-to-end: the mounted route answers 200 with the Prometheus exposition content type."""
    httpx = pytest.importorskip("httpx")

    settings = _metrics_on()
    parent = make_parent("kx-mcp", observability=settings)
    mount_bundle(parent, lambda: _ok_backend("e1"), namespace="e1")
    mount_metrics_route(parent, settings, "streamable-http")

    _dispatch(parent, "e1_read")  # generate a sample to scrape

    async def scrape():
        transport = httpx.ASGITransport(app=parent.http_app())
        async with httpx.AsyncClient(transport=transport, base_url="http://kx-mcp") as client:
            return await client.get("/metrics")

    response = asyncio.run(scrape())

    assert response.status_code == 200
    assert "text/plain" in response.headers["content-type"]
    assert COUNTER_NAME in response.text
    assert 'tool_name="e1_read"' in response.text


def _scrape(parent) -> str:
    """GET the mounted /metrics route through a real ASGI round-trip and return the body."""
    httpx = pytest.importorskip("httpx")

    async def go():
        transport = httpx.ASGITransport(app=parent.http_app())
        async with httpx.AsyncClient(transport=transport, base_url="http://kx-mcp") as client:
            return await client.get("/metrics")

    response = asyncio.run(go())
    assert response.status_code == 200
    return response.text


def _families(text: str) -> dict:
    """Parse Prometheus exposition text into {family_name: [label-set strings]}."""
    families: dict = {}
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        series = line.split(" ")[0]
        name, _, labels = series.partition("{")
        families.setdefault(name, []).append(labels.rstrip("}"))
    return families


def test_metrics_exposition_matches_pinned_names(alice, tmp_path):
    """Real scrape-and-parse: the exposition keeps the operator-facing naming contract.

    Asserts against the *parsed scrape text*, not internal objects, because the text is what an
    operator's dashboard is written against. Worth keeping regardless of which library renders it —
    these exact strings are the contract, and a rename would break every existing dashboard.
    """
    policy = tmp_path / "p.yaml"
    policy.write_text("x1:\n  write: [admins]\n")  # alice not in admins -> a `denied` sample
    configure_authz(AuthzSettings(mode="static", policy_file=str(policy)))

    settings = _metrics_on()
    parent = make_parent("kx-mcp", observability=settings)
    mount_bundle(parent, lambda: _ok_backend("x1"), namespace="x1")
    mount_bundle(parent, lambda: _boom_backend("x1b"), namespace="x1b")
    mount_bundle(parent, lambda: _gated_backend("x1", "write", "x1:thing"), namespace="x1g")
    mount_metrics_route(parent, settings, "streamable-http")

    _dispatch(parent, "x1_read")
    _dispatch(parent, "x1b_boom", expect_error=True)
    _dispatch(parent, "x1g_gated", expect_error=True)

    families = _families(_scrape(parent))

    # 1. The counter family name is exactly the contracted string.
    assert "kx_mcp_dispatches_total" in families

    # 2. The histogram's three families are exactly the contracted strings.
    for suffix in ("_bucket", "_count", "_sum"):
        assert f"kx_mcp_dispatch_duration_seconds{suffix}" in families

    # 3. Label names are exactly `tool_name` + `outcome`, and nothing else.
    counter_labels = families["kx_mcp_dispatches_total"]
    assert counter_labels, "counter emitted no series"
    for labels in counter_labels:
        keys = {part.split("=")[0] for part in labels.split(",")}
        assert keys == {"tool_name", "outcome"}, keys

    for labels in families["kx_mcp_dispatch_duration_seconds_count"]:
        keys = {part.split("=")[0] for part in labels.split(",")}
        assert keys == {"tool_name"}, keys

    # 4. All three outcome values still appear, with the same spellings.
    outcomes = {
        part.split("=")[1].strip('"')
        for labels in counter_labels
        for part in labels.split(",")
        if part.startswith("outcome=")
    }
    assert outcomes == {"ok", "error", "denied"}, outcomes


def test_python_collector_still_exposed(alice):
    """The scrape carries the default registry's own collectors alongside ours.

    Our collectors live in ``prometheus_client``'s *global* ``REGISTRY``, which is what keeps
    ``python_*`` in the exposition (GCCollector/PlatformCollector — no ``/proc`` dependency, so this
    holds on every platform). Switching to a private registry would silently drop it from every
    existing dashboard, so this pins the choice. (``process_*``, from ``ProcessCollector``, is
    intentionally not asserted here — it depends on reading ``/proc`` and produces nothing on macOS.)
    """
    settings = _metrics_on()
    parent = make_parent("kx-mcp", observability=settings)
    mount_metrics_route(parent, settings, "streamable-http")

    families = _families(_scrape(parent))

    assert any(f.startswith("python_") for f in families), sorted(families)


# --- settings ------------------------------------------------------------------------------------


@pytest.mark.parametrize("raw", ["", "   ", None])
def test_blank_modes_collapse_to_off(raw):
    settings = ObservabilitySettings(metrics=raw, tracing=raw)
    assert settings.metrics == ""
    assert settings.tracing == ""


@pytest.mark.parametrize("raw", ["PROMETHEUS", " prometheus ", "Prometheus"])
def test_metrics_mode_is_normalised(raw):
    assert ObservabilitySettings(metrics=raw).metrics_enabled is True


def test_unknown_metrics_mode_is_rejected_loudly():
    """A typo must fail at startup, not silently leave the seam off."""
    with pytest.raises(Exception) as exc:
        ObservabilitySettings(metrics="prometheous")
    assert "KX_MCP_METRICS" in str(exc.value)


def test_unknown_tracing_mode_is_rejected_loudly():
    with pytest.raises(Exception) as exc:
        ObservabilitySettings(tracing="jaeger")
    assert "KX_MCP_TRACING" in str(exc.value)


def test_settings_read_from_environment(monkeypatch):
    """The bare selectors + their detail vars bind from env, like KX_MCP_AUTH / KX_MCP_AUTHZ."""
    monkeypatch.setenv("KX_MCP_METRICS", "prometheus")
    monkeypatch.setenv("KX_MCP_METRICS_PATH", "/m")
    monkeypatch.setenv("KX_MCP_TRACING", "otlp")
    monkeypatch.setenv("KX_MCP_TRACING_OTLP_ENDPOINT", "http://collector:4317")
    monkeypatch.setenv("KX_MCP_TRACING_SERVICE_NAME", "kx-mcp-prod")

    settings = ObservabilitySettings()

    assert settings.metrics_enabled and settings.tracing_enabled
    assert settings.metrics_path == "/m"
    assert settings.otlp_endpoint == "http://collector:4317"
    assert settings.service_name == "kx-mcp-prod"


def test_metrics_path_gets_a_leading_slash():
    assert ObservabilitySettings(metrics_path="metrics").metrics_path == "/metrics"


def test_declared_modes_are_the_accepted_ones():
    assert METRICS_MODES == ("prometheus",)
    assert TRACING_MODES == ("otlp",)


# --- tracing -------------------------------------------------------------------------------------


@pytest.fixture
def spans():
    """A real TracerProvider writing to an in-memory exporter; yields the finished-span getter."""
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("test")
    yield tracer, exporter


def _traced_parent(tracer) -> FastMCP:
    """A parent with the tracing middleware attached against an injected tracer.

    Injecting the tracer (rather than going through ``init_tracing``) keeps the test off the network
    while still exercising the real middleware and real SDK spans. Attached after audit, matching
    ``make_parent``'s order.
    """
    parent = make_parent("kx-mcp")
    parent.add_middleware(TracingMiddleware(tracer=tracer))
    return parent


def test_tracing_opens_one_span_per_dispatch(alice, spans):
    tracer, exporter = spans
    parent = _traced_parent(tracer)
    mount_bundle(parent, lambda: _ok_backend("t1"), namespace="t1")

    _dispatch(parent, "t1_read")

    finished = exporter.get_finished_spans()
    assert len(finished) == 1
    span = finished[0]
    assert span.name == "mcp.tool_invoke"
    assert span.attributes["mcp.action"] == "tool_invoke"
    assert span.attributes["mcp.target"] == "t1_read"
    assert span.attributes["mcp.outcome"] == "ok"


def test_tracing_records_exception_and_closes_span(alice, spans):
    """A failing dispatch still ends its span, marked ERROR and carrying the exception event."""
    from opentelemetry.trace import StatusCode

    tracer, exporter = spans
    parent = _traced_parent(tracer)
    mount_bundle(parent, lambda: _boom_backend("t2"), namespace="t2")

    _dispatch(parent, "t2_boom", expect_error=True)

    finished = exporter.get_finished_spans()
    assert len(finished) == 1  # closed in the finally despite the raise
    span = finished[0]
    assert span.attributes["mcp.outcome"] == "error"
    assert span.status.status_code is StatusCode.ERROR
    assert [e.name for e in span.events] == ["exception"]


def test_tracing_span_target_is_namespaced_per_backend(alice, spans):
    """Same no-special-casing property as metrics: the span target is already namespaced."""
    tracer, exporter = spans
    parent = _traced_parent(tracer)
    mount_bundle(parent, lambda: _ok_backend("kdbx"), namespace="kdbx")
    mount_bundle(parent, lambda: _ok_backend("kdbai"), namespace="kdbai")

    _dispatch(parent, "kdbx_read")
    _dispatch(parent, "kdbai_read")

    targets = [s.attributes["mcp.target"] for s in exporter.get_finished_spans()]
    assert targets == ["kdbx_read", "kdbai_read"]


def test_tracing_stamps_the_authz_decision(alice, spans, tmp_path):
    """A capability denial shows up on the span as decision + adapter + a denied outcome."""
    policy = tmp_path / "capability-policy.yaml"
    policy.write_text("t3:\n  write: [admins]\n")  # alice is in 'traders' -> denied
    configure_authz(AuthzSettings(mode="static", policy_file=str(policy)))

    tracer, exporter = spans
    parent = _traced_parent(tracer)
    mount_bundle(
        parent, lambda: _gated_backend("t3", "write", "t3:thing"), namespace="t3"
    )

    _dispatch(parent, "t3_gated", expect_error=True)

    span = exporter.get_finished_spans()[0]
    assert span.attributes["mcp.outcome"] == "denied"
    assert span.attributes["mcp.authz.decision"] == "deny"
    assert span.attributes["mcp.authz.adapter"] == "static"


def test_init_tracing_is_a_noop_when_off(uninitialised_tracing):
    """Off -> returns False before importing or configuring anything."""
    assert tracing_mod.init_tracing(ObservabilitySettings()) is False
    assert tracing_mod._initialised is False


def test_init_tracing_sets_up_the_exporter_once(uninitialised_tracing, stub_otlp_exporter):
    """The setup path builds one exporter and marks itself initialised."""
    settings = ObservabilitySettings(
        tracing="otlp", otlp_endpoint="http://collector:4317", service_name="kx-mcp-test"
    )

    assert tracing_mod.init_tracing(settings) is True
    assert tracing_mod._initialised is True
    assert len(stub_otlp_exporter.instances) == 1
    assert stub_otlp_exporter.instances[0].kwargs["endpoint"] == "http://collector:4317"


def test_init_tracing_omits_endpoint_when_unset(uninitialised_tracing, stub_otlp_exporter):
    """No endpoint configured -> construct the exporter bare so the OTel SDK applies its own default."""
    assert tracing_mod.init_tracing(ObservabilitySettings(tracing="otlp")) is True
    assert stub_otlp_exporter.instances[0].kwargs == {}


def test_init_tracing_is_idempotent(uninitialised_tracing, stub_otlp_exporter):
    """Both the launcher and hand-written glue may reach init_tracing; the second call no-ops."""
    settings = ObservabilitySettings(tracing="otlp", otlp_endpoint="http://collector:4317")

    assert tracing_mod.init_tracing(settings) is True
    assert tracing_mod.init_tracing(settings) is True  # still True...
    assert len(stub_otlp_exporter.instances) == 1  # ...but no second provider/exporter


def test_init_tracing_survives_a_broken_exporter(uninitialised_tracing, monkeypatch, caplog):
    """Telemetry setup must never take the container down (the try_mount_bundle posture)."""
    from opentelemetry.exporter.otlp.proto.grpc import trace_exporter

    def explode(*args, **kwargs):
        raise RuntimeError("simulated exporter failure")

    monkeypatch.setattr(trace_exporter, "OTLPSpanExporter", explode)

    settings = ObservabilitySettings(tracing="otlp", otlp_endpoint="http://collector:4317")

    with caplog.at_level(logging.WARNING, logger="kx_mcp_core.observability.tracing"):
        assert tracing_mod.init_tracing(settings) is False

    assert any("serving without traces" in r.message for r in caplog.records)
    assert tracing_mod._initialised is False  # a failed setup must not latch the guard


def test_init_tracing_names_the_extra_when_it_is_missing(
    uninitialised_tracing, monkeypatch, caplog
):
    """A missing `tracing` extra must be told apart from any other setup failure.

    OpenTelemetry is an optional extra (``kx-mcp-core[tracing]``), so the one failure an operator can
    actually fix by installing something must say so by name — not surface a bare
    ModuleNotFoundError. Still non-fatal: same "serving without traces" outcome as every other
    setup failure.
    """
    from opentelemetry.exporter.otlp.proto.grpc import trace_exporter

    def missing(*args, **kwargs):
        raise ModuleNotFoundError("No module named 'opentelemetry.exporter'")

    monkeypatch.setattr(trace_exporter, "OTLPSpanExporter", missing)

    settings = ObservabilitySettings(tracing="otlp", otlp_endpoint="http://collector:4317")

    with caplog.at_level(logging.WARNING, logger="kx_mcp_core.observability.tracing"):
        assert tracing_mod.init_tracing(settings) is False

    message = " ".join(r.getMessage() for r in caplog.records)
    assert "kx-mcp-core[tracing]" in message  # the actionable fix
    assert "extra is not installed" in message  # distinct from the generic "setup failed"
    assert "serving without traces" in message  # ...but the same non-fatal outcome
    assert tracing_mod._initialised is False


# --- middleware attach order (the subtle invariant) ----------------------------------------------


def test_make_parent_attaches_observability_after_audit():
    """Audit must stay outermost — see the ordering note in make_parent and the denial test above."""
    settings = ObservabilitySettings(metrics="prometheus", tracing="otlp")
    parent = make_parent("kx-mcp", observability=settings)

    order = [type(m).__name__ for m in parent.middleware]
    assert order.index("AuditMiddleware") < order.index("TracingMiddleware")
    assert order.index("AuditMiddleware") < order.index("MetricsMiddleware")


def test_middleware_can_be_attached_independently():
    """Metrics on / tracing off (and vice versa) each attach only their own middleware.

    One settings object now drives both seams, so "independently" is decided by the two *selectors*
    within it rather than by handing ``make_parent`` two different objects.
    """
    only_metrics = make_parent("a", observability=_metrics_on())
    kinds = {type(m).__name__ for m in only_metrics.middleware}
    assert "MetricsMiddleware" in kinds and "TracingMiddleware" not in kinds

    tracing_settings = ObservabilitySettings(tracing="otlp", otlp_endpoint="http://collector:4317")
    only_tracing = make_parent("b", observability=tracing_settings)
    kinds = {type(m).__name__ for m in only_tracing.middleware}
    assert "TracingMiddleware" in kinds and "MetricsMiddleware" not in kinds


# --- resource / prompt hooks ---------------------------------------------------------------------


def test_resource_read_is_counted_by_uri(alice):
    """The resource hook keys the counter on the (namespaced) URI, not a tool name."""
    mcp = FastMCP("r1")

    @mcp.resource("data://thing")
    def thing() -> str:
        return "payload"

    settings = _metrics_on()
    parent = make_parent("kx-mcp", observability=settings)
    mount_bundle(parent, lambda: mcp, namespace="r1")

    uris = [str(r.uri) for r in asyncio.run(parent.list_resources())]
    assert len(uris) == 1, uris
    uri = uris[0]

    before = _counter(uri, "ok")

    async def go():
        async with Client(parent) as client:
            await client.read_resource(uri)

    asyncio.run(go())

    assert _counter(uri, "ok") == before + 1


def test_prompt_get_is_counted_by_name(alice):
    mcp = FastMCP("p1")

    @mcp.prompt()
    def greet() -> str:
        return "hello"

    settings = _metrics_on()
    parent = make_parent("kx-mcp", observability=settings)
    mount_bundle(parent, lambda: mcp, namespace="p1")

    before = _counter("p1_greet", "ok")

    async def go():
        async with Client(parent) as client:
            await client.get_prompt("p1_greet", {})

    asyncio.run(go())

    assert _counter("p1_greet", "ok") == before + 1


# --- module-level collector identity -------------------------------------------------------------


def test_collectors_are_module_level_and_shared():
    """The registry is process-global on purpose: re-importing must not create a second collector.

    ``prometheus_client`` refuses a duplicate timeseries name, so a module reload would raise without
    ``metric``'s recovery — and a second collector would split one logical series in two.
    """
    reimported = importlib.import_module("kx_mcp_core.observability.metrics")
    assert reimported.DISPATCH_COUNTER is metrics_mod.DISPATCH_COUNTER
    assert reimported.DISPATCH_DURATION is metrics_mod.DISPATCH_DURATION


def test_metric_returns_the_existing_collector_on_duplicate():
    """The duplicate-registration recovery: a second definition of the same name reuses the first.

    Directly exercises ``metric``'s except-branch — the reason a module reload is a no-op rather than
    a ``Duplicated timeseries`` crash. Note the caller passes only the metric's real name; deriving
    the registry key (``kx_mcp_dispatches``, the ``_total`` munged away) is ``metric``'s job.
    """
    again = metric(
        Counter,
        "kx_mcp_dispatches_total",
        "duplicate definition — must not register a second collector",
        ["tool_name", "outcome"],
    )
    assert again is metrics_mod.DISPATCH_COUNTER


def test_metric_recovers_a_histogram_without_the_total_munge():
    """The base-name derivation is type-aware: only a Counter sheds a trailing ``_total``."""
    again = metric(
        Histogram,
        "kx_mcp_dispatch_duration_seconds",
        "duplicate definition — must not register a second collector",
        ["tool_name"],
    )
    assert again is metrics_mod.DISPATCH_DURATION


def test_metric_rejects_a_name_already_taken_by_a_different_collector(owned_metrics):
    """A real collision between two bundles must fail here, not at some later ``.labels()`` call.

    Returning the registered collector regardless would appear to work until a label name mismatched,
    and the traceback would then surface far from the definition that caused it.
    """
    name = _unique("acme_collision_total")
    owned_metrics.append(metric(Counter, name, "first definition", ["kind"]))

    with pytest.raises(ValueError, match="already registered as Counter"):
        metric(Counter, name, "second definition, different labels", ["tenant"])

    with pytest.raises(ValueError, match="already registered as Counter"):
        metric(Gauge, name, "second definition, different class", ["kind"])


def test_metric_forwards_constructor_kwargs(owned_metrics):
    """``**kwargs`` reaches the collector, so buckets/namespace/unit are not lost to the helper."""
    hist = metric(
        Histogram,
        _unique("acme_latency_seconds"),
        "custom buckets survive",
        ["kind"],
        buckets=(0.1, 1.0),
    )
    owned_metrics.append(hist)
    assert hist._upper_bounds == [0.1, 1.0, float("inf")]


def test_metric_reraises_a_valueerror_that_is_not_a_collision():
    """A ValueError that is not a duplicate registration is a caller bug — do not swallow it.

    ``metric`` recovers from ``ValueError`` because that is what a duplicate name raises, so it has to
    be careful not to absorb the *other* things prometheus_client raises it for. A reserved (``__``)
    label name is one: it fails before registration, so no collector exists to recover, and the error
    must reach the author. (Note prometheus_client validates neither the metric name nor ordinary
    label names at construction, so those are not available as a test case.)
    """
    with pytest.raises(ValueError, match="Reserved label"):
        metric(Counter, _unique("acme_reserved_total"), "nope", ["__reserved"])


# --- degrading instead of arming a broken hot path -----------------------------------------------
#
# Telemetry must never be the reason a dispatch fails. Two ways that could happen, both covered here:
# tracing requested without the `kx-mcp-core[tracing]` extra (so `init_tracing` declines and the
# middleware must not be attached at all), and a collector that raises while the middleware records.


def test_tracing_middleware_is_not_attached_when_setup_fails(uninitialised_tracing, monkeypatch):
    """Asked to trace, but the extra is missing -> serve untraced, with no span hook attached.

    ``init_tracing`` already logs "serving without traces" in this case. Attaching the middleware
    anyway would put a span-opening hook on every dispatch that can never export anything, and would
    reintroduce the per-dispatch ``opentelemetry`` import the operator was just told we were skipping.
    """
    from opentelemetry.exporter.otlp.proto.grpc import trace_exporter

    def missing(*args, **kwargs):
        raise ModuleNotFoundError("No module named 'opentelemetry.exporter'")

    monkeypatch.setattr(trace_exporter, "OTLPSpanExporter", missing)

    parent = make_parent(
        "kx-mcp", observability=ObservabilitySettings(tracing="otlp", metrics="prometheus")
    )

    kinds = {type(m).__name__ for m in parent.middleware}
    assert "TracingMiddleware" not in kinds
    assert "MetricsMiddleware" in kinds  # the other seam is unaffected by tracing's failure


class _ExplodingCollector:
    """Stands in for a collector that raises on use (a cardinality blow-up, a bad label value)."""

    def labels(self, **_kwargs):
        raise ValueError("Incorrect label names")


def test_a_broken_counter_does_not_disturb_a_successful_dispatch(monkeypatch):
    monkeypatch.setattr(metrics_mod, "DISPATCH_COUNTER", _ExplodingCollector())
    middleware = metrics_mod.MetricsMiddleware()

    async def call_next(_context):
        return "served"

    assert asyncio.run(middleware._observe("acme_read", None, call_next)) == "served"


def test_a_broken_histogram_does_not_chain_onto_the_tools_own_exception(monkeypatch):
    """The one place a raising collector does real damage, so it gets its own test.

    ``_observe`` records the duration in a ``finally``. An exception raised there while the tool's own
    exception is in flight does not *replace* it — Python chains it via ``__context__`` — so the client
    reads a confusing "During handling of the above exception, another exception occurred" wrapping the
    real error. The tool's failure must reach the caller exactly as the tool raised it.
    """
    monkeypatch.setattr(metrics_mod, "DISPATCH_DURATION", _ExplodingCollector())
    middleware = metrics_mod.MetricsMiddleware()

    async def call_next(_context):
        raise RuntimeError("kaboom")

    with pytest.raises(RuntimeError, match="kaboom") as excinfo:
        asyncio.run(middleware._observe("acme_boom", None, call_next))

    assert excinfo.value.__context__ is None
    assert excinfo.value.__cause__ is None


# --- the bundle-author instrumentation surface: span() -------------------------------------------


@pytest.fixture
def global_tracer_provider(monkeypatch):
    """Install a real TracerProvider *globally*, scoped to one test, and yield its exporter.

    ``span()`` deliberately resolves through ``trace.get_tracer`` — the global provider — because that
    is what a bundle add-in gets at runtime and what makes it a no-op when tracing is off. So unlike
    the ``spans`` fixture (which injects a tracer straight into the middleware and never touches
    global state), asserting anything about a bundle's span requires the global slot to be filled.

    OpenTelemetry guards that slot with a one-shot ``Once`` and refuses a second
    ``set_tracer_provider``, which would make this leak into every later test in the session. Swapping
    both module globals via ``monkeypatch`` keeps the install scoped: teardown restores the original
    (unset) state, so the ``init_tracing`` tests below still see a free slot.
    """
    from opentelemetry import trace as trace_api
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    from opentelemetry.util._once import Once

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))

    monkeypatch.setattr(trace_api, "_TRACER_PROVIDER_SET_ONCE", Once())
    monkeypatch.setattr(trace_api, "_TRACER_PROVIDER", None)
    trace_api.set_tracer_provider(provider)

    yield exporter


def _instrumented_backend(name: str) -> FastMCP:
    """A bundle whose tool body instruments itself the way ``docs/extending.md`` tells it to."""
    mcp = FastMCP(name)

    @mcp.tool()
    async def enrich(kind: str) -> str:
        with span("acme.widget.enrich", {"acme.widget.kind": kind}) as current:
            current.set_attribute("acme.widget.count", 2)
        return "enriched"

    return mcp


def test_a_bundle_span_lands_in_the_same_trace_as_the_dispatch_span(alice, global_tracer_provider):
    """The whole claim ``span()`` makes: a bundle's span joins the dispatch's trace, with no plumbing.

    Not asserted as *direct* parentage — FastMCP's own protocol and mount-delegation spans sit in
    between, which is why ``docs/extending.md`` tells authors to filter on their own name prefix
    rather than on parentage. Sharing a trace id is the property that actually matters.
    """
    parent = make_parent("kx-mcp")
    parent.add_middleware(TracingMiddleware())  # resolves the same global provider as span()
    mount_bundle(parent, lambda: _instrumented_backend("acme"), namespace="acme")

    async def go():
        async with Client(parent) as client:
            await client.call_tool("acme_enrich", {"kind": "gadget"})

    asyncio.run(go())

    finished = global_tracer_provider.get_finished_spans()
    by_name = {s.name: s for s in finished}
    assert "acme.widget.enrich" in by_name, sorted(by_name)
    assert "mcp.tool_invoke" in by_name, sorted(by_name)

    bundle_span, dispatch_span = by_name["acme.widget.enrich"], by_name["mcp.tool_invoke"]
    assert bundle_span.context.trace_id == dispatch_span.context.trace_id
    assert bundle_span.parent is not None  # nested, not a disconnected root trace


def test_span_sets_attributes_at_start_so_a_sampler_can_see_them(global_tracer_provider):
    """Attributes passed to ``span()`` are on the span from the outset, not bolted on afterwards.

    This is one of the reasons ``span()`` returns OpenTelemetry's own context manager rather than
    wrapping it: a sampler consulted at span start sees only what was passed to ``start_as_current_span``.
    """
    with span("acme.widget.enrich", {"acme.widget.kind": "gadget"}) as current:
        current.set_attribute("acme.widget.count", 7)

    (recorded,) = global_tracer_provider.get_finished_spans()
    assert recorded.attributes["acme.widget.kind"] == "gadget"
    assert recorded.attributes["acme.widget.count"] == 7


def test_span_records_an_escaping_exception_once_on_its_own_span(global_tracer_provider):
    """OpenTelemetry's defaults apply: the exception propagates and is recorded on *this* span.

    One event per span, not a duplicate on one span — the middleware turns these defaults off on the
    dispatch span precisely so it can record there itself.
    """
    with pytest.raises(RuntimeError, match="kaboom"):
        with span("acme.widget.enrich"):
            raise RuntimeError("kaboom")

    (recorded,) = global_tracer_provider.get_finished_spans()
    assert [event.name for event in recorded.events] == ["exception"]
    assert recorded.status.status_code.name == "ERROR"


@pytest.fixture
def no_tracer_provider(monkeypatch):
    """Guarantee an *empty* global provider slot — the tracing-off posture.

    Cannot be left to chance: ``test_init_tracing_sets_up_the_exporter_once`` really does call
    ``trace.set_tracer_provider``, which is a process-global one-way door, so by the time this module
    reaches the tests below a provider may already be installed and "tracing off" would silently
    become "tracing on". Clearing both module globals (restored by monkeypatch) makes the posture
    explicit and order-independent.
    """
    from opentelemetry import trace as trace_api
    from opentelemetry.util._once import Once

    monkeypatch.setattr(trace_api, "_TRACER_PROVIDER_SET_ONCE", Once())
    monkeypatch.setattr(trace_api, "_TRACER_PROVIDER", None)


def test_span_is_inert_with_no_tracer_provider_installed(no_tracer_provider):
    """Tracing off -> a no-op span, so a tool body never checks whether tracing is enabled."""
    with span("acme.widget.enrich", {"acme.widget.kind": "gadget"}) as current:
        current.set_attribute("acme.widget.count", 2)  # must not raise
        assert current.is_recording() is False


def test_span_does_not_swallow_the_body_exception():
    with pytest.raises(RuntimeError, match="kaboom"):
        with span("acme.widget.enrich"):
            raise RuntimeError("kaboom")


def test_span_is_not_a_contextlib_wrapper():
    """Pins the shape, because getting it wrong fails silently rather than loudly.

    ``start_as_current_span`` returns an ``_AgnosticContextManager``, whose one behavioural difference
    from ``contextlib.contextmanager`` is that decorating an ``async def`` ends the span when the
    coroutine completes. A plain ``@contextmanager`` wrapper is decorator-capable too, but ends the
    span as soon as the coroutine object is constructed — every span 0ms, no error, nothing to notice.
    Returning OpenTelemetry's own object is what avoids that, so assert we still are.
    """
    from opentelemetry.util._decorator import _AgnosticContextManager

    assert isinstance(span("acme.probe"), _AgnosticContextManager)


@pytest.mark.parametrize(
    "endpoint, expected",
    [
        ("http://collector:4318", ("collector", 4318)),
        ("https://collector", ("collector", 443)),
        ("http://collector", ("collector", 80)),
        ("collector:4317", ("collector", 4317)),
        ("collector", ("collector", 4317)),
    ],
)
def test_probe_target_handles_http_and_grpc_forms(endpoint, expected):
    host, port, _ = tracing_mod._probe_target(endpoint)
    assert (host, port) == expected


def test_probe_target_defaults_when_unset(monkeypatch):
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", raising=False)
    assert tracing_mod._probe_target(None)[:2] == ("localhost", 4317)



def test_probe_target_display_carries_no_credentials():
    """The warning quotes the endpoint; userinfo, path and query (where secrets sit) must not leak."""
    _, _, shown = tracing_mod._probe_target("http://user:hunter2@collector:4318/v1/traces?token=abc")
    assert shown == "http://collector:4318"
