"""The failing bundle's entry point — stands in for a backend whose pre-flight can't connect."""

from __future__ import annotations

import logging
import sys

from fastmcp import FastMCP

logger = logging.getLogger(__name__)


def build_server() -> FastMCP:
    """Simulate an eager connectivity pre-flight that fails, and exit like the real bundles do.

    The shipped kdbx/kdbai bundles run a reachability check in ``build_server()`` and ``sys.exit(1)``
    when the backend is unreachable. This fixture reproduces that posture (log ERROR, then exit)
    with no real backend, so a test can prove the container degrades gracefully over the wire.
    """
    logger.error("backend pre-flight failed (simulated) — cannot reach backend")
    sys.exit(1)
