#!/bin/bash
# Launch Claude Code CLI routed through the Codex (GPT) backend.
#
# Points Claude Code at agent-router's /api/codex endpoint, which forwards
# to a local codex-api-server (CLI passthrough — uses your ChatGPT
# subscription) or directly to OpenAI's API if CODEX_MODE=api.

PROXY_PORT="${AGENT_ROUTER_PORT:-8001}"
PROXY_KEY="${AGENT_ROUTER_KEY:?AGENT_ROUTER_KEY env var required (proxy API key)}"

# Probe proxy health
if ! curl -sf "http://127.0.0.1:${PROXY_PORT}/health" >/dev/null 2>&1; then
    echo "ERROR: agent-router proxy not running on port ${PROXY_PORT}"
    echo "       Start it with: python3 main.py"
    exit 1
fi

echo ""
echo "╔═══════════════════════════════════════════════════╗"
echo "║  Claude Code → Codex (GPT)                        ║"
echo "║  Router: http://127.0.0.1:${PROXY_PORT}/api/codex          ║"
echo "╚═══════════════════════════════════════════════════╝"
echo ""

unset CLAUDECODE
export ANTHROPIC_BASE_URL="http://127.0.0.1:${PROXY_PORT}/api/codex"
export ANTHROPIC_API_KEY="${PROXY_KEY}"

EXTRA_FLAGS=""
if [ "${CLAUDE_SKIP_PERMS:-0}" = "1" ]; then
    EXTRA_FLAGS="--dangerously-skip-permissions"
fi
MODEL="${CLAUDE_MODEL:-claude-sonnet-4-6}"

claude ${EXTRA_FLAGS} --model "${MODEL}"

echo ""
echo "Session ended."
