"""The mount budget: how long a bundle's ``build_server()`` has left under ``KX_MCP_MOUNT_TIMEOUT``.

The launcher's worker thread cannot be cancelled, and ``join(timeout)`` does not return on time while
the worker holds the GIL (an embedded-q ``hopen`` does). So the timeout only bounds startup if the
bundle bounds its own blocking calls. The launcher opens a budget around each ``build_server()`` it
runs on a worker; a bundle reads :func:`remaining_mount_budget` and caps its connect timeouts by it.
Outside a bounded mount (no ``KX_MCP_MOUNT_TIMEOUT``, the hand-written glue, a tool call at serve
time) there is no budget and the reader gets ``None``.
"""

from __future__ import annotations

import contextlib
import threading
import time
from typing import Iterator, Optional

# Thread-local, not a contextvar: the budget belongs to the one worker thread the launcher starts,
# and must not leak into threads (or tasks) the bundle starts to serve later.
_local = threading.local()


@contextlib.contextmanager
def mount_budget(seconds: float) -> Iterator[None]:
    """Run the body with ``seconds`` of mount budget on this thread (``<= 0`` means unbounded)."""
    previous = getattr(_local, "deadline", None)
    _local.deadline = time.monotonic() + seconds if seconds > 0 else None
    try:
        yield
    finally:
        _local.deadline = previous


def remaining_mount_budget() -> Optional[float]:
    """Seconds left in this thread's mount budget, floored at 0; ``None`` when there is none."""
    deadline = getattr(_local, "deadline", None)
    if deadline is None:
        return None
    return max(0.0, deadline - time.monotonic())
