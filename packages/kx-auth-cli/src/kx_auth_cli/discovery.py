"""OAuth discovery + device-code primitives for ``kx auth login`` (stdlib + httpx, fastmcp-free).

The mediation model is fixed by the MCP spec and `mcp-container-design.md`: the agent points at an
endpoint it already knows — **the MCP server (the container / resource server)** — and that endpoint
advertises its authorization server. So the flow is:

1. **RFC 9728** — GET the protected-resource metadata → ``authorization_servers``. The path-aware
   form the RFC specifies is tried first (``/.well-known/oauth-protected-resource/<path>`` for a
   server mounted under a path), falling back to the origin-root form.
2. **RFC 8414 / OIDC** — GET the AS's ``.well-known`` metadata → device + token (+ registration)
   endpoints.
3. **RFC 7591 (optional)** — Dynamic Client Registration when the AS advertises a
   ``registration_endpoint`` and no ``--client-id`` was given (the spec's "user never hand-configures
   a client-id" ideal).
4. **RFC 8628** — device-authorization request, then poll the token endpoint.

After discovery the client talks **directly to the authorization server** — never back through the
container (the spec forbids the resource server passing the token through). Errors carry a ``reason``
code so the command layer can map them to the stable exit-code contract.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable, Optional
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx


class DiscoveryError(Exception):
    """RFC 9728/8414 discovery failed (no metadata, no AS, no required endpoint)."""


class DeviceAuthError(Exception):
    """The device-code flow failed. ``reason`` is the OAuth error code (or a synthetic one).

    Reasons the command layer maps to exit codes: ``access_denied`` → 4 (denied);
    ``expired_token`` / ``timeout`` → 3 (auth-required); everything else → 1 (error).
    """

    def __init__(self, message: str, *, reason: str = "error") -> None:
        super().__init__(message)
        self.reason = reason


@dataclass
class AuthMetadata:
    """The authorization-server endpoints `login` needs, resolved from discovery."""

    authorization_server: str
    token_endpoint: str
    device_authorization_endpoint: Optional[str] = None
    registration_endpoint: Optional[str] = None


def _prm_candidates(server: str) -> list[str]:
    """RFC 9728 well-known URLs for a resource: the path-aware form first, then the origin root."""
    parts = urlsplit(server)
    origin = urlunsplit((parts.scheme, parts.netloc, "", "", ""))
    root = origin + "/.well-known/oauth-protected-resource"
    path = parts.path.strip("/")
    return [f"{root}/{path}", root] if path else [root]


def _get_json(client: httpx.Client, url: str) -> dict[str, Any]:
    resp = client.get(url, headers={"Accept": "application/json"})
    resp.raise_for_status()
    try:
        body = resp.json()
    except ValueError as exc:
        raise DiscoveryError(f"metadata at {url} was not valid JSON") from exc
    if not isinstance(body, dict):
        raise DiscoveryError(f"metadata at {url} was not a JSON object")
    return body


def discover(server: str, *, client: httpx.Client) -> AuthMetadata:
    """Resolve the AS endpoints for an MCP-server (resource) URL via RFC 9728 → RFC 8414/OIDC."""
    prm: Optional[dict[str, Any]] = None
    last_prm_exc: Optional[Exception] = None
    for prm_url in _prm_candidates(server):
        try:
            candidate = _get_json(client, prm_url)
        except (httpx.HTTPError, DiscoveryError) as exc:
            last_prm_exc = exc
            continue
        if candidate.get("authorization_servers"):
            prm = candidate
            break
        last_prm_exc = DiscoveryError(
            f"{prm_url} advertised no authorization_servers — cannot discover an IdP"
        )
    if prm is None:
        raise DiscoveryError(
            f"could not discover protected-resource metadata for {server}: {last_prm_exc}"
        )
    as_url = prm["authorization_servers"][0]

    # RFC 8414 and OIDC differ in well-known path; try both against the AS origin + issuer path.
    candidates = [
        urljoin(as_url.rstrip("/") + "/", ".well-known/oauth-authorization-server"),
        urljoin(as_url.rstrip("/") + "/", ".well-known/openid-configuration"),
    ]
    last_exc: Optional[Exception] = None
    for url in candidates:
        try:
            meta = _get_json(client, url)
        except (httpx.HTTPError, DiscoveryError) as exc:
            last_exc = exc
            continue
        token_endpoint = meta.get("token_endpoint")
        if not token_endpoint:
            last_exc = DiscoveryError(f"{url} advertised no token_endpoint")
            continue
        return AuthMetadata(
            authorization_server=as_url,
            token_endpoint=token_endpoint,
            device_authorization_endpoint=meta.get("device_authorization_endpoint"),
            registration_endpoint=meta.get("registration_endpoint"),
        )
    raise DiscoveryError(
        f"could not fetch authorization-server metadata from {as_url}: {last_exc}"
    )


def register_client(
    meta: AuthMetadata, *, client: httpx.Client, client_name: str, scopes: Optional[list[str]] = None
) -> str:
    """RFC 7591 Dynamic Client Registration for the device-code flow → the issued ``client_id``."""
    if not meta.registration_endpoint:
        raise DiscoveryError(
            "the authorization server advertises no registration_endpoint — pass --client-id "
            "(or $KX_AUTH_CLIENT_ID) instead"
        )
    payload: dict[str, Any] = {
        "client_name": client_name,
        "grant_types": ["urn:ietf:params:oauth:grant-type:device_code"],
        "token_endpoint_auth_method": "none",  # public client — device-code, no secret
    }
    if scopes:
        payload["scope"] = " ".join(scopes)
    try:
        resp = client.post(meta.registration_endpoint, json=payload)
        resp.raise_for_status()
        body = resp.json()
    except (httpx.HTTPError, ValueError) as exc:  # ValueError: a non-JSON body
        raise DiscoveryError(f"dynamic client registration failed: {exc}") from exc
    client_id = body.get("client_id")
    if not client_id:
        raise DiscoveryError("registration endpoint returned no client_id")
    return client_id


def start_device_authorization(
    meta: AuthMetadata, client_id: str, *, client: httpx.Client, scopes: Optional[list[str]] = None
) -> dict[str, Any]:
    """RFC 8628 device-authorization request → the device/user codes + polling parameters."""
    if not meta.device_authorization_endpoint:
        raise DeviceAuthError(
            "the authorization server advertises no device_authorization_endpoint — "
            "device-code login is unavailable",
            reason="device_flow_unsupported",
        )
    data = {"client_id": client_id}
    if scopes:
        data["scope"] = " ".join(scopes)
    try:
        resp = client.post(meta.device_authorization_endpoint, data=data)
        resp.raise_for_status()
        body = resp.json()
    except (httpx.HTTPError, ValueError) as exc:  # ValueError: a non-JSON body
        raise DeviceAuthError(f"device-authorization request failed: {exc}") from exc
    if not body.get("device_code") or not body.get("user_code"):
        raise DeviceAuthError("device-authorization response missing device_code/user_code")
    return body


def poll_for_token(
    meta: AuthMetadata,
    client_id: str,
    device_code: str,
    *,
    client: httpx.Client,
    interval: int = 5,
    expires_in: int = 600,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """RFC 8628 polling: hit the token endpoint until success, denial, or expiry.

    ``sleep`` / ``monotonic`` are injectable so tests drive the loop without real waits.
    """
    deadline = monotonic() + expires_in
    wait = max(1, interval)
    while True:
        resp = client.post(
            meta.token_endpoint,
            data={
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "device_code": device_code,
                "client_id": client_id,
            },
        )
        try:
            body: dict[str, Any] = resp.json() if resp.content else {}
        except ValueError:
            # A non-JSON body (e.g. a proxy's HTML error page) → fall through to the status-code
            # error below rather than crashing out of the exit-code contract.
            body = {}
        if resp.status_code == 200 and body.get("access_token"):
            return body

        error = body.get("error", "")
        if error == "authorization_pending":
            pass
        elif error == "slow_down":
            wait += 5
        elif error == "access_denied":
            raise DeviceAuthError("the user denied the authorization request", reason="access_denied")
        elif error == "expired_token":
            raise DeviceAuthError("the device code expired before approval", reason="expired_token")
        else:
            raise DeviceAuthError(
                f"token endpoint returned an unexpected error: {error or resp.status_code}"
            )

        if monotonic() >= deadline:
            raise DeviceAuthError("timed out waiting for device-code approval", reason="timeout")
        sleep(wait)
