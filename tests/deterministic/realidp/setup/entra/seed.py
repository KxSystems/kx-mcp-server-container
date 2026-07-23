#!/usr/bin/env python3
"""Seed script: creates databases, tables, and ACL grants for the kdbai OAuth ACL
lane under **Microsoft Entra ID**.

The Entra sibling of ``setup/keycloak/seed.py``. Same databases, tables, and schema;
the only differences are Entra-shaped:

  - the root token is acquired via Entra ROPC (password grant) instead of Keycloak;
  - grants are keyed on the claims kdbai-db reads from an Entra token —
    ``tenant = tid = ENTRA_TENANT_ID`` and ``groups = <Object ID GUIDs>`` — rather
    than the Keycloak realm name + bare group names.

Run ONCE before the KA.* tests under Entra (idempotent — drops + recreates):

    source tests/deterministic/realidp/envs/.env.entra
    uv run python tests/deterministic/realidp/setup/entra/seed.py

Prerequisites:
    - Entra provisioned (entra_setup.py already run — alice/bob/root + groups exist)
    - kdbai-db running and reachable (setup/entra/docker-compose.yaml up)
    - KDB_LICENSE_B64 set (kdbai-db requires a valid kdb+ license)

Databases and grants created (identical shape to the Keycloak lane):
    db_read           — T1; quants-trader group → DB-level read grant
    db_read_isolated  — T1 + T2; quants-viewer group → table-level read on T1 only (KA.6)

Environment variables (from .env.entra):
    ENTRA_TENANT_ID            — directory (tenant) ID; also the kdbai-db tenant claim value
    ENTRA_CLIENT_ID            — kdbai-service-mcp-agent app (client) ID
    ENTRA_SCOPE                — ROPC scope (default api://<client-id>/access offline_access)
    ENTRA_DOMAIN               — tenant domain (root UPN = root@<domain>)
    ENTRA_PASSWORD_ROOT        — root's password (never committed)
    ENTRA_GROUP_QUANTS_TRADER  — Object ID of the quants-trader group
    ENTRA_GROUP_QUANTS_VIEWER  — Object ID of the quants-viewer group
    KDBAI_ENDPOINT             — KDB.AI REST endpoint (default: http://localhost:8081)
"""

import os
import sys
import tempfile

try:
    import requests
except ImportError:
    print("ERROR: requests is not installed — run: pip install requests", file=sys.stderr)
    sys.exit(1)

try:
    import kdbai_client as kdbai
except ImportError:
    print("ERROR: kdbai_client is not installed", file=sys.stderr)
    sys.exit(1)

try:
    import pandas as pd
    import yaml
except ImportError as e:
    print(f"ERROR: missing dependency — {e}", file=sys.stderr)
    sys.exit(1)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

KDBAI_ENDPOINT = os.environ.get("KDBAI_ENDPOINT", "http://localhost:8081")

_TOKEN_URL_TEMPLATE = "https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"


def _require(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        print(
            f"ERROR: {name} is not set — source tests/deterministic/realidp/envs/.env.entra first",
            file=sys.stderr,
        )
        sys.exit(1)
    return value


# Seed schema and index (matches the Keycloak lane + the similarity-search AU-R fixtures)
_SCHEMA = [
    {"name": "id", "type": "int32"},
    {"name": "embeddings", "type": "float32s"},
]
_INDEXES = [
    {"type": "flat", "name": "flat_idx", "column": "embeddings", "params": {"dims": 3}},
]
_DATA = pd.DataFrame([{"id": 1, "embeddings": [0.1, 0.2, 0.3]}])


# ---------------------------------------------------------------------------
# Token acquisition (Entra ROPC — root persona, manager-admin group → system_admin)
# ---------------------------------------------------------------------------

def _get_root_token() -> str:
    """ROPC-grant the root persona against the Entra tenant.

    root@<ENTRA_DOMAIN> is a member of the manager-admin group, whose Object ID is
    kdbai-db's ACL_SYSTEM_ADMIN_GROUP — so this token has system_admin and can add grants.
    """
    tenant_id = _require("ENTRA_TENANT_ID")
    client_id = _require("ENTRA_CLIENT_ID")
    domain = _require("ENTRA_DOMAIN")
    password = _require("ENTRA_PASSWORD_ROOT")
    scope = os.environ.get("ENTRA_SCOPE", f"api://{client_id}/access offline_access")

    url = _TOKEN_URL_TEMPLATE.format(tenant_id=tenant_id)
    resp = requests.post(
        url,
        data={
            "grant_type": "password",
            "client_id": client_id,
            "username": f"root@{domain}",
            "password": password,
            "scope": scope,
        },
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


# ---------------------------------------------------------------------------
# Session construction (REST mode, external_token bridge — no PyKX needed)
# ---------------------------------------------------------------------------

def _make_session(bearer: str) -> tuple:
    """Build a kdbai REST Session using the bearer as an external_token oauth config.

    Returns (session, config_file_path); caller must unlink config_file_path when done.
    """
    fd, config_file = tempfile.mkstemp(prefix="kdbai-seed-entra-", suffix=".yaml")
    try:
        with os.fdopen(fd, "w") as f:
            yaml.safe_dump({"oauth": {"grant_type": "external_token", "access_token": bearer}}, f)
        os.chmod(config_file, 0o600)
    except Exception:
        os.unlink(config_file)
        raise
    session = kdbai.Session(
        endpoint=KDBAI_ENDPOINT,
        mode="rest",
        oauth={"enabled": True, "config_file": config_file},
    )
    return session, config_file


# ---------------------------------------------------------------------------
# Database / table helpers
# ---------------------------------------------------------------------------

def _ensure_clean_database(session: kdbai.Session, name: str) -> None:
    """Drop the database if it exists from a previous run, then create fresh."""
    try:
        session.database(name).drop()
        print(f"  dropped existing database: {name}")
    except Exception:
        pass  # didn't exist, that's fine
    session.create_database(name)
    print(f"  created database: {name}")


def _create_table(db, name: str) -> None:
    """Create a table with the seed schema + flat index, insert one seed row."""
    t = db.create_table(table=name, schema=_SCHEMA, indexes=_INDEXES)
    t.insert(_DATA)
    print(f"    table {name}: created + seed row inserted")


# ---------------------------------------------------------------------------
# Grants
# ---------------------------------------------------------------------------

def _add_grant(session: kdbai.Session, payload: dict) -> None:
    session.admin.add_grants([payload])
    desc = (
        f"resource={payload['resource']} tenant={payload['tenant']} "
        f"groups={payload['groups']} db={payload.get('databaseName')} "
        f"table={payload.get('table', '*')} actions={payload['actions']}"
    )
    print(f"  grant: {desc}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    tenant_id = _require("ENTRA_TENANT_ID")
    trader_group = _require("ENTRA_GROUP_QUANTS_TRADER")
    viewer_group = _require("ENTRA_GROUP_QUANTS_VIEWER")

    print(f"Acquiring root token from Entra tenant {tenant_id} (ROPC) ...")
    token = _get_root_token()
    print("Root token acquired.")

    session, config_file = _make_session(token)
    try:
        print(f"\nConnected to KDB.AI at {KDBAI_ENDPOINT}")

        # ---- db_read (T1) -----------------------------------------------
        print("\n[db_read] Creating database + T1 ...")
        _ensure_clean_database(session, "db_read")
        _create_table(session.database("db_read"), "T1")

        # ---- db_read_isolated (T1, T2) -----------------------------------
        print("\n[db_read_isolated] Creating database + T1 + T2 ...")
        _ensure_clean_database(session, "db_read_isolated")
        db_iso = session.database("db_read_isolated")
        _create_table(db_iso, "T1")
        _create_table(db_iso, "T2")

        # ---- ACL grants --------------------------------------------------
        # Keyed on the claims kdbai-db reads from an Entra token: tenant = tid =
        # ENTRA_TENANT_ID, groups = group Object IDs. Same authorization shape as the
        # Keycloak lane (just GUID-valued) — so the provider-agnostic KA.* tests pass
        # unchanged: alice (trader+viewer OIDs) sees T1; bob (viewer OID only) sees [].
        print("\n[grants] Adding ACL grants ...")

        # quants-trader group → DB-level read on db_read (drives KA.2–KA.4).
        _add_grant(session, {
            "resource": "database",
            "tenant": tenant_id,
            "groups": [trader_group],
            "databaseName": "db_read",
            "actions": ["read"],
        })

        # quants-viewer group → table-level read on db_read_isolated/T1 only (KA.6 no-bleed)
        _add_grant(session, {
            "resource": "table",
            "tenant": tenant_id,
            "groups": [viewer_group],
            "databaseName": "db_read_isolated",
            "table": "T1",
            "actions": ["read"],
        })

        print("\nSeed complete.")
        print("\nVerify grants with:")
        print(f"  curl -s -H 'Authorization: Bearer <root-token>' {KDBAI_ENDPOINT}/api/v2/grants | python3 -m json.tool")

    finally:
        try:
            os.unlink(config_file)
        except OSError:
            pass


if __name__ == "__main__":
    main()
