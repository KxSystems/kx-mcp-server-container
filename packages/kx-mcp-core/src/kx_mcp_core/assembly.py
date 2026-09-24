"""Assembly seam: build a parent FastMCP server and mount backend bundles onto it.

This is the entirety of "the container" in the composition approach — FastMCP itself is the
container; this module is just the thin glue + the seam where the control plane attaches. Keeping
`FastMCP(...)` behind `make_parent()` means the glue and launcher never construct the parent
directly, so auth and middleware have exactly one place to plug in: inbound auth (the `KX_MCP_AUTH`
verifier), the audit middleware, the observability middleware (`KX_MCP_METRICS` /
`KX_MCP_TRACING`), and the `/health` liveness route all attach here.
"""

from __future__ import annotations

import importlib
import logging
from typing import Callable, Optional

from fastmcp import FastMCP
from fastmcp.server.auth import AuthProvider
from starlette.requests import Request
from starlette.responses import PlainTextResponse

from .auth import AuditMiddleware
from .observability import ObservabilitySettings

logger = logging.getLogger(__name__)

# Convention: a bundle named "<name>" is the importable package "kx_mcp_<name>" exposing
# a module-level `build_server() -> FastMCP`. (kdbx -> kx_mcp_kdbx, example -> kx_mcp_example)
BUNDLE_PACKAGE_PREFIX = "kx_mcp_"

# The container's liveness path. A bundle is mount-only, so the parent owns it: FastMCP forwards a
# mounted child's custom_route up to the parent app, and the parent wins a path collision.
HEALTH_PATH = "/health"


def _attach_health_route(parent: FastMCP, path: str = HEALTH_PATH) -> None:
    """Register the container's liveness probe — a bare 200, unauthenticated.

    Unauthenticated by construction: FastMCP guards the MCP endpoint by wrapping *that route's*
    endpoint in ``RequireAuthMiddleware``, while an auth provider's app-wide middleware only
    populates the auth context. So the probe answers under every ``KX_MCP_AUTH`` mode — required,
    since a kubelet has no identity to present.

    Liveness, not readiness: ``make_parent`` runs before any bundle is mounted, so it cannot report
    which backends came up.
    """

    @parent.custom_route(path, methods=["GET"])
    async def health(_request: Request) -> PlainTextResponse:
        return PlainTextResponse("OK")


def make_parent(
    name: str = "kx-mcp",
    *,
    auth: Optional[AuthProvider] = None,
    audit: bool = True,
    observability: Optional[ObservabilitySettings] = None,
    health: bool = True,
) -> FastMCP:
    """Create the container's parent FastMCP server — the single control-plane attach point.

    ``auth`` is the resolved inbound-auth provider (``None`` = no auth, the single-principal
    bundling posture); FastMCP 3.x parent auth guards all mounted subservers. Resolve it from
    ``KX_MCP_AUTH`` with :func:`kx_mcp_core.build_auth_provider`. ``audit`` attaches the
    who/what/outcome :class:`~kx_mcp_core.auth.AuditMiddleware`. ``health`` serves the
    unauthenticated ``GET /health`` liveness probe (see :func:`_attach_health_route`), so every
    composition — launcher, glue, or a downstream assembler's own server — has one. Inert under
    ``stdio``, which serves no HTTP routes.

    ``observability`` is the resolved :class:`~kx_mcp_core.observability.ObservabilitySettings`
    (``None`` = off, the default posture — read it from the environment with
    ``ObservabilitySettings()``). **One** object drives every seam, each attaching its middleware only
    when its own selector is on; a single parameter is deliberate, since separate per-seam parameters
    only ever received the same object and made mismatched settings possible for no benefit. Because
    they attach to the *parent*, they observe every mounted backend with no per-backend wiring.
    Tracing also installs the ``TracerProvider`` here (idempotent), so hand-written glue gets it
    without a second call.

    Note the scrape endpoint is **not** mounted here: it needs the transport, which only the caller
    knows — see :func:`kx_mcp_core.observability.mount_metrics_route`.

    **Middleware attach order matters.** Audit goes on first, so it is the *outermost* wrapper and the
    observability middleware run inside its scope. Audit resets the
    authz decision slot in its own ``finally``; if metrics/tracing ran outside audit
    they would read that slot after the reset and record every capability denial as a plain error.
    """
    parent = FastMCP(name, auth=auth)
    if audit:
        parent.add_middleware(AuditMiddleware())

    # Attached after audit on purpose — see the ordering note above.
    if observability is not None and observability.tracing_enabled:
        from .observability import TracingMiddleware, init_tracing

        # Gated on the real outcome, not just the selector: init_tracing() returns False when setup
        # failed (the `tracing` extra missing, an exporter construction error, …), and attaching the
        # middleware anyway would mean every dispatch pays a middleware frame + two lazy
        # opentelemetry imports for zero spans — and would rest the "never crash" invariant entirely
        # on opentelemetry-api continuing to arrive transitively via fastmcp.
        if init_tracing(observability):
            parent.add_middleware(TracingMiddleware())

    if observability is not None and observability.metrics_enabled:
        from .observability import MetricsMiddleware, set_build_info

        parent.add_middleware(MetricsMiddleware())
        # Build identity is a startup fact, not a per-dispatch one, and needs no event loop — so it
        # is published here rather than from the middleware. The event-loop sampler cannot start
        # here (no loop yet) and starts on the first dispatch instead.
        set_build_info()

    # A route, not middleware, so it is order-independent with respect to the block above.
    if health:
        _attach_health_route(parent)

    return parent


def load_build_server(package: str) -> Callable[..., FastMCP]:
    """Resolve a bundle package name to its ``build_server`` callable via import."""
    module = importlib.import_module(package)
    try:
        return getattr(module, "build_server")
    except AttributeError as exc:  # pragma: no cover - defensive, clear operator error
        raise AttributeError(
            f"bundle '{package}' does not expose build_server() — it is not a kx-mcp bundle"
        ) from exc


def mount_bundle(parent: FastMCP, build_server: Callable[..., FastMCP], namespace: str) -> None:
    """Mount a bundle's server under ``namespace`` (3.x Provider + Namespace Transform).

    Tools become ``{namespace}_{tool}``; resource URIs and prompts are namespaced likewise.

    This is the *strict* mount: a backend whose ``build_server()`` pre-flight fails propagates
    (including the bundle's ``sys.exit(1)`` as ``SystemExit``). Use :func:`try_mount_bundle` for
    the container's "never crash the container" posture, which disables only the bad backend.
    """
    mounted: set = getattr(parent, "_kx_mounted_namespaces", None) or set()
    if namespace in mounted:
        # fastmcp does not object to two mounts under one prefix; `list_tools()` just returns
        # DUPLICATED tool names, which is an MCP protocol violation the *client* discovers rather
        # than an error the operator sees at startup. State lives on the parent, not in a module
        # global, so two parents in one process stay independent (the instance-safety convention).
        raise ValueError(
            f"namespace '{namespace}' is already mounted on this parent — mounting twice under one "
            "namespace duplicates every tool name"
        )

    server = build_server()
    if not isinstance(server, FastMCP):
        # `Callable[..., FastMCP]` is an annotation, not a check, and `load_build_server` only
        # verifies the attribute EXISTS. A bundle whose build_server returns None (an early return, a
        # forgotten `return`) used to "mount successfully" and then fail deep inside fastmcp on first
        # actual use, where the message names neither the bundle nor the real cause. Fail here, where
        # the bundle can be named — try_mount_bundle then reports it as an ordinary mount failure.
        raise TypeError(
            f"bundle '{namespace}' build_server() returned {type(server).__name__}, not a FastMCP "
            "instance — it does not satisfy the extension contract"
        )
    parent.mount(server, namespace=namespace)
    mounted.add(namespace)
    parent._kx_mounted_namespaces = mounted  # type: ignore[attr-defined]


def try_mount_bundle(parent: FastMCP, build_server: Callable[..., FastMCP], namespace: str) -> bool:
    """Mount a bundle, disabling only *that* backend if its pre-flight fails.

    A bundle's ``build_server()`` runs an eager connectivity pre-flight and ``sys.exit(1)``s
    (raising ``SystemExit``) when its backend is unreachable. In a multi-backend container "never
    crash the container" means catching that, serving the backends that *did* come up, and
    surfacing a clear warning for the one that didn't.

    Returns ``True`` if the bundle mounted, ``False`` if it was skipped.
    """
    try:
        # Delegate rather than re-spell `parent.mount(...)`: the contract check (and the namespace
        # guard) live in mount_bundle, and duplicating the call here is how they came to apply to
        # only one of the two mount postures.
        mount_bundle(parent, build_server, namespace=namespace)
    except SystemExit as exc:  # bundle pre-flight failed its connectivity check and exited
        logger.warning(
            "backend '%s' unavailable — build_server() exited (%s); serving without it",
            namespace,
            exc.code,
        )
        return False
    except Exception as exc:  # any other build_server() failure must not take the container down
        logger.warning(
            "backend '%s' failed to mount (%s: %s); serving without it",
            namespace,
            type(exc).__name__,
            exc,
        )
        return False
    logger.info("backend '%s' mounted", namespace)
    return True

