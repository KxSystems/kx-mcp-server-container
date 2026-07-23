#!/usr/bin/env python3
"""Seed script: creates databases, tables, and ACL grants for the kdbai OAuth ACL lane.

Lifted from the similarity-search kdbai-db-tests harness (fixtures/databases.py +
fixtures/grants.py) and adapted as a standalone operator pre-step.

Run ONCE before the KA.* tests (idempotent — drops + recreates if the DB already exists):

    uv run python tests/deterministic/realidp/setup/keycloak/seed.py

Prerequisites:
    - Keycloak provisioned (keycloak_setup.py already run)
    - kdbai-db running and reachable
    - KDB_LICENSE_B64 set (kdbai-db requires a valid kdb+ license)

Databases and grants created:
    db_read           — T1 (3-dim flat index); quants/trader → DB-level read grant
    db_read_isolated  — T1 + T2; quants/viewer → table-level read on T1 only (no-bleed test KA.6)
    db_read           — quants/service → DB-level read grant, distinct from quants/trader (the
                        service_account outbound-strategy machine identity's own grant, not
                        inherited from any human persona)

Environment variables:
    KC_BASE          — Keycloak base URL (default: http://localhost:8080)
    KC_CLIENT_ID     — OAuth client id (default: kdbai-service)
    KDBAI_ENDPOINT   — KDB.AI REST endpoint (default: http://localhost:8081; the REST_PORT,
                       distinct from the qipc gateway on 8082 that KDBAI_DB_PORT/settings.py
                       default to — this script always connects in REST mode)
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

KC_BASE = os.environ.get("KC_BASE", "http://localhost:8080")
KC_CLIENT_ID = os.environ.get("KC_CLIENT_ID", "kdbai-service")
KDBAI_ENDPOINT = os.environ.get("KDBAI_ENDPOINT", "http://localhost:8081")

# Root persona — manager realm, admin group → system_admin in KDB.AI.
# Credentials are local-only test values committed alongside this script.
_ROOT_REALM = "manager"
_ROOT_USERNAME = "root"
_ROOT_PASSWORD = "root123"

# Seed schema and index (matches the similarity-search kdbai-db-tests AU-R fixtures)
_SCHEMA = [
    {"name": "id", "type": "int32"},
    {"name": "embeddings", "type": "float32s"},
]
_INDEXES = [
    {"type": "flat", "name": "flat_idx", "column": "embeddings", "params": {"dims": 3}},
]
_DATA = pd.DataFrame([{"id": 1, "embeddings": [0.1, 0.2, 0.3]}])


# ---------------------------------------------------------------------------
# Token acquisition
# ---------------------------------------------------------------------------

def _get_root_token() -> str:
    """Password-grant the root persona from the manager realm (no client_secret — public client)."""
    url = f"{KC_BASE}/realms/{_ROOT_REALM}/protocol/openid-connect/token"
    resp = requests.post(
        url,
        data={
            "grant_type": "password",
            "client_id": KC_CLIENT_ID,
            "username": _ROOT_USERNAME,
            "password": _ROOT_PASSWORD,
            "scope": "openid",
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
    fd, config_file = tempfile.mkstemp(prefix="kdbai-seed-", suffix=".yaml")
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
    print(f"Acquiring root token from {KC_BASE}/realms/{_ROOT_REALM} ...")
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
        print("\n[grants] Adding ACL grants ...")

        # quants/trader → DB-level read on db_read (drives KA.2–KA.4).
        # alice is [viewer, trader]; bob is [viewer] only — so alice sees T1, bob sees [].
        # This is the grant that produced the 2026-06-18 live differentiation result.
        _add_grant(session, {
            "resource": "database",
            "tenant": "quants",
            "groups": ["trader"],
            "databaseName": "db_read",
            "actions": ["read"],
        })

        # quants/viewer → table-level read on db_read_isolated/T1 only (KA.6 no-bleed)
        _add_grant(session, {
            "resource": "table",
            "tenant": "quants",
            "groups": ["viewer"],
            "databaseName": "db_read_isolated",
            "table": "T1",
            "actions": ["read"],
        })

        # quants/service → DB-level read on db_read (service_account outbound-strategy test).
        # A grant of its own, distinct from quants/trader above — proves the ACL enforces on the
        # machine identity's own groups claim, not on whichever human happened to call the tool.
        _add_grant(session, {
            "resource": "database",
            "tenant": "quants",
            "groups": ["service"],
            "databaseName": "db_read",
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
