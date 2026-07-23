"""``kx auth`` — the agent-facing auth CLI for the kx-mcp control plane.

A thin ``kx`` umbrella (stdlib argparse) whose ``auth`` group exposes the auth flows as a headless
surface for an autonomous MCP client: structured ``--json`` output and a stable exit-code contract so
the agent branches on codes, not prose. Subcommands: ``introspect``, ``login``, ``exchange``,
``assert``. Verification reuses the lean :mod:`kx_auth_core` (no fastmcp).
"""
