#!/usr/bin/env bash
# claude-code-live-kdbai demo — turnkey operator path.
#
# Starts one MCP container (alice on :8000) and registers it with Claude Code WITHOUT a
# pre-supplied token.  Claude Code then self-authenticates via RFC 9728 native discovery:
#   401 → /.well-known/oauth-protected-resource → Keycloak DCR → auth-code browser flow.
#
# Usage:
#   demos/claude-code-live-kdbai/run.sh   # start container + register
#
# Prerequisites (one-time):
#   docker login registry.gitlab.com
#   export KDB_LICENSE_B64=$(base64 ~/.kx/kc.lic)
#   cd tests/deterministic/realidp/setup/keycloak
#   KDB_LICENSE_B64=$KDB_LICENSE_B64 docker compose --profile backends up -d
#   uv run keycloak_setup.py keycloak_config.json
#   uv run seed.py
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"

DEMO_DIR="demos/claude-code-live-kdbai"
ENV_FILE="$DEMO_DIR/kdbai.env"

ALICE_PORT=8000
KC_BASE="${KC_BASE:-http://localhost:8080}"
KDBAI_PORT="${KDBAI_DB_PORT:-8082}"

ALICE_PID=""
LOG_ALICE="/tmp/kx-mcp-alice.log"

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

_teardown() {
  echo
  echo "==> teardown ..."
  [[ -n "$ALICE_PID" ]] && kill "$ALICE_PID" 2>/dev/null && echo "  stopped container"
  claude mcp remove kx-kdbai-alice 2>/dev/null && echo "  removed kx-kdbai-alice" || true
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

trap '_teardown' EXIT

echo "==> pre-flight checks ..."

if ! curl -sf "${KC_BASE}/health/ready" >/dev/null 2>&1; then
  echo "ERROR: Keycloak not ready at ${KC_BASE}/health/ready" >&2
  echo "       Run: docker compose up -d  (from tests/deterministic/realidp/setup/keycloak/)" >&2
  exit 1
fi
echo "  Keycloak:  OK (${KC_BASE})"

_check_port "kdbai-db" "$KDBAI_PORT"
echo "  kdbai-db:  OK (:${KDBAI_PORT})"

# Load the shared env
set -a; source "$ENV_FILE"; set +a

echo
echo "==> starting container ..."

KX_MCP_AUTH_RESOURCE_URL="http://localhost:${ALICE_PORT}" \
  uv run kx-mcp --bundles kdbai --transport streamable-http \
    --host 127.0.0.1 --port "$ALICE_PORT" \
    >"$LOG_ALICE" 2>&1 &
ALICE_PID=$!
echo "  container pid=$ALICE_PID (log: $LOG_ALICE)"

_wait_for_mcp "kx-kdbai-alice" "$ALICE_PORT"

echo
echo "==> registering MCP server (no token — Claude Code will authenticate itself) ..."
claude mcp remove kx-kdbai-alice 2>/dev/null || true
claude mcp add --transport http kx-kdbai-alice "http://localhost:${ALICE_PORT}/mcp"
echo "  registered kx-kdbai-alice → http://localhost:${ALICE_PORT}/mcp"

echo
echo "============================================================"
echo " Container running.  Next steps:"
echo ""
echo "  1. Start Claude Code FROM THE REPO ROOT:"
echo "       cd $(pwd) && claude"
echo ""
echo "  2. Run /mcp — you should see kx-kdbai-alice with 'needs authentication'."
echo "     (If you already had Claude Code open, restart it.)"
echo ""
echo "  3. Hit Authenticate — a browser window will open."
echo "     Log in as alice (password: alice123)."
echo "     Browser shows 'Authentication successful'."
echo ""
echo "  4. Run /mcp again — should show 'connected · 11 tools'."
echo ""
echo "  5. Paste the scenario from demos/claude-code-live-kdbai/agent.md"
echo ""
echo "  Ctrl-C here to tear down the container + remove the registration."
echo "============================================================"

# Keep alive until Ctrl-C
wait "$ALICE_PID" 2>/dev/null || true
