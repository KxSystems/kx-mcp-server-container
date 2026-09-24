"""Unit tests for the shared native-discovery helper (kx_mcp_core.discovery).

These exercise the behaviors we layer on top of FastMCP's native filesystem discovery, against
a synthetic temp directory (backend-agnostic — the helper is shared by kdb-x and kdb.ai):

  1. Discovered standalone components are added onto the passed-in instance.
  2. An import failure in a module is fail-fast (raises), not swallowed.
  3. A ``*.py.template`` file is inert: ``discover_files`` globs ``*.py``, so a template is never
     imported and never registered.
"""

import importlib

import pytest
from fastmcp import FastMCP

from kx_mcp_core import register_components


def _write(directory, name, body):
    path = directory / name
    path.write_text(body)
    return path


def test_discovers_standalone_tool(tmp_path):
    _write(
        tmp_path,
        "good_tool.py",
        "from fastmcp.tools import tool\n"
        "@tool\n"
        "async def my_good_tool(x: int) -> int:\n"
        "    '''doc'''\n"
        "    return x\n",
    )

    mcp = FastMCP("t")
    registered = register_components(mcp, tmp_path)
    assert registered == ["my_good_tool"]


def test_ignores_py_template_files(tmp_path):
    # A live tool plus a *.py.template with a deliberate import error: the template must be
    # neither imported (so its error never surfaces) nor registered — discover_files globs *.py.
    _write(
        tmp_path,
        "live_tool.py",
        "from fastmcp.tools import tool\n"
        "@tool\n"
        "async def live(x: int) -> int:\n"
        "    '''doc'''\n"
        "    return x\n",
    )
    _write(tmp_path, "tool.py.template", "import this_module_does_not_exist\n")

    mcp = FastMCP("t")
    registered = register_components(mcp, tmp_path)
    assert registered == ["live"]


def test_fail_fast_on_import_error(tmp_path):
    # A live module that fails to import must abort, not start half-registered.
    _write(
        tmp_path,
        "ok_tool.py",
        "from fastmcp.tools import tool\n"
        "@tool\n"
        "async def ok(x: int) -> int:\n"
        "    '''doc'''\n"
        "    return x\n",
    )
    _write(tmp_path, "broken_tool.py", "import a_package_that_is_not_installed\n")

    mcp = FastMCP("t")
    with pytest.raises(RuntimeError, match="broken_tool.py"):
        register_components(mcp, tmp_path)


def test_two_sibling_addins_packages_do_not_collide(tmp_path):
    # Regression: two bundles each shipping an `addins` PACKAGE (as kdbx and kdbai both do) must
    # register in the same process without colliding. Before the fix, each imported as the bare
    # `addins.<stem>`, so both claimed the single top-level `addins` module in sys.modules and the
    # second bundle failed to import — which is exactly what `--bundles kdbx,kdbai` triggers.
    def make_bundle(name: str):
        addins = tmp_path / name / "addins"
        addins.mkdir(parents=True)
        (tmp_path / name / "__init__.py").write_text("")
        (addins / "__init__.py").write_text("")
        _write(
            addins,
            f"{name}_tool.py",
            "from fastmcp.tools import tool\n"
            "@tool\n"
            f"async def {name}_probe(x: int) -> int:\n"
            "    '''doc'''\n"
            "    return x\n",
        )
        # This package is created *after* an earlier import has already scanned (and cached) a
        # FileFinder for tmp_path, so the new sibling stays invisible to the import system until
        # that cache is dropped — the import then fails with a bare `No module named 'bundleb'`.
        # Whether it bites depends on the filesystem's mtime granularity: fine-grained timestamps
        # (APFS) hide it, coarser ones (some CI overlay filesystems) expose it — which is why this
        # passed locally and failed intermittently in CI. invalidate_caches() is the documented
        # remedy when modules are created during a run.
        importlib.invalidate_caches()
        return addins

    a = register_components(FastMCP("a"), make_bundle("bundlea"))
    b = register_components(FastMCP("b"), make_bundle("bundleb"))

    assert a == ["bundlea_probe"]
    assert b == ["bundleb_probe"]
