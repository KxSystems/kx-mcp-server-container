"""Shared filesystem-discovery glue for backend add-ins (tools / resources / prompts, one folder).

A thin adapter over FastMCP 3.x's native filesystem discovery
(``fastmcp.server.providers.filesystem_discovery``). The native helper walks a directory,
imports every module, and extracts the component objects that the standalone ``@tool`` /
``@resource`` / ``@prompt`` decorators attach as ``__fastmcp__`` metadata. We then register
each discovered component onto the *passed-in* FastMCP instance.

This lives in ``kx-mcp-core`` because it is shared across backend bundles: both the kdb-x
(``kx_mcp_kdbx.addins``) and kdb.ai (``kx_mcp_kdbai.addins``) extensions consume it, so it is
real cross-bundle DRY rather than a single-consumer helper. It is fastmcp-only — it registers
components onto a FastMCP instance and knows nothing about any specific backend.

Two behaviors the native helper does not give us for free are reproduced here:

1. **Fail-fast.** ``discover_and_import`` collects import failures into ``.failed_files``
   and does NOT raise. We log each failure and then RAISE — a bundle must not start
   half-registered (matches the pre-refactor behavior where a failed module aborted
   ``build_server()``).

2. **Instance-safety.** A discovered component object is added onto the per-call FastMCP
   instance passed in, never a module global, so the same source can back two mounted
   backends. The tools read their config from ``ctx.fastmcp`` at call time.

Disabling a module is by convention: ``discover_files`` globs ``*.py`` (and skips
``__init__.py`` / ``__pycache__``), so a copy-me template named ``*.py.template`` is never
matched, never imported, and never registered — no bespoke filter needed.
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastmcp import FastMCP
from fastmcp.prompts.base import Prompt
from fastmcp.resources.base import Resource
from fastmcp.resources.template import ResourceTemplate
from fastmcp.server.providers.filesystem_discovery import (
    discover_files,
    extract_components,
    import_module_from_file,
)
from fastmcp.tools.base import Tool

logger = logging.getLogger(__name__)


def _package_root(directory: Path) -> Path:
    """Walk up from ``directory`` while each parent is still a package (has ``__init__.py``).

    Returns the topmost package directory containing ``directory``. Passed to FastMCP's
    ``import_module_from_file`` as the ``provider_root`` boundary so each add-in imports under its
    fully-qualified module name (e.g. ``kx_mcp_kdbai.addins.kdbai_data``) rather than a bare
    ``addins.<stem>``. The bare form collides when TWO bundles each ship an ``addins/`` package
    (both would claim the top-level ``addins`` module in ``sys.modules``) — which happens whenever
    the container mounts more than one such backend (``--bundles kdbx,kdbai``).
    """
    root = directory.resolve()
    while (root.parent / "__init__.py").is_file():
        root = root.parent
    return root


def register_components(mcp: FastMCP, directory: Path) -> list[str]:
    """Discover add-in modules under ``directory`` and register them onto ``mcp``.

    Composed from FastMCP's native discovery primitives (``discover_files`` +
    ``import_module_from_file`` + ``extract_components``) rather than the all-in-one
    ``discover_and_import``, so we can fail-fast on an import error instead of swallowing it.
    The add-method is dispatched off each discovered component's concrete type, so a directory
    that yields a mix of tools, resources, resource templates, and prompts is handled in one
    pass.

    Args:
        mcp: The per-call FastMCP instance to register onto (instance-safe — never a global).
        directory: The package directory to scan (typically ``Path(__file__).parent``).

    Returns:
        The list of registered component names (bare names; the container supplies the
        backend qualifier at ``mount(namespace=...)``).

    Raises:
        RuntimeError: if any module fails to import (fail-fast — the bundle must not start
            half-registered).
    """
    logger.info("Starting add-in discovery in %s", directory)

    # Import each add-in under its fully-qualified module name (rooted at the top installed
    # package), not the bare folder name — so two bundles' `addins/` packages don't collide.
    provider_root = _package_root(directory)

    registered: list[str] = []
    failed: dict[Path, str] = {}

    for source_path in discover_files(directory):
        try:
            module = import_module_from_file(source_path, provider_root=provider_root)
        except Exception as e:  # noqa: BLE001 - collected for fail-fast below
            failed[source_path] = str(e)
            logger.error("Failed to import add-in module '%s': %s", source_path.name, e)
            continue

        for component in extract_components(module):
            if isinstance(component, Tool):
                mcp.add_tool(component)
            elif isinstance(component, ResourceTemplate):
                mcp.add_template(component)
            elif isinstance(component, Resource):
                mcp.add_resource(component)
            elif isinstance(component, Prompt):
                mcp.add_prompt(component)
            else:  # pragma: no cover - defensive; FastMCP only yields the four types above
                logger.warning(
                    "Skipping unknown component type %r from '%s'",
                    type(component).__name__,
                    source_path.name,
                )
                continue

            registered.append(component.name)
            logger.info(
                "Registered add-in '%s' from module '%s'", component.name, source_path.name
            )

    # Fail-fast: a half-registered bundle is worse than a clean startup error. The native
    # discover_and_import swallows import failures into .failed_files; we raise instead, matching
    # the pre-refactor behavior where a failed primitive module aborted build_server().
    if failed:
        names = ", ".join(p.name for p in failed)
        raise RuntimeError(
            f"add-in discovery failed to import: {names}. Refusing to start half-registered."
        )

    logger.info("Successfully registered %d add-in(s): %s", len(registered), registered)
    return registered
