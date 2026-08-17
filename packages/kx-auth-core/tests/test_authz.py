"""Authorization-decision seam: the registry + decide()'s three postures + obligations.

The contract package is fastmcp-free and policy-free, so these are pure-local: register a fake
adapter, drive decide(), assert the decision shape. No backend, no fastmcp. Mirrors test_outbound.py
(registry lookup, unknown-strategy ValueError, the postures split, payload passthrough).

The action/resource vocabulary is a documented convention, NOT validated here — so there is
deliberately no "rejects a bad action" test; that agreement is exercised where the q gate keys on
the strings.
"""

from __future__ import annotations

import pytest

from kx_auth_core import (
    AuthzDecision,
    AuthzRequest,
    authz_adapters,
    decide,
    register_authz_adapter,
)


def _req(action: str = "query", resource: str = "kdbx.sql") -> AuthzRequest:
    return AuthzRequest(
        subject="alice",
        action=action,
        resource=resource,
        namespace="kdbx",
        claims={"sub": "alice"},
    )


# --- registry ----------------------------------------------------------------------------------


def test_register_and_list_adapters():
    register_authz_adapter("test_list", lambda req: True)
    assert "test_list" in authz_adapters()
    assert authz_adapters() == sorted(authz_adapters())  # stable, sorted (mirrors outbound_strategies)


def test_unknown_strategy_raises():
    """A *named* strategy that was never registered is a config error — loud, mirrors exchange()."""
    with pytest.raises(ValueError, match="unknown authz strategy"):
        decide(_req(), strategy="never_registered")


# --- the three postures ------------------------------------------------------------------------


@pytest.mark.parametrize("strategy", [None, "", "unset", "none"])
def test_route_only_allows_when_no_adapter(strategy):
    """Unset / sentinel strategy => allow, adapter=None (the inbound `unset` opt-in precedent)."""
    decision = decide(_req(), strategy=strategy)
    assert decision.allowed is True
    assert decision.adapter is None
    assert decision.obligations == {}


def test_adapter_bool_true_allows_and_stamps_adapter():
    register_authz_adapter("test_yes", lambda req: True)
    decision = decide(_req(), strategy="test_yes")
    assert decision.allowed is True
    assert decision.adapter == "test_yes"


def test_adapter_bool_false_denies():
    register_authz_adapter("test_no", lambda req: False)
    decision = decide(_req(), strategy="test_no")
    assert decision.allowed is False
    assert decision.adapter == "test_no"


def test_adapter_exception_fails_closed_never_raises():
    """A registered adapter that throws => deny, NOT an exception bubbling up or a fall-through allow."""

    def boom(req):
        raise RuntimeError("backend unreachable")

    register_authz_adapter("test_boom", boom)
    decision = decide(_req(), strategy="test_boom")  # must not raise
    assert decision.allowed is False
    assert decision.adapter == "test_boom"
    assert "backend unreachable" in (decision.reason or "")


# --- obligations passthrough (the data-gate scope-down bridge) ----------------------------------


def test_adapter_decision_passes_obligations_through():
    """An adapter returning a full AuthzDecision keeps its obligations; adapter is stamped if unset."""
    obligations = {"entitled_tables": ["trades", "quotes"], "filter": "region=`EMEA"}

    def scoped(req):
        return AuthzDecision(allowed=True, reason="scoped to team", obligations=obligations)

    register_authz_adapter("test_scoped", scoped)
    decision = decide(_req(), strategy="test_scoped")
    assert decision.allowed is True
    assert decision.adapter == "test_scoped"  # stamped (adapter left None by the adapter)
    assert decision.reason == "scoped to team"
    assert decision.obligations == obligations


def test_adapter_decision_keeps_its_own_adapter_stamp():
    """If an adapter names itself (e.g. delegating to a sub-adapter), decide() does not overwrite it."""

    def delegating(req):
        return AuthzDecision(allowed=False, adapter="downstream", reason="denied downstream")

    register_authz_adapter("test_delegating", delegating)
    decision = decide(_req(), strategy="test_delegating")
    assert decision.adapter == "downstream"
