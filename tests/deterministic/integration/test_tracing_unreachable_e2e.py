"""An unreachable OTLP endpoint is warned about, over a real launcher process.

Regression: exporter construction never contacts the collector and the log filter hid the
SDK's own export-failure warnings, so a mistyped or down endpoint produced no line at all. In-process
tests can't see that: the filter and the startup probe only matter in the launcher's real logging setup.
License-free `example` bundle.
"""

from __future__ import annotations

import socket
import subprocess
import time

import httpx


def _closed_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_unreachable_otlp_endpoint_warns_and_serving_is_not_delayed(spawn_container):
    endpoint = f"http://127.0.0.1:{_closed_port()}"

    started = time.monotonic()
    url, proc = spawn_container(KX_MCP_TRACING="otlp", KX_MCP_TRACING_OTLP_ENDPOINT=endpoint)
    listening_after = time.monotonic() - started

    health = httpx.get(f"{url.rsplit('/mcp', 1)[0]}/health", timeout=10)
    assert health.status_code == 200, health.text
    assert listening_after < 15, f"startup took {listening_after:.1f}s"

    time.sleep(3)  # the probe runs on a background thread; give it its connect timeout
    proc.terminate()
    try:
        out, _ = proc.communicate(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, _ = proc.communicate()

    assert "serving without traces" in out, out
    assert "is unreachable" in out, out


def test_reachable_otlp_endpoint_does_not_warn(spawn_container):
    """The probe must not cry wolf: a port that accepts the connection produces no warning."""
    with socket.socket() as collector:
        collector.bind(("127.0.0.1", 0))
        collector.listen(8)
        endpoint = f"http://127.0.0.1:{collector.getsockname()[1]}"

        _, proc = spawn_container(KX_MCP_TRACING="otlp", KX_MCP_TRACING_OTLP_ENDPOINT=endpoint)
        time.sleep(3)
        proc.terminate()
        try:
            out, _ = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            out, _ = proc.communicate()

    assert "is unreachable" not in out, out
