#!/bin/bash
# Launch Claude Code CLI routed through MiniMax M2.5 Swarm
#
# Starts the MiniMax Bridge (Anthropic→OpenAI translator) on port 8004,
# then opens Claude Code CLI pointing at it.

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BRIDGE_PORT=8004
BRIDGE_PID=""

# Unset CLAUDECODE to allow nested launch
unset CLAUDECODE

cleanup() {
    if [ -n "$BRIDGE_PID" ] && kill -0 "$BRIDGE_PID" 2>/dev/null; then
        kill "$BRIDGE_PID" 2>/dev/null
        wait "$BRIDGE_PID" 2>/dev/null
    fi
}
trap cleanup EXIT

# Kill any existing bridge on our port
lsof -ti ":${BRIDGE_PORT}" 2>/dev/null | xargs kill 2>/dev/null
sleep 0.3

# Start the bridge in background
python3 "${SCRIPT_DIR}/minimax-bridge.py" --port "${BRIDGE_PORT}" --swarm &
BRIDGE_PID=$!

# Wait for bridge to be ready
for i in $(seq 1 20); do
    if curl -sf "http://127.0.0.1:${BRIDGE_PORT}/health" >/dev/null 2>&1; then
        break
    fi
    sleep 0.25
done

if ! curl -sf "http://127.0.0.1:${BRIDGE_PORT}/health" >/dev/null 2>&1; then
    echo "ERROR: MiniMax Bridge failed to start on port ${BRIDGE_PORT}"
    exit 1
fi

echo ""
echo "╔═══════════════════════════════════════════════════╗"
echo "║  Claude Code → MiniMax M2.5 Swarm                ║"
echo "║  Bridge: http://127.0.0.1:${BRIDGE_PORT}                ║"
echo "║  Mode: 3-worker swarm + consolidation             ║"
echo "╚═══════════════════════════════════════════════════╝"
echo ""

# Launch Claude Code pointing at our bridge
export ANTHROPIC_BASE_URL="http://127.0.0.1:${BRIDGE_PORT}"
export ANTHROPIC_API_KEY="minimax-bridge-local"

# Set CLAUDE_SKIP_PERMS=1 to add --dangerously-skip-permissions (off by default).
EXTRA_FLAGS=""
if [ "${CLAUDE_SKIP_PERMS:-0}" = "1" ]; then
    EXTRA_FLAGS="--dangerously-skip-permissions"
fi
MODEL="${CLAUDE_MODEL:-claude-sonnet-4-6}"

claude ${EXTRA_FLAGS} --model "${MODEL}"

echo ""
echo "Session ended. Shutting down bridge..."
