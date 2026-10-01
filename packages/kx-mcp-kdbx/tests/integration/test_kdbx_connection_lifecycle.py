"""Every kdb-x connection-lifecycle event, checked against its metric, on a real q host.

No lifecycle event used to be checked against its metric, and mocks at the PyKX boundary could not
have caught the gap: the defects came from PyKX's real `reconnection_attempts` semantics (`N` tries,
`0` forever). With `reconnection_attempts=KDBX_DB_RETRY`, PyKX reopened and resent a dead handle
itself, so `kdbx_reconnects_total` never moved and a refused reopen was never counted `failed`. And
`KDBX_DB_RETRY=0` made no attempt at all. Each test here drives a real q process (or a real socket)
and reads the process-global Prometheus registry.

**Self-skipping**: the q-host tests need a `q` binary and a kdb-x license, the same convention as
`tests/deterministic/integration/test_kdbx_sql_blocklist_bypass_e2e.py`. The mount-budget test
needs only PyKX.
"""

from __future__ import annotations

import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path

import pytest
from prometheus_client import REGISTRY

from _pykx_env import clean_env
from kx_mcp_core import mount_budget
from kx_mcp_kdbx.settings import AppSettings, KDBConfig
from kx_mcp_kdbx.utils import kdbx as kdbx_utils
from kx_mcp_kdbx.utils.kdbx import _connect, cleanup_kdb_connection, get_kdb_connection

pytestmark = pytest.mark.integration

# `deny` lets a test make the live host refuse sync queries (the probe included) with a q error.
_HOST_Q = ".s.init[];\nt:([] id:0 1 2);\ndeny:0b;\n.z.pg:{$[deny;'denied;value x]};\n"


def _q_binary() -> str | None:
    """The ``q`` on PATH, else a co-located kdb-x install under ~/.kx (the repo's dev convention)."""
    found = shutil.which("q")
    if found:
        return found
    fallback = Path.home() / ".kx" / "bin" / "q"
    return str(fallback) if fallback.exists() else None


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _sample(name: str, labels: dict | None = None) -> float:
    return REGISTRY.get_sample_value(name, labels or {}) or 0.0


def _counts() -> dict:
    return {
        "ok": _sample("kdbx_connects_total", {"outcome": "ok"}),
        "failed": _sample("kdbx_connects_total", {"outcome": "failed"}),
        "reconnects": _sample("kdbx_reconnects_total"),
    }


def _delta(before: dict) -> dict:
    return {k: v - before[k] for k, v in _counts().items()}


class _QHost:
    """One real q process on a fixed port, stoppable and restartable on that same port."""

    def __init__(self, q: str, workdir: Path):
        self.q, self.workdir, self.port = q, workdir, _free_port()
        (workdir / "host.q").write_text(_HOST_Q)
        self.env = clean_env()  # `import pykx` rewrote QHOME/QPATH for this process
        kx = Path.home() / ".kx"
        if kx.exists():
            self.env["QHOME"] = str(kx)
            self.env.setdefault("QLIC", str(kx))
        self.proc: subprocess.Popen | None = None

    def start(self) -> None:
        self.proc = subprocess.Popen(
            [self.q, "host.q", "-p", str(self.port), "-q"],
            cwd=self.workdir, env=self.env, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                out = self.proc.stdout.read() if self.proc.stdout else ""
                if "licen" in out.lower():
                    pytest.skip(f"no kdb-x license available ({out.strip()[:120]})")
                pytest.fail(f"q host exited early ({self.proc.returncode}):\n{out}")
            try:
                socket.create_connection(("127.0.0.1", self.port), timeout=0.2).close()
                return
            except OSError:
                time.sleep(0.05)
        pytest.fail(f"q host never listened on :{self.port}")

    def stop(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            self.proc.wait(timeout=10)


@pytest.fixture
def q_host(tmp_path):
    q = _q_binary()
    if not q:
        pytest.skip("no `q` binary; the connection-lifecycle tests need a kdb-x install")
    host = _QHost(q, tmp_path)
    host.start()
    yield host
    host.stop()


@pytest.fixture(autouse=True)
def _fresh_cache():
    cleanup_kdb_connection()
    yield
    cleanup_kdb_connection()


def _config(port: int, retry: int = 1, **kw) -> KDBConfig:
    return KDBConfig(_env_file=None, host="127.0.0.1", port=port, retry=retry, **kw)


@pytest.mark.parametrize("retry", [0, 1, 2])
def test_initial_connect_ok(q_host, retry):
    """`retry=0` used to mean no attempt at all; every value now connects a live host."""
    before = _counts()

    conn = get_kdb_connection(_config(q_host.port, retry))

    assert conn("count t").py() == 3
    assert _delta(before) == {"ok": 1, "failed": 0, "reconnects": 0}


@pytest.mark.parametrize("retry", [0, 1, 2])
def test_initial_connect_failed_makes_retry_plus_one_attempts(retry, monkeypatch):
    """A listener that accepts then hangs up fails every handshake; it counts the attempts.

    Attempts are counted on the client side, by spying on the real `SyncQConnection`, not by
    counting the listener's `accept()`s. The server-side count raced: the loop has no backoff, so
    the last attempt's connection could still be in the kernel backlog when the `finally` closed
    the listener, and a loaded CI runner then saw one accept fewer than attempts made (it failed
    `retry=0` and `retry=2` on the same commit, while every attempt was logged). The listener stays
    real so each handshake genuinely fails over a socket.
    """
    server = socket.create_server(("127.0.0.1", 0))
    port = server.getsockname()[1]

    def slam():
        while True:
            try:
                sock, _ = server.accept()
            except OSError:
                return
            sock.close()

    threading.Thread(target=slam, daemon=True).start()

    attempts = []
    real_connection = kdbx_utils.kx.SyncQConnection

    def counting_connection(*args, **kwargs):
        attempts.append(kwargs.get("port"))
        return real_connection(*args, **kwargs)

    monkeypatch.setattr(kdbx_utils.kx, "SyncQConnection", counting_connection)

    before = _counts()
    try:
        with pytest.raises(Exception):
            get_kdb_connection(_config(port, retry))
    finally:
        server.close()

    assert attempts == [port] * (retry + 1)
    assert _delta(before) == {"ok": 0, "failed": 1, "reconnects": 0}


def test_preflight_connect_is_counted(q_host):
    """The startup pre-flight goes through `_connect`, so it is counted like any other connect."""
    from kx_mcp_kdbx.server import build_server

    before = _counts()
    build_server(AppSettings(db=_config(q_host.port)))
    assert _delta(before) == {"ok": 1, "failed": 0, "reconnects": 0}

    q_host.stop()
    before = _counts()
    with pytest.raises(SystemExit):
        build_server(AppSettings(db=_config(q_host.port)))
    assert _delta(before) == {"ok": 0, "failed": 1, "reconnects": 0}


def test_backend_restart_counts_a_reconnect_and_the_query_succeeds(q_host):
    """PyKX used to reopen and resend silently, so the reconnect was never counted."""
    cfg = _config(q_host.port)
    get_kdb_connection(cfg)
    q_host.stop()
    q_host.start()
    before = _counts()

    conn = get_kdb_connection(cfg)

    assert conn("count t").py() == 3
    assert _delta(before) == {"ok": 1, "failed": 0, "reconnects": 1}


def test_refused_reconnect_counts_a_failed_connect(q_host):
    """a refused reopen surfaced as "hop… Connection refused" and went uncounted."""
    cfg = _config(q_host.port)
    get_kdb_connection(cfg)
    q_host.stop()
    before = _counts()

    with pytest.raises(Exception):
        get_kdb_connection(cfg)

    assert _delta(before) == {"ok": 0, "failed": 1, "reconnects": 0}


def test_q_side_error_is_not_a_reconnect(q_host):
    """A live host answering the probe with a q error keeps its handle: no reopen, nothing counted."""
    cfg = _config(q_host.port)
    get_kdb_connection(cfg)("deny:1b")
    before = _counts()

    with pytest.raises(Exception, match="denied"):
        get_kdb_connection(cfg)

    assert _delta(before) == {"ok": 0, "failed": 0, "reconnects": 0}


@pytest.fixture
def wedged_port():
    """A real listener that accepts and never answers the qIPC handshake."""
    server = socket.create_server(("127.0.0.1", 0))
    held: list[socket.socket] = []

    def hold():
        while True:
            try:
                held.append(server.accept()[0])
            except OSError:
                return

    threading.Thread(target=hold, daemon=True).start()
    yield server.getsockname()[1]
    server.close()
    for sock in held:
        sock.close()


def test_wedged_connect_is_bounded_by_the_mount_budget(wedged_port):
    """a host that accepts and never answers the handshake held `hopen`, and with it the
    GIL, for `KDBX_DB_TIMEOUT` per attempt, so the launcher's mount timeout could not fire."""
    started = time.monotonic()
    with mount_budget(1.0), pytest.raises(Exception) as exc:
        _connect(_config(wedged_port, retry=2, timeout=5))

    assert time.monotonic() - started < 2.0
    assert type(exc.value).__name__ == "MountBudgetExhausted"
    assert "mount budget" in str(exc.value)


def test_wedged_preflight_is_bounded_by_the_mount_budget(wedged_port):
    """The same bound on the pre-flight, which is what the launcher's worker actually runs."""
    from kx_mcp_kdbx.server import build_server

    started = time.monotonic()
    with mount_budget(1.0), pytest.raises(SystemExit):
        build_server(AppSettings(db=_config(wedged_port, retry=2, timeout=5)))

    assert time.monotonic() - started < 2.0
