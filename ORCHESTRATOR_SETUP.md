# Autonomous Repository Orchestrator

Agent Router supervises sequential repository work through the real Antigravity CLI.

## Prerequisites

1. Antigravity CLI is installed and authenticated.
2. agy can run headlessly.
3. Antigravity permissions allow the commands the agent needs.
4. Python dependencies for Agent Router are installed.

The Antigravity CLI supports headless -p execution and stream-json NDJSON events for monitoring tool calls and progress.

## Run one cycle

From the Agent Router repository:

python -m orchestrator.manager --once

The orchestrator discovers repositories below C:\Ashish\POC, creates/reuses agent/auto/<repo>, invokes agy, runs verification, retries failures, and commits only verified work.

## Watch live progress

The terminal prints events such as:

[2026-09-25T21:30:00] Discovered 3 repositories under C:\Ashish\POC
[2026-09-25T21:30:01] START smartbot: Antigravity attempt 1/6
[2026-09-25T21:30:03] smartbot: AGY initialized: model=gemini-3.8-flash-medium permission=always-proceed
[2026-09-25T21:30:05] smartbot: AGY done tool=run_command -> git status
[2026-09-25T21:32:15] smartbot: AGY result: SUCCESS (130.2s)
[2026-09-25T21:32:16] COMPLETED C:\Ashish\POC\smartbot on agent/auto/smartbot

Raw NDJSON is saved under logs\orchestrator\<repo>\.

## Configuration

Edit orchestrator-config.json:

- agy_model: Antigravity model slug.
- agy_effort: optional low, medium, or high.
- agy_print_timeout: Antigravity timeout such as 10m.
- agy_hard_timeout_seconds: supervisor-level safety ceiling.
- auto_push: set to true only after validating the workflow.

Environment overrides:

$env:AGY_MODEL = "gemini-3.8-flash-medium"
$env:AGY_EFFORT = "medium"
$env:AGY_PATH = "$env:LOCALAPPDATA\agy\bin\agy.exe"

## Verification

The completion gate currently supports:

- .NET: dotnet build --no-restore, then dotnet test --no-restore --no-build
- Python/pytest: python -m pytest
- Node: npm run build and npm test -- --runInBand

Extend orchestrator/completion_gate.py for project-specific checks.

## Push

Push is disabled by default. After validation, set auto_push to true. The target repository must have an origin remote and valid Git credentials.

## Important behavior

The orchestrator does not use the Antigravity desktop GUI. It calls the supported agy CLI directly from each repository working directory.
