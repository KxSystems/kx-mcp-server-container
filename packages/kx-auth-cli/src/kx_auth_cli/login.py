"""``kx auth login`` — acquire a bearer for an MCP server via server-advertised device-code OAuth.

The mediation model the MCP spec mandates (see ``discovery``): point ``kx auth`` at the **MCP server
(the container / resource server)** you connect to — *not* a backend. The container publishes RFC 9728
metadata naming its authorization server; ``kx auth`` discovers the AS and runs the **RFC 8628
device-code** flow directly against it (never redirecting through the container). The token is
audienced to the MCP-server resource; backend-scoped tokens come from a separate ``kx auth exchange``.

Client identity is **DCR-first**: when the AS advertises a ``registration_endpoint`` and no
``--client-id`` is given, register dynamically (RFC 7591) so the user never hand-configures a
client-id; ``--client-id`` / ``$KX_AUTH_CLIENT_ID`` overrides. The acquired credential is written to
the endpoint-keyed token cache for ``kx auth exchange`` to chain off.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

import httpx

from . import cache, discovery
from .discovery import DeviceAuthError, DiscoveryError

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_AUTH_REQUIRED = 3
EXIT_DENIED = 4

# DeviceAuthError.reason → exit code (anything unlisted is a plain error).
_REASON_EXIT = {
    "access_denied": EXIT_DENIED,
    "expired_token": EXIT_AUTH_REQUIRED,
    "timeout": EXIT_AUTH_REQUIRED,
}

HELP_DESCRIPTION = (
    "Acquire a bearer for an MCP server via the device-code flow against the authorization server "
    "the server advertises (RFC 9728 discovery → RFC 8628). Caches the token for `exchange`."
)
HELP_EPILOG = """\
exit codes (branch on the code, not the text):
  0  ok             a token was acquired and cached
  1  error          discovery failed, unreachable endpoint, no device-code support, or DCR failure
  2  usage          bad flags
  3  auth-required  the device code expired / timed out before approval — retry
  4  denied         the user denied the authorization request

--server is the MCP-server (resource) URL you connect to — NOT a backend. The client talks directly
to the discovered authorization server; the token is audienced to that resource. Use `kx auth
exchange` for backend-scoped tokens.

client identity (DCR-first):  RFC 7591 dynamic registration if the AS supports it, else --client-id
                              (or $KX_AUTH_CLIENT_ID).

examples:
  kx auth login --server https://mcp.example --json
  kx auth login --server https://mcp.example --client-id my-public-client --scope "kdbx.read offline_access"
"""


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--server", required=True, help="MCP-server (resource) URL to authenticate to.")
    parser.add_argument("--client-id", help="OAuth client id (overrides DCR; falls back to $KX_AUTH_CLIENT_ID).")
    parser.add_argument(
        "--scope", action="append", metavar="SCOPE",
        help="Requested scope; repeatable, or comma/space-separated within one value.",
    )
    parser.add_argument("--no-verify", action="store_true", help="Disable TLS verification (dev IdPs).")
    parser.add_argument("--timeout", type=float, default=30.0, help="Per-request timeout, seconds.")
    parser.add_argument("--json", action="store_true", help="Emit a structured JSON envelope (the agent path).")


def _scopes(args: argparse.Namespace) -> list[str] | None:
    if not args.scope:
        return None
    out: list[str] = []
    for chunk in args.scope:
        out.extend(part for part in chunk.replace(",", " ").split() if part)
    return out or None


def _prompt(device: dict) -> None:
    """Show the user where to approve — to stderr, so a --json stdout stays clean."""
    complete = device.get("verification_uri_complete")
    uri = device.get("verification_uri", "<unknown>")
    code = device.get("user_code", "<unknown>")
    if complete:
        print(f"To authorize, open: {complete}", file=sys.stderr)
    print(f"To authorize, visit {uri} and enter code: {code}", file=sys.stderr)


def _emit_ok(args: argparse.Namespace, meta, token: dict, cache_path) -> None:
    if args.json:
        print(json.dumps(
            {
                "status": "ok",
                "server": args.server,
                "authorization_server": meta.authorization_server,
                "access_token": token["access_token"],
                "token_type": token.get("token_type", "Bearer"),
                "expires_in": token.get("expires_in"),
                "cached": True,
                "cache_path": str(cache_path),
            },
            indent=2,
            default=str,
        ))
        return
    print(f"ok — logged in to {args.server} (cached at {cache_path})")


def _emit_fail(args: argparse.Namespace, status: str, reason: str) -> None:
    if args.json:
        print(json.dumps({"status": status, "reason": reason}, indent=2, default=str))
    else:
        print(f"{status}: {reason}", file=sys.stderr)


def run(args: argparse.Namespace) -> int:
    scopes = _scopes(args)
    client_id = args.client_id or os.environ.get("KX_AUTH_CLIENT_ID")
    try:
        with httpx.Client(verify=not args.no_verify, timeout=args.timeout) as client:
            meta = discovery.discover(args.server, client=client)
            if not client_id:
                client_id = discovery.register_client(
                    meta, client=client, client_name="kx-auth-cli", scopes=scopes
                )
            device = discovery.start_device_authorization(meta, client_id, client=client, scopes=scopes)
            _prompt(device)
            token = discovery.poll_for_token(
                meta,
                client_id,
                device["device_code"],
                client=client,
                interval=int(device.get("interval", 5)),
                expires_in=int(device.get("expires_in", 600)),
            )
    except DiscoveryError as exc:
        _emit_fail(args, "error", str(exc))
        return EXIT_ERROR
    except DeviceAuthError as exc:
        status = "denied" if exc.reason == "access_denied" else (
            "auth_required" if _REASON_EXIT.get(exc.reason) == EXIT_AUTH_REQUIRED else "error"
        )
        _emit_fail(args, status, str(exc))
        return _REASON_EXIT.get(exc.reason, EXIT_ERROR)
    except httpx.HTTPError as exc:
        _emit_fail(args, "error", f"network error talking to the authorization server: {exc}")
        return EXIT_ERROR

    expires_in = token.get("expires_in")
    now = int(time.time())
    credential = {
        "access_token": token["access_token"],
        "refresh_token": token.get("refresh_token"),
        "token_type": token.get("token_type", "Bearer"),
        "scope": token.get("scope"),
        "expires_at": now + int(expires_in) if expires_in else None,
        "issued_at": now,
        "authorization_server": meta.authorization_server,
    }
    cache_path = cache.save(args.server, credential)
    _emit_ok(args, meta, token, cache_path)
    return EXIT_OK
