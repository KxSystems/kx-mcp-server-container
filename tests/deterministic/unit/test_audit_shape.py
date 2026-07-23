"""The audit dispatch line has one uniform shape across every backend.

The audit line is emitted by a single parent-level ``AuditMiddleware`` (``make_parent`` /
``assembly.py``) that every mounted backend's dispatch flows through — so the shape is inherently
backend-agnostic. Real backends need licenses or live servers and can't run
here; instead we mount three tiny fake bundles at those namespaces via ``mount_bundle`` — valid
because the middleware doesn't know or care what a namespace's tool actually does.

"Which-backend" is not a structured field on the audit line — only inferable from the ``target``
prefix the namespaced mount produces (``kdbx_...`` / ``kdbai_...`` / ``acme_...``). This test locks
in that current shape.

Scope note: this covers the *dispatch*-audit line only (``action=tool_invoke`` etc., from
``AuditMiddleware``). The outbound ``exchange()`` seam emits a differently-shaped
``audit exchange strategy=...`` line (see ``kx_auth_core.outbound`` + ``test_outbound.py``); the two
vocabularies are not yet reconciled, which is out of scope here.
"""

from __future__ import annotations

import asyncio
import importlib
import logging

import pytest
from fastmcp import Client, FastMCP

from kx_mcp_core import make_parent, mount_bundle
from kx_mcp_core.auth import (
    AuthzSettings,
    authorize,
    configure_authz,
    current_authz_decision,
    register_authz_adapter,
)

# The package attribute `kx_mcp_core.auth.authorize` is the *function* (re-exported), shadowing the
# submodule (see test_authorize.py) — so fetch the real module object to monkeypatch its
# `current_principal` lookup.
authorize_mod = importlib.import_module("kx_mcp_core.auth.authorize")

BACKEND_NAMESPACES = ["kdbx", "kdbai", "acme"]


def _parse_audit(line: str) -> dict:
    """Split an ``audit key=value ...`` line into a dict. Fields are whitespace-separated
    ``key=value`` tokens; values never contain spaces in this codebase's format strings."""
    assert line.startswith("audit "), line
    fields: dict[str, str] = {}
    for token in line[len("audit "):].split():
        key, _, value = token.partition("=")
        fields[key] = value
    return fields


def _read_backend(name: str) -> FastMCP:
    """A minimal one-tool bundle standing in for a mounted backend extension."""
    mcp = FastMCP(name)

    @mcp.tool()
    def read() -> str:
        return "ok"

    return mcp


def _gated_backend(name: str, action: str, resource: str) -> FastMCP:
    """A one-tool bundle whose tool carries an ``@authorize`` capability check."""
    mcp = FastMCP(name)

    @mcp.tool()
    @authorize(action=action, resource=resource)
    async def gated() -> str:
        return "did it"

    return mcp


@pytest.fixture(autouse=True)
def _reset_authz():
    """Isolate the module-global authz settings + decision contextvar between tests (mirrors
    tests/deterministic/unit/test_authorize.py's ``_reset_authz`` fixture)."""
    authorize_mod._SETTINGS = None
    current_authz_decision.set(None)
    yield
    authorize_mod._SETTINGS = None


@pytest.fixture
def alice(monkeypatch):
    """current_principal() returns a fake authenticated principal named 'alice'."""

    class _FakePrincipal:
        client_id = "alice"
        claims: dict = {}

    monkeypatch.setattr(
        "kx_mcp_core.auth.audit.current_principal", lambda: _FakePrincipal()
    )
    monkeypatch.setattr(authorize_mod, "current_principal", lambda: _FakePrincipal())


def _dispatch(parent: FastMCP, tool_name: str) -> None:
    async def go():
        async with Client(parent) as client:
            await client.call_tool(tool_name, {})

    asyncio.run(go())


def _audit_lines(caplog) -> list[str]:
    return [r.message for r in caplog.records if r.name == "kx_mcp.audit"]


# --- cross-backend uniform shape -----------------------------------------------------------------


@pytest.mark.parametrize("namespace", BACKEND_NAMESPACES)
def test_dispatch_audit_shape_uniform_across_backends(namespace, alice, caplog):
    """One dispatch on any of the three simulated backends yields one line carrying
    subject/action/target(→backend)/outcome, with the backend consistently derivable from the
    target prefix."""
    parent = make_parent("kx-mcp")
    mount_bundle(parent, lambda: _read_backend(namespace), namespace=namespace)

    with caplog.at_level(logging.INFO, logger="kx_mcp.audit"):
        _dispatch(parent, f"{namespace}_read")

    lines = [_parse_audit(m) for m in _audit_lines(caplog) if m.startswith("audit ")]
    assert len(lines) == 1, lines
    fields = lines[0]

    assert fields["subject"] == "alice"
    assert fields["action"] == "tool_invoke"
    assert fields["target"] == f"{namespace}_read"
    assert fields["outcome"] == "ok"
    # "which-backend" today = the target's namespace prefix, not a structured field.
    assert fields["target"].split("_", 1)[0] == namespace


def test_dispatch_audit_shape_distinguishes_backends_in_one_run(alice, caplog):
    """Mounting all three simulated backends on one parent and dispatching one tool on each
    produces three lines, each correctly attributing its own backend via the target prefix."""
    parent = make_parent("kx-mcp")
    for namespace in BACKEND_NAMESPACES:
        mount_bundle(parent, lambda ns=namespace: _read_backend(ns), namespace=namespace)

    with caplog.at_level(logging.INFO, logger="kx_mcp.audit"):
        for namespace in BACKEND_NAMESPACES:
            _dispatch(parent, f"{namespace}_read")

    lines = [_parse_audit(m) for m in _audit_lines(caplog) if m.startswith("audit ")]
    assert len(lines) == len(BACKEND_NAMESPACES), lines
    seen_backends = {fields["target"].split("_", 1)[0] for fields in lines}
    assert seen_backends == set(BACKEND_NAMESPACES)
    for fields in lines:
        assert fields["subject"] == "alice"
        assert fields["action"] == "tool_invoke"
        assert fields["outcome"] == "ok"


# --- the decision leg -----------------------------------------------------------------------------


def test_dispatch_audit_shape_carries_allow_decision_and_adapter(alice, caplog):
    """A capability-gated tool that's allowed enriches the line with decision=allow + the adapter name."""
    register_authz_adapter("fake_allow_shape_test", lambda request: True)
    configure_authz(AuthzSettings(mode="fake_allow_shape_test"))

    parent = make_parent("kx-mcp")
    mount_bundle(
        parent,
        lambda: _gated_backend("kdbx", action="read", resource="kdbx:sql"),
        namespace="kdbx",
    )

    with caplog.at_level(logging.INFO, logger="kx_mcp.audit"):
        _dispatch(parent, "kdbx_gated")

    lines = [_parse_audit(m) for m in _audit_lines(caplog) if m.startswith("audit ")]
    assert len(lines) == 1, lines
    fields = lines[0]

    assert fields["subject"] == "alice"
    assert fields["target"] == "kdbx_gated"
    assert fields["outcome"] == "ok"
    assert fields["decision"] == "allow"
    assert fields["adapter"] == "fake_allow_shape_test"


def test_dispatch_audit_shape_carries_deny_decision_and_adapter(alice, caplog):
    """A capability-gated tool that's denied logs outcome=denied + decision=deny + the adapter name —
    the denial never reaches the tool body, and the container never crashes."""
    register_authz_adapter("fake_deny_shape_test", lambda request: False)
    configure_authz(AuthzSettings(mode="fake_deny_shape_test"))

    parent = make_parent("kx-mcp")
    mount_bundle(
        parent,
        lambda: _gated_backend("kdbai", action="write", resource="kdbai:table"),
        namespace="kdbai",
    )

    async def go():
        async with Client(parent) as client:
            with pytest.raises(Exception):
                await client.call_tool("kdbai_gated", {})

    with caplog.at_level(logging.INFO, logger="kx_mcp.audit"):
        asyncio.run(go())

    lines = [_parse_audit(m) for m in _audit_lines(caplog) if m.startswith("audit ")]
    assert len(lines) == 1, lines
    fields = lines[0]

    assert fields["subject"] == "alice"
    assert fields["target"] == "kdbai_gated"
    assert fields["outcome"] == "denied"
    assert fields["decision"] == "deny"
    assert fields["adapter"] == "fake_deny_shape_test"
