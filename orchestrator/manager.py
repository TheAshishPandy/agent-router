from __future__ import annotations
import argparse, os, shlex, subprocess, time
from datetime import datetime, timezone
from pathlib import Path
from .completion_gate import verify
from .config import OrchestratorConfig
from .git_manager import GitError, commit, ensure_work_branch, push
from .repo_discovery import discover_repositories
from .state_manager import load, save

def log(msg,cfg):
    cfg.log_dir.mkdir(parents=True,exist_ok=True); line=f"[{datetime.now().isoformat(timespec='seconds')}] {msg}"; print(line,flush=True)
    with (cfg.log_dir/"orchestrator.log").open("a",encoding="utf-8") as f: f.write(line+"\n")

def invoke_agent(repo,cfg,feedback=""):
    template=os.environ.get("ANTIGRAVITY_COMMAND","")
    if not template: raise RuntimeError("ANTIGRAVITY_COMMAND is not configured. Set it to the Antigravity CLI command/template containing {repo} and {task}.")
    task=f"Work autonomously in {repo}. Inspect the entire repository, complete the highest-priority incomplete work, implement it, generate or update tests, build and test it, fix failures, verify the result, and stop only when genuinely complete. Do not expose or commit secrets. {feedback}".strip()
    command=template.replace("{repo}",str(repo)).replace("{task}",task)
    log(f"Launching Antigravity for {repo}",cfg); return subprocess.call(shlex.split(command,posix=False),cwd=repo)

def process_repo(repo,cfg,state):
    key=str(repo); entry=state.setdefault("repositories",{}).setdefault(key,{})
    entry["status"]="working"; save(cfg.state_file,state)
    try:
        branch_name=ensure_work_branch(repo,cfg.branch_prefix); entry["branch"]=branch_name; save(cfg.state_file,state); feedback=""
        for attempt in range(cfg.max_fix_attempts+1):
            code=invoke_agent(repo,cfg,feedback); verification=verify(repo); entry["last_verification"]=verification; entry["attempts"]=attempt+1; save(cfg.state_file,state)
            if verification["passed"]:
                if cfg.auto_commit: entry["commit"]=commit(repo,f"Complete autonomous work for {repo.name}")
                if cfg.auto_push: push(repo,branch_name); entry["pushed"]=True
                entry["status"]="completed"; entry["completed_at"]=datetime.now(timezone.utc).isoformat(); save(cfg.state_file,state); log(f"COMPLETED {repo} on {branch_name}",cfg); return True
            feedback="Previous attempt failed verification. Fix these failures before continuing:\n"+"\n".join(f"{x['name']}: {x['output'][-3000:]}" for x in verification["checks"] if not x["ok"])
            if code!=0: feedback+=f"\nAntigravity process exited with code {code}."
            log(f"Verification failed for {repo}; retry {attempt+1}/{cfg.max_fix_attempts}",cfg)
        entry["status"]="blocked"; save(cfg.state_file,state); return False
    except (GitError,RuntimeError,OSError) as exc:
        entry["status"]="error"; entry["error"]=str(exc); save(cfg.state_file,state); log(f"ERROR {repo}: {exc}",cfg); return False

def main():
    parser=argparse.ArgumentParser(description="Sequential autonomous repository supervisor"); parser.add_argument("--config",default="orchestrator-config.json"); parser.add_argument("--once",action="store_true"); args=parser.parse_args()
    cfg=OrchestratorConfig.from_file(args.config); state=load(cfg.state_file)
    while True:
        progress=False
        for repo in discover_repositories(cfg.workspace):
            if state.get("repositories",{}).get(str(repo),{}).get("status")=="completed": continue
            progress=True
            if not process_repo(repo,cfg,state) and cfg.stop_on_blocked: return 2
        if args.once or not progress: return 0
        time.sleep(5)
if __name__=="__main__": raise SystemExit(main())
