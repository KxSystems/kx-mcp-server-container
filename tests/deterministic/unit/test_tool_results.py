"""The tool-result contract: a failed dispatch must carry ``isError: true``.

Three layers, because each can regress independently:

1. **The helpers** (:mod:`kx_mcp_core.results`) — the shape they produce, and that a success is not
   wrapped.
2. **Over a real dispatch** — that ``isError`` survives the mount boundary and that the observability
   middleware classifies a returned failure as ``error`` (and a returned *denial* as ``denied``).
   This is the pairing that was broken before the contract existed: the payload said "error", the
   protocol said success, and the counter agreed with the protocol.
3. **The shipped bundles comply** — an AST sweep asserting every registered tool in kdbx/kdbai either
   routes its return through the helpers or raises, plus the ``async def`` requirement that makes an
   authz stamp survive to the middleware.
"""

from __future__ import annotations

import ast
import asyncio
import pathlib

import pytest
from fastmcp import Client, FastMCP
from fastmcp.tools import ToolResult
from kx_auth_core.authz import AuthzDecision
from kx_mcp_core import (
    FAILURE_STATUSES,
    STATUS_DENIED,
    STATUS_ERROR,
    STATUS_OK,
    ObservabilitySettings,
    error_result,
    make_parent,
    tool_result,
)
from kx_mcp_core.auth import begin_authz_dispatch, stamp_authz_decision
from prometheus_client import REGISTRY

COUNTER_NAME = "kx_mcp_dispatches_total"

# The installed source trees the contract applies to. Resolved from the packages dir so a new
# shipped bundle is swept the moment it lands, with no list to maintain here.
PACKAGES = pathlib.Path(__file__).resolve().parents[3] / "packages"
SHIPPED_BUNDLES = ("kx-mcp-kdbx", "kx-mcp-kdbai")


# --- 1. the helpers ------------------------------------------------------------------------------


def test_error_result_marks_is_error_and_keeps_the_payload():
    payload = {"code": "not_found", "message": "no such table; call list_tables"}
    result = error_result(payload)

    assert isinstance(result, ToolResult)
    assert result.is_error is True
    assert result.structured_content == payload


def test_error_result_copies_the_payload():
    """A caller mutating its dict afterwards must not retroactively edit the result."""
    payload = {"status": STATUS_ERROR, "message": "boom"}
    result = error_result(payload)
    payload["message"] = "mutated"

    assert result.structured_content["message"] == "boom"


@pytest.mark.parametrize("status", sorted(FAILURE_STATUSES))
def test_tool_result_wraps_every_failure_status(status):
    result = tool_result({"status": status, "message": "nope"})

    assert isinstance(result, ToolResult)
    assert result.is_error is True
    assert result.structured_content["status"] == status


def test_tool_result_passes_a_success_through_unwrapped():
    payload = {"status": STATUS_OK, "data": [1, 2, 3]}
    result = tool_result(payload)

    assert result == payload
    assert not isinstance(result, ToolResult)


def test_tool_result_treats_a_missing_status_as_success():
    """Absence of a failure signal is not a failure — guessing would flip every unannotated tool."""
    result = tool_result({"data": []})

    assert not isinstance(result, ToolResult)


def test_failure_statuses_are_exactly_error_and_denied():
    """Pins the vocabulary: widening it silently reclassifies existing payloads."""
    assert FAILURE_STATUSES == {STATUS_ERROR, STATUS_DENIED}
    assert STATUS_OK not in FAILURE_STATUSES


# --- 2. over a real dispatch ---------------------------------------------------------------------


def _counter(target: str, outcome: str) -> float:
    value = REGISTRY.get_sample_value(COUNTER_NAME, {"tool_name": target, "outcome": outcome})
    return 0.0 if value is None else value


@pytest.fixture(autouse=True)
def _reset_decision():
    begin_authz_dispatch()
    yield
    begin_authz_dispatch()


def _backend(name: str) -> FastMCP:
    mcp = FastMCP(name)

    @mcp.tool()
    async def fine() -> dict:
        return tool_result({"status": STATUS_OK, "data": [1]})

    @mcp.tool()
    async def broken() -> dict:
        return tool_result({"status": STATUS_ERROR, "message": "no_such_table"})

    @mcp.tool()
    async def refused() -> dict:
        # What record_denial does: stamp the decision, then return the structured envelope.
        stamp_authz_decision(
            AuthzDecision(allowed=False, adapter="kx_auth_qside", reason="denied: no")
        )
        return tool_result({"status": STATUS_DENIED, "message": "Access denied"})

    return mcp


def _call(parent: FastMCP, tool: str):
    async def go():
        async with Client(parent) as client:
            return await client.call_tool(tool, {}, raise_on_error=False)

    return asyncio.run(go())


@pytest.mark.parametrize(
    ("tool", "expect_error", "expect_outcome"),
    [
        ("b_fine", False, "ok"),
        ("b_broken", True, "error"),
        ("b_refused", True, "denied"),
    ],
)
def test_returned_failures_reach_the_client_and_the_counter(tool, expect_error, expect_outcome):
    settings = ObservabilitySettings(metrics="prometheus")
    parent = make_parent("kx-mcp", observability=settings)
    parent.mount(_backend("b"), namespace="b")

    before = _counter(tool, expect_outcome)
    result = _call(parent, tool)

    assert result.is_error is expect_error
    assert _counter(tool, expect_outcome) == before + 1


def _stamping_backend(name: str, *, is_async: bool) -> FastMCP:
    """A backend with one denial-stamping tool, in either shape, otherwise identical.

    The stamp goes through ``stamp_authz_decision`` — the same writer ``record_denial`` and
    ``consult_data_gate`` use — so this exercises the real mechanism rather than a hand-rolled
    contextvar write that could drift from it.
    """
    mcp = FastMCP(name)

    def body() -> dict:
        stamp_authz_decision(
            AuthzDecision(allowed=False, adapter="kx_auth_qside", reason="denied: no")
        )
        return tool_result({"status": STATUS_DENIED, "message": "Access denied"})

    if is_async:

        @mcp.tool()
        async def refused() -> dict:
            return body()

    else:

        @mcp.tool()
        def refused() -> dict:  # noqa: F811 - the sync counterpart, one shape per backend
            return body()

    return mcp


@pytest.mark.parametrize("is_async", [True, False], ids=["async_tool", "sync_tool"])
def test_a_denial_stamped_in_a_tool_body_reaches_the_middleware(is_async):
    """A returned denial must be counted ``denied`` whether the tool body is sync or async.

    This is the test the ``async def`` AST guard was standing in for. fastmcp dispatches a **sync**
    tool through ``anyio.to_thread.run_sync``, which copies the context into a worker thread: a
    ``ContextVar.set`` there is invisible to the dispatching task, so the stamp is lost and an
    authorization refusal is misreported as a plain ``error``. Reads work across that boundary,
    writes do not — which is why fastmcp's own "propagates contextvars" docstring is not the whole
    story.

    Both shapes must agree, because *where the body runs* is a performance decision and must not
    silently change a security-relevant outcome.
    """
    namespace = "s" if is_async else "t"
    target = f"{namespace}_refused"
    settings = ObservabilitySettings(metrics="prometheus")
    parent = make_parent("kx-mcp", observability=settings)
    parent.mount(_stamping_backend(namespace, is_async=is_async), namespace=namespace)

    before_denied = _counter(target, "denied")
    before_error = _counter(target, "error")
    result = _call(parent, target)

    assert result.is_error is True
    assert _counter(target, "denied") == before_denied + 1, "the denial did not reach the middleware"
    assert _counter(target, "error") == before_error, "counted as a plain error instead of a denial"


def test_a_returned_error_is_not_counted_as_ok():
    """The regression this contract exists for: payload says error, protocol said success."""
    settings = ObservabilitySettings(metrics="prometheus")
    parent = make_parent("kx-mcp", observability=settings)
    parent.mount(_backend("c"), namespace="c")

    before_ok = _counter("c_broken", "ok")
    _call(parent, "c_broken")

    assert _counter("c_broken", "ok") == before_ok


# --- 3. the shipped bundles comply ---------------------------------------------------------------


def _tool_functions(tree: ast.Module):
    """Every function in ``tree`` carrying the standalone ``@tool`` decorator."""
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            target = dec.func if isinstance(dec, ast.Call) else dec
            if isinstance(target, ast.Name) and target.id == "tool":
                yield node
                break


def _addin_files():
    for bundle in SHIPPED_BUNDLES:
        src = PACKAGES / bundle / "src"
        for path in src.rglob("addins/*.py"):
            yield path


def _async_functions(tree: ast.Module):
    """Every ``async def`` in ``tree``, paired with whether it is a registered primitive."""
    primitives = {"tool", "resource", "prompt"}
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef):
            continue
        registered = False
        for dec in node.decorator_list:
            target = dec.func if isinstance(dec, ast.Call) else dec
            if isinstance(target, ast.Name) and target.id in primitives:
                registered = True
                break
        yield node, registered


def _awaits(node: ast.AsyncFunctionDef) -> bool:
    return any(isinstance(n, (ast.Await, ast.AsyncFor, ast.AsyncWith)) for n in ast.walk(node))


@pytest.mark.parametrize("bundle", SHIPPED_BUNDLES)
def test_no_impl_is_async_without_awaiting(bundle):
    """An ``async def`` that never awaits is a lie about the code, everywhere except a primitive.

    The registered ``@tool`` / ``@resource`` wrappers are the one legitimate exception — there
    ``async`` means "dispatch me on the loop" and carries no implication about the body (see
    ``test_every_registered_tool_is_async``). Anywhere else it misleads: it says the function yields
    when it does not, it forces every caller and every test into an async context for nothing, and it
    hides which work actually blocks.

    Sixteen ``*_impl`` functions were async-without-await before this guard existed. The four that
    remain async — the similarity and hybrid search impls in both bundles — genuinely ``await`` an
    embedding provider, which is real async work.

    ruff's ``RUF029`` (unused-async) is the same rule, but it is preview-only and would also flag the
    deliberate wrappers above, so each would need a ``noqa``. An AST guard states the exception once.
    """
    offenders = [
        f"{path.name}::{node.name}"
        for path in (PACKAGES / bundle / "src").rglob("addins/*.py")
        for node, registered in _async_functions(ast.parse(path.read_text()))
        if not registered and not _awaits(node)
    ]

    assert not offenders, (
        "async def with no await — declare these `def` unless they need an async context: "
        f"{offenders}"
    )


@pytest.mark.parametrize("bundle", SHIPPED_BUNDLES)
def test_every_registered_tool_is_async(bundle):
    """Every registered primitive must be ``async def``. This is correctness, not style.

    ``async`` here is not a claim about the body — most of these wrappers contain no ``await``, and
    that is deliberate. It is how fastmcp is told **where to dispatch**: a coroutine is awaited inline
    on the event loop, while a plain ``def`` is handed to ``anyio.to_thread.run_sync`` and runs in a
    worker thread. Two things depend on staying on the loop:

    * **Licensed q refuses to open a socket off the main thread** (``nosocket: Cannot open or use a
      socket on a thread other than main``), so a worker thread cannot establish a connection. It may
      freely *use* one opened on the main thread — the restriction is on opening.
    * **The connections carry no lock.** q IPC has no message ids and PyKX matches replies to callers
      by queue position, so two threads sharing a handle can each be handed the other's reply. Single-
      threaded dispatch is what makes the absence of a lock sound, so a sync tool would reintroduce a
      real data-corruption hazard, not merely a performance change.

    Not the original reason. This guard was written to protect the authz stamp — a sync body runs with
    a *copied* context, so a ``ContextVar.set`` inside it never reached the parent middleware and a
    denial was recorded as a plain ``error``. That is fixed independently (the decision now travels in
    a mutable :class:`~kx_mcp_core.auth.AuthzSlot`, proven by
    ``test_a_denial_stamped_in_a_tool_body_reaches_the_middleware``), so it is no longer what keeps
    this rule alive.

    To retire this guard you would need a lock on every handle and a main-thread connect path. See the
    reverted-offload note in ``mcp-container/BACKLOG.md`` for why that was tried and undone.
    """
    src = PACKAGES / bundle / "src"
    sync_tools = [
        f"{path.name}::{fn.name}"
        for path in src.rglob("addins/*.py")
        for fn in _tool_functions(ast.parse(path.read_text()))
        if not isinstance(fn, ast.AsyncFunctionDef)
    ]

    assert not sync_tools, f"sync @tool functions lose authz stamps: {sync_tools}"


def test_every_registered_tool_routes_failures_through_the_contract():
    """Each registered tool must return via ``tool_result``/``error_result``, or raise.

    The sweep that makes Rule 1 enforced rather than aspirational: a new tool returning a bare
    ``{"status": "error"}`` dict fails here instead of silently reporting a failure as a success.
    A tool returning a plain non-dict (a ``str`` guidance blob) has no failure payload to mark and
    is exempt.
    """
    offenders = []
    for path in _addin_files():
        tree = ast.parse(path.read_text())
        for fn in _tool_functions(tree):
            returns = [n for n in ast.walk(fn) if isinstance(n, ast.Return) and n.value is not None]
            if not returns:
                continue
            wrapped = any(
                isinstance(r.value, ast.Call)
                and isinstance(r.value.func, ast.Name)
                and r.value.func.id in {"tool_result", "error_result"}
                for r in returns
            )
            # A tool whose declared return type is `str` carries no structured failure payload.
            plain_text = isinstance(fn.returns, ast.Name) and fn.returns.id == "str"
            if not wrapped and not plain_text:
                offenders.append(f"{path.name}::{fn.name}")

    assert not offenders, (
        "these registered tools do not route their return through the tool-result contract "
        f"(kx_mcp_core.tool_result / error_result): {offenders}"
    )


@pytest.mark.parametrize("bundle", SHIPPED_BUNDLES)
def test_read_tools_declare_read_only_hint(bundle):
    """Every registered tool declares annotations, so hosts can auto-approve reads.

    Unset defaults to "assume the worst", which puts an approval prompt in front of a plain read.
    """
    src = PACKAGES / bundle / "src"
    unannotated = []
    for path in src.rglob("addins/*.py"):
        for fn in _tool_functions(ast.parse(path.read_text())):
            for dec in fn.decorator_list:
                if not isinstance(dec, ast.Call):
                    continue
                target = dec.func
                if isinstance(target, ast.Name) and target.id == "tool":
                    if not any(kw.arg == "annotations" for kw in dec.keywords):
                        unannotated.append(f"{path.name}::{fn.name}")
                    break
            else:
                unannotated.append(f"{path.name}::{fn.name}")

    assert not unannotated, f"registered tools without annotations: {unannotated}"
