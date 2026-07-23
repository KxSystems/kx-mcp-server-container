import logging
import os
import tempfile
from typing import Optional
import kdbai_client as kdbai
from kx_mcp_kdbai.settings import KDBAIConfig
from kx_mcp_kdbai.utils.kdbai_auth import cleanup_token_managers, get_token_manager

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
_session_cache: dict = {}
_oauth_files: list = []


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
    """The inbound caller's raw bearer to forward (passthrough): the validated principal's token, else
    the raw inbound `Authorization` header. None outside an HTTP request context (stdio, tests)."""
    raw = getattr(principal, "token", None) if principal is not None else None
    if raw:
        return raw
    try:
        from fastmcp.server.dependencies import get_http_headers
        auth = (get_http_headers() or {}).get("authorization")
        if auth:
            return auth[7:] if auth[:7].lower() == "bearer " else auth
    except Exception:
        pass
    return None


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

    cached = _session_cache.get(cache_key)
    if cached is not None:
        return cached

    bearer = _incoming_bearer(principal) if config.outbound_strategy == "passthrough" else None
    logger.debug(f"KDBAIConfig: {config=}")
    logger.info(f"Connecting to KDB.AI at {config.host}:{config.port}")
    retry = config.retry
    for attempt in range(1, retry + 1):
        try:
            client = _open_session(config, bearer=bearer)
            logger.info("Connected to KDB.AI")
            _session_cache[cache_key] = client
            return client
        except ValueError:
            # A misconfiguration (e.g. an unsupported strategy) is an operator error, not a transient
            # connectivity failure — surface it immediately rather than burning retries on it.
            raise
        except Exception as e:
            logger.warning(f"KDB.AI connectivity attempt {attempt}/{retry} failed: {str(e)}")
            if attempt == retry:
                logger.error(f"Failed to connect to KDB.AI after {retry} attempts")
                raise


def get_table(table_name: str, database_name: Optional[str] = None,
              config: Optional[KDBAIConfig] = None) -> kdbai.Table:
    cfg = config if config is not None else db_config
    if database_name is None:
        database_name = cfg.database_name

    try:
        client = get_kdbai_client(cfg)
        logger.debug(f"Retrieving table '{table_name}' from database '{database_name}'")
        return client.database(database_name).table(table_name)
    except Exception as e:
        if "Error during creating connection" in str(e):
            logger.warning("KDBAI connection issue detected. Reinitializing...")
            cleanup_kdbai_client()
            client = get_kdbai_client(cfg)
            return client.database(database_name).table(table_name)
        else:
            logger.error(f"Error retrieving KDBAI table '{table_name}': {e}")
            raise


def cleanup_kdbai_client():
    _session_cache.clear()
    # Unlink any temp REST oauth config files we wrote (external_token bridge).
    while _oauth_files:
        path = _oauth_files.pop()
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
