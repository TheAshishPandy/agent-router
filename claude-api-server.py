#!/usr/bin/env python3
"""
Claude API Server — wraps the `claude` CLI as a local HTTP API.

Lets you use a Claude Max subscription (the `claude` CLI binary) as if it
were the Anthropic API. The proxy in front of it (proxy.py) layers auth,
swarm, cascade, etc. This server is upstream-only — bind to 127.0.0.1.

Endpoints
---------
POST /v1/messages   Anthropic-compatible (Claude Code, Anthropic SDK)
POST /opus          Simple {"prompt": "..."} → Opus
POST /sonnet        Simple {"prompt": "..."} → Sonnet
POST /prompt        Simple {"prompt": "...", "model": "opus|sonnet"}
GET  /health        Liveness probe
GET  /stats         Local analytics (request counts, tokens, hourly)

Configuration
-------------
CLAUDE_PATH         Path to `claude` binary (auto-detected if unset)
CLAUDE_PORT         Listen port (default 8000)
CLAUDE_BIND         Bind host (default 127.0.0.1 — DO NOT bind 0.0.0.0)
ANALYTICS_DIR       Where daily analytics JSON lives (default ~/.agent-router/analytics)
MINIMAX_FALLBACK_URL  If set, rate-limit responses cascade to this URL
                      (typically http://127.0.0.1:8001/api/minimax)
MINIMAX_FALLBACK_KEY  API key for the fallback URL
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from socketserver import ThreadingMixIn
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

# ── Locate `claude` CLI ───────────────────────────────────────────────
CLAUDE_PATH = os.environ.get("CLAUDE_PATH") or shutil.which("claude") or ""
if not CLAUDE_PATH:
    for _candidate in (
        os.path.expanduser("~/.local/bin/claude"),
        "/usr/local/bin/claude",
        "/opt/homebrew/bin/claude",
    ):
        if os.path.isfile(_candidate):
            CLAUDE_PATH = _candidate
            break
if not CLAUDE_PATH:
    print(
        "[claude-api] FATAL: `claude` CLI not found. "
        "Install Claude Code or set CLAUDE_PATH=/path/to/claude",
        file=sys.stderr,
    )
    sys.exit(1)

# ── Bind / port ──────────────────────────────────────────────────────
PORT = int(os.environ.get("CLAUDE_PORT", "8000"))
BIND = os.environ.get("CLAUDE_BIND", "127.0.0.1")
if BIND not in ("127.0.0.1", "localhost", "::1"):
    print(
        f"[claude-api] WARNING: binding to {BIND}. This server has no auth — "
        "put it behind the proxy. Loopback bind strongly recommended.",
        file=sys.stderr,
    )

# ── Analytics ────────────────────────────────────────────────────────
ANALYTICS_DIR = Path(
    os.environ.get("ANALYTICS_DIR")
    or os.path.expanduser("~/.agent-router/analytics")
)
ANALYTICS_DIR.mkdir(parents=True, exist_ok=True)

# Inherit env, but unset CLAUDECODE so nested launch works.
ENV = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}

# ── Optional MiniMax fallback (for rate-limit cascade) ────────────────
MINIMAX_FALLBACK_URL = os.environ.get("MINIMAX_FALLBACK_URL", "")
MINIMAX_FALLBACK_KEY = os.environ.get("MINIMAX_FALLBACK_KEY", "")


def _atomic_write_json(path: Path, data: dict, indent: int = 2) -> None:
    """Write JSON atomically: temp file then rename. Survives crashes mid-write."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}.{int(time.time()*1000)}")
    with open(tmp, "w") as f:
        json.dump(data, f, indent=indent)
    os.replace(tmp, path)


# ── Anthropic /v1/messages helpers ────────────────────────────────────

def flatten_message_content(msg: dict) -> str:
    """Flatten a Claude content block to plain text for the CLI."""
    content = msg.get("content", "")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return str(content)
    parts = []
    for block in content:
        if isinstance(block, dict):
            t = block.get("type")
            if t == "text":
                parts.append(block.get("text", ""))
            elif t == "tool_result":
                inner = block.get("content", block.get("output", ""))
                if isinstance(inner, list):
                    for sub in inner:
                        if isinstance(sub, dict) and sub.get("type") == "text":
                            parts.append(sub.get("text", ""))
                        else:
                            parts.append(str(sub))
                else:
                    parts.append(str(inner))
        elif isinstance(block, str):
            parts.append(block)
    return "\n".join(p for p in parts if p)


def build_prompt_from_messages(messages: list, system: str = "") -> str:
    """Build a single prompt string from Anthropic messages array."""
    lines = []
    if system:
        lines.append(f"<system>\n{system}\n</system>")
    for msg in messages:
        role = msg.get("role", "user").upper()
        text = flatten_message_content(msg)
        if text:
            lines.append(f"{role}: {text}")
    return "\n\n".join(lines)


# ── Analytics tracker ─────────────────────────────────────────────────

class Analytics:
    """Lightweight in-memory analytics with daily JSON persistence."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.server_start = time.time()
        self.requests = {"total": 0, "success": 0, "errors": 0, "timeouts": 0}
        self.by_model: dict = {}
        self.by_endpoint: dict = {}
        self.by_hour: dict = {}
        self.recent: list = []  # last 50 requests for /stats?recent=1
        self._load_today()

    def _today_file(self) -> Path:
        return ANALYTICS_DIR / f"analytics-{datetime.now(timezone.utc).strftime('%Y-%m-%d')}.json"

    def _load_today(self) -> None:
        f = self._today_file()
        if f.exists():
            try:
                data = json.loads(f.read_text())
                self.requests = data.get("requests", self.requests)
                self.by_model = data.get("by_model", {})
                self.by_endpoint = data.get("by_endpoint", {})
                self.by_hour = data.get("by_hour", {})
            except Exception as e:
                print(f"[claude-api] failed to load analytics: {e}", file=sys.stderr)

    def _persist(self) -> None:
        try:
            _atomic_write_json(self._today_file(), {
                "requests": self.requests,
                "by_model": self.by_model,
                "by_endpoint": self.by_endpoint,
                "by_hour": self.by_hour,
                "updated": datetime.now(timezone.utc).isoformat(),
            })
        except Exception as e:
            print(f"[claude-api] analytics persist error: {e}", file=sys.stderr)

    def record(self, endpoint: str, model: str, input_tokens: int,
               output_tokens: int, elapsed: float, status: str = "success") -> None:
        now = datetime.now(timezone.utc)
        hour_key = now.strftime("%Y-%m-%d %H")
        with self._lock:
            self.requests["total"] += 1
            if status == "success":
                self.requests["success"] += 1
            elif status == "timeout":
                self.requests["timeouts"] += 1
            else:
                self.requests["errors"] += 1
            for bucket, key in (
                (self.by_model, model or "unknown"),
                (self.by_endpoint, endpoint),
                (self.by_hour, hour_key),
            ):
                row = bucket.setdefault(key, {"count": 0, "input_tokens": 0,
                                              "output_tokens": 0, "total_time": 0.0})
                row["count"] += 1
                row["input_tokens"] += input_tokens
                row["output_tokens"] += output_tokens
                row["total_time"] += elapsed
            self.recent.append({
                "ts": now.isoformat(),
                "endpoint": endpoint,
                "model": model,
                "elapsed": round(elapsed, 3),
                "status": status,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
            })
            del self.recent[:-50]
            self._persist()

    def summary(self) -> dict:
        with self._lock:
            uptime = time.time() - self.server_start
            return {
                "uptime_seconds": round(uptime, 1),
                "requests": dict(self.requests),
                "by_model": dict(self.by_model),
                "by_endpoint": dict(self.by_endpoint),
                "by_hour": dict(self.by_hour),
            }


analytics = Analytics()


# ── HTTP handler ──────────────────────────────────────────────────────

class ClaudeHandler(BaseHTTPRequestHandler):
    ROUTE_MODELS = {
        "/opus": "claude-opus-4-6",
        "/sonnet": "claude-sonnet-4-6",
        "/prompt": None,  # default opus, override via body.model
    }

    def log_message(self, fmt, *args):
        # Quiet default access log; we have analytics.
        return

    def _send_json(self, status: int, data: dict, extra_headers: dict | None = None):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length <= 0:
            return {}
        return json.loads(self.rfile.read(length))

    def _is_localhost(self) -> bool:
        peer = (self.client_address[0] if self.client_address else "")
        return peer in ("127.0.0.1", "::1", "localhost")

    # ── Routing ──────────────────────────────────────────────────────

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header(
            "Access-Control-Allow-Headers",
            "Content-Type, anthropic-version, x-agent-id, x-source",
        )
        self.end_headers()

    def do_GET(self):
        if self.path == "/health":
            return self._send_json(200, {"ok": True, "claude_path": CLAUDE_PATH})
        if self.path.startswith("/stats"):
            return self._send_json(200, analytics.summary())
        return self._send_json(404, {"error": "not_found", "path": self.path})

    def do_POST(self):
        if self.path in self.ROUTE_MODELS:
            return self._handle_simple(self.ROUTE_MODELS[self.path])
        if self.path == "/v1/messages":
            return self._handle_anthropic()
        return self._send_json(404, {"error": "not_found", "path": self.path})

    # ── Handlers ─────────────────────────────────────────────────────

    def _handle_simple(self, route_model: str | None):
        start = time.time()
        try:
            body = self._read_json()
        except Exception as e:
            return self._send_json(400, {"error": "invalid_json", "detail": str(e)})
        prompt = (body or {}).get("prompt", "")
        if not prompt:
            return self._send_json(400, {"error": "missing_prompt"})
        model = route_model or body.get("model") or "claude-opus-4-6"
        try:
            response = self._run_claude(prompt, model)
        except subprocess.TimeoutExpired:
            elapsed = time.time() - start
            analytics.record(self.path, model, len(prompt) // 4, 0, elapsed, "timeout")
            return self._send_json(504, {"error": "timeout"})
        except Exception as e:
            elapsed = time.time() - start
            analytics.record(self.path, model, len(prompt) // 4, 0, elapsed, "error")
            return self._send_json(500, {"error": "claude_cli_error", "detail": str(e)[:300]})

        if self._is_rate_limited(response):
            fallback = self._try_fallback([{"role": "user", "content": prompt}])
            elapsed = time.time() - start
            if fallback:
                analytics.record(self.path, "minimax-fallback",
                                 len(prompt) // 4, len(fallback) // 4, elapsed, "success")
                return self._send_json(200, {
                    "prompt": prompt,
                    "response": f"(rate limit hit on {model}, served by MiniMax fallback)\n\n{fallback}",
                })
            analytics.record(self.path, model, len(prompt) // 4, 0, elapsed, "error")
            return self._send_json(429, {"error": "rate_limited", "detail": response[:300]})

        elapsed = time.time() - start
        analytics.record(self.path, model, len(prompt) // 4, len(response) // 4, elapsed, "success")
        return self._send_json(200, {"prompt": prompt, "response": response})

    def _handle_anthropic(self):
        start = time.time()
        try:
            body = self._read_json()
        except Exception as e:
            return self._send_json(400, {"error": "invalid_json", "detail": str(e)})
        messages = body.get("messages", [])
        if not messages:
            return self._send_json(400, {"error": "missing_messages"})
        system = body.get("system", "")
        if isinstance(system, list):
            system = "\n".join(
                b.get("text", str(b)) if isinstance(b, dict) else str(b) for b in system
            )
        model = body.get("model") or "claude-sonnet-4-6"
        prompt = build_prompt_from_messages(messages, system)

        try:
            response = self._run_claude(prompt, model)
        except subprocess.TimeoutExpired:
            elapsed = time.time() - start
            analytics.record("/v1/messages", model, len(prompt) // 4, 0, elapsed, "timeout")
            return self._send_json(504, {"error": "timeout"})
        except Exception as e:
            elapsed = time.time() - start
            analytics.record("/v1/messages", model, len(prompt) // 4, 0, elapsed, "error")
            return self._send_json(500, {"error": "claude_cli_error", "detail": str(e)[:300]})

        if self._is_rate_limited(response):
            fallback = self._try_fallback(messages, system)
            elapsed = time.time() - start
            if fallback:
                analytics.record("/v1/messages", "minimax-fallback",
                                 len(prompt) // 4, len(fallback) // 4, elapsed, "success")
                response = fallback
                model_out = "minimax-fallback"
            else:
                analytics.record("/v1/messages", model, len(prompt) // 4, 0, elapsed, "error")
                return self._send_json(429, {
                    "type": "error",
                    "error": {"type": "rate_limit_error", "message": response[:300]},
                })
        else:
            model_out = model

        elapsed = time.time() - start
        in_tokens = len(prompt) // 4
        out_tokens = len(response) // 4
        analytics.record("/v1/messages", model_out, in_tokens, out_tokens, elapsed, "success")

        return self._send_json(200, {
            "id": f"msg_{uuid.uuid4().hex[:24]}",
            "type": "message",
            "role": "assistant",
            "model": model_out,
            "content": [{"type": "text", "text": response}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": in_tokens, "output_tokens": out_tokens},
        })

    # ── Helpers ──────────────────────────────────────────────────────

    @staticmethod
    def _is_rate_limited(text: str) -> bool:
        if not text:
            return False
        low = text.lower()
        return (
            "rate limit" in low
            or "too many requests" in low
            or "overloaded" in low
            or "usage limit reached" in low
        )

    def _run_claude(self, prompt: str, model: str) -> str:
        cmd = [
            CLAUDE_PATH, "-p", prompt,
            "--no-session-persistence",
            "--model", model,
        ]
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=300,
            env=ENV,
            stdin=subprocess.DEVNULL,
        )
        return result.stdout or result.stderr or ""

    def _try_fallback(self, messages: list, system: str = "") -> str:
        """Attempt MiniMax fallback if configured. Returns response text or ''."""
        if not MINIMAX_FALLBACK_URL:
            return ""
        payload = {
            "messages": (
                ([{"role": "system", "content": system}] if system else []) + messages
            ),
            "model": "MiniMax-M2.5",
            "max_tokens": 4096,
        }
        try:
            req = Request(
                MINIMAX_FALLBACK_URL,
                data=json.dumps(payload).encode(),
                headers={
                    "Content-Type": "application/json",
                    **({"x-api-key": MINIMAX_FALLBACK_KEY} if MINIMAX_FALLBACK_KEY else {}),
                },
                method="POST",
            )
            with urlopen(req, timeout=120) as resp:
                data = json.loads(resp.read())
            # Try Anthropic-shape, then OpenAI-shape
            if "content" in data and isinstance(data["content"], list):
                texts = [b.get("text", "") for b in data["content"] if isinstance(b, dict)]
                return "\n".join(t for t in texts if t)
            if "choices" in data and data["choices"]:
                return data["choices"][0].get("message", {}).get("content", "")
            return ""
        except (HTTPError, URLError, TimeoutError, ValueError) as e:
            print(f"[claude-api] fallback failed: {e}", file=sys.stderr)
            return ""


class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main() -> None:
    print(
        f"[claude-api] starting on {BIND}:{PORT}\n"
        f"  claude binary: {CLAUDE_PATH}\n"
        f"  analytics dir: {ANALYTICS_DIR}\n"
        f"  fallback url: {MINIMAX_FALLBACK_URL or '(none)'}"
    )
    server = ThreadedHTTPServer((BIND, PORT), ClaudeHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[claude-api] shutting down")
        server.server_close()


if __name__ == "__main__":
    main()
