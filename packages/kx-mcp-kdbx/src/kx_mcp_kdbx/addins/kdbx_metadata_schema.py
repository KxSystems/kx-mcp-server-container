"""Machine-readable schema for the agent-facing metadata contract."""

from importlib.resources import files

from fastmcp.resources import resource


def kdbx_metadata_schema_impl() -> str:
    return (
        files("kx_mcp_kdbx.schemas")
        .joinpath("metadata-v1.schema.json")
        .read_text(encoding="utf-8")
    )


@resource("schema://metadata/v1", mime_type="application/schema+json")
async def kdbx_metadata_schema() -> str:
    """JSON Schema for every KDB-X table and function metadata response (contract version 1)."""
    return kdbx_metadata_schema_impl()
