# Changelog

All notable changes to the public release of the KX MCP composition container are documented here.
The top versioned heading drives the public release: the release tooling reads it to derive the
tag `vX.Y.Z` and uses this section's body as the GitHub Release notes.

The format is loosely based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the
project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html). Versions are shared
with the internally published wheels (same `X.Y.Z`), though the public cut happens on its own,
less-frequent cadence.

## [Unreleased]

## [0.2.2] - 2026-07-23

### Added
- Initial public release of the KX MCP server container: the
  `kx-mcp-core` container, the `kx-mcp-kdbx` / `kx-mcp-kdbai` backends, and the
  `kx-auth-core` / `kx-auth-cli` auth libraries, plus the deterministic + real-IdP test suites.