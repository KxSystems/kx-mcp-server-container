"""Build the versioned MCP metadata projection from aimeta and live KDB-X state."""

from __future__ import annotations

import copy
import json
import logging
from datetime import datetime, timezone
from typing import Any, Sequence

from kx_mcp_kdbx.settings import KDBConfig
from kx_mcp_kdbx.utils import kdbx as kdbx_utils
from kx_mcp_kdbx.utils.aimeta import MetadataCache, MetadataDetection, detect_metadata
from kx_mcp_kdbx.utils.authz_kx_entitlements import consult_data_gate
from kx_mcp_kdbx.utils.embeddings_helpers import get_csv_data
from kx_mcp_kdbx.utils.kdbx import get_kdb_connection

logger = logging.getLogger(__name__)

# The contract URI names the backend type, not the mount: a backend mounted under a non-default
# namespace serves its schema resource elsewhere, but documents still declare this global URI.
CONTRACT_SCHEMA_URI = "schema://kdbx/metadata/v1"
CONTRACT_SCHEMA_VERSION = 1
DEFAULT_PREVIEW_ROWS = 3
MAX_PREVIEW_ROWS = 100
_INTERNAL_TABLE_SUFFIXES = ("document", "stats", "token")

_ROW_COUNTS = "{.j.j x!@[{count get x};;0Nj] each x}"
_META = "{.j.j 0!meta x}"
_PREVIEW = "{[n;t;d] r:n sublist get t; .j.j ((cols r) except d)#r}"
_PREVIEW_PARTITIONED = "{[n;t;d] r:.Q.ind[get t;til n]; .j.j ((cols r) except d)#r}"


def _decode(value: Any) -> Any:
    return value.decode("utf-8") if isinstance(value, bytes) else value


def _loads(value: Any) -> Any:
    return json.loads(_decode(value))


def _config(config: KDBConfig | None) -> KDBConfig:
    return config if config is not None else kdbx_utils.db_config


def _project_process(process: dict[str, Any]) -> dict[str, Any]:
    return {
        key: copy.deepcopy(process[key])
        for key in ("name", "host", "port", "compileStatus", "warnings")
        if key in process
    }


def _project_column(column: dict[str, Any]) -> dict[str, Any]:
    return {
        key: copy.deepcopy(column[key])
        for key in (
            "name",
            "kdbType",
            "desc",
            "semanticType",
            "foreignRef",
            "cardinality",
            "attributes",
            "label",
        )
        if key in column
    }


def _project_table(table: dict[str, Any]) -> dict[str, Any]:
    projected: dict[str, Any] = {
        key: copy.deepcopy(table[key])
        for key in (
            "name",
            "private",
            "desc",
            "reference",
            "labels",
            "sampleData",
            "tags",
        )
        if key in table
    }
    projected["columns"] = [
        _project_column(column) for column in table.get("columns") or []
    ]
    return projected


def _project_reference(reference: dict[str, Any]) -> dict[str, Any]:
    return {
        key: copy.deepcopy(reference[key])
        for key in ("semanticType", "table", "keyColumn", "label", "labels")
        if key in reference
    }


def _project_function(function: dict[str, Any]) -> dict[str, Any]:
    projected: dict[str, Any] = {
        key: copy.deepcopy(function[key])
        for key in ("name", "desc", "examples", "uses", "tags")
        if key in function
    }
    projected["params"] = [
        {
            key: copy.deepcopy(param[key])
            for key in ("name", "type", "desc")
            if key in param
        }
        for param in function.get("params") or []
    ]
    returns = function.get("returns") or {}
    projected["returns"] = {
        key: copy.deepcopy(returns[key]) for key in ("type", "desc") if key in returns
    }
    return projected


def _base(detected: MetadataDetection) -> dict[str, Any]:
    document = detected.document or {}
    metadata: dict[str, Any] = {
        "tier": detected.tier,
        "annotationStatus": detected.annotation_status,
        "source": detected.source,
        "generatedAt": datetime.now(timezone.utc).isoformat(),
    }
    if isinstance(document.get("schemaVersion"), int):
        metadata["aimetaSchemaVersion"] = document["schemaVersion"]
    if detected.detail:
        metadata["annotationDetail"] = detected.detail
    if document.get("process"):
        metadata["process"] = _project_process(document["process"])
    return {
        "$schema": CONTRACT_SCHEMA_URI,
        "schemaVersion": CONTRACT_SCHEMA_VERSION,
        "status": "success",
        "metadata": metadata,
        "tables": [],
        "references": [],
        "functions": [],
    }


def error_document(
    detected: MetadataDetection, message: str, *, status: str = "error"
) -> dict[str, Any]:
    result = _base(detected)
    result["status"] = status
    result["message"] = message
    return result


def unexpected_error_document(message: str) -> dict[str, Any]:
    """Return a schema-valid error when discovery failed before it could classify the host."""
    detected = MetadataDetection(None, 1, "error", "native", message)
    return error_document(detected, message)


def is_internal_table_name(name: str) -> bool:
    return name.endswith(_INTERNAL_TABLE_SUFFIXES)


def live_table_names(conn) -> list[str]:
    return [str(_decode(name)) for name in conn.tables(None).py()]


def columns_from_meta(conn, table: str) -> list[dict[str, Any]]:
    rows = _loads(conn(_META, table).py())
    columns = []
    for row in rows:
        column: dict[str, Any] = {"name": row.get("c"), "kdbType": row.get("t")}
        if row.get("f"):
            column["foreignRef"] = row["f"]
        if row.get("a"):
            column["attributes"] = [row["a"]]
        columns.append(column)
    return columns


def table_entry_from_meta(conn, table: str) -> dict[str, Any]:
    try:
        columns = columns_from_meta(conn, table)
    except Exception as error:
        logger.warning(f"Could not read meta for table '{table}': {error}")
        columns = []
    return {"name": table, "private": False, "desc": "", "columns": columns}


def _vector_columns(table: str, config: KDBConfig) -> list[str]:
    """Embedding columns to exclude from previews, resolved from this backend's config."""
    try:
        rows = get_csv_data(config.embedding_csv_path)
        matching = rows[rows["table"] == table]
        if len(matching) != 1:
            return []
        row = matching.iloc[0]
        embedding = row["embedding_column"]
        sparse = row["sparse_embedding_column"]
    except Exception as error:
        logger.debug(f"No embedding config for table {table}: {error}")
        return []
    return [column for column in (embedding, sparse) if column]


def _live_block(
    conn, table: str, counts: dict, partitioned: set[str], rows: int, config: KDBConfig
) -> dict[str, Any]:
    row_count = counts.get(table)
    live: dict[str, Any] = {
        "rowCount": row_count,
        "partitioned": table in partitioned,
        "rowSource": "none",
    }
    if not rows or not row_count:
        return live
    query = _PREVIEW_PARTITIONED if table in partitioned else _PREVIEW
    try:
        preview = _loads(
            conn(
                query, min(rows, row_count), table, _vector_columns(table, config)
            ).py()
        )
        if preview:
            live["preview"] = preview
            live["rowSource"] = "preview"
    except Exception as error:
        logger.warning(f"Could not preview table '{table}': {error}")
    return live


def _reconcile_row_data(entry: dict[str, Any], live: dict[str, Any]) -> None:
    if live.get("preview"):
        entry.pop("sampleData", None)
    elif entry.get("sampleData"):
        live["rowSource"] = "sampleData"


def _entitled_names(
    config: KDBConfig, names: Sequence[str]
) -> tuple[list[str], int, str | None]:
    ordered = sorted(dict.fromkeys(names))
    if not config.data_gate or not ordered:
        return ordered, 0, None
    decision = consult_data_gate("read", ordered)
    if not decision.allowed:
        return (
            [],
            len(ordered),
            decision.reason or "not entitled to read the requested metadata",
        )
    entitled = decision.obligations.get("entitled")
    if entitled is None:
        return ordered, 0, None
    allowed = [name for name in ordered if name in set(entitled)]
    return allowed, len(ordered) - len(allowed), None


def _visible_references(
    references: Sequence[dict[str, Any]],
    tables: Sequence[dict[str, Any]],
    visible_names: set[str],
) -> list[dict[str, Any]]:
    semantic_types = {
        column.get("semanticType")
        for table in tables
        for column in table.get("columns") or []
        if column.get("semanticType")
    }
    return [
        _project_reference(reference)
        for reference in references
        if reference.get("table") in visible_names
        and reference.get("semanticType") in semantic_types
    ]


def _visible_functions(
    functions: Sequence[dict[str, Any]], visible_names: set[str]
) -> list[dict[str, Any]]:
    return [
        _project_function(function)
        for function in functions
        if set(function.get("uses") or []).issubset(visible_names)
    ]


def build_tables_document(
    *,
    config: KDBConfig | None = None,
    cache: MetadataCache | None = None,
    table: str | None = None,
    preview_rows: int = DEFAULT_PREVIEW_ROWS,
) -> dict[str, Any]:
    if not 0 <= preview_rows <= MAX_PREVIEW_ROWS:
        raise ValueError(f"preview_rows must be between 0 and {MAX_PREVIEW_ROWS}")
    cfg = _config(config)
    conn = get_kdb_connection(cfg)
    detected = detect_metadata(conn=conn, config=cfg, cache=cache)
    result = _base(detected)
    document = detected.document if detected.tier >= 2 else None
    by_name = {
        entry.get("name"): entry for entry in (document or {}).get("tables") or []
    }
    live_names = live_table_names(conn)

    if document is None:
        live_names = [name for name in live_names if not is_internal_table_name(name)]
    else:
        live_names = [
            name for name in live_names if not (by_name.get(name) or {}).get("private")
        ]

    if table is not None:
        if table not in live_names:
            return error_document(detected, f"Table '{table}' not found")
        names = [table]
        selected = by_name.get(table) or {}
        semantic_types = {
            column.get("semanticType")
            for column in selected.get("columns") or []
            if column.get("semanticType")
        }
        resolver_names = [
            reference.get("table")
            for reference in (document or {}).get("references") or []
            if reference.get("semanticType") in semantic_types
            and reference.get("table") in live_names
        ]
        authorization_names = names + resolver_names
    else:
        names = live_names
        authorization_names = names

    authorized, hidden, denied = _entitled_names(cfg, authorization_names)
    visible = [name for name in names if name in set(authorized)]
    if denied or (names and not visible):
        return error_document(
            detected,
            denied or "not entitled to read the requested table metadata",
            status="permission_denied",
        )
    result["metadata"]["hiddenByEntitlements"] = hidden

    counts: dict[str, Any] = {}
    partitioned: set[str] = set()
    if visible:
        try:
            counts = _loads(conn(_ROW_COUNTS, visible).py())
        except Exception as error:
            logger.warning(f"Could not read row counts: {error}")
        try:
            partitioned = {str(_decode(name)) for name in (conn(".Q.pt").py() or [])}
        except Exception as error:
            logger.debug(f"Could not read .Q.pt: {error}")

    tables = []
    for name in visible:
        annotated = by_name.get(name)
        entry: dict[str, Any] = (
            _project_table(annotated)
            if annotated is not None
            else table_entry_from_meta(conn, name)
        )
        live = _live_block(conn, name, counts, partitioned, preview_rows, cfg)
        _reconcile_row_data(entry, live)
        entry["live"] = live
        tables.append(entry)
    result["tables"] = tables
    result["references"] = _visible_references(
        (document or {}).get("references") or [], tables, set(authorized)
    )
    return result


def build_functions_document(
    *,
    config: KDBConfig | None = None,
    cache: MetadataCache | None = None,
    function: str | None = None,
) -> dict[str, Any]:
    cfg = _config(config)
    conn = get_kdb_connection(cfg)
    detected = detect_metadata(conn=conn, config=cfg, cache=cache)
    result = _base(detected)
    document = detected.document if detected.tier >= 2 else None
    functions = (document or {}).get("functions") or []
    if function is not None:
        functions = [entry for entry in functions if entry.get("name") == function]
        if not functions:
            return error_document(detected, f"Function '{function}' not found")

    live_names = live_table_names(conn)
    visible, hidden, denied = _entitled_names(cfg, live_names)
    if denied:
        return error_document(detected, denied, status="permission_denied")
    filtered = _visible_functions(functions, set(visible))
    if function is not None and not filtered:
        uses = set(functions[0].get("uses") or [])
        missing = sorted(uses - set(live_names))
        if missing and (uses & set(live_names)) <= set(visible):
            return error_document(
                detected,
                f"Function '{function}' metadata references tables not present on the "
                "host: " + ", ".join(missing) + "; recompile and refresh the annotations",
            )
        return error_document(
            detected,
            "not authorized to inspect the requested function metadata",
            status="permission_denied",
        )
    result["metadata"]["hiddenByEntitlements"] = hidden
    result["functions"] = filtered
    if not filtered and detected.tier < 3:
        result["message"] = "Annotated public functions require aimeta metadata"
    return result
