from __future__ import annotations
from pathlib import Path

def discover_repositories(workspace: Path) -> list[Path]:
    if (workspace / ".git").is_dir(): return [workspace]
    repos=[]
    ignored={"node_modules",".venv","venv","bin","obj","__pycache__"}
    for git_dir in workspace.rglob(".git"):
        repo=git_dir.parent
        if not any(part in ignored for part in repo.parts): repos.append(repo)
    return sorted(set(repos), key=lambda p: str(p).lower())
