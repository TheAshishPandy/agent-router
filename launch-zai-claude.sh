#!/bin/bash
# Launch Claude Code CLI routed through Z.AI (GLM-4.6).
#
# Points Claude Code at agent-router's /api/zai endpoint, which forwards
# to Z.AI's Anthropic-compatible /v1/messages endpoint.

PROXY_PORT="${AGENT_ROUTER_PORT:-8001}"
PROXY_KEY="${AGENT_ROUTER_KEY:?AGENT_ROUTER_KEY env var required (proxy API key)}"

if ! curl -sf "http://127.0.0.1:${PROXY_PORT}/health" >/dev/null 2>&1; then
    echo "ERROR: agent-router proxy not running on port ${PROXY_PORT}"
    exit 1
fi

echo ""
echo "╔═══════════════════════════════════════════════════╗"
echo "║  Claude Code → Z.AI (GLM-4.6)                     ║"
echo "║  Router: http://127.0.0.1:${PROXY_PORT}/api/zai            ║"
echo "╚═══════════════════════════════════════════════════╝"
echo ""

unset CLAUDECODE
export ANTHROPIC_BASE_URL="http://127.0.0.1:${PROXY_PORT}/api/zai"
export ANTHROPIC_API_KEY="${PROXY_KEY}"

EXTRA_FLAGS=""
if [ "${CLAUDE_SKIP_PERMS:-0}" = "1" ]; then
    EXTRA_FLAGS="--dangerously-skip-permissions"
fi
MODEL="${CLAUDE_MODEL:-glm-4.6}"

claude ${EXTRA_FLAGS} --model "${MODEL}"

echo ""
echo "Session ended."
