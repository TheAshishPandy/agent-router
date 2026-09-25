from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path


@dataclass
class OrchestratorConfig:
    workspace: Path
    state_file: Path
    log_dir: Path
    max_fix_attempts: int = 5
    auto_commit: bool = True
    auto_push: bool = False
    branch_prefix: str = "agent/auto"
    stop_on_blocked: bool = False
    agy_path: str = ""
    agy_model: str = "gemini-3.8-flash-medium"
    agy_effort: str = ""
    agy_print_timeout: str = "10m"
    agy_hard_timeout_seconds: int = 900
    agent_router_url: str = "http://127.0.0.1:8001/api"
    agent_router_model: str = "claude-sonnet-4-6"
    agent_router_max_turns: int = 30
    agent_router_timeout_seconds: int = 300

    @classmethod
    def from_file(cls, path: str | Path) -> "OrchestratorConfig":
        p = Path(path).resolve()
        data = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}

        workspace = Path(
            os.path.expandvars(data.get("workspace", str(Path.cwd().parent)))
        ).expanduser().resolve()

        state_raw = os.path.expandvars(
            data.get("state_file", str(p.parent / "orchestrator-state.json"))
        )
        state = Path(state_raw).expanduser()
        if not state.is_absolute():
            state = (p.parent / state).resolve()

        log_raw = os.path.expandvars(
            data.get("log_dir", str(p.parent / "logs"))
        )
        log_dir = Path(log_raw).expanduser()
        if not log_dir.is_absolute():
            log_dir = (p.parent / log_dir).resolve()

        return cls(
            workspace=workspace,
            state_file=state,
            log_dir=log_dir,
            max_fix_attempts=int(data.get("max_fix_attempts", 5)),
            auto_commit=bool(data.get("auto_commit", True)),
            auto_push=bool(data.get("auto_push", False)),
            branch_prefix=str(data.get("branch_prefix", "agent/auto")),
            stop_on_blocked=bool(data.get("stop_on_blocked", False)),
            agy_path=str(data.get("agy_path", "")),
            agy_model=str(
                data.get(
                    "agy_model",
                    os.environ.get("AGY_MODEL", "gemini-3.8-flash-medium"),
                )
            ),
            agy_effort=str(
                data.get("agy_effort", os.environ.get("AGY_EFFORT", ""))
            ),
            agy_print_timeout=str(data.get("agy_print_timeout", "10m")),
            agy_hard_timeout_seconds=int(
                data.get("agy_hard_timeout_seconds", 900)
            ),
            agent_router_url=str(
                data.get("agent_router_url", os.environ.get("AGENT_ROUTER_URL", "http://127.0.0.1:8001/api"))
            ),
            agent_router_model=str(
                data.get("agent_router_model", os.environ.get("AGENT_ROUTER_MODEL", "claude-sonnet-4-6"))
            ),
            agent_router_max_turns=int(data.get("agent_router_max_turns", 30)),
            agent_router_timeout_seconds=int(data.get("agent_router_timeout_seconds", 300)),
        )
