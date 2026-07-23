"""``kx auth introspect`` — decode/validate a bearer against the configured ``KX_MCP_AUTH`` keys.

Reuses the shared :func:`kx_auth_core.verify_token` (the same validation the container enforces) and
maps its categorised verdict to the stable exit-code contract:

* ``0`` valid · ``1`` error (malformed / bad signature / unreachable JWKS) · ``2`` usage (no token) ·
  ``3`` auth-required (expired — the agent should ``login``) · ``4`` denied (issuer / audience /
  required-scope mismatch).

Config defaults come from the ``KX_MCP_AUTH*`` environment (so the CLI checks against the same keys
the container uses); the flags below override per-invocation. ``introspect`` is stateless — it does
not touch the token cache. (Server-advertised RFC 9728 discovery is handled by ``login``.)
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from kx_auth_core import (
    AUTH_REQUIRED,
    DENIED,
    ERROR,
    OK,
    AuthSettings,
    VerifyResult,
    verify_token,
)

EXIT_CODES = {OK: 0, ERROR: 1, AUTH_REQUIRED: 3, DENIED: 4}

# CLI help kept separate from the module docstring above: the docstring is RST for code/IDE readers;
# this is clean plaintext for the terminal (and for an agent reading `--help`). RawDescriptionHelpFormatter
# prints the epilog verbatim, so the alignment below is preserved.
HELP_DESCRIPTION = (
    "Validate a bearer token against the same keys the container enforces and report a "
    "machine-readable verdict. Stateless — does not touch any token cache."
)
HELP_EPILOG = """\
exit codes (branch on the code, not the text):
  0  ok             valid
  1  error          malformed / bad signature / unreachable JWKS
  2  usage          bad flags, or no token supplied
  3  auth-required  well-signed but expired — re-authenticate (kx auth login)
  4  denied         well-signed but wrong issuer / audience / required scope

token source (first found wins):  argument  ->  $KX_AUTH_TOKEN  ->  stdin

config comes from the environment (the same vars the container reads); flags override per call:
  --public-key-path   KX_MCP_AUTH_PUBLIC_KEY_PATH   static mode (PEM on disk)
  --public-key        KX_MCP_AUTH_PUBLIC_KEY        static mode (inline PEM)
  --jwks-uri          KX_MCP_AUTH_JWKS_URI          jwks mode (remote endpoint)
  --issuer            KX_MCP_AUTH_ISSUER
  --audience          KX_MCP_AUTH_AUDIENCE
  --algorithm         KX_MCP_AUTH_ALGORITHM         default RS256
  --required-scopes   KX_MCP_AUTH_REQUIRED_SCOPES   comma/space separated

examples:
  # token + key as flags, structured verdict
  kx auth introspect "$TOKEN" --public-key-path key.pub --issuer https://idp --audience my-api --json

  # config + token entirely from the environment (matches the container)
  KX_MCP_AUTH=static KX_MCP_AUTH_PUBLIC_KEY_PATH=key.pub \\
    KX_AUTH_TOKEN="$TOKEN" kx auth introspect --json

  # validate against a live JWKS endpoint, token piped on stdin
  echo "$TOKEN" | kx auth introspect --jwks-uri https://idp/.well-known/jwks.json
"""


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "token",
        nargs="?",
        help="The bearer token to validate. Falls back to $KX_AUTH_TOKEN, then stdin.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit a structured JSON envelope (status/valid/claims/reason) — the agent path.",
    )
    # Verification config — overrides the KX_MCP_AUTH* environment for this invocation.
    parser.add_argument("--jwks-uri", help="Remote JWKS endpoint (selects jwks mode).")
    parser.add_argument("--public-key", help="Inline RS256 public-key PEM (selects static mode).")
    parser.add_argument(
        "--public-key-path", help="Path to an RS256 public-key PEM (selects static mode)."
    )
    parser.add_argument("--issuer", help="Expected `iss` claim.")
    parser.add_argument("--audience", help="Expected `aud` claim.")
    parser.add_argument("--algorithm", help="Signing algorithm to accept (default RS256).")
    parser.add_argument(
        "--required-scopes", help="Comma/space-separated scopes that must be present."
    )


def _resolve_token(args: argparse.Namespace) -> str | None:
    if args.token:
        return args.token
    env = os.environ.get("KX_AUTH_TOKEN")
    if env:
        return env.strip()
    try:
        if not sys.stdin.isatty():
            piped = sys.stdin.read().strip()
            if piped:
                return piped
    except (OSError, ValueError):
        # stdin unavailable (e.g. captured under a test harness) — treat as "no token".
        pass
    return None


def _settings_from_args(args: argparse.Namespace) -> AuthSettings:
    """Start from the KX_MCP_AUTH* environment, then apply explicit flag overrides. Key material on
    the CLI selects the mode, so `introspect <token> --public-key-path k.pub` works with no env."""
    overrides: dict = {}
    for flag in ("jwks_uri", "public_key", "public_key_path", "issuer", "audience"):
        value = getattr(args, flag)
        if value is not None:
            overrides[flag] = value
    if args.algorithm:
        overrides["algorithm"] = args.algorithm
    if args.required_scopes:
        overrides["required_scopes"] = args.required_scopes
    if args.public_key or args.public_key_path:
        overrides["mode"] = "static"
    elif args.jwks_uri:
        overrides["mode"] = "jwks"
    return AuthSettings(**overrides)


def _render(result: VerifyResult, *, json_output: bool) -> None:
    if json_output:
        envelope: dict = {"status": result.category, "valid": result.valid}
        if result.valid:
            envelope["client_id"] = result.client_id
            envelope["scopes"] = result.scopes
            envelope["claims"] = result.claims
        else:
            envelope["reason"] = result.reason
        print(json.dumps(envelope, indent=2, default=str))
        return
    if result.valid:
        print(f"valid — client_id={result.client_id} scopes={result.scopes or []}")
    else:
        print(f"{result.category}: {result.reason}", file=sys.stderr)


def run(args: argparse.Namespace) -> int:
    token = _resolve_token(args)
    if not token:
        print(
            "error: no token provided (pass as an argument, $KX_AUTH_TOKEN, or via stdin)",
            file=sys.stderr,
        )
        return 2  # usage error — consistent with argparse's own exit 2

    try:
        settings = _settings_from_args(args)
        result = verify_token(token, settings)
    except Exception as exc:  # config / unexpected failure → error
        result = VerifyResult.fail(ERROR, str(exc))

    _render(result, json_output=args.json)
    return EXIT_CODES[result.category]
