"""Tests for the kdb-x RBAC authorization adapter (capability check).

Cover the contract-independent mechanism — `rbac_check` consulting the q `.kx.auth` policy engine —
the shared `is_denial` predicate, and the `kdbx_rbac` adapter driven through the real
`kx_auth_core.authz.decide` contract. The q connection is mocked, so no live kdb+ is needed.
"""
from unittest.mock import Mock

import pykx as kx

from kx_auth_core.authz import AuthzRequest, authz_adapters, decide
from kx_mcp_kdbx.utils import authz_kx_rbac
from kx_mcp_kdbx.utils.authz_kx_rbac import rbac_check
from kx_mcp_kdbx.utils.denial import is_denial


class TestRbacCheck:
    """rbac_check delegates the decision to .kx.auth.authorize on the bound handle."""

    def test_allow_returns_principal_and_sends_symbols(self):
        """On allow, returns whatever q returns and sends (action;resource) as q symbols."""
        principal = {"sub": "alice"}
        conn = Mock(return_value=principal)

        result = rbac_check(conn, "query", "kdbx:sql")

        assert result is principal
        # Called .kx.auth.authorize with two SymbolAtoms carrying the action + resource.
        args = conn.call_args.args
        assert args[0] == ".kx.auth.authorize"
        assert isinstance(args[1], kx.SymbolAtom) and args[1].py() == "query"
        # The colon-namespaced MCP resource survives the symbol round-trip.
        assert isinstance(args[2], kx.SymbolAtom) and args[2].py() == "kdbx:sql"

    def test_deny_propagates_q_denied_signal(self):
        """On deny, the q `'denied: ...` error propagates for the caller to map."""
        conn = Mock(side_effect=Exception("denied: alice not permitted query on kdbx:sql"))

        try:
            rbac_check(conn, "query", "kdbx:sql")
            assert False, "expected the denial to propagate"
        except Exception as exc:
            assert is_denial(exc)


class TestIsDenial:
    """is_denial is the single source for the `.kx.auth` `denied:`-prefix convention."""

    def test_recognises_denied_prefix_case_insensitively(self):
        assert is_denial(Exception("denied: bob not permitted read on trades"))
        assert is_denial(Exception("DENIED: nope"))
        assert is_denial(Exception("  denied: leading space"))

    def test_other_errors_are_not_denials(self):
        assert not is_denial(Exception("Connection timeout"))
        assert not is_denial(Exception("some error with .s.e in it"))


def _req(action: str = "query", resource: str = "kdbx:sql") -> AuthzRequest:
    return AuthzRequest(
        subject="alice", action=action, resource=resource, namespace="kdbx", claims={"sub": "alice"}
    )


class TestKdbxRbacAdapter:
    """The `kdbx_rbac` adapter driven through the real `kx_auth_core.authz.decide` contract.

    The adapter resolves its bound handle from the request context, so the unit boundary is
    `_resolve_bound_conn` — monkeypatched to a mock conn. Everything else exercises the genuine
    contract: the registry, `decide`'s adapter stamping, bool->decision normalisation, and the
    fail-closed-on-raise posture.
    """

    def test_adapter_is_registered_on_import(self):
        """Importing the module self-registers `kdbx_rbac` (mirrors the outbound built-ins)."""
        assert "kdbx_rbac" in authz_adapters()

    def test_allow_routes_through_decide_and_stamps_adapter(self, monkeypatch):
        """q allow -> True -> decide() returns allowed, adapter stamped, no obligations."""
        conn = Mock(return_value={"sub": "alice"})
        monkeypatch.setattr(authz_kx_rbac, "_resolve_bound_conn", lambda: conn)

        decision = decide(_req(), strategy="kdbx_rbac")

        assert decision.allowed is True
        assert decision.adapter == "kdbx_rbac"
        # The MCP-semantic (action;resource) reached the q policy call as symbols.
        args = conn.call_args.args
        assert args[0] == ".kx.auth.authorize"
        assert args[1].py() == "query" and args[2].py() == "kdbx:sql"

    def test_q_denial_becomes_deny_with_reason(self, monkeypatch):
        """A `'denied: ...` q signal -> AuthzDecision(allowed=False) carrying the reason."""
        conn = Mock(side_effect=Exception("denied: alice not permitted query on kdbx:sql"))
        monkeypatch.setattr(authz_kx_rbac, "_resolve_bound_conn", lambda: conn)

        decision = decide(_req(), strategy="kdbx_rbac")

        assert decision.allowed is False
        assert decision.adapter == "kdbx_rbac"
        assert "denied" in (decision.reason or "").lower()

    def test_infrastructure_error_fails_closed_via_decide(self, monkeypatch):
        """A non-denial error is re-raised so decide() fails closed (not a fall-through allow)."""
        conn = Mock(side_effect=Exception("Connection timeout"))
        monkeypatch.setattr(authz_kx_rbac, "_resolve_bound_conn", lambda: conn)

        decision = decide(_req(), strategy="kdbx_rbac")  # must not raise

        assert decision.allowed is False
        assert decision.adapter == "kdbx_rbac"
        assert "Connection timeout" in (decision.reason or "")
