# Agent Router Autonomous Orchestrator

The orchestrator supervises sequential repository work. It discovers Git repositories, launches the configured Antigravity command, independently verifies build/tests, retries failures with concrete feedback, and commits verified work. Optional push is controlled by configuration.

Antigravity CLI executable names and flags vary by installation, so ANTIGRAVITY_COMMAND is intentionally configurable and must contain {repo} and {task}.
