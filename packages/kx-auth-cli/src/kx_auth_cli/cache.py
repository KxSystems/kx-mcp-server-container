"""Client-side token cache for ``kx auth`` — a single JSON file, keyed by endpoint.

``login`` writes the acquired credential here; ``exchange`` reads it as a subject-token fallback.
The cache is a *client-side* concern (the agent/operator's own credentials), so it lives in the CLI
rather than the shared core. Templates include product CLIs' endpoint-keyed caches and ``gh``'s
``~/.config/gh/hosts.yml`` — a single JSON file under a dotdir, path overridable, keyed by endpoint
so multiple deployments coexist. OS-keyring storage is a later hardening, not the v1 bar.

The keys are the ``--server`` (MCP-server / resource) URLs the agent connects to. ``introspect`` is
stateless and never touches this file.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Optional


def cache_path() -> Path:
    """The credential file path: ``$KX_AUTH_CACHE`` if set, else ``~/.kx/credentials.json``."""
    override = os.environ.get("KX_AUTH_CACHE")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".kx" / "credentials.json"


def load() -> dict[str, Any]:
    """The whole cache as ``{server: credential}``; ``{}`` if absent or unreadable (never raises)."""
    path = cache_path()
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        # Missing or corrupt cache is "no cached credentials", not a hard error.
        return {}


def get(server: str) -> Optional[dict[str, Any]]:
    """The cached credential for ``server``, or ``None`` if not present."""
    entry = load().get(server)
    return entry if isinstance(entry, dict) else None


def save(server: str, credential: dict[str, Any]) -> Path:
    """Merge ``credential`` under ``server`` and persist. Dir ``0700``, file ``0600``."""
    path = cache_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    data = load()
    data[server] = credential
    # Write via a temp file then replace, so a crash mid-write can't truncate the cache. The temp
    # file is created 0600 at open (not chmod'd after), so tokens never sit on disk umask-readable.
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, default=str)
    os.replace(tmp, path)
    return path
