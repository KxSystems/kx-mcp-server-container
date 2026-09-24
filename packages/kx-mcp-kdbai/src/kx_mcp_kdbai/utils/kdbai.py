import logging
import os
import tempfile
import threading
from collections import OrderedDict
from typing import Optional
import kdbai_client as kdbai
from kx_mcp_kdbai.settings import KDBAIConfig
from kx_mcp_kdbai.utils.kdbai_auth import cleanup_token_managers, get_token_manager
from kx_mcp_kdbai.utils.observe import call, record_connect, record_sessions, timed

# Module-level DEFAULT config (env-driven), used only when no per-instance config is supplied.
# Decoupled from server.app_settings so the connection layer has no import-time dependency on the
# server module. The real per-backend config is threaded in from build_server() (which stashes it on
# the server object) -> the tool closures via config_from_ctx() -> here, which is what makes the
# bundle instance-safe: two KDB.AI backends get distinct, independently-cached clients.
db_config = KDBAIConfig()
logger = logging.getLogger(__name__)

# Session cache keyed on (frozen config, principal_key). principal_key is None for every strategy
# except `passthrough`, so service_account/static/anonymous share one session per config; passthrough
# partitions the cache per principal, since each caller's session authenticates AS that principal (the
# inbound bearer is the connection credential). Mirrors the kdb-x bundle's per-(config, principal)
# connection scheme. A plain dict (not @lru_cache) because the bearer must reach session construction
# without becoming a cache key. `_oauth_files` tracks temp REST oauth config files so cleanup can
# unlink them.
_session_cache: OrderedDict = OrderedDict()
_oauth_files: list = []

# The cache is bounded. It used to be a bare dict with no cap and no eviction short of a full clear,
# so under `passthrough` it grew one live stateful `Session` per distinct principal, forever — worse
# than the kdb-x cache, which at least had @lru_cache's implicit 128. An LRU bound is the same trade
# the kdb-x bundle makes: a rarely-active principal pays a reconnect instead of the process leaking.
MAX_CACHED_SESSIONS = 128

# One lock for every mutation of `_session_cache` / `_oauth_files`, plus the recovery generation
# counter below. There was none, so `cleanup_kdbai_client()`'s clear could race a concurrent
# `get_kdbai_client()` populate — handing back a session that was being torn down.
_cache_lock = threading.Lock()

# Bumped on every completed recovery. `get_table` reads it BEFORE trying, and skips its own recovery
# if someone else already recovered in the meantime — so N concurrent callers hitting the same
# outage produce one recovery, not N. Without this, a real outage triggered an uncoordinated
# reconnect storm at exactly the moment the backend could least absorb one.
_recovery_generation = 0


def _close_session(session, why: str) -> None:
    """Best-effort close of a session we are discarding. Never let cleanup raise into a request."""
    close = getattr(session, "close", None)
    if close is None:
        return
    try:
        close()
    except Exception as exc:  # noqa: BLE001 — an already-dead session is the common case here
        logger.debug(f"Ignoring error closing {why} KDB.AI session: {exc}")


def _evict_session(cache_key) -> None:
    """Drop and close ONE principal's session.

    The recovery path used to call `cleanup_kdbai_client()`, which clears the entire cache and every
    backend's cached token — so one principal's transient connection error forced every other
    concurrently-active principal into a full reconnect, and re-minted service-account tokens for
    unrelated configs. A failing session is the failing session's problem.
    """
    with _cache_lock:
        session = _session_cache.pop(cache_key, None)
    if session is not None:
        _close_session(session, "evicted")


def _current_principal():
    """Read the validated inbound principal without hard-depending on the container package.

    Mirrors `kx_mcp_core.auth.current_principal` (and the kdb-x bundle's helper) but imports fastmcp's
    accessor directly so the bundle stays runnable standalone. None when there is no authenticated
    principal (anonymous / stdio / tests).
    """
    try:
        from fastmcp.server.dependencies import get_access_token
        return get_access_token()
    except Exception:
        return None


def _principal_key(principal) -> Optional[tuple]:
    """Hashable per-principal cache key: (subject, issuer). None when anonymous.

    fastmcp's `JWTVerifier` leaves `AccessToken.subject` unset and folds the client id to `azp`, so
    prefer the `sub` claim (the real user identity) and fall back to client_id only when absent —
    else every user on one OAuth client collapses to a single shared session. Same rule as kdb-x.
    """
    if principal is None:
        return None
    claims = getattr(principal, "claims", None) or {}
    sub = getattr(principal, "subject", None) or claims.get("sub") or getattr(principal, "client_id", None)
    return (sub, claims.get("iss"))


def _incoming_bearer(principal) -> Optional[str]:
    """The **validated** principal's bearer to forward under `passthrough`; None when there isn't one.

    There used to be a fallback here that read the raw inbound `Authorization` header via
    `get_http_headers()` and forwarded it verbatim as the KDB.AI credential. Two reasons it is gone
    rather than hardened:

    * **It never ran.** `get_http_headers()` carries `authorization` in its *default* exclude set, so
      the lookup was always `None` and the whole branch — the `Bearer ` strip included — was dead
      code on every fastmcp this package supports. The adversarial-review finding that flagged it as
      a live HIGH reproduced it only by mocking `get_http_headers` to return a header the real
      function does not return.
    * **Repairing it would be the actual vulnerability.** Someone reading the dead branch and
      "fixing" it with `include_all=True` would forward a string nothing has verified as the
      connection credential — and because `_principal_key(None)` is `None`, cache it under the same
      `(config, None)` key that service_account and anonymous share. Every user-facing doc says the
      *validated* principal is forwarded; only that branch said otherwise.

    So the contract is now exactly what the docs promise: a validated principal's token, or nothing.
    A deployment that wants the inbound bearer forwarded must configure inbound auth, which is what
    makes it validated — enforced at startup by `require_validated_passthrough`.
    """
    return getattr(principal, "token", None) if principal is not None else None


def require_validated_passthrough(config: KDBAIConfig, auth_mode: Optional[str] = None) -> None:
    """Refuse `passthrough` without inbound auth — the combination cannot do what it claims.

    Under `passthrough` the *inbound* bearer is the outbound credential, so with `KX_MCP_AUTH` unset
    there is never a validated principal and never a bearer: every call silently degrades to an
    anonymous session. If the KDB.AI server permits anonymous access — a real, supported posture —
    that quietly defeats the entire point of configuring `passthrough`, and looks identical in the
    logs to a legitimately anonymous deployment. Nothing checked this before.
    """
    if config.outbound_strategy != "passthrough":
        return
    if auth_mode is None:
        auth_mode = os.environ.get("KX_MCP_AUTH", "").strip().lower()
    if auth_mode in ("", "unset", "none"):
        raise ValueError(
            "KDBAI_DB_OUTBOUND_STRATEGY=passthrough requires inbound auth: it forwards the "
            "validated inbound principal's bearer as the KDB.AI credential, and with KX_MCP_AUTH "
            f"={auth_mode or 'unset'!r} there is no validated principal, so every call would open "
            "an anonymous session. Set KX_MCP_AUTH (e.g. jwks), or choose a different "
            "KDBAI_DB_OUTBOUND_STRATEGY (service_account/static)."
        )


def build_conn_options(config: KDBAIConfig, bearer: Optional[str] = None) -> dict:
    """Build the kdbai `Session` `options` dict (qipc password placement + static/anonymous creds).

    Outbound-auth paths:
      - `service_account` — mint a workload-identity bearer via the shared exchange() seam and carry
        it as the connection password (the SDK does exactly this for its own OAuth path).
      - `passthrough` — carry the inbound caller's `bearer` as the connection password (forward the
        validated principal). When `bearer` is None (no request context, e.g. the startup pre-flight)
        fall through to an anonymous reachability check rather than minting anything.
      - static username/password — a KDB.AI server in AUTH_TYPE=static.
      - anonymous — no credentials.

    Mode-agnostic: REST-mode OAuth is injected separately (a file-backed oauth manager) in
    `_open_session`, which calls this only for the non-OAuth (qipc / static / anonymous) paths.
    """
    if config.outbound_strategy == "service_account":
        token = get_token_manager(config).get_token()
        return {"username": config.username or "user", "password": token, "reconnection_attempts": 2}
    if config.outbound_strategy == "passthrough":
        if not bearer:
            return {"reconnection_attempts": 2}
        return {"username": config.username or "user", "password": bearer, "reconnection_attempts": 2}
    if config.outbound_strategy:
        raise ValueError(
            f"KDBAI_DB_OUTBOUND_STRATEGY={config.outbound_strategy!r} is not supported by the KDB.AI "
            "bundle (expected 'service_account', 'passthrough', or empty for static/anonymous)."
        )
    if config.password:
        return {"username": config.username, "password": config.password.get_secret_value(), "reconnection_attempts": 2}
    return {"reconnection_attempts": 2}


def _write_external_token_oauth(token: str) -> str:
    """Write a temp kdbai oauth `config_file` carrying a pre-minted bearer as the `external_token`
    grant, and return its path. This is the REST-mode injection point: the public `Session` rebuilds
    its own file-backed `JWTTokenManager` from a `config_file` (no inline-bearer kwarg as qipc has),
    and `external_token` is the grant that lets it consume a token we already hold. Written 0600;
    tracked in `_oauth_files` for cleanup. See the README's "Validating outbound identity" section for
    how to test this against a live OAuth KDB.AI server.
    """
    import yaml
    fd, path = tempfile.mkstemp(prefix="kdbai-oauth-", suffix=".yaml")
    try:
        with os.fdopen(fd, "w") as fh:
            yaml.safe_dump({"oauth": {"grant_type": "external_token", "access_token": token}}, fh)
        os.chmod(path, 0o600)
    except Exception:
        os.unlink(path)
        raise
    _oauth_files.append(path)
    return path


def _open_session(config: KDBAIConfig, bearer: Optional[str] = None) -> kdbai.Session:
    """Construct a `kdbai.Session` for `config`, injecting outbound auth per mode + strategy.

    qipc (and REST static/anonymous): credentials ride in the `options` dict (`build_conn_options`).
    REST + (`service_account`|`passthrough`): the bearer is minted/forwarded and handed to the SDK via
    a file-backed `external_token` oauth config (`_write_external_token_oauth`). Shared by the cached
    connection path and the startup pre-flight so both exercise the same auth wiring.
    """
    protocol = config.rest_protocol if config.mode == "rest" else "http"
    endpoint = f"{protocol}://{config.host}:{config.port}"

    if config.mode == "rest" and config.outbound_strategy in ("service_account", "passthrough"):
        token = bearer if config.outbound_strategy == "passthrough" else get_token_manager(config).get_token()
        if not token:
            # passthrough with no inbound principal (e.g. the pre-flight) — reachability only.
            return kdbai.Session(endpoint=endpoint, mode="rest", options={})
        config_file = _write_external_token_oauth(token)
        return kdbai.Session(endpoint=endpoint, mode="rest", oauth={"enabled": True, "config_file": config_file})

    options = build_conn_options(config, bearer=bearer)
    if config.mode == "qipc" and config.qipc_tls:
        options["tls"] = True
    return kdbai.Session(endpoint=endpoint, mode=config.mode, options=options)


def get_kdbai_client(config: Optional[KDBAIConfig] = None) -> kdbai.Session:
    """Return a cached `kdbai.Session` for `config`, partitioned per principal under `passthrough`.

    The session is cached on `(config, principal_key)`: for `passthrough` the inbound principal's
    bearer is the connection credential, so each principal gets its own session; every other strategy
    has `principal_key=None` and so shares one session per config (unchanged behaviour).
    """
    config = config if config is not None else db_config
    principal = _current_principal() if config.outbound_strategy == "passthrough" else None
    pkey = _principal_key(principal)
    cache_key = (config, pkey)

    with _cache_lock:
        cached = _session_cache.get(cache_key)
        if cached is not None:
            _session_cache.move_to_end(cache_key)  # LRU: touching an entry keeps it alive
            return cached

    bearer = _incoming_bearer(principal) if config.outbound_strategy == "passthrough" else None
    if config.outbound_strategy == "passthrough" and principal is not None and not bearer:
        # A validated principal that carries no forwardable bearer must not silently open an
        # ANONYMOUS session — that is indistinguishable in the logs from a legitimately anonymous
        # deployment, and if the KDB.AI server permits anonymous access it defeats the point of
        # configuring passthrough. kdb-x's `_bind_principal` guards the equivalent case by leaving
        # the handle unbound so q's default-deny catches it; kdb.ai had no backstop at all.
        raise ValueError(
            "passthrough: the validated principal carries no bearer to forward, so the connection "
            "would be anonymous. Refusing rather than silently degrading."
        )
    logger.debug(f"KDBAIConfig: {config=}")
    logger.info(f"Connecting to KDB.AI at {config.host}:{config.port}")
    retry = config.retry
    for attempt in range(1, retry + 1):
        try:
            with timed("connect", {"server.address": config.host, "server.port": config.port}):
                client = _open_session(config, bearer=bearer)
            logger.info("Connected to KDB.AI")
            record_connect("ok")
            with _cache_lock:
                existing = _session_cache.get(cache_key)
                if existing is not None:
                    # Another caller won the race. Keep theirs and close ours, or this is a leak by
                    # a different route than the one the cache bound fixes.
                    _session_cache.move_to_end(cache_key)
                    evicted = None
                else:
                    _session_cache[cache_key] = client
                    evicted = None
                    if len(_session_cache) > MAX_CACHED_SESSIONS:
                        _, evicted = _session_cache.popitem(last=False)
                        logger.info(
                            "KDB.AI session cache full; closing the least-recently-used session"
                        )
                # Inside the lock, after every mutation: the gauge must not report a size that no
                # caller ever saw.
                record_sessions(len(_session_cache))
            if existing is not None:
                _close_session(client, "duplicate")
                return existing
            if evicted is not None:
                _close_session(evicted, "evicted")
            return client
        except ValueError:
            # A misconfiguration (e.g. an unsupported strategy) is an operator error, not a transient
            # connectivity failure — surface it immediately rather than burning retries on it.
            raise
        except Exception as e:
            logger.warning(f"KDB.AI connectivity attempt {attempt}/{retry} failed: {str(e)}")
            if attempt == retry:
                logger.error(f"Failed to connect to KDB.AI after {retry} attempts")
                record_connect("failed")
                raise


def get_table(table_name: str, database_name: Optional[str] = None,
              config: Optional[KDBAIConfig] = None) -> kdbai.Table:
    cfg = config if config is not None else db_config
    if database_name is None:
        database_name = cfg.database_name

    with _cache_lock:
        generation = _recovery_generation

    try:
        client = get_kdbai_client(cfg)
        logger.debug(f"Retrieving table '{table_name}' from database '{database_name}'")
        return call("resolve_table", lambda: client.database(database_name).table(table_name))
    except Exception as e:
        recoverable = _is_recoverable(e)
        if not recoverable:
            logger.error(f"Error retrieving KDBAI table '{table_name}': {e}")
            raise
        _recover(cfg, e, generation, auth_error=recoverable == "auth")
        client = get_kdbai_client(cfg)
        return call("resolve_table", lambda: client.database(database_name).table(table_name))


def _is_recoverable(error: Exception) -> Optional[str]:
    """`"connection"`, `"auth"`, or None — what kind of retry-once failure this is, if any.

    Previously this matched the single literal `"Error during creating connection"`, which is why
    `ServiceAccountTokenManager.invalidate()` was dead code: a token the KDB.AI server *rejects*
    (revoked, or expired on an already-live cached session) does not produce that phrase, so the
    stale token was replayed on every call until the process restarted. Auth-shaped failures are now
    recognised and drive a token purge before the retry.

    Deliberately narrow, not "retry on any exception": a query error such as `RuntimeError("bad
    query")` must still propagate untouched with no reconnect and no token drop.
    """
    text = str(error).lower()
    if "error during creating connection" in text:
        return "connection"
    if any(signal in text for signal in ("401", "403", "unauthorized", "forbidden", "token rejected")):
        return "auth"
    return None


def _recover(cfg: KDBAIConfig, error: Exception, generation: int, *, auth_error: bool) -> None:
    """Evict this principal's session once, coordinated across concurrent callers.

    Two things this fixes. It evicts only the failing `(config, principal)` entry, where it used to
    call `cleanup_kdbai_client()` and clear every principal's session plus every backend's cached
    token. And it is generation-gated, so N callers hitting the same outage together perform one
    recovery between them rather than each triggering their own — the reconnect storm arrives exactly
    when the backend can least absorb it.
    """
    global _recovery_generation
    principal = _current_principal() if cfg.outbound_strategy == "passthrough" else None
    cache_key = (cfg, _principal_key(principal))

    with _cache_lock:
        if generation != _recovery_generation:
            # Someone else already recovered while we were failing; their fresh session is good.
            logger.debug("KDB.AI recovery already performed by a concurrent caller; not repeating")
            return
        _recovery_generation += 1

    logger.warning(f"KDB.AI connection issue detected ({error}). Reinitializing this principal.")
    _evict_session(cache_key)
    if auth_error:
        # The server rejected the credential, so the next connect must MINT a new one rather than
        # replay the same token. Scoped to this config's manager, not every backend's.
        logger.warning("KDB.AI rejected the outbound token; invalidating it so the next call re-mints")
        get_token_manager(cfg).invalidate()


def cleanup_kdbai_client():
    """Drop and close EVERY cached session, for shutdown/reconfiguration and tests.

    Still deliberately global — that is what this function is for. What changed is that the
    *recovery* path no longer calls it: a single principal's connection error used to nuke every
    other principal's healthy session and every backend's minted token through here.
    """
    with _cache_lock:
        # Snapshot then empty, both under the lock, so a concurrent get_kdbai_client cannot see a
        # half-cleared cache. The actual closing happens outside the lock (below): close() does I/O.
        sessions = list(_session_cache.values())
        _session_cache.clear()
        oauth_files = list(_oauth_files)
        _oauth_files.clear()
        record_sessions(0)
    for session in sessions:
        _close_session(session, "cleared")
    # Unlink any temp REST oauth config files we wrote (external_token bridge).
    for path in oauth_files:
        try:
            os.unlink(path)
        except OSError:
            pass
    # Also drop cached outbound tokens: a reconnect may be triggered by the KDB.AI server rejecting a
    # stale/revoked bearer, so the next connect must re-mint rather than replay the same token.
    cleanup_token_managers()
    logger.info("KDBAI client cache cleared")


def config_from_ctx(ctx) -> Optional[KDBAIConfig]:
    """Resolve the per-instance KDBAIConfig the calling tool belongs to.

    FastMCP injects a `Context`; under composition `ctx.fastmcp` is the **bundle's own** server (the
    mounted child, not the parent), on which `build_server()` stashed `_kdbai_config`. Reading it here
    means tools pull their config from context at call time — no threading through `register_*`.
    Returns None if unavailable (callers fall back to the module default).
    """
    server = getattr(ctx, "fastmcp", None)
    return getattr(server, "_kdbai_config", None)
