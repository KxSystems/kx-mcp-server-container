# Changelog

All notable changes to the public release of the KX MCP composition container are documented here.
The top versioned heading drives the public release: the release tooling reads it to derive the
tag `vX.Y.Z` and uses this section's body as the GitHub Release notes.

The format is loosely based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the
project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html). Versions are shared
with the internally published wheels (same `X.Y.Z`), though the public cut happens on its own,
less-frequent cadence.

## [Unreleased]

## [0.3.0] - 2026-08-05

### Added
- Added aimeta-backed semantic discovery to the kdb-x extension over its existing qIPC connection,
  with graceful native introspection fallback, per-mounted-server caching, table/function lookup
  resources, a metadata refresh tool, and an annotated reference demo host.
- Added the versioned agent-facing KDB-X metadata contract v1 at
  `schema://kdbx/metadata/v1`, including richness tier, annotation status, semantic references,
  function dependencies, authorization filtering, and live table state.
- Added five kdb-x primitives on top of that contract: the resources `tables://kdbx/{table}`,
  `functions://kdbx/all`, `functions://kdbx/{function}`, and `schema://kdbx/metadata/v1`, plus the
  `kdbx_get_table_metadata` tool (single table with a bounded live preview) and the administrative
  `kdbx_refresh_metadata` tool, which reloads recompiled annotations without restarting the
  container. `kdbx_refresh_metadata` is route-only when `KX_MCP_AUTHZ` is unset (the default); when
  capability authz is configured, callers need the `admin` capability for `kdbx:metadata`.
- Added `KDBX_DB_AIMETA_CACHE_TTL` (default `300` seconds) to control how long aimeta metadata is
  cached, including a negative probe against an unannotated host. Set it to `0` to disable caching;
  use `kdbx_refresh_metadata` to pick up recompiled annotations sooner.

### Changed
- Clarified mount-time backend requirements, feature gates, call-time search prerequisites, and the
  KDB-X identity-assertion credential/policy setup across the public operator documentation.
- Made the implicit KDB.AI port follow the selected protocol (`8082` for qipc, `8081` for REST);
  an explicit `KDBAI_DB_PORT` continues to take precedence.
- Corrected KDB.AI authentication diagnostics to name the public `KDBAI_DB_USERNAME` and
  `KDBAI_DB_PASSWORD` settings.
- `tables://kdbx/all` now returns metadata-contract-v1 JSON instead of the former ASCII overview.
- `kx-mcp-kdbx` now declares a `jsonschema>=4,<5` runtime dependency, used to validate output
  against the packaged metadata-contract schema.

### Fixed
- `kx.auth`'s `.kx.auth.bind` now replaces a handle's asserted principal **wholesale**. It previously
  merged column-wise, so re-binding a principal with fewer promoted fields on a live handle kept the
  previous one's extras — a `tenant` (or `groups`/`act`/`exp`) could outlive the token that resolved it
  and a q-side policy could then authorize on stale identity. Reachable on the ordinary
  identity-assertion path, since connections are cached per `(sub, iss)` and re-bound on every call, so
  a token refresh for the same user reuses the same qIPC handle. The same defect made a bind
  signal `'mismatch` when another handle held a principal with a different field set.
- `.kx.auth.bind` no longer signals `'type` on a valid principal whose fields are all atoms (e.g. a
  claims-free `sub`/`iss` pair): promotion widens the incoming dict before storing the promoted
  symbol vectors.

### Security
- `cryptography` 49.0.0→50.0.0 to clear a newly-published High-severity timing-attack advisory
  ([SNYK-PYTHON-CRYPTOGRAPHY-18516621](https://security.snyk.io/vuln/SNYK-PYTHON-CRYPTOGRAPHY-18516621)),
  with the declared floor raised to `cryptography>=50.0.0` (kx-auth-core) so fresh installs get the
  fix. `cryptography` backs the static-PEM verification path, so this is on the inbound auth chain.
- Refreshed the dependency lockfile to clear the Snyk dependency-scan findings (all High/Medium,
  no Critical): `mcp` 1.27.2→1.28.1 (WebSocket origin validation), `starlette` 1.2.1→1.3.1,
  `cryptography` 48.0.0→49.0.0, `aiohttp`, `pillow`, `langsmith`, `python-multipart`, and
  `pydantic-settings` 2.14.1→2.14.2. All fixes were bound-compatible patch/minor bumps.
- Bumped the declared floors we own so fresh installs get the fixed versions:
  `cryptography>=48.0.1` (kx-auth-core) and `pydantic-settings>=2.14.2` (all backends + core).

## [0.2.2] - 2026-07-23

### Added
- Initial public release of the KX MCP server container: the
  `kx-mcp-core` container, the `kx-mcp-kdbx` / `kx-mcp-kdbai` backends, and the
  `kx-auth-core` / `kx-auth-cli` auth libraries, plus the deterministic + real-IdP test suites.
