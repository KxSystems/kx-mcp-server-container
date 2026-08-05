"""A deliberately-failing fixture bundle for the graceful-degradation-over-the-wire test (X.8).

Its ``build_server()`` simulates a real backend whose eager connectivity pre-flight fails and
``sys.exit(1)``s. The package itself imports cleanly, so the launcher resolves ``build_server`` and
the failure happens *inside the call* — exactly the ``SystemExit`` path ``try_mount_bundle`` catches
to disable one backend while the container keeps serving the healthy ones. Dependency-free (no
pykx / license), so it runs in CI alongside the ``example`` fixture.
"""

from .server import build_server

__all__ = ["build_server"]
