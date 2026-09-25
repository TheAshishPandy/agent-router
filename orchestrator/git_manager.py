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


def branch_exists(repo: Path, name: str) -> bool:
    return bool(run_git(repo, "branch", "--list", name).strip())


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

    if branch_exists(repo, name):
        run_git(repo, "switch", name)
        return name

    base = ""
    if branch_exists(repo, "main"):
        base = "main"
    elif branch_exists(repo, "master"):
        base = "master"
    elif current:
        base = current

    if base and current != base:
        run_git(repo, "switch", base)

    if base:
        run_git(repo, "switch", "-c", name, base)
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
