import logging
from pathlib import Path

from fastmcp.resources import resource

logger = logging.getLogger(__name__)

# Co-located data file, resolved relative to THIS module (not the process CWD) so it is found
# regardless of where the server is launched from and survives packaging into the wheel.
_GUIDANCE_PATH = Path(__file__).with_name("kdbai_operations_guidance.txt")


def kdbai_operations_guidance_impl() -> str:
    with open(_GUIDANCE_PATH, "r", encoding="utf-8") as file:
        return file.read()


# Standalone @resource decorator (FastMCP 3.x native discovery): no params and no URI template,
# so this is a static Resource. Bare URI with a host+path (mirrors the kdb-x guidance resource) so
# the container's mount(namespace="kdbai") composes a clean `file://kdbai/guidance/kdbai-operations`.
@resource("file://guidance/kdbai-operations")
async def operations_guidance() -> str:
    """
    Provides guidance when using KDBAI operations like query, search and hybrid search.
    Incluides:
        - syntax information
        - examples of each operation
        - filters usage and examples
        - all other parameters usage and examples
        - guidance to follow in general
        - specific points to  take care while making decisions

    Returns:
        str: Guidance on using KDBAI operations.

    """
    return kdbai_operations_guidance_impl()
