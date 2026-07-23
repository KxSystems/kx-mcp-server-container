"""Assembly seam: build a parent FastMCP server and mount backend bundles onto it.

This is the entirety of "the container" in the composition approach — FastMCP itself is the
container; this module is just the thin glue + the seam where the control plane attaches. Keeping
`FastMCP(...)` behind `make_parent()` means the glue and launcher never construct the parent
directly, so auth and middleware have exactly one place to plug in: inbound auth (the `KX_MCP_AUTH`
verifier) and the audit middleware both attach here.
"""

from __future__ import annotations

import importlib
import logging
from typing import Callable, Optional

from fastmcp import FastMCP
from fastmcp.server.auth import AuthProvider

from .auth import AuditMiddleware

logger = logging.getLogger(__name__)

# Convention: a bundle named "<name>" is the importable package "kx_mcp_<name>" exposing
# a module-level `build_server() -> FastMCP`. (kdbx -> kx_mcp_kdbx, example -> kx_mcp_example)
BUNDLE_PACKAGE_PREFIX = "kx_mcp_"


def make_parent(
    name: str = "kx-mcp",
    *,
    auth: Optional[AuthProvider] = None,
    audit: bool = True,
) -> FastMCP:
    """Create the container's parent FastMCP server — the single control-plane attach point.

    ``auth`` is the resolved inbound-auth provider (``None`` = no auth, the single-principal
    bundling posture); FastMCP 3.x parent auth guards all mounted subservers. Resolve it from
    ``KX_MCP_AUTH`` with :func:`kx_mcp_core.build_auth_provider`. ``audit`` attaches the
    who/what/outcome :class:`~kx_mcp_core.auth.AuditMiddleware`.
    """
    parent = FastMCP(name, auth=auth)
    if audit:
        parent.add_middleware(AuditMiddleware())
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
    parent.mount(build_server(), namespace=namespace)


def try_mount_bundle(parent: FastMCP, build_server: Callable[..., FastMCP], namespace: str) -> bool:
    """Mount a bundle, disabling only *that* backend if its pre-flight fails.

    A bundle's ``build_server()`` runs an eager connectivity pre-flight and ``sys.exit(1)``s
    (raising ``SystemExit``) when its backend is unreachable. In a multi-backend container "never
    crash the container" means catching that, serving the backends that *did* come up, and
    surfacing a clear warning for the one that didn't.

    Returns ``True`` if the bundle mounted, ``False`` if it was skipped.
    """
    try:
        parent.mount(build_server(), namespace=namespace)
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
