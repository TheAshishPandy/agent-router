#!/usr/bin/env python3
"""
MiniMax Bridge — Anthropic→OpenAI format translator for Claude Code CLI.

Accepts Anthropic /v1/messages requests (what Claude Code sends),
translates to OpenAI chat/completions format, routes to MiniMax
(directly or via the Agent Router swarm), then translates the
response back to Anthropic format.

Supports both streaming (SSE) and non-streaming.

Usage:
    python3 minimax-bridge.py [--port 8004] [--swarm] [--direct]
"""

import argparse
import json
import os
import sys
import time
import uuid
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

# ── Config ──────────────────────────────────────────────────────────

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(SCRIPT_DIR, "data")

# Load MiniMax API key — env first, then local data/config.json
MINIMAX_KEY = os.environ.get("MINIMAX_API_KEY", "")
_local_cfg = os.path.join(DATA_DIR, "config.json")
if not MINIMAX_KEY and os.path.isfile(_local_cfg):
    try:
        with open(_local_cfg) as f:
            cfg = json.load(f)
        providers = cfg.get("models", {}).get("providers", {}) or cfg.get("providers", {})
        MINIMAX_KEY = providers.get("minimax", {}).get("apiKey", "")
    except Exception:
        pass

# Endpoints (overridable via env)
MINIMAX_DIRECT = os.environ.get("MINIMAX_DIRECT_URL",
                                "https://api.minimaxi.chat/v1/chat/completions")
SWARM_ENDPOINT = os.environ.get("AGENT_ROUTER_URL",
                                "http://127.0.0.1:8001/api/minimax")

# Proxy API key for swarm access — env first, then any key in proxy-keys.json
PROXY_KEY = os.environ.get("AGENT_ROUTER_KEY", "")
proxy_keys_path = os.path.join(DATA_DIR, "proxy-keys.json")
if not PROXY_KEY and os.path.isfile(proxy_keys_path):
    try:
        with open(proxy_keys_path) as f:
            pk = json.load(f)
        keys = pk.get("keys", {})
        # Prefer a key labeled 'local' or 'bridge'; else first available.
        for label in ("local", "bridge", "default"):
            if keys.get(label, {}).get("key"):
                PROXY_KEY = keys[label]["key"]
                break
        if not PROXY_KEY and keys:
            PROXY_KEY = next(iter(keys.values())).get("key", "")
    except Exception:
        pass

# Mode: "swarm" routes through the Agent Router's 3-worker swarm,
#        "direct" hits MiniMax API directly
MODE = "swarm"


# ── Format Translation ──────────────────────────────────────────────

def anthropic_to_openai(payload: dict) -> dict:
    """Convert Anthropic /v1/messages format to OpenAI chat/completions."""
    messages = []

    # System prompt
    system = payload.get("system", "")
    if isinstance(system, list):
        # Anthropic system can be a list of content blocks
        parts = [b.get("text", "") for b in system if isinstance(b, dict)]
        system = "\n".join(parts)
    if system:
        messages.append({"role": "system", "content": system})

    # Messages
    for msg in payload.get("messages", []):
        role = msg.get("role", "user")
        content = msg.get("content", "")

        # Anthropic uses content blocks: [{"type": "text", "text": "..."}]
        if isinstance(content, list):
            text_parts = []
            for block in content:
                if isinstance(block, dict):
                    if block.get("type") == "text":
                        text_parts.append(block.get("text", ""))
                    elif block.get("type") == "tool_result":
                        # Flatten tool results to text
                        tc = block.get("content", "")
                        if isinstance(tc, list):
                            tc = "\n".join(
                                b.get("text", "") for b in tc
                                if isinstance(b, dict) and b.get("type") == "text"
                            )
                        text_parts.append(f"[Tool Result: {tc}]")
                    elif block.get("type") == "tool_use":
                        text_parts.append(
                            f"[Tool Call: {block.get('name', '')}({json.dumps(block.get('input', {}))})]"
                        )
                elif isinstance(block, str):
                    text_parts.append(block)
            content = "\n".join(text_parts)

        messages.append({"role": role, "content": content})

    result = {
        "model": "MiniMax-M2.5",
        "messages": messages,
        "max_tokens": min(payload.get("max_tokens", 4096), 8192),
    }

    if payload.get("temperature") is not None:
        result["temperature"] = payload["temperature"]
    if payload.get("top_p") is not None:
        result["top_p"] = payload["top_p"]
    if payload.get("stream"):
        result["stream"] = True

    # Explicitly do NOT pass: thinking, context_management, tool_choice,
    # metadata, tools, stop_sequences — these are Claude-specific
    return result


def openai_to_anthropic(openai_resp: dict, model: str = "minimax-m2.5", has_thinking: bool = False) -> dict:
    """Convert OpenAI chat/completions response to Anthropic /v1/messages format."""
    choice = (openai_resp.get("choices") or [{}])[0]
    message = choice.get("message", {})
    content_text = message.get("content", "")
    usage = openai_resp.get("usage", {})

    content = []
    if has_thinking:
        content.append({"type": "thinking", "thinking": "(MiniMax M2.5 — thinking not supported)"})
    content.append({"type": "text", "text": content_text})

    return {
        "id": f"msg_{uuid.uuid4().hex[:24]}",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": content,
        "stop_reason": _map_finish_reason(choice.get("finish_reason", "stop")),
        "stop_sequence": None,
        "usage": {
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
        },
    }


def _map_finish_reason(reason: str) -> str:
    mapping = {
        "stop": "end_turn",
        "length": "max_tokens",
        "content_filter": "end_turn",
    }
    return mapping.get(reason, "end_turn")


def openai_stream_chunk_to_anthropic_events(chunk_data: dict, index: int, has_thinking: bool = False) -> list:
    """Convert a single OpenAI streaming chunk to Anthropic SSE events.

    When has_thinking=True, emits a minimal thinking block first (index 0)
    and the text block at index 1, matching what Claude Code expects with
    extended thinking enabled.
    """
    events = []
    choice = (chunk_data.get("choices") or [{}])[0]
    delta = choice.get("delta", {})
    finish = choice.get("finish_reason")

    text_idx = 1 if has_thinking else 0

    if index == 0:
        # First chunk: send message_start
        events.append({
            "type": "message_start",
            "message": {
                "id": f"msg_{uuid.uuid4().hex[:24]}",
                "type": "message",
                "role": "assistant",
                "model": "minimax-m2.5",
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": 0, "output_tokens": 0},
            },
        })

        if has_thinking:
            # Emit a minimal thinking block (required by Claude Code)
            events.append({
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "thinking", "thinking": ""},
            })
            events.append({
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "thinking_delta", "thinking": "(MiniMax M2.5 — thinking not supported)"},
            })
            events.append({"type": "content_block_stop", "index": 0})

        # Start text content block
        events.append({
            "type": "content_block_start",
            "index": text_idx,
            "content_block": {"type": "text", "text": ""},
        })

    text = delta.get("content", "")
    if text:
        events.append({
            "type": "content_block_delta",
            "index": text_idx,
            "delta": {"type": "text_delta", "text": text},
        })

    if finish:
        events.append({"type": "content_block_stop", "index": text_idx})
        events.append({
            "type": "message_delta",
            "delta": {"stop_reason": _map_finish_reason(finish), "stop_sequence": None},
            "usage": {"output_tokens": 0},
        })
        events.append({"type": "message_stop"})

    return events


# ── HTTP Handler ────────────────────────────────────────────────────

class BridgeHandler(BaseHTTPRequestHandler):
    """Handles Anthropic-format requests, translates, routes to MiniMax."""
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        # Log all incoming requests for debugging
        pass

    def log_request(self, code='-', size='-'):
        sys.stdout.write(f"[bridge] {self.command} {self.path} → {code}\n")
        sys.stdout.flush()

    def _clean_path(self):
        """Strip query string from path for routing."""
        return self.path.split("?")[0]

    def do_POST(self):
        clean = self._clean_path()
        sys.stdout.write(f"[bridge] POST {self.path}\n")
        sys.stdout.flush()
        if clean == "/v1/messages":
            self._handle_messages()
        elif clean == "/v1/messages/count_tokens":
            # Claude Code calls this for token counting — return a dummy
            try:
                length = int(self.headers.get("Content-Length", 0))
                self.rfile.read(length)
            except Exception:
                pass
            self._send_json(200, {"input_tokens": 100})
        elif self.path == "/v1/messages/batches":
            try:
                length = int(self.headers.get("Content-Length", 0))
                self.rfile.read(length)
            except Exception:
                pass
            self._send_json(200, {"results": []})
        else:
            sys.stdout.write(f"[bridge] UNKNOWN POST {self.path}\n")
            sys.stdout.flush()
            self._send_json(404, {"type": "error", "error": {"type": "not_found_error", "message": f"Not found: {self.path}"}})

    def do_GET(self):
        clean = self._clean_path()
        sys.stdout.write(f"[bridge] GET {self.path}\n")
        sys.stdout.flush()
        if clean == "/health":
            self._send_json(200, {"status": "ok", "mode": MODE, "model": "MiniMax-M2.5"})
        elif clean == "/v1/models" or clean.startswith("/v1/models"):
            self._send_json(200, {
                "data": [
                    {"id": "claude-opus-4-6", "display_name": "MiniMax M2.5 (via Opus alias)", "created_at": "2025-01-01T00:00:00Z"},
                    {"id": "claude-sonnet-4-20250514", "display_name": "MiniMax M2.5 (via Sonnet alias)", "created_at": "2025-01-01T00:00:00Z"},
                    {"id": "claude-sonnet-4-6", "display_name": "MiniMax M2.5 (via Sonnet alias)", "created_at": "2025-01-01T00:00:00Z"},
                    {"id": "claude-haiku-4-5-20251001", "display_name": "MiniMax M2.5 (via Haiku alias)", "created_at": "2025-01-01T00:00:00Z"},
                ],
                "has_more": False,
                "first_id": "claude-opus-4-6",
                "last_id": "claude-haiku-4-5-20251001",
            })
        else:
            self._send_json(404, {"type": "error", "error": {"type": "not_found_error", "message": f"Not found: {self.path}"}})

    def _handle_messages(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length)) if length else {}
        except Exception as e:
            self._send_json(400, {"type": "error", "error": {"type": "invalid_request_error", "message": str(e)}})
            return

        is_stream = body.get("stream", False)
        has_thinking = bool(body.get("thinking"))
        openai_payload = anthropic_to_openai(body)

        if is_stream:
            self._handle_stream(openai_payload, has_thinking=has_thinking)
        else:
            self._handle_sync(openai_payload, has_thinking=has_thinking)

    def _handle_sync(self, payload: dict, has_thinking: bool = False):
        start = time.time()
        payload.pop("stream", None)

        try:
            if MODE == "swarm":
                url = SWARM_ENDPOINT
                headers = {"Content-Type": "application/json"}
                if PROXY_KEY:
                    headers["x-api-key"] = PROXY_KEY
            else:
                url = MINIMAX_DIRECT
                headers = {
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {MINIMAX_KEY}",
                }

            req = Request(url, data=json.dumps(payload).encode(), headers=headers)
            with urlopen(req, timeout=120) as resp:
                result = json.loads(resp.read().decode())

            anthropic_resp = openai_to_anthropic(result, has_thinking=has_thinking)
            elapsed = time.time() - start
            print(f"[bridge] /v1/messages → MiniMax ({MODE}) {elapsed:.1f}s")
            self._send_json(200, anthropic_resp)

        except HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")[:500]
            print(f"[bridge] MiniMax error {e.code}: {detail}")
            self._send_json(e.code, {
                "type": "error",
                "error": {"type": "api_error", "message": f"MiniMax upstream: {detail}"},
            })
        except Exception as e:
            print(f"[bridge] Error: {e}")
            self._send_json(502, {
                "type": "error",
                "error": {"type": "api_error", "message": str(e)},
            })

    def _handle_stream(self, payload: dict, has_thinking: bool = False):
        """Stream: call MiniMax directly with stream=true, translate SSE chunks."""
        payload["stream"] = True
        start = time.time()

        try:
            # Streaming always goes direct (swarm doesn't support streaming translation)
            url = MINIMAX_DIRECT
            headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {MINIMAX_KEY}",
            }

            req = Request(url, data=json.dumps(payload).encode(), headers=headers)
            resp = urlopen(req, timeout=120)

            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()

            chunk_index = 0
            buffer = b""

            while True:
                # Read small chunks for low latency
                data = resp.read(4096)
                if not data:
                    break
                buffer += data

                # Process complete lines
                while b"\n" in buffer:
                    line_bytes, buffer = buffer.split(b"\n", 1)
                    line = line_bytes.decode("utf-8", errors="replace").strip()

                    if not line.startswith("data: "):
                        continue

                    data_str = line[6:]
                    if data_str == "[DONE]":
                        # Only send message_stop if we haven't sent it via finish_reason
                        continue

                    try:
                        chunk = json.loads(data_str)
                    except json.JSONDecodeError:
                        continue

                    events = openai_stream_chunk_to_anthropic_events(chunk, chunk_index, has_thinking=has_thinking)
                    chunk_index += 1

                    for evt in events:
                        evt_type = evt.get("type", "content_block_delta")
                        sse_line = f"event: {evt_type}\ndata: {json.dumps(evt)}\n\n"
                        self.wfile.write(sse_line.encode())
                    self.wfile.flush()

            resp.close()
            # Signal end of stream by closing the connection
            try:
                self.wfile.flush()
                self.wfile.close()
            except Exception:
                pass
            elapsed = time.time() - start
            sys.stdout.write(f"[bridge] /v1/messages stream → MiniMax (direct) {elapsed:.1f}s\n")
            sys.stdout.flush()

        except Exception as e:
            import traceback
            sys.stdout.write(f"[bridge] Stream error: {traceback.format_exc()}\n")
            sys.stdout.flush()
            try:
                error_event = {
                    "type": "error",
                    "error": {"type": "api_error", "message": str(e)},
                }
                self.wfile.write(f"event: error\ndata: {json.dumps(error_event)}\n\n".encode())
                self.wfile.flush()
            except Exception:
                pass

    def _send_json(self, code: int, data: dict):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


# ── Main ────────────────────────────────────────────────────────────

def main():
    global MODE

    parser = argparse.ArgumentParser(description="MiniMax Bridge for Claude Code")
    parser.add_argument("--port", type=int, default=8004, help="Port (default: 8004)")
    parser.add_argument("--swarm", action="store_true", help="Route through Agent Router swarm (default)")
    parser.add_argument("--direct", action="store_true", help="Route directly to MiniMax API")
    args = parser.parse_args()

    if args.direct:
        MODE = "direct"
    else:
        MODE = "swarm"

    if not MINIMAX_KEY and MODE == "direct":
        print("[bridge] ERROR: No MiniMax API key found")
        sys.exit(1)

    server = ThreadedHTTPServer(("127.0.0.1", args.port), BridgeHandler)
    print(f"[bridge] MiniMax Bridge started on http://127.0.0.1:{args.port}")
    print(f"[bridge] Mode: {MODE} | Model: MiniMax-M2.5")
    print(f"[bridge] Upstream: {SWARM_ENDPOINT if MODE == 'swarm' else MINIMAX_DIRECT}")
    print(f"[bridge] Set ANTHROPIC_BASE_URL=http://127.0.0.1:{args.port}")
    print()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[bridge] Shutting down...")
        server.shutdown()


if __name__ == "__main__":
    main()
