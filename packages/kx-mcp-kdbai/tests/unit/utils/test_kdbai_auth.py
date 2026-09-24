"""Outbound service-account lifecycle + connection-option wiring for the KDB.AI bundle.

The token *mint* delegates to kx-auth-core's shared `exchange` (the `service_account` strategy); these
tests mock that seam and exercise what the bundle owns — the sync token-cache lifecycle (per-config
cache + expiry buffer + invalidate), per-config isolation, and `build_conn_options`' three paths
(service_account / static / anonymous) plus the REST + unsupported-strategy guards. Exercises the
bundle's test_kxi_ie_auth.py, adapted to the bundle's synchronous connection path.
"""

import os

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


def test_rest_passthrough_two_principals_get_distinct_oauth_files(mocker):
    """REST + passthrough: two principals each get their OWN oauth file — alice's bearer never
    ends up in bob's file. The qipc equivalent (`test_passthrough_caches_per_principal`) proves
    session isolation via the options dict; REST's isolation is file-based, so it needs its own
    proof at that boundary."""
    kdbai.cleanup_kdbai_client()
    sess = mocker.patch("kdbai_client.Session", side_effect=lambda *a, **k: mocker.Mock())
    cfg = KDBAIConfig(mode="rest", outbound_strategy="passthrough")
    kdbai._open_session(cfg, bearer="ALICE-TOKEN")
    kdbai._open_session(cfg, bearer="BOB-TOKEN")

    import yaml
    alice_path = sess.call_args_list[0].kwargs["oauth"]["config_file"]
    bob_path = sess.call_args_list[1].kwargs["oauth"]["config_file"]
    assert alice_path != bob_path
    alice_written = yaml.safe_load(open(alice_path))
    bob_written = yaml.safe_load(open(bob_path))
    assert alice_written["oauth"]["access_token"] == "ALICE-TOKEN"
    assert bob_written["oauth"]["access_token"] == "BOB-TOKEN"
    kdbai.cleanup_kdbai_client()


def test_external_token_oauth_file_is_0600(mocker):
    """The temp oauth file carries a real bearer — must not be group/world-readable."""
    import stat
    kdbai.cleanup_kdbai_client()
    mocker.patch("kdbai_client.Session", side_effect=lambda *a, **k: mocker.Mock())
    cfg = KDBAIConfig(mode="rest", outbound_strategy="passthrough")
    kdbai._open_session(cfg, bearer="SECRET-TOKEN")
    path = kdbai._oauth_files[-1]
    mode = stat.S_IMODE(os.stat(path).st_mode)
    assert mode == 0o600
    kdbai.cleanup_kdbai_client()


def test_oauth_files_tracked_and_fully_drained_on_cleanup(mocker):
    """`_oauth_files` grows one entry per REST-oauth write and `cleanup_kdbai_client()` unlinks
    every one — no leftover temp files, no leftover tracking entries, across multiple principals."""
    kdbai.cleanup_kdbai_client()
    mocker.patch("kdbai_client.Session", side_effect=lambda *a, **k: mocker.Mock())
    cfg = KDBAIConfig(mode="rest", outbound_strategy="passthrough")

    assert kdbai._oauth_files == []
    kdbai._open_session(cfg, bearer="TOKEN-1")
    kdbai._open_session(cfg, bearer="TOKEN-2")
    kdbai._open_session(cfg, bearer="TOKEN-3")
    assert len(kdbai._oauth_files) == 3
    written_paths = list(kdbai._oauth_files)
    assert all(os.path.exists(p) for p in written_paths)

    kdbai.cleanup_kdbai_client()

    assert kdbai._oauth_files == []
    assert not any(os.path.exists(p) for p in written_paths)


def test_rest_passthrough_without_bearer_is_reachability_only(mocker):
    """REST + passthrough with NO inbound bearer (e.g. the startup pre-flight, no request context)
    falls through to an anonymous reachability check — no oauth file written at all. The qipc
    equivalent is `test_conn_options_passthrough_without_bearer_is_anonymous`; REST's no-bearer path
    is a distinct code branch in `_open_session` (short-circuits before `_write_external_token_oauth`
    is ever called) and had no coverage of its own."""
    kdbai.cleanup_kdbai_client()
    sess = mocker.patch("kdbai_client.Session", side_effect=lambda *a, **k: mocker.Mock())
    cfg = KDBAIConfig(mode="rest", outbound_strategy="passthrough")

    kdbai._open_session(cfg, bearer=None)

    kwargs = sess.call_args.kwargs
    assert kwargs["mode"] == "rest"
    assert kwargs["options"] == {}
    assert "oauth" not in kwargs
    assert kdbai._oauth_files == []
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


def test_no_principal_passthrough_forwards_nothing(mocker):
    """REGRESSION, reframed (see mcp-container/adversarial-review-2026-08.md § kdb.ai backend).

    This was written as "the raw `Authorization` header is forwarded unverified as the KDB.AI
    credential", filed HIGH / live-confirmed. It was neither, and the original test only failed
    because it mocked `get_http_headers` to return a header the real function never returns:
    `authorization` is in that function's DEFAULT exclude set, so the fallback branch was dead code
    on every fastmcp this package supports. Mocking away the dependency under test manufactured the
    vulnerability.

    So this now asserts the contract against the REAL `get_http_headers`: with no validated
    principal there is no bearer, full stop. The dead branch is gone — repairing it with
    `include_all=True` is what would have created the bug, and it would have cached the resulting
    session under the same `(config, None)` key that service_account and anonymous share.
    """
    assert kdbai._incoming_bearer(None) is None

    # Belt and braces: even with a live-looking header dict, nothing reads it any more.
    mocker.patch(
        "fastmcp.server.dependencies.get_http_headers",
        return_value={"authorization": "Bearer RAW-UNVALIDATED-TOKEN"},
    )
    assert kdbai._incoming_bearer(None) is None


def test_passthrough_without_inbound_auth_is_refused_at_startup():
    """The real gap the finding above was reaching for.

    `passthrough` forwards the *validated* inbound principal's bearer, so with `KX_MCP_AUTH` unset
    there is never a principal and never a bearer — every call would open an anonymous session, and
    if the KDB.AI server permits anonymous access (a supported posture) that silently defeats the
    whole point of configuring passthrough while looking identical in the logs to a legitimately
    anonymous deployment. Nothing validated the combination before.
    """
    cfg = KDBAIConfig(mode="qipc", outbound_strategy="passthrough")
    with pytest.raises(ValueError, match="requires inbound auth"):
        kdbai.require_validated_passthrough(cfg, auth_mode="unset")
    with pytest.raises(ValueError, match="requires inbound auth"):
        kdbai.require_validated_passthrough(cfg, auth_mode="")

    kdbai.require_validated_passthrough(cfg, auth_mode="jwks")  # must not raise
    # Other strategies do not depend on an inbound principal, so they are unaffected.
    kdbai.require_validated_passthrough(
        KDBAIConfig(mode="qipc", outbound_strategy="service_account"), auth_mode="unset"
    )


def test_authenticated_principal_with_no_forwardable_bearer_is_refused_not_anonymous(mocker):
    """A validated principal carrying no forwardable bearer must be REFUSED, not silently
    downgraded to an anonymous session.

    NARROW by construction: FastMCP's `AccessToken.token` is a required plain `str`, so none of the
    auth providers this repo ships can produce this shape — it matters for a third-party
    `AuthProvider` that withholds the raw token while returning an otherwise-valid principal. Kept
    as a defensive-path regression, not a claim of production reachability. Distinct from
    `test_no_principal_passthrough_forwards_nothing`, which covers the reachable
    no-principal-at-all case.

    kdb-x's `_bind_principal` guards the equivalent case by leaving the handle unbound so q's
    default-deny catches it; kdb.ai had no backstop at all.
    """
    kdbai.cleanup_kdbai_client()
    session = mocker.patch("kdbai_client.Session", side_effect=lambda *a, **k: mocker.Mock())

    class _P:
        subject = None
        client_id = "shared-client"
        token = None
        claims = {"sub": "alice", "iss": "https://idp"}

    mocker.patch.object(kdbai, "_current_principal", return_value=_P())
    cfg = KDBAIConfig(mode="qipc", outbound_strategy="passthrough")

    # A specific error, not a bare `Exception` — which would also have passed on a typo in this test.
    with pytest.raises(ValueError, match="no bearer to forward"):
        kdbai.get_kdbai_client(cfg)
    session.assert_not_called()  # no session may be opened at all

    kdbai.cleanup_kdbai_client()


def test_one_principals_connection_error_does_not_disrupt_another_principals_session(mocker):
    """cleanup_kdbai_client() clears the ENTIRE _session_cache, not just the failing principal's
    entry. One principal's transient connection error must not force every other concurrently
    -active principal into a full reconnect."""
    kdbai.cleanup_kdbai_client()
    principals = {"alice": _fake_principal("alice", "tok-a"), "bob": _fake_principal("bob", "tok-b")}
    current = {"who": "bob"}
    mocker.patch.object(kdbai, "_current_principal", side_effect=lambda: principals[current["who"]])

    bob_client = mocker.Mock()
    bob_client.database.return_value.table.return_value = "bobs-table"
    alice_client = mocker.Mock()
    alice_client.database.return_value.table.side_effect = Exception("Error during creating connection")
    clients = {"alice": alice_client, "bob": bob_client}
    mocker.patch.object(kdbai, "_open_session", side_effect=lambda cfg, bearer=None: clients[current["who"]])

    cfg = KDBAIConfig(mode="qipc", outbound_strategy="passthrough")

    current["who"] = "bob"
    kdbai.get_table("t", config=cfg)  # bob warms a healthy cached session
    bob_key = (cfg, kdbai._principal_key(principals["bob"]))
    assert bob_key in kdbai._session_cache

    current["who"] = "alice"
    with pytest.raises(Exception):
        kdbai.get_table("t", config=cfg)  # alice's connection error triggers the global nuke

    assert bob_key in kdbai._session_cache, "bob's healthy session must survive alice's connection error"
    kdbai.cleanup_kdbai_client()


def test_session_cache_has_a_bound(mocker):
    """_session_cache is a bare dict with no size cap and no eviction outside a full clear —
    unlike kdb-x's connection cache (capped by @lru_cache), this one grows forever under
    passthrough with many distinct principals."""
    kdbai.cleanup_kdbai_client()
    mocker.patch("kdbai_client.Session", side_effect=lambda *a, **k: mocker.Mock())
    cfg = KDBAIConfig(mode="qipc", outbound_strategy="passthrough")

    for i in range(200):
        p = _fake_principal(f"user{i}", f"tok{i}")
        mocker.patch.object(kdbai, "_current_principal", return_value=p)
        kdbai.get_kdbai_client(cfg)

    assert len(kdbai._session_cache) <= 128, "session cache must be bounded, not grow without limit"
    kdbai.cleanup_kdbai_client()


def test_concurrent_connection_errors_perform_one_coordinated_recovery(mocker):
    """REGRESSION (see mcp-container/adversarial-review-2026-08.md § kdb.ai backend).

    Recovery had no coordination or locking, so N concurrent callers each hitting a connection error
    independently triggered their OWN full cache+token clear — compounding under exactly the load
    conditions (a real outage) the backend can least absorb. A generation counter now means the
    callers who lose the race see that someone has already recovered and skip their own.

    Asserts against `_evict_session` — the actual per-cache-key action `_recover` performs exactly
    once for the caller that wins the generation race — rather than `cleanup_kdbai_client`, which the
    fixed recovery path no longer calls at all (that global nuke is precisely the earlier defect
    `_recover`'s scoped eviction replaced; a version of this test that patched `cleanup_kdbai_client`
    would pass even with the generation gate deleted, since the count it watches would trivially stay
    at zero either way).

    A SECOND barrier sits inside `.table()`'s failure, so all 20 threads are guaranteed to raise their
    exception at the same instant — each having already captured the SAME `_recovery_generation` value
    at the top of `get_table`, before any of them could have incremented it. Without that barrier, the
    GIL can simply run one thread's entire open-fail-recover-retry-succeed cycle to completion before
    a second thread is even scheduled, which — with a `bad_client` that stays broken forever — makes
    `calls["n"] == 1` pass by accident (only one thread ever actually experiences a failure) regardless
    of whether the generation gate is doing anything at all; confirmed by deliberately deleting the
    gate and watching that version of this test still pass. With the second barrier, every one of the
    20 threads reaches `_recover` with an identical captured generation, so the assertion is exercising
    the gate itself: with it, lock ordering lets exactly one thread through and the rest see a
    generation mismatch; with it deleted, all 20 would call `_evict_session` unconditionally."""
    import threading

    kdbai.cleanup_kdbai_client()
    mocker.patch.object(kdbai, "_current_principal", return_value=None)
    fail_barrier = threading.Barrier(20)
    bad_client = mocker.Mock()

    def _fail(*a, **k):
        fail_barrier.wait()  # hold every caller here until all 20 are about to raise together
        raise Exception("Error during creating connection")

    bad_client.database.return_value.table.side_effect = _fail
    mocker.patch.object(kdbai, "_open_session", return_value=bad_client)

    calls = {"n": 0}
    lock = threading.Lock()
    real_evict = kdbai._evict_session

    def _counting_evict(cache_key):
        with lock:
            calls["n"] += 1
        real_evict(cache_key)

    mocker.patch.object(kdbai, "_evict_session", side_effect=_counting_evict)
    cfg = KDBAIConfig(mode="qipc", outbound_strategy="passthrough")
    start_barrier = threading.Barrier(20)

    def _go():
        start_barrier.wait()
        try:
            kdbai.get_table("t", config=cfg)
        except Exception:
            pass

    threads = [threading.Thread(target=_go) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert calls["n"] == 1, f"expected exactly one coordinated eviction, got {calls['n']}"
    real_evict((cfg, None))


def test_invalidate_is_called_when_kdb_ai_rejects_the_service_account_token(mocker):
    """invalidate() is never reached from get_table's retry path — that path only matches
    'Error during creating connection', not an auth rejection. A revoked/rejected service-account
    token must be purged so the next call re-mints, not retried unchanged."""
    import time

    kdbai.cleanup_kdbai_client()
    kdbai_auth.cleanup_token_managers()
    cfg = _cfg()
    mgr = kdbai_auth.get_token_manager(cfg)
    mgr._token = "stale-token"
    mgr._expiry = time.time() + 3600  # not expired by the manager's own clock

    bad_client = mocker.Mock()
    bad_client.database.return_value.table.side_effect = Exception("401 Unauthorized: token rejected")
    mocker.patch.object(kdbai, "_open_session", return_value=bad_client)

    with pytest.raises(Exception):
        kdbai.get_table("t", config=cfg)

    assert kdbai_auth.get_token_manager(cfg)._token != "stale-token"
    kdbai.cleanup_kdbai_client()
    kdbai_auth.cleanup_token_managers()


def test_nonpositive_expires_in_is_clamped_so_the_token_still_caches(mocker):
    """REGRESSION (see mcp-container/adversarial-review-2026-08.md § kdb.ai backend).

    `expires_in <= 0` made the freshness check (`_now() < expiry - buffer`) unsatisfiable, so a
    fresh token was minted on EVERY call instead of being cached — a misconfigured or hostile IdP
    turned the container into a loop hammering that endpoint. Any lifetime at or below the freshness
    buffer is now clamped to the default, with a warning naming the reported value."""
    kdbai_auth.cleanup_token_managers()
    cfg = _cfg()
    ex = _patch_exchange(mocker, return_value=_cred("tok", expires_in=0))
    mgr = kdbai_auth.get_token_manager(cfg)

    for _ in range(5):
        mgr.get_token()

    assert ex.call_count == 1, f"expected the token to be cached, got {ex.call_count} mint calls"
    kdbai_auth.cleanup_token_managers()


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
    """get_table's reconnect branch: a connection error evicts and retries exactly once.

    Recovery now evicts only the FAILING `(config, principal)` entry, where it used to call
    `cleanup_kdbai_client()` and clear every principal's session plus every backend's cached token.
    So this asserts the scoped eviction, not the global nuke it used to assert — the change is the
    fix, not a relaxation. `test_one_principals_connection_error_does_not_disrupt_another_principals_session`
    is the test that pins why it matters.
    """
    kdbai.cleanup_kdbai_client()
    working_client = mocker.Mock()
    get_client = mocker.patch.object(
        kdbai, "get_kdbai_client",
        side_effect=[Exception("Error during creating connection"), working_client],
    )
    evict = mocker.patch.object(kdbai, "_evict_session")
    nuke = mocker.patch.object(kdbai, "cleanup_kdbai_client")

    table = kdbai.get_table("docs", "mydb", KDBAIConfig())

    assert table is working_client.database.return_value.table.return_value
    evict.assert_called_once()
    nuke.assert_not_called()  # recovery must not clear every principal's session
    assert get_client.call_count == 2


def test_get_table_purges_the_token_on_an_auth_rejection(mocker):
    """REGRESSION (see mcp-container/adversarial-review-2026-08.md § kdb.ai backend).

    `ServiceAccountTokenManager.invalidate()` was dead production code: the retry path matched only
    the literal `"Error during creating connection"`, so a token the KDB.AI server *rejects* — revoked,
    or expired on an already-live cached session — was replayed on every call until the process
    restarted. An auth-shaped failure now purges that config's token so the next call re-mints.
    """
    kdbai.cleanup_kdbai_client()
    working_client = mocker.Mock()
    mocker.patch.object(
        kdbai, "get_kdbai_client",
        side_effect=[Exception("401 Unauthorized: token rejected"), working_client],
    )
    mocker.patch.object(kdbai, "_evict_session")
    manager = mocker.Mock()
    mocker.patch.object(kdbai, "get_token_manager", return_value=manager)

    kdbai.get_table("docs", "mydb", _cfg())

    manager.invalidate.assert_called_once()


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
