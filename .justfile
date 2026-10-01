# https://cheatography.com/linux-china/cheat-sheets/justfile/
# Workspace dev recipes for the KX MCP composition container. This justfile ships publicly and is
# the entry point for building/running/testing the packages. (Internal release/ops recipes —
# snyk/sonar/publish-release — live in the dev repo's root .justfile, which does not ship.)
set shell := ["bash", "-c"]

# Where `install-modules` gets the kx.auth / kx.rbac q modules. They are canonical in the kx-auth repo
# and deliberately not vendored here. Override with a local checkout for development:
#   just kx_auth_src=/path/to/kx-auth install-modules
kx_auth_src := "https://github.com/KxSystems/kx-auth.git"
kx_auth_ref := "v0.5.0"

[private]
default:
    @just --list

# run the composition container with the kdb-x backend (zero-code launcher; kdb-x quickstart)
run:
  uv run kx-mcp --bundles kdbx

# run the kdb-x backend — mount-only, so this routes through the container (no standalone process)
run-kdbx:
  uv run kx-mcp --bundles kdbx

# run the kdb.ai backend — mount-only, so this routes through the container (no standalone process)
run-kdbai:
  uv run kx-mcp --bundles kdbai

# run the composition container from the hand-written glue (server.py)
run-compose:
  uv run python server.py

# run all deterministic tests (unit + integration, no real IdP) — default CI run
test:
  uv run pytest

# run only in-process unit tests (fast feedback; includes per-package unit trees)
test-unit:
  uv run pytest -m "not integration and not realidp"

# run only subprocess integration tests (real HTTP/stdio, deterministic, license-free)
test-integration:
  uv run pytest -m integration

# lint the shippable workspace + public demos (ruff check, no autofix)
lint:
  uv run ruff check .

# apply ruff's safe autofixes + formatter (dev convenience; not gated in CI)
fmt:
  uv run ruff check . --fix
  uv run ruff format .

# check formatting without writing (not gated in CI yet — no formatted baseline committed)
fmt-check:
  uv run ruff format --check .

# type-check the shipped library code + public demos (needs no KDB license — static only)
typecheck:
  uv run mypy packages/*/src demos

# the pre-push gate: lint + typecheck (matches the blocking CI job)
check: lint typecheck

# install the kx.auth / kx.rbac modules onto the q module path (~/.kx/mod); idempotent
install-modules:
  #!/usr/bin/env bash
  set -euo pipefail
  # These modules are NOT vendored in this repo — they are canonical in kx-auth. This fetches a PINNED
  # tag and deliberately does not track a branch, so an upstream change cannot alter this repo's
  # behaviour without a visible bump to kx_auth_ref at the top of this file.
  #
  # To work from a local checkout instead (symlinked, so edits are picked up live), point at the
  # directory that holds `modules/kx`:
  #   just kx_auth_src=/path/to/kx-auth install-modules
  mod_root="${HOME}/.kx/mod/kx"
  src="{{ kx_auth_src }}"
  mkdir -p "$mod_root"

  if [ -d "$src" ]; then
    # A local checkout: symlink straight at it so edits land without re-running this. Same layout as a
    # release, modules at `modules/kx` under the directory given; no other layout is probed.
    src_root="$(cd "$src" && pwd)/modules/kx"
    origin="$src (local checkout)"
  else
    # Shallow-clone the pinned tag into a cache dir. Re-cloned rather than fetched: a tag is immutable,
    # so there is nothing to update, and a stale cache from a previous ref would silently win.
    cache="${XDG_CACHE_HOME:-$HOME/.cache}/kx-mcp-server-container/kx-auth-{{ kx_auth_ref }}"
    if [ ! -d "$cache/modules/kx" ]; then
      rm -rf "$cache"
      git clone --depth 1 --branch "{{ kx_auth_ref }}" "$src" "$cache" 2>&1 | sed 's/^/  /'
    fi
    src_root="$cache/modules/kx"
    origin="$src @ {{ kx_auth_ref }}"
  fi

  if [ ! -d "$src_root" ]; then
    echo "no modules at $src_root" >&2
    echo "  kx.auth / kx.rbac are canonical in the kx-auth repo, not vendored here." >&2
    echo "  For a local checkout: just kx_auth_src=/path/to/kx-auth install-modules" >&2
    echo "  (kx_auth_src must be the directory that holds modules/kx)" >&2
    exit 1
  fi

  for d in "$src_root"/*/; do
    name="$(basename "$d")"
    # `ln -sfn` onto a real directory links INSIDE it and leaves the old copy in use, e.g. one
    # installed from a release tarball. Refuse rather than delete something this recipe did not make.
    if [ -e "$mod_root/$name" ] && [ ! -L "$mod_root/$name" ]; then
      echo "$mod_root/$name is a directory, not a link this recipe made; remove it and re-run" >&2
      exit 1
    fi
    ln -sfn "$d" "$mod_root/$name"
    echo "linked kx.$name -> $mod_root/$name"
  done
  echo "  source: $origin"

# update packages / add dev dependencies (dev is a default dependency-group, so plain sync pulls it)
update:
  uv sync --upgrade

# source new venv
venv-source:
  uv venv --python=3.12 --seed; uv sync --python=3.12; . ./.venv/bin/activate && exec $SHELL

# generate code coverage
coverage:
  uv run coverage run; uv run coverage report

# run real-IdP tests against a local Keycloak instance (manual, not in CI)
# Prerequisites:
#   1. docker compose -f tests/deterministic/realidp/setup/keycloak/docker-compose.yaml up -d
#   2. uv run python tests/deterministic/realidp/setup/keycloak/keycloak_setup.py tests/deterministic/realidp/setup/keycloak/keycloak_config.json
#   3. cp tests/deterministic/realidp/envs/.env.keycloak.example tests/deterministic/realidp/envs/.env.keycloak  # fill in values
_KC_ENV := "tests/deterministic/realidp/envs/.env.keycloak"
test-keycloak:
  @test -f {{_KC_ENV}} || (echo "ERROR: {{_KC_ENV}} not found — copy tests/deterministic/realidp/envs/.env.keycloak.example and fill it in" && exit 1)
  bash -c 'set -a; source {{_KC_ENV}}; set +a; uv run pytest -m "realidp and not kdbai and not kdbx" -v'

# run the kdbx "ferry" identity-assertion tests against a real q process + the same live Keycloak
# stack test-keycloak uses (no kdbai-db needed — kdbx spawns its own throwaway q process).
# Prerequisites: same Keycloak stack as test-keycloak (steps 1-3 above), plus a kdb-x install
# (`q` on PATH or ~/.kx/bin/q) with a valid license.
# NB: scoped to the kdbx test dir (not just `-m kdbx`) — collecting the full test suite imports
# kx_mcp_kdbx.server at collection time, which sets PYKX_LICENSED=true and triggers `import pykx`
# in the pytest process *itself* before any fixture runs, breaking the later-spawned container
# subprocess's own licensed pykx init. Scoping collection avoids the contamination at its source.
test-kdbx:
  @test -f {{_KC_ENV}} || (echo "ERROR: {{_KC_ENV}} not found — copy tests/deterministic/realidp/envs/.env.keycloak.example and fill it in" && exit 1)
  bash -c 'set -a; source {{_KC_ENV}}; set +a; uv run pytest tests/deterministic/realidp/kdbx -m kdbx -v'

# run kdbai OAuth ACL tests against a local OAuth kdbai-db + Keycloak (manual, not in CI)
# Prerequisites:
#   1. docker login portal.dl.kx.com -u <portal-email> -p <bearer-token>  (bearer token from
#      portal.dl.kx.com -> username -> Token Management; see
#      https://code.kx.com/kdbai/latest/gettingStarted/kdb-ai-server-setup.html#get-the-kdbai-docker-image)
#   2. mkdir -p tests/deterministic/realidp/setup/keycloak/kdbai-data tests/deterministic/realidp/setup/keycloak/acl-data && chmod 777 tests/deterministic/realidp/setup/keycloak/kdbai-data tests/deterministic/realidp/setup/keycloak/acl-data
#   3. KDB_LICENSE_B64=$(base64 < ~/.kx/kc.lic) docker compose -f tests/deterministic/realidp/setup/keycloak/docker-compose.yaml --profile backends up -d
#   4. uv run python tests/deterministic/realidp/setup/keycloak/keycloak_setup.py tests/deterministic/realidp/setup/keycloak/keycloak_config.json
#   5. uv run python tests/deterministic/realidp/setup/keycloak/seed.py
#   6. cp tests/deterministic/realidp/envs/.env.kdbai.example tests/deterministic/realidp/envs/.env.kdbai
_KDBAI_ENV := "tests/deterministic/realidp/envs/.env.kdbai"
test-kdbai:
  @test -f {{_KDBAI_ENV}} || (echo "ERROR: {{_KDBAI_ENV}} not found — copy tests/deterministic/realidp/envs/.env.kdbai.example and fill it in" && exit 1)
  bash -c 'set -a; source {{_KDBAI_ENV}}; set +a; uv run pytest -m kdbai -v'

# run the kdbai OAuth ACL tests against a live OAuth kdbai-db trusting Microsoft Entra ID
# (manual, not in CI). alice/bob get different ACL results purely off the Entra-propagated tid/groups.
# Prerequisites (Entra provisioned via entra_setup.py — see setup/entra/README.md):
#   1. docker login portal.dl.kx.com -u <portal-email> -p <bearer-token>  (bearer token from
#      portal.dl.kx.com -> username -> Token Management; see
#      https://code.kx.com/kdbai/latest/gettingStarted/kdb-ai-server-setup.html#get-the-kdbai-docker-image)
#   2. mkdir -p tests/deterministic/realidp/setup/entra/kdbai-data tests/deterministic/realidp/setup/entra/acl-data && chmod 777 tests/deterministic/realidp/setup/entra/{kdbai-data,acl-data}
#   3. set -a; source tests/deterministic/realidp/envs/.env.entra; set +a
#      KDB_LICENSE_B64=$(base64 < ~/.kx/kc.lic) docker compose -f tests/deterministic/realidp/setup/entra/docker-compose.yaml up -d
#   4. uv run python tests/deterministic/realidp/setup/entra/seed.py
test-kdbai-entra:
  @test -f {{_ENTRA_ENV}} || (echo "ERROR: {{_ENTRA_ENV}} not found — see setup/entra/README.md" && exit 1)
  bash -c 'set -a; source {{_ENTRA_ENV}}; set +a; uv run pytest -m kdbai -v'

# run real-IdP tests against Microsoft Entra ID (manual, not in CI)
# Prerequisites (first time only):
#   1. cp tests/deterministic/realidp/envs/.env.entra.example tests/deterministic/realidp/envs/.env.entra
#      Fill in the INPUT vars (ENTRA_TENANT_ID, ENTRA_CLIENT_ID, ENTRA_CLIENT_SECRET, ENTRA_PASSWORD_*)
#   2. source tests/deterministic/realidp/envs/.env.entra
#      uv run python tests/deterministic/realidp/setup/entra/entra_setup.py
#      (writes ENTRA_DOMAIN, group Object IDs, KX_MCP_AUTH_* into .env.entra)
_ENTRA_ENV := "tests/deterministic/realidp/envs/.env.entra"
test-entra:
  @test -f {{_ENTRA_ENV}} || (echo "ERROR: {{_ENTRA_ENV}} not found — copy tests/deterministic/realidp/envs/.env.entra.example and fill in the ENTRA_* vars" && exit 1)
  bash -c 'set -a; source {{_ENTRA_ENV}}; set +a; uv run pytest -m "realidp and not kdbai and not kdbx" -v'
