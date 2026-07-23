"""Thin, optional zero-code launcher: assemble a composition server from config and run it.

    kx-mcp --bundles kdbx,example
    KX_MCP_BUNDLES=kdbx,example kx-mcp

This is the convenience path for a simple CLI invocation or pointing an MCP client at a URL with no
extra glue code. It is built from the same `kx_mcp_core.assembly` seam as the hand-written glue in
`server.py` — nothing the launcher does is unavailable to a few lines of glue. Inbound auth is
resolved from `KX_MCP_AUTH`; it defaults to off (the single-principal bundling posture).
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Optional, Sequence

from fastmcp import FastMCP

from .assembly import (
    BUNDLE_PACKAGE_PREFIX,
    load_build_server,
    make_parent,
    try_mount_bundle,
)
from .auth import AuthSettings, build_auth_provider, configure_authz
from .logging import configure_logging


def _parse_bundles(raw: str | None) -> list[str]:
    return [b.strip() for b in raw.split(",")] if raw else []


def build_app(
    bundles: Sequence[str],
    name: str = "kx-mcp",
    auth_settings: Optional[AuthSettings] = None,
) -> FastMCP:
    """Assemble a parent server with each named bundle mounted under its own namespace.

    Inbound auth is resolved from ``auth_settings`` (read from ``KX_MCP_AUTH*`` env by default) and
    passed to the parent, so it guards every mounted backend. Mounts via ``try_mount_bundle`` so an
    unreachable backend is disabled with a warning rather than taking the whole container down — the
    zero-code path honours the same "never crash the container" invariant as the hand-written glue
    in ``server.py``.

    NOTE: importing a bundle may run its own settings/CLI parsing at import time, so callers that
    have their own CLI flags should clear ``sys.argv`` before calling this (the launcher does).
    """
    parent = make_parent(name, auth=build_auth_provider(auth_settings or AuthSettings()))
    for short_name in bundles:
        build_server = load_build_server(f"{BUNDLE_PACKAGE_PREFIX}{short_name}")
        try_mount_bundle(parent, build_server, namespace=short_name)
    return parent


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="kx-mcp",
        description="Assemble and run a kx-mcp composition server from a set of backend bundles.",
    )
    parser.add_argument(
        "--bundles",
        help="Comma-separated bundle names to mount, e.g. 'kdbx,example'. "
        "Defaults to $KX_MCP_BUNDLES.",
    )
    parser.add_argument("--name", default=os.environ.get("KX_MCP_NAME", "kx-mcp"))
    parser.add_argument(
        "--transport",
        default=os.environ.get("KX_MCP_TRANSPORT", "streamable-http"),
        # No "sse": the HTTP+SSE transport is deprecated in the MCP spec (superseded by
        # streamable-http), so the container does not offer it.
        choices=["stdio", "streamable-http", "http"],
    )
    parser.add_argument("--host", default=os.environ.get("KX_MCP_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("KX_MCP_PORT", "8000")))
    parser.add_argument(
        "--log-level",
        default=os.environ.get("KX_MCP_LOG_LEVEL", "INFO"),
        help="Level for the container's own logs — the audit line and mount warnings. "
        "Defaults to $KX_MCP_LOG_LEVEL or INFO.",
    )
    args = parser.parse_args(argv)

    # Configure the container's own logging before assembly so audit lines and any mount-time
    # warnings are visible (the launcher is an entry point; the library seam stays logging-free).
    configure_logging(args.log_level)

    # Resolve the capability-check config now so a bad KX_MCP_AUTHZ policy file fails loudly at
    # startup (a clean operator error) rather than as a silent per-request deny. No-op when
    # KX_MCP_AUTHZ is unset (route-only). The decorator falls back to a lazy read if this is
    # ever skipped (e.g. glue).
    configure_authz()

    bundles = _parse_bundles(args.bundles or os.environ.get("KX_MCP_BUNDLES"))
    if not bundles:
        parser.error("no bundles selected — pass --bundles or set KX_MCP_BUNDLES")

    # Bundles may CLI-parse their own settings at import; don't let them see our flags.
    sys.argv = [sys.argv[0]]

    app = build_app(bundles, name=args.name)
    if args.transport == "stdio":
        app.run(transport="stdio")
    else:
        app.run(transport=args.transport, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
