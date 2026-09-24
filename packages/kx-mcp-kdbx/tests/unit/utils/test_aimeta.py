import copy
import json

from kx_mcp_kdbx.utils.aimeta import (
    MetadataCache,
    classify_document,
    detect_metadata,
    reload_remote_metadata,
    scrub_ipc_artifacts,
)


def _document(*, rich=True, compile_status=None):
    process = {"name": "demo"}
    if compile_status:
        process["compileStatus"] = compile_status
        process["warnings"] = ["compile warning"]
    table = {
        "name": "trades",
        "private": False,
        "desc": "Trade tape" if rich else "",
        "columns": [
            {
                "name": "sym",
                "kdbType": "s",
                **(
                    {"semanticType": "instrument", "desc": "Instrument"} if rich else {}
                ),
            }
        ],
    }
    return {
        "schemaVersion": 2,
        "compilerVersion": "0.1.0",
        "process": process,
        "tables": [table],
        "references": [],
        "functions": [],
    }


def test_richness_tier_is_not_compile_status_only():
    assert classify_document(_document(rich=True)).tier == 3
    assert classify_document(_document(rich=False)).tier == 2
    failed_but_rich = classify_document(_document(rich=True, compile_status="failed"))
    assert failed_but_rich.tier == 3
    assert failed_but_rich.annotation_status == "compile_failed_stale"
    failed_native = classify_document(_document(rich=False, compile_status="failed"))
    assert failed_native.tier == 1


def test_empty_required_function_shape_does_not_fake_annotation_richness():
    document = _document(rich=False)
    document["functions"] = [
        {
            "name": ".basic.fn",
            "desc": "",
            "params": [],
            "returns": {"type": "", "desc": ""},
            "examples": [],
            "uses": [],
            "tags": [],
        }
    ]
    assert classify_document(document).tier == 2


def test_invalid_and_future_documents_fall_back_to_native():
    invalid = _document()
    invalid.pop("tables")
    assert classify_document(invalid).annotation_status == "invalid"
    future = {**_document(), "schemaVersion": 99}
    assert classify_document(future).annotation_status == "unsupported_schema"


def test_qipc_padding_is_scrubbed_without_mutating_input():
    document = _document()
    document["tables"][0].update({"reference": "", "labels": [""], "sampleData": None})
    document["tables"][0]["columns"][0].update({"foreignRef": "", "label": False})
    original = copy.deepcopy(document)

    scrubbed = scrub_ipc_artifacts(document)

    assert scrubbed["tables"][0] == {
        "name": "trades",
        "private": False,
        "desc": "Trade tape",
        "columns": [
            {
                "name": "sym",
                "kdbType": "s",
                "semanticType": "instrument",
                "desc": "Instrument",
            }
        ],
    }
    assert document == original


class _Result:
    def __init__(self, value):
        self.value = value

    def py(self):
        return self.value


class _Conn:
    def __init__(self, document):
        self.document = document
        self.fetches = 0

    def __call__(self, query, *args):
        if query.startswith("@["):
            return _Result(True)
        if query == ".j.j .aimeta.data[]":
            self.fetches += 1
            return _Result(json.dumps(self.document).encode())
        if query == ".aimeta.reload[]":
            return _Result(None)
        raise AssertionError(query)


def test_cache_is_explicit_and_instance_scoped():
    conn = _Conn(_document())
    cache_a = MetadataCache(300)
    cache_b = MetadataCache(300)

    detect_metadata(conn=conn, cache=cache_a)
    detect_metadata(conn=conn, cache=cache_a)
    assert conn.fetches == 1
    detect_metadata(conn=conn, cache=cache_b)
    assert conn.fetches == 2


def test_zero_ttl_disables_caching():
    conn = _Conn(_document())
    cache = MetadataCache(0)
    detect_metadata(conn=conn, cache=cache)
    detect_metadata(conn=conn, cache=cache)
    assert conn.fetches == 2


def test_concurrent_cold_cache_requests_do_not_each_refetch():
    """REGRESSION (see mcp-container/adversarial-review-2026-08.md § kdb-x backend).

    detect_metadata's check-then-fetch-then-store sequence had no single-flight lock — N
    concurrent callers against a cold cache each paid their own full fetch instead of one winner +
    N waiters, a thundering herd at exactly the moment the cache exists to prevent one."""
    import threading
    import time

    class _SlowConn(_Conn):
        def __call__(self, query, *args):
            if query == ".j.j .aimeta.data[]":
                time.sleep(0.05)
            return super().__call__(query, *args)

    conn = _SlowConn(_document())
    cache = MetadataCache(300)
    barrier = threading.Barrier(20)

    def _go():
        barrier.wait()
        detect_metadata(conn=conn, cache=cache)

    threads = [threading.Thread(target=_go) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Exactly one: `MetadataCache.fill` holds a dedicated fill lock across check -> fetch -> store,
    # so the 19 losers re-check and hit the cache the winner just populated. Pinned at 1 rather than
    # a loose bound because any number above 1 means the coalescing is not actually happening.
    assert conn.fetches == 1, conn.fetches


def test_malformed_qipc_json_is_reported_as_invalid():
    class MalformedConn(_Conn):
        def __call__(self, query, *args):
            if query.startswith("@["):
                return _Result(True)
            if query == ".j.j .aimeta.data[]":
                return _Result(b"{not-json")
            raise AssertionError(query)

    conn = MalformedConn(_document())
    detected = detect_metadata(conn=conn, force=True)
    assert detected.annotation_status == "invalid"
    assert "malformed JSON" in (detected.detail or "")


def test_failed_refresh_preserves_last_valid_cache_entry():
    valid = classify_document(_document())
    cache = MetadataCache(300)
    cache.put(valid)
    invalid = _document()
    invalid.pop("tables")

    reloaded, detected = reload_remote_metadata(conn=_Conn(invalid), cache=cache)

    assert reloaded is True
    assert detected.annotation_status == "invalid"
    assert cache.peek() == valid
