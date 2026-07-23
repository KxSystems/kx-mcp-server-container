"""Assertion helpers for the kdbai OAuth ACL tests (KA.*).

Operates on MCP tool-result dicts returned by the kdbai bundle tools.

Denial semantics for kdbai tool results:
  - ``kdbai_list_tables`` success: ``{"database":..., "tables":[...]}``  (no ``status`` key)
  - ``kdbai_list_tables`` empty-allowed: ``{"database":..., "tables":[]}``  (not an error)
  - All other tools success: ``{"status":"success", ...}``
  - ACL denial (any tool): ``{"status":"error", "message":"access not allowed ..."}``
  - No bearer: HTTP 401 (not a tool-result dict — raises at the transport layer)

Inbound-auth helpers (provider-agnostic) live in ``realidp.idp.helpers``.
"""

from __future__ import annotations


def assert_permitted(result: dict) -> None:
    """Assert a tool-result dict does not carry ``status:error``."""
    assert result is not None, "call_tool returned None"
    assert result.get("status") != "error", (
        f"Expected permitted result but got status:error — message: {result.get('message', result)!r}"
    )


def assert_denied(result: dict) -> None:
    """Assert a tool-result dict carries ``status:error`` (an ACL denial).

    All kdbai tools surface ACL denials as ``{"status":"error","message":"..."}``
    rather than HTTP errors — the container translates the kdbai-db exception into
    a structured error response.
    """
    assert result is not None, "call_tool returned None"
    assert result.get("status") == "error", (
        f"Expected denial (status:error) but got: {result!r}"
    )
    assert result.get("message"), f"status:error but message is empty: {result!r}"


def assert_visible_tables(result: dict, table_name: str) -> None:
    """Assert that ``kdbai_list_tables`` result includes ``table_name``.

    ``list_tables`` returns ``{"database":..., "tables":[...]}`` on success —
    no ``status`` key on the happy path.
    """
    assert result is not None, "call_tool returned None"
    assert "tables" in result and result.get("status") != "error", (
        f"Expected a tables list but got: {result!r}"
    )
    assert table_name in result["tables"], (
        f"Expected {table_name!r} in tables but got: {result['tables']}"
    )


def assert_no_visible_tables(result: dict) -> None:
    """Assert that ``kdbai_list_tables`` result is an empty list.

    An empty list means the persona has no grant on the database — kdbai-db returns
    an empty view, not an error, for the list operation. The denial surfaces only on
    direct table operations (query, table_info, etc.).
    """
    assert result is not None, "call_tool returned None"
    assert "tables" in result and result.get("status") != "error", (
        f"Expected a tables list (possibly empty) but got: {result!r}"
    )
    assert result["tables"] == [], (
        f"Expected empty tables but got: {result['tables']}"
    )
