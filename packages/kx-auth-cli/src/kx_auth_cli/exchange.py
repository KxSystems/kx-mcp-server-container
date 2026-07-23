"""``kx auth exchange`` — swap a subject token for a backend-scoped credential (RFC 8693 from a shell).

A thin wrapper over the shared :func:`kx_auth_core.exchange` seam — the *same* outbound code path the
container's backends use — so the wire shape and audit chain are one implementation. Default strategy
is ``rfc_8693`` (the workload-identity bootstrap target: ``kubectl create token`` / ``gh
actions-token`` → ``kx auth exchange`` → backend-scoped token); ``passthrough`` / ``service_account``
/ custom registered strategies are selectable too.

Subject-token source (first found wins): ``--subject`` → ``$KX_AUTH_TOKEN`` → stdin → the cached
``login`` token for ``--server`` (the seamless ``login`` → ``exchange`` chain). ``service_account``
needs no subject. Maps the seam's result to the stable exit-code contract: ``0`` ok · ``1`` error ·
``2`` usage · ``3`` auth-required (the only subject candidate was an expired ``login`` cache entry) ·
``4`` denied (a ``passthrough`` audience-guard refusal).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time

from kx_auth_core import OutboundConfig, exchange

from . import cache

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_AUTH_REQUIRED = 3
EXIT_DENIED = 4

_NEEDS_SUBJECT = {"passthrough", "rfc_8693"}

HELP_DESCRIPTION = (
    "Exchange a subject token for a backend-scoped credential via the shared outbound seam "
    "(the same code path the container's backends use). Default strategy: rfc_8693."
)
HELP_EPILOG = """\
exit codes (branch on the code, not the text):
  0  ok             a credential was minted/forwarded
  1  error          misconfig, unreachable endpoint, or the endpoint returned no token
  2  usage          bad flags, or no subject token for a strategy that needs one
  3  auth-required  the cached login for --server has expired — `kx auth login` again
  4  denied         the strategy refused (e.g. passthrough audience-guard mismatch)

subject source (first found wins):  --subject  ->  $KX_AUTH_TOKEN  ->  stdin  ->  login cache (--server)

strategies:
  rfc_8693         (default) token exchange at --token-url for --audience; needs a subject token
  passthrough      forward the subject unchanged iff its aud already includes --audience
  service_account  client_credentials at --token-url (the container's own identity; no subject)

examples:
  # RFC 8693 swap (subject as a flag), structured output
  kx auth exchange --subject "$TOK" --audience kdbai \\
    --token-url https://idp/token --client-id mcp-container --client-secret "$SECRET" --json

  # chain off a prior `kx auth login` — subject pulled from the cache for that server
  kx auth login --server https://mcp.example --json
  kx auth exchange --server https://mcp.example --audience backend-api --token-url https://idp/token --json
"""


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--subject", help="The subject (inbound) token. Falls back to $KX_AUTH_TOKEN, stdin, then the login cache.")
    parser.add_argument("--audience", help="Target backend audience (the token is scoped to this).")
    parser.add_argument("--strategy", default="rfc_8693", help="Outbound strategy (default rfc_8693).")
    parser.add_argument("--token-url", help="OIDC/STS token endpoint (rfc_8693 / service_account).")
    parser.add_argument("--resource", help="RFC 8707 resource URI — an alternative to --audience.")
    parser.add_argument("--client-id", help="Client id for authenticating at the token endpoint.")
    parser.add_argument(
        "--client-secret",
        default=os.environ.get("KX_AUTH_CLIENT_SECRET"),
        help="Client secret paired with --client-id. Falls back to $KX_AUTH_CLIENT_SECRET "
        "(preferred — keeps the secret out of shell history and process listings).",
    )
    parser.add_argument(
        "--client-auth", choices=("post", "basic"), default="post",
        help="How client credentials reach the token endpoint (default post).",
    )
    parser.add_argument(
        "--scope", action="append", metavar="SCOPE",
        help="Requested scope; repeatable, or comma/space-separated within one value.",
    )
    parser.add_argument("--server", help="MCP-server URL — used to pull the subject from the login cache.")
    parser.add_argument("--no-verify", action="store_true", help="Disable TLS verification (dev IdPs).")
    parser.add_argument("--timeout", type=float, default=30.0, help="Token-request timeout, seconds.")
    parser.add_argument("--json", action="store_true", help="Emit a structured JSON envelope (the agent path).")


def _resolve_subject(args: argparse.Namespace) -> tuple[str | None, bool]:
    """Resolve the subject token; the second element flags that the only candidate was an
    expired ``login``-cache entry (so the caller can map it to auth-required, not usage)."""
    if args.subject:
        return args.subject, False
    env = os.environ.get("KX_AUTH_TOKEN")
    if env:
        return env.strip(), False
    try:
        if not sys.stdin.isatty():
            piped = sys.stdin.read().strip()
            if piped:
                return piped, False
    except (OSError, ValueError):
        pass
    if args.server:
        entry = cache.get(args.server)
        if entry and entry.get("access_token"):
            expires_at = entry.get("expires_at")
            if expires_at and time.time() >= float(expires_at):
                return None, True
            return entry["access_token"], False
    return None, False


def _scopes(args: argparse.Namespace) -> list[str] | None:
    if not args.scope:
        return None
    out: list[str] = []
    for chunk in args.scope:
        out.extend(part for part in chunk.replace(",", " ").split() if part)
    return out or None


def _config_from_args(args: argparse.Namespace) -> OutboundConfig:
    return OutboundConfig(
        strategy=args.strategy,
        token_url=args.token_url,
        audience=args.audience,
        resource=args.resource,
        client_id=args.client_id,
        client_secret=args.client_secret,
        client_auth=args.client_auth,
        scopes=_scopes(args),
        verify=not args.no_verify,
        timeout=args.timeout,
    )


def _emit(args: argparse.Namespace, *, status: str, reason: str | None = None, credential=None) -> None:
    if args.json:
        if credential is not None:
            envelope = {
                "status": status,
                "access_token": credential.access_token,
                "token_type": credential.token_type,
                "expires_in": credential.expires_in,
                "strategy": credential.strategy,
                "claims": credential.claims,
            }
        else:
            envelope = {"status": status, "reason": reason}
        print(json.dumps(envelope, indent=2, default=str))
        return
    if credential is not None:
        print(f"ok — strategy={credential.strategy} token_type={credential.token_type} access_token={credential.access_token}")
    else:
        print(f"{status}: {reason}", file=sys.stderr)


def run(args: argparse.Namespace) -> int:
    subject, cache_expired = _resolve_subject(args)
    if args.strategy in _NEEDS_SUBJECT and not subject:
        if cache_expired:
            _emit(
                args,
                status="auth_required",
                reason=f"the cached login for {args.server} has expired — "
                f"run `kx auth login --server {args.server}` again",
            )
            return EXIT_AUTH_REQUIRED
        print(
            f"error: strategy '{args.strategy}' needs a subject token "
            "(pass --subject, $KX_AUTH_TOKEN, stdin, or --server for the login cache)",
            file=sys.stderr,
        )
        return EXIT_USAGE

    config = _config_from_args(args)
    try:
        credential = asyncio.run(exchange(config, subject))
    except PermissionError as exc:  # strategy refusal (e.g. passthrough audience guard)
        _emit(args, status="denied", reason=str(exc))
        return EXIT_DENIED
    except Exception as exc:  # ValueError (misconfig / no token) / httpx / unexpected
        _emit(args, status="error", reason=str(exc))
        return EXIT_ERROR

    _emit(args, status="ok", credential=credential)
    return EXIT_OK
