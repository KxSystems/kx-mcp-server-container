"""Versioned JSON resources for aimeta-documented public q functions."""

import json
import logging
from typing import List

from fastmcp import Context
from fastmcp.resources import resource
from mcp.types import TextContent

from kx_mcp_kdbx.utils.kdbx import config_from_ctx, metadata_cache_from_ctx
from kx_mcp_kdbx.utils.metadata_model import (
    build_functions_document,
    unexpected_error_document,
)

logger = logging.getLogger(__name__)


async def kdbx_functions_impl(
    function: str | None = None, config=None, cache=None
) -> List[TextContent]:
    try:
        document = build_functions_document(
            config=config, cache=cache, function=function
        )
    except Exception as error:
        logger.error(f"Function discovery failed: {error}")
        document = unexpected_error_document(str(error))
    return [TextContent(type="text", text=json.dumps(document, indent=2, default=str))]


@resource("functions://all")
async def kdbx_functions(ctx: Context) -> str:
    """All public q functions documented by aimeta, including signatures, examples, and table uses."""
    return (
        await kdbx_functions_impl(
            config=config_from_ctx(ctx), cache=metadata_cache_from_ctx(ctx)
        )
    )[0].text


@resource("functions://{function}")
async def kdbx_function(function: str, ctx: Context) -> str:
    """One public q function's metadata; prefer this lookup when its qualified name is known."""
    return (
        await kdbx_functions_impl(
            function, config=config_from_ctx(ctx), cache=metadata_cache_from_ctx(ctx)
        )
    )[0].text
