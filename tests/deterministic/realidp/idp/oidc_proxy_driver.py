"""Drives a real authorization-code flow against ``KX_MCP_AUTH=oidc_proxy`` (FastMCP's
``OIDCProxy``), the way a browser-based MCP client would — DCR at the container, ``/authorize``,
the container's own consent page, a real Keycloak login form, the container's callback, then
``/token``.

Why this exists at all: the existing realidp harness mints tokens by password grant
(``idp/providers/keycloak.py``), which never touches the proxy. In ``oidc_proxy`` mode ``/token``
hands the client a bearer the *container* minted; the Keycloak token is kept server-side and is
what ``JWTVerifier`` actually checks per request (``fastmcp.server.auth.oauth_proxy.proxy``,
``load_access_token`` → ``_get_verification_token`` → the verifier). A password-grant token can
never exercise that path, so this module is the only way to reach it from a test.

One ``requests.Session`` for the whole flow — cookies must survive from the consent page through
to the callback (a double-submit CSRF cookie plus a binding cookie the callback checks). Every
hop disables ``allow_redirects`` and is followed by hand, so a failure is attributable to a
specific hop instead of surfacing as an opaque final-state mismatch.

Kept out of ``idp/helpers.py`` (the assertion module other lanes import) — this is flow machinery,
not an assertion helper.
"""

from __future__ import annotations

import base64
import hashlib
import html
import re
import secrets
from dataclasses import dataclass
from typing import Any

import jwt
import requests

# --- scraping patterns ---------------------------------------------------------------------------
# Keycloak is pinned to 24.0 in this lane's docker-compose.yaml, so the rendered templates are
# fixed. The form tag is matched first, then its `action` pulled out of that match — Keycloak's
# attribute order (id, class, onsubmit, action, method) makes one ordered "action=..." regex over
# the whole page fragile (other forms/links on the page can match first).

_LOGIN_FORM_TAG_RE = re.compile(r"<form\b[^>]*\bid=[\"']kc-form-login[\"'][^>]*>", re.I | re.S)
_ACTION_RE = re.compile(r"\baction=[\"']([^\"']+)[\"']", re.I)
_CSRF_RE = re.compile(r"name=[\"']csrf_token[\"']\s+value=[\"']([^\"']+)[\"']", re.I)
_TXN_RE = re.compile(r"name=[\"']txn_id[\"']\s+value=[\"']([^\"']+)[\"']", re.I)
_KC_ERROR_RE = re.compile(r"id=[\"'](?:input-error|kc-feedback-text)[\"'][^>]*>\s*([^<]+)", re.I)
_TITLE_RE = re.compile(r"<title[^>]*>\s*([^<]+)", re.I)


@dataclass(frozen=True)
class ProxyFlowResult:
    """The outcome of a completed authorization-code flow against the container."""

    access_token: str
    token_response: dict[str, Any]
    client_id: str
    header: dict[str, Any]
    claims: dict[str, Any]


def run_authorization_code_flow(
    *,
    base_url: str,
    username: str,
    password: str,
    client_redirect_uri: str = "http://localhost:9876/callback",
    timeout: float = 15.0,
) -> ProxyFlowResult:
    """Drive the full flow and return the container-minted bearer.

    ``base_url`` MUST be the exact string the container was spawned with as
    ``KX_MCP_AUTH_RESOURCE_URL`` (host spelling included — ``127.0.0.1`` vs ``localhost`` are
    different cookie domains and different registered redirect URIs).
    """
    session = requests.Session()

    metadata = _discover(session, base_url, timeout)
    client_id = _register_client(session, metadata["registration_endpoint"], client_redirect_uri, timeout)
    verifier, challenge = _pkce_pair()
    consent_url = _authorize(
        session, metadata["authorization_endpoint"], client_id, client_redirect_uri, challenge, timeout
    )
    upstream_authorize_url = _approve_consent(session, consent_url, timeout)
    callback_url = _keycloak_login(session, upstream_authorize_url, username, password, base_url, timeout)
    client_redirect = _follow_callback(session, callback_url, base_url, timeout)
    code = _extract_code(client_redirect, client_redirect_uri, "callback -> client redirect")
    token_response = _exchange(
        session, metadata["token_endpoint"], code, client_redirect_uri, client_id, verifier, timeout
    )

    access_token = token_response["access_token"]
    header = jwt.get_unverified_header(access_token)
    claims = jwt.decode(access_token, options={"verify_signature": False})
    return ProxyFlowResult(
        access_token=access_token,
        token_response=token_response,
        client_id=client_id,
        header=header,
        claims=claims,
    )


# --- hops ------------------------------------------------------------------------------------


def _discover(session: requests.Session, base_url: str, timeout: float) -> dict[str, Any]:
    url = f"{base_url.rstrip('/')}/.well-known/oauth-authorization-server"
    resp = session.get(url, timeout=timeout)
    _expect_status(resp, "discovery", 200)
    return resp.json()


def _register_client(
    session: requests.Session, registration_endpoint: str, client_redirect_uri: str, timeout: float
) -> str:
    resp = session.post(
        registration_endpoint,
        json={
            "client_name": "kx-realidp-oidc-proxy-driver",
            "redirect_uris": [client_redirect_uri],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
        },
        timeout=timeout,
    )
    _expect_status(resp, "DCR", 200, 201)
    client_id = resp.json().get("client_id")
    assert client_id, f"DCR: response carried no client_id — body: {resp.text[:500]!r}"
    return client_id


def _pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


def _authorize(
    session: requests.Session,
    authorization_endpoint: str,
    client_id: str,
    client_redirect_uri: str,
    challenge: str,
    timeout: float,
) -> str:
    resp = session.get(
        authorization_endpoint,
        params={
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": client_redirect_uri,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": "realidp-oidc-proxy",
        },
        timeout=timeout,
        allow_redirects=False,
    )
    return _expect_redirect(resp, "authorize -> consent")


def _approve_consent(session: requests.Session, consent_url: str, timeout: float) -> str:
    get_resp = session.get(consent_url, timeout=timeout)
    _expect_status(get_resp, "consent GET", 200)
    body = get_resp.text
    txn_id = _scrape(_TXN_RE, body, "consent GET", "txn_id")
    csrf_token = _scrape(_CSRF_RE, body, "consent GET", "csrf_token")

    post_resp = session.post(
        consent_url,
        data={"txn_id": txn_id, "csrf_token": csrf_token, "action": "approve", "submit": "true"},
        timeout=timeout,
        allow_redirects=False,
    )
    if post_resp.status_code == 403:
        raise AssertionError(
            "consent POST: expected a redirect to Keycloak, got 403 — the double-submit "
            "__MCP_CONSENT_STATE cookie was not sent back. Is this using ONE requests.Session for "
            "the whole flow, and is base_url the same host spelling (127.0.0.1 vs localhost) as "
            "KX_MCP_AUTH_RESOURCE_URL?"
        )
    return _expect_redirect(post_resp, "consent POST -> upstream Keycloak authorize")


def _keycloak_login(
    session: requests.Session,
    upstream_authorize_url: str,
    username: str,
    password: str,
    base_url: str,
    timeout: float,
) -> str:
    get_resp = session.get(upstream_authorize_url, timeout=timeout)
    _expect_status(get_resp, "Keycloak login page GET", 200)
    body = get_resp.text
    form_match = _LOGIN_FORM_TAG_RE.search(body)
    assert form_match, (
        "Keycloak login page GET: could not find the kc-form-login form.\n"
        f"  page title: {_first(_TITLE_RE, body)!r}\n"
        f"  page message: {_first(_KC_ERROR_RE, body)!r}\n"
        f"  body[:500]: {body[:500]!r}"
    )
    action_match = _ACTION_RE.search(form_match.group(0))
    assert action_match, f"Keycloak login page GET: found the login form but no action= in it: {form_match.group(0)!r}"
    login_action = html.unescape(action_match.group(1))

    post_resp = session.post(
        login_action,
        data={"username": username, "password": password, "credentialId": ""},
        timeout=timeout,
        allow_redirects=False,
    )
    if post_resp.status_code == 200:
        raise AssertionError(
            "Keycloak login POST: expected a redirect back to the container's /auth/callback, got "
            "200 (the login page re-rendered instead of completing).\n"
            f"  page message: {_first(_KC_ERROR_RE, post_resp.text)!r}\n"
            "  Common causes: wrong username/password, or Keycloak's own session cookie "
            "(AUTH_SESSION_ID / KC_RESTART) was not carried from the GET to this POST."
        )
    location = _expect_redirect(post_resp, "Keycloak login POST -> container /auth/callback")
    if not location.startswith(base_url.rstrip("/")):
        raise AssertionError(
            f"Keycloak login POST: redirected to {location!r}, which is not under base_url "
            f"{base_url!r}. Is the kx-mcp-proxy client's registered redirect URI "
            f"{base_url.rstrip('/')}/auth/callback exactly, and not stale from a different port?"
        )
    return location


def _follow_callback(session: requests.Session, callback_url: str, base_url: str, timeout: float) -> str:
    resp = session.get(callback_url, timeout=timeout, allow_redirects=False)
    if resp.status_code == 403:
        raise AssertionError(
            "container /auth/callback: got 403 — the __MCP_CONSENT_BINDING cookie set by the "
            "consent POST was not sent back. Confirm the whole flow used one requests.Session."
        )
    if resp.status_code == 500:
        raise AssertionError(
            "container /auth/callback: got 500 — the container's SERVER-SIDE exchange with "
            "Keycloak failed (a wrong KX_MCP_AUTH_CLIENT_SECRET for kx-mcp-proxy, or a redirect_uri "
            f"not registered at Keycloak as {base_url.rstrip('/')}/auth/callback exactly).\n"
            f"  body[:500]: {resp.text[:500]!r}"
        )
    return _expect_redirect(resp, "container /auth/callback -> client redirect_uri")


def _extract_code(redirect_location: str, client_redirect_uri: str, hop: str) -> str:
    from urllib.parse import parse_qs, urlsplit

    parsed = urlsplit(redirect_location)
    if not redirect_location.startswith(client_redirect_uri):
        raise AssertionError(
            f"{hop}: expected a redirect starting with {client_redirect_uri!r}, got "
            f"{redirect_location!r}"
        )
    params = parse_qs(parsed.query)
    error = params.get("error")
    if error:
        raise AssertionError(f"{hop}: the redirect carried an OAuth error: {params}")
    code = params.get("code")
    assert code, f"{hop}: no 'code' param in redirect {redirect_location!r}"
    return code[0]


def _exchange(
    session: requests.Session,
    token_endpoint: str,
    code: str,
    client_redirect_uri: str,
    client_id: str,
    verifier: str,
    timeout: float,
) -> dict[str, Any]:
    resp = session.post(
        token_endpoint,
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": client_redirect_uri,
            "client_id": client_id,
            "code_verifier": verifier,
        },
        timeout=timeout,
    )
    _expect_status(resp, "/token", 200)
    return resp.json()


# --- assertion primitives ---------------------------------------------------------------------


def _first(pattern: re.Pattern, text: str) -> str | None:
    m = pattern.search(text)
    return m.group(1).strip() if m else None


def _expect_status(resp: requests.Response, hop: str, *expected: int) -> None:
    if resp.status_code not in expected:
        raise AssertionError(
            f"{hop}: expected status {expected}, got {resp.status_code}.\n"
            f"  url: {resp.request.method} {resp.request.url}\n"
            f"  body[:500]: {resp.text[:500]!r}"
        )


def _expect_redirect(resp: requests.Response, hop: str, *, location_startswith: str | None = None) -> str:
    if resp.status_code not in (301, 302, 303, 307, 308):
        raise AssertionError(
            f"{hop}: expected a redirect, got {resp.status_code}.\n"
            f"  url: {resp.request.method} {resp.request.url}\n"
            f"  body[:500]: {resp.text[:500]!r}"
        )
    location = resp.headers.get("Location")
    assert location, f"{hop}: redirect had no Location header"
    if location_startswith and not location.startswith(location_startswith):
        raise AssertionError(f"{hop}: redirected to {location!r}, expected it to start with {location_startswith!r}")
    return location


def _scrape(pattern: re.Pattern, text: str, hop: str, what: str) -> str:
    m = pattern.search(text)
    assert m, (
        f"{hop}: could not find {what} in the response.\n"
        f"  page title: {_first(_TITLE_RE, text)!r}\n"
        f"  page message: {_first(_KC_ERROR_RE, text)!r}\n"
        f"  body[:500]: {text[:500]!r}"
    )
    return m.group(1)
