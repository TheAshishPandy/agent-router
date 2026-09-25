# Agent Router Autonomous Orchestrator

The orchestrator supervises sequential repository work using the real Antigravity CLI (agy).

## Flow

1. Discover Git repositories under the configured workspace.
2. Refuse to touch repositories with pre-existing working-tree changes.
3. Create or reuse agent/auto/<repo>.
4. Launch Antigravity in that repository with --output-format stream-json.
5. Display live tool/progress events and save the raw NDJSON stream.
6. Run an independent build/test completion gate.
7. Feed failures back to Antigravity and retry up to max_fix_attempts times.
8. Commit only when Antigravity reports SUCCESS and the completion gate passes.
9. Push the autonomous branch only when auto_push is enabled.

## Antigravity configuration

The default model is gemini-3.8-flash-medium. Override it with agy_model or AGY_MODEL.

On Windows the orchestrator automatically checks %LOCALAPPDATA%\agy\bin\agy.exe before falling back to agy on PATH. This matches the official Windows CLI installation location.

## Logs

Each attempt is written to:
logs\orchestrator\<repo>\<timestamp>-attempt-<n>.jsonl

The global human-readable log is:
logs\orchestrator\orchestrator.log

## Run once

python -m orchestrator.manager --once

## Run continuously

.\start-orchestrator.ps1

## Safety defaults

- auto_commit is enabled.
- auto_push is disabled.
- Existing uncommitted changes block autonomous branch creation/switching.
- Agent-reported completion is not trusted by itself.
- No commit is created when no supported verification check is detected.
- The orchestrator does not pass --dangerously-skip-permissions; use the configured Antigravity permission policy instead.
