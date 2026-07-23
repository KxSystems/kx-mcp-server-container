"""Best-effort JWT claim decode for **audit / traceability only** — never for authorization.

The container does not re-verify tokens it mints/forwards outbound: a `passthrough` token was already
validated inbound, and an `rfc_8693` / `service_account` token came straight from a just-called
trusted STS. We decode the payload only to record `sub` / `aud` / `jti` / `scope` in the audit chain.
Opaque (non-JWT) tokens decode to ``{}`` — the credential still works, it just carries no claims.
"""

from __future__ import annotations

import base64
import json
from typing import Any


def decode_claims_unverified(token: str | None) -> dict[str, Any]:
    """Decode a JWT's payload without signature verification. ``{}`` for opaque/empty tokens."""
    if not token:
        return {}
    try:
        payload_b64 = token.split(".")[1]
        padded = payload_b64 + "=" * (-len(payload_b64) % 4)
        decoded = json.loads(base64.urlsafe_b64decode(padded))
    except Exception:
        return {}
    return decoded if isinstance(decoded, dict) else {}
