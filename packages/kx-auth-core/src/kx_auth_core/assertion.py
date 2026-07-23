"""Shape a validated principal into the dict ferried to kdb+ for identity assertion.

Plain kdb+ has no bearer concept over qIPC, so the container does not mint a token for it (that's
the ``outbound`` token-exchange seam used by OAuth backends such as KDB.AI). Instead it **asserts** the caller's
identity: it ferries the validated structured fields + the raw claims to q, which calls
``.kx.auth.bind[principal]``.

**Promotion lives in q, not here.** ``.kx.auth.promote`` is the single authority that extracts
``groups``/``tenant`` from the configured claim path, symbolises the promoted fields, and
canonicalises ``exp`` — so the qIPC and HTTP transports converge on one promotion implementation.
This helper only assembles the ferry shape: the structured fields q can't re-derive (``sub`` /
``client`` / ``scopes`` / ``exp`` / ``aud`` / ``iss`` / ``act``) plus the raw ``claims`` blob q
promotes from. It does not extract groups.

It is deliberately:

- **fastmcp-free** — operates on plain claim fields / a claims ``dict``, not fastmcp's
  ``AccessToken``, so the container, the bundles, and the fastmcp-free ``kx auth`` CLI reuse one
  implementation.
- **pykx-free** — returns a plain Python ``dict``. The kdbx connection layer (which has PyKX) wraps
  the raw ``claims`` string values as ``CharVector`` before sending, so high-cardinality values
  (``jti`` …) do not intern as q symbols; q symbolises only the promoted fields.

Two entry points, one implementation: :func:`project_principal` builds the ferry dict from
already-extracted fields (what the kdbx connection layer has from ``current_principal()``);
:func:`project_from_claims` builds it from a raw JWT claims dict (an indicative preview used by
``kx auth assert --principal`` — the authoritative shape is whatever ``.kx.auth.promote`` produces).
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence


def _as_scope_list(scopes: Sequence[str] | str | None) -> list[str]:
    """Normalise scopes to a list. Accepts a list or an OAuth space-delimited ``scope`` string."""
    if scopes is None:
        return []
    if isinstance(scopes, str):
        return scopes.split()
    return [str(s) for s in scopes]


def project_principal(
    *,
    subject: str | None,
    client_id: str | None = None,
    scopes: Sequence[str] | str | None = None,
    expires_at: int | None = None,
    audience: Any = None,
    issuer: str | None = None,
    act: Mapping[str, Any] | None = None,
    claims: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Assemble the ferry dict from extracted fields.

    The structured fields q cannot re-derive are passed through; ``aud`` / ``iss`` / ``act`` fall back
    to the raw ``claims`` when not given explicitly. ``sub`` / ``client`` / ``scopes`` / ``claims`` are
    always present so the q side can rely on their shape. **No groups/tenant extraction here** — q's
    ``promote`` reads those from ``claims`` so qIPC and HTTP share one promotion path.
    """
    claims = dict(claims or {})
    sub = subject or client_id or ""

    if audience is None:
        audience = claims.get("aud")
    if issuer is None:
        issuer = claims.get("iss")
    if act is None:
        act = claims.get("act")

    out: dict[str, Any] = {
        "sub": sub,
        "client": client_id or "",
        "scopes": _as_scope_list(scopes),
        "claims": claims,
    }
    if audience is not None:
        out["aud"] = audience
    if issuer:
        out["iss"] = issuer
    if expires_at is not None:
        out["exp"] = int(expires_at)
    if act:
        out["act"] = dict(act)
    return out


def project_from_claims(claims: Mapping[str, Any]) -> dict[str, Any]:
    """Assemble the ferry dict from a raw JWT claims dict (the ``kx auth assert`` preview path).

    Derives the structured fields (``sub`` / ``client`` / scopes / ``exp``) from the standard claim
    names, then delegates to :func:`project_principal`. Groups are *not* derived here — they are q's
    job (``.kx.auth.promote``), so this is an indicative preview of what gets ferried, not the final
    promoted principal.
    """
    claims = dict(claims)
    scope = claims.get("scope")
    if scope is None:
        scope = claims.get("scopes")
    return project_principal(
        subject=claims.get("sub"),
        client_id=claims.get("client_id") or claims.get("azp"),
        scopes=scope,
        expires_at=claims.get("exp"),
        audience=claims.get("aud"),
        issuer=claims.get("iss"),
        act=claims.get("act"),
        claims=claims,
    )
