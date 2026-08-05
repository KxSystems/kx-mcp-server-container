"""Versioned JSON resources for KDB-X table metadata."""

import json
import logging
from typing import List

from fastmcp import Context
from fastmcp.resources import resource
from mcp.types import TextContent

from kx_mcp_kdbx.utils.kdbx import config_from_ctx, metadata_cache_from_ctx
from kx_mcp_kdbx.utils.metadata_model import (
    build_tables_document,
    unexpected_error_document,
)

logger = logging.getLogger(__name__)


def _as_content(document: dict) -> List[TextContent]:
    return [TextContent(type="text", text=json.dumps(document, indent=2, default=str))]


async def kdbx_describe_table_impl(
    table: str, config=None, cache=None, preview_rows: int = 3
) -> List[TextContent]:
    try:
        return _as_content(
            build_tables_document(
                config=config, cache=cache, table=table, preview_rows=preview_rows
            )
        )
    except Exception as error:
        logger.error(f"Failed to analyze table '{table}': {error}")
        return _as_content(unexpected_error_document(str(error)))


async def kdbx_describe_tables_impl(config=None, cache=None) -> List[TextContent]:
    try:
        return _as_content(build_tables_document(config=config, cache=cache))
    except Exception as error:
        logger.error(f"Database schema analysis failed: {error}")
        return _as_content(unexpected_error_document(str(error)))


@resource("tables://all")
async def kdbx_describe_tables(ctx: Context) -> str:
    """All visible KDB-X tables under metadata contract v1, with semantics and live samples."""
    items = await kdbx_describe_tables_impl(
        config=config_from_ctx(ctx), cache=metadata_cache_from_ctx(ctx)
    )
    return items[0].text


@resource("tables://{table}")
async def kdbx_describe_table(table: str, ctx: Context) -> str:
    """One visible KDB-X table under metadata contract v1; prefer this token-efficient lookup."""
    items = await kdbx_describe_table_impl(
        table, config=config_from_ctx(ctx), cache=metadata_cache_from_ctx(ctx)
    )
    return items[0].text
