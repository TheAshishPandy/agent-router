from __future__ import annotations

import json
import os
import subprocess
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


class AgentRouterError(RuntimeError):
    pass


def _api_key(repo: Path) -> str:
    for name in ("AGENT_ROUTER_API_KEY", "ROUTER_API_KEY"):
        value = os.environ.get(name)
        if value:
            return value

    key_file = repo / "data" / "proxy-keys.json"
    if key_file.exists():
        try:
            data = json.loads(key_file.read_text(encoding="utf-8"))
            keys = data.get("keys", {})
            for item in keys.values():
                if item.get("key"):
                    return str(item["key"])
        except (OSError, ValueError, TypeError):
            pass

    # Local trusted-network deployments may accept requests without a key.
    return ""


def _safe_path(repo: Path, relative: str) -> Path:
    target = (repo / relative).resolve()
    root = repo.resolve()
    if target != root and root not in target.parents:
        raise AgentRouterError(f"Path escapes repository: {relative}")
    return target


def _run_command(repo: Path, command: str, timeout: int = 120) -> str:
    allowed = ("git ", "python ", "python -m ", "pytest ", "dotnet ", "npm ", "node ")
    normalized = command.strip().lower()
    if not normalized.startswith(allowed):
        raise AgentRouterError(
            "Command is not allowed. Use git/python/pytest/dotnet/npm/node commands only."
        )
    lowered = normalized.replace(" ", "")
    forbidden = ("gitpush--force", "gitreset--hard", "rmdir", "format", "shutdown")
    if any(token in lowered for token in forbidden):
        raise AgentRouterError("Potentially destructive command rejected.")
    completed = subprocess.run(
        command,
        cwd=repo,
        shell=True,
        text=True,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    output = (completed.stdout + "\n" + completed.stderr).strip()
    return f"exit_code={completed.returncode}\n{output[-12000:]}"


TOOLS = [
    {
        "name": "list_files",
        "description": "List files and directories in a repository-relative path.",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string", "default": "."}},
        },
    },
    {
        "name": "read_file",
        "description": "Read a UTF-8 text file from the repository.",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
    {
        "name": "write_file",
        "description": "Create or replace a UTF-8 text file in the repository. Use only for files needed for the task.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "run_command",
        "description": "Run a safe repository command for inspection, building, or testing.",
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {"type": "string"},
                "timeout": {"type": "integer", "default": 120},
            },
            "required": ["command"],
        },
    },
    {
        "name": "git_diff",
        "description": "Show the current working-tree diff.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "git_status",
        "description": "Show concise Git status and current branch.",
        "input_schema": {"type": "object", "properties": {}},
    },
]


def _tool(repo: Path, name: str, args: dict[str, Any]) -> str:
    if name == "list_files":
        path = _safe_path(repo, args.get("path", "."))
        if not path.is_dir():
            return f"Not a directory: {args.get('path', '.')}"
        rows = []
        for item in sorted(path.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))[:300]:
            rows.append(f"{'[DIR] ' if item.is_dir() else '      '}{item.name}")
        return "\n".join(rows) or "(empty)"

    if name == "read_file":
        path = _safe_path(repo, args["path"])
        if not path.is_file():
            return f"File not found: {args['path']}"
        return path.read_text(encoding="utf-8", errors="replace")[-30000:]

    if name == "write_file":
        path = _safe_path(repo, args["path"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(args["content"], encoding="utf-8")
        return f"Wrote {args['path']}"

    if name == "run_command":
        return _run_command(repo, args["command"], int(args.get("timeout", 120)))

    if name == "git_diff":
        return _run_command(repo, "git diff -- .", 60)

    if name == "git_status":
        return _run_command(repo, "git status --short --branch", 60)

    raise AgentRouterError(f"Unknown tool: {name}")


def run_agent_router(
    repo: Path,
    base_url: str,
    model: str,
    task: str,
    max_turns: int = 30,
    timeout: int = 300,
) -> dict[str, Any]:
    key = _api_key(repo)
    messages: list[dict[str, Any]] = [{"role": "user", "content": task}]
    last_provider = ""
    final_text = ""

    for _ in range(max_turns):
        payload = {
            "model": model,
            "max_tokens": 8192,
            "messages": messages,
            "tools": TOOLS,
        }
        request = urllib.request.Request(
            base_url.rstrip("/") + "/v1/messages",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                **({"x-api-key": key} if key else {}),
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read().decode("utf-8", errors="replace")
                last_provider = response.headers.get("x-cascade", "")
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise AgentRouterError(
                f"Agent Router HTTP {exc.code}: {body[-4000:]}"
            ) from exc
        except (OSError, TimeoutError) as exc:
            raise AgentRouterError(f"Agent Router connection failed: {exc}") from exc

        try:
            data = json.loads(raw)
        except ValueError as exc:
            raise AgentRouterError(f"Agent Router returned invalid JSON: {raw[:2000]}") from exc

        content = data.get("content", [])
        messages.append({"role": "assistant", "content": content})

        tool_uses = [block for block in content if block.get("type") == "tool_use"]
        texts = [block.get("text", "") for block in content if block.get("type") == "text"]
        if texts:
            final_text += "\n".join(texts)

        if not tool_uses:
            return {
                "success": data.get("stop_reason", "end_turn") in ("end_turn", "stop"),
                "status": data.get("stop_reason", "end_turn"),
                "response": final_text.strip(),
                "provider": last_provider,
                "model": data.get("model", model),
                "usage": data.get("usage", {}),
            }

        results = []
        for call in tool_uses:
            try:
                result = _tool(repo, call["name"], call.get("input", {}))
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": call["id"],
                        "content": result,
                    }
                )
            except Exception as exc:
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": call["id"],
                        "is_error": True,
                        "content": str(exc),
                    }
                )
        messages.append({"role": "user", "content": results})

    return {
        "success": False,
        "status": "max_turns",
        "response": final_text.strip(),
        "provider": last_provider,
        "model": model,
        "usage": {},
    }
