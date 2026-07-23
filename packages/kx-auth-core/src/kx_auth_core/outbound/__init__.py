"""Outbound identity propagation: the pluggable token-exchange seam.

A backend tool reads the validated inbound principal (`current_principal()`) and calls
`exchange(config, subject_token)` to mint the backend-shaped credential — `passthrough` / `rfc_8693`
/ `service_account`, or a `custom` driver registered via `register_outbound_strategy`. The exchange
runs in the tool, not in parent middleware (the principal crosses the mount boundary; middleware
state does not).
"""

from .claims import decode_claims_unverified
from .config import OutboundConfig, OutboundCredential
from .strategies import exchange, outbound_strategies, register_outbound_strategy

__all__ = [
    "OutboundConfig",
    "OutboundCredential",
    "exchange",
    "register_outbound_strategy",
    "outbound_strategies",
    "decode_claims_unverified",
]
