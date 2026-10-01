"""Container logging: the self-maintaining brand-namespace handler.

`configure_logging` installs a single filtered handler on the *root* logger rather than per-tree
handlers, because the container and its bundles are separate distributions with underscore import
names (``kx_mcp_core``, ``kx_mcp_kdbai``, …) whose ``getLogger(__name__)`` loggers are unrelated
*sibling* trees — ``kx_mcp_kdbai`` is not a child of ``kx_mcp``. These tests pin that one handler:
surfaces every ``kx_mcp*`` logger (dotted child *and* underscore sibling), filters third-party noise,
and is idempotent. This is the guarantee that would have caught the original "bundle pre-flight lines
are invisible" bug, and it covers any future ``kx_mcp_*`` backend for free.
"""

from __future__ import annotations

import logging

import pytest

from kx_mcp_core.logging import _HANDLER_TAG, _BrandFilter, configure_logging


@pytest.fixture
def fresh_root_logging():
    """Isolate the root logger so each test sees a clean slate and the suite is unaffected after.

    Strips any previously-installed tagged handler (so ``configure_logging`` installs a fresh one
    bound to the test's captured stderr), and restores the root handlers + level on teardown.
    """
    root = logging.getLogger()
    saved_handlers = root.handlers[:]
    saved_level = root.level
    root.handlers = [h for h in saved_handlers if not getattr(h, _HANDLER_TAG, False)]
    try:
        yield
    finally:
        root.handlers = saved_handlers
        root.setLevel(saved_level)


@pytest.mark.parametrize(
    "name, admitted",
    [
        ("kx_mcp", True),  # the brand root itself
        ("kx_mcp.audit", True),  # dotted child — the audit line
        ("kx_mcp_core.assembly", True),  # container internals (underscore sibling)
        ("kx_mcp_acme.server", True),  # downstream bundle pre-flight (underscore sibling)
        ("kx_mcp_kdbx.utils.kdbx", True),  # kdb-x bundle (underscore sibling)
        ("kx_mcp_kdbai.utils.kdbai_auth", True),  # KDB.AI bundle (underscore sibling)
        ("httpx", False),  # third party
        ("uvicorn.access", False),  # third party
        ("root", False),  # not in the brand namespace
    ],
)
def test_brand_filter_admits_brand_rejects_others(name, admitted):
    record = logging.LogRecord(name, logging.INFO, __file__, 0, "msg", None, None)
    assert _BrandFilter().filter(record) is admitted


def test_configure_logging_surfaces_sibling_and_audit_but_filters_third_party(capsys, fresh_root_logging):
    configure_logging("INFO")

    logging.getLogger("kx_mcp_kdbai.server").info("PREFLIGHT-OK")  # underscore sibling tree
    logging.getLogger("kx_mcp.audit").info("AUDIT-LINE")  # dotted child
    logging.getLogger("httpx").info("THIRD-PARTY-NOISE")  # must be created+propagated yet filtered out

    err = capsys.readouterr().err
    assert "PREFLIGHT-OK" in err, err  # the original bug: bundle sibling logs were dropped
    assert "AUDIT-LINE" in err, err
    assert "THIRD-PARTY-NOISE" not in err, err  # the filter keeps httpx/uvicorn quiet


def test_configure_logging_sets_root_level_so_brand_info_is_created(fresh_root_logging):
    logging.getLogger().setLevel(logging.WARNING)  # default-ish: would drop sibling-tree INFO
    configure_logging("INFO")
    assert logging.getLogger().level <= logging.INFO


def test_configure_logging_is_idempotent(fresh_root_logging):
    configure_logging("INFO")
    configure_logging("INFO")
    tagged = [h for h in logging.getLogger().handlers if getattr(h, _HANDLER_TAG, False)]
    assert len(tagged) == 1


@pytest.mark.parametrize(
    "name, level, admitted",
    [
        # the brand namespace passes at every level (the handler's own level does the gating)
        ("kx_mcp.audit", logging.INFO, True),
        ("kx_mcp_kdbx.utils.kdbx", logging.DEBUG, True),
        # the OpenTelemetry tree passes only at WARNING and above: export failures, not chatter
        ("opentelemetry.exporter.otlp.proto.grpc.exporter", logging.WARNING, True),
        ("opentelemetry.sdk.trace.export", logging.ERROR, True),
        ("opentelemetry", logging.CRITICAL, True),
        ("opentelemetry.exporter.otlp.proto.grpc.exporter", logging.INFO, False),
        ("opentelemetry.sdk.trace.export", logging.DEBUG, False),
        # everything else stays dropped, whatever the level, including look-alike prefixes
        ("opentelemetry_extra", logging.WARNING, False),
        ("httpx", logging.WARNING, False),
        ("uvicorn.error", logging.ERROR, False),
        ("root", logging.CRITICAL, False),
    ],
)
def test_brand_filter_policy_table(name, level, admitted):
    """Which namespaces and levels reach the handler. OTel warnings are the operator's only sign
    spans are being dropped, so they must pass; third-party noise must not."""
    record = logging.LogRecord(name, level, __file__, 0, "msg", None, None)
    assert _BrandFilter().filter(record) is admitted
