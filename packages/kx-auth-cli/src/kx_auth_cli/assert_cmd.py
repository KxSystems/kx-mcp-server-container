"""``kx auth assert`` — exercise the kdb+ identity-assertion handshake from a shell.

plain kdb+ has no bearer over qIPC, so the container does not mint a token for it — it **asserts**
the caller's identity: projects the validated claims into a q dict and binds them to a trusted
service-account connection via ``.kx.auth.bind[principal]``. This command runs that handshake
end-to-end *without* a full container, so an operator or CI step can verify a kdb+ target's
``kx.auth`` module + permission functions behave as expected for a given principal.

Two modes:

- **project-only** (no ``--connect``): build and print the principal wire-dict the container *would*
  bind. Uses the shared, fastmcp-free projection — always available, no PyKX needed.
- **handshake** (``--connect host:port``): connect as the service account, call ``.kx.auth.bind``,
  then read back ``.kx.auth.current[]`` / ``.kx.auth.valid[]`` (and optionally run a ``--probe``
  query) to confirm the assertion took. The qIPC leg needs PyKX, pulled in via the optional
  ``kx-auth-cli[qipc]`` extra so the base CLI stays light + fastmcp-free.

Claims source (first found wins): ``--principal`` JSON (``-`` for stdin, ``@file`` for a file) →
``--token`` (a JWT, decoded **unverified** — this is a local handshake exerciser, not a validator) →
``$KX_AUTH_TOKEN`` (as a JWT). Exit codes: ``0`` ok · ``1`` error · ``2`` usage · ``4`` denied
(a ``--probe`` query refused by the q-side permission check).
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from kx_auth_core import decode_claims_unverified, project_from_claims

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_DENIED = 4

HELP_DESCRIPTION = (
    "Exercise the kdb+ identity-assertion handshake: project a principal into the q wire-dict and "
    "(with --connect) bind it on a service-account connection to verify a target's kx.auth module."
)
HELP_EPILOG = """\
exit codes (branch on the code, not the text):
  0  ok        projected (project-only), or connected + bound + asserted
  1  error     bad claims, target missing .kx.auth, connect failure, or PyKX not installed
  2  usage     no principal/claims supplied
  4  denied    a --probe query was refused by the q-side permission check

claims source (first found wins):  --principal JSON  ->  --token JWT  ->  $KX_AUTH_TOKEN (JWT)
  --principal '-'        read JSON claims from stdin
  --principal @file.json read JSON claims from a file

examples:
  # project-only: see exactly what would be bound (no kdb+, no PyKX)
  kx auth assert --principal '{"sub":"alice","scope":"kdbx.read","aud":"kx-mcp"}' --json

  # full handshake against a kdb+ carrying the kx.auth module (needs kx-auth-cli[qipc])
  kx auth assert --token "$TOK" --connect localhost:5010 \\
    --user kxmcp --password "$SVC_PW" --probe "select from trades" --json
"""


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--principal", help="Principal claims as JSON ('-' for stdin, '@file' for a file).")
    parser.add_argument("--token", help="A JWT to decode (UNVERIFIED) into claims — convenience for a local handshake.")
    parser.add_argument("--connect", metavar="HOST:PORT", help="kdb+ target to run the bind handshake against (needs the [qipc] extra).")
    parser.add_argument("--user", default=os.environ.get("KX_AUTH_KDB_USER", ""), help="Service-account user for the qIPC login (or $KX_AUTH_KDB_USER).")
    parser.add_argument(
        "--password",
        default=os.environ.get("KX_AUTH_KDB_PASSWORD"),
        help="Service-account password (or $KX_AUTH_KDB_PASSWORD — preferred, keeps it out of shell history).",
    )
    parser.add_argument("--tls", action="store_true", help="Use TLS for the qIPC connection.")
    parser.add_argument("--probe", help="Optional q expression to run after binding (a denial maps to exit 4).")
    parser.add_argument("--timeout", type=float, default=5.0, help="qIPC connection timeout, seconds.")
    parser.add_argument("--json", action="store_true", help="Emit a structured JSON envelope (the agent path).")


def _resolve_claims(args: argparse.Namespace) -> tuple[dict | None, str | None]:
    """Return (claims, error). Claims source: --principal JSON, then --token, then $KX_AUTH_TOKEN."""
    if args.principal:
        raw = args.principal
        if raw == "-":
            raw = sys.stdin.read()
        elif raw.startswith("@"):
            try:
                with open(raw[1:], "r") as fh:
                    raw = fh.read()
            except OSError as exc:
                return None, f"cannot read principal file: {exc}"
        try:
            claims = json.loads(raw)
        except json.JSONDecodeError as exc:
            return None, f"--principal is not valid JSON: {exc}"
        if not isinstance(claims, dict):
            return None, "--principal JSON must be an object of claims"
        return claims, None

    token = args.token or os.environ.get("KX_AUTH_TOKEN")
    if token:
        claims = decode_claims_unverified(token.strip())
        if not claims:
            return None, "--token did not decode to any claims (opaque or malformed JWT)"
        return claims, None

    return None, None


def _emit(args: argparse.Namespace, *, status: str, principal=None, reason=None, **extra) -> None:
    if args.json:
        envelope = {"status": status}
        if principal is not None:
            envelope["principal"] = principal
        if reason is not None:
            envelope["reason"] = reason
        envelope.update(extra)
        print(json.dumps(envelope, indent=2, default=str))
        return
    if status == "ok":
        print(f"ok — principal sub={principal.get('sub')!r}", end="")
        if extra:
            print(" " + " ".join(f"{k}={v}" for k, v in extra.items()))
        else:
            print()
    else:
        print(f"{status}: {reason}", file=sys.stderr)


def _handshake(args: argparse.Namespace, wire: dict) -> int:
    """Connect as the service account, bind the principal, confirm, optionally probe."""
    try:
        import pykx as kx
    except Exception:
        _emit(
            args,
            status="error",
            principal=wire,
            reason="PyKX is required for --connect; install the qIPC extra: pip install 'kx-auth-cli[qipc]'",
        )
        return EXIT_ERROR

    host, _, port = args.connect.partition(":")
    if not port.isdigit():
        _emit(args, status="error", reason="--connect must be HOST:PORT")
        return EXIT_USAGE

    try:
        conn = kx.SyncQConnection(
            host=host or "localhost",
            port=int(port),
            username=args.user,
            password=args.password or "",
            timeout=args.timeout,
            tls=args.tls,
        )
    except Exception as exc:
        _emit(args, status="error", reason=f"connect failed: {exc}")
        return EXIT_ERROR

    try:
        conn(".kx.auth.bind", wire)
    except Exception as exc:
        _emit(
            args,
            status="error",
            principal=wire,
            reason=f"bind failed (target missing the kx.auth module?): {exc}",
        )
        return EXIT_ERROR

    try:
        bound_valid = bool(conn(".kx.auth.valid[]").py())
    except Exception as exc:
        _emit(args, status="error", principal=wire, reason=f"could not read .kx.auth.valid[]: {exc}")
        return EXIT_ERROR

    if args.probe:
        try:
            conn(args.probe)
        except Exception as exc:
            if str(exc).strip().lower().startswith("denied"):
                _emit(args, status="denied", principal=wire, reason=str(exc), bound=True, valid=bound_valid)
                return EXIT_DENIED
            _emit(args, status="error", principal=wire, reason=f"probe failed: {exc}")
            return EXIT_ERROR

    _emit(args, status="ok", principal=wire, bound=True, valid=bound_valid, probed=bool(args.probe))
    return EXIT_OK


def run(args: argparse.Namespace) -> int:
    claims, err = _resolve_claims(args)
    if err is not None:
        _emit(args, status="error", reason=err)
        return EXIT_ERROR
    if claims is None:
        print(
            "error: no principal supplied (pass --principal JSON, --token JWT, or $KX_AUTH_TOKEN)",
            file=sys.stderr,
        )
        return EXIT_USAGE

    wire = project_from_claims(claims)

    if not args.connect:
        _emit(args, status="ok", principal=wire)
        return EXIT_OK

    return _handshake(args, wire)
