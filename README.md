# agent-router

**One Claude-Code-compatible endpoint, four LLM backends, your tailnet.**

Bring your Claude Max, ChatGPT, MiniMax, and Z.AI subscriptions. Run agent-router on a Mac mini or VPS. Every device on your Tailscale tailnet now has a single API endpoint that:

- speaks Anthropic `/v1/messages` natively (Claude Code just works)
- routes to whichever provider you ask for, with **automatic cascade** when one rate-limits
- can run **3 workers in parallel** and consolidate (`X-Swarm-Mode: parallel`)
- can run **N rounds of self-deliberation** (`X-Swarm-Mode: deliberation`)
- strips identifying headers before any upstream call
- tracks usage per provider, per day, with a live dashboard

```
your laptop                                          your Mac mini
─────────                                            ─────────────
 claude  ─── Tailscale WireGuard ───→  agent-router ─┬─→  claude  CLI    (Claude Max)
                                       :8001        ├─→  codex   CLI    (ChatGPT)
                                                    ├─→  api.z.ai       (Anthropic-compat)
                                                    ├─→  api.minimaxi   (3-worker swarm)
                                                    └─→  Gemini Flash   (image gen)
```

## Get it running in 90 seconds

You need: Python 3.10+, a Tailscale tailnet (optional but recommended), and at least one of `claude` CLI / `codex` CLI / a MiniMax key / a Z.AI key.

```bash
git clone https://github.com/<you>/agent-router && cd agent-router
make setup        # interactive: creates dashboard user, .env, first API key
make install      # pip install -r requirements.txt
make run          # starts proxy + claude-api-server + codex-api-server
```

Open http://127.0.0.1:8001/api/dashboard, log in, and you're done. The setup step prints the API key you'll use from clients.

## Use it from Claude Code

```bash
export ANTHROPIC_BASE_URL="http://your-tailnet-host:8001/api"
export ANTHROPIC_API_KEY="pcx-..."     # the key make setup printed
claude
```

That's it. Claude Code now flows through your router. Any device that can reach your tailnet works — laptop, phone, the Mac in the next room.

## Use it from curl

```bash
PROXY=http://127.0.0.1:8001/api
KEY=pcx-...

# Claude (default upstream)
curl -s "$PROXY/v1/messages" \
  -H "x-api-key: $KEY" -H "Content-Type: application/json" \
  -d '{"model":"claude-sonnet-4-6","max_tokens":256,"messages":[{"role":"user","content":"Hi"}]}'

# Z.AI (GLM-4.6) — drop-in for Claude
curl -s "$PROXY/zai" \
  -H "x-api-key: $KEY" -H "Content-Type: application/json" \
  -d '{"max_tokens":256,"messages":[{"role":"user","content":"Hi"}]}'

# Codex (GPT) — uses your ChatGPT subscription via codex CLI
curl -s "$PROXY/codex" \
  -H "x-api-key: $KEY" -H "Content-Type: application/json" \
  -d '{"max_tokens":256,"messages":[{"role":"user","content":"Hi"}]}'

# MiniMax — 3-worker swarm
curl -s "$PROXY/minimax" \
  -H "x-api-key: $KEY" -H "Content-Type: application/json" \
  -H "X-Swarm-Mode: parallel" -H "X-Swarm-Workers: 3" \
  -d '{"messages":[{"role":"user","content":"Hi"}]}'

# Deliberation: 5 rounds, each builds on the last
curl -s "$PROXY/v1/messages" \
  -H "x-api-key: $KEY" -H "Content-Type: application/json" \
  -H "X-Swarm-Mode: deliberation" -H "X-Deliberation-Rounds: 5" \
  -d '{"model":"claude-sonnet-4-6","max_tokens":1024,"messages":[{"role":"user","content":"Plan a web scraper"}]}'
```

## Use Claude Code through any provider

These scripts swap the upstream provider Claude Code thinks it's talking to:

```bash
make claude-zai       # Claude Code → Z.AI (GLM-4.6)
make claude-codex     # Claude Code → Codex (GPT, via your ChatGPT subscription)
make claude-minimax   # Claude Code → MiniMax M2.5 swarm
```

Each one sets `ANTHROPIC_BASE_URL` to the right `/api/<provider>` route and launches `claude`. Same workflow, different brain.

## Make targets

```
make help          List all targets
make setup         First-run wizard: dashboard user, .env, API key
make install       pip install -r requirements.txt (uses .venv if present)
make run           Start everything (proxy + claude-api + codex-api in background)
make run-proxy     Just the proxy (foreground, for development)
make stop          Stop background servers started by make run
make claude        Open Claude Code through the proxy (default: Claude upstream)
make claude-zai    Open Claude Code routed through Z.AI
make claude-codex  Open Claude Code routed through Codex
make claude-minimax Open Claude Code routed through MiniMax swarm
make monitor       Health/diagnostic report
make watch         Live-watch monitor (30s refresh)
make clean         Remove __pycache__, *.pyc, sessions, logs
```

## Endpoints

| Method | Path | Auth | Purpose |
|---|---|---|---|
| POST | `/api/v1/messages` | API key | Anthropic-compatible (Claude via CLI) |
| POST | `/api/opus`, `/api/sonnet`, `/api/prompt` | API key | Simple `{prompt}` form |
| POST | `/api/codex` | API key | Codex/GPT |
| POST | `/api/zai` | API key | Z.AI GLM-4.6 |
| POST | `/api/minimax` | API key | MiniMax M2.5 |
| POST | `/api/image` | API key | Gemini Flash image generation |
| GET | `/api/health` | Public | Liveness |
| GET | `/api/help` | Public | Machine-readable endpoint list |
| GET | `/api/network-status` | Public | Tailnet hostname, IP, peer count |
| GET | `/api/stats` | Tailnet | Usage analytics (sanitized) |
| GET | `/api/limits` | Tailnet | Rate-limit headroom per provider |
| GET | `/api/swarm` | Tailnet | Swarm status and history |
| GET | `/api/dashboard` | Session | Web dashboard |
| GET | `/api/setup` | API key | Generates ready-to-paste shell exports + Claude Code settings |
| GET/POST/DELETE | `/api/keys` | Tailnet | API key CRUD |

**Auth tiers:**
- *Public* — anyone who can reach the proxy
- *Tailnet* — any tailnet peer (CGNAT 100.64/10), no key needed
- *API key* — `x-api-key: pcx-...` or `Authorization: Bearer pcx-...`
- *Session* — browser session cookie issued by `/api/login`

## Swarm modes

Set per request via headers (works on `/api/v1/messages`, `/api/minimax`, `/api/zai`):

| Header | Value | Effect |
|---|---|---|
| `X-Swarm-Mode` | `parallel` *(default)* | Run N workers in parallel; consolidate. |
| `X-Swarm-Mode` | `deliberation` | Run N rounds sequentially; each round sees the previous. |
| `X-Swarm-Mode` | `none` | Single call, no swarm. |
| `X-Swarm-Workers` | `1`–`5` | Override worker count. |
| `X-Deliberation-Rounds` | `1`–`10` | Override round count. |

## Cascade

When the primary provider rate-limits, agent-router falls through the chain in `platform.json` → `cascade.chain`. Default:

```
claude  →  zai  →  minimax
```

Z.AI is first fallback because it speaks Anthropic `/v1/messages` natively (zero translation cost). MiniMax is last because it needs format translation. The response indicates which provider served it.

## Codex modes

`CODEX_MODE=cli` *(default)* — agent-router forwards to `codex-api-server.py` on port 8006, which wraps `codex exec`. Uses your ChatGPT subscription, no API key.

`CODEX_MODE=api` — agent-router translates Anthropic → OpenAI Responses format and hits `https://api.openai.com/v1/responses` directly. Requires `OPENAI_API_KEY`.

Set in `.env`:

```bash
CODEX_MODE=cli   # or 'api'
```

## Configuration

Three places, in priority order:

1. **`.env`** (or shell env) — `MINIMAX_API_KEY`, `ZAI_API_KEY`, `OPENAI_API_KEY`, `GEMINI_API_KEY`, paths
2. **`platform.json`** — service ports, timeouts, rate limits, cascade chain, default models
3. **`data/config.json`** — file-based fallback for provider keys (mode 0600)

The dashboard accounts and API keys live in `data/dashboard-users.json` and `data/proxy-keys.json` — see `data/README.md`.

## Security model

**Threat model.** This proxy is for a **single trusted operator** running on infrastructure they control, accessed by a small number of devices via Tailscale. It is **not** a multi-tenant SaaS gateway:

- No isolation between API keys — all keys can hit all upstreams.
- No request body sanitization — your prompts go upstream as-is.
- Dashboard sessions are 24h signed cookies; no CSRF token (Tailscale is the perimeter).

**What we do guarantee:**

- API keys stored as plain strings in `data/proxy-keys.json` (mode 0600). Treat as secret.
- Dashboard passwords are bcrypt-hashed (`$2b$12`).
- Login attempts are rate-limited per source IP.
- Privacy headers stripped before any upstream call (`X-Forwarded-For`, `X-Real-IP`, `Via`, `Forwarded`, `X-Envoy-External-Address`).
- `/api/stats` goes through `_sanitize_stats()` — no key bodies, no IPs, no internal paths.
- All write operations to JSON state files are atomic (temp + rename).
- Upstream wrappers (`claude-api-server.py`, `codex-api-server.py`) bind 127.0.0.1 by default and warn loudly if you change that.

**Tailscale is not optional in production.** Without it you have no transport security and no peer identity. If you must expose this on a public IP: put it behind a real reverse proxy with TLS, set `allow_origin_regex` in `main.py` accordingly, and don't expect the privacy claims above to hold.

## Troubleshooting

**`claude` CLI not found**
Install [Claude Code](https://docs.claude.com/claude-code) or set `CLAUDE_PATH=/path/to/claude` in `.env`.

**`codex` CLI not found**
Either install OpenAI's `codex` CLI, or skip CLI mode and use API mode: set `CODEX_MODE=api` and `OPENAI_API_KEY=...` in `.env`. The proxy then bypasses `codex-api-server.py` entirely.

**`make run` says port 8001 is in use**
Kill the old process: `make stop` or `lsof -ti:8001 | xargs kill`. If something else owns the port, change `services.proxy.port` in `platform.json`.

**Dashboard says "Invalid credentials"**
Re-run `make setup` to regenerate `data/dashboard-users.json`. The bcrypt hash there must be a real `$2b$12$...` string, not a placeholder.

**Claude Code returns "Connection refused"**
Check `curl http://127.0.0.1:8001/api/health` returns `{"status":"ok"}`. If yes but you still get refused, your `ANTHROPIC_BASE_URL` is wrong (must end in `/api`, no trailing slash). If no, the proxy isn't running — `make run`.

**Rate limited on Claude but cascade didn't kick in**
Cascade only triggers on specific error patterns (rate-limit, overloaded, usage-limit). Other errors bubble up. Check `cascade.chain` in `platform.json` and verify your fallback providers have keys configured (`make monitor` shows which providers are reachable).

**Tailscale peers can't reach the dashboard**
Bind the proxy to `0.0.0.0` (already the default in `platform.json`). Verify with `tailscale status` that your peer sees this host. CORS allowlist is configured for the Tailscale CGNAT range automatically.

## Project layout

```
agent-router/
├── proxy.py                 FastAPI router; all /api/* routes; swarm, cascade, auth
├── main.py                  ASGI entrypoint; CORS, middleware, lifecycle
├── platform.json            Single source of truth: ports, timeouts, providers
├── platform_config.py       Loader + typed accessors for platform.json
├── claude-api-server.py     Wraps the `claude` CLI as HTTP (Anthropic-compat)
├── codex-api-server.py      Wraps the `codex` CLI as HTTP (Anthropic-compat)
├── minimax-bridge.py        Standalone Anthropic→OpenAI translator
├── monitor.py               CLI ops/diagnostic tool
├── dashboard.html           Web ops UI
├── static/                  api.js + design-tokens.css
├── data/                    Runtime state (gitignored, *.example.json committed)
├── Makefile                 Common commands
├── setup.sh                 Interactive first-run wizard
├── launch-minimax-claude.sh
├── launch-zai-claude.sh
└── launch-codex-claude.sh
```

## Contributing

Issues and PRs welcome. Please:

- Don't add upstream providers without format-translation tests.
- Don't add features that require persistent state outside `data/` and `~/.agent-router/`.
- Run `python3 -m py_compile` on any file you touch.

## License

Apache 2.0 — see [LICENSE](LICENSE).
