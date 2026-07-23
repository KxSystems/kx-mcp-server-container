import logging
from typing import List
from mcp.types import TextContent
from fastmcp import Context
from fastmcp.resources import resource
from kx_mcp_kdbx.utils import kdbx as kdbx_utils
from kx_mcp_kdbx.utils.kdbx import get_kdb_connection, config_from_ctx
from kx_mcp_kdbx.utils.authz_kx_entitlements import consult_data_gate
from kx_mcp_kdbx.utils.format_utils import format_data_for_display

logger = logging.getLogger(__name__)


async def kdbx_describe_table_impl(table: str, config=None) -> List[TextContent]:
    """
    Describe a specific KDB table with metadata and sample rows.

    Args:
        table: Name of the KDB table to describe
        config: Optional per-instance KDBConfig (routes to the right backend connection)

    Returns:
        List[TextContent]: Table description with metadata and sample data
    """
    try:
        conn = get_kdb_connection(config)

        total_records = conn('{count get x}', table).py()

        schema_data = conn.meta(table).py()
        partitioned_table = table in conn.Q.pt.py()

        output_lines = [
            f"\n  TABLE ANALYSIS: {table}",
            f"{'=' * 60}",
        ]

        output_lines.extend([
            "\n Schema Information:",
            format_data_for_display(schema_data, table)
        ])

        if total_records > 0:
            preview_size = min(3, total_records)
            if not partitioned_table:
                preview_data = conn('{x sublist get y}', preview_size, table).py()
            else:
                preview_data = conn('{.Q.ind[get y;til x]}', preview_size, table).py()

            output_lines.extend([
                f"\n Data Preview ({preview_size} records):",
                format_data_for_display(preview_data, table)
            ])
        else:
            output_lines.append("\n Table is empty - no data to preview")

        final_output = "\n".join(output_lines)

        return [TextContent(type="text", text=final_output)]

    except Exception as error:
        logger.error(f"Failed to analyze table '{table}': {error}")
        error_output = f"\n TABLE ANALYSIS FAILED: {table}\n{'=' *60}\nError: {error}"
        return [TextContent(type="text", text=error_output)]


async def kdbx_describe_tables_impl(config=None) -> List[TextContent]:
    try:
        conn = get_kdb_connection(config)

        available_tables = conn.tables(None).py()
        
        # Filter out internal AI library index tables (*document, *stats, *token)
        available_tables = [
            table for table in available_tables
            if not (table.endswith('document') or table.endswith('stats') or table.endswith('token'))
        ]

        # Data gate (opt-in via KDBX_DB_DATA_GATE): scope the listing down to the tables the bound
        # principal may read — the direct application of the decision's obligations
        # (allow-with-obligations → filter). A full deny yields a clean denial, not an empty lie.
        scope_note = ""
        cfg = config if config is not None else kdbx_utils.db_config
        if getattr(cfg, "data_gate", False) and available_tables:
            decision = consult_data_gate("read", sorted(available_tables))
            if not decision.allowed:
                return [TextContent(
                    type="text",
                    text=f" DATABASE ACCESS DENIED\n{'═' * 60}\n"
                         f"{decision.reason or 'not entitled to read any table'}"
                )]
            entitled = decision.obligations.get("entitled")
            if entitled is not None:
                entitled_set = set(entitled)
                hidden = [t for t in available_tables if t not in entitled_set]
                available_tables = [t for t in available_tables if t in entitled_set]
                scope_note = (
                    f" ({len(hidden)} table(s) hidden by entitlements)" if hidden else ""
                )

        if not available_tables:
            return [TextContent(
                type="text",
                text=" Database is empty - no tables found"
            )]

        overview_parts = [
            "  DATABASE SCHEMA OVERVIEW",
            "═" * 60,
            f" Found {len(available_tables)} table(s){scope_note}\n"
        ]

        for table_name in available_tables:
            table_analysis = await kdbx_describe_table_impl(table_name, config=config)
            overview_parts.append(table_analysis[0].text)

        complete_overview = "\n".join(overview_parts)
        logger.debug(complete_overview)
        return [TextContent(type="text", text=complete_overview)]

    except Exception as error:
        logger.error(f"Database schema analysis failed: {error}")
        return [TextContent(
            type="text",
            text=f" DATABASE ANALYSIS ERROR\n{'═' * 60}\nFailed to analyze database schema: {error}"
        )]



# Standalone @resource decorator (FastMCP 3.x native discovery): attaches __fastmcp__ metadata
# and returns the function unchanged. The `ctx` param is an injected dependency (stripped before
# the URI/param check), so with no URI template this stays a static Resource. Bare scheme —
# composed as `tables://kdbx/all` via mount(namespace="kdbx"). Config is read from ctx.fastmcp.
@resource("tables://all")
async def kdbx_describe_tables(ctx: Context) -> str:
    """
    Generate comprehensive KDB database schema overview with all KDB table details.

    Returns:
        str: Complete database analysis with all tables
    """
    # The impl keeps its tested List[TextContent] contract; FastMCP 3.x serializes resource
    # results as str/bytes, so flatten to text here (v1 accepted List[TextContent] directly).
    items = await kdbx_describe_tables_impl(config=config_from_ctx(ctx))
    return "\n".join(getattr(tc, "text", str(tc)) for tc in items)