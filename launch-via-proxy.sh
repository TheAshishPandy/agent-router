#!/bin/bash
# Launch Claude Code routed through agent-router (default Claude upstream).
# Same as setting ANTHROPIC_BASE_URL/API_KEY by hand, but reads the key from
# data/proxy-keys.json automatically.

set -euo pipefail
cd "$(dirname "$0")"

PROXY_PORT="${AGENT_ROUTER_PORT:-8001}"
PROXY_KEY="${AGENT_ROUTER_KEY:-}"

# Read first key from data/proxy-keys.json if not in env.
if [ -z "$PROXY_KEY" ] && [ -f data/proxy-keys.json ]; then
    PROXY_KEY=$(python3 -c '
import json,sys
try:
    keys = json.load(open("data/proxy-keys.json")).get("keys", {})
    for v in keys.values():
        if v.get("key"):
            print(v["key"]); break
except Exception:
    pass
')
fi

if [ -z "$PROXY_KEY" ]; then
    echo "ERROR: no API key available."
    echo "       Either run 'make setup' (creates one) or export AGENT_ROUTER_KEY=pcx-..."
    exit 1
fi

if ! curl -sf "http://127.0.0.1:${PROXY_PORT}/health" >/dev/null 2>&1; then
    echo "ERROR: agent-router proxy not running on port ${PROXY_PORT}"
    echo "       Start it with: make run"
    exit 1
fi

echo
echo "╔═══════════════════════════════════════════════════╗"
echo "║  Claude Code → agent-router (default: Claude)     ║"
echo "║  Router: http://127.0.0.1:${PROXY_PORT}/api                ║"
echo "╚═══════════════════════════════════════════════════╝"
echo

unset CLAUDECODE
export ANTHROPIC_BASE_URL="http://127.0.0.1:${PROXY_PORT}/api"
export ANTHROPIC_API_KEY="${PROXY_KEY}"

EXTRA_FLAGS=""
if [ "${CLAUDE_SKIP_PERMS:-0}" = "1" ]; then
    EXTRA_FLAGS="--dangerously-skip-permissions"
fi
MODEL="${CLAUDE_MODEL:-claude-sonnet-4-6}"

claude ${EXTRA_FLAGS} --model "${MODEL}"
