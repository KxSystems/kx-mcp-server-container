# Tests

The test suite is all exact pass/fail tests — unit, subprocess integration, and real-IdP. Each
test owns its own inputs and checks one specific outcome.

## Layout

```
tests/                    ← this directory
  deterministic/     ← exact pass/fail assertions, runs in CI
    unit/            ← in-process, no subprocess, fastest
    integration/     ← real subprocess over HTTP/stdio, license-free
    realidp/         ← requires live Keycloak/Entra, excluded from CI
  docs/              ← TESTING.md (what's tested / how it works / coverage tracker)
  fixtures/          ← shared test bundles (kx-mcp-example + composition test doubles)
  conftest.py        ← spawn_container harness (used by integration; realidp has its own _spawn.py)
```

## Which command to run?

| What you want | Command |
|---|---|
| Fast feedback, everything in CI | `just test` |
| Unit tests only (no subprocess) | `just test-unit` |
| Subprocess integration tests only | `just test-integration` |
| Real-IdP tests (Keycloak must be running) | `just test-keycloak` |
| Coverage report | `just coverage` |

All commands run from the **repo root** (`kx-mcp-server-container/`).

## Further reading

- [`docs/TESTING.md`](docs/TESTING.md) — the one test doc: what's tested (inbound/authz/outbound
  at a glance), how the suite works (tiers, fixtures, real-IdP harness, how to run it), and the
  row-by-row coverage tracker for every feature area.
- Per-tier READMEs: [`deterministic/unit/`](deterministic/unit/README.md),
  [`deterministic/integration/`](deterministic/integration/README.md),
  [`deterministic/realidp/`](deterministic/realidp/README.md).
