"""Tests for the kdb-x entitlements adapter (data-gate explicit-consult).

The contract under test: the kdb-x entitlements adapter allows / denies / scopes-down by
principal. Mirrors ``test_authz_kx_rbac.py``: the q connection is mocked at the
``_resolve_bound_conn`` unit boundary, and the adapter is driven through the **real**
``kx_auth_core.authz.decide`` contract (registry lookup, adapter stamping, obligations passthrough,
fail-closed-on-raise), so no live kdb+ is needed. The live-q leg belongs to the ``realidp/kdbx``
lane.
"""
from unittest.mock import Mock

import pykx as kx

from kx_auth_core.authz import AuthzDecision, AuthzRequest, authz_adapters, decide
from kx_mcp_kdbx.utils import authz_kx_entitlements as ent
from kx_mcp_kdbx.utils.denial import denial_from_decision


def _req(tables: str, action: str = "read", subject: str = "alice") -> AuthzRequest:
    return AuthzRequest(
        subject=subject,
        action=action,
        resource=ent.RESOURCE_PREFIX + tables,
        namespace="kdbx",
        claims={"sub": subject},
    )


def _entitled_conn(entitled: list[str]) -> Mock:
    """A conn whose `.kx.auth.entitled` call yields the given subset (as q would)."""
    return Mock(return_value=Mock(py=Mock(return_value=list(entitled))))


class TestDeriveTables:
    """derive_tables = the query's identifier tokens ∩ the backend's tables[] (best-effort)."""

    def test_finds_referenced_known_tables(self):
        q = "SELECT t.sym, a.balance FROM trades t JOIN accounts a ON t.sym = a.sym"
        assert ent.derive_tables(q, ["trades", "accounts", "orders"]) == ["accounts", "trades"]

    def test_ignores_keywords_and_unknown_identifiers(self):
        assert ent.derive_tables("SELECT sym FROM trades WHERE size > 10", ["trades"]) == ["trades"]

    def test_case_sensitive_exact_match(self):
        # q table names are case-sensitive; TRADES is not the trades table.
        assert ent.derive_tables("SELECT * FROM TRADES", ["trades"]) == []

    def test_no_known_table_referenced_derives_nothing(self):
        assert ent.derive_tables("SELECT 1+1", ["trades", "accounts"]) == []


class TestResourceRoundTrip:
    """The kdbx:data:<t1>,<t2> resource convention survives the there-and-back."""

    def test_tables_ride_the_resource_string(self):
        req = _req("accounts,trades")
        assert ent.tables_from_resource(req.resource) == ["accounts", "trades"]

    def test_foreign_resource_yields_nothing(self):
        assert ent.tables_from_resource("kdbx:sql") == []


class TestEntitledCheck:
    """entitled_check consults q `.kx.auth.entitled` with symbol-typed args, one round-trip."""

    def test_sends_symbols_and_returns_subset(self):
        conn = _entitled_conn(["trades"])

        result = ent.entitled_check(conn, "read", ["trades", "accounts"])

        assert result == ["trades"]
        args = conn.call_args.args
        assert args[0] == ".kx.auth.entitled"
        assert isinstance(args[1], kx.SymbolAtom) and args[1].py() == "read"
        assert isinstance(args[2], kx.SymbolVector) and args[2].py() == ["trades", "accounts"]


class TestKdbxEntitlementsAdapter:
    """The adapter driven through the real decide() contract: allows / denies / scopes-down."""

    def test_adapter_is_registered_on_import(self):
        assert "kdbx_entitlements" in authz_adapters()

    def test_all_entitled_allows(self, monkeypatch):
        monkeypatch.setattr(ent, "_resolve_bound_conn", lambda: _entitled_conn(["accounts", "trades"]))

        decision = decide(_req("accounts,trades"), strategy="kdbx_entitlements")

        assert decision.allowed is True
        assert decision.adapter == "kdbx_entitlements"
        assert not decision.obligations

    def test_none_entitled_denies_with_reason(self, monkeypatch):
        monkeypatch.setattr(ent, "_resolve_bound_conn", lambda: _entitled_conn([]))

        decision = decide(_req("trades", subject="bob"), strategy="kdbx_entitlements")

        assert decision.allowed is False
        assert decision.adapter == "kdbx_entitlements"
        assert "bob" in decision.reason and "trades" in decision.reason

    def test_partial_entitlement_scopes_down(self, monkeypatch):
        """THE scope-down: a strict subset → allow-with-obligations (entitled + denied lists)."""
        monkeypatch.setattr(ent, "_resolve_bound_conn", lambda: _entitled_conn(["trades"]))

        decision = decide(_req("accounts,trades"), strategy="kdbx_entitlements")

        assert decision.allowed is True
        assert decision.adapter == "kdbx_entitlements"
        assert decision.obligations["entitled"] == ["trades"]
        assert decision.obligations["denied"] == ["accounts"]
        assert "accounts" in decision.reason

    def test_by_principal(self, monkeypatch):
        """Same request, different bound principal (different q verdict) → different decision —
        the 'by principal' clause: the decision tracks whoever the handle is bound to."""
        monkeypatch.setattr(ent, "_resolve_bound_conn", lambda: _entitled_conn(["trades"]))
        alice = decide(_req("trades", subject="alice"), strategy="kdbx_entitlements")
        monkeypatch.setattr(ent, "_resolve_bound_conn", lambda: _entitled_conn([]))
        bob = decide(_req("trades", subject="bob"), strategy="kdbx_entitlements")

        assert alice.allowed is True
        assert bob.allowed is False

    def test_unbound_handle_q_denial_becomes_deny(self, monkeypatch):
        """require[]'s default-deny ('denied: no valid principal...) maps to a deny, not an error."""
        conn = Mock(side_effect=Exception("denied: no valid principal bound to this handle"))
        monkeypatch.setattr(ent, "_resolve_bound_conn", lambda: conn)

        decision = decide(_req("trades"), strategy="kdbx_entitlements")

        assert decision.allowed is False
        assert "denied" in decision.reason.lower()

    def test_infrastructure_error_fails_closed_via_decide(self, monkeypatch):
        conn = Mock(side_effect=Exception("Connection timeout"))
        monkeypatch.setattr(ent, "_resolve_bound_conn", lambda: conn)

        decision = decide(_req("trades"), strategy="kdbx_entitlements")  # must not raise

        assert decision.allowed is False
        assert "Connection timeout" in decision.reason

    def test_empty_table_set_allows_without_consulting(self, monkeypatch):
        """Nothing derivable to consult → allow (the tool skips the gate for such queries too)."""
        resolve = Mock()
        monkeypatch.setattr(ent, "_resolve_bound_conn", resolve)

        decision = decide(_req(""), strategy="kdbx_entitlements")

        assert decision.allowed is True
        resolve.assert_not_called()


class TestDenialFromDecision:
    """denial_from_decision renders both deny shapes onto the one permission_denied vocabulary."""

    def test_hard_deny_envelope(self):
        d = AuthzDecision(allowed=False, adapter="kdbx_entitlements",
                          reason="bob not permitted read on trades")
        env = denial_from_decision(d)
        assert env["status"] == "error"
        assert env["error_type"] == "permission_denied"
        assert "bob not permitted read on trades" in env["message"]

    def test_scope_down_envelope_carries_guidance(self):
        d = AuthzDecision(
            allowed=True, adapter="kdbx_entitlements",
            reason="scoped down: alice not permitted read on accounts",
            obligations={"entitled": ["trades"], "denied": ["accounts"]},
        )
        env = denial_from_decision(d)
        assert env["error_type"] == "permission_denied"
        assert env["entitled_tables"] == ["trades"]
        assert env["denied_tables"] == ["accounts"]
        # The guidance is agent-actionable: it names what to re-scope to.
        assert "trades" in env["message"] and "re-issue" in env["message"]
