import sys

# Reset sys.argv so pytest's own argv can't leak into anything that reads it. The bundle is
# mount-only now (no standalone CLI to parse), so AppSettings no longer touches argv — this is a
# harmless defensive baseline rather than a parsing workaround.
sys.argv = ["kx-mcp-kdbx-tests"]