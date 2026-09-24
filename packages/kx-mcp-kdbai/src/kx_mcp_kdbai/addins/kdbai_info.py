import logging
from typing import Dict, Any, Optional
from fastmcp import Context
from fastmcp.tools import tool
from kx_mcp_kdbai.utils.kdbai import get_kdbai_client, config_from_ctx
from kx_mcp_kdbai.utils.observe import call
from kx_mcp_kdbai.settings import KDBAIConfig

logger = logging.getLogger(__name__)


def kdbai_session_info_impl(config: Optional[KDBAIConfig] = None) -> Dict[str, Any]:
    try:
        client = get_kdbai_client(config)
        info = call("session_info", client.session_info)
        return info
    except Exception as e:
        logger.error(f"Error getting session info: {e}")
        raise


def kdbai_system_info_impl(config: Optional[KDBAIConfig] = None) -> Dict[str, Any]:
    try:
        client = get_kdbai_client(config)
        info = call("system_info", client.system_info)
        return info
    except Exception as e:
        logger.error(f"Error getting system info: {e}")
        raise


def kdbai_process_info_impl(config: Optional[KDBAIConfig] = None) -> Dict[str, Any]:
    try:
        client = get_kdbai_client(config)
        info = call("process_info", client.process_info)
        return info
    except Exception as e:
        logger.error(f"Error getting process info: {e}")
        raise


# Standalone @tool decorators — bare tool names; the container supplies the `kdbai` qualifier via
# mount(namespace="kdbai"), so the composed tools are `kdbai_session_info` etc. (not
# double-prefixed `kdbai_kdbai_...`).
@tool(annotations={"readOnlyHint": True})
async def session_info(ctx: Context) -> str:
    """
    Get session information from KDB.AI.

    Returns:
        Dictionary containing session information and metadata.
    """
    info = kdbai_session_info_impl(config_from_ctx(ctx))
    return str(info)


@tool(annotations={"readOnlyHint": True})
async def system_info(ctx: Context) -> str:
    """
    Get system information from KDB.AI.

    Returns:
        Dictionary containing system information and metadata.
    """
    info = kdbai_system_info_impl(config_from_ctx(ctx))
    return str(info)


@tool(annotations={"readOnlyHint": True})
async def process_info(ctx: Context) -> str:
    """
    Get process information from KDB.AI.

    Returns:
        Dictionary containing process information and metadata.
    """
    info = kdbai_process_info_impl(config_from_ctx(ctx))
    return str(info)
