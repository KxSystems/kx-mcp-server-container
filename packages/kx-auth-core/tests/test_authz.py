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


def test_decision_obligations_are_not_aliased_to_the_adapters_dict():
    """`decide` hands the adapter's own AuthzDecision back via `replace(result, adapter=strategy)` —
    a SHALLOW copy — so `decision.obligations` is the very dict object the adapter owns. `decide`
    must return an independent snapshot; the post-append assertion forces the copy to be DEEP, since
    a top-level `dict()` copy still shares `obligations["entitled"]`."""
    shared = {"entitled": ["t1"]}
    register_authz_adapter(
        "test_obligation_aliasing", lambda req: AuthzDecision(allowed=True, obligations=shared)
    )

    decision = decide(_req(), strategy="test_obligation_aliasing")
    assert decision.allowed is True
    assert decision.obligations == {"entitled": ["t1"]}

    shared["entitled"].append("t2")  # the adapter reuses / extends its cached payload
    shared["injected"] = True

    assert decision.obligations is not shared
    assert decision.obligations == {"entitled": ["t1"]}


def test_self_stamping_adapters_obligations_are_also_snapshotted():
    """The aliasing fix must cover BOTH `decide()` return paths. An adapter that names itself skips
    `replace(result, adapter=...)` entirely, so a fix applied only to the stamping path would leave
    exactly this decision sharing the adapter's mapping — the harder half of the same bug, and the
    one a `replace`-only fix silently misses."""
    shared = {"entitled": ["t1"]}
    register_authz_adapter(
        "test_self_stamped_aliasing",
        lambda req: AuthzDecision(allowed=True, adapter="i-name-myself", obligations=shared),
    )

    decision = decide(_req(), strategy="test_self_stamped_aliasing")
    assert decision.adapter == "i-name-myself"  # self-stamp still respected

    shared["entitled"].append("t2")
    assert decision.obligations == {"entitled": ["t1"]}


def test_adapter_decision_keeps_its_own_adapter_stamp():
    """If an adapter names itself (e.g. delegating to a sub-adapter), decide() does not overwrite it."""

    def delegating(req):
        return AuthzDecision(allowed=False, adapter="downstream", reason="denied downstream")

    register_authz_adapter("test_delegating", delegating)
    decision = decide(_req(), strategy="test_delegating")
    assert decision.adapter == "downstream"
