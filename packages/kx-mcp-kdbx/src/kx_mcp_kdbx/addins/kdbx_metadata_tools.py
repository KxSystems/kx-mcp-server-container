"""Tools for token-efficient table lookup and administrative aimeta refresh."""

import logging
from typing import Annotated, Any, Dict

from fastmcp import Context
from fastmcp.tools import tool
from pydantic import Field

from kx_mcp_core.auth import authorize
from kx_mcp_kdbx.utils.aimeta import reload_remote_metadata
from kx_mcp_kdbx.utils.kdbx import (
    config_from_ctx,
    get_kdb_connection,
    metadata_cache_from_ctx,
)
from kx_mcp_kdbx.utils.metadata_model import (
    build_tables_document,
    unexpected_error_document,
)

logger = logging.getLogger(__name__)


async def kdbx_get_table_metadata_impl(
    table: str, preview_rows: int = 3, config=None, cache=None
) -> Dict[str, Any]:
    try:
        return build_tables_document(
            config=config, cache=cache, table=table, preview_rows=preview_rows
        )
    except Exception as error:
        logger.error(f"Failed to read metadata for table '{table}': {error}")
        return unexpected_error_document(str(error))


@authorize(action="admin", resource="kdbx:metadata")
async def kdbx_refresh_metadata_impl(config=None, cache=None) -> Dict[str, Any]:
    try:
        conn = get_kdb_connection(config)
        remote_reload, detected = reload_remote_metadata(conn=conn, cache=cache)
        if detected.document is None:
            return {
                "status": "error",
                "remoteReload": remote_reload,
                "message": detected.detail
                or "KDB-X did not return valid aimeta metadata",
            }
        return {
            "status": "success",
            "remoteReload": remote_reload,
            "tier": detected.tier,
            "annotationStatus": detected.annotation_status,
            "message": "KDB-X aimeta metadata reloaded and the local cache was refreshed",
        }
    except Exception as error:
        logger.error(f"Metadata refresh failed: {error}")
        return {"status": "error", "message": str(error)}


@tool
async def get_table_metadata(
    table: str,
    ctx: Context,
    preview_rows: Annotated[int, Field(ge=0, le=100)] = 3,
) -> Dict[str, Any]:
    """Get one table's contract-v1 schema and live state before composing a query.

    Semantic types and foreign references explain joins and vocabulary resolution. `rowSource`
    distinguishes real preview rows from illustrative annotation samples. Read
    `schema://kdbx/metadata/v1` for the complete response contract.
    """
    return await kdbx_get_table_metadata_impl(
        table,
        preview_rows,
        config=config_from_ctx(ctx),
        cache=metadata_cache_from_ctx(ctx),
    )


@tool
async def refresh_metadata(ctx: Context) -> Dict[str, Any]:
    """Administratively reload recompiled aimeta annotations without restarting the MCP server."""
    return await kdbx_refresh_metadata_impl(
        config=config_from_ctx(ctx), cache=metadata_cache_from_ctx(ctx)
    )
