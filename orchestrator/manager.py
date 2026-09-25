from __future__ import annotations

import argparse
import json
import os
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from .completion_gate import verify
from .config import OrchestratorConfig
from .git_manager import GitError, commit, ensure_work_branch, push
from .repo_discovery import discover_repositories
from .state_manager import load, save


def log(msg: str, cfg: OrchestratorConfig) -> None:
    cfg.log_dir.mkdir(parents=True, exist_ok=True)
    line = f"[{datetime.now().isoformat(timespec='seconds')}] {msg}"
    print(line, flush=True)
    with (cfg.log_dir / "orchestrator.log").open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def _resolve_agy(cfg: OrchestratorConfig) -> str:
    configured = os.environ.get("AGY_PATH") or cfg.agy_path
    if configured:
        path = Path(os.path.expandvars(configured)).expanduser()
        if path.exists():
            return str(path)
        if Path(configured).name.lower() != "agy.exe":
            raise RuntimeError(f"Configured AGY_PATH does not exist: {configured}")
    if os.name == "nt":
        default = Path(os.environ.get("LOCALAPPDATA", "")) / "agy" / "bin" / "agy.exe"
        if default.exists():
            return str(default)
    return "agy"


def _event_message(event: dict) -> str:
    kind = event.get("event")
    if kind == "init":
        init = event.get("init", {})
        return f"AGY initialized: model={init.get('model', 'default')} permission={init.get('permission_mode', 'unknown')}"
    if kind == "result":
        result = event.get("result", {})
        status = result.get("status", "UNKNOWN")
        duration = result.get("duration_seconds")
        return f"AGY result: {status}" + (f" ({duration:.1f}s)" if isinstance(duration, (int, float)) else "")
    if kind == "step_update":
        step = event.get("step_update", {})
        step_type = step.get("step_type", "step")
        state = step.get("state", "")
        if step_type == "tool":
            tool = step.get("tool_name") or step.get("tool_info", {}).get("name", "tool")
            info = step.get("tool_info", {})
            params = info.get("parameters") or {}
            command = params.get("CommandLine") or params.get("command") or ""
            suffix = f" -> {command}" if command else ""
            return f"AGY {state.lower()} tool={tool}{suffix}"
        if step_type == "agent_response" and step.get("text_delta"):
            text = " ".join(str(step["text_delta"]).split())
            return f"AGY response: {text[:240]}"
        return f"AGY {state.lower()} {step_type}"
    return ""


def _stream_stderr(pipe, log_file, cfg: OrchestratorConfig, repo: Path) -> None:
    for line in iter(pipe.readline, ""):
        message = line.rstrip()
        if not message:
            continue
        event = {
            "event": "stderr",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "message": message,
        }
        log_file.write(json.dumps(event, ensure_ascii=False) + "\n")
        log_file.flush()
        log(f"{repo.name}: AGY stderr: {message}", cfg)


def invoke_agent(repo: Path, cfg: OrchestratorConfig, feedback: str = "", attempt: int = 1) -> dict:
    task = (
        f"Work autonomously in the repository {repo}. "
        "Inspect the entire repository, documentation, configuration, issues/TODOs and existing tests. "
        "Identify the highest-priority unfinished engineering work and complete it. "
        "Implement the changes, add or update tests, build and test the project, fix failures, and verify the result. "
        "For web/UI work, perform browser verification when the available tools support it. "
        "If a required skill, library, image, icon, font or other asset is missing, research a legitimate source online, "
        "prefer clearly licensed/free assets, add it to the project, and verify that it loads. "
        "Never download suspicious executables, expose secrets, or modify unrelated repositories. "
        "Do not declare completion until the requested work is actually implemented and verified. "
        f"This is autonomous attempt {attempt}. "
    )
    if feedback:
        task += "\n\nPrevious verification feedback that must be fixed:\n" + feedback

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    repo_log_dir = cfg.log_dir / repo.name
    repo_log_dir.mkdir(parents=True, exist_ok=True)
    log_path = repo_log_dir / f"{timestamp}-attempt-{attempt}.jsonl"

    agy = _resolve_agy(cfg)
    command = [
        agy,
        "--model", cfg.agy_model,
    ]
    if cfg.agy_effort:
        command.extend(["--effort", cfg.agy_effort])
    command.extend([
        "--mode", "accept-edits",
        "--output-format", "stream-json",
        "--print-timeout", cfg.agy_print_timeout,
        "-p", task,
    ])

    log(f"START {repo.name}: Antigravity attempt {attempt}/{cfg.max_fix_attempts + 1}", cfg)
    log(f"AGY log: {log_path}", cfg)

    started = time.monotonic()
    process = subprocess.Popen(
        command,
        cwd=repo,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )

    stderr_log = log_path.open("a", encoding="utf-8")
    stderr_thread = threading.Thread(
        target=_stream_stderr,
        args=(process.stderr, stderr_log, cfg, repo),
        daemon=True,
    )
    stderr_thread.start()

    result_event = None
    response_parts: list[str] = []
    with log_path.open("a", encoding="utf-8") as log_file:
        for line in process.stdout:
            raw = line.rstrip("\r\n")
            if not raw:
                continue
            try:
                event = json.loads(raw)
            except json.JSONDecodeError:
                event = {
                    "event": "non_json_stdout",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "raw": raw,
                }
            event["_received_at"] = datetime.now(timezone.utc).isoformat()
            log_file.write(json.dumps(event, ensure_ascii=False) + "\n")
            log_file.flush()

            if event.get("event") == "step_update":
                delta = event.get("step_update", {}).get("text_delta")
                if delta:
                    response_parts.append(str(delta))
            elif event.get("event") == "result":
                result_event = event.get("result", {})

            message = _event_message(event)
            if message:
                log(f"{repo.name}: {message}", cfg)

            if time.monotonic() - started > cfg.agy_hard_timeout_seconds:
                log(f"{repo.name}: hard timeout reached; terminating Antigravity", cfg)
                process.terminate()
                break

    process.wait()
    stderr_thread.join(timeout=2)
    stderr_log.close()

    result = result_event or {}
    status = result.get("status", "NO_RESULT")
    response = result.get("response") or "".join(response_parts)
    exit_code = process.returncode
    success = status == "SUCCESS" and exit_code == 0

    log(f"END {repo.name}: status={status} exit={exit_code} success={success}", cfg)
    return {
        "success": success,
        "status": status,
        "exit_code": exit_code,
        "response": response,
        "conversation_id": result.get("conversation_id"),
        "usage": result.get("usage", {}),
        "duration_seconds": result.get("duration_seconds"),
        "log_path": str(log_path),
    }


def process_repo(repo: Path, cfg: OrchestratorConfig, state: dict) -> bool:
    key = str(repo)
    entry = state.setdefault("repositories", {}).setdefault(key, {})
    entry["status"] = "working"
    save(cfg.state_file, state)

    try:
        branch_name = ensure_work_branch(repo, cfg.branch_prefix)
        entry["branch"] = branch_name
        save(cfg.state_file, state)

        feedback = ""
        total_attempts = cfg.max_fix_attempts + 1
        for attempt in range(1, total_attempts + 1):
            agent = invoke_agent(repo, cfg, feedback, attempt)
            entry["last_agent"] = agent
            entry["attempts"] = attempt
            save(cfg.state_file, state)

            verification = verify(repo)
            entry["last_verification"] = verification
            save(cfg.state_file, state)

            if agent["success"] and verification["passed"]:
                if cfg.auto_commit:
                    entry["commit"] = commit(repo, f"Complete autonomous work for {repo.name}")
                if cfg.auto_push:
                    push(repo, branch_name)
                    entry["pushed"] = True
                entry["status"] = "completed"
                entry["completed_at"] = datetime.now(timezone.utc).isoformat()
                save(cfg.state_file, state)
                log(f"COMPLETED {repo} on {branch_name}", cfg)
                return True

            failures = [
                f"{check['name']}: {check['output'][-4000:]}"
                for check in verification["checks"]
                if not check["ok"]
            ]
            feedback = (
                "The previous autonomous attempt did not pass the completion gate. "
                "Inspect the current working tree and fix the problem; do not discard valid work.\n"
            )
            if failures:
                feedback += "\n".join(failures)
            if not verification["checks"]:
                feedback += f"\n{verification.get('reason', '')}"
            if not agent["success"]:
                feedback += (
                    f"\nAntigravity status={agent['status']} exit={agent['exit_code']}. "
                    f"Review the Antigravity log at {agent['log_path']}."
                )

            if attempt < total_attempts:
                log(f"RETRY {repo}: attempt {attempt + 1}/{total_attempts}", cfg)

        entry["status"] = "blocked"
        save(cfg.state_file, state)
        log(f"BLOCKED {repo}: maximum attempts reached", cfg)
        return False

    except (GitError, RuntimeError, OSError) as exc:
        entry["status"] = "error"
        entry["error"] = str(exc)
        save(cfg.state_file, state)
        log(f"ERROR {repo}: {exc}", cfg)
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description="Sequential autonomous repository supervisor")
    parser.add_argument("--config", default="orchestrator-config.json")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()

    cfg = OrchestratorConfig.from_file(args.config)
    state = load(cfg.state_file)

    while True:
        progress = False
        repositories = discover_repositories(cfg.workspace)
        log(f"Discovered {len(repositories)} repositories under {cfg.workspace}", cfg)

        for repo in repositories:
            if state.get("repositories", {}).get(str(repo), {}).get("status") == "completed":
                log(f"SKIP {repo}: already completed", cfg)
                continue
            progress = True
            if not process_repo(repo, cfg, state) and cfg.stop_on_blocked:
                return 2

        if args.once or not progress:
            return 0
        time.sleep(5)


if __name__ == "__main__":
    raise SystemExit(main())
