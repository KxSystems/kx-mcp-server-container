"""Discover, validate, normalize, and cache aimeta metadata over qIPC.

The cache is owned by a mounted FastMCP instance. Raw metadata may be shared by requests on that
instance, but authorization filtering is deliberately performed later, per request.
"""

from __future__ import annotations

import copy
import json
import logging
import threading
import time
from dataclasses import dataclass
from functools import lru_cache
from importlib.resources import files
from typing import Any

from jsonschema import Draft202012Validator  # type: ignore[import-untyped]

from kx_mcp_kdbx.settings import KDBConfig

logger = logging.getLogger(__name__)

SUPPORTED_AIMETA_SCHEMA_VERSION = 2
AIMETA_PRESENCE_PROBE = "@[{100h~type get x};`.aimeta.data;{0b}]"

_TABLE_OPTIONAL = frozenset({"reference", "labels", "sampleData", "tags"})
_COLUMN_OPTIONAL = frozenset(
    {"desc", "semanticType", "foreignRef", "cardinality", "attributes", "label"}
)


@dataclass(frozen=True)
class MetadataDetection:
    document: dict[str, Any] | None
    tier: int
    annotation_status: str
    source: str
    detail: str | None = None


class MetadataCache:
    """Thread-safe cache scoped to one mounted kdb-x backend."""

    def __init__(self, ttl: int = 300):
        self.ttl = ttl
        self._lock = threading.Lock()
        self._value: MetadataDetection | None = None
        self._fetched_at = 0.0

    def get(self) -> MetadataDetection | None:
        with self._lock:
            if self.ttl == 0 or not self._fetched_at:
                return None
            if time.monotonic() - self._fetched_at >= self.ttl:
                return None
            return self._value

    def put(self, value: MetadataDetection) -> None:
        with self._lock:
            self._value = copy.deepcopy(value)
            self._fetched_at = time.monotonic()

    def peek(self) -> MetadataDetection | None:
        with self._lock:
            return copy.deepcopy(self._value)


class MetadataDocumentError(ValueError):
    """The aimeta endpoint resolved but did not return a parseable document."""


def _is_empty(value: Any) -> bool:
    # `False` counts as padding: the only optional boolean is a column's `label`, whose schema is
    # const-true / field-omitted — a hydrated `label: false` would otherwise invalidate the document.
    if value is None or value is False or value == "" or value == [] or value == {}:
        return True
    return isinstance(value, list) and all(item in ("", None) for item in value)


def scrub_ipc_artifacts(document: dict[str, Any]) -> dict[str, Any]:
    """Remove empty optional fields introduced by qIPC uniform-table hydration."""
    scrubbed = copy.deepcopy(document)
    tables = []
    for table in scrubbed.get("tables") or []:
        entry = {
            key: value
            for key, value in table.items()
            if not (key in _TABLE_OPTIONAL and _is_empty(value))
        }
        entry["columns"] = [
            {
                key: value
                for key, value in column.items()
                if not (key in _COLUMN_OPTIONAL and _is_empty(value))
            }
            for column in table.get("columns") or []
        ]
        tables.append(entry)
    if "tables" in scrubbed:
        scrubbed["tables"] = tables
    return scrubbed


@lru_cache(maxsize=1)
def _schema() -> dict[str, Any]:
    path = files("kx_mcp_kdbx.schemas").joinpath("aimeta-v2.schema.json")
    return json.loads(path.read_text(encoding="utf-8"))


def _rich_annotation_content(document: dict[str, Any]) -> bool:
    for table in document.get("tables") or []:
        if any(
            table.get(key)
            for key in ("desc", "reference", "labels", "sampleData", "tags")
        ):
            return True
        for column in table.get("columns") or []:
            if any(
                column.get(key)
                for key in (
                    "desc",
                    "semanticType",
                    "foreignRef",
                    "cardinality",
                    "label",
                )
            ):
                return True
    for function in document.get("functions") or []:
        returns = function.get("returns") or {}
        params = function.get("params") or []
        if (
            function.get("desc")
            or any(param.get("type") or param.get("desc") for param in params)
            or returns.get("type")
            or returns.get("desc")
            or function.get("examples")
            or function.get("uses")
            or function.get("tags")
        ):
            return True
    return False


def classify_document(document: dict[str, Any] | None) -> MetadataDetection:
    """Classify metadata richness independently from aimeta's transport-tier convention."""
    if document is None:
        return MetadataDetection(None, 1, "unavailable", "native")

    version = document.get("schemaVersion")
    if version != SUPPORTED_AIMETA_SCHEMA_VERSION:
        return MetadataDetection(
            None,
            1,
            "unsupported_schema",
            "native",
            f"aimeta schemaVersion {version!r} is unsupported; expected 2",
        )

    errors = sorted(
        Draft202012Validator(_schema()).iter_errors(document),
        key=lambda e: list(e.path),
    )
    if errors:
        first = errors[0]
        path = ".".join(str(part) for part in first.path) or "$"
        return MetadataDetection(
            None,
            1,
            "invalid",
            "native",
            f"aimeta document failed validation at {path}: {first.message}",
        )

    status = (document.get("process") or {}).get("compileStatus")
    rich = _rich_annotation_content(document)
    if rich:
        annotation_status = (
            "compile_failed_stale" if status == "failed" else "annotated"
        )
        return MetadataDetection(document, 3, annotation_status, "aimeta_qipc")
    if status == "failed":
        return MetadataDetection(document, 1, "compile_failed", "aimeta_qipc")
    if status == "empty":
        return MetadataDetection(document, 1, "empty", "aimeta_qipc")
    return MetadataDetection(document, 2, "basic", "aimeta_qipc")


def _fetch_ipc(conn) -> dict[str, Any] | None:
    try:
        if not conn(AIMETA_PRESENCE_PROBE).py():
            return None
        raw = conn(".j.j .aimeta.data[]").py()
    except Exception as error:
        logger.debug(f"aimeta qIPC fetch failed: {error}")
        return None
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    try:
        return scrub_ipc_artifacts(json.loads(raw))
    except Exception as error:
        raise MetadataDocumentError(
            f"aimeta qIPC returned malformed JSON: {error}"
        ) from error


def detect_metadata(
    *,
    conn=None,
    config: KDBConfig | None = None,
    cache: MetadataCache | None = None,
    force: bool = False,
) -> MetadataDetection:
    """Return the cached or freshly detected metadata capability for this backend instance."""
    if cache is not None and not force:
        cached = cache.get()
        if cached is not None:
            return cached

    if conn is None:
        from kx_mcp_kdbx.utils.kdbx import get_kdb_connection

        conn = get_kdb_connection(config)
    try:
        detected = classify_document(_fetch_ipc(conn))
    except MetadataDocumentError as error:
        detected = MetadataDetection(None, 1, "invalid", "native", str(error))
    if detected.detail:
        logger.warning(detected.detail)
    if cache is not None:
        cache.put(detected)
    return detected


def reload_remote_metadata(
    *, conn, cache: MetadataCache | None
) -> tuple[bool, MetadataDetection]:
    """Reload aimeta and replace the cache only when the refreshed document is usable."""
    previous = cache.peek() if cache is not None else None
    try:
        if not conn(AIMETA_PRESENCE_PROBE).py():
            return False, classify_document(None)
        conn(".aimeta.reload[]")
        try:
            refreshed = classify_document(_fetch_ipc(conn))
        except MetadataDocumentError as error:
            refreshed = MetadataDetection(None, 1, "invalid", "native", str(error))
        if refreshed.document is None:
            if cache is not None and previous is not None:
                cache.put(previous)
            return True, refreshed
        if cache is not None:
            cache.put(refreshed)
        return True, refreshed
    except Exception as error:
        if cache is not None and previous is not None:
            cache.put(previous)
        return False, MetadataDetection(None, 1, "reload_failed", "native", str(error))


def metadata_guidance(detected: MetadataDetection) -> str:
    if detected.tier == 3:
        document = detected.document or {}
        return (
            "KDB-X aimeta check: SUCCESS - annotated metadata available "
            f"({len(document.get('tables') or [])} tables, "
            f"{len(document.get('functions') or [])} functions, "
            f"{len(document.get('references') or [])} references)"
        )
    if detected.tier == 2:
        return "KDB-X aimeta check: PARTIAL - basic metadata is available without semantic annotations"
    suffix = f" ({detected.detail})" if detected.detail else ""
    return (
        "KDB-X aimeta check: DEGRADED - using native table and column introspection; "
        "load and annotate kx.aimeta for semantic metadata" + suffix
    )
