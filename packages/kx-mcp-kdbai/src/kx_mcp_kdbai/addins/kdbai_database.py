import logging
from typing import Optional, Dict, Any
from fastmcp import Context
from fastmcp.tools import tool
from kx_mcp_kdbai.utils.kdbai import get_kdbai_client, config_from_ctx
from kx_mcp_kdbai.settings import KDBAIConfig

logger = logging.getLogger(__name__)

async def kdbai_list_databases_impl(config: Optional[KDBAIConfig] = None) -> Dict[str, Any]:
    try:
        client = get_kdbai_client(config)
        return {
            "status": "success",
            "databases": [db.name for db in client.databases()]
        }
    except Exception as e:
        logger.error(f"Error listing databases: {e}")
        return {
            "status": "error",
            "message": str(e)
        }

async def kdbai_databases_info_impl(database: Optional[str] = None,
                                    config: Optional[KDBAIConfig] = None) -> Dict[str, Any]:
    try:
        client = get_kdbai_client(config)
        if database is None: # all database info
            info = client.databases_info()
        else:  # specific database info
            info = client.database(database).info()
        return {
            "status": "success",
            "info": info
        }
    except Exception as e:
        logger.error(f"Error in getting database info: {e}")
        return {
            "status": "error",
            "message": str(e)
        }

# Standalone @tool decorators — bare names; `mount(namespace="kdbai")` yields `kdbai_list_databases`
# / `kdbai_database_info` / `kdbai_all_databases_info`.
@tool
async def list_databases(ctx: Context) -> Dict[str, Any]:
    """
    List all databases name in the KDB.AI database.

    Returns:
        A dictionary with following data:
        status: 'success' for successfull execution , 'error' if function fails
        databases: list of databases names
    """
    return await kdbai_list_databases_impl(config_from_ctx(ctx))


@tool
async def database_info(ctx: Context, database: Optional[str] = "default") -> Dict[str, Any]:
    """
    Get KDBAI database information. Database information also includes tables information for each table in the database.

    Args:
        database (Optional[str], optional): Name of the database. If not given then uses 'default' database.

    Returns:
        A dictionary with following data:
            status: 'success' for successfull execution , 'error' if function fails
            info: dictionary containing database information.
                - The dictionary has a key 'tables' which maps to a list of dictionaries.
                - Each dictionary in this list represents a single table, and holds its corresponding information (eg: name,rowCount).
    """
    info = await kdbai_databases_info_impl(database, config_from_ctx(ctx))
    return info


@tool
async def all_databases_info(ctx: Context) -> Dict[str, Any]:
    """
    Get information of all databases in KDBAI database. Each database entry includes tables information of each table in that database.

    Returns:
        A dictionary with following data:
            status: 'success' for successfull execution , 'error' if function fails
            info: dictionary that has information of all databases.
                - The dictionary has a key 'databases' which maps to a list of dictionaries.
                - Each dictionary in this list represents a single database, and holds its corresponding information (eg: tableCount, tables).
    """
    info = await kdbai_databases_info_impl(None, config_from_ctx(ctx))
    return info
