# Autonomous Repository Orchestrator

The orchestrator performs sequential repository work through the local Agent Router API. Agent Router owns provider selection and can cascade Hugging Face → Ollama → OpenRouter.

## Prerequisites

1. Agent Router is running on `http://127.0.0.1:8001`.
2. The configured Agent Router API key is available to the router runtime.
3. Hugging Face, Ollama and/or OpenRouter are configured in `platform.json`.
4. Python dependencies for Agent Router are installed.

Agent Router exposes an Anthropic-compatible `/v1/messages` endpoint. The orchestrator supplies controlled repository tools to the selected model, while Agent Router handles provider failover.

## Run one cycle

From the Agent Router repository:

python -m orchestrator.manager --once

The orchestrator discovers repositories below C:\Ashish\POC, creates/reuses agent/auto/<repo>, calls Agent Router, runs verification, retries failures, and commits only verified work.

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

- agent_router_url: Agent Router API base, normally `http://127.0.0.1:8001/api`.
- agent_router_model: requested model; provider selection is handled by Agent Router.
- agent_router_max_turns: maximum tool/agent turns per attempt.
- agent_router_timeout_seconds: per-request timeout.
- auto_push: set to true only after validating the workflow.

Environment overrides:

$env:AGENT_ROUTER_URL = "http://127.0.0.1:8001/api"
$env:AGENT_ROUTER_MODEL = "claude-sonnet-4-6"

## Verification

The completion gate currently supports:

- .NET: dotnet build --no-restore, then dotnet test --no-restore --no-build
- Python/pytest: python -m pytest
- Node: npm run build and npm test -- --runInBand

Extend orchestrator/completion_gate.py for project-specific checks.

## Push

Push is disabled by default. After validation, set auto_push to true. The target repository must have an origin remote and valid Git credentials.

## Important behavior

The orchestrator does not depend on the Antigravity desktop GUI or CLI. It calls Agent Router directly from each repository working directory.


## Safely test one repository first

Use the single-repository mode before enabling workspace-wide autonomous processing:

python -m orchestrator.manager --once --repo C:\Ashish\POC\agent-router

This still creates/reuses `agent/auto/agent-router`, calls Agent Router, runs the completion gate, and commits only after verification.

After validating the workflow, omit --repo to process all repositories discovered below the workspace.
