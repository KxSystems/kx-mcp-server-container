"""The ``kx`` umbrella entry point (stdlib argparse).

The ``auth`` group exposes ``introspect``, ``login``, ``exchange``, and ``assert``; the umbrella
leaves room for future top-level ``kx`` commands. Each leaf command sets ``func``; ``main`` dispatches
and propagates its int return as the process exit code. Argparse's own usage errors exit ``2`` — the
code the spec reserves for that.
"""

from __future__ import annotations

import argparse
import sys
from typing import Optional, Sequence

from . import assert_cmd, exchange, introspect, login


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="kx",
        description="kx control-plane CLI. Agent-first: every command supports --json and a stable "
        "exit-code contract (0 ok · 1 error · 2 usage · 3 auth-required · 4 denied).",
    )
    groups = parser.add_subparsers(dest="group", metavar="<group>")

    auth = groups.add_parser(
        "auth",
        help="Authentication flows (introspect / login / exchange / assert).",
        description="Authentication flows for the kx-mcp control plane: `introspect`, `login`, "
        "`exchange`, `assert`.",
    )
    auth_commands = auth.add_subparsers(dest="auth_command", metavar="<command>")

    introspect_parser = auth_commands.add_parser(
        "introspect",
        help="Validate a bearer token against the configured KX_MCP_AUTH keys.",
        description=introspect.HELP_DESCRIPTION,
        epilog=introspect.HELP_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    introspect.add_arguments(introspect_parser)
    introspect_parser.set_defaults(func=introspect.run)

    login_parser = auth_commands.add_parser(
        "login",
        help="Acquire a bearer for an MCP server via server-advertised device-code OAuth.",
        description=login.HELP_DESCRIPTION,
        epilog=login.HELP_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    login.add_arguments(login_parser)
    login_parser.set_defaults(func=login.run)

    exchange_parser = auth_commands.add_parser(
        "exchange",
        help="Swap a subject token for a backend-scoped credential (RFC 8693).",
        description=exchange.HELP_DESCRIPTION,
        epilog=exchange.HELP_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    exchange.add_arguments(exchange_parser)
    exchange_parser.set_defaults(func=exchange.run)

    assert_parser = auth_commands.add_parser(
        "assert",
        help="Exercise the kdb+ identity-assertion handshake (project a principal; optionally bind it).",
        description=assert_cmd.HELP_DESCRIPTION,
        epilog=assert_cmd.HELP_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    assert_cmd.add_arguments(assert_parser)
    assert_parser.set_defaults(func=assert_cmd.run)

    return parser


def main(argv: Optional[Sequence[str]] = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    func = getattr(args, "func", None)
    if func is None:
        # `kx` or `kx auth` with no leaf command — show help and exit as a usage error.
        parser.print_help(sys.stderr)
        sys.exit(2)
    sys.exit(func(args))
