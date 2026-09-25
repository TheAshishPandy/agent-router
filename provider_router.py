"""provider_router.py - free/local provider cascade for Agent Router.

Chain: Ollama -> Hugging Face -> OpenRouter.
Anthropic /v1/messages outside, OpenAI /chat/completions upstream.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
from typing import AsyncIterator, Optional

import httpx
from fastapi import HTTPException
from fastapi.responses import JSONResponse, StreamingResponse

from platform_config import cfg

logger = logging.getLogger("provider-router")

OLLAMA_TARGET     = cfg.get("upstream", {}).get("ollama",      "http://127.0.0.1:11434/v1")
HF_TARGET         = cfg.get("upstream", {}).get("huggingface", "https://router.huggingface.co/v1")
OPENROUTER_TARGET = cfg.get("upstream", {}).get("openrouter",  "https://openrouter.ai/api/v1")

_pcfg = cfg.get("providers", {})
OLLAMA_MODEL     = os.environ.get("OLLAMA_MODEL")     or _pcfg.get("ollama",      {}).get("default_model", "qwen2.5-coder:14b")
HF_MODEL         = os.environ.get("HF_MODEL")         or _pcfg.get("huggingface", {}).get("default_model", "deepseek-ai/DeepSeek-V4.1-Flash")
OPENROUTER_MODEL = os.environ.get("OPENROUTER_MODEL") or _pcfg.get("openrouter",  {}).get("default_model", "openrouter/free")

HF_TOKEN       = os.environ.get("HF_TOKEN", "")
OPENROUTER_KEY = os.environ.get("OPENROUTER_API_KEY", "")

_FREE_PROVIDER_CHAIN = cfg.get("cascade", {}).get("chain", ["ollama", "huggingface", "openrouter"])
_PROVIDER_MODELS = {
    "ollama":      OLLAMA_MODEL,
    "huggingface": HF_MODEL,
    "openrouter":  OPENROUTER_MODEL,
}

_connect_timeout = cfg.get("timeouts", {}).get("connect", 10.0)
_timeouts = cfg.get("timeouts", {})


# Live routing telemetry for the operations dashboard.
_ROUTING_STATE = {
    "active": {},
    "recent": [],
    "provider_stats": {},
}
_ROUTING_LOCK = __import__("threading").Lock()

def _routing_event(req_id: str, provider: str, model: str, status: str, error: str = ""):
    now = __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(timespec="seconds")
    with _ROUTING_LOCK:
        if status in ("started", "attempt"):
            _ROUTING_STATE["active"][req_id] = {"provider": provider, "model": model, "status": "running", "started_at": now}
        else:
            _ROUTING_STATE["active"].pop(req_id, None)
            stats = _ROUTING_STATE["provider_stats"].setdefault(provider, {"requests": 0, "success": 0, "failed": 0})
            stats["requests"] += 1
            stats["success" if status == "success" else "failed"] += 1
            _ROUTING_STATE["recent"].append({"ts": now, "request_id": req_id, "provider": provider, "model": model, "status": status, "error": error[:240]})
            _ROUTING_STATE["recent"] = _ROUTING_STATE["recent"][-100:]

def routing_snapshot() -> dict:
    with _ROUTING_LOCK:
        return json.loads(json.dumps(_ROUTING_STATE))

_clients = {
    "ollama": httpx.AsyncClient(timeout=httpx.Timeout(_timeouts.get("ollama", 180.0), connect=_connect_timeout)),
    "huggingface": httpx.AsyncClient(timeout=httpx.Timeout(_timeouts.get("huggingface", 180.0), connect=_connect_timeout)),
    "openrouter": httpx.AsyncClient(timeout=httpx.Timeout(_timeouts.get("openrouter", 180.0), connect=_connect_timeout)),
}
_TARGETS = {
    "ollama":      OLLAMA_TARGET,
    "huggingface": HF_TARGET,
    "openrouter":  OPENROUTER_TARGET,
}


def _provider_headers(provider: str) -> dict:
    h = {"Content-Type": "application/json"}
    if provider == "ollama":
        return h
    if provider == "huggingface":
        if not HF_TOKEN:
            raise RuntimeError("HF_TOKEN is not configured")
        h["Authorization"] = f"Bearer {HF_TOKEN}"
        return h
    if provider == "openrouter":
        if not OPENROUTER_KEY:
            raise RuntimeError("OPENROUTER_API_KEY is not configured")
        h["Authorization"] = f"Bearer {OPENROUTER_KEY}"
        return h
    raise RuntimeError(f"Unknown provider: {provider}")


def _text_from_anthropic_content(content) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return str(content)
    parts = []
    for b in content:
        if not isinstance(b, dict):
            continue
        t = b.get("type")
        if t == "text":
            parts.append(b.get("text", ""))
        elif t == "tool_result":
            inner = b.get("content", b.get("output", ""))
            parts.append(inner if isinstance(inner, str) else json.dumps(inner))
    return "\n".join(p for p in parts if p)


def _anthropic_to_openai_messages(payload: dict) -> list:
    out = []
    system = payload.get("system", "")
    if system:
        out.append({"role": "system", "content": _text_from_anthropic_content(system)})

    for msg in payload.get("messages", []):
        role = msg.get("role", "user")
        content = msg.get("content", "")

        if isinstance(content, str):
            if content:
                out.append({"role": role, "content": content})
            continue
        if not isinstance(content, list):
            continue

        text_parts, images, tool_calls, tool_results = [], [], [], []
        for b in content:
            if not isinstance(b, dict):
                continue
            t = b.get("type")
            if t == "text":
                text_parts.append(b.get("text", ""))
            elif t == "image":
                src = b.get("source", {})
                if src.get("type") == "base64":
                    mt = src.get("media_type", "image/png")
                    images.append({"type": "image_url",
                                   "image_url": {"url": f"data:{mt};base64,{src.get('data','')}"}})
            elif t == "tool_use":
                tool_calls.append({
                    "id": b.get("id", f"call_{secrets.token_hex(6)}"),
                    "type": "function",
                    "function": {"name": b.get("name", ""),
                                 "arguments": json.dumps(b.get("input", {}))},
                })
            elif t == "tool_result":
                inner = b.get("content", b.get("output", ""))
                tool_results.append({
                    "role": "tool",
                    "tool_call_id": b.get("tool_use_id", ""),
                    "content": inner if isinstance(inner, str) else json.dumps(inner),
                })

        if text_parts or images or tool_calls:
            if images:
                parts = [{"type": "text", "text": "\n".join(text_parts)}] if text_parts else []
                parts.extend(images)
                content_val = parts
            else:
                content_val = "\n".join(text_parts) if text_parts else None

            entry = {"role": role}
            if content_val is not None:
                entry["content"] = content_val
            if tool_calls:
                entry["tool_calls"] = tool_calls
                entry.setdefault("content", None)
            out.append(entry)

        out.extend(tool_results)

    return out


def _anthropic_tools_to_openai(payload: dict) -> list:
    tools = []
    for tool in payload.get("tools", []) or []:
        if not isinstance(tool, dict):
            continue
        tools.append({
            "type": "function",
            "function": {
                "name": tool.get("name", ""),
                "description": tool.get("description", ""),
                "parameters": tool.get("input_schema",
                                       {"type": "object", "properties": {}}),
            },
        })
    return tools


def _build_openai_request(payload: dict, provider: str, stream: bool) -> dict:
    body = {
        "model": _PROVIDER_MODELS[provider],
        "messages": _anthropic_to_openai_messages(payload),
    }
    if payload.get("max_tokens") is not None:
        body["max_tokens"] = payload["max_tokens"]
    if payload.get("temperature") is not None:
        body["temperature"] = payload["temperature"]
    if payload.get("top_p") is not None:
        body["top_p"] = payload["top_p"]

    tools = _anthropic_tools_to_openai(payload)
    if tools:
        body["tools"] = tools
        body["tool_choice"] = "auto"

    if stream:
        body["stream"] = True
    return body


def _openai_to_anthropic_response(result: dict, provider: str) -> dict:
    choices = result.get("choices", [])
    if not choices:
        raise RuntimeError(f"{provider} returned no choices")

    choice = choices[0]
    message = choice.get("message", {})
    content = []

    if message.get("content"):
        content.append({"type": "text", "text": message["content"]})

    for tc in message.get("tool_calls", []) or []:
        fn = tc.get("function", {})
        args = fn.get("arguments", "{}")
        try:
            args = json.loads(args)
        except Exception:
            pass
        content.append({
            "type": "tool_use",
            "id": tc.get("id", f"toolu_{secrets.token_hex(8)}"),
            "name": fn.get("name", ""),
            "input": args,
        })

    stop_reason = {
        "stop": "end_turn",
        "length": "max_tokens",
        "tool_calls": "tool_use",
        "function_call": "tool_use",
    }.get(choice.get("finish_reason"), "end_turn")

    usage = result.get("usage", {}) or {}
    return {
        "id": result.get("id", f"msg_router_{secrets.token_hex(8)}"),
        "type": "message",
        "role": "assistant",
        "content": content,
        "model": _PROVIDER_MODELS[provider],
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
        },
    }


async def _call_free_provider(provider: str, payload: dict, req_id: str = "") -> dict:
    client = _clients[provider]
    target = _TARGETS[provider].rstrip("/")
    body = _build_openai_request(payload, provider, stream=False)
    headers = _provider_headers(provider)

    _routing_event(req_id, provider, body["model"], "attempt")
    logger.info(f"FREE_PROVIDER_REQUEST provider={provider} model={body['model']} req_id={req_id}")
    resp = await client.post(f"{target}/chat/completions", json=body, headers=headers)
    if resp.status_code >= 400:
        raise HTTPException(status_code=resp.status_code,
                            detail=f"{provider} error: {resp.text[:1000]}")
    return _openai_to_anthropic_response(resp.json(), provider)


def _sse(event: str, data: dict) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n".encode()


async def _stream_free_provider(provider: str, payload: dict, req_id: str = "") -> StreamingResponse:
    client = _clients[provider]
    target = _TARGETS[provider].rstrip("/")
    body = _build_openai_request(payload, provider, stream=True)
    headers = _provider_headers(provider)

    msg_id = f"msg_{secrets.token_hex(12)}"
    model_name = _PROVIDER_MODELS[provider]

    async def event_stream() -> AsyncIterator[bytes]:
        yield _sse("message_start", {
            "type": "message_start",
            "message": {
                "id": msg_id, "type": "message", "role": "assistant",
                "model": model_name, "content": [],
                "stop_reason": None, "stop_sequence": None,
                "usage": {"input_tokens": 0, "output_tokens": 0},
            },
        })
        yield _sse("content_block_start", {
            "type": "content_block_start", "index": 0,
            "content_block": {"type": "text", "text": ""},
        })

        input_tokens = output_tokens = 0
        stop_reason = "end_turn"
        block_open = True

        try:
            async with client.stream("POST", f"{target}/chat/completions",
                                     json=body, headers=headers) as resp:
                if resp.status_code >= 400:
                    err = (await resp.aread()).decode("utf-8", "replace")[:1000]
                    raise RuntimeError(err)

                async for line in resp.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except Exception:
                        continue

                    usage = chunk.get("usage") or {}
                    if usage:
                        input_tokens = usage.get("prompt_tokens", input_tokens)
                        output_tokens = usage.get("completion_tokens", output_tokens)

                    for choice in chunk.get("choices", []):
                        delta = choice.get("delta", {}) or {}
                        text = delta.get("content")
                        if text:
                            yield _sse("content_block_delta", {
                                "type": "content_block_delta", "index": 0,
                                "delta": {"type": "text_delta", "text": text},
                            })
                        fr = choice.get("finish_reason")
                        if fr:
                            stop_reason = {
                                "stop": "end_turn", "length": "max_tokens",
                                "tool_calls": "tool_use",
                            }.get(fr, "end_turn")

        except Exception as exc:
            logger.exception(f"STREAM_PROVIDER_FAILED provider={provider} req_id={req_id}")
            yield _sse("error", {
                "type": "error",
                "error": {"type": "upstream_error",
                          "message": f"{provider}: {type(exc).__name__}"},
            })
            return

        if block_open:
            yield _sse("content_block_stop", {"type": "content_block_stop", "index": 0})

        yield _sse("message_delta", {
            "type": "message_delta",
            "delta": {"stop_reason": stop_reason, "stop_sequence": None},
            "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
        })
        yield _sse("message_stop", {"type": "message_stop"})

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive",
                 "X-Accel-Buffering": "no"},
    )


async def cascade_request(payload: dict, req_id: str = "") -> Optional[JSONResponse]:
    """Try each provider in order (non-streaming). Returns None if all fail."""
    errors = []
    for provider in _FREE_PROVIDER_CHAIN:
        if provider == "huggingface" and not HF_TOKEN:
            errors.append("huggingface: HF_TOKEN missing")
            continue
        if provider == "openrouter" and not OPENROUTER_KEY:
            errors.append("openrouter: OPENROUTER_API_KEY missing")
            continue
        try:
            logger.info(f"CASCADE_ATTEMPT provider={provider} req_id={req_id}")
            result = await _call_free_provider(provider, payload, req_id=req_id)
            _routing_event(req_id, provider, _PROVIDER_MODELS[provider], "success")
            logger.info(f"CASCADE_SUCCESS provider={provider} req_id={req_id}")
            return JSONResponse(content=result, headers={"X-Cascade": provider})
        except Exception as exc:
            err = f"{provider}: {type(exc).__name__}: {str(exc)[:300]}"
            _routing_event(req_id, provider, _PROVIDER_MODELS[provider], "failed", err)
            errors.append(err)
            logger.warning(f"CASCADE_FAILED {err} req_id={req_id}")
    logger.error(f"CASCADE_EXHAUSTED req_id={req_id} errors={errors}")
    return None


async def cascade_stream(payload: dict, req_id: str = "") -> Optional[StreamingResponse]:
    """Return a streaming response from the first provider that accepts, or None."""
    for provider in _FREE_PROVIDER_CHAIN:
        if provider == "huggingface" and not HF_TOKEN:
            continue
        if provider == "openrouter" and not OPENROUTER_KEY:
            continue
        try:
            logger.info(f"CASCADE_STREAM_ATTEMPT provider={provider} req_id={req_id}")
            resp = await _stream_free_provider(provider, payload, req_id=req_id)
            _routing_event(req_id, provider, _PROVIDER_MODELS[provider], "success")
            return resp
        except Exception as exc:
            _routing_event(req_id, provider, _PROVIDER_MODELS[provider], "failed", str(exc))
            logger.warning(f"CASCADE_STREAM_FAILED {provider}: {type(exc).__name__}: {exc}")
            continue
    return None
