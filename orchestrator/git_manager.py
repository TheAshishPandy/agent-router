from __future__ import annotations

import subprocess
from pathlib import Path


class GitError(RuntimeError):
    pass


def run_git(repo: Path, *args: str) -> str:
    p = subprocess.run(["git", *args], cwd=repo, text=True, capture_output=True)
    if p.returncode:
        raise GitError(p.stderr.strip() or p.stdout.strip() or "git command failed")
    return p.stdout.strip()


def status(repo: Path) -> str:
    return run_git(repo, "status", "--short")


def branch(repo: Path) -> str:
    return run_git(repo, "branch", "--show-current")


def ensure_work_branch(repo: Path, prefix: str) -> str:
    current = branch(repo)
    name = f"{prefix}/{repo.name}".replace(" ", "-")

    if current == name:
        return name

    if status(repo):
        raise GitError(
            f"Refusing to switch/create autonomous branch for {repo}: "
            "working tree is not clean. Commit or stash existing changes first."
        )

    if run_git(repo, "branch", "--list", name).strip():
        run_git(repo, "switch", name)
    else:
        run_git(repo, "switch", "-c", name)

    return name


def commit(repo: Path, message: str) -> str:
    if not status(repo):
        return run_git(repo, "rev-parse", "HEAD")
    run_git(repo, "add", "-A")
    run_git(repo, "commit", "-m", message)
    return run_git(repo, "rev-parse", "HEAD")


def push(repo: Path, branch_name: str) -> None:
    run_git(repo, "push", "-u", "origin", branch_name)
