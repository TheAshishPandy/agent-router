from __future__ import annotations

import json
import subprocess
from pathlib import Path


def run(repo: Path, command: list[str], timeout: int = 1800):
    try:
        p = subprocess.run(
            command,
            cwd=repo,
            text=True,
            capture_output=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return False, "TIMEOUT: " + " ".join(command)
    return p.returncode == 0, (p.stdout + "\n" + p.stderr).strip()[-12000:]


def detect_checks(repo: Path):
    checks = []
    if list(repo.glob("*.sln")) or list(repo.glob("*.csproj")):
        checks += [
            ("dotnet-build", ["dotnet", "build", "--no-restore"]),
            ("dotnet-test", ["dotnet", "test", "--no-restore", "--no-build"]),
        ]
    elif (
        (repo / "pyproject.toml").exists()
        or (repo / "pytest.ini").exists()
        or (repo / "tests").is_dir()
    ):
        checks.append(("pytest", ["python", "-m", "pytest"]))
    elif (repo / "package.json").exists():
        try:
            scripts = json.loads(
                (repo / "package.json").read_text(encoding="utf-8")
            ).get("scripts", {})
            if "build" in scripts:
                checks.append(("npm-build", ["npm", "run", "build"]))
            if "test" in scripts:
                checks.append(("npm-test", ["npm", "test", "--", "--runInBand"]))
        except Exception:
            pass
    return checks


def verify(repo: Path):
    checks = detect_checks(repo)
    if not checks:
        return {
            "passed": False,
            "checks": [],
            "reason": "No supported build/test checks detected; refusing to commit unverified work.",
        }

    results = []
    for name, cmd in checks:
        ok, out = run(repo, cmd)
        results.append({"name": name, "ok": ok, "output": out})
        if not ok:
            break

    return {
        "passed": bool(results) and all(x["ok"] for x in results),
        "checks": results,
        "reason": "",
    }
