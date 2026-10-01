"""Audit logging: record *who / what / outcome* on every access decision.

Two kinds of decision, one logger (``kx_mcp.audit``), one ``audit key=value …`` line shape:

* **Dispatch** — :class:`AuditMiddleware` rides the parent ``add_middleware`` seam, logging one line
  per tool/resource/prompt call with a uniform Subject/Action/Resource shape (``tool_invoke`` /
  ``resource_read`` / ``prompt_get``). When the dispatched primitive carried an ``@authorize``
  capability check, the recorded decision and adapter are folded into the same line.
* **Authentication** — :func:`audit_authentication_denials` makes the inbound verifier log every
  bearer it *rejects* (``action=authenticate outcome=denied``). A rejected request never reaches
  dispatch, so without this the audit stream could not answer "who tried and failed".
"""

from __future__ import annotations

import logging
import time
from typing import Any

from fastmcp.server.middleware import Middleware, MiddlewareContext
from kx_auth_core import AuthSettings, decode_claims_unverified

from ..observability._outcome import OUTCOME_DENIED, outcome_for_exception, outcome_for_result
from .authorize import authz_decision, begin_authz_dispatch, end_authz_dispatch
from .principal import current_principal, subject_from

logger = logging.getLogger("kx_mcp.audit")


class AuditMiddleware(Middleware):
    """Emit one audit line per tool/resource/prompt dispatch, before and around the call.

    Folds in the capability-check decision when the dispatched primitive carried an ``@authorize``:
    the decorator (which runs inside the tool) records the :class:`~kx_auth_core.AuthzDecision` on
    the :func:`~kx_mcp_core.auth.authz_decision` (the ``AuthzSlot``), and this middleware
    reads it after the dispatch — so one line answers who / what / decision / which-adapter.
    ``outcome`` is classified by the same functions the metrics and tracing middleware use
    (``observability/_outcome.py``), so the audit line and the ``outcome`` label never disagree:
    ``denied`` whenever a recorded decision is not allowed (whether the tool returned a denial or
    the decorator raised); ``error`` for any other raise or a result marked ``isError: true``; ``ok``
    otherwise.
    """

    async def on_call_tool(self, context: MiddlewareContext, call_next):
        return await self._audit("tool_invoke", _target_name(context), context, call_next)

    async def on_read_resource(self, context: MiddlewareContext, call_next):
        return await self._audit("resource_read", _target_uri(context), context, call_next)

    async def on_get_prompt(self, context: MiddlewareContext, call_next):
        return await self._audit("prompt_get", _target_name(context), context, call_next)

    async def _audit(self, action, target, context, call_next):
        # subject_from is shared with the @authorize capability check on purpose: the line must
        # name the same identity the decision was made about (sub claim first, not client_id).
        subject = subject_from(current_principal())
        # A fresh slot per dispatch: isolates this one and lets a stamp made in a worker thread
        # (a sync tool body) reach us — see AuthzSlot.
        reset = begin_authz_dispatch()
        try:
            try:
                result = await call_next(context)
            except Exception as exc:
                outcome = outcome_for_exception()
                # A denial names no error: the refusal is the whole story, and the decision says so.
                error = exc if outcome != OUTCOME_DENIED else None
                self._log(subject, action, target, outcome, authz_decision(), error=error)
                raise
            # Not just the decision: a tool that RETURNS `isError: true` failed too. This used to
            # read only the decision, so every returned failure was audited `ok` while the metric
            # for the same dispatch said `error`.
            self._log(subject, action, target, outcome_for_result(result), authz_decision())
            return result
        finally:
            end_authz_dispatch(reset)

    @staticmethod
    def _log(subject, action, target, outcome, decision, error=None):
        extra = ""
        if decision is not None:
            extra = " decision=%s adapter=%s" % (
                "allow" if decision.allowed else "deny",
                decision.adapter,
            )
        if error is not None:
            logger.info(
                "audit subject=%s action=%s target=%s outcome=%s error=%s%s",
                subject, action, target, outcome, type(error).__name__, extra,
            )
        else:
            logger.info(
                "audit subject=%s action=%s target=%s outcome=%s%s",
                subject, action, target, outcome, extra,
            )


def _target_name(context: MiddlewareContext) -> str:
    return getattr(getattr(context, "message", None), "name", "?")


def _target_uri(context: MiddlewareContext) -> str:
    return str(getattr(getattr(context, "message", None), "uri", "?"))


# --- authentication denials ----------------------------------------------------------------------

#: Modes whose verifier checks the presented token against ``AuthSettings`` (issuer, audience,
#: scopes) directly. The proxy modes (``oidc_proxy``, ``entra``) verify a token the *proxy itself*
#: issued, whose ``iss``/``aud`` are the proxy's own — comparing those against the upstream settings
#: would misdiagnose every denial as an issuer mismatch, so for them only the mode-independent
#: reasons are derived.
_SETTINGS_DESCRIBE_THE_TOKEN = frozenset({"static", "jwks"})
_WRAPPED_TAG = "_kx_mcp_audits_denials"  # marks a wrapped verify_token so wrapping twice is a no-op
#: Longest ``claimed_*`` value written to the log — these come from an unverified token, so an
#: attacker chooses their content and their length.
_CLAIMED_MAX_CHARS = 128


def audit_authentication_denials(provider: Any, settings: AuthSettings) -> Any:
    """Make ``provider`` audit every bearer it rejects, on the same ``kx_mcp.audit`` logger.

    A rejected token never reaches dispatch, so :class:`AuditMiddleware` cannot see it, and the only
    record was FastMCP's DEBUG line in another format. Every mode's provider ends in
    ``verify_token(token) -> None`` on rejection, and the SDK's bearer backend calls that method on
    the provider *instance* — so wrapping it here covers ``static`` / ``jwks`` / ``oidc_proxy`` /
    ``entra`` alike, with no per-mode code. Requests carrying **no** bearer are deliberately not
    audited: that is every MCP client's first discovery probe (RFC 6750 §3.1), not an attempt.

    Returns ``provider`` — the same object, mutated — so ``isinstance`` checks downstream still hold.
    A provider without ``verify_token`` (a test double) is returned untouched.
    """
    verify = getattr(provider, "verify_token", None)
    if verify is None or getattr(verify, _WRAPPED_TAG, False):
        return provider

    async def verify_token(token: str) -> Any:
        result = await verify(token)
        if result is None:
            _log_denied_authentication(settings, token)
        return result

    setattr(verify_token, _WRAPPED_TAG, True)
    setattr(provider, "verify_token", verify_token)
    return provider


def _log_denied_authentication(settings: AuthSettings, token: str) -> None:
    """One ``outcome=denied`` line per rejected bearer. Never raises; never logs the token itself.

    ``claimed_*`` are what the token *said* — decoded without verification, so they name who kept
    trying and must never be read as who was proven. Values with whitespace, or longer than
    ``_CLAIMED_MAX_CHARS``, are dropped rather than break the one-token-per-field line shape or
    let a forged token dictate the size of a log line.
    """
    try:
        claims = decode_claims_unverified(token)
        reason = _denial_reason(settings, claims)
        claimed = "".join(
            f" claimed_{name}={value}"
            for name, value in (
                ("iss", claims.get("iss")),
                ("sub", claims.get("sub")),
                ("azp", claims.get("azp") or claims.get("client_id")),
            )
            if isinstance(value, str)
            and value
            and len(value) <= _CLAIMED_MAX_CHARS
            and not any(ch.isspace() for ch in value)
        )
    except Exception:  # the diagnosis is best-effort; the denial itself must still be recorded
        reason, claimed = "unknown", ""
    logger.info(
        "audit subject=anonymous action=authenticate target=%s outcome=denied error=invalid_token reason=%s%s",
        settings.mode,
        reason,
        claimed,
    )


def _denial_reason(settings: AuthSettings, claims: dict[str, Any]) -> str:
    """Best-effort diagnosis of why the verifier said no, from the *unverified* claims.

    FastMCP returns only ``None`` for a rejection. Re-verifying to learn the reason would cost a
    second signature check and, for ``jwks``, a network call per bad token — an amplifier on the
    failure path. Comparing the presented claims against the settings is free and exact for every
    reason but one: when everything checkable matches, the failure lay in the signature or the key
    material (a wrong key, an unknown ``kid``, an unreachable JWKS), which cannot be told apart from
    outside the verifier — hence ``signature_or_key``.
    """
    if not claims:
        return "malformed"  # not a JWT, or one that does not decode
    exp = claims.get("exp")
    if isinstance(exp, (int, float)) and exp < time.time():
        return "expired"
    if settings.mode in _SETTINGS_DESCRIBE_THE_TOKEN:
        if settings.issuer and claims.get("iss") != settings.issuer:
            return "issuer"
        if settings.audience:
            aud = claims.get("aud")
            audiences = aud if isinstance(aud, list) else [aud]
            if settings.audience not in audiences:
                return "audience"
        if settings.required_scopes:
            granted = set(str(claims.get("scope") or "").split())
            scp = claims.get("scp")
            if isinstance(scp, list):
                granted |= {str(item) for item in scp}
            if not set(settings.required_scopes) <= granted:
                return "scope"
    return "signature_or_key"
