# agent-router — common commands
#
# `make help` for the full list. `make setup` for first run.

# ── Config ──────────────────────────────────────────────────────────
PYTHON       ?= python3
PIP          ?= pip3
PROXY_PORT   ?= 8001
CLAUDE_PORT  ?= 8000
CODEX_PORT   ?= 8006
PID_DIR      := .pids
LOG_DIR      := logs

# Use .venv automatically if present.
ifneq ($(wildcard .venv/bin/python),)
    PYTHON := .venv/bin/python
    PIP    := .venv/bin/pip
endif

.PHONY: help setup install run run-proxy run-claude-api run-codex-api stop \
        claude claude-zai claude-codex claude-minimax \
        monitor watch clean check-env

# ── Help (default target) ───────────────────────────────────────────
help:
	@echo "agent-router — common commands"
	@echo
	@echo "First time setup:"
	@echo "  make setup           Interactive wizard (creates dashboard user, .env, API key)"
	@echo "  make install         pip install -r requirements.txt"
	@echo
	@echo "Run:"
	@echo "  make run             Start proxy + claude-api + codex-api in background"
	@echo "  make run-proxy       Just the proxy in foreground (for development)"
	@echo "  make stop            Stop background servers"
	@echo
	@echo "Use Claude Code through different providers:"
	@echo "  make claude          Through Claude Max (default)"
	@echo "  make claude-zai      Through Z.AI (GLM-4.6)"
	@echo "  make claude-codex    Through Codex (GPT)"
	@echo "  make claude-minimax  Through MiniMax M2.5 swarm"
	@echo
	@echo "Diagnostics:"
	@echo "  make monitor         One-shot health report"
	@echo "  make watch           Live monitor (30s refresh)"
	@echo "  make clean           Remove pycache, sessions, local logs"

# ── Setup & install ────────────────────────────────────────────────
setup:
	@bash setup.sh

install: check-env
	$(PIP) install -r requirements.txt
	@echo
	@echo "Installed. Next: make setup (if you haven't), then make run."

check-env:
	@command -v $(PYTHON) >/dev/null 2>&1 || { \
	    echo "ERROR: $(PYTHON) not found. Install Python 3.10+ or set PYTHON=..."; exit 1; }

# ── Run ─────────────────────────────────────────────────────────────
$(PID_DIR) $(LOG_DIR):
	@mkdir -p $@

run: $(PID_DIR) $(LOG_DIR) check-env
	@$(MAKE) -s run-claude-api
	@$(MAKE) -s run-codex-api
	@$(MAKE) -s _run-proxy-bg
	@sleep 1
	@echo
	@echo "  proxy        → http://127.0.0.1:$(PROXY_PORT)/api/health"
	@echo "  claude-api   → http://127.0.0.1:$(CLAUDE_PORT)/health"
	@echo "  codex-api    → http://127.0.0.1:$(CODEX_PORT)/health"
	@echo "  dashboard    → http://127.0.0.1:$(PROXY_PORT)/api/dashboard"
	@echo "  logs         → tail -f $(LOG_DIR)/*.log"
	@echo "  stop         → make stop"

run-proxy: check-env
	$(PYTHON) main.py

_run-proxy-bg: $(PID_DIR) $(LOG_DIR)
	@if [ -f $(PID_DIR)/proxy.pid ] && kill -0 $$(cat $(PID_DIR)/proxy.pid) 2>/dev/null; then \
	    echo "proxy already running (pid $$(cat $(PID_DIR)/proxy.pid))"; \
	else \
	    nohup $(PYTHON) main.py >$(LOG_DIR)/proxy.log 2>&1 & echo $$! > $(PID_DIR)/proxy.pid; \
	    echo "proxy started (pid $$(cat $(PID_DIR)/proxy.pid))"; \
	fi

run-claude-api: $(PID_DIR) $(LOG_DIR)
	@if [ -f $(PID_DIR)/claude-api.pid ] && kill -0 $$(cat $(PID_DIR)/claude-api.pid) 2>/dev/null; then \
	    echo "claude-api already running (pid $$(cat $(PID_DIR)/claude-api.pid))"; \
	elif command -v claude >/dev/null 2>&1 || [ -n "$$CLAUDE_PATH" ]; then \
	    nohup $(PYTHON) claude-api-server.py >$(LOG_DIR)/claude-api.log 2>&1 & echo $$! > $(PID_DIR)/claude-api.pid; \
	    echo "claude-api started (pid $$(cat $(PID_DIR)/claude-api.pid))"; \
	else \
	    echo "claude CLI not found — skipping claude-api-server (install Claude Code or set CLAUDE_PATH)"; \
	fi

run-codex-api: $(PID_DIR) $(LOG_DIR)
	@if [ -f $(PID_DIR)/codex-api.pid ] && kill -0 $$(cat $(PID_DIR)/codex-api.pid) 2>/dev/null; then \
	    echo "codex-api already running (pid $$(cat $(PID_DIR)/codex-api.pid))"; \
	elif command -v codex >/dev/null 2>&1 || [ -n "$$CODEX_PATH" ]; then \
	    nohup $(PYTHON) codex-api-server.py >$(LOG_DIR)/codex-api.log 2>&1 & echo $$! > $(PID_DIR)/codex-api.pid; \
	    echo "codex-api started (pid $$(cat $(PID_DIR)/codex-api.pid))"; \
	else \
	    echo "codex CLI not found — skipping codex-api-server (install OpenAI codex CLI or set CODEX_MODE=api)"; \
	fi

stop:
	@for svc in proxy claude-api codex-api; do \
	    if [ -f $(PID_DIR)/$$svc.pid ]; then \
	        pid=$$(cat $(PID_DIR)/$$svc.pid); \
	        if kill -0 $$pid 2>/dev/null; then \
	            kill $$pid && echo "stopped $$svc (pid $$pid)"; \
	        else \
	            echo "$$svc not running"; \
	        fi; \
	        rm -f $(PID_DIR)/$$svc.pid; \
	    fi; \
	done

# ── Use Claude Code through any provider ──────────────────────────
claude:
	@bash launch-via-proxy.sh

claude-zai:
	@bash launch-zai-claude.sh

claude-codex:
	@bash launch-codex-claude.sh

claude-minimax:
	@bash launch-minimax-claude.sh

# ── Diagnostics ─────────────────────────────────────────────────────
monitor:
	$(PYTHON) monitor.py

watch:
	$(PYTHON) monitor.py --watch

# ── Cleanup ─────────────────────────────────────────────────────────
clean:
	@find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	@find . -type f -name '*.pyc' -delete 2>/dev/null || true
	@rm -rf $(PID_DIR) $(LOG_DIR) sessions/
	@echo "cleaned"
