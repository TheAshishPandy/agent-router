#!/usr/bin/env bash
# agent-router — interactive first-run setup.
#
# Idempotent. Prompts for what's missing, leaves alone what's already there.
# Creates: .env (provider keys), data/dashboard-users.json (admin login),
#          data/proxy-keys.json (first API key for clients).

set -euo pipefail

cd "$(dirname "$0")"

PYTHON=${PYTHON:-python3}

cyan()   { printf "\033[1;36m%s\033[0m" "$1"; }
green()  { printf "\033[1;32m%s\033[0m" "$1"; }
yellow() { printf "\033[1;33m%s\033[0m" "$1"; }
dim()    { printf "\033[2m%s\033[0m"   "$1"; }

echo
echo "$(cyan "agent-router setup")"
echo "$(dim "──────────────────────")"
echo

# ── 1. Python check ─────────────────────────────────────────────────
if ! command -v "$PYTHON" >/dev/null 2>&1; then
    echo "$(yellow "✗") Python 3.10+ not found. Install Python and rerun."
    exit 1
fi
echo "$(green "✓") Python: $($PYTHON --version 2>&1)"

# ── 2. .env file ────────────────────────────────────────────────────
if [ ! -f .env ]; then
    cp .env.example .env
    chmod 600 .env
    echo "$(green "✓") Created .env from template (mode 0600). Edit it to add provider keys:"
    echo "    $(dim "MINIMAX_API_KEY, ZAI_API_KEY, OPENAI_API_KEY, GEMINI_API_KEY")"
else
    echo "$(green "✓") .env already exists (leaving alone)"
fi

# ── 3. Dashboard admin user ────────────────────────────────────────
mkdir -p data
chmod 700 data 2>/dev/null || true

if [ ! -f data/dashboard-users.json ]; then
    echo
    echo "$(cyan "Set up dashboard admin login")"
    read -r -p "  username [admin]: " ADMIN_USER
    ADMIN_USER=${ADMIN_USER:-admin}

    while true; do
        read -r -s -p "  password (won't echo): " PW1; echo
        if [ -z "$PW1" ]; then
            echo "  $(yellow "password cannot be empty, try again")"
            continue
        fi
        if [ "${#PW1}" -lt 8 ]; then
            echo "  $(yellow "password must be at least 8 chars")"
            continue
        fi
        read -r -s -p "  confirm:               " PW2; echo
        if [ "$PW1" != "$PW2" ]; then
            echo "  $(yellow "passwords don't match, try again")"
            continue
        fi
        break
    done

    # Need bcrypt — try to use whatever python is available.
    HASH=$("$PYTHON" - <<PY 2>/dev/null
import sys
try:
    import bcrypt
except ImportError:
    sys.exit(1)
print(bcrypt.hashpw(sys.stdin.buffer.read().rstrip(b"\n"), bcrypt.gensalt(12)).decode())
PY
<<< "$PW1") || HASH=""

    if [ -z "$HASH" ]; then
        echo "  $(yellow "bcrypt not installed yet — install it first (make install or pip install bcrypt) and rerun setup")"
        unset PW1 PW2
        exit 1
    fi
    unset PW1 PW2

    cat > data/dashboard-users.json <<JSON
{
  "users": {
    "$ADMIN_USER": {
      "password_hash": "$HASH",
      "role": "admin"
    }
  }
}
JSON
    chmod 600 data/dashboard-users.json
    echo "$(green "✓") Created data/dashboard-users.json (mode 0600)"
else
    echo "$(green "✓") data/dashboard-users.json already exists (leaving alone)"
fi

# ── 4. First API key ────────────────────────────────────────────────
if [ ! -f data/proxy-keys.json ] || ! grep -q '"key"' data/proxy-keys.json; then
    KEY="pcx-default-$("$PYTHON" -c 'import secrets; print(secrets.token_hex(24))')"
    cat > data/proxy-keys.json <<JSON
{
  "keys": {
    "default": {
      "key": "$KEY",
      "label": "Local default key",
      "created": "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    }
  }
}
JSON
    chmod 600 data/proxy-keys.json
    echo "$(green "✓") Created data/proxy-keys.json with first API key (mode 0600)"
    echo
    echo "  $(cyan "Your client API key:")"
    echo "    $(green "$KEY")"
    echo
    echo "  $(dim "Save this — it's how clients authenticate. Manage more via the dashboard.")"
else
    echo "$(green "✓") data/proxy-keys.json already exists (leaving alone)"
    KEY=""
fi

# ── 5. Tailscale config (optional) ─────────────────────────────────
if [ ! -f data/tailscale.json ]; then
    cp data/tailscale.example.json data/tailscale.json
    echo "$(green "✓") Created data/tailscale.json from example. Edit to set CORS allowlist + node map."
fi

# ── 6. Done ────────────────────────────────────────────────────────
echo
echo "$(cyan "Setup complete.") Next:"
echo
echo "  $(dim "1.")  Edit .env and add at least one provider key"
echo "  $(dim "2.")  make install      $(dim "(if you haven't)")"
echo "  $(dim "3.")  make run          $(dim "(starts proxy + upstream wrappers)")"
echo "  $(dim "4.")  Open http://127.0.0.1:8001/api/dashboard"
echo
if [ -n "${KEY:-}" ]; then
    echo "  $(dim "From clients:")"
    echo "    export ANTHROPIC_BASE_URL=\"http://127.0.0.1:8001/api\""
    echo "    export ANTHROPIC_API_KEY=\"$KEY\""
    echo "    claude"
    echo
fi
