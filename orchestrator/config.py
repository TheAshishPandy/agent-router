from __future__ import annotations
import json, os
from dataclasses import dataclass, field
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

    @classmethod
    def from_file(cls, path: str | Path) -> "OrchestratorConfig":
        p = Path(path).resolve()
        data = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
        workspace = Path(os.path.expandvars(data.get("workspace", str(Path.cwd().parent)))).expanduser().resolve()
        state = Path(os.path.expandvars(data.get("state_file", str(p.parent / "orchestrator-state.json"))))
        log_dir = Path(os.path.expandvars(data.get("log_dir", str(p.parent / "logs"))))
        return cls(workspace, state, log_dir, int(data.get("max_fix_attempts", 5)), bool(data.get("auto_commit", True)), bool(data.get("auto_push", False)), str(data.get("branch_prefix", "agent/auto")), bool(data.get("stop_on_blocked", False)))
