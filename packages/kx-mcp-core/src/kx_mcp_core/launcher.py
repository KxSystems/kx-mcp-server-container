"""Thin, optional zero-code launcher: assemble a composition server from config and run it.

    kx-mcp --bundles kdbx,example
    KX_MCP_BUNDLES=kdbx,example kx-mcp

This is the convenience path for a simple CLI invocation or pointing an MCP client at a URL with no
extra glue code. It is built from the same `kx_mcp_core.assembly` seam as the hand-written glue in
`server.py` — nothing the launcher does is unavailable to a few lines of glue. Inbound auth is
resolved from `KX_MCP_AUTH`; it defaults to off (the single-principal bundling posture).

Both assembly mount postures are reachable from the CLI: the default forgiving `try_mount_bundle`,
and the strict `mount_bundle` behind `--exit-on-mount-failure` / `KX_MCP_EXIT_ON_MOUNT_FAILURE` for
one-backend-per-container deployments that want a failed pre-flight to terminate the process.
"""

from __future__ import annotations

import argparse
import logging
import math
import os
import sys
import threading
import time
from typing import Callable, Optional, Sequence

from fastmcp import FastMCP

from .assembly import (
    BUNDLE_PACKAGE_PREFIX,
    load_build_server,
    make_parent,
    mount_bundle,
    try_mount_bundle,
)
from .auth import (
    AuthSettings,
    authz_adapters,
    build_auth_provider,
    configure_authz,
    require_authz_adapter,
)
from .logging import configure_logging
from .mount_budget import mount_budget
from .observability import ObservabilitySettings, mount_metrics_route

# Named, not __name__: under `python -m kx_mcp_core.launcher` __name__ is "__main__", which the
# logging brand filter drops, so every launcher warning and error vanished from that entry point.
logger = logging.getLogger("kx_mcp_core.launcher")

_TRUTHY = {"1", "true", "yes", "on"}
_FALSY = {"", "0", "false", "no", "off"}

# No "sse": the HTTP+SSE transport is deprecated in the MCP spec (superseded by streamable-http), so
# the container does not offer it.
TRANSPORTS = ("stdio", "streamable-http", "http")


def _parse_bundles(raw: str | None) -> list[str]:
    """Split the comma-separated bundle list, dropping empty entries.

    A stray or trailing comma (`--bundles kdbx,`) used to yield `["kdbx", ""]`, and the empty name
    became `load_build_server("kx_mcp_")` -> ModuleNotFoundError, which crashed the whole launcher
    and took the healthy `kdbx` down with it. `","` alone was worse: `["", ""]` is truthy, so it
    sailed past the "no bundles selected" guard entirely.
    """
    if not raw:
        return []
    return [name for name in (part.strip() for part in raw.split(",")) if name]


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in _TRUTHY


def _resolve(package: str, namespace: str) -> Optional[Callable[..., FastMCP]]:
    """Resolve `package`'s build_server, treating a RESOLUTION failure as a mount failure (`None`).

    `load_build_server` sat outside every failure handler in build_app's loop, so an import-time
    failure — a mistyped bundle name, an empty name from a stray comma, or a bundle whose own
    module-level config construction raises (the `app_settings = AppSettings()` pattern kdb-x uses)
    — crashed the whole container even in the default forgiving posture, taking down healthy bundles
    requested alongside it. The "never crash on a partial failure" invariant covered
    `build_server()` *invocation*, never its *import*.

    Deliberately here and not in `assembly.py`: catching-and-warning is a POSTURE choice, and the
    assembly seam stays policy-free (the same reason `mount_bundle` and `try_mount_bundle` are two
    functions rather than one with a flag).
    """
    try:
        build_server = load_build_server(package)
    except SystemExit as exc:  # a bundle that exits at import time
        logger.warning(
            "backend '%s' unavailable — importing %s exited (%s); serving without it",
            namespace,
            package,
            exc.code,
        )
        return None
    except Exception as exc:
        logger.warning(
            "backend '%s' could not be resolved from '%s' (%s: %s); serving without it",
            namespace,
            package,
            type(exc).__name__,
            exc,
        )
        return None
    return build_server


def _load_and_mount(parent: FastMCP, package: str, namespace: str) -> bool:
    """Resolve `package`'s build_server and mount it. True if it mounted."""
    build_server = _resolve(package, namespace)
    if build_server is None:
        return False
    return try_mount_bundle(parent, build_server, namespace=namespace)


def _tcp_port(raw: str) -> int:
    """An argparse type for a bindable TCP port, so an out-of-range value is a USAGE error.

    `type=int` accepted anything parseable — `0`, `-1`, `999999` — and the failure only surfaced far
    later, from inside uvicorn/starlette's socket bind as an `OverflowError` with no mention of
    `--port`. Raising ArgumentTypeError makes argparse print the flag, the bad value and the valid
    range, and exit 2 like every other usage error.
    """
    try:
        port = int(raw)
    except ValueError:
        raise argparse.ArgumentTypeError(f"port must be an integer, got {raw!r}")
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError(f"port must be between 1 and 65535, got {port}")
    return port


def _transport(raw: str) -> str:
    """An argparse type that enforces the transport ``choices``, for the env default as well.

    argparse checks ``choices`` only for a value typed on the command line, never for a ``default``,
    so ``KX_MCP_TRANSPORT=sse`` served the deprecated SSE transport that ``--transport sse`` refuses,
    and ``KX_MCP_TRANSPORT=bogus`` was a traceback from ``run()`` after every bundle had mounted. A
    string default does go through ``type=``, so validating here makes both a usage error (exit 2).
    """
    if raw not in TRANSPORTS:
        raise argparse.ArgumentTypeError(
            f"invalid choice: {raw!r} (choose from {', '.join(TRANSPORTS)})"
        )
    return raw


def _log_level(raw: str) -> str:
    """An argparse type for ``--log-level`` (and so ``KX_MCP_LOG_LEVEL``): a stdlib level name.

    An unknown name used to fall back to INFO silently, so ``KX_MCP_LOG_LEVEL=verbose`` looked like
    it worked while hiding the DEBUG output the operator asked for.
    """
    if not isinstance(logging.getLevelName(raw.strip().upper()), int):
        raise argparse.ArgumentTypeError(
            f"unknown log level {raw!r} (use DEBUG, INFO, WARNING, ERROR or CRITICAL)"
        )
    return raw


def _mount_timeout(raw: str) -> float:
    """An argparse type for ``--mount-timeout``: finite seconds, ``>= 0`` (``0`` is off).

    Its env twin, ``KX_MCP_MOUNT_TIMEOUT``, now goes through here as the flag's default, so a bad
    value is the same usage error either way rather than a warning and a silently disabled bound.
    (:func:`_env_float` still reads it leniently for glue that calls :func:`build_app` directly.)
    """
    try:
        value = float(raw)
    except ValueError:
        raise argparse.ArgumentTypeError(f"must be a number of seconds, got {raw!r}")
    if not math.isfinite(value) or value < 0:
        raise argparse.ArgumentTypeError(f"must be a finite number of seconds >= 0, got {raw!r}")
    return value


def _env_float(name: str) -> float:
    """A non-negative float from the environment; 0.0 (disabled) if unset or unparseable."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return 0.0
    try:
        value = float(raw)
    except ValueError:
        logger.warning("%s=%r is not a number; ignoring it", name, raw)
        return 0.0
    if value < 0:
        logger.warning("%s=%r is negative; ignoring it", name, raw)
        return 0.0
    return value


def _mount_forgiving(parent: FastMCP, package: str, namespace: str, timeout: float) -> bool:
    """Mount one bundle in the forgiving posture, optionally under a startup timeout.

    With ``timeout <= 0`` (the default) this is a plain, in-thread :func:`_load_and_mount`.

    **Why the timeout is opt-in.** Nothing in the chain bounded bundle startup, so a
    ``build_server()`` that hangs — a backend accepting the TCP connection but never answering, say —
    wedged the container's startup *forever*, and every bundle requested after it never mounted. But
    a synchronous call cannot be cancelled in Python: the only way to stop waiting is to run it on a
    worker thread and abandon that thread, which then keeps whatever it allocated (for kdb-x, an
    embedded-PyKX/qIPC handle) for the life of the process. Making that the default would put every
    backend's pre-flight on a worker thread to fix a case most deployments never hit, so instead:

    * the container's obligation is the matching **extension-contract amendment** — a bundle's
      ``build_server()`` must bound its own pre-flight (kdb-x has ``KDBX_DB_TIMEOUT`` for exactly
      this), which is the only place a hang can be cancelled properly rather than abandoned;
    * ``KX_MCP_MOUNT_TIMEOUT`` is the backstop for operators who cannot rely on that — an
      orchestrated deployment that would rather serve the healthy backends than wait on a wedged
      one. It logs the abandonment explicitly, because a leaked thread is a real cost, not a detail.

    **The bound is only as good as the bundle's blocking calls.** ``join(timeout)`` needs the GIL to
    return, so a call that holds it (an embedded-q ``hopen`` does) keeps the launcher waiting past
    the timeout until that call ends. The worker therefore runs under a :func:`mount_budget` of the
    same length, and a bundle caps its own blocking calls by ``remaining_mount_budget()`` (kdb-x
    caps its connect this way). Both log lines report the real elapsed time next to the configured
    bound, so an overrun is visible rather than reported as "within the timeout". A worker that
    *finished* but overran is still mounted: we did wait for it, nothing was declared unavailable,
    and dropping a healthy backend after the fact would help no one.

    The worker resolves and *builds* only; the ``mount`` happens back on the calling thread, so a
    bundle the operator was told is unavailable can never mount LATE once its hang clears. See the
    comment on the split below.
    """
    if timeout <= 0:
        return _load_and_mount(parent, package, namespace)

    # Only RESOLVE + BUILD on the worker; the mount itself happens back on this thread. That split
    # is what makes the abandonment honest: a bundle we have already given up on can never mount
    # LATE onto a parent that is by then serving. (It could: the abandoned thread ran the mount too,
    # so once its hang cleared it quietly added the backend the operator was just told was
    # unavailable — and raced `mount_bundle`'s namespace bookkeeping and the mounted-count guard
    # while doing it.) Keeping `parent` off the worker leaves exactly the one documented cost: the
    # thread runs on and holds whatever it allocated.
    result: dict = {}

    def _run():
        # The budget covers the import too: it is one clock with the join below, so a slow import
        # leaves the bundle's connect less time rather than stretching the whole bound.
        with mount_budget(timeout):
            try:
                build_server = _resolve(package, namespace)
                if build_server is None:  # already warned, with the resolution-specific message
                    result["unresolved"] = True
                    return
                result["server"] = build_server()
            except BaseException as exc:  # noqa: BLE001 — the strict path is the caller's, not ours
                result["error"] = exc

    # daemon=True so an abandoned thread can never keep the process alive at shutdown.
    worker = threading.Thread(target=_run, name=f"start-{namespace}", daemon=True)
    started = time.monotonic()
    worker.start()
    worker.join(timeout)
    elapsed = time.monotonic() - started

    if worker.is_alive():
        logger.error(
            "backend '%s' did not finish starting; gave up after %.1fs (KX_MCP_MOUNT_TIMEOUT=%ss) "
            "and serving without it. NOTE: its startup thread is abandoned, not cancelled — a "
            "synchronous build_server() cannot be interrupted, so it keeps any connection it opened "
            "until the process exits. The bundle should bound its own pre-flight instead.",
            namespace,
            elapsed,
            timeout,
        )
        return False
    if elapsed > timeout:
        logger.warning(
            "backend '%s' finished starting after %.1fs, over KX_MCP_MOUNT_TIMEOUT=%ss: a blocking "
            "call it made held the interpreter past the bound, so the timeout could not fire. "
            "Mounting it anyway, since it did finish. The bundle should cap that call by the mount "
            "budget (kx_mcp_core.remaining_mount_budget).",
            namespace,
            elapsed,
            timeout,
        )
    if result.get("unresolved"):
        return False

    error = result.get("error")
    if error is not None and not isinstance(error, (SystemExit, Exception)):
        # KeyboardInterrupt and friends: nothing below would catch it, and re-raising a worker's
        # BaseException on this thread would take the container down through the forgiving path.
        logger.warning(
            "backend '%s' failed to start (%s: %s); serving without it",
            namespace,
            type(error).__name__,
            error,
        )
        return False

    def _replay() -> FastMCP:
        """Hand the worker's outcome to try_mount_bundle so its existing SystemExit / Exception
        handling and mount_bundle's contract check apply unchanged, on this thread."""
        if error is not None:
            raise error
        return result["server"]

    return try_mount_bundle(parent, _replay, namespace=namespace)


def build_app(
    bundles: Sequence[str],
    name: str = "kx-mcp",
    auth_settings: Optional[AuthSettings] = None,
    exit_on_mount_failure: bool = False,
    observability: Optional[ObservabilitySettings] = None,
    mount_timeout: Optional[float] = None,
) -> FastMCP:
    """Assemble a parent server with each named bundle mounted under its own namespace.

    Inbound auth is resolved from ``auth_settings`` (read from ``KX_MCP_AUTH*`` env by default) and
    passed to the parent, so it guards every mounted backend.

    Mount posture, matching the hand-written glue in ``server.py``:

    * default — mount via :func:`try_mount_bundle`, so an unreachable backend is disabled with a
      warning while the backends that came up keep serving ("never crash the container").
    * ``exit_on_mount_failure=True`` — mount via the strict :func:`mount_bundle`, letting a failed
      pre-flight's ``SystemExit`` propagate. An operator running one backend per container wants the
      process to die so the orchestrator restarts it and surfaces the misconfiguration.

    Regardless of that flag, requesting bundles and mounting **none** of them exits non-zero: a
    parent with no backends cannot serve a single tool, so staying alive only hides the cause.

    ``observability`` is the resolved ``KX_MCP_METRICS`` / ``KX_MCP_TRACING`` config (read from the
    environment when not passed); it attaches the metrics/tracing middleware on the parent, so every
    mounted backend is observed. The scrape *route* is mounted separately by ``main()``, which knows
    the transport.

    NOTE: importing a bundle may run its own settings/CLI parsing at import time, so callers that
    have their own CLI flags should clear ``sys.argv`` before calling this (the launcher does).
    """
    # Resolve the capability-check config here too, not only in main(): a bad KX_MCP_AUTHZ must fail
    # at startup for hand-written glue (server.py) and tests as well, which enter through build_app
    # and so used to get the decorator's lazy per-request read instead. A static policy file is
    # checked now; whether the mode has an adapter is checked after the loop below, because a
    # backend's adapter (kdbx_rbac) registers when its bundle is imported. Checking first refused
    # KX_MCP_AUTHZ=kdbx_rbac under `kx-mcp --bundles kdbx`, the documented launch.
    authz = configure_authz(require_adapter=False)

    observability = observability if observability is not None else ObservabilitySettings()
    parent = make_parent(
        name,
        auth=build_auth_provider(auth_settings or AuthSettings()),
        observability=observability,
    )
    mounted = 0
    seen_namespaces: set = set()
    mounted_names: set = set()
    # Which requested bundle registered each authz adapter, seen as the names that appear while
    # that bundle is resolved (kx_mcp_kdbx registers kdbx_rbac on import). The launcher does the
    # importing, so this attributes an adapter to its bundle without reaching into the registry.
    adapter_providers: dict = {}
    timeout = mount_timeout if mount_timeout is not None else _env_float("KX_MCP_MOUNT_TIMEOUT")

    for short_name in bundles:
        if short_name in seen_namespaces:
            # Two bundles under one namespace does not raise in fastmcp — list_tools() just returns
            # DUPLICATED tool names, which is an MCP protocol violation the client sees rather than
            # an error the operator sees.
            logger.warning(
                "backend '%s' requested more than once; ignoring the duplicate "
                "(mounting twice under one namespace duplicates every tool name)",
                short_name,
            )
            continue
        seen_namespaces.add(short_name)

        package = f"{BUNDLE_PACKAGE_PREFIX}{short_name}"
        registered_before = set(authz_adapters())
        if exit_on_mount_failure:
            # Strict: the bundle's own pre-flight ERROR log states the cause, and its sys.exit(1)
            # propagates out of here with its exit code intact.
            mount_bundle(parent, load_build_server(package), namespace=short_name)
            ok = True
        else:
            ok = _mount_forgiving(parent, package, short_name, timeout)
        for adapter_name in set(authz_adapters()) - registered_before:
            adapter_providers[adapter_name] = short_name
        if ok:
            mounted += 1
            mounted_names.add(short_name)

    if bundles and not mounted:
        logger.error(
            "no backends mounted of %d requested (%s) — nothing to serve; exiting",
            len(bundles),
            ", ".join(bundles),
        )
        raise SystemExit(1)
    try:
        # Still fail closed: a mode whose provider bundle did not import never serves.
        require_authz_adapter(authz)
    except ValueError as exc:
        logger.error("%s Refusing to serve.", exc)
        raise SystemExit(1)
    provider = adapter_providers.get(authz.mode)
    if provider is not None and provider not in mounted_names:
        # Its import registered the adapter, but its pre-flight failed: the adapter would decide
        # against a backend that is not there, so every gated call would be denied at runtime.
        # Fail at startup instead, where the cause is visible.
        logger.error(
            "KX_MCP_AUTHZ=%r is provided by backend '%s', which did not mount; refusing to serve "
            "rather than deny every authorized call. Fix that backend, or choose another mode.",
            authz.mode,
            provider,
        )
        raise SystemExit(1)
    return parent


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="kx-mcp",
        description="Assemble and run a kx-mcp composition server from a set of backend bundles.",
    )
    parser.add_argument(
        "--bundles",
        help="Comma-separated bundle names to mount, e.g. 'kdbx,example'. "
        "Defaults to $KX_MCP_BUNDLES.",
    )
    parser.add_argument("--name", default=os.environ.get("KX_MCP_NAME", "kx-mcp"))
    parser.add_argument(
        "--transport",
        default=os.environ.get("KX_MCP_TRANSPORT", "streamable-http"),
        # `type` as well as `choices`: only `type` also validates the env-sourced default.
        type=_transport,
        choices=TRANSPORTS,
    )
    parser.add_argument("--host", default=os.environ.get("KX_MCP_HOST", "127.0.0.1"))
    # The default is resolved through the same validator, and lazily via a string default, because
    # the old `int(os.environ.get(...))` ran at parser-CONSTRUCTION time: a non-numeric KX_MCP_PORT
    # was an uncaught ValueError traceback out of main() rather than a usage error, and it fired even
    # for `--help` or `--transport stdio`, where the port is never used.
    parser.add_argument(
        "--port",
        type=_tcp_port,
        default=os.environ.get("KX_MCP_PORT", "8000"),
        help="TCP port for HTTP transports (1-65535). Defaults to $KX_MCP_PORT or 8000.",
    )
    parser.add_argument(
        "--mount-timeout",
        type=_mount_timeout,
        # A string default runs through `type`, so the env var is validated like the flag.
        default=os.environ.get("KX_MCP_MOUNT_TIMEOUT", "").strip() or None,
        help="Seconds to wait for each bundle's build_server() before giving up on it and serving "
        "without that backend. Off by default. NOTE: a synchronous build_server() cannot be "
        "cancelled, so a timeout ABANDONS its startup thread rather than stopping it — prefer a "
        "bundle-level pre-flight timeout (e.g. KDBX_DB_TIMEOUT). Defaults to $KX_MCP_MOUNT_TIMEOUT.",
    )
    parser.add_argument(
        "--log-level",
        type=_log_level,
        default=os.environ.get("KX_MCP_LOG_LEVEL", "INFO"),
        help="Level for the container's own logs — the audit line and mount warnings. "
        "Defaults to $KX_MCP_LOG_LEVEL or INFO.",
    )
    parser.add_argument(
        "--exit-on-mount-failure",
        action="store_true",
        default=None,  # unset: resolved from the env var below, strictly
        help="Terminate instead of serving without a backend whose pre-flight failed. Off by "
        "default (an unreachable backend is disabled with a warning and the rest keep serving); "
        "turn it on for a one-backend-per-container deployment that wants the orchestrator to "
        "restart the process and surface the misconfiguration. "
        "Defaults to $KX_MCP_EXIT_ON_MOUNT_FAILURE.",
    )
    args = parser.parse_args(argv)
    if args.exit_on_mount_failure is None:
        # A store_true flag has no `type` to validate its env twin, so check it here. An unknown
        # value (a `ture` typo) used to read as false: the strict posture silently switched off.
        raw = os.environ.get("KX_MCP_EXIT_ON_MOUNT_FAILURE", "")
        if raw.strip().lower() not in _TRUTHY | _FALSY:
            parser.error(
                f"KX_MCP_EXIT_ON_MOUNT_FAILURE must be a boolean (true/false, 1/0, yes/no, on/off), "
                f"got {raw!r}"
            )
        args.exit_on_mount_failure = _env_flag("KX_MCP_EXIT_ON_MOUNT_FAILURE")

    # Configure the container's own logging before assembly so audit lines and any mount-time
    # warnings are visible (the launcher is an entry point; the library seam stays logging-free).
    configure_logging(args.log_level)

    # Resolve the capability-check config now so a bad KX_MCP_AUTHZ policy file fails loudly at
    # startup (a clean operator error) rather than as a silent per-request deny. No-op when
    # KX_MCP_AUTHZ is unset (route-only). The adapter check waits for build_app, once the bundles
    # that register adapters are imported. The decorator falls back to a lazy read if this is
    # ever skipped (e.g. glue).
    configure_authz(require_adapter=False)

    # Resolve the observability config now, for the same reason: a bad KX_MCP_METRICS /
    # KX_MCP_TRACING mode fails loudly here (a clean operator error) rather than silently leaving
    # the seam off. No-op when both are unset — the default posture.
    observability = ObservabilitySettings()

    bundles = _parse_bundles(args.bundles or os.environ.get("KX_MCP_BUNDLES"))
    if not bundles:
        parser.error("no bundles selected — pass --bundles or set KX_MCP_BUNDLES")

    # Bundles may CLI-parse their own settings at import; don't let them see our flags.
    sys.argv = [sys.argv[0]]

    app = build_app(
        bundles,
        name=args.name,
        exit_on_mount_failure=args.exit_on_mount_failure,
        observability=observability,
        mount_timeout=args.mount_timeout,
    )

    # The scrape endpoint is a route on the app we are about to serve, so it can only be mounted once
    # the transport is known — under stdio there is no HTTP listener and this logs a warning and
    # skips (the metrics middleware still collects in-process either way).
    mount_metrics_route(app, observability, args.transport)

    if args.transport == "stdio":
        app.run(transport="stdio")
    else:
        app.run(transport=args.transport, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
