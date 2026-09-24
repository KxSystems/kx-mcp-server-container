"""An add-in that follows the config → cached-connection → query path a real backend tool uses.

Keeps the ``*_impl`` function separate from the decorated entry point (the convention the shipping
bundles follow) so a test can call ``list_widgets_impl(kind, config=...)`` directly with a config,
while the discovered ``@tool`` reads its per-mount config from the request Context — never a module
global — for instance-safety.

It is also the worked example of the **failure contract** (``docs/extending.md`` § Signalling
failure): an unknown widget kind is a failure, so the payload says ``status: "error"`` *and* the
``@tool`` wrapper routes it through ``tool_result``, which marks the dispatch ``isError: true``. Both
halves are required — a payload that reports its own failure while the protocol reports success
reaches the host as a success and is counted as one.
"""

from __future__ import annotations

from fastmcp import Context
from fastmcp.tools import tool

from kx_mcp_core import tool_result

from ..settings import AcmeConfig
from ..utils.acme import config_from_ctx, get_connection


def list_widgets_impl(kind: str, config: AcmeConfig | None = None) -> dict:
    """Query the (stub) backend for widgets of the given kind, reusing the cached connection.

    Returns a plain dict either way — the ``*_impl`` stays directly testable and knows nothing about
    MCP result envelopes. The failure *message* names the valid kinds: a recovery hint turns a dead
    end into a next step, which is worth more to an agent than the word "error".
    """
    conn = get_connection(config or AcmeConfig())
    if not conn.known(kind):
        return {
            "status": "error",
            "endpoint": conn.endpoint,
            "message": f"unknown widget kind {kind!r}. Valid kinds: {', '.join(conn.kinds())}.",
        }
    return {"status": "success", **conn.query(kind)}


# `annotations` lets a host auto-approve this read rather than prompting; unset means it assumes
# the worst. `-> dict` stays: FastMCP derives the advertised outputSchema from that annotation.
@tool(annotations={"readOnlyHint": True})
def list_widgets(kind: str, ctx: Context) -> dict:
    """List Acme widgets of the given kind, served from the (stub) backend connection.

    Resolves the mount's config from the request Context and reuses the cached connection —
    the config → connection → tool path every real backend tool follows.
    """
    # One line is the whole failure contract: `isError: true` on a failure, payload untouched,
    # success passed straight through.
    return tool_result(list_widgets_impl(kind, config=config_from_ctx(ctx)))
