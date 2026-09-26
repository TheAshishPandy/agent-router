from __future__ import annotations

import argparse
import time
from datetime import datetime, timezone
from pathlib import Path

from .agent_router_client import AgentRouterError, run_agent_router
from .completion_gate import verify
from .config import OrchestratorConfig
from .git_manager import GitError, commit, ensure_work_branch, push
from .project_plan import ProjectPlan
from .repo_discovery import discover_repositories
from .state_manager import load, save


def log(msg: str, cfg: OrchestratorConfig) -> None:
    cfg.log_dir.mkdir(parents=True, exist_ok=True)
    line = f"[{datetime.now().isoformat(timespec='seconds')}] {msg}"
    print(line, flush=True)
    with (cfg.log_dir / "orchestrator.log").open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def project_complete(plan: ProjectPlan) -> bool:
    return bool(plan.features) and all(f.status in {"DONE", "SKIPPED"} for f in plan.features)


def invoke_agent(repo: Path, cfg: OrchestratorConfig, plan: ProjectPlan, feature_id: str, feedback: str = "", attempt: int = 1) -> dict:
    feature = next((f for f in plan.features if f.feature_id == feature_id), None)
    if feature is None:
        raise RuntimeError(f"Feature {feature_id} is not present in {plan.path}")
    task = (
        plan.task_context(feature)
        + f"\nREPOSITORY: {repo}\nAUTONOMOUS ATTEMPT: {attempt}\n"
        + "Use the available repository tools to inspect relevant files, implement the feature, run its acceptance checks, and fix failures.\n"
        + "Do not expose secrets, modify unrelated repositories, force-push, or use destructive commands.\n"
        + "Do not declare completion until the feature is implemented, verified, and its plan status is updated to DONE.\n"
    )
    if feedback:
        task += "\n\nPrevious verification feedback that must be fixed:\n" + feedback

    log(f"START {repo.name}: Agent Router attempt {attempt}/{cfg.max_fix_attempts + 1}", cfg)
    started = time.monotonic()
    try:
        result = run_agent_router(
            repo=repo,
            base_url=cfg.agent_router_url,
            model=cfg.agent_router_model,
            task=task,
            max_turns=cfg.agent_router_max_turns,
            timeout=cfg.agent_router_timeout_seconds,
        )
    except AgentRouterError as exc:
        result = {
            "success": False,
            "status": "ROUTER_ERROR",
            "exit_code": 1,
            "response": str(exc),
            "provider": "",
            "model": cfg.agent_router_model,
            "usage": {},
        }

    duration = time.monotonic() - started
    provider = result.get("provider") or "unknown"
    log(
        f"END {repo.name}: Agent Router provider={provider} "
        f"model={result.get('model', cfg.agent_router_model)} "
        f"status={result.get('status')} success={result.get('success')} duration={duration:.1f}s",
        cfg,
    )
    return {
        **result,
        "exit_code": 0 if result.get("success") else 1,
        "duration_seconds": duration,
        "log_path": str(cfg.log_dir / repo.name),
    }


def process_repo(repo: Path, cfg: OrchestratorConfig, state: dict) -> bool:
    key = str(repo)
    entry = state.setdefault("repositories", {}).setdefault(key, {})
    entry["status"] = "working"

    try:
        plan = ProjectPlan.load(repo / cfg.project_plan_file)
        feature = plan.next_feature()
        if feature is None:
            entry["status"] = "complete" if project_complete(plan) else "waiting"
            entry["plan"] = str(plan.path)
            save(cfg.state_file, state)
            log(f"SKIP {repo}: no eligible planned feature (status={entry['status']})", cfg)
            return entry["status"] == "complete"
        entry["plan"] = str(plan.path)
        entry["feature_id"] = feature.feature_id
        entry["feature_status"] = feature.status
        save(cfg.state_file, state)

        branch_name = ensure_work_branch(repo, cfg.branch_prefix)
        entry["branch"] = branch_name
        save(cfg.state_file, state)

        feedback = ""
        total_attempts = cfg.max_fix_attempts + 1
        for attempt in range(1, total_attempts + 1):
            agent = invoke_agent(repo, cfg, plan, feature.feature_id, feedback, attempt)
            entry["last_agent"] = agent
            entry["attempts"] = attempt
            save(cfg.state_file, state)

            verification = verify(repo)
            entry["last_verification"] = verification
            current_plan = ProjectPlan.load(repo / cfg.project_plan_file)
            current_feature = next((f for f in current_plan.features if f.feature_id == feature.feature_id), None)
            entry["feature_status_after_agent"] = current_feature.status if current_feature else "MISSING"
            save(cfg.state_file, state)

            if agent["success"] and verification["passed"] and current_feature and current_feature.status == "DONE":
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
                "The previous autonomous attempt did not pass the completion gate or the planned feature was not marked DONE. "
                "Inspect the current working tree and fix the problem; do not discard valid work.\n"
            )
            if failures:
                feedback += "\n".join(failures)
            if not verification["checks"]:
                feedback += f"\n{verification.get('reason', '')}"
            if not agent["success"]:
                feedback += (
                    f"\nAgent Router status={agent['status']} exit={agent['exit_code']}. "
                    f"Review the Agent Router log at {agent['log_path']}."
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
    parser.add_argument("--repo", help="Process only this repository path")
    args = parser.parse_args()

    cfg = OrchestratorConfig.from_file(args.config)
    state = load(cfg.state_file)

    if args.repo:
        selected = Path(args.repo).expanduser().resolve()
        if not (selected / ".git").exists():
            parser.error(f"Not a Git repository: {selected}")
        repositories = [selected]
    else:
        repositories = None

    while True:
        progress = False
        current_repositories = repositories if repositories is not None else discover_repositories(cfg.workspace)
        log(f"Discovered {len(current_repositories)} repositories under {cfg.workspace}", cfg)

        for repo in current_repositories:
            entry = state.get("repositories", {}).get(str(repo), {})
            if entry.get("status") == "completed":
                plan_path = repo / cfg.project_plan_file
                if plan_path.exists():
                    current_plan = ProjectPlan.load(plan_path)
                    if project_complete(current_plan):
                        log(f"SKIP {repo}: project plan is complete", cfg)
                        continue
                    log(
                        f"RESUME {repo}: persisted completed state is stale; "
                        f"plan has unfinished feature {current_plan.next_feature().feature_id if current_plan.next_feature() else 'NONE'}",
                        cfg,
                    )
                    entry["status"] = "working"
                    entry.pop("error", None)
                    save(cfg.state_file, state)
                else:
                    log(f"RESUME {repo}: project plan is missing; process_repo will report the error", cfg)
            progress = True
            if not process_repo(repo, cfg, state) and cfg.stop_on_blocked:
                return 2

        if args.once or not progress:
            return 0
        time.sleep(5)


if __name__ == "__main__":
    raise SystemExit(main())
