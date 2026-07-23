"""Audit logging: record *who / what / outcome* on every dispatch.

Rides the parent ``add_middleware`` seam, logging one line per tool/resource/prompt call with a
uniform Subject/Action/Resource shape (``tool_invoke`` / ``resource_read`` / ``prompt_get``). When
the dispatched primitive carried an ``@authorize`` capability check, the recorded decision and
adapter are folded into the same line.
"""

from __future__ import annotations

import logging

from fastmcp.server.middleware import Middleware, MiddlewareContext

from .authorize import current_authz_decision
from .principal import current_principal

logger = logging.getLogger("kx_mcp.audit")


class AuditMiddleware(Middleware):
    """Emit one audit line per tool/resource/prompt dispatch, before and around the call.

    Folds in the capability-check decision when the dispatched primitive carried an ``@authorize``:
    the decorator (which runs inside the tool) records the :class:`~kx_auth_core.AuthzDecision` on
    the :data:`~kx_mcp_core.auth.authorize.current_authz_decision` contextvar, and this middleware
    reads it after the dispatch — so one line answers who / what / decision / which-adapter.
    ``outcome`` is ``denied`` whenever a recorded decision is not allowed (whether the tool returned
    a denial or the decorator raised); ``ok`` for an allowed or undecorated (route-only) dispatch.
    """

    async def on_call_tool(self, context: MiddlewareContext, call_next):
        return await self._audit("tool_invoke", _target_name(context), context, call_next)

    async def on_read_resource(self, context: MiddlewareContext, call_next):
        return await self._audit("resource_read", _target_uri(context), context, call_next)

    async def on_get_prompt(self, context: MiddlewareContext, call_next):
        return await self._audit("prompt_get", _target_name(context), context, call_next)

    async def _audit(self, action, target, context, call_next):
        token = current_principal()
        subject = (token.client_id if token else None) or "anonymous"
        reset = current_authz_decision.set(None)  # isolate this dispatch; don't inherit a stale one
        try:
            try:
                result = await call_next(context)
            except Exception as exc:
                decision = current_authz_decision.get()
                if decision is not None and not decision.allowed:
                    self._log(subject, action, target, "denied", decision)
                    raise
                self._log(subject, action, target, "error", decision, error=exc)
                raise
            decision = current_authz_decision.get()
            outcome = "denied" if (decision is not None and not decision.allowed) else "ok"
            self._log(subject, action, target, outcome, decision)
            return result
        finally:
            current_authz_decision.reset(reset)

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
