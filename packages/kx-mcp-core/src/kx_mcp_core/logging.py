"""Container logging setup — surface the container's *own* logs (audit, mount warnings) when it runs.

The container is an application entry point, so it configures logging here — in the launcher's run
path and the ``server.py`` glue — **not** in the library seam ``make_parent`` (a library constructor
shouldn't reach for the root logger). Without this, the ``kx_mcp.audit`` line — emitted at INFO by the
audit middleware *and* the outbound exchange seam — reaches no handler and is silently dropped. That
is exactly why running via the ``kx-mcp`` launcher showed no audit, while ``server.py``'s historic
``logging.basicConfig`` did.

**Why a filtered root handler rather than per-tree handlers.** The container and its backend bundles are
separate distributions with *underscore* import names — ``kx_mcp_core``, ``kx_mcp_kdbx``,
``kx_mcp_kdbai`` — so their ``getLogger(__name__)`` loggers are unrelated *sibling* trees: ``kx_mcp_kdbai``
is **not** a child of ``kx_mcp`` (the hierarchy splits on dots). Attaching handlers to a fixed list of
trees therefore drops every bundle's pre-flight/auth line, and silently drops any *new* backend until
someone extends the list. Instead we install a single handler on the **root** logger gated by a filter
that admits only the ``kx_mcp*`` brand namespace (``kx_mcp`` and ``kx_mcp.*`` *and* ``kx_mcp_*.*``). One
self-maintaining rule: every current and future ``kx_mcp*`` logger surfaces, and third-party
(httpx / uvicorn) records — which propagate to root too — are rejected by the filter, so visible output
stays free of their noise. A root handler is appropriate *here* because this is an entry-point helper,
not the ``make_parent`` library seam the "never touch root" rule protects; the filter preserves that
rule's intent (no third-party noise).
"""

from __future__ import annotations

import logging

_BRAND = "kx_mcp"  # the shared import prefix: kx_mcp, kx_mcp.audit, kx_mcp_core.*, bundles, …
_OTEL = "opentelemetry"  # the SDK / exporter's own loggers; their export-failure warnings must surface
_HANDLER_TAG = "_kx_mcp_handler"  # marks our handler so configure_logging() is idempotent


class _BrandFilter(logging.Filter):
    """Admit only records from the ``kx_mcp*`` brand namespace, whatever the package's dot/underscore.

    Covers the dotted children (``kx_mcp.audit``) *and* the underscore sibling trees the backend
    bundles log under (``kx_mcp_kdbai.server`` etc.) — so the one root handler serves them all while
    third-party records propagating to root are dropped. The one exception is WARNING and above from
    ``opentelemetry.*``: with an unreachable collector the exporter's own retry/failure warnings are
    the only signal that spans are being lost, so hiding them leaves the operator blind.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if record.name == _BRAND or record.name.startswith(_BRAND):
            return True
        return record.levelno >= logging.WARNING and (
            record.name == _OTEL or record.name.startswith(_OTEL + ".")
        )


def configure_logging(level: str = "INFO") -> None:
    """Install one filtered stream handler on the root logger at ``level`` (idempotent).

    Defaults to ``INFO`` so the audit line is visible out of the box; override with the
    ``KX_MCP_LOG_LEVEL`` env var or the launcher's ``--log-level`` flag. Safe to call more than once
    (it won't stack handlers) and from either entry point.
    """
    resolved = logging.getLevelName(level.upper())
    if not isinstance(resolved, int):  # unknown name → getLevelName returns a str
        resolved = logging.INFO

    root = logging.getLogger()
    # The sibling trees' only ancestor is root, so root must accept records at ``resolved`` for them to
    # be created at all. Lower the threshold if needed, but never make root *less* verbose than it is.
    if root.level == logging.NOTSET or root.level > resolved:
        root.setLevel(resolved)

    if not any(getattr(h, _HANDLER_TAG, False) for h in root.handlers):
        handler = logging.StreamHandler()
        handler.setLevel(resolved)  # our handler emits at the requested level, even if root is more verbose
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
        handler.addFilter(_BrandFilter())  # only kx_mcp* surfaces; httpx/uvicorn propagate here but are dropped
        setattr(handler, _HANDLER_TAG, True)
        root.addHandler(handler)
