import logging
import threading
from collections import OrderedDict
from typing import Any, NamedTuple, Optional
import pykx as kx
from pykx.exceptions import QError
from kx_auth_core import project_principal
from kx_mcp_core import remaining_mount_budget, span
from kx_mcp_kdbx.settings import KDBConfig
from kx_mcp_kdbx.utils.observe import q, record_connect, record_reconnect

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


# Deepest nesting accepted in a ferried `claims` value. Real JWT claims are shallow — a handful of
# levels at most — so this is generous, while still bounding `_charvec`'s recursion well short of
# Python's own limit.
_MAX_CLAIM_DEPTH = 32


class MalformedPrincipal(ValueError):
    """A validated principal that still cannot be safely asserted to q.

    The message deliberately starts with ``denied:`` — the q ``.kx.auth`` refusal convention — so
    :func:`kx_mcp_kdbx.utils.denial.is_denial` maps it to the same structured ``permission_denied``
    envelope a q-side refusal produces. One denial vocabulary for the agent regardless of which
    layer refused, and, more to the point, a malformed *token* never surfaces as an
    infrastructure-shaped error the agent will read as "retry later".

    Raised BEFORE anything is cached or sent, because both shapes this catches used to fail late and
    unhelpfully. A non-string ``sub`` bound without complaint and then broke the first q policy check
    that compared it against a symbol, with a raw q ``'type`` error that ``is_denial`` correctly does
    *not* treat as a denial — so the tool fell through to a bare ``{"status": "error"}`` with no
    ``error_type`` at all. A dict- or list-valued ``sub`` died even earlier, as
    ``TypeError: unhashable type: 'dict'`` from the connection cache key. And a pathologically deep
    claims value blew ``_charvec``'s unguarded recursion; the tool-level broad excepts that absorbed
    that were incidental, not a guarantee this layer made.
    """

    def __init__(self, reason: str):
        super().__init__(f"denied: {reason}")


def _assert_assertable(principal) -> None:
    """Refuse a principal q cannot promote, before it reaches the cache key or the wire."""
    claims = getattr(principal, "claims", None) or {}
    if not isinstance(claims, dict):
        raise MalformedPrincipal(f"`claims` must be an object, got {type(claims).__name__}")
    sub = (
        getattr(principal, "subject", None)
        or claims.get("sub")
        or getattr(principal, "client_id", None)
    )
    if sub is not None and not isinstance(sub, str):
        raise MalformedPrincipal(
            f"`sub` claim must be a string, got {type(sub).__name__} — this identity cannot be "
            "asserted (a JSON number or object `sub` is not a valid subject identifier)"
        )
    _check_claim_depth(claims)


def _check_claim_depth(value, depth: int = 0) -> None:
    """Walk `claims` once up front so an over-deep value is a clean denial, not a RecursionError."""
    if depth > _MAX_CLAIM_DEPTH:
        raise MalformedPrincipal(
            f"`claims` nests deeper than {_MAX_CLAIM_DEPTH} levels — refusing to ferry it"
        )
    if isinstance(value, dict):
        for nested in value.values():
            _check_claim_depth(nested, depth + 1)
    elif isinstance(value, (list, tuple)):
        for nested in value:
            _check_claim_depth(nested, depth + 1)


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


def _charvec(obj, depth: int = 0):
    """Recursively wrap str leaves as q char vectors, so PyKX does not auto-symbolise them.

    Applied to the raw `claims` blob before binding: q symbols are never garbage-collected, so
    high-cardinality claim values (e.g. `jti`, unique per token) would grow the symbol table without
    bound if sent as Python `str` (PyKX maps `str` -> q symbol). As char vectors they never intern.
    The q side (`.kx.auth.promote`) symbolises only the *promoted* fields it extracts.
    """
    if depth > _MAX_CLAIM_DEPTH:
        # Backstop. `_assert_assertable` normally rejects an over-deep claims blob before we get
        # here, but this function is the one that actually recurses, so it carries its own bound
        # rather than trusting every future caller to have checked first.
        raise MalformedPrincipal(
            f"`claims` nests deeper than {_MAX_CLAIM_DEPTH} levels — refusing to ferry it"
        )
    if isinstance(obj, str):
        return kx.CharVector(obj)
    if isinstance(obj, dict):
        return {k: _charvec(v, depth + 1) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_charvec(v, depth + 1) for v in obj]
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
    q(conn, "bind", ".kx.auth.bind", d)


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
    if principal is not None:
        # Before the cache key: an unhashable `sub` would otherwise fail here as a bare
        # `TypeError: unhashable type`, and a non-string one would bind silently and break the
        # first q policy check with a raw `'type`.
        _assert_assertable(principal)
    pkey = _principal_key(principal)
    try:
        # A failure here is a failed first connect, already counted `failed` by `_connect`.
        conn = kdb_sync_connection(cfg, pkey)
    except Exception as e:
        logger.error(f"Error in creating KDBX connection: {e}")
        raise
    try:
        q(conn, "probe", '') # check if conn is live for existing connection from cache
    except Exception as e:
        if not _is_dead_handle(e, conn):
            # A q-side refusal of the probe (`.z.pg` saying no, say): the handle is fine, and
            # reconnecting would only get the same answer on a fresh one.
            logger.error(f"Error in creating KDBX connection: {e}")
            raise
        logger.warning(f"KDB-X connection was closed. Reinitializing... ({e})")
        _evict_connection(cfg, pkey, conn)
        try:
            # Through `_connect`, so a refused reopen is counted `failed` and honours the retries.
            conn = kdb_sync_connection(cfg, pkey)
            q(conn, "probe", '')
        except Exception as reopen_error:
            logger.error(f"Error in re-creating KDBX connection: {reopen_error}")
            raise
        # Counted once the handle is back, which is what the metric documents: found closed AND
        # re-established. A reopen that fails shows up as `kdbx_connects_total{outcome="failed"}`.
        record_reconnect()
    if assert_identity:
        _bind_principal(conn, principal)
    return conn

class _CacheInfo(NamedTuple):
    """The `functools.lru_cache().cache_info()` fields callers/tests already read."""

    hits: int
    misses: int
    maxsize: Optional[int]
    currsize: int


class _ConnectionCache:
    """A bounded LRU of qIPC handles that **closes** what it evicts.

    Replaces `@lru_cache()`, which was the right shape but silently leaked: at its default
    `maxsize=128` the 129th distinct key dropped the oldest handle from the *cache* while nothing
    ever called `.close()` on it, so the underlying qIPC socket stayed open until GC happened to
    reclaim the object. Harmless with one principal; a steady leak once identity assertion is on and
    the cache is keyed per `(config, principal)`, which is exactly the deployment that needs it least.

    `cache_info()` / `cache_clear()` and the positional `(config, principal_key)` call shape are kept
    deliberately — existing tests read `cache_info().currsize`, patch `cache_clear`, and assert on
    `call_args_list[0].args[1]`, and the whole point is that this is a drop-in for what was there.
    """

    def __init__(self, maxsize: int = 128):
        self.maxsize = maxsize
        self._entries: OrderedDict = OrderedDict()
        self._lock = threading.Lock()
        self._hits = 0
        self._misses = 0

    @staticmethod
    def _close(conn: Any, why: str) -> None:
        """Best-effort close. A handle we are discarding must never take a request down with it."""
        try:
            conn.close()
        except Exception as exc:  # noqa: BLE001 — already-dead handles are the common case here
            logger.debug(f"Ignoring error closing {why} KDB-X connection: {exc}")

    def get_or_create(self, key, factory):
        with self._lock:
            if key in self._entries:
                self._entries.move_to_end(key)
                self._hits += 1
                return self._entries[key]
            self._misses += 1

        # Connect OUTSIDE the lock: it does real network I/O with retries, and holding the lock
        # across it would serialise every principal's first call behind one slow connect.
        conn = factory()

        with self._lock:
            existing = self._entries.get(key)
            if existing is not None:
                # Another caller won the race. Keep theirs, close ours — otherwise this is the same
                # leak by a different route.
                self._close(conn, "duplicate")
                self._entries.move_to_end(key)
                return existing
            self._entries[key] = conn
            while len(self._entries) > self.maxsize:
                _, evicted = self._entries.popitem(last=False)
                logger.info("KDB-X connection cache full; closing the least-recently-used handle")
                self._close(evicted, "evicted")
        return conn

    def evict(self, key, conn) -> None:
        """Drop and close `conn` under `key`, unless another caller already replaced it."""
        with self._lock:
            if self._entries.get(key) is conn:
                del self._entries[key]
        self._close(conn, "dead")

    def cache_info(self) -> _CacheInfo:
        with self._lock:
            return _CacheInfo(self._hits, self._misses, self.maxsize, len(self._entries))

    def cache_clear(self) -> None:
        with self._lock:
            entries, self._entries = self._entries, OrderedDict()
        for conn in entries.values():
            self._close(conn, "cleared")


_connection_cache = _ConnectionCache()


def kdb_sync_connection(config: Optional[KDBConfig] = None, principal_key: Optional[tuple] = None) -> kx.QConnection:
    """Cached connection, keyed on `(config, principal_key)`.

    `principal_key` does not change the connection parameters — every handle authenticates with the
    same service-account credentials — but it partitions the cache so each asserted principal gets
    its OWN qIPC handle (its own `.z.w`), which is what makes the q-side per-handle bind stable and
    race-free. `None` (assertion off / anonymous) preserves the single shared connection.
    """
    cfg = db_config if config is None else config
    return _connection_cache.get_or_create((cfg, principal_key), lambda: _connect(cfg))


kdb_sync_connection.cache_info = _connection_cache.cache_info  # type: ignore[attr-defined]
kdb_sync_connection.cache_clear = _connection_cache.cache_clear  # type: ignore[attr-defined]


def _evict_connection(config: KDBConfig, principal_key: Optional[tuple], conn: Any) -> None:
    """Drop one dead cached handle. Other keys' handles are left to find out on their own probe."""
    _connection_cache.evict((config, principal_key), conn)


# How PyKX reports a handle whose peer has gone, with its own reconnect disabled
# (`reconnection_attempts=-1`, `pykx/ipc.py`): the peer closed it (`closed IPC connection`, also
# raised for a handle PyKX already closed), it died mid-message (`connection closed`), or the write
# failed (`Failed to send query on IPC socket`). The TLS path goes through q's own handle and reports
# the same as a QError naming the handle; those three texts are the ones PyKX's own reconnect
# treats as dead. Every other QError is q answering, so the handle is alive.
_DEAD_HANDLE_TEXT = ("closed IPC connection", "connection closed", "Failed to send query on IPC socket")
_DEAD_HANDLE_QERROR_TEXT = ("snd handle", "write to handle", "close handle")


def _is_dead_handle(exc: BaseException, conn: Any) -> bool:
    """True when `exc` from a round-trip on `conn` means the handle is dead, not that q said no."""
    text = str(exc)
    if isinstance(exc, QError):
        return any(t in text for t in _DEAD_HANDLE_QERROR_TEXT)
    if isinstance(exc, OSError):  # ECONNRESET / EPIPE / EBADF straight off the socket
        return True
    if getattr(conn, "closed", False) is True:
        return True
    return any(t in text for t in _DEAD_HANDLE_TEXT)


# Below this, a mount budget is treated as spent. PyKX hands the connect timeout to `hopen` as
# `int(seconds * 1000)` milliseconds, and a 0 there means *no* timeout, the opposite of what a nearly
# exhausted budget wants. 50ms also keeps a last attempt from being a guaranteed timeout.
_MIN_CONNECT_TIMEOUT = 0.05


class MountBudgetExhausted(ConnectionError):
    """The connect ran out of the launcher's `KX_MCP_MOUNT_TIMEOUT` budget before it succeeded."""


def _connect_timeout(config: KDBConfig, attempts_made: int = 0, cause: Optional[BaseException] = None) -> Optional[float]:
    """The `connection_timeout` for the next connect attempt; `None` keeps PyKX's default.

    Outside a bounded mount this is `None`, so PyKX times the connect with `config.timeout` exactly
    as before. Inside one it is capped by what is left of the budget, because an embedded-q `hopen`
    holds the GIL and the launcher's `join(timeout)` cannot fire until it returns: the bundle has to
    bound its own wait. Only the connect is capped; `timeout` still governs queries on the handle,
    which outlives the mount. Raises `MountBudgetExhausted` once the budget is spent.
    """
    budget = remaining_mount_budget()
    if budget is None:
        return None
    if budget < _MIN_CONNECT_TIMEOUT:
        detail = f"; last error: {cause}" if cause is not None else ""
        raise MountBudgetExhausted(
            f"KDB-X connect to {config.host}:{config.port} abandoned: the mount budget "
            f"(KX_MCP_MOUNT_TIMEOUT) ran out after {attempts_made} attempt(s){detail}"
        )
    # `timeout` 0 is PyKX's "no timeout", so the budget alone bounds it.
    return budget if config.timeout <= 0 else min(float(config.timeout), budget)


def _connect(config: KDBConfig, retries: Optional[int] = None) -> kx.QConnection:
    """Open one qIPC handle: one attempt plus `retries` (default `config.retry`), within any mount budget."""
    logger.debug(f"KDBConfig: {config=}")
    logger.info(f"Connecting to KDB at {config.host}:{config.port}")
    attempts = (config.retry if retries is None else retries) + 1
    last_error: Optional[Exception] = None
    for attempt in range(1, attempts + 1):
        try:
            con_timeout = _connect_timeout(config, attempt - 1, last_error)
        except MountBudgetExhausted as e:
            logger.error(str(e))
            record_connect("failed")
            raise e from last_error
        try:
            with span("kdbx.connect", {"server.address": config.host, "server.port": config.port}):
                conn = kx.SyncQConnection(
                    host=config.host,
                    port=config.port,
                    username=config.username,
                    password=config.password.get_secret_value(),
                    timeout=config.timeout,
                    connection_timeout=con_timeout,
                    # PyKX's own reconnect (N tries, 0 = forever) is off: it silently reopened
                    # and resent under `get_kdb_connection`, which then never saw a dead handle to
                    # count, and a refused reopen surfaced as an uncounted connect error. The
                    # probe there owns reconnection, through this function.
                    reconnection_attempts=-1,
                    tls=config.tls,
                )
            logger.info("Connected to Q/KDB-X")
            record_connect("ok")
            return conn
        except Exception as e:
            last_error = e
            logger.warning(f"KDB-X connectivity attempt {attempt}/{attempts} failed: {str(e)}")

    logger.error("Failed to connect to KDB")
    record_connect("failed")
    # Re-raise the real cause, not a bare `raise` outside an except block (which reported
    # `RuntimeError: No active exception to re-raise`): the pre-flight in server.py catches the
    # QError by type. `attempts >= 1` (`retry` is validated `ge=0`), so the loop always ran.
    assert last_error is not None
    raise last_error

def cleanup_kdb_connection():
    """Drop every cached handle, closing each one."""
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


def metadata_cache_from_ctx(ctx):
    """Resolve the metadata cache owned by the mounted backend instance."""
    server = getattr(ctx, "fastmcp", None)
    return getattr(server, "_kdbx_metadata_cache", None)
