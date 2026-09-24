"""Instrumentation an *add-in* reaches for: :func:`span`.

The boundary middleware already counts, times and spans every dispatch with the namespace applied, so
a bundle instruments further only to say something about the *inside* of a tool that the boundary
cannot see. This module is what it calls to do that, so an add-in imports nothing from
``opentelemetry`` and declares nothing beyond ``kx-mcp-core``.

The metrics counterpart is :func:`kx_mcp_core.observability.metric` plus the collector classes
re-exported alongside it — see the package ``__init__``.

**No module-scope third-party imports here, deliberately.** Everything ``opentelemetry`` is reached
inside the function, which is what lets the SDK and OTLP exporter stay in the ``kx-mcp-core[tracing]``
extra, and what lets a test simulate their absence without reloading a module that touches
``prometheus_client``'s process-global registry.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional

# One tracer name for every bundle-authored span. The useful axis for filtering a trace is the span
# *name* (`acme.widget.enrich`), which the caller owns and namespaces by bundle; an instrumentation
# scope per add-in module would add a second, redundant one.
TRACER_NAME = "kx_mcp_core.bundle"


def span(name: str, attributes: Optional[Mapping[str, Any]] = None, **kwargs: Any) -> Any:
    """Open a child span beneath the container's per-dispatch span.

    ::

        from kx_mcp_core import span

        def enrich_widget_impl(kind, config=None):
            with span("acme.widget.enrich", {"acme.widget.kind": kind}) as current:
                ...
                current.set_attribute("acme.widget.count", len(rows))

    The dispatch span is already *current* when a tool body runs (``TracingMiddleware`` opens it with
    ``start_as_current_span``, and ``contextvars`` carries it across ``await``), so this nests with no
    plumbing — though not as its *direct* child, since FastMCP's own protocol and mount-delegation
    spans sit in between. Filter on your own name prefix, not on parentage.

    Inert when tracing is off: with no ``TracerProvider`` installed, OpenTelemetry hands back a no-op
    tracer whose spans discard everything. A tool never checks whether tracing is enabled.

    Name spans and attributes for your own backend (``acme.*``, ``kdbx.*``) — never ``mcp.*``, which
    is the boundary middleware's vocabulary. Attributes are a wire egress, so carry shape and outcome,
    never row contents or anything off ``current_principal()``.

    **Returns OpenTelemetry's own context manager rather than wrapping it**, which matters more than
    it looks. ``start_as_current_span`` is an ``_AgnosticContextManager``: used as a *decorator* on an
    ``async def`` it wraps the coroutine so the span ends when the coroutine does. A
    ``contextlib.contextmanager`` around it is silently decorator-capable too, but ends the span the
    moment the coroutine object is created — every span 0ms, no error, nothing to notice. Handing back
    the same object also means ``attributes`` are set *at span start*, where a sampler can see them,
    and that ``kind`` / ``links`` / ``end_on_exit`` keep working.

    Exceptions propagate, and OpenTelemetry's defaults record them: the escaping exception is recorded
    on **this** span and its status set to ERROR. That is one event per span, not a duplicate — the
    middleware turns those defaults *off* on the dispatch span precisely so it can record there once
    itself. If you are swallowing an error rather than re-raising, pass ``record_exception=False`` and
    record it yourself with whatever context you have.
    """
    from opentelemetry import trace

    return trace.get_tracer(TRACER_NAME).start_as_current_span(
        name, attributes=attributes, **kwargs
    )
