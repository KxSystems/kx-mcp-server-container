#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "requests",
#     "python-keycloak",
# ]
# ///
"""
Keycloak provisioner for the kdbai OAuth ACL lane.

Lifted from the similarity-search kdbai-db-tests harness and adapted for this repo.
Provisions: realms (tenants), the public ``kdbai-service`` client with
groups/tenant/audience protocol mappers, groups, users, and group memberships.
Idempotent — safe to re-run.

Run:
    uv run tests/deterministic/realidp/setup/keycloak/keycloak_setup.py keycloak_config.json
    uv run ... keycloak_config.json --delete-first   # wipe + re-provision
    uv run ... keycloak_config.json --delete-only    # wipe tenants only
    uv run ... keycloak_config.json --cleanup-master # remove artefacts from master realm

Environment variables:
    KC_BASE            — Keycloak base URL (default: http://localhost:8080)
    KC_ADMIN           — admin username (default: admin)
    KC_ADMIN_PASSWORD  — admin password (default: admin)
"""

import argparse
import json
import sys
from typing import Any, Dict, List, Optional, Set

import requests
from keycloak import KeycloakAdmin


# ---------------- Logging (buffered output) ----------------

class LogBuffer:
    def __init__(self) -> None:
        self.lines: List[str] = []

    def log(self, msg: str) -> None:
        self.lines.append(msg)

    def dump(self) -> None:
        print("\n".join(self.lines))


# ---------------- Config / Auth ----------------

def norm_base_url(url: str) -> str:
    url = url.strip()
    return url if url.endswith("/") else url + "/"


def load_config(path: str) -> Dict[str, Any]:
    import os
    with open(path, "r", encoding="utf-8") as f:
        cfg = json.load(f)

    kc = cfg.get("keycloak", {})
    for k in ["base_url", "admin_user", "admin_password", "admin_realm", "admin_client_id"]:
        if k in kc and isinstance(kc[k], str):
            kc[k] = kc[k].strip()

    if "base_url" in kc:
        kc["base_url"] = norm_base_url(kc["base_url"])

    # Allow env-var overrides (KC_BASE / KC_ADMIN / KC_ADMIN_PASSWORD)
    if os.environ.get("KC_BASE"):
        kc["base_url"] = norm_base_url(os.environ["KC_BASE"])
    if os.environ.get("KC_ADMIN"):
        kc["admin_user"] = os.environ["KC_ADMIN"]
    if os.environ.get("KC_ADMIN_PASSWORD"):
        kc["admin_password"] = os.environ["KC_ADMIN_PASSWORD"]

    return cfg


def get_token_response(cfg: Dict[str, Any]) -> dict:
    kc = cfg["keycloak"]
    base_url = kc["base_url"]
    realm = kc.get("admin_realm", "master")
    client_id = kc.get("admin_client_id", "admin-cli")

    token_url = f"{base_url}realms/{realm}/protocol/openid-connect/token"
    data = {
        "client_id": client_id,
        "grant_type": "password",
        "username": kc["admin_user"],
        "password": kc["admin_password"],
    }
    r = requests.post(token_url, data=data, timeout=20)
    if r.status_code != 200:
        raise RuntimeError(f"Token request failed: {r.status_code} {r.text[:500]}")
    return r.json()  # includes expires_in (required by old python-keycloak)


def admin_for_realm(cfg: Dict[str, Any], realm: str) -> KeycloakAdmin:
    kc = cfg["keycloak"]
    token_resp = get_token_response(cfg)
    return KeycloakAdmin(
        server_url=kc["base_url"],
        realm_name=realm,
        token=token_resp,          # dict, not string
        verify=kc.get("verify", False),
    )


# ---------------- Raw REST helpers (version-proof) ----------------

def raw_get_json(admin: KeycloakAdmin, path: str) -> Any:
    resp = admin.connection.raw_get(path)
    return resp.json()


def raw_post_json(admin: KeycloakAdmin, path: str, payload: dict) -> None:
    resp = admin.connection.raw_post(path, data=json.dumps(payload))
    if hasattr(resp, "status_code") and resp.status_code >= 400:
        text = getattr(resp, "text", "")
        raise RuntimeError(f"POST {path} failed: {resp.status_code} {text[:500]}")


def raw_put_json(admin: KeycloakAdmin, path: str, payload: dict) -> None:
    resp = admin.connection.raw_put(path, data=json.dumps(payload))
    if hasattr(resp, "status_code") and resp.status_code >= 400:
        text = getattr(resp, "text", "")
        raise RuntimeError(f"PUT {path} failed: {resp.status_code} {text[:500]}")


# ---------------- Realm operations (master-context) ----------------

def list_realms(cfg: Dict[str, Any]) -> List[dict]:
    madmin = admin_for_realm(cfg, "master")
    return madmin.get_realms()


def realm_exists(cfg: Dict[str, Any], realm_name: str) -> bool:
    return any(r.get("realm") == realm_name for r in list_realms(cfg))


def create_realm_if_missing(cfg: Dict[str, Any], realm_name: str, logs: LogBuffer) -> None:
    if realm_exists(cfg, realm_name):
        logs.log(f"[create] Realm already exists: {realm_name}")
        return
    madmin = admin_for_realm(cfg, "master")
    madmin.create_realm({"realm": realm_name, "enabled": True})
    logs.log(f"[create] Realm created: {realm_name}")


def delete_realm_if_exists(cfg: Dict[str, Any], realm_name: str, logs: LogBuffer) -> None:
    if realm_name == "master":
        logs.log("[delete] Refusing to delete master realm")
        return
    if not realm_exists(cfg, realm_name):
        logs.log(f"[delete] Realm not found (skip): {realm_name}")
        return
    madmin = admin_for_realm(cfg, "master")
    madmin.delete_realm(realm_name)
    logs.log(f"[delete] Realm deleted: {realm_name}")


# ---------------- Token settings per realm ----------------

def set_realm_access_token_lifespan_seconds(tadmin: KeycloakAdmin, realm: str, seconds: int, logs: LogBuffer) -> None:
    path = f"admin/realms/{realm}"
    rep = raw_get_json(tadmin, path)
    rep["accessTokenLifespan"] = int(seconds)
    raw_put_json(tadmin, path, rep)
    logs.log(f"[config] Realm {realm} accessTokenLifespan set to {seconds}s")


# ---------------- Client + protocol mappers ----------------

def get_client_uuid(tadmin: KeycloakAdmin, realm: str, client_id: str) -> str:
    path = f"admin/realms/{realm}/clients?clientId={client_id}"
    clients = raw_get_json(tadmin, path) or []
    return clients[0]["id"] if clients else ""


def ensure_client(tadmin: KeycloakAdmin, realm: str, client_cfg: Dict[str, Any], logs: LogBuffer) -> str:
    """Create or skip the client. Returns the client's internal UUID."""
    client_id = client_cfg["client_id"]
    public_client = bool(client_cfg.get("public_client", False))

    rep = {
        "clientId": client_id,
        "enabled": True,
        "protocol": "openid-connect",
        "publicClient": public_client,
        "directAccessGrantsEnabled": True,   # needed for password-grant token minting in tests
        "standardFlowEnabled": True,
        # off unless the config opts in — a machine/workload identity for the client_credentials
        # grant (service_account outbound strategy), distinct from the human password-grant clients.
        "serviceAccountsEnabled": bool(client_cfg.get("service_accounts_enabled", False)),
        "attributes": {
            "oauth2.device.authorization.grant.enabled": "true",  # kx auth login device-code flow
        },
    }
    if not public_client:
        rep["secret"] = client_cfg.get("secret")

    cuuid = get_client_uuid(tadmin, realm, client_id)
    if cuuid:
        logs.log(f"[create] Client already exists in {realm}: {client_id}")
        return cuuid

    tadmin.create_client(rep)
    logs.log(f"[create] Client created in {realm}: {client_id}")

    cuuid = get_client_uuid(tadmin, realm, client_id)
    if not cuuid:
        raise RuntimeError(f"Could not resolve client UUID for clientId={client_id} in realm={realm}")
    return cuuid


def get_service_account_user_id(tadmin: KeycloakAdmin, realm: str, client_uuid: str) -> str:
    """The user Keycloak auto-creates for a `serviceAccountsEnabled` client (the machine identity
    behind its client_credentials grant) — needed to add it to a group, since the `groups` claim
    comes from a group-membership mapper, not the client-level mappers."""
    path = f"admin/realms/{realm}/clients/{client_uuid}/service-account-user"
    rep = raw_get_json(tadmin, path)
    if not rep or "id" not in rep:
        raise RuntimeError(f"Could not resolve service-account user for client_uuid={client_uuid} in realm={realm}")
    return rep["id"]


def ensure_extra_audience_mapper(
    tadmin: KeycloakAdmin, realm: str, client_uuid: str, target_client_id: str, logs: LogBuffer
) -> None:
    """Add an audience mapper pointing at a *different* client's client_id.

    ``ensure_groups_tenant_aud_mappers`` gives every client an audience mapper hardcoded to its
    *own* client_id — fine for a client whose own client_id is what the backend validates
    (kdbai-service, since kdbai-db's OAUTH_CLIENT_ID=kdbai-service matches it exactly). A second,
    differently-named client (e.g. a service-account worker) would otherwise mint tokens with
    aud=[its own client_id], which kdbai-db rejects. This adds a second mapper so the token's
    `aud` also carries ``target_client_id`` — the audience the backend actually trusts.
    """
    mapper = {
        "name": f"audience-override-{target_client_id}-mapper",
        "protocol": "openid-connect",
        "protocolMapper": "oidc-audience-mapper",
        "config": {
            "included.client.audience": target_client_id,
            "access.token.claim": "true",
            "id.token.claim": "false",
        },
    }
    ensure_protocol_mapper(tadmin, realm, client_uuid, mapper, logs)


def list_client_mappers(tadmin: KeycloakAdmin, realm: str, client_uuid: str) -> Dict[str, dict]:
    path = f"admin/realms/{realm}/clients/{client_uuid}/protocol-mappers/models"
    mappers = raw_get_json(tadmin, path) or []
    return {m.get("name"): m for m in mappers}


def ensure_protocol_mapper(tadmin: KeycloakAdmin, realm: str, client_uuid: str, mapper: dict, logs: LogBuffer) -> None:
    existing = list_client_mappers(tadmin, realm, client_uuid)
    name = mapper.get("name")
    if name in existing:
        logs.log(f"[create] Mapper already exists: {name}")
        return
    path = f"admin/realms/{realm}/clients/{client_uuid}/protocol-mappers/models"
    raw_post_json(tadmin, path, mapper)
    logs.log(f"[create] Added mapper: {name}")


def ensure_groups_tenant_aud_mappers(tadmin: KeycloakAdmin, realm: str, client_uuid: str, client_id: str, logs: LogBuffer) -> None:
    """Add the three protocol mappers kdbai-db depends on: groups membership, tenant (hardcoded), audience."""
    groups_mapper = {
        "name": "groups-mapper",
        "protocol": "openid-connect",
        "protocolMapper": "oidc-group-membership-mapper",
        "config": {
            "full.path": "false",
            "claim.name": "groups",
            "jsonType.label": "String",
            "access.token.claim": "true",
            "id.token.claim": "true",
            "userinfo.token.claim": "true",
        },
    }
    ensure_protocol_mapper(tadmin, realm, client_uuid, groups_mapper, logs)

    tenant_mapper = {
        "name": "tenant-mapper",
        "protocol": "openid-connect",
        "protocolMapper": "oidc-hardcoded-claim-mapper",
        "config": {
            "claim.name": "tenant",
            "claim.value": realm,       # realm name is the tenant identifier
            "jsonType.label": "String",
            "access.token.claim": "true",
            "id.token.claim": "true",
            "userinfo.token.claim": "true",
        },
    }
    ensure_protocol_mapper(tadmin, realm, client_uuid, tenant_mapper, logs)

    audience_mapper = {
        "name": "audience-mapper",
        "protocol": "openid-connect",
        "protocolMapper": "oidc-audience-mapper",
        "config": {
            "included.client.audience": client_id,
            "access.token.claim": "true",
            "id.token.claim": "false",
        },
    }
    ensure_protocol_mapper(tadmin, realm, client_uuid, audience_mapper, logs)


# ---------------- DCR policies (native discovery support) ----------------

def configure_dcr_policies(tadmin: KeycloakAdmin, realm: str, logs: LogBuffer, extra_scopes: Optional[List[str]] = None) -> None:
    """Configure anonymous DCR policies so Claude Code can self-register via native discovery.

    Three anonymous policies need updating:

    1. Trusted Hosts — by default blocks all DCR from unlisted hosts.  We disable the
       reverse-DNS host check (unreliable for localhost) and keep client-URI validation enabled
       (device/auth-code clients send no redirect URIs so it passes vacuously).  Keycloak
       requires at least one of the two checks to stay on; we keep client-uris-must-match.

    2. Allowed Client Scopes — blocks DCR requests that include scopes outside the allowed list.
       We add the standard OIDC scopes Claude Code requests.

    Components are looked up by name (not UUID) because Keycloak regenerates UUIDs on restart.
    """
    path = (
        f"admin/realms/{realm}/components"
        "?type=org.keycloak.services.clientregistration.policy.ClientRegistrationPolicy"
    )
    components = raw_get_json(tadmin, path) or []

    for comp in components:
        if comp.get("subType") != "anonymous":
            continue

        name = comp.get("name", "")
        comp_id = comp["id"]

        if name == "Trusted Hosts":
            updated = {
                **comp,
                "config": {
                    "host-sending-registration-request-must-match": ["false"],
                    "trusted-hosts": ["localhost", "127.0.0.1"],
                    "client-uris-must-match": ["true"],   # must keep one check enabled
                },
            }
            raw_put_json(tadmin, f"admin/realms/{realm}/components/{comp_id}", updated)
            logs.log(f"[dcr] {realm}: Trusted Hosts policy — host check disabled, localhost trusted")

        elif name == "Allowed Client Scopes":
            allowed = [
                "openid", "profile", "email", "roles",
                "web-origins", "acr", "offline_access",
            ] + (extra_scopes or [])
            updated = {
                **comp,
                "config": {
                    "allow-default-scopes": ["true"],
                    "allowed-client-scopes": allowed,
                },
            }
            raw_put_json(tadmin, f"admin/realms/{realm}/components/{comp_id}", updated)
            logs.log(f"[dcr] {realm}: Allowed Client Scopes policy — scopes: {allowed}")


def ensure_kdbai_audience_default_scope(
    tadmin: KeycloakAdmin, realm: str, client_id: str, logs: LogBuffer
) -> str:
    """Add a realm default scope that hardcodes client_id as audience in every token.

    When Claude Code does DCR it receives a new generated client ID.  The token Keycloak issues
    for that client has aud=[<generated-id>] — which the container's KX_MCP_AUTH_AUDIENCE check
    rejects.  The fix: a default client scope with a hardcoded audience mapper so every client
    in this realm (including DCR-registered ones) automatically carries client_id in aud.
    """
    scope_name = "kdbai-resource-audience"

    # --- ensure the scope exists ---
    existing_scopes = raw_get_json(tadmin, f"admin/realms/{realm}/client-scopes") or []
    scope_by_name: Dict[str, str] = {s["name"]: s["id"] for s in existing_scopes}

    if scope_name not in scope_by_name:
        raw_post_json(tadmin, f"admin/realms/{realm}/client-scopes", {
            "name": scope_name,
            "protocol": "openid-connect",
            "description": (
                f"Hardcodes {client_id} as audience — ensures DCR-registered clients "
                "(e.g. Claude Code native discovery) receive the right aud claim."
            ),
        })
        # re-fetch to pick up the generated ID
        scopes = raw_get_json(tadmin, f"admin/realms/{realm}/client-scopes") or []
        scope_by_name = {s["name"]: s["id"] for s in scopes}
        logs.log(f"[dcr] {realm}: Created client scope '{scope_name}'")
    else:
        logs.log(f"[dcr] {realm}: Client scope '{scope_name}' already exists")

    scope_id = scope_by_name[scope_name]

    # --- ensure the audience mapper is on the scope ---
    mappers_path = f"admin/realms/{realm}/client-scopes/{scope_id}/protocol-mappers/models"
    mappers = raw_get_json(tadmin, mappers_path) or []
    mapper_names = {m["name"] for m in mappers}

    mapper_name = f"{client_id}-audience-mapper"
    if mapper_name not in mapper_names:
        raw_post_json(tadmin, mappers_path, {
            "name": mapper_name,
            "protocol": "openid-connect",
            "protocolMapper": "oidc-audience-mapper",
            "config": {
                "included.custom.audience": client_id,   # hardcoded string, not a client ref
                "access.token.claim": "true",
                "id.token.claim": "false",
            },
        })
        logs.log(f"[dcr] {realm}: Added audience mapper '{mapper_name}' to '{scope_name}'")
    else:
        logs.log(f"[dcr] {realm}: Audience mapper '{mapper_name}' already exists")

    # groups and tenant mappers must also live on this scope so DCR-registered clients
    # (e.g. Claude Code native discovery) receive the claims kdbai-db requires for ACL.
    # Per-client mappers on kdbai-service are NOT inherited by DCR clients.
    if "groups-mapper" not in mapper_names:
        raw_post_json(tadmin, mappers_path, {
            "name": "groups-mapper",
            "protocol": "openid-connect",
            "protocolMapper": "oidc-group-membership-mapper",
            "config": {
                "full.path": "false",
                "claim.name": "groups",
                "jsonType.label": "String",
                "access.token.claim": "true",
                "id.token.claim": "true",
                "userinfo.token.claim": "true",
            },
        })
        logs.log(f"[dcr] {realm}: Added groups-mapper to '{scope_name}'")
    else:
        logs.log(f"[dcr] {realm}: groups-mapper already exists on '{scope_name}'")

    if "tenant-mapper" not in mapper_names:
        raw_post_json(tadmin, mappers_path, {
            "name": "tenant-mapper",
            "protocol": "openid-connect",
            "protocolMapper": "oidc-hardcoded-claim-mapper",
            "config": {
                "claim.name": "tenant",
                "claim.value": realm,
                "jsonType.label": "String",
                "access.token.claim": "true",
                "id.token.claim": "true",
                "userinfo.token.claim": "true",
            },
        })
        logs.log(f"[dcr] {realm}: Added tenant-mapper to '{scope_name}'")
    else:
        logs.log(f"[dcr] {realm}: tenant-mapper already exists on '{scope_name}'")

    # --- add scope as a realm default (applies to all clients, including DCR-registered) ---
    default_scopes = raw_get_json(tadmin, f"admin/realms/{realm}/default-default-client-scopes") or []
    default_scope_ids = {s["id"] for s in default_scopes}

    if scope_id not in default_scope_ids:
        raw_put_json(tadmin, f"admin/realms/{realm}/default-default-client-scopes/{scope_id}", {})
        logs.log(f"[dcr] {realm}: '{scope_name}' added as realm default client scope")
    else:
        logs.log(f"[dcr] {realm}: '{scope_name}' already a realm default client scope")

    return scope_name


# ---------------- Groups / Users ----------------

def ensure_group(tadmin: KeycloakAdmin, group_name: str, logs: LogBuffer) -> str:
    try:
        tadmin.create_group({"name": group_name})
        logs.log(f"[create] Group created: {group_name}")
    except Exception:
        logs.log(f"[create] Group already exists: {group_name}")
    grp = tadmin.get_group_by_path(f"/{group_name}")
    return grp["id"]


def ensure_user(tadmin: KeycloakAdmin, username: str, password: str, logs: LogBuffer) -> str:
    uid = tadmin.get_user_id(username)
    if uid:
        logs.log(f"[create] User already exists: {username}")
        return uid

    uid = tadmin.create_user(
        {
            "username": username,
            "enabled": True,
            "email": f"{username}@example.com",
            "emailVerified": True,
            "firstName": username,
            "lastName": username,
            "requiredActions": [],
            "credentials": [{"type": "password", "value": password, "temporary": False}],
        }
    )
    logs.log(f"[create] User created: {username}")
    return uid


def add_user_to_group(tadmin: KeycloakAdmin, user_id: str, group_id: str, group_name: str, logs: LogBuffer) -> None:
    try:
        tadmin.group_user_add(user_id, group_id)
        logs.log(f"[assign] Added user to group: {group_name}")
    except Exception:
        logs.log(f"[assign] User already in group (or could not add): {group_name}")


# ---------------- Tenant setup ----------------

def setup_tenant(cfg: Dict[str, Any], tenant: str, tenant_cfg: Dict[str, Any], client_cfg: Dict[str, Any], logs: LogBuffer) -> None:
    create_realm_if_missing(cfg, tenant, logs)
    tadmin = admin_for_realm(cfg, tenant)

    token_settings = tenant_cfg.get("token_settings", {}) or {}
    lifespan = token_settings.get("access_token_lifespan_seconds")
    if lifespan is not None:
        set_realm_access_token_lifespan_seconds(tadmin, tenant, int(lifespan), logs)
    else:
        logs.log(f"[config] Realm {tenant} accessTokenLifespan not set (no token_settings in JSON)")

    client_uuid = ensure_client(tadmin, tenant, client_cfg, logs)
    ensure_groups_tenant_aud_mappers(tadmin, tenant, client_uuid, client_cfg["client_id"], logs)
    audience_scope_name = ensure_kdbai_audience_default_scope(tadmin, tenant, client_cfg["client_id"], logs)
    configure_dcr_policies(tadmin, tenant, logs, extra_scopes=[audience_scope_name])

    group_ids: Dict[str, str] = {}
    for g in tenant_cfg.get("groups", []):
        group_ids[g] = ensure_group(tadmin, g, logs)

    for u in tenant_cfg.get("users", []):
        uid = ensure_user(tadmin, u["username"], u["password"], logs)
        for g in u.get("groups", []):
            if g not in group_ids:
                group_ids[g] = ensure_group(tadmin, g, logs)
            add_user_to_group(tadmin, uid, group_ids[g], g, logs)

    # Optional per-tenant machine identity (client_credentials / service_account outbound strategy)
    # — a second client, distinct from the human password-grant client above, whose auto-created
    # service-account user gets the same group-membership treatment as a human user. No DCR-related
    # setup here (ensure_kdbai_audience_default_scope / configure_dcr_policies) — that machinery is
    # for dynamically-registered clients (e.g. Claude Code native discovery).
    service_client_cfg = tenant_cfg.get("service_client")
    if service_client_cfg:
        service_client_uuid = ensure_client(tadmin, tenant, service_client_cfg, logs)
        ensure_groups_tenant_aud_mappers(tadmin, tenant, service_client_uuid, service_client_cfg["client_id"], logs)

        # This client's own client_id != the audience kdbai-db validates (OAUTH_CLIENT_ID=kdbai-service),
        # unlike the human client above where client_id already matches — so it needs the extra mapper.
        audience_override = service_client_cfg.get("audience_override")
        if audience_override:
            ensure_extra_audience_mapper(tadmin, tenant, service_client_uuid, audience_override, logs)

        service_uid = get_service_account_user_id(tadmin, tenant, service_client_uuid)
        for g in service_client_cfg.get("service_account_groups", []):
            if g not in group_ids:
                group_ids[g] = ensure_group(tadmin, g, logs)
            add_user_to_group(tadmin, service_uid, group_ids[g], g, logs)

    users = tadmin.get_users()
    logs.log(f"[debug] realm={tenant} users_count={len(users)} sample={[x.get('username') for x in users[:10]]}")


# ---------------- Cleanup MASTER by config names ----------------

def cleanup_master_from_config(cfg: Dict[str, Any], logs: LogBuffer) -> None:
    madmin = admin_for_realm(cfg, "master")

    client_id = cfg.get("client", {}).get("client_id")
    if client_id:
        try:
            cid = madmin.get_client_id(client_id)
            madmin.delete_client(cid)
            logs.log(f"[cleanup-master] Client deleted: {client_id}")
        except Exception:
            logs.log(f"[cleanup-master] Client not found (skip): {client_id}")

    groups: Set[str] = set(cfg.get("groups", []))
    for _, tcfg in cfg.get("tenants", {}).items():
        groups.update(tcfg.get("groups", []))

    for g in sorted(groups):
        try:
            grp = madmin.get_group_by_path(f"/{g}")
            if grp:
                madmin.delete_group(grp["id"])
                logs.log(f"[cleanup-master] Group deleted: {g}")
            else:
                logs.log(f"[cleanup-master] Group not found (skip): {g}")
        except Exception:
            logs.log(f"[cleanup-master] Group not found (skip): {g}")

    usernames: Set[str] = set()
    for _, tcfg in cfg.get("tenants", {}).items():
        for u in tcfg.get("users", []):
            if "username" in u:
                usernames.add(u["username"])

    for u in sorted(usernames):
        if u.lower() == "admin":
            logs.log("[cleanup-master] Refusing to delete user 'admin'")
            continue
        try:
            uid = madmin.get_user_id(u)
            if uid:
                madmin.delete_user(uid)
                logs.log(f"[cleanup-master] User deleted: {u}")
            else:
                logs.log(f"[cleanup-master] User not found (skip): {u}")
        except Exception:
            logs.log(f"[cleanup-master] Could not delete user (skip): {u}")


# ---------------- Main ----------------

def main() -> int:
    ap = argparse.ArgumentParser(description="Provision Keycloak for the kdbai OAuth ACL lane")
    ap.add_argument("config", help="Path to JSON config file (default: keycloak_config.json)")
    ap.add_argument("--delete-first", action="store_true", help="Delete tenant realms before creating")
    ap.add_argument("--delete-only", action="store_true", help="Only delete tenant realms, then exit")
    ap.add_argument("--cleanup-master", action="store_true",
                    help="Delete users/groups/client in master realm matching config names")
    args = ap.parse_args()

    logs = LogBuffer()

    try:
        cfg = load_config(args.config)
        tenants = cfg.get("tenants", {})
        client_cfg = cfg["client"]

        if args.cleanup_master:
            logs.log("=== Cleaning MASTER realm (targeted by config names) ===")
            cleanup_master_from_config(cfg, logs)

        if args.delete_first or args.delete_only:
            logs.log("=== Deleting tenant realms from config ===")
            for tenant in tenants.keys():
                delete_realm_if_exists(cfg, tenant, logs)
            if args.delete_only:
                logs.dump()
                return 0

        logs.log("=== Setting up tenants ===")
        for tenant, tenant_cfg in tenants.items():
            logs.log(f"\n--- Tenant/Realm: {tenant} ---")
            setup_tenant(cfg, tenant, tenant_cfg, client_cfg, logs)

        logs.log("\nDone.")
        logs.dump()
        return 0

    except Exception as e:
        logs.log("\nERROR:")
        logs.log(str(e))
        logs.dump()
        return 2


if __name__ == "__main__":
    sys.exit(main())
