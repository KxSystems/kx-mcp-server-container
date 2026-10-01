"""Real-q-host fixtures for the kdb-x backend's in-process integration tests.

The unit tests mock the connection, which is exactly where the `.j.j` -> `json.loads` path and the
qIPC accounting can drift from reality unseen. These tests spawn a real `q` with a small host
script and drive the backend's `*_impl` / document builders against it over a real PyKX handle.

Every test here is marked `integration` and runs in the default suite. They skip cleanly when no
`q` binary is found or q cannot start for want of a license, the same convention as
`tests/deterministic/integration/test_kdbx_sql_blocklist_bypass_e2e.py`.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import time
from pathlib import Path

import pytest

from kx_mcp_kdbx.settings import KDBConfig
from kx_mcp_kdbx.utils.kdbx import cleanup_kdb_connection


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    here = str(Path(__file__).parent)
    for item in items:
        if str(item.fspath).startswith(here):
            item.add_marker(pytest.mark.integration)


def _q_binary() -> str | None:
    """The `q` on PATH, else a co-located kdb-x install under ~/.kx (the repo's dev convention)."""
    found = shutil.which("q")
    if found:
        return found
    fallback = Path.home() / ".kx" / "bin" / "q"
    return str(fallback) if fallback.exists() else None


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def host_config(request, tmp_path_factory):
    """Run the test module's `HOST_Q` script in a real q on a free port; yield a `KDBConfig` for it.

    One host per module, so each module's tables are exactly the ones its script defines.
    """
    script = request.module.HOST_Q
    directory = tmp_path_factory.mktemp("qhost")
    q = _q_binary()
    if not q:
        pytest.skip("no `q` binary: these tests need a kdb-x install")
    path = directory / "host.q"
    path.write_text(script, encoding="utf-8")
    # `import pykx` rewrites QHOME/QPATH in this process; hand the child the kdb-x install's own.
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYKX_") and k != "QPATH"}
    kx_home = Path.home() / ".kx"
    if kx_home.exists():
        env["QHOME"] = str(kx_home)
    port = _free_port()
    proc = subprocess.Popen(
        [q, str(path), "-p", str(port), "-q"],
        cwd=directory,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    import pykx as kx  # local import: only a real host needs a real handle

    deadline, out, last = time.time() + 20.0, "", None
    try:
        while True:
            if proc.poll() is not None:
                out = proc.stdout.read() if proc.stdout else ""
                if "licen" in out.lower():
                    pytest.skip(f"no kdb-x license available ({out.strip()[:120]})")
                pytest.fail(f"q host exited early ({proc.returncode}):\n{out}")
            try:
                probe = kx.SyncQConnection(host="127.0.0.1", port=port, timeout=2)
                probe.close()
                break
            except Exception as error:  # noqa: BLE001 - not listening yet, keep polling
                last = error
                if time.time() > deadline:
                    pytest.fail(f"q host never listened on :{port}: {last}")
                time.sleep(0.2)
        yield KDBConfig(
            host="127.0.0.1", port=port, timeout=5, retry=1, assert_identity=False, data_gate=False
        )
    finally:
        cleanup_kdb_connection()
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
