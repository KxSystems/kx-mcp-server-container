"""Unit tests for the principal projection (the kdb+ identity-assertion wire format).

These pin the contract a q-side permission-check function reads. License-free, fastmcp-free,
pykx-free — pure dict-shaping.
"""

from __future__ import annotations

from kx_auth_core import project_from_claims, project_principal


def test_project_principal_core_keys_always_present():
    """sub / client / scopes / claims are always present so q can rely on their shape.

    `groups` is intentionally NOT here — q's `.kx.auth.promote` extracts it from `claims`, so qIPC and
    HTTP share one promotion path. This helper only assembles the ferry shape.
    """
    d = project_principal(subject="alice", client_id="cli", scopes=["kdbx.read"])
    assert d["sub"] == "alice"
    assert d["client"] == "cli"
    assert d["scopes"] == ["kdbx.read"]
    assert d["claims"] == {}
    assert "groups" not in d  # promotion is q-side, not here


def test_subject_falls_back_to_client_id():
    d = project_principal(subject=None, client_id="svc-client")
    assert d["sub"] == "svc-client"


def test_scopes_accepts_space_delimited_string():
    """OAuth `scope` is a space-delimited string; normalise to a list."""
    d = project_principal(subject="alice", scopes="kdbx.read kdbx.write")
    assert d["scopes"] == ["kdbx.read", "kdbx.write"]


def test_optional_keys_emitted_only_when_present():
    """aud / iss / exp / act appear only when they have a value."""
    bare = project_principal(subject="alice")
    assert "aud" not in bare and "iss" not in bare and "exp" not in bare and "act" not in bare

    full = project_principal(
        subject="alice",
        expires_at=1893456000,
        audience="kx-mcp",
        issuer="https://issuer.test",
        act={"sub": "agent-1"},
    )
    assert full["exp"] == 1893456000
    assert full["aud"] == "kx-mcp"
    assert full["iss"] == "https://issuer.test"
    assert full["act"] == {"sub": "agent-1"}


def test_aud_iss_act_derived_from_claims_when_not_explicit():
    d = project_principal(
        subject="alice",
        claims={"aud": "kx-mcp", "iss": "https://issuer.test", "act": {"sub": "agent-1"}},
    )
    assert d["aud"] == "kx-mcp"
    assert d["iss"] == "https://issuer.test"
    assert d["act"] == {"sub": "agent-1"}


def test_exp_coerced_to_int():
    d = project_principal(subject="alice", expires_at=1893456000.0)
    assert d["exp"] == 1893456000
    assert isinstance(d["exp"], int)


def test_project_from_claims_extracts_standard_names():
    """The kx auth assert path: derive structured fields from a raw JWT claims dict."""
    d = project_from_claims(
        {
            "sub": "bob",
            "scope": "kdbx.read",
            "exp": 1893456000,
            "aud": "kx-mcp",
            "iss": "https://issuer.test",
            "azp": "bob-client",
        }
    )
    assert d["sub"] == "bob"
    assert d["client"] == "bob-client"
    assert d["scopes"] == ["kdbx.read"]
    assert d["exp"] == 1893456000
    assert d["aud"] == "kx-mcp"


def test_project_from_claims_accepts_scopes_list():
    d = project_from_claims({"sub": "bob", "scopes": ["a", "b"]})
    assert d["scopes"] == ["a", "b"]


def test_both_entry_points_agree():
    """The two callers must produce an identical wire shape for the same identity."""
    claims = {"sub": "alice", "scope": "kdbx.read", "exp": 1893456000, "aud": "kx-mcp"}
    from_claims = project_from_claims(claims)
    from_fields = project_principal(
        subject="alice", scopes="kdbx.read", expires_at=1893456000, audience="kx-mcp", claims=claims
    )
    assert from_claims == from_fields


# --- ferry shape: groups/tenant are NOT promoted here (q's .kx.auth.promote owns that) ------------

def test_groups_not_promoted_in_ferry_shape():
    """The ferry helper carries raw claims; it does not extract groups — that is q-side."""
    d = project_from_claims({"sub": "alice", "realm_access": {"roles": ["traders"]}})
    assert "groups" not in d
    # the raw claims ride along so q's promote can extract groups from them
    assert d["claims"]["realm_access"] == {"roles": ["traders"]}


def test_raw_claims_preserved_for_q_side_promotion():
    """Custom claims a q policy/promote might read are passed through untouched."""
    d = project_principal(subject="a", claims={"department": "trading-desk", "tenant": "acme"})
    assert d["claims"]["department"] == "trading-desk"
    assert d["claims"]["tenant"] == "acme"
    assert "tenant" not in d  # not promoted to top-level here
