"""An add-in that instruments itself — the runnable version of the extender guide's observability section.

Shows the two things a bundle can add with no plumbing at all (see
[`docs/extending.md`](../../../../../../docs/extending.md) § Observability for bundle authors):

* a **module-level counter** which lands in the container's own `/metrics` scrape, because both sit
  on the same process-global registry; and
* a **child span** which joins the container's per-dispatch trace automatically, since
  ``TracingMiddleware`` opens that span with ``start_as_current_span`` and ``contextvars`` carries it.

Both come from ``kx_mcp_core``, so this bundle imports neither ``prometheus_client`` nor
``opentelemetry`` and declares nothing beyond ``kx-mcp-core`` — which metrics or tracing library the
container uses is the container's business, and it has changed once already.

Both are also **inert, not broken, when observability is off** — the demo ships with `KX_MCP_METRICS`
and `KX_MCP_TRACING` unset, so this tool runs identically either way: the counter increments in-process
with nothing scraping it, and ``span`` yields a no-op span when no tracer provider is installed. That
is why neither code path needs an "is it enabled?" check. Note that is a statement about the seams
being *switched off*, not about the libraries being *absent*: the container declares what it imports,
so absence is not a case an add-in has to handle.

Keeps the ``*_impl`` / ``@tool`` split of the sibling add-ins, so a test can call
``enrich_widget_impl(kind, config=...)`` directly — instrument the ``_impl``, never the wrapper.

Its unknown-kind path uses ``error_result`` rather than the sibling's ``tool_result``: this tool's
success payload carries no ``status`` field, so there is nothing to inspect and the flag is set
directly. See ``docs/extending.md`` § Signalling failure.
"""

from __future__ import annotations

from fastmcp import Context
from fastmcp.tools import tool

from kx_mcp_core import error_result, span
from kx_mcp_core.observability import Counter, metric

from ..settings import AcmeConfig
from ..utils.acme import config_from_ctx, get_connection

# Module level on purpose — the documented exception to the instance-safety rule, because Prometheus's
# registry is process-global by design: the per-mount dimension rides on the `kind` LABEL, not on a
# separate collector per mount. Constructing a collector registers it as a side effect, and a
# duplicate name raises — so `metric` recovers the already-registered collector when this module is
# imported twice. The name below is the name that appears in the scrape, and the only place it is
# written.
WIDGETS_ENRICHED = metric(
    Counter,
    "acme_widgets_enriched_total",
    "Widgets enriched by the acme bundle, by kind.",
    ["kind"],
)


def enrich_widget_impl(kind: str, config: AcmeConfig | None = None):
    """"Enrich" the widgets of one kind, counting the call and tracing the work.

    The enrichment itself is a deliberate one-line placeholder — the point of this add-in is the
    metrics/tracing wiring around it, not widget logic.
    """
    conn = get_connection(config or AcmeConfig())
    if not conn.known(kind):
        # This tool's success payload carries no `status` field, so there is nothing for
        # `tool_result` to inspect — `error_result` is the primitive for exactly that case: it flags
        # the dispatch `isError: true` and keeps whatever shape the payload already has. Nothing is
        # counted or traced for the enrichment, because none happened.
        return error_result(
            {"endpoint": conn.endpoint, "kind": kind, "message": f"unknown widget kind {kind!r}."}
        )
    # Attributes are namespaced `acme.*` — never `mcp.*`, which is the boundary middleware's own
    # vocabulary — and carry only shape/outcome, never row contents or caller identity. Passing them
    # to `span()` sets them at span start, where a sampler can still see them.
    with span("acme.widget.enrich", {"acme.widget.kind": kind}) as current:
        rows = conn.query(kind)["rows"]
        enriched = [{**row, "enriched": True} for row in rows]  # the placeholder "enrichment"
        current.set_attribute("acme.widget.count", len(enriched))

    WIDGETS_ENRICHED.labels(kind=kind).inc()
    return {"endpoint": conn.endpoint, "kind": kind, "count": len(enriched), "widgets": enriched}


@tool(annotations={"readOnlyHint": True})
def enrich_widget(kind: str, ctx: Context) -> dict:
    """Enrich Acme widgets of the given kind, demonstrating bundle-owned metrics and tracing.

    Increments this bundle's own `acme_widgets_enriched_total` counter (visible in the container's
    `/metrics` scrape when `KX_MCP_METRICS=prometheus`) and opens an `acme.widget.enrich` span in the
    same trace as the container's per-dispatch span (when `KX_MCP_TRACING=otlp`). Both are inert when
    those are unset, which is the demo's default.
    """
    return enrich_widget_impl(kind, config=config_from_ctx(ctx))
