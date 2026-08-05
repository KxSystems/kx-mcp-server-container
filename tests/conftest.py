"""Container-level test fixtures for the streamable-http subprocess spawn harness.

Scoped to the container ``tests/`` tree (not the per-package suites). Provides
``_free_port``, ``_wait_until_listening``, and ``spawn_container`` for any test that
needs to drive the launcher as a real child process over HTTP — used by
``test_auth_integration.py`` and the real-IdP harness in ``tests/deterministic/realidp/``.

Auth fixtures (``keypair``, ``mint``, ``other_priv``, ``jwks_uri``) and the
``KID``/``ISSUER``/``AUDIENCE`` constants live one level up in the repo-root
``conftest.py``, shared across all package test trees.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

# Every fixture bundle's src/ dir goes on the spawned container's PYTHONPATH so the launcher
# subprocess can import it by package name (kx-mcp-example/src -> `kx_mcp_example`). Globbed rather
# than enumerated, so a new tests/fixtures/<bundle>/src is discovered with no edit here.
_FIXTURE_SRCS = [str(p) for p in sorted((Path(__file__).parent / "fixtures").glob("*/src"))]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_until_listening(port: int, proc: subprocess.Popen, timeout: float = 20.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            out = proc.stdout.read() if proc.stdout else ""
            raise RuntimeError(f"launcher exited early ({proc.returncode}):\n{out}")
        with socket.socket() as s:
            s.settimeout(0.25)
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.2)
    raise RuntimeError("launcher did not start listening in time")


@pytest.fixture
def spawn_container(tmp_path):
    """Spawn the launcher as a real streamable-http child process.

    Yields a factory ``start(**env_overrides) -> (url, proc)`` so callers can
    configure auth env vars per-test. The process is terminated on teardown.

    Usage::

        def test_something(spawn_container):
            url, proc = spawn_container(
                KX_MCP_AUTH="static",
                KX_MCP_AUTH_PUBLIC_KEY_PATH=str(key_path),
                KX_MCP_AUTH_ISSUER="https://issuer.test",
                KX_MCP_AUTH_AUDIENCE="kx-mcp",
            )
            # ... drive the server at url ...

        def test_acme(spawn_container):
            url, proc = spawn_container("acme", KX_MCP_AUTH="jwks", ...)
            # ... drive the mounted extension ...
    """
    procs: list[subprocess.Popen] = []

    def _start(bundles: str = "example", **env_overrides: str) -> tuple[str, subprocess.Popen]:
        port = _free_port()
        env = {
            **os.environ,
            "PYTHONPATH": os.pathsep.join([*_FIXTURE_SRCS, os.environ.get("PYTHONPATH", "")]),
            **env_overrides,
        }
        proc = subprocess.Popen(
            [
                sys.executable, "-m", "kx_mcp_core.launcher",
                "--bundles", bundles,
                "--transport", "streamable-http",
                "--host", "127.0.0.1",
                "--port", str(port),
            ],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        procs.append(proc)
        _wait_until_listening(port, proc)
        return f"http://127.0.0.1:{port}/mcp", proc

    yield _start

    for proc in procs:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
