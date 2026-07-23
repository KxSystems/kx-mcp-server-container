#!/usr/bin/env bash
# claude-code-live-kdbai-entra demo — turnkey operator path.
#
# Starts one MCP container (alice on :8000) with KX_MCP_AUTH=entra (FastMCP AzureProvider) and
# registers it with Claude Code WITHOUT a pre-supplied token. Claude Code then self-authenticates
# against Microsoft Entra ID:
#   401 → AzureProvider's DCR-compatible facade → Entra app login → auth-code browser flow.
#
# Usage:
#   demos/claude-code-live-kdbai-entra/run.sh   # start container + register
#
# Prerequisites (one-time):
#   docker login registry.gitlab.com
#   export KDB_LICENSE_B64=$(base64 ~/.kx/kc.lic)
#   Provision Entra + .env.entra (see tests/deterministic/realidp/setup/entra/README.md):
#     uv run tests/deterministic/realidp/setup/entra/entra_setup.py
#   Bring up + seed the Entra-trusting kdbai-db (mkdir/chmod first — kdbai-db runs as 'nobody'):
#     mkdir -p tests/deterministic/realidp/setup/entra/{kdbai-data,acl-data}
#     chmod 777 tests/deterministic/realidp/setup/entra/{kdbai-data,acl-data}
#     docker compose -f tests/deterministic/realidp/setup/entra/docker-compose.yaml up -d
#     uv run python tests/deterministic/realidp/setup/entra/seed.py
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"

DEMO_DIR="demos/claude-code-live-kdbai-entra"
ENTRA_ENV_FILE="tests/deterministic/realidp/envs/.env.entra"
DEMO_ENV_FILE="$DEMO_DIR/entra.env"

ALICE_PORT=8000
KDBAI_PORT="${KDBAI_DB_PORT:-8082}"
MCP_SERVER_NAME="kx-kdbai-entra"
MCP_URL="http://localhost:${ALICE_PORT}/mcp"

ALICE_PID=""
LOG_ALICE="/tmp/kx-mcp-entra-alice.log"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_check_port() {
  local label="$1" port="$2"
  if ! (exec 3<>"/dev/tcp/127.0.0.1/${port}") 2>/dev/null; then
    exec 3>&- 3<&- 2>/dev/null || true
    echo "ERROR: $label not reachable on :${port}" >&2
    return 1
  fi
  exec 3>&- 3<&- 2>/dev/null || true
}

_wait_for_mcp() {
  local name="$1" port="$2"
  local url="http://localhost:${port}/mcp"
  echo "  waiting for $name on :${port} ..."
  for _ in $(seq 1 40); do
    local code
    code=$(curl -s -o /dev/null -w '%{http_code}' \
           -X POST "$url" \
           -H 'Content-Type: application/json' \
           -H 'Accept: application/json, text/event-stream' \
           -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' 2>/dev/null || echo 0)
    # 401 = container up + auth gate active (expected — no token yet)
    if [[ "$code" == "401" ]]; then echo "  $name ready (HTTP 401 auth gate ✓)"; return 0; fi
    sleep 0.5
  done
  echo "ERROR: $name on :${port} never came up (see log below):" >&2
  tail -20 "${LOG_ALICE}" 2>/dev/null || true
  exit 1
}

_warn_stale_cache() {
  # A prior demo (Keycloak, or a previous Entra run against a different tenant) may have left a
  # stale token keyed on this exact MCP URL. kx_cache is the `kx auth` CLI's cache (only in play
  # if you've run `kx auth login` against this URL — Claude Code never reads it); the needs-auth
  # cache is Claude Code's own. A stale token gets silently rejected by kdbai-db with no useful
  # error. NB `jq 'del(...)' file` prints to stdout — write to a temp file and move it back.
  local kx_cache="$HOME/.kx/credentials.json"
  local needs_auth_cache="$HOME/.claude/mcp-needs-auth-cache.json"
  if [[ -f "$kx_cache" ]] && grep -q "$MCP_URL" "$kx_cache" 2>/dev/null; then
    echo "  WARNING: found a cached token for $MCP_URL in $kx_cache (the kx auth CLI's cache)"
    echo "           If kx auth calls fail, clear it:"
    echo "           jq 'del(.\"$MCP_URL\")' $kx_cache > /tmp/kxcred.\$\$ && mv /tmp/kxcred.\$\$ $kx_cache"
  fi
  if [[ -f "$needs_auth_cache" ]] && grep -q "$MCP_SERVER_NAME" "$needs_auth_cache" 2>/dev/null; then
    echo "  WARNING: found a stale entry for $MCP_SERVER_NAME in $needs_auth_cache"
    echo "           If /mcp re-auth loops, clear it:"
    echo "           jq 'del(.\"$MCP_SERVER_NAME\")' $needs_auth_cache > /tmp/mcpauth.\$\$ && mv /tmp/mcpauth.\$\$ $needs_auth_cache"
  fi
}

_teardown() {
  echo
  echo "==> teardown ..."
  [[ -n "$ALICE_PID" ]] && kill "$ALICE_PID" 2>/dev/null && echo "  stopped container"
  claude mcp remove "$MCP_SERVER_NAME" 2>/dev/null && echo "  removed $MCP_SERVER_NAME" || true
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

trap '_teardown' EXIT

echo "==> pre-flight checks ..."

if [[ ! -f "$ENTRA_ENV_FILE" ]]; then
  echo "ERROR: $ENTRA_ENV_FILE not found." >&2
  echo "       Provision Entra first: uv run tests/deterministic/realidp/setup/entra/entra_setup.py" >&2
  exit 1
fi
echo "  Entra env:  OK ($ENTRA_ENV_FILE)"

_check_port "kdbai-db" "$KDBAI_PORT"
echo "  kdbai-db:   OK (:${KDBAI_PORT})"

_warn_stale_cache

# Load Entra-provisioned values first, then the demo's KX_MCP_AUTH_* mapping.
set -a
source "$ENTRA_ENV_FILE"
source "$DEMO_ENV_FILE"
set +a

echo
echo "==> starting container (KX_MCP_AUTH=entra) ..."

KX_MCP_AUTH_RESOURCE_URL="http://localhost:${ALICE_PORT}" \
  uv run kx-mcp --bundles kdbai --transport streamable-http \
    --host 127.0.0.1 --port "$ALICE_PORT" \
    >"$LOG_ALICE" 2>&1 &
ALICE_PID=$!
echo "  container pid=$ALICE_PID (log: $LOG_ALICE)"

_wait_for_mcp "$MCP_SERVER_NAME" "$ALICE_PORT"

echo
echo "==> registering MCP server (no token — Claude Code will authenticate against Entra itself) ..."
claude mcp remove "$MCP_SERVER_NAME" 2>/dev/null || true
claude mcp add --transport http "$MCP_SERVER_NAME" "$MCP_URL"
echo "  registered $MCP_SERVER_NAME → $MCP_URL"

echo
echo "============================================================"
echo " Container running.  Next steps:"
echo ""
echo "  1. Start Claude Code FROM THE REPO ROOT:"
echo "       cd $(pwd) && claude"
echo ""
echo "  2. Run /mcp — you should see $MCP_SERVER_NAME with 'needs authentication'."
echo "     (If you already had Claude Code open, restart it.)"
echo ""
echo "  3. Hit Authenticate — a browser window will open."
echo "     Log in as alice@\${ENTRA_DOMAIN} (password: \$ENTRA_PASSWORD_ALICE)."
echo "     Browser shows 'Authentication successful'."
echo ""
echo "  4. Run /mcp again — should show 'connected · 11 tools'."
echo ""
echo "  5. Paste the scenario from demos/claude-code-live-kdbai-entra/agent.md"
echo ""
echo "  If authentication is rejected, see 'Entra wrinkles' in manual.md —"
echo "  most likely a stale cached token for $MCP_URL from a prior demo."
echo ""
echo "  Ctrl-C here to tear down the container + remove the registration."
echo "============================================================"

# Keep alive until Ctrl-C
wait "$ALICE_PID" 2>/dev/null || true
