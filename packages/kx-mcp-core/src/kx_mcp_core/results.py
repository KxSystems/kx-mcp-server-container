"""Tool-result helpers: mark a failed dispatch as an MCP tool error (``isError: true``).

**The contract.** A tool that fails MUST tell the protocol so, by returning a result with
``isError: true`` — not by returning a success whose payload happens to contain an error field. This
is the MCP-sanctioned shape ("return MCP tool errors, not exceptions that crash the transport"), and
it is *required* of every extension: see ``docs/extending.md`` § Signalling failure.

Two things depend on it, and neither can be fixed downstream:

* **The agent.** ``isError`` is the signal a host and a model treat as "this call failed, try
  something else". A structured payload alone is advisory — the model usually reads it, but nothing
  in the protocol says it must, and a host rendering only ``structuredContent`` shows a green tick
  over a failure.
* **Observability.** Parent middleware sees the dispatch, not your payload conventions. Without
  ``isError`` the metrics counter records ``outcome="ok"`` for a failed call and
  ``kx_mcp_dispatches_total{outcome="error"}`` reads zero during an outage — see
  ``docs/observability.md`` § What ``outcome`` can and cannot see.

**Why return a result rather than raise.** Raising :class:`~fastmcp.exceptions.ToolError` also yields
``isError: true``, but it *discards ``structuredContent`` entirely* — the client gets text only, so
every discriminating field (``error_type``, ``technical_details``, an entitled-subset list) is lost,
and fastmcp logs the expected condition at ``ERROR``. Raise only when there is genuinely nothing
structured to say; return an error result whenever there is. Both helpers here keep the payload:
``ToolResult`` populates the text content from ``structured_content`` as well, so a client reading
either sees the same thing.

Signatures do not change. A tool keeps its ``-> Dict[str, Any]`` annotation and its generated
``outputSchema``; only the *error* returns get wrapped.
"""

from __future__ import annotations

from typing import Any, Mapping

from fastmcp.tools import ToolResult

# The status vocabulary the shipped KX backends use in their structured payloads. `success` is the
# only non-failure value; `permission_denied` is a failure that is *also* an authorization refusal
# (recorded as outcome="denied" when the refusing layer stamps the authz decision — see
# kx_mcp_core.auth.stamp_authz_decision / authz_decision).
STATUS_OK = "success"
STATUS_ERROR = "error"
STATUS_DENIED = "permission_denied"

#: Every status value that means "this dispatch failed".
FAILURE_STATUSES = frozenset({STATUS_ERROR, STATUS_DENIED})


def error_result(payload: Mapping[str, Any]) -> ToolResult:
    """Wrap a structured error payload as an MCP tool error (``isError: true``).

    The Rule-1 primitive, independent of any status-field convention — use it directly when your
    bundle's payloads do not carry a ``status`` field, or when you already know the call failed::

        return error_result({"code": "not_found", "message": "no such table; call list_tables"})

    Put a recovery hint in the message. "Item not found. Use search_items to find valid IDs" turns a
    dead end into a next step; "error" does not.
    """
    return ToolResult(structured_content=dict(payload), is_error=True)


def tool_result(payload: Mapping[str, Any]) -> Any:
    """Return ``payload`` as a tool result, marked ``isError: true`` iff its ``status`` says it failed.

    The one-call way to comply with Rule 1 for a payload following the KX ``status`` vocabulary
    (:data:`STATUS_OK` / :data:`STATUS_ERROR` / :data:`STATUS_DENIED`). Apply it at the registered
    ``@tool`` wrapper, over the ``*_impl``'s return value::

        @tool
        async def get_table_metadata(table: str, ctx: Context) -> Dict[str, Any]:
            return tool_result(await get_table_metadata_impl(table, config=config_from_ctx(ctx)))

    Wrapping at the wrapper rather than inside the ``*_impl`` is deliberate: the ``*_impl`` stays a
    plain-dict function — which is what tests call directly, what ``@authorize`` decorates, and what
    the pinned ``schema://kdbx/metadata/v1`` document contract describes — so adopting this costs one
    line per tool and changes no payload and no test.

    A success passes through as the bare mapping (no needless wrapper). A payload with **no**
    ``status`` key is treated as a success: absence of a failure signal is not a failure, and
    guessing otherwise would flip every unannotated tool to ``isError`` at once.

    **The return type is ``Any`` deliberately.** The honest union — ``ToolResult | dict`` — would
    force every caller to widen its own ``-> Dict[str, Any]`` annotation, and FastMCP derives a
    tool's ``outputSchema`` from exactly that annotation: relaxing it to ``Any`` drops the schema
    from the tool's advertised surface altogether (verified — ``outputSchema`` becomes ``None``).
    ``ToolResult`` is a transport envelope FastMCP unwraps, not part of the tool's declared data
    shape, so ``Any`` here keeps the type checker quiet without costing clients their schema.
    """
    if payload.get("status") in FAILURE_STATUSES:
        return error_result(payload)
    return dict(payload)
