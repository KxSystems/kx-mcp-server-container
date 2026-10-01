# CLAUDE.md

Guidance for coding agents working in this workspace. `AGENTS.md` is a symlink to this file. Paths below are relative to this directory.

## What this is

A **KX MCP composition container**: a [FastMCP](https://gofastmcp.com) 3.x parent that owns the **control plane** (inbound authentication, the Subject/Action/Resource (S/A/R) authorization seam, and outbound identity propagation) and mounts pluggable **backend extensions** into one served surface. Each extension is a bundle exposing `build_server() -> FastMCP`. Two backends ship: **KDB-X** and **KDB.AI**.

**Framing rule for new prose** (docs, comments, commit messages): lead with the control plane, then the extension contract, then the shipped backends. Don't describe the project as "a kdb-x query tool"; that's one extension's surface. FastMCP 3.x is the chosen framework, so don't reintroduce framework-openness.

| Package | Role |
|---|---|
| `packages/kx-mcp-core/` | The container: `assembly.py` (`make_parent`, the only place the parent is built; `mount_bundle` / `try_mount_bundle`), `launcher.py` (the `kx-mcp` script), `auth/` (`KX_MCP_AUTH` modes, `current_principal`, audit), `discovery.py` (`register_components`), `observability/`, `tool_result`. |
| `packages/kx-auth-core/` | Auth mechanisms shared by container, bundles and CLI. **fastmcp-free.** Inbound `AuthSettings` + joserfc `verify_token`; outbound `exchange()` + `register_outbound_strategy`; `assertion` (principal projection, also pykx-free); `authz` (adapter registry behind `@authorize`). |
| `packages/kx-auth-cli/` | The `kx auth` CLI (`introspect`/`login`/`exchange`/`assert`), run where the agent runs. Depends on `kx-auth-core` + `httpx` only, never fastmcp. **A bugfix-only mirror**: don't extend it here. |
| `packages/kx-mcp-kdbx/` | KDB-X over qIPC via PyKX. `KDBX_DB_*` config, cached connection in `utils/kdbx.py`, SQL tool (`.s.e` + `.j.j`, 1000-row cap, write-keyword blocklist), identity assertion + data gate. |
| `packages/kx-mcp-kdbai/` | KDB.AI via `kdbai-client` (no PyKX license). `KDBAI_DB_*`; `service_account` or `passthrough`, over qipc or rest. |

The `kx.auth` and `kx.rbac` q modules are not in this repo: `just install-modules` installs them from a
pinned `kx-auth` release (see `public/.justfile`).

The root `pyproject.toml` is a non-package workspace aggregator. `server.py` is the hand-written equivalent of the launcher.

## Commands

Run from this directory. PyKX needs a license (see Gotchas); most kdb-x work also needs a reachable KDB-X on `KDBX_DB_HOST:KDBX_DB_PORT` (default `127.0.0.1:5010`).

| Task | Command |
|---|---|
| Run the container (kdb-x / kdb.ai) | `just run` (= `uv run kx-mcp --bundles kdbx`) / `just run-kdbai` |
| Run from glue | `just run-compose` (= `uv run python server.py`) |
| Tests / unit / integration | `just test` / `just test-unit` / `just test-integration` |
| One test | `uv run pytest packages/kx-mcp-kdbx/tests/unit/addins/test_kdbx_run_sql_query.py -k <name>` |
| Lint + type-check (the blocking gate) | `just check` (= `just lint` + `just typecheck`); autofix `just fmt` |
| Coverage | `just coverage` |
| Set up from the lockfile | `uv sync --locked` |
| Upgrade dependencies intentionally | `just update` |
| Install q modules onto the q path | `just install-modules` |
| Live smoke | `q examples/host.q` (serves `:5010`), then `just run` |
| Introspect a bearer | `uv run kx auth introspect <token> --json` |

## Contracts

Each is an API. Change one only together with its docs and a pinning test.

- **Extension contract.** `build_server(config=None) -> FastMCP` registers **bare** names; the container namespaces them at `mount(namespace=…)`. Config is an env-prefixed fragment (`KDBX_DB_*`, `KDBAI_DB_*`; the container is `KX_MCP_*`). The eager pre-flight must be **bounded by the bundle's own timeout**: bundles mount sequentially, and a synchronous `build_server()` can't be cancelled, so `KX_MCP_MOUNT_TIMEOUT` is only a backstop that abandons the thread. Shipped bundles are **mount-only** (no `main()`/`.run()`). See `docs/extending.md`.
- **Instance-safety.** A bundle must be mountable twice. Per-instance state lives on the server object (`mcp._kdbx_config`), and tools read it at call time via `ctx.fastmcp` (`config_from_ctx(ctx)`). Never module globals.
- **Tool results (`isError`).** A failing tool returns `isError: true` or raises. Use `tool_result` / `error_result` at the `@tool` wrapper, so the `*_impl` stays a plain-dict function. A failure reported only inside the payload is counted `ok`. Tools are `async def` and declare `annotations` (`readOnlyHint` for reads). See `docs/extending.md` § Signalling failure.
- **`KX_MCP_AUTH` modes**: `unset` / `static` / `jwks` / `oidc_proxy` / `entra`, registered via `register_auth_mode`. `jwks` + `KX_MCP_AUTH_RESOURCE_URL` advertises RFC 9728 discovery. The proxy modes are stateful and do discovery I/O at startup. Prefer `jwks` when the client already holds a token. See `docs/auth.md`.
- **`GET /health`**: unauthenticated in every auth mode, liveness not readiness, and the parent's route wins over a bundle's. See `docs/deployment.md`.
- **`kx-auth-core`** stays fastmcp-free, and `verify_token` must agree with the container's `JWTVerifier` (pinned by `tests/deterministic/unit/test_auth_cli_contract.py`). The CLI's `--json` envelope and exit codes (`0` ok, `1` error, `2` usage, `3` auth-required, `4` denied) are public.
- **Outbound exchange**: `exchange(config, subject_token)` + `register_outbound_strategy` (`passthrough` / `rfc_8693` / `service_account` / `custom`).
- **Identity assertion (plain kdb+)**: qIPC has no bearer, so the container ferries the validated principal and q **promotes** it (`.kx.auth.promote`, the single authority). `bind` is caller-gated by the same policy (`assert` on `kx.identity`). The `kx.*` resource root (`kx.identity`, `kx.rbac`, `kx.q`) is reserved for the module; hosts use `data.<table>` or their own root. Opt-in via `KDBX_DB_ASSERT_IDENTITY`.
- **S/A/R authz, two enforcement points.** PEP-1 is the data-agnostic `@authorize` capability check at the tool boundary, applied selectively (the SQL tool's `run_query_impl` carries it). PEP-2 is the backend's native data gate (q `.kx.auth.entitled`, the KDB.AI ACL), which can scope down via `obligations`; kdb-x applies it as a filter or as re-scope guidance, never SQL rewriting. The SQL blocklist is a guardrail, not authorization.
- **KDB-X metadata contract** `schema://kdbx/metadata/v1`: a stable projection over aimeta. Keep the JSON Schema, projection, descriptions, tests and changelog in sync; breaking output means a new version and URI.
- **Instrumentation surface**: `span`, `metric` and the re-exported Prometheus collectors. Bundles import neither `prometheus_client` nor `opentelemetry`. A helper, not a contract obligation. See `docs/extending.md` § Observability.

## Conventions

- **The MCP spec and best practices are canonical** over local house style.
- **Never crash on a partial failure.** A bad token, a denial, or an unreachable backend gives a clean denial or startup error. `try_mount_bundle` disables only the failing backend (import failures included). But the launcher exits non-zero if **no** requested backend mounts, and `--exit-on-mount-failure` opts into strict mounting. Degrading still returns `isError`. The assembly seam stays policy-free; posture belongs to the launcher.
- **No regression.** Re-shipped extensions keep every existing guardrail (e.g. the SQL write-keyword blocklist).
- **Tooling lowers the floor, never raises the complexity ceiling.** Every flow an agent automates must stay achievable by a human following the docs alone.
- **Current posture**: auth is opt-in (`KX_MCP_AUTH=unset`); single-principal STDIO is a first-class path ("zero-config" means no auth, not no arguments, so bundle selection stays explicit). Default transport is `streamable-http` on port 8000.
- **Lint gate.** ruff (deliberately minimal: `E4,E7,E9,F`) + mypy over `packages/*/src` + `demos`, both blocking. Keep `just check` green.
- **Versions are tag-driven via hatch-vcs, in lockstep.** Never add a static `version =`.
- **Use canonical kdb+ values in demos, not toy ones.**

## Adding things

- **A primitive**: start from `addins/*.py.template` in the bundle, keep the `*_impl` separate from the registered `@tool`, and add a test under the package's `tests/` mirroring `src/`. `demos/extending/` is the runnable worked example (the Acme bundle).
- **A backend**: follow `docs/extending.md`; same contract as kdb-x.

## Testing

- Package unit tests live in `packages/<pkg>/tests/`, targeting `*_impl` with PyKX mocked. Container tests are in `tests/`, using the dependency-free `tests/fixtures/kx-mcp-example/` bundle.
- `tests/deterministic/`: `unit/` (in-process), `integration/` (real subprocess over the wire, license-free, `@pytest.mark.integration`), `realidp/` (live Keycloak/Entra, `@pytest.mark.realidp`, excluded by default).
- Registration, transport and auth wiring are proven by an **integration test against a real process**, not mocks alone.
- Detail and coverage tracker: `tests/docs/TESTING.md`.

## Gotchas

For q identity storage and promotion, read [q dictionary gotchas](docs/q-gotchas.md).

- PyKX runs licensed (`PYKX_LICENSED=true`). With kdb-x installed the license resolves via the `q` binary; set `QLIC` only for standalone PyKX.
- macOS `:5000` is AirPlay; a qIPC connect there times out. kdb-x defaults to `:5010`.
- Use the standalone `fastmcp` 3.x package, not the FastMCP v1 inside `mcp[cli]`.
- PyKX turns `str` into a q **symbol**; wrap free text (and per-token claims) in `kx.CharVector`.
- Embedded q in Python must be a raw string (`r'''…'''`): `\:` is an invalid escape and `\t` / `\l` get mangled.
- Direct `ContextVar.set` in worker threads does not propagate back; authorization stamps use the shared `AuthzSlot` via `stamp_authz_decision`. Keep tools `async def`.
- `CallToolResult.data` is `None` on an `isError` result; read `.structured_content`.
- A bare `JWTVerifier` advertises no discovery; `RemoteAuthProvider` (via `KX_MCP_AUTH_RESOURCE_URL`) does.
- Logging is configured by entry points (`configure_logging`), never by the library, and never with `basicConfig`.
- The uv workspace hides undeclared sibling deps; `tests/deterministic/unit/test_packaging_deps.py` guards it.
- `kdbai-client` pins `pykx<4`; the `[tool.uv] override-dependencies` bridges it, so install with uv, not pip.
- A lone `/` line in a `.q` file silently comments out the rest of the file.
- `prior` and `next` are reserved in q; as parameter names they fail at call time with `'nyi`.
