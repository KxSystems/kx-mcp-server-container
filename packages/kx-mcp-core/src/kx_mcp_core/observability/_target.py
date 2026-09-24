"""Read the dispatch target off a ``MiddlewareContext`` — shared by the metrics + tracing middleware.

The two accessors mirror the ones in :mod:`kx_mcp_core.auth.audit`, which is the reference for how a
FastMCP dispatch names its target: tools and prompts carry ``message.name``, resources carry
``message.uri``. Kept private and local to the observability package: metrics and tracing genuinely
share them, while ``audit.py`` keeps its own copy so the reference implementation stays
self-contained and readable on its own.

``getattr`` chains rather than attribute access, so a hook whose message shape lacks the field
degrades to ``"?"`` instead of raising inside middleware.
"""

from __future__ import annotations

from fastmcp.server.middleware import MiddlewareContext

UNKNOWN_TARGET = "?"


def target_name(context: MiddlewareContext) -> str:
    """The dispatched tool's or prompt's name (already namespaced by the mount)."""
    return getattr(getattr(context, "message", None), "name", UNKNOWN_TARGET)


def target_uri(context: MiddlewareContext) -> str:
    """The dispatched resource's URI (already namespaced by the mount)."""
    return str(getattr(getattr(context, "message", None), "uri", UNKNOWN_TARGET))
