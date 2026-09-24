"""Proves the structural thesis: bundles compose under a FastMCP 3.x parent with namespacing.

Deliberately uses the dependency-free `example` bundle + an inline stub so this runs without a
PyKX license or a live KDB-X. The kdbx bundle's own composition is exercised by the over-the-wire
smoke step (see PROPOSAL.md / plan verification) and its behaviour by the carried-over unit suite.
"""

import asyncio

from fastmcp import FastMCP

from kx_mcp_core import (
    load_build_server,
    make_parent,
    mount_bundle,
    try_mount_bundle,
)
from kx_mcp_example import build_server as example


def _tool_names(parent: FastMCP) -> set[str]:
    return {t.name for t in asyncio.run(parent.list_tools())}


def test_single_bundle_is_namespaced():
    parent = make_parent("test")
    mount_bundle(parent, example, namespace="ex")
    assert "ex_echo" in _tool_names(parent)


def test_multi_backend_recomposition_without_collision():
    """Two independent bundles compose, each under its own namespace, no name clash."""
    parent = make_parent("test")
    mount_bundle(parent, example, namespace="ex")

    stub = FastMCP("stub")

    @stub.tool()
    def echo(text: str) -> str:  # same bare name as example's tool — must not collide
        return text.upper()

    parent.mount(stub, namespace="other")

    names = _tool_names(parent)
    assert "ex_echo" in names
    assert "other_echo" in names  # namespacing keeps the identically-named tools distinct


def test_load_build_server_resolves_bundle_by_package():
    build_server = load_build_server("kx_mcp_example")
    assert build_server is example
    assert isinstance(build_server(), FastMCP)


# --- Graceful degradation: never crash the container ---------------------------------------
# A backend's build_server() runs an eager pre-flight and sys.exit(1)s (SystemExit) when its
# database is unreachable. try_mount_bundle must disable only that backend and serve the rest.


def _exits(*_args, **_kwargs):
    """Stand-in for a bundle whose connectivity pre-flight fails and calls sys.exit(1)."""
    raise SystemExit(1)


def _raises(*_args, **_kwargs):
    """Stand-in for a bundle whose build_server() fails for a non-exit reason."""
    raise RuntimeError("boom")


def test_try_mount_skips_backend_that_exits_and_keeps_the_rest():
    """kdb-x-only scenario: one backend up, the other's pre-flight sys.exit(1)s — container serves."""
    parent = make_parent("test")
    assert try_mount_bundle(parent, example, namespace="ex") is True
    assert try_mount_bundle(parent, _exits, namespace="down") is False

    names = _tool_names(parent)
    assert "ex_echo" in names  # the reachable backend is served
    assert not any(n.startswith("down_") for n in names)  # the unreachable one is absent


def test_try_mount_skips_backend_that_raises():
    """A non-SystemExit build_server() failure is also contained, not propagated."""
    parent = make_parent("test")
    assert try_mount_bundle(parent, _raises, namespace="bad") is False
    assert _tool_names(parent) == set()


def test_try_mount_all_backends_down_yields_a_bare_but_live_parent():
    """No backend reachable: the parent still starts (bare), rather than crashing."""
    parent = make_parent("test")
    assert try_mount_bundle(parent, _exits, namespace="a") is False
    assert try_mount_bundle(parent, _raises, namespace="b") is False
    assert _tool_names(parent) == set()


def test_mount_rejects_a_non_fastmcp_return_value():
    """REGRESSION (see mcp-container/adversarial-review-2026-08.md § container/assembly seam).

    Neither mount_bundle nor try_mount_bundle validates that build_server() actually returned
    a FastMCP instance — a broken bundle returning None "mounts successfully" and only fails
    later, deep inside fastmcp internals, on first actual use."""
    parent = make_parent("test")
    assert try_mount_bundle(parent, lambda: None, namespace="bad") is False


def test_duplicate_namespace_mount_does_not_silently_duplicate_tools():
    """REGRESSION (see mcp-container/adversarial-review-2026-08.md § container/assembly seam).

    Mounting two bundles under the SAME namespace succeeds silently today (both calls return
    True) — list_tools() then returns duplicated tool names instead of a clear startup error at
    the point of the second mount."""
    parent = make_parent("test")
    assert try_mount_bundle(parent, example, namespace="dup") is True
    try_mount_bundle(parent, example, namespace="dup")

    names = [t.name for t in asyncio.run(parent.list_tools())]
    assert len(names) == len(set(names)), f"duplicate tool names: {names}"
