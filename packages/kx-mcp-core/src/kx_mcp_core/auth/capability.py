"""The built-in ``static`` capability adapter: a YAML group-grant store.

Answers exactly one question — *may this subject's GROUPS invoke this tool-class (``action``) in this
backend (``namespace``)?* The file is keyed ``namespace -> action -> [groups]``; a subject is allowed
iff their groups intersect the listed groups. It does **not** decide which rows / tables / symbols the
subject sees — that is the backend's own data gate (KDB.AI ACL, q ``.kx.auth.authorize``, or a
downstream extension's native policy engine).

* a decorated action **absent** from the file is **denied** — decorating a primitive declares a
  capability concern; no grant for it means nobody is granted. Route-only is expressed by *not
  decorating* the primitive, never by omitting a line here.
* the file is loaded + **shape-validated** once at construction (against the ``namespace -> action ->
  [groups]`` type via a pydantic :class:`~pydantic.TypeAdapter`), so a missing or *malformed* file
  fails loudly at startup (a clean operator error), not as a silent per-request deny. In particular a
  scalar grant (the natural typo ``write: admin`` instead of ``write: [admin]``) is **rejected** — it
  would otherwise make ``set("admin")`` a set of *characters* that silently matches no group.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

import yaml
from pydantic import TypeAdapter, ValidationError

from kx_auth_core import AuthzDecision, AuthzRequest

from .authz_settings import AuthzSettings

# The grant store's shape: namespace -> action -> [groups]. Validated at construction so a malformed
# file (a scalar grant, a non-string group, a non-mapping at any level) is a loud startup error, not a
# silent per-request deny. A bare `Mapping[str, Any]` annotation can't enforce this — YAML crosses a
# runtime trust boundary, so the type must be checked against the loaded data, not just annotated.
_PolicyShape = Dict[str, Dict[str, List[str]]]
_POLICY_ADAPTER: TypeAdapter[_PolicyShape] = TypeAdapter(_PolicyShape)


class StaticCapabilityAdapter:
    """A callable :class:`~kx_auth_core.AuthzAdapter` backed by a YAML grant store."""

    def __init__(self, settings: AuthzSettings) -> None:
        if not settings.policy_file:
            raise ValueError("KX_MCP_AUTHZ=static requires KX_MCP_AUTHZ_POLICY_FILE")
        text = Path(settings.policy_file).read_text()  # missing file -> clear startup error
        loaded = yaml.safe_load(text) or {}
        try:
            # Validate the loaded data against namespace -> action -> [groups]. Rejects a scalar grant
            # (`write: admin`), a non-string group, or a non-mapping at any level — a loud startup
            # error instead of a silent deny-all. `pydantic` is already a container dependency.
            self._policy: _PolicyShape = _POLICY_ADAPTER.validate_python(loaded)
        except ValidationError as exc:
            raise ValueError(
                f"KX_MCP_AUTHZ_POLICY_FILE {settings.policy_file!r} is malformed "
                f"(expected namespace -> action -> [groups]):\n{exc}"
            ) from exc
        self._groups_claim = settings.groups_claim

    def __call__(self, request: AuthzRequest) -> AuthzDecision:
        ns = self._policy.get(request.namespace)
        if ns is None:
            return AuthzDecision(False, reason=f"no policy for namespace {request.namespace!r}")
        granted = ns.get(request.action)
        if granted is None:
            # Decorated but unlisted -> deny (a declared concern with no grant). NOT route-only.
            return AuthzDecision(
                False,
                reason=f"no grant for {request.namespace}:{request.action} (decorated but unlisted)",
            )
        subject_groups = _as_groups(request.claims.get(self._groups_claim))
        allowed = bool(set(subject_groups) & set(granted))  # granted is a validated List[str]
        reason = None if allowed else f"groups {subject_groups} not in {list(granted)}"
        return AuthzDecision(allowed, reason=reason)


def _as_groups(value: Any) -> list[str]:
    """Normalise a groups claim: a list (Keycloak/Entra), a space-delimited string, or absent."""
    if value is None:
        return []
    if isinstance(value, str):
        return value.split()
    if isinstance(value, (list, tuple)):
        return [str(g) for g in value]
    return []
