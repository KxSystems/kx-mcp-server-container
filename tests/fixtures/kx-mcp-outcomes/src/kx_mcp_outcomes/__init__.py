"""A fixture bundle with one tool per dispatch outcome, and an authz adapter it registers on import.

Dependency-free (no pykx, no license), so the launcher tests that need these run in CI alongside the
``example`` fixture:

* ``ok`` / ``failed`` (returns ``isError: true``) / ``raises`` / ``denied`` (an ``@authorize`` gate)
  drive the audit-vs-metric outcome parity test over the wire;
* ``IMPORT_TIME_ADAPTER`` registers the way ``kx_mcp_kdbx`` registers ``kdbx_rbac``: as a side effect
  of importing the bundle, which the launcher does only once it starts mounting. It denies
  everything; only its registration timing matters.
"""

from kx_auth_core.authz import AuthzDecision, register_authz_adapter

from .server import build_server

IMPORT_TIME_ADAPTER = "outcomes_import_time"

register_authz_adapter(
    IMPORT_TIME_ADAPTER,
    lambda request: AuthzDecision(allowed=False, adapter=IMPORT_TIME_ADAPTER, reason="fixture"),
)

__all__ = ["build_server", "IMPORT_TIME_ADAPTER"]
