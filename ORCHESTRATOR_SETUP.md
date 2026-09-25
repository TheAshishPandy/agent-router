# Autonomous Repository Orchestrator

Agent Router can supervise sequential repository work. It discovers Git repositories under the configured workspace, invokes a configured Antigravity CLI command, independently verifies build/tests, retries failures with feedback, commits verified work, and optionally pushes the working branch.

## Safety defaults

- auto_commit is enabled.
- auto_push is disabled until you explicitly enable it.
- Agent-reported completion is never trusted by itself; the completion gate must pass.
- The orchestrator does not intentionally add secrets.

## Antigravity command

Set ANTIGRAVITY_COMMAND to the Antigravity CLI invocation available on your machine. The template must contain {repo} and {task}. The exact executable and flags depend on the Antigravity CLI version installed locally, so they are intentionally not hard-coded here.

## Run once

python -m orchestrator.manager --once

## Run continuously

.\\start-orchestrator.ps1

## Enable push

After validating the workflow, set auto_push to true in orchestrator-config.json. The repository must have an origin remote and credentials configured for the push.

## Completion gate

Common .NET, Python/pytest, and Node projects are detected automatically. Extend orchestrator/completion_gate.py with project-specific build, test, lint, or browser checks as needed.
