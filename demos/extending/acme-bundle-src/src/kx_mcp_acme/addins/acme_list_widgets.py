"""An add-in that follows the config → cached-connection → query path a real backend tool uses.

Keeps the ``*_impl`` function separate from the decorated entry point (the convention the shipping
bundles follow) so a test can call ``list_widgets_impl(kind, config=...)`` directly with a config,
while the discovered ``@tool`` reads its per-mount config from the request Context — never a module
global — for instance-safety.
"""

from __future__ import annotations

from fastmcp import Context
from fastmcp.tools import tool

from ..settings import AcmeConfig
from ..utils.acme import config_from_ctx, get_connection


def list_widgets_impl(kind: str, config: AcmeConfig | None = None) -> dict:
    """Query the (stub) backend for widgets of the given kind, reusing the cached connection."""
    conn = get_connection(config or AcmeConfig())
    return conn.query(kind)


@tool
def list_widgets(kind: str, ctx: Context) -> dict:
    """List Acme widgets of the given kind, served from the (stub) backend connection.

    Resolves the mount's config from the request Context and reuses the cached connection —
    the config → connection → tool path every real backend tool follows.
    """
    return list_widgets_impl(kind, config=config_from_ctx(ctx))
