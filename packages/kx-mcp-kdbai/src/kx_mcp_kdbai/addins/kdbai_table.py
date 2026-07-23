import logging
from typing import Optional, Dict, Any
from fastmcp import Context
from fastmcp.tools import tool
from kx_mcp_kdbai.utils.kdbai import get_kdbai_client, config_from_ctx
from kx_mcp_kdbai.settings import KDBAIConfig

logger = logging.getLogger(__name__)


async def list_tables_impl(database_name: Optional[str] = None,
                           config: Optional[KDBAIConfig] = None) -> Dict[str, Any]:
    cfg = config if config is not None else KDBAIConfig()
    try:
        if database_name is None:
            database_name = cfg.database_name
        client = get_kdbai_client(config)
        db = client.database(database_name)
        tables = [table.name for table in db.tables]
        return {'database': database_name, 'tables': tables}
    except Exception as e:
        logger.error(f"Error listing tables in database {database_name}: {e}")
        return {
            "status": "error",
            "message": str(e),
            "database": database_name
        }


async def kdbai_table_info_impl(table_name: str, database_name: Optional[str] = None,
                                config: Optional[KDBAIConfig] = None) -> Dict[str, Any]:
    """
        Get comprehensive information about a table including schema and statistics.
        Check function kdbai_info_table for details
    """
    cfg = config if config is not None else KDBAIConfig()
    try:
        if database_name is None:
            database_name = cfg.database_name

        client = get_kdbai_client(config)
        table = client.database(database_name).table(table_name)
        data = table.info()
        data['schema'] = table.schema
        if len(table.indexes) > 0:
            data['indexes'] = table.indexes
        return data
    except Exception as e:
        logger.error(f"Error getting table info for {table_name}: {e}")
        return {
            "status": "error",
            "message": str(e),
            "database": database_name
        }


# Standalone @tool decorators — bare names; `mount(namespace="kdbai")` yields `kdbai_list_tables`
# / `kdbai_table_info`.
@tool
async def list_tables(ctx: Context, database_name: Optional[str] = None) -> Dict[str, Any]:
    """
    List all tables in the given database.

    Args:
        database_name: Name of the database (optional:defaults to configured database)

    Returns:
        A dictionary with following details:
        database: name of database
        tables: list of tables in the mentioned database
    """
    return await list_tables_impl(database_name, config_from_ctx(ctx))


@tool
async def table_info(table_name: str, ctx: Context, database_name: Optional[str] = None) -> Dict[str, Any]:
    """
    Get comprehensive information about a table including schema and statistics.

    Args:
        table_name: Name of the table
        database_name: Name of the database (defaults to configured database)

    Returns:
        Dictionary containing table information and metadata as below.
        name: table name
        database: database name
        disk: disk size used by table in MB
        rowCount: total number of rows in the table
        schema: table schema as list of dictionary. Each item as column name and column type.
        indexes: table indexes as list of dictionary. Each entry is one index information.
    """
    return await kdbai_table_info_impl(table_name, database_name, config_from_ctx(ctx))
