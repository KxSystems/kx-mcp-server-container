# kx-mcp-example — the minimal-contract fixture

A dependency-free backend bundle (~73 lines, one file) proving the smallest thing that satisfies
the extension contract (see the [extender guide](../../../docs/extending.md)): a package named
`kx_mcp_<name>` exposing `build_server() -> FastMCP` with bare primitive names and no module-global
state. Container-level tests mount it (it sits on the pytest `pythonpath`; it is **not** a shipped
bundle), and it doubles as the floor-of-the-contract reading for bundle authors — see the
[extender guide](../../../docs/extending.md).

Its four tools exercise each seam that crosses the mount boundary:

| Tool | Proves |
| --- | --- |
| `echo` | Plain tool dispatch through a namespaced mount |
| `whoami` | `current_principal()` — the container-validated inbound principal reaches a mounted tool |
| `privileged` | The capability-check decorator (`@authorize`, PEP-1) — a deny surfaces as a clean tool error |
| `propagate` | The outbound `exchange()` seam — inbound principal → outbound credential, per strategy |

It is a *contract validator*, not a starter: a real backend wants the structure in the
[extender guide](../../../docs/extending.md) (config fragment, eager pre-flight, cached
connections, `addins/` discovery). To see it mounted for real (the fixture rides pytest's
`pythonpath`, so outside pytest it needs an explicit `PYTHONPATH` — run from the repo root):
`PYTHONPATH=tests/fixtures/kx-mcp-example/src uv run kx-mcp --bundles example`.
