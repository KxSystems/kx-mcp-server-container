"""Outbound identity for the KDB.AI bundle: obtain a bearer for an OAuth-protected KDB.AI server.

The bundle delegates the actual grant to the
shared :func:`kx_auth_core.exchange` seam (the *one* outbound implementation + the ``kx_mcp.audit``
line), keeping only the bundle-local lifecycle (per-config token cache + expiry buffer + invalidate).

This module wires ``service_account`` — the container's own workload identity via the OIDC
client-credentials grant — which needs **no request context**, so the whole path stays synchronous and
config-driven. ``passthrough`` (forwarding the inbound principal) lives in
:mod:`kx_mcp_kdbai.utils.kdbai`, which reads the inbound bearer from the request context and partitions
the connection cache per principal (the KDB.AI ``Session`` is stateful, unlike a per-request REST
client).

**Injection differs by mode** (verified against ``kdbai-client`` 2.0.0): qipc carries the token as the
connection password (``options['password']``); rest hands it to the SDK's file-backed
``JWTTokenManager`` via a ``config_file`` (no inline-bearer kwarg). This module only *mints* the
service-account token; :func:`kx_mcp_kdbai.utils.kdbai._open_session` places it — as the qipc password
or, for rest, written into a temp ``external_token`` oauth config. **Live validation against a running
OAuth KDB.AI is still pending** (registry-gated bring-up — see the bundle README).
"""

import asyncio
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
from typing import Optional

from kx_auth_core import OutboundConfig, exchange

from kx_mcp_kdbai.settings import KDBAIConfig

logger = logging.getLogger(__name__)

# Refresh this many seconds before the token actually expires, to avoid edge-of-expiry races
# (kept local to the bundle's connection lifecycle).
_EXPIRY_BUFFER_SECONDS = 60


def _now() -> float:
    return time.time()


def _run_coro(coro):
    """Run an async coroutine to completion from synchronous code.

    The KDB.AI connection path (:func:`get_kdbai_client`) is sync but is reached from inside async tool
    handlers — i.e. a running event loop — where ``asyncio.run`` would raise. When a loop is already
    running we execute the coroutine on a throwaway loop in a worker thread; ``service_account`` reads
    no request-context contextvars, so the thread hop is safe. Outside any loop (standalone server,
    tests) we just ``asyncio.run`` in place.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    with ThreadPoolExecutor(max_workers=1) as executor:
        return executor.submit(asyncio.run, coro).result()


def _service_account_config(config: KDBAIConfig) -> OutboundConfig:
    """Build the shared :class:`OutboundConfig` for the client-credentials grant from KDBAIConfig."""
    secret = config.client_secret.get_secret_value()
    return OutboundConfig(
        strategy="service_account",
        token_url=config.token_url or None,
        audience=config.audience or None,
        client_id=config.client_id or None,
        client_secret=config.client_secret if secret else None,
        scopes=config.scopes.split() if config.scopes else None,
        verify=config.ssl_verify,
        timeout=30.0,
    )


class ServiceAccountTokenManager:
    """Mints + caches an outbound KDB.AI bearer via the OIDC client-credentials grant.

    One manager per frozen :class:`KDBAIConfig` (see :func:`get_token_manager`), so its mutable token
    cache is per-backend — never shared across mounts. The grant itself is delegated to the shared
    ``service_account`` strategy; this manager owns only the cache/expiry lifecycle. It is
    sync-facing because the KDB.AI connection path is synchronous.
    """

    def __init__(self, config: KDBAIConfig):
        self._config = config
        self._token: Optional[str] = None
        self._expiry: float = 0.0

    def get_token(self) -> str:
        if self._token and _now() < (self._expiry - _EXPIRY_BUFFER_SECONDS):
            return self._token
        return self._request_token()

    def _request_token(self) -> str:
        cfg = self._config
        if not cfg.token_url:
            raise RuntimeError(
                "KDBAI_DB_TOKEN_URL is not set — cannot obtain an outbound KDB.AI service-account token"
            )
        logger.info("Requesting KDB.AI access token via client credentials from %s", cfg.token_url)
        try:
            cred = _run_coro(exchange(_service_account_config(cfg)))
        except Exception as exc:
            raise RuntimeError(
                f"Token request failed ({exc}). "
                "Check KDBAI_DB_CLIENT_ID / KDBAI_DB_CLIENT_SECRET and KDBAI_DB_TOKEN_URL."
            ) from exc
        self._token = cred.access_token
        expires_in = cred.expires_in if cred.expires_in is not None else 3600
        self._expiry = _now() + float(expires_in)
        logger.info("KDB.AI access token obtained (valid for %ss).", expires_in)
        return self._token

    def invalidate(self) -> None:
        """Drop the cached token so the next get_token() re-fetches (used on an outbound auth error)."""
        self._token = None
        self._expiry = 0.0


@lru_cache()
def get_token_manager(config: Optional[KDBAIConfig] = None) -> ServiceAccountTokenManager:
    """One token manager per frozen config (instance-safe). Same config -> same cached manager."""
    if config is None:
        config = KDBAIConfig()
    return ServiceAccountTokenManager(config)


def cleanup_token_managers() -> None:
    """Clear the per-config manager cache (tests; reconfiguration)."""
    get_token_manager.cache_clear()
