
import logging
from pathlib import Path

from fastmcp.resources import resource

logger = logging.getLogger(__name__)

# Co-located data file, resolved relative to THIS module (not the process CWD) so it is found
# regardless of where the server is launched from and survives packaging into the wheel.
_GUIDANCE_PATH = Path(__file__).with_name("kdbx_sql_query_guidance.txt")


def kdbx_sql_query_guidance_impl() -> str:
    with open(_GUIDANCE_PATH, 'r', encoding='utf-8') as file:
        return file.read()


# Standalone @resource decorator (FastMCP 3.x native discovery): no params and no URI template,
# so this is a static Resource.
@resource("file://guidance/kdbx-sql-queries")
async def kdbx_sql_query_guidance() -> str:
    """
    Provides guidance when using SQL select statements with the kdbx_run_sql_query tool.

    Returns:
        str: Details and examples on supported select statement when using the sql tool.
    """
    return kdbx_sql_query_guidance_impl()