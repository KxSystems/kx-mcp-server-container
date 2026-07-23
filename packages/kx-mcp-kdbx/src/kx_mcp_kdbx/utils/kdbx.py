import logging
from functools import lru_cache
import pykx as kx
from typing import Optional
from kx_auth_core import project_principal
from kx_mcp_kdbx.settings import KDBConfig

# Module-level DEFAULT config (env-driven), used only when no per-instance config is supplied.
# Decoupled from server.app_settings so the connection layer has no import-time dependency on the
# server module. The real per-backend config is threaded in from build_server() -> register_* ->
# the tool/resource closures -> here, which is what makes the bundle instance-safe: two kdb-x
# backends at different host+ports get distinct, independently-cached connections.
db_config = KDBConfig()
logger = logging.getLogger(__name__)


def _current_principal():
    """Read the validated inbound principal without hard-depending on the container package.

    Mirrors `kx_mcp_core.auth.current_principal`, but imports fastmcp's accessor directly so the
    kdb-x bundle stays runnable standalone. Returns None when there is no authenticated principal.
    """
    try:
        from fastmcp.server.dependencies import get_access_token
        return get_access_token()
    except Exception:
        return None


def _principal_key(principal) -> Optional[tuple]:
    """Hashable per-principal cache key: (subject, issuer). None when anonymous.

    fastmcp's `JWTVerifier` does not populate `AccessToken.subject` and folds the client id down to
    `azp` for the `client_id` field, so keying on `subject or client_id` collapses every user on one
    OAuth client to a single handle — defeating the per-principal isolation. Prefer the `sub` claim
    (the real user identity) and fall back to client_id only when it is genuinely absent.
    """
    if principal is None:
        return None
    claims = getattr(principal, "claims", None) or {}
    sub = getattr(principal, "subject", None) or claims.get("sub") or getattr(principal, "client_id", None)
    return (sub, claims.get("iss"))


def _charvec(obj):
    """Recursively wrap str leaves as q char vectors, so PyKX does not auto-symbolise them.

    Applied to the raw `claims` blob before binding: q symbols are never garbage-collected, so
    high-cardinality claim values (e.g. `jti`, unique per token) would grow the symbol table without
    bound if sent as Python `str` (PyKX maps `str` -> q symbol). As char vectors they never intern.
    The q side (`.kx.auth.promote`) symbolises only the *promoted* fields it extracts.
    """
    if isinstance(obj, str):
        return kx.CharVector(obj)
    if isinstance(obj, dict):
        return {k: _charvec(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_charvec(v) for v in obj]
    return obj


def _bind_principal(conn: kx.QConnection, principal) -> None:
    """Ferry the principal onto the (service-account) handle via `.kx.auth.bind`.

    Idempotent on a per-principal handle, so it is safe to re-issue per call (it also refreshes the
    bound expiry). When `principal` is None the handle is left unbound — the q-side default-deny then
    refuses queries, the intended signal that assertion is enabled without an inbound principal.

    The container only *ferries*: it sends the structured fields (sub from the `sub` claim, since
    fastmcp leaves `AccessToken.subject` unset) plus the raw `claims` (string values as char vectors
    to avoid symbol-interning). **Promotion — `groups`/`tenant` extraction + typing — happens q-side**
    in `.kx.auth.promote`, so qIPC and HTTP share one promotion path. The groups/tenant claim sources
    are configured on q (`.kx.auth.setClaims`), a host concern.
    """
    if principal is None:
        return
    claims = getattr(principal, "claims", None) or {}
    d = project_principal(
        subject=getattr(principal, "subject", None) or claims.get("sub"),
        client_id=getattr(principal, "client_id", None),
        scopes=getattr(principal, "scopes", None),
        expires_at=getattr(principal, "expires_at", None),
        audience=getattr(principal, "resource", None),
        claims=claims,
    )
    d["claims"] = _charvec(d["claims"])
    conn(".kx.auth.bind", d)


def get_kdb_connection(config: Optional[KDBConfig] = None) -> kx.QConnection:
    """Return a live connection for the given backend `config` (defaults to the module config).

    Connections are cached by `kdb_sync_connection` on `(config, principal_key)`, so distinct configs
    never share a connection (one process hosts multiple kdb-x backends) and — when identity
    assertion is on — each principal gets its own qIPC handle. When `config.assert_identity` is set,
    the validated inbound principal is projected and bound to the handle before it is returned.
    """
    cfg = config if config is not None else db_config
    assert_identity = bool(getattr(cfg, "assert_identity", False))
    principal = _current_principal() if assert_identity else None
    pkey = _principal_key(principal)
    try:
        conn = kdb_sync_connection(cfg, pkey)
        conn('') # check if conn is live for existing connection from cache
    except Exception as e:
        if "Attempted to use a closed IPC connection" in str(e):
            logger.warning("KDB-X connection was closed. Reinitializing...")
            cleanup_kdb_connection()
            conn = kdb_sync_connection(cfg, pkey)
            conn('')
        else:
            logger.error(f"Error in creating KDBX connection: {e}")
            raise
    if assert_identity:
        _bind_principal(conn, principal)
    return conn

@lru_cache()
def kdb_sync_connection(config: Optional[KDBConfig] = None, principal_key: Optional[tuple] = None) -> kx.QConnection:
    """Cached connection, keyed on `(config, principal_key)`.

    `principal_key` does not change the connection parameters — every handle authenticates with the
    same service-account credentials — but it partitions the cache so each asserted principal gets
    its OWN qIPC handle (its own `.z.w`), which is what makes the q-side per-handle bind stable and
    race-free. `None` (assertion off / anonymous) preserves the single shared connection.
    """
    if config is None:
        config = db_config

    logger.debug(f"KDBConfig: {config=}")
    logger.info(f"Connecting to KDB at {config.host}:{config.port}")
    retry = config.retry

    for attempt in range(1, retry + 1):
        try:
            conn = kx.SyncQConnection(
                host=config.host,
                port=config.port,
                username=config.username,
                password=config.password.get_secret_value(),
                timeout=config.timeout,
                reconnection_attempts=config.retry,
                tls=config.tls,
            )
            logger.info("Connected to Q/KDB-X")
            return conn
        except Exception as e:
            logger.warning(f"KDB-X connectivity attempt {attempt}/{retry} failed: {str(e)}")

    logger.error("Failed to connect to KDB")
    raise

def cleanup_kdb_connection():
    kdb_sync_connection.cache_clear()
    logger.info("KDBX connection cache cleared")


def config_from_ctx(ctx) -> Optional[KDBConfig]:
    """Resolve the per-instance KDBConfig the calling tool/resource belongs to.

    FastMCP injects a `Context`; under composition `ctx.fastmcp` is the **bundle's own** server (the
    mounted child, not the parent), on which `build_server()` stashed `_kdbx_config`. Reading it here
    means primitives pull their config from context at call time — no threading through `register_*`.
    Returns None if unavailable (callers fall back to the module default).
    """
    server = getattr(ctx, "fastmcp", None)
    return getattr(server, "_kdbx_config", None)
