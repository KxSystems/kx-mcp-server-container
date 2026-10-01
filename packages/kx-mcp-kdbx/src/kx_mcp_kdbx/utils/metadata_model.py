"""Build the versioned MCP metadata projection from aimeta and live KDB-X state."""

from __future__ import annotations

import copy
import json
import logging
from datetime import datetime, timezone
from typing import Any, Sequence

from kx_mcp_kdbx.settings import KDBConfig
from kx_mcp_kdbx.utils.observe import q
from kx_mcp_kdbx.utils import kdbx as kdbx_utils
from kx_mcp_kdbx.utils.aimeta import MetadataCache, MetadataDetection, detect_metadata
from kx_mcp_kdbx.utils.authz_kx_entitlements import consult_data_gate
from kx_mcp_kdbx.utils.denial import record_denial
from kx_mcp_kdbx.utils.embeddings_helpers import get_csv_data
from kx_mcp_kdbx.utils.kdbx import get_kdb_connection
from kx_mcp_kdbx.utils.non_finite import NON_FINITE_ERROR_TYPE, has_non_finite

logger = logging.getLogger(__name__)

# The contract URI names the backend type, not the mount: a backend mounted under a non-default
# namespace serves its schema resource elsewhere, but documents still declare this global URI.
CONTRACT_SCHEMA_URI = "schema://kdbx/metadata/v1"
CONTRACT_SCHEMA_VERSION = 1
DEFAULT_PREVIEW_ROWS = 3
MAX_PREVIEW_ROWS = 100

# Previews are the one remaining PER-TABLE round trip, and nothing bounded the table count: a host
# with 500 tables meant ~500 sequential `.j.j`-serialised preview queries per uncached call, each
# parsed in Python. `MAX_PREVIEW_ROWS` capped the rows, never the tables. Tables past this cap still
# appear in full — name, columns, row count, partitioned flag — they just carry no sample rows, which
# `live.rowSource == "none"` already expresses, so no contract change. A caller that wants a specific
# table's sample asks for that table (`table=` requests are never capped). A preview that was
# attempted and FAILED is a different state: `rowSource: "error"` plus `live.previewError`.
MAX_PREVIEW_TABLES = 20
_INTERNAL_TABLE_SUFFIXES = ("document", "stats", "token")

_ROW_COUNTS = "{.j.j x!@[{count get x};;0Nj] each x}"
_META = "{.j.j 0!meta x}"
# One round trip for EVERY un-annotated table's meta, instead of one per table. Same `@[f;;fallback]
# each` trap idiom as _ROW_COUNTS above, so a single unreadable table yields an empty column list
# rather than losing the whole batch.
_META_BATCH = "{.j.j x!@[{0!meta x};;()] each x}"
# `0!` unkeys a keyed table first: `cols#` on a keyed table signals 'length, so without it a keyed
# table never previewed.
_PREVIEW = "{[n;t;d] r:n sublist 0!get t; .j.j ((cols r) except d)#r}"
_PREVIEW_PARTITIONED = "{[n;t;d] r:.Q.ind[get t;til n]; .j.j ((cols r) except d)#r}"
# The same previews with +/-infinity in float and real columns replaced by that type's null, which
# `.j.j` emits as JSON null. Issued only after the plain preview failed on a bare `inf` token, so a
# finite table keeps its single round trip. A nested float column (a list per cell) is not reached,
# and still fails as `non_finite_number`.
_FINITE = "@[r;where(type each flip r)in 8 9h;{@[x;where 0w=abs x;:;first 0#x]}]"
_PREVIEW_FINITE = "{[n;t;d] r:n sublist 0!get t; r:((cols r) except d)#r; .j.j " + _FINITE + "}"
_PREVIEW_PARTITIONED_FINITE = (
    "{[n;t;d] r:.Q.ind[get t;til n]; r:((cols r) except d)#r; .j.j " + _FINITE + "}"
)


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
        for key in ("name", "desc", "examples", "uses", "authorize", "tags")
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
    """A schema-valid metadata document reporting a failure, per the v1 response contract.

    A ``permission_denied`` status is *also* stamped with ``stamp_authz_decision``, so a metadata
    dispatch refused by entitlements is counted ``denied`` rather than ``error`` — the same
    treatment ``denial_response`` gives a q-side refusal. This matters beyond bookkeeping on the
    partial-entitlement path: ``consult_data_gate`` records an *allow*-with-obligations there, so
    without this stamp a document that denies the caller would ride out under that allow.
    """
    result = _base(detected)
    result["status"] = status
    result["message"] = message
    if status == "permission_denied":
        record_denial(message, adapter="kdbx_entitlements")
    return result


def unexpected_error_document(message: str) -> dict[str, Any]:
    """Return a schema-valid error when discovery failed before it could classify the host."""
    detected = MetadataDetection(None, 1, "error", "native", message)
    return error_document(detected, message)


def is_internal_table_name(name: str) -> bool:
    return name.endswith(_INTERNAL_TABLE_SUFFIXES)


def live_table_names(conn) -> list[str]:
    # A plain `tables[]` query, not PyKX's `conn.tables(None)`: that helper first sends its own
    # is-this-a-function probe, so it was two round trips on the wire counted as one.
    names = q(conn, "tables", "tables[]").py()
    return [str(_decode(name)) for name in names]


def columns_from_meta(conn, table: str) -> list[dict[str, Any]]:
    return _columns_from_meta_rows(_loads(q(conn, "meta", _META, table).py()))


def _columns_from_meta_rows(rows) -> list[dict[str, Any]]:
    """Project one table's `0!meta` rows into contract columns. Shared by the single-table query and
    the batched one, so the two cannot drift in how they read a meta row."""
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
    return _entry_from_columns(table, columns)


def _entry_from_columns(table: str, columns: list[dict[str, Any]]) -> dict[str, Any]:
    return {"name": table, "private": False, "desc": "", "columns": columns}


def _batch_columns_from_meta(conn, tables: list) -> dict:
    """`{table: columns}` for `tables` in ONE round trip; `{}` if the batch itself fails.

    An empty return makes the caller fall back to its per-table path, so a q build that dislikes the
    batched form degrades in cost, never in correctness.
    """
    if not tables:
        return {}
    try:
        # Through `q()` like every other round trip, so it is counted and spanned as `meta`; a bare
        # `conn(...)` here left the batch out of `kdbx_qipc_calls_total` entirely.
        batched = _loads(q(conn, "meta", _META_BATCH, tables).py())
    except Exception as error:
        logger.warning(f"Could not batch-read meta for {len(tables)} tables: {error}")
        return {}
    columns = {}
    for table in tables:
        rows = batched.get(table)
        if not rows:
            continue  # unreadable table: leave it out so the caller retries it individually
        try:
            columns[table] = _columns_from_meta_rows(rows)
        except Exception as error:
            logger.warning(f"Could not project meta for table '{table}': {error}")
    return columns


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
    is_partitioned = table in partitioned
    args = (min(rows, row_count), table, _vector_columns(table, config))
    try:
        raw = q(conn, "preview", _PREVIEW_PARTITIONED if is_partitioned else _PREVIEW, *args).py()
        try:
            preview = _loads(raw)
        except ValueError:
            if not has_non_finite(raw):
                raise
            # `.j.j` wrote bare `inf`: re-read with infinities projected to null, and flag it, since
            # a projected null is otherwise indistinguishable from a q null.
            finite = _PREVIEW_PARTITIONED_FINITE if is_partitioned else _PREVIEW_FINITE
            raw = q(conn, "preview", finite, *args).py()
            try:
                preview = _loads(raw)
            except ValueError as error:
                if not has_non_finite(raw):
                    raise
                return _preview_failed(
                    live,
                    table,
                    NON_FINITE_ERROR_TYPE,
                    "the rows hold non-finite floats (infinity) that could not be projected to "
                    f"null, e.g. inside a nested column: {error}",
                )
            live["nonFiniteAsNull"] = True
        if preview:
            live["preview"] = preview
            live["rowSource"] = "preview"
    except Exception as error:
        return _preview_failed(live, table, "preview_failed", str(error))
    return live


def _preview_failed(live: dict[str, Any], table: str, code: str, message: str) -> dict[str, Any]:
    """Report a failed preview in the document, not only in the server log.

    It used to be logged and dropped, leaving `rowSource: "none"`, which reads as "past the preview
    cap": a silent success for a table the caller asked for by name. `_reconcile_row_data` may still
    fall back to annotated `sampleData`; `previewError` stays either way.
    """
    logger.warning(f"Could not preview table '{table}': {message}")
    live["rowSource"] = "error"
    live["previewError"] = {"code": code, "message": message}
    return live


def _reconcile_row_data(entry: dict[str, Any], live: dict[str, Any]) -> None:
    if live.get("preview"):
        entry.pop("sampleData", None)
    elif entry.get("sampleData"):
        live["rowSource"] = "sampleData"  # also after a failed preview; `previewError` says why


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
            counts = _loads(q(conn, "rowcounts", _ROW_COUNTS, visible).py())
        except Exception as error:
            logger.warning(f"Could not read row counts: {error}")
        try:
            partitioned = {str(_decode(name)) for name in (q(conn, "partitioned", ".Q.pt").py() or [])}
        except Exception as error:
            logger.debug(f"Could not read .Q.pt: {error}")

    # One batched meta call for every un-annotated table, rather than one call each.
    unannotated = [name for name in visible if by_name.get(name) is None]
    batched_columns = _batch_columns_from_meta(conn, unannotated)

    # Cap the per-table preview leg. `visible` is already deterministic, so the previewed subset is
    # stable across calls rather than varying run to run.
    previewable = set(visible[:MAX_PREVIEW_TABLES])
    if len(visible) > MAX_PREVIEW_TABLES:
        logger.info(
            f"{len(visible)} tables visible; sampling rows for the first {MAX_PREVIEW_TABLES}. "
            "Request a table by name for its sample rows."
        )

    tables = []
    for name in visible:
        annotated = by_name.get(name)
        if annotated is not None:
            entry: dict[str, Any] = _project_table(annotated)
        elif name in batched_columns:
            entry = _entry_from_columns(name, batched_columns[name])
        else:
            entry = table_entry_from_meta(conn, name)  # batch missed it — read this one directly
        rows_for_table = preview_rows if name in previewable else 0
        live = _live_block(conn, name, counts, partitioned, rows_for_table, cfg)
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
