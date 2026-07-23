"""`kx auth login` end to end: RFC 9728 discovery → RFC 7591 DCR → RFC 8628 device-code → cache.

A dependency-free in-process mock authorization server (one origin doubling as the resource server's
metadata host and the AS) serves the well-known documents and the device/token/registration
endpoints. Its behaviour is configurable per test (device support, registration support, the token
poll sequence) so we can drive the happy path, the DCR path, denial, and the no-device-support error
without a live IdP or Docker. `discovery.time.sleep` is neutralised so the poll loop is instant.
"""

from __future__ import annotations

import json
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from kx_auth_cli import cache, cli, discovery


def _run(argv: list[str]) -> int:
    with pytest.raises(SystemExit) as exc:
        cli.main(argv)
    code = exc.value.code
    return code if isinstance(code, int) else 1


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    monkeypatch.setenv("KX_AUTH_CACHE", str(tmp_path / "creds.json"))
    monkeypatch.delenv("KX_AUTH_CLIENT_ID", raising=False)
    monkeypatch.setattr(discovery.time, "sleep", lambda _s: None)  # no real waiting in the poll loop


@pytest.fixture
def mock_as():
    """Start a mock AS; returns (base_url, state). Mutate state['behavior'] before invoking login."""
    state = {
        "captured": [],
        "gets": [],
        "behavior": {
            "device": True,
            "registration": True,
            "path_prm": False,  # serve the RFC 9728 path-aware PRM form too
            # (status, body) popped from the front; the last entry repeats.
            # A str body is served raw as text/html (a non-JSON proxy error page).
            "token_responses": [
                (400, {"error": "authorization_pending"}),
                (200, {"access_token": "at-123", "token_type": "Bearer", "expires_in": 600,
                       "refresh_token": "rt-9", "scope": "kdbx.read"}),
            ],
        },
    }
    base_holder: dict = {}

    def _json(handler, status, body):
        if isinstance(body, str):
            payload, ctype = body.encode(), "text/html"
        else:
            payload, ctype = json.dumps(body).encode(), "application/json"
        handler.send_response(status)
        handler.send_header("Content-Type", ctype)
        handler.end_headers()
        handler.wfile.write(payload)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            base = base_holder["base"]
            beh = state["behavior"]
            state["gets"].append(self.path)
            if self.path.endswith("/.well-known/oauth-protected-resource") or (
                beh["path_prm"] and "/.well-known/oauth-protected-resource/" in self.path
            ):
                _json(self, 200, {"resource": base, "authorization_servers": [base]})
            elif self.path.endswith(".well-known/oauth-authorization-server"):
                meta = {"issuer": base, "token_endpoint": f"{base}/token"}
                if beh["device"]:
                    meta["device_authorization_endpoint"] = f"{base}/device"
                if beh["registration"]:
                    meta["registration_endpoint"] = f"{base}/register"
                _json(self, 200, meta)
            else:
                _json(self, 404, {"error": "not_found"})

        def do_POST(self):  # noqa: N802
            base = base_holder["base"]
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length).decode()
            form = {k: v[0] for k, v in urllib.parse.parse_qs(raw).items()}
            self_path = self.path.rstrip("/")
            state["captured"].append({"path": self_path, **form})
            if self_path.endswith("/register"):
                _json(self, 201, {"client_id": "dyn-client-123"})
            elif self_path.endswith("/device"):
                _json(self, 200, {
                    "device_code": "dev-abc", "user_code": "WXYZ-1234",
                    "verification_uri": f"{base}/activate", "interval": 0, "expires_in": 600,
                })
            elif self_path.endswith("/token"):
                seq = state["behavior"]["token_responses"]
                status, body = seq[0] if len(seq) == 1 else seq.pop(0)
                _json(self, status, body)
            else:
                _json(self, 404, {"error": "not_found"})

        def log_message(self, *_a):
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    base_holder["base"] = f"http://127.0.0.1:{httpd.server_address[1]}"
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield base_holder["base"], state
    finally:
        httpd.shutdown()


def test_device_code_happy_path_caches_token(mock_as, capsys):
    base, state = mock_as
    code = _run(["auth", "login", "--server", base, "--json"])
    assert code == 0
    envelope = json.loads(capsys.readouterr().out)
    assert envelope["status"] == "ok"
    assert envelope["access_token"] == "at-123"
    assert envelope["cached"] is True
    # the token landed in the endpoint-keyed cache
    entry = cache.get(base)
    assert entry["access_token"] == "at-123"
    assert entry["refresh_token"] == "rt-9"


def test_dcr_registers_a_client_when_no_client_id(mock_as):
    base, state = mock_as
    assert _run(["auth", "login", "--server", base]) == 0
    # DCR happened, and the dynamically-registered client_id was used for device + token calls
    paths = [c["path"] for c in state["captured"]]
    assert any(p.endswith("/register") for p in paths)
    device_calls = [c for c in state["captured"] if c["path"].endswith("/device")]
    assert device_calls and device_calls[0]["client_id"] == "dyn-client-123"


def test_explicit_client_id_skips_dcr(mock_as):
    base, state = mock_as
    assert _run(["auth", "login", "--server", base, "--client-id", "my-public-client"]) == 0
    paths = [c["path"] for c in state["captured"]]
    assert not any(p.endswith("/register") for p in paths)  # --client-id overrides DCR
    device_calls = [c for c in state["captured"] if c["path"].endswith("/device")]
    assert device_calls[0]["client_id"] == "my-public-client"


def test_access_denied_is_denied_4(mock_as, capsys):
    base, state = mock_as
    state["behavior"]["token_responses"] = [(400, {"error": "access_denied"})]
    code = _run(["auth", "login", "--server", base, "--json"])
    assert code == 4
    assert json.loads(capsys.readouterr().out)["status"] == "denied"


def test_no_device_support_is_error_1(mock_as, capsys):
    base, state = mock_as
    state["behavior"]["device"] = False
    state["behavior"]["registration"] = False  # force an explicit --client-id path past discovery
    code = _run(["auth", "login", "--server", base, "--client-id", "c1", "--json"])
    assert code == 1
    assert json.loads(capsys.readouterr().out)["status"] == "error"


def test_path_mounted_server_uses_path_aware_prm(mock_as):
    base, state = mock_as
    state["behavior"]["path_prm"] = True
    server = f"{base}/mcp"
    assert _run(["auth", "login", "--server", server]) == 0
    # the RFC 9728 path-aware form was tried first, and the cache is keyed by the full server URL
    assert state["gets"][0] == "/.well-known/oauth-protected-resource/mcp"
    assert cache.get(server)["access_token"] == "at-123"


def test_prm_discovery_falls_back_to_origin_root(mock_as):
    base, state = mock_as  # the mock serves only the origin-root PRM form by default
    assert _run(["auth", "login", "--server", f"{base}/mcp"]) == 0
    assert state["gets"][0] == "/.well-known/oauth-protected-resource/mcp"  # tried (404s)...
    assert state["gets"][1] == "/.well-known/oauth-protected-resource"  # ...then the root form


def test_non_json_token_response_is_clean_error_1(mock_as, capsys):
    base, state = mock_as
    state["behavior"]["token_responses"] = [(502, "<html>bad gateway</html>")]
    code = _run(["auth", "login", "--server", base, "--json"])
    assert code == 1  # a clean envelope, not a JSONDecodeError traceback
    assert json.loads(capsys.readouterr().out)["status"] == "error"
