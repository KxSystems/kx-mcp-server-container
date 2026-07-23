from .server import build_server

# The KDB.AI backend is mount-only: it exposes only the `build_server() -> FastMCP` extension seam
# for the container to mount(). There is no standalone entry point — run it via the container:
# `uv run kx-mcp --bundles kdbai`.
__all__ = ["build_server"]
