"""The launcher's startup config, validated where an operator meets it: a real `kx-mcp` process.

Both defects here passed every unit test, because they live in the ORDER and SOURCE of startup
config, which only the real entry point exercises:

* **Authz mode vs bundle import.** A backend's authz adapter (`kdbx_rbac`) registers when its bundle
  is imported, which the launcher does only when it mounts. The adapter check ran before that, so
  `KX_MCP_AUTHZ=kdbx_rbac kx-mcp --bundles kdbx`, the documented launch, refused to start. Every mode
  gets a startup case, and a mode nothing registers must still refuse (fail closed).
* **Env-sourced defaults.** argparse checks `choices` only for a typed flag, never for a default, so
  `KX_MCP_TRANSPORT=sse` served the SSE transport that `--transport sse` refuses. Every env default
  with a closed set of values gets a bad-value case: it must be the same usage error (exit 2).

The `kdbx_rbac` case needs a `q` binary and a kdb-x license and skips without them; the rest use the
license-free fixture bundles.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

from _pykx_env import stripped_from_os_environ

pytestmark = pytest.mark.integration

_FIXTURE_SRCS = [
    str(p) for p in sorted((Path(__file__).resolve().parents[2] / "fixtures").glob("*/src"))
]
_HOST_Q = Path(__file__).with_name("kdbx_sql_blocklist_host.q")


def _run_launcher(*argv: str, **env: str) -> subprocess.CompletedProcess:
    """Run `kx-mcp` to completion, for a config that must refuse to start.

    stdin is closed so a stdio server that wrongly starts exits at once instead of hanging the test.
    """
    return subprocess.run(
        [sys.executable, "-m", "kx_mcp_core.launcher", *argv],
        env={
            **os.environ,
            "PYTHONPATH": os.pathsep.join([*_FIXTURE_SRCS, os.environ.get("PYTHONPATH", "")]),
            **env,
        },
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=60,
    )


# --- authz mode: checked after the bundles that register adapters are imported -------------------


@pytest.fixture(scope="module")
def q_host():
    """A plain q process for the kdbx bundle's pre-flight; skips without `q` or a license."""
    q = shutil.which("q") or str(Path.home() / ".kx" / "bin" / "q")
    if not Path(q).exists():
        pytest.skip("no `q` binary — the kdbx_rbac startup case needs a kdb-x install")
    env = dict(os.environ)
    kx = Path.home() / ".kx"
    if kx.exists():
        env["QHOME"] = str(kx)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    proc = subprocess.Popen(
        [q, str(_HOST_Q), "-p", str(port)],
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    deadline = time.time() + 20
    while time.time() < deadline:
        if proc.poll() is not None:
            out = proc.stdout.read() if proc.stdout else ""
            pytest.skip(f"q exited before listening (no license?): {out.strip()[:120]}")
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                break
        time.sleep(0.2)
    else:
        proc.kill()
        pytest.fail(f"q never listened on :{port}")
    yield port
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()


@pytest.fixture
def policy_file(tmp_path) -> str:
    path = tmp_path / "capability-policy.yaml"
    path.write_text("example:\n  write: [traders]\n")
    return str(path)


def _serves(spawn_container, bundles: str, **env: str) -> None:
    url, _ = spawn_container(bundles, **env)  # raises if the launcher exits before listening
    assert httpx.get(url.rsplit("/mcp", 1)[0] + "/health", timeout=10).status_code == 200


@pytest.mark.parametrize(
    "mode",
    [
        "static",  # the built-in adapter: registered by configure_authz, policy file checked eagerly
        "outcomes_import_time",  # registered on bundle import, like kdbx_rbac, but license-free
        "kdbx_rbac",  # the shipped import-time adapter, through the real kdbx bundle
    ],
)
def test_every_registered_authz_mode_starts_under_the_launcher(
    mode, spawn_container, policy_file, request
):
    """REGRESSION: an adapter that registers on bundle import no longer refuses to start.

    Add a case here for each new `KX_MCP_AUTHZ` mode, with the bundle that provides it.
    """
    if mode == "static":
        _serves(spawn_container, "example", KX_MCP_AUTHZ=mode, KX_MCP_AUTHZ_POLICY_FILE=policy_file)
    elif mode == "outcomes_import_time":
        _serves(spawn_container, "outcomes", KX_MCP_AUTHZ=mode)
    else:
        port = request.getfixturevalue("q_host")
        # The pytest process may have imported pykx already, whose env would break the child's own.
        with stripped_from_os_environ():
            _serves(
                spawn_container,
                "kdbx",
                KX_MCP_AUTHZ=mode,
                KDBX_DB_HOST="127.0.0.1",
                KDBX_DB_PORT=str(port),
                KDBX_DB_TIMEOUT="10",
            )


@pytest.mark.parametrize(
    ("mode", "bundles"),
    [
        ("no_such_mode", "example"),  # a typo: nothing will ever register it
        ("outcomes_import_time", "example"),  # a real mode whose provider bundle did not load
    ],
)
def test_a_mode_with_no_registered_adapter_refuses_to_start(mode, bundles):
    """Deferring the adapter check must not open a gap: unregistered after mounting is still fatal."""
    proc = _run_launcher("--bundles", bundles, "--transport", "stdio", KX_MCP_AUTHZ=mode)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "no registered authz adapter" in proc.stderr
    assert "Uvicorn running" not in proc.stderr + proc.stdout


def test_a_mode_whose_provider_bundle_did_not_mount_refuses_to_start(q_host):
    """The adapter registered (kx_mcp_kdbx imported) but kdbx's pre-flight failed, so it did not mount.

    Serving the healthy `example` bundle would leave `kdbx_rbac` deciding against a backend that is
    not there: every gated call denied at runtime. Refuse at startup and name the backend instead.
    `q_host` is requested only for its skip (no q, no license: kx_mcp_kdbx cannot import).
    """
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        closed_port = s.getsockname()[1]  # released on exit, so nothing listens there
    with stripped_from_os_environ():
        proc = _run_launcher(
            "--bundles", "kdbx,example", "--transport", "stdio",
            KX_MCP_AUTHZ="kdbx_rbac",
            KDBX_DB_HOST="127.0.0.1",
            KDBX_DB_PORT=str(closed_port),
        )
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "provided by backend 'kdbx', which did not mount" in proc.stderr


# --- env-sourced defaults: validated like the flag they default ---------------------------------


@pytest.mark.parametrize(
    ("var", "value"),
    [
        ("KX_MCP_TRANSPORT", "sse"),  # deprecated; `--transport sse` was already refused
        ("KX_MCP_TRANSPORT", "bogus"),  # was a traceback from run(), after every bundle mounted
        ("KX_MCP_LOG_LEVEL", "verbose"),  # was silently INFO
        ("KX_MCP_EXIT_ON_MOUNT_FAILURE", "ture"),  # was silently off: the strict posture lost
        ("KX_MCP_MOUNT_TIMEOUT", "3s"),  # was a warning, and the bound silently off
        ("KX_MCP_PORT", "http"),  # already a usage error; pinned with its siblings
    ],
)
def test_a_bad_env_default_is_a_usage_error(var, value):
    """REGRESSION: each env twin of a flag is held to the flag's own validation (exit 2)."""
    # stdio unless the transport is what's under test, so a value wrongly accepted exits fast.
    transport = [] if var == "KX_MCP_TRANSPORT" else ["--transport", "stdio"]
    proc = _run_launcher("--bundles", "example", *transport, **{var: value})
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "usage: kx-mcp" in proc.stderr
    assert value in proc.stderr


# --- mounting nothing: a container with no backends exits rather than serve nothing -----------------


def test_mounting_no_requested_backend_exits_non_zero():
    """README and deployment.md disagreed on a lone backend that does not come up; the code exits.

    `try_mount_bundle` keeps serving past one failed backend, but with none mounted there is nothing
    to serve, so the launcher exits 1 even without KX_MCP_EXIT_ON_MOUNT_FAILURE.
    """
    result = _run_launcher("--bundles", "no_such_bundle", "--transport", "stdio")

    assert result.returncode == 1, result.stderr
    assert "no backends mounted" in result.stderr
