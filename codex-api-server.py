#!/usr/bin/env python3
"""
Codex API Server — wraps the OpenAI `codex` CLI as a local HTTP API.

Lets you use a ChatGPT subscription (the `codex` CLI binary) as if it were
the Anthropic API. The proxy in front of it (proxy.py) layers auth, swarm,
cascade, etc. This server is upstream-only — bind to 127.0.0.1.

Endpoints
---------
POST /v1/messages   Anthropic-compatible (so Claude Code can target it directly)
POST /prompt        Simple {"prompt": "..."} → default model
GET  /health        Liveness probe
GET  /stats         Local analytics

Configuration
-------------
CODEX_PATH          Path to `codex` binary (auto-detected if unset)
CODEX_PORT          Listen port (default 8006)
CODEX_BIND          Bind host (default 127.0.0.1 — DO NOT bind 0.0.0.0)
CODEX_DEFAULT_MODEL Default model (default gpt-5)
ANALYTICS_DIR       Daily analytics dir (default ~/.agent-router/analytics)
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

# ── Locate `codex` CLI ────────────────────────────────────────────────
CODEX_PATH = os.environ.get("CODEX_PATH") or shutil.which("codex") or ""
if not CODEX_PATH:
    for _candidate in (
        os.path.expanduser("~/.local/bin/codex"),
        "/usr/local/bin/codex",
        "/opt/homebrew/bin/codex",
    ):
        if os.path.isfile(_candidate):
            CODEX_PATH = _candidate
            break
if not CODEX_PATH:
    print(
        "[codex-api] FATAL: `codex` CLI not found. "
        "Install OpenAI Codex CLI or set CODEX_PATH=/path/to/codex",
        file=sys.stderr,
    )
    sys.exit(1)

PORT = int(os.environ.get("CODEX_PORT", "8006"))
BIND = os.environ.get("CODEX_BIND", "127.0.0.1")
DEFAULT_MODEL = os.environ.get("CODEX_DEFAULT_MODEL", "gpt-5")

if BIND not in ("127.0.0.1", "localhost", "::1"):
    print(
        f"[codex-api] WARNING: binding to {BIND}. This server has no auth — "
        "put it behind the proxy. Loopback bind strongly recommended.",
        file=sys.stderr,
    )

ANALYTICS_DIR = Path(
    os.environ.get("ANALYTICS_DIR")
    or os.path.expanduser("~/.agent-router/analytics")
)
ANALYTICS_DIR.mkdir(parents=True, exist_ok=True)

ENV = {k: v for k, v in os.environ.items()}


def _atomic_write_json(path: Path, data: dict, indent: int = 2) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}.{int(time.time()*1000)}")
    with open(tmp, "w") as f:
        json.dump(data, f, indent=indent)
    os.replace(tmp, path)


# ── Anthropic message flattening (re-used for Claude-Code compat) ────

def flatten_message_content(msg: dict) -> str:
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
    lines = []
    if system:
        lines.append(f"<system>\n{system}\n</system>")
    for msg in messages:
        role = msg.get("role", "user").upper()
        text = flatten_message_content(msg)
        if text:
            lines.append(f"{role}: {text}")
    return "\n\n".join(lines)


class Analytics:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.server_start = time.time()
        self.requests = {"total": 0, "success": 0, "errors": 0, "timeouts": 0}
        self.by_model: dict = {}
        self.by_endpoint: dict = {}
        self.by_hour: dict = {}
        self.recent: list = []
        self._load_today()

    def _today_file(self) -> Path:
        return ANALYTICS_DIR / f"codex-{datetime.now(timezone.utc).strftime('%Y-%m-%d')}.json"

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
                print(f"[codex-api] failed to load analytics: {e}", file=sys.stderr)

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
            print(f"[codex-api] persist error: {e}", file=sys.stderr)

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
                "ts": now.isoformat(), "endpoint": endpoint, "model": model,
                "elapsed": round(elapsed, 3), "status": status,
                "input_tokens": input_tokens, "output_tokens": output_tokens,
            })
            del self.recent[:-50]
            self._persist()

    def summary(self) -> dict:
        with self._lock:
            return {
                "uptime_seconds": round(time.time() - self.server_start, 1),
                "requests": dict(self.requests),
                "by_model": dict(self.by_model),
                "by_endpoint": dict(self.by_endpoint),
                "by_hour": dict(self.by_hour),
            }


analytics = Analytics()


class CodexHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
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

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers",
                         "Content-Type, anthropic-version, x-agent-id, x-source")
        self.end_headers()

    def do_GET(self):
        if self.path == "/health":
            return self._send_json(200, {"ok": True, "codex_path": CODEX_PATH})
        if self.path.startswith("/stats"):
            return self._send_json(200, analytics.summary())
        return self._send_json(404, {"error": "not_found", "path": self.path})

    def do_POST(self):
        if self.path == "/v1/messages":
            return self._handle_anthropic()
        if self.path == "/prompt":
            return self._handle_prompt()
        return self._send_json(404, {"error": "not_found", "path": self.path})

    # ── Handlers ─────────────────────────────────────────────────────

    def _handle_prompt(self):
        start = time.time()
        try:
            body = self._read_json()
        except Exception as e:
            return self._send_json(400, {"error": "invalid_json", "detail": str(e)})
        prompt = (body or {}).get("prompt", "")
        if not prompt:
            return self._send_json(400, {"error": "missing_prompt"})
        model = body.get("model") or DEFAULT_MODEL
        try:
            response = self._run_codex(prompt, model)
        except subprocess.TimeoutExpired:
            elapsed = time.time() - start
            analytics.record("/prompt", model, len(prompt) // 4, 0, elapsed, "timeout")
            return self._send_json(504, {"error": "timeout"})
        except Exception as e:
            elapsed = time.time() - start
            analytics.record("/prompt", model, len(prompt) // 4, 0, elapsed, "error")
            return self._send_json(500, {"error": "codex_cli_error", "detail": str(e)[:300]})

        elapsed = time.time() - start
        analytics.record("/prompt", model, len(prompt) // 4, len(response) // 4, elapsed, "success")
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
        model = body.get("model") or DEFAULT_MODEL
        prompt = build_prompt_from_messages(messages, system)

        try:
            response = self._run_codex(prompt, model)
        except subprocess.TimeoutExpired:
            elapsed = time.time() - start
            analytics.record("/v1/messages", model, len(prompt) // 4, 0, elapsed, "timeout")
            return self._send_json(504, {"error": "timeout"})
        except Exception as e:
            elapsed = time.time() - start
            analytics.record("/v1/messages", model, len(prompt) // 4, 0, elapsed, "error")
            return self._send_json(500, {"error": "codex_cli_error", "detail": str(e)[:300]})

        elapsed = time.time() - start
        in_tokens = len(prompt) // 4
        out_tokens = len(response) // 4
        analytics.record("/v1/messages", model, in_tokens, out_tokens, elapsed, "success")

        return self._send_json(200, {
            "id": f"msg_{uuid.uuid4().hex[:24]}",
            "type": "message",
            "role": "assistant",
            "model": model,
            "content": [{"type": "text", "text": response}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": in_tokens, "output_tokens": out_tokens},
        })

    def _run_codex(self, prompt: str, model: str) -> str:
        # `codex exec` runs a one-shot non-interactive prompt and prints the response.
        # The exact flag set varies by codex version; this matches the common shape.
        cmd = [CODEX_PATH, "exec", "--model", model, "--", prompt]
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=240,
                env=ENV, stdin=subprocess.DEVNULL,
            )
        except FileNotFoundError:
            # Fallback: older codex CLIs use positional prompt without exec.
            result = subprocess.run(
                [CODEX_PATH, "--model", model, prompt],
                capture_output=True, text=True, timeout=240,
                env=ENV, stdin=subprocess.DEVNULL,
            )
        return result.stdout or result.stderr or ""


class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main() -> None:
    print(
        f"[codex-api] starting on {BIND}:{PORT}\n"
        f"  codex binary: {CODEX_PATH}\n"
        f"  default model: {DEFAULT_MODEL}\n"
        f"  analytics dir: {ANALYTICS_DIR}"
    )
    server = ThreadedHTTPServer((BIND, PORT), CodexHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[codex-api] shutting down")
        server.server_close()


if __name__ == "__main__":
    main()
