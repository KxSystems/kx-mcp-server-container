"""Outbound service-account lifecycle + connection-option wiring for the KDB.AI bundle.

The token *mint* delegates to kx-auth-core's shared `exchange` (the `service_account` strategy); these
tests mock that seam and exercise what the bundle owns — the sync token-cache lifecycle (per-config
cache + expiry buffer + invalidate), per-config isolation, and `build_conn_options`' three paths
(service_account / static / anonymous) plus the REST + unsupported-strategy guards. Exercises the
bundle's test_kxi_ie_auth.py, adapted to the bundle's synchronous connection path.
"""

import pytest
from kx_auth_core import OutboundCredential

from kx_mcp_kdbai.settings import KDBAIConfig
from kx_mcp_kdbai.utils import kdbai, kdbai_auth
from kx_mcp_kdbai.utils.kdbai_auth import cleanup_token_managers, get_token_manager


def _cred(token="tok", expires_in=3600):
    return OutboundCredential(access_token=token, expires_in=expires_in, strategy="service_account")


def _cfg(**kw):
    kw.setdefault("mode", "qipc")
    kw.setdefault("outbound_strategy", "service_account")
    kw.setdefault("token_url", "https://idp.example/realms/kdbai/protocol/openid-connect/token")
    kw.setdefault("client_id", "svc")
    kw.setdefault("client_secret", "secret")
    return KDBAIConfig(**kw)


def _patch_exchange(mocker, **kw):
    """Patch the shared seam as imported into kdbai_auth (async)."""
    return mocker.patch.object(kdbai_auth, "exchange", new_callable=mocker.AsyncMock, **kw)


# --- token-manager lifecycle --------------------------------------------------------------------


def test_distinct_configs_get_distinct_managers_and_tokens(mocker):
    """Two token endpoints -> two independently-cached managers; same config -> reused."""
    cleanup_token_managers()
    ex = _patch_exchange(mocker, return_value=_cred("tok"))

    cfg_a = _cfg(token_url="https://a/token", client_id="a")
    cfg_b = _cfg(token_url="https://b/token", client_id="b")

    m_a = get_token_manager(cfg_a)
    m_b = get_token_manager(cfg_b)

    assert m_a is not m_b
    assert m_a is get_token_manager(cfg_a)

    assert m_a.get_token() == "tok"
    assert m_b.get_token() == "tok"
    assert ex.call_count == 2
    cleanup_token_managers()


def test_token_cached_until_pre_expiry(mocker):
    cleanup_token_managers()
    ex = _patch_exchange(mocker, return_value=_cred("tok", expires_in=3600))
    mgr = get_token_manager(_cfg())
    assert mgr.get_token() == "tok"
    assert mgr.get_token() == "tok"
    assert ex.call_count == 1  # cached within the pre-expiry window
    cleanup_token_managers()


def test_token_refreshes_after_expiry(mocker):
    cleanup_token_managers()
    clock = {"t": 1000.0}
    mocker.patch.object(kdbai_auth, "_now", side_effect=lambda: clock["t"])
    ex = _patch_exchange(
        mocker, side_effect=[_cred("tok1", expires_in=100), _cred("tok2", expires_in=100)]
    )
    mgr = get_token_manager(_cfg())
    assert mgr.get_token() == "tok1"
    clock["t"] += 200  # past expiry (100s) + buffer
    assert mgr.get_token() == "tok2"
    assert ex.call_count == 2
    cleanup_token_managers()


def test_invalidate_forces_refetch(mocker):
    cleanup_token_managers()
    ex = _patch_exchange(mocker, side_effect=[_cred("tok1"), _cred("tok2")])
    mgr = get_token_manager(_cfg())
    assert mgr.get_token() == "tok1"
    mgr.invalidate()
    assert mgr.get_token() == "tok2"
    assert ex.call_count == 2
    cleanup_token_managers()


def test_missing_token_url_raises_clear_error(mocker):
    cleanup_token_managers()
    _patch_exchange(mocker, return_value=_cred())
    mgr = get_token_manager(_cfg(token_url=""))
    with pytest.raises(RuntimeError, match="KDBAI_DB_TOKEN_URL"):
        mgr.get_token()
    cleanup_token_managers()


def test_service_account_outbound_config_mapping(mocker):
    """The OutboundConfig handed to exchange() carries the KDBAI_DB_* outbound fields."""
    cleanup_token_managers()
    ex = _patch_exchange(mocker, return_value=_cred())
    cfg = _cfg(audience="kdbai-api", scopes="api://kdbai/.default offline", ssl_verify=False)
    get_token_manager(cfg).get_token()

    oc = ex.call_args.args[0]
    assert oc.strategy == "service_account"
    assert oc.token_url == cfg.token_url
    assert oc.client_id == "svc"
    assert oc.audience == "kdbai-api"
    assert oc.scopes == ["api://kdbai/.default", "offline"]
    assert oc.verify is False
    cleanup_token_managers()


# --- build_conn_options: the three paths + guards ----------------------------------------------


def test_conn_options_static_path_unchanged():
    opts = kdbai.build_conn_options(KDBAIConfig(username="u", password="p", mode="qipc"))
    assert opts == {"username": "u", "password": "p", "reconnection_attempts": 2}


def test_conn_options_anonymous_path():
    assert kdbai.build_conn_options(KDBAIConfig(mode="qipc")) == {"reconnection_attempts": 2}


def test_conn_options_service_account_injects_token_as_password(mocker):
    cleanup_token_managers()
    _patch_exchange(mocker, return_value=_cred("JWT"))
    opts = kdbai.build_conn_options(_cfg())
    assert opts["password"] == "JWT"
    assert opts["username"] == "user"  # default qipc username when none configured
    cleanup_token_managers()


def test_conn_options_passthrough_carries_inbound_bearer_as_password():
    """passthrough forwards the inbound caller's bearer as the qipc connection password."""
    opts = kdbai.build_conn_options(KDBAIConfig(mode="qipc", outbound_strategy="passthrough"), bearer="INBOUND")
    assert opts["password"] == "INBOUND"
    assert opts["username"] == "user"


def test_conn_options_passthrough_without_bearer_is_anonymous():
    """No inbound principal (e.g. the startup pre-flight) -> anonymous reachability, not a mint."""
    assert kdbai.build_conn_options(
        KDBAIConfig(mode="qipc", outbound_strategy="passthrough"), bearer=None
    ) == {"reconnection_attempts": 2}


def test_conn_options_unsupported_strategy_raises():
    with pytest.raises(ValueError, match="not supported"):
        kdbai.build_conn_options(KDBAIConfig(mode="qipc", outbound_strategy="rfc_8693"))


# --- REST-mode OAuth: the external_token bridge ------------------------------------------------


def test_rest_service_account_writes_external_token_oauth_file(mocker):
    """REST + service_account: the minted bearer is handed to the SDK via an external_token oauth file."""
    cleanup_token_managers()
    kdbai.cleanup_kdbai_client()
    _patch_exchange(mocker, return_value=_cred("MINTED"))
    sess = mocker.patch("kdbai_client.Session", side_effect=lambda *a, **k: mocker.Mock())
    kdbai._open_session(_cfg(mode="rest"))
    kwargs = sess.call_args.kwargs
    assert kwargs["mode"] == "rest"
    assert kwargs["oauth"]["enabled"] is True
    import yaml
    written = yaml.safe_load(open(kwargs["oauth"]["config_file"]))
    assert written["oauth"]["grant_type"] == "external_token"
    assert written["oauth"]["access_token"] == "MINTED"
    kdbai.cleanup_kdbai_client()  # unlinks the temp file
    cleanup_token_managers()


def test_rest_passthrough_uses_inbound_bearer_in_oauth_file(mocker):
    """REST + passthrough: the *inbound* bearer (not a minted one) is written as the external_token."""
    kdbai.cleanup_kdbai_client()
    sess = mocker.patch("kdbai_client.Session", side_effect=lambda *a, **k: mocker.Mock())
    cfg = KDBAIConfig(mode="rest", outbound_strategy="passthrough")
    kdbai._open_session(cfg, bearer="INBOUND")
    import yaml
    written = yaml.safe_load(open(sess.call_args.kwargs["oauth"]["config_file"]))
    assert written["oauth"]["access_token"] == "INBOUND"
    kdbai.cleanup_kdbai_client()


# --- per-principal connection cache (passthrough) ----------------------------------------------


def _fake_principal(sub, token, iss="https://idp"):
    """A stand-in for fastmcp's AccessToken: subject unset, client_id folded to a shared azp, the real
    identity in the `sub` claim, and the raw bearer on `.token` (what passthrough forwards)."""
    class _P:
        pass
    p = _P()
    p.subject = None              # fastmcp leaves this unset; sub comes from claims
    p.client_id = "shared-client"
    p.token = token
    p.claims = {"sub": sub, "iss": iss}
    return p


def test_passthrough_caches_per_principal(mocker):
    """Two principals on one OAuth client get DISTINCT sessions; the same principal reuses one."""
    kdbai.cleanup_kdbai_client()
    sess = mocker.patch("kdbai_client.Session", side_effect=lambda *a, **k: mocker.Mock())
    cfg = KDBAIConfig(host="kdbai", port=8082, mode="qipc", outbound_strategy="passthrough")

    principals = {"alice": _fake_principal("alice", "tok-a"), "bob": _fake_principal("bob", "tok-b")}
    current = {"who": "alice"}
    mocker.patch.object(kdbai, "_current_principal", side_effect=lambda: principals[current["who"]])

    a1 = kdbai.get_kdbai_client(cfg)
    current["who"] = "bob"
    b1 = kdbai.get_kdbai_client(cfg)
    current["who"] = "alice"
    a2 = kdbai.get_kdbai_client(cfg)

    assert a1 is not b1          # distinct principals -> distinct sessions (no identity bleed)
    assert a1 is a2              # same principal -> reused
    assert sess.call_count == 2
    # alice's session authenticated with alice's bearer
    alice_opts = [c.kwargs.get("options") for c in sess.call_args_list if c.kwargs.get("options")]
    assert any(o.get("password") == "tok-a" for o in alice_opts)
    kdbai.cleanup_kdbai_client()


# --- reconnect-driven token re-mint (the kdb.ai self-heal) --------------------------------------
#
# KDB.AI caches a stateful `Session`, so its recovery path is reconnect-and-remint rather than a
# with the bearer baked in at construction. Its self-heal is connection-error-triggered, not
# auth-error-triggered: `get_table` retries once on a connection error, and `cleanup_kdbai_client`
# (which that retry calls) also drops the cached outbound token via `cleanup_token_managers()`, so
# the *next* connect re-mints rather than replaying a stale/revoked bearer. A token that expires on
# an already-live cached session without a connection error is not re-minted at query time — a
# known, deliberate constraint (kdb.ai has no live-OAuth validation yet; see the
# bundle README's "Validating outbound identity" section).


def test_get_table_reconnects_on_connection_error_and_retries_once(mocker):
    """get_table's existing reconnect branch: a connection error triggers cleanup + one retry."""
    kdbai.cleanup_kdbai_client()
    working_client = mocker.Mock()
    get_client = mocker.patch.object(
        kdbai, "get_kdbai_client",
        side_effect=[Exception("Error during creating connection"), working_client],
    )
    cleanup = mocker.patch.object(kdbai, "cleanup_kdbai_client")

    table = kdbai.get_table("docs", "mydb", KDBAIConfig())

    assert table is working_client.database.return_value.table.return_value
    cleanup.assert_called_once()
    assert get_client.call_count == 2


def test_get_table_non_connection_error_is_not_retried(mocker):
    """The negative case: a non-connection error propagates with no reconnect and no token drop."""
    kdbai.cleanup_kdbai_client()
    get_client = mocker.patch.object(kdbai, "get_kdbai_client", side_effect=RuntimeError("bad query"))
    cleanup = mocker.patch.object(kdbai, "cleanup_kdbai_client")

    with pytest.raises(RuntimeError, match="bad query"):
        kdbai.get_table("docs", "mydb", KDBAIConfig())

    cleanup.assert_not_called()
    assert get_client.call_count == 1


def test_reconnect_drops_cached_service_account_token_so_next_connect_re_mints(mocker):
    """A reconnect re-mints the service-account credential and never replays a failed request."""
    cleanup_token_managers()
    ex = _patch_exchange(mocker, side_effect=[_cred("tok1"), _cred("tok2")])
    cfg = _cfg()

    assert get_token_manager(cfg).get_token() == "tok1"
    kdbai.cleanup_kdbai_client()  # the reconnect path's cleanup: drops cached sessions + tokens
    assert get_token_manager(cfg).get_token() == "tok2"
    assert ex.call_count == 2
    cleanup_token_managers()


def test_get_kdbai_client_fast_fails_on_valueerror_without_burning_retries(mocker):
    """An unsupported outbound strategy is a misconfiguration -> fail on attempt 1, never retried."""
    kdbai.cleanup_kdbai_client()
    cfg = KDBAIConfig(mode="qipc", outbound_strategy="rfc_8693", retry=3)
    open_session = mocker.patch.object(kdbai, "_open_session", wraps=kdbai._open_session)

    with pytest.raises(ValueError, match="not supported"):
        kdbai.get_kdbai_client(cfg)

    assert open_session.call_count == 1


def test_get_kdbai_client_retries_transient_then_succeeds(mocker):
    """A transient connect failure is retried within `config.retry`, not surfaced immediately."""
    kdbai.cleanup_kdbai_client()
    working_client = mocker.Mock()
    open_session = mocker.patch.object(
        kdbai, "_open_session", side_effect=[Exception("connection refused"), working_client]
    )
    cfg = KDBAIConfig(mode="qipc", retry=2)

    client = kdbai.get_kdbai_client(cfg)

    assert client is working_client
    assert open_session.call_count == 2
    kdbai.cleanup_kdbai_client()
