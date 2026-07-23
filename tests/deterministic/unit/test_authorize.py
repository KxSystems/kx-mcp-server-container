"""The capability-check fastmcp binding: the @authorize decorator + the static YAML capability
adapter.

In-process and license-free. ``current_principal()`` (the fastmcp contextvar) is monkeypatched to a
fake token so we drive the decorator without standing up a server; the over-the-wire proof (audit
enrichment + denial rendering across the mount boundary) is the integration test.

Covers: route-only allow (authz off), namespace derived from the resource prefix, group-intersection
allow/deny, the decorated-but-unlisted -> deny posture, the contextvar carrying the decision for
audit, and the static adapter's loud failure on a missing/garbled policy file.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Optional

import pytest

import importlib

from kx_mcp_core.auth import (
    AuthorizationDenied,
    AuthzSettings,
    authorize,
    configure_authz,
    current_authz_decision,
)

# The package attribute `kx_mcp_core.auth.authorize` is the *function* (re-exported), shadowing the
# submodule — so fetch the real module object to monkeypatch its `current_principal` lookup.
authorize_mod = importlib.import_module("kx_mcp_core.auth.authorize")


@dataclass
class _FakeToken:
    client_id: str = "svc"
    claims: Optional[dict] = None


@pytest.fixture
def as_principal(monkeypatch):
    """Make current_principal() (as seen by the decorator) return a token with the given claims."""

    def _set(claims: Optional[dict], client_id: str = "svc"):
        monkeypatch.setattr(
            authorize_mod, "current_principal", lambda: _FakeToken(client_id, claims)
        )

    return _set


@pytest.fixture
def policy_file(tmp_path):
    p = tmp_path / "capability-policy.yaml"
    p.write_text(
        "kdbx:\n"
        "  write: [admin]\n"
        "example:\n"
        "  write: [traders, admin]\n"
    )
    return str(p)


@pytest.fixture(autouse=True)
def _reset_authz():
    """Reset the cached settings between tests so env/state never leaks across cases."""
    authorize_mod._SETTINGS = None
    current_authz_decision.set(None)
    yield
    authorize_mod._SETTINGS = None


def invoke(fn):
    """Run a gated tool (sync/async) and return ``(result, exc, decision)``.

    The decision is read **inside** the run: ``asyncio.run`` starts a fresh contextvar context, so
    the decorator's ``current_authz_decision.set(...)`` would be discarded before an outer ``.get()``
    (in the real server the middleware reads it within the same awaited task — see the integration
    test)."""

    async def arun():
        try:
            r = await fn()
            return r, None, current_authz_decision.get()
        except Exception as e:  # noqa: BLE001 — tests inspect the captured exception
            return None, e, current_authz_decision.get()

    if asyncio.iscoroutinefunction(fn):
        return asyncio.run(arun())
    try:
        return fn(), None, current_authz_decision.get()
    except Exception as e:  # noqa: BLE001
        return None, e, current_authz_decision.get()


# --- route-only (authz off) --------------------------------------------------------------------


def test_route_only_allows_when_authz_unset(as_principal):
    """KX_MCP_AUTHZ unset => mode="" => decide() route-only allow; the decorated tool just runs."""
    configure_authz(AuthzSettings(mode=""))
    as_principal({"sub": "alice", "groups": []})

    @authorize(action="write", resource="example:thing")
    async def tool():
        return "ran"

    result, exc, decision = invoke(tool)
    assert result == "ran" and exc is None
    assert decision.allowed is True and decision.adapter is None


# --- static adapter: allow / deny by group ----------------------------------------------------


def test_static_allows_when_group_matches(as_principal, policy_file):
    configure_authz(AuthzSettings(mode="static", policy_file=policy_file))
    as_principal({"sub": "alice", "groups": ["traders"]})

    @authorize(action="write", resource="example:thing")
    async def tool():
        return "ran"

    result, exc, decision = invoke(tool)
    assert result == "ran" and exc is None
    assert decision.allowed is True and decision.adapter == "static"


def test_static_denies_when_group_missing(as_principal, policy_file):
    configure_authz(AuthzSettings(mode="static", policy_file=policy_file))
    as_principal({"sub": "bob", "groups": ["viewers"]})

    @authorize(action="write", resource="example:thing")
    async def tool():
        return "ran"

    result, exc, decision = invoke(tool)
    assert isinstance(exc, AuthorizationDenied)
    assert "not authorized: write on example:thing" in str(exc)
    assert decision.allowed is False


def test_namespace_derived_from_resource_prefix(as_principal, policy_file):
    """resource 'kdbx:sql' -> namespace 'kdbx'; the admin grant under kdbx: applies."""
    configure_authz(AuthzSettings(mode="static", policy_file=policy_file))
    as_principal({"sub": "root", "groups": ["admin"]})

    @authorize(action="write", resource="kdbx:sql")
    async def tool():
        return "ran"

    result, exc, _ = invoke(tool)
    assert result == "ran" and exc is None


def test_decorated_but_unlisted_action_denies(as_principal, policy_file):
    """'query' is not listed under kdbx: -> deny (decorated => a declared concern, no grant)."""
    configure_authz(AuthzSettings(mode="static", policy_file=policy_file))
    as_principal({"sub": "root", "groups": ["admin"]})

    @authorize(action="query", resource="kdbx:sql")
    async def tool():
        return "ran"

    _, exc, decision = invoke(tool)
    assert isinstance(exc, AuthorizationDenied)
    assert "decorated but unlisted" in (decision.reason or "")


def test_groups_claim_path_is_configurable(as_principal, policy_file):
    """Entra-shaped: groups live under 'roles', not 'groups'."""
    configure_authz(AuthzSettings(mode="static", policy_file=policy_file, groups_claim="roles"))
    as_principal({"sub": "alice", "roles": ["admin"], "groups": []})

    @authorize(action="write", resource="kdbx:sql")
    async def tool():
        return "ran"

    result, exc, _ = invoke(tool)
    assert result == "ran" and exc is None


def test_sync_tool_is_also_gated(as_principal, policy_file):
    configure_authz(AuthzSettings(mode="static", policy_file=policy_file))
    as_principal({"sub": "bob", "groups": ["viewers"]})

    @authorize(action="write", resource="example:thing")
    def tool():
        return "ran"

    _, exc, _ = invoke(tool)
    assert isinstance(exc, AuthorizationDenied)


# --- static adapter construction (loud config errors) -----------------------------------------


def test_static_requires_policy_file():
    with pytest.raises(ValueError, match="KX_MCP_AUTHZ_POLICY_FILE"):
        configure_authz(AuthzSettings(mode="static"))


def test_static_missing_file_fails_at_configure(tmp_path):
    missing = str(tmp_path / "nope.yaml")
    with pytest.raises(FileNotFoundError):
        configure_authz(AuthzSettings(mode="static", policy_file=missing))


# --- static adapter shape validation (a malformed policy fails loudly, never silently denies) ---
# Regression for the scalar-grant footgun: `write: admin` (vs `write: [admin]`) would make
# `set("admin")` a set of *characters* matching no group — a silent deny-all. The shape is now
# validated against namespace -> action -> [groups] at construction (startup), so each malformation
# is a clear operator error, not a per-request mystery.


@pytest.mark.parametrize(
    "label,body",
    [
        ("scalar_grant", "kdbx:\n  write: admin\n"),  # the footgun: meant `write: [admin]`
        ("int_group", "kdbx:\n  write: [123]\n"),  # a group must be a string, not a bare int
        ("action_is_scalar", "kdbx: oops\n"),  # a namespace must map action -> [groups]
        ("top_level_list", "- kdbx\n- kdbai\n"),  # the whole file must be a mapping
        ("group_is_mapping", "kdbx:\n  write:\n    admin: true\n"),  # groups must be a flat list
    ],
)
def test_static_rejects_malformed_policy_at_configure(tmp_path, label, body):
    p = tmp_path / f"{label}.yaml"
    p.write_text(body)
    with pytest.raises(ValueError, match="is malformed"):
        configure_authz(AuthzSettings(mode="static", policy_file=str(p)))


def test_static_scalar_grant_does_not_silently_deny(tmp_path, as_principal):
    """The specific footgun, end-to-end: a scalar grant must raise at configure — NOT load and then
    deny an admin who *is* in the (mis-typed) group. Proves we fail loud, not silent-wrong."""
    p = tmp_path / "scalar.yaml"
    p.write_text("kdbx:\n  write: admin\n")  # should have been `write: [admin]`
    with pytest.raises(ValueError, match="is malformed"):
        configure_authz(AuthzSettings(mode="static", policy_file=str(p)))


def test_static_empty_file_is_valid_and_denies_everything(tmp_path, as_principal):
    """An empty (or `{}`) policy is well-formed — no namespaces — so every decorated action denies
    (no grant), but it must NOT be a malformed-file error."""
    p = tmp_path / "empty.yaml"
    p.write_text("")  # yaml.safe_load -> None -> {} -> valid empty mapping
    configure_authz(AuthzSettings(mode="static", policy_file=str(p)))
    as_principal({"sub": "root", "groups": ["admin"]})

    @authorize(action="write", resource="kdbx:sql")
    async def tool():
        return "ran"

    _, exc, decision = invoke(tool)
    assert isinstance(exc, AuthorizationDenied)
    assert decision.allowed is False
