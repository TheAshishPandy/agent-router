# Autonomous Project Execution Plan

> **Purpose:** This file is the single source of truth for autonomous AI agents working on this repository.
>
> **Rule:** An agent MUST NOT scan the entire repository looking for arbitrary work when this file contains an actionable feature. Read this file first, select the next eligible item, work only on that item, verify it, and update this file before handing off.

---

## 1. Project Identity

- **Project:** Agent Router + Autonomous Repository Orchestrator
- **Repository:** `TheAshishPandy/agent-router`
- **Primary goal:** Provide a reliable local/API agent router with provider fallback plus a sequential autonomous engineering system that can safely continue repository work across multiple model runs.
- **Execution model:** One feature at a time, with persistent handoff state.
- **Source of truth:** This file + the repository's current working tree + automated verification.

---

## 2. Agent Rules

Every autonomous model MUST follow these rules:

1. Read this file before inspecting unrelated repository files.
2. Read the **Current Execution State** section first.
3. Select the first feature whose status is `IN_PROGRESS` or the first `TODO` feature whose dependencies are `DONE`.
4. Do NOT choose a different feature because it looks more interesting.
5. Do NOT redesign completed features unless the selected feature explicitly requires it.
6. Inspect only the files relevant to the selected feature first.
7. Implement the smallest complete change required by that feature.
8. Run the feature's acceptance checks.
9. If checks fail, fix the selected feature before moving to another feature.
10. When the feature is fully verified, change its status to `DONE`.
11. Record implementation details, verification results, commit SHA (when known), and remaining follow-up work.
12. Never mark a feature `DONE` merely because code was written.
13. Never mark a feature `DONE` if tests/build/verification required by the feature are failing.
14. If blocked, set the feature to `BLOCKED`, record the exact blocker, and stop. Do not randomly switch to another feature unless this plan explicitly permits it.
15. Never modify secrets, force-push, delete unrelated work, or make destructive changes.

---

## 3. Status Definitions

| Status | Meaning |
|---|---|
| `TODO` | Planned but work has not started. |
| `IN_PROGRESS` | The active feature currently being implemented. |
| `BLOCKED` | Work cannot continue because a documented dependency, environment, API, credential, or design decision is missing. |
| `DONE` | Implementation and acceptance verification are complete. |
| `SKIPPED` | Intentionally not required; explain why. |

Only **one** feature should normally be `IN_PROGRESS` at a time.

---

## 4. Current Execution State

### Active Feature

- **Feature ID:** F-07
- **Status:** IN_PROGRESS
- **Started:** 2026-09-26
- **Owner:** Autonomous Agent
- **Objective:** Make the autonomous orchestrator execute from this project plan instead of asking every model to rediscover project work.
- **Next action:** Finish plan-driven execution integration, test it, then mark F-07 `DONE`.

### Handoff

The next model should begin here:

1. Read F-07.
2. Inspect only the orchestrator plan/state/task-generation code needed for F-07.
3. Complete F-07.
4. Run its acceptance checks.
5. Update this file.
6. Continue with F-08 only after F-07 is `DONE`.

**Do not start another feature while F-07 is `IN_PROGRESS`.**

---

## 5. Feature Roadmap

### F-01 — Provider Cascade
- **Status:** DONE
- **Goal:** Route requests through configured providers with fallback behavior.
- **Dependencies:** None.
- **Acceptance:**
  - Provider configuration is loaded.
  - Cascade can select a configured provider.
  - Failure can move to the next configured provider.
  - Response identifies the selected/cascade provider.
- **Completed work:** Provider routing and cascade configuration are implemented.
- **Verification:** Existing router/provider tests and successful API routing checks.
- **Handoff:** No action required unless a later feature exposes a regression.

### F-02 — Hugging Face Provider
- **Status:** DONE
- **Goal:** Support Hugging Face Router as an Agent Router provider.
- **Dependencies:** F-01.
- **Acceptance:**
  - HF credentials can be configured without hard-coding secrets.
  - HF model can be selected from configuration/environment.
  - Agent Router can successfully route a request to HF.
  - Provider failures participate in cascade handling.
- **Completed work:** HF configuration and routing integration implemented.
- **Verification:** Successful HTTP 200 routing test through the Agent Router.
- **Handoff:** No action required unless provider behavior regresses.

### F-03 — Autonomous Agent Router Client
- **Status:** DONE
- **Goal:** Allow the repository orchestrator to use Agent Router instead of directly depending on Antigravity/Gemini.
- **Dependencies:** F-01, F-02.
- **Acceptance:**
  - Orchestrator can call `/v1/messages`.
  - Agent can inspect/read/write repository files through controlled tools.
  - Agent can run approved engineering commands.
  - Provider/model information is returned to orchestrator state/logs.
  - Tool execution is restricted to safe repository operations.
- **Completed work:** `orchestrator/agent_router_client.py` implemented and manager integration added.
- **Verification:** Client unit tests added; integration verification remains part of later runtime testing.
- **Handoff:** Do not rebuild this client unless F-07 or a regression requires it.

### F-04 — Persistent Orchestrator State
- **Status:** DONE
- **Goal:** Preserve repository progress between autonomous runs.
- **Dependencies:** F-03.
- **Acceptance:**
  - Repository status survives process restart.
  - Completed repositories are skipped.
  - Working branches are recorded.
  - Verification and attempt results are recorded.
  - Runtime state does not make the Git working tree dirty.
- **Completed work:** State file moved under `.orchestrator/` and ignored by Git.
- **Verification:** State handling implemented in the orchestrator.
- **Handoff:** Extend only when required by plan-driven execution.

### F-05 — Live Routing Telemetry
- **Status:** DONE
- **Goal:** Expose provider/model execution telemetry to the dashboard.
- **Dependencies:** F-01, F-03.
- **Acceptance:**
  - Active requests can be observed.
  - Provider/model attempts are recorded.
  - Success/failure counts are available.
  - Router state endpoint exposes current routing information.
- **Completed work:** Routing telemetry and `/api/router-state` endpoint added.
- **Verification:** Backend telemetry code and dashboard integration implemented.
- **Handoff:** Future dashboard features should consume the existing telemetry instead of duplicating routing state.

### F-06 — Operations Dashboard
- **Status:** DONE
- **Goal:** Provide a glassmorphism operations dashboard showing current models, providers, requests, failures, activity, and capacity.
- **Dependencies:** F-05.
- **Acceptance:**
  - Current execution is visible.
  - Provider cascade is visible.
  - Recent requests are visible.
  - Request/failure metrics are visible.
  - Dashboard refreshes live.
  - UI remains usable without external dashboard runtime dependencies.
- **Completed work:** Glassmorphism dashboard implemented in `dashboard.html`, using shadcn/Vercel-inspired design tokens and live router/stat endpoints.
- **Verification:** Dashboard/backend code is committed; full browser runtime verification remains a future integration check.
- **Handoff:** Do not replace the dashboard framework unless a dedicated dashboard migration feature is active.

### F-07 — Plan-Driven Autonomous Execution
- **Status:** IN_PROGRESS
- **Goal:** Stop agents from repeatedly scanning a repository to decide what to do. The orchestrator must provide the exact current feature and acceptance criteria from this file.
- **Dependencies:** F-03, F-04.
- **Required implementation:**
  - Add a standard project-plan filename/configuration.
  - Load the plan before creating the agent task.
  - Parse the active feature and eligible next feature.
  - Send the selected feature, dependencies, acceptance criteria, and previous verification feedback to Agent Router.
  - Do not ask the model to discover arbitrary work when a plan exists.
  - Preserve the selected feature in orchestrator state.
  - Prevent a new feature from starting while the current feature is `IN_PROGRESS` unless the current feature is completed/blocked according to the plan.
  - Require the agent to update the plan status only after verification.
  - Record the selected feature ID in logs/dashboard telemetry.
- **Acceptance checks:**
  - Running the orchestrator on this repository selects F-07.
  - The generated agent task contains F-07 and its acceptance criteria.
  - The generated task does not contain “find the highest-priority unfinished work” or equivalent discovery instructions.
  - State records `feature_id=F-07`.
  - A completed feature is not selected again.
  - A feature with incomplete dependencies is not selected.
  - A blocked feature does not silently cause unrelated work to start.
  - Unit tests cover plan parsing and feature selection.
- **Definition of done:** All acceptance checks pass and this feature is changed to `DONE`.

### F-08 — Feature Handoff / Completion Ledger
- **Status:** TODO
- **Goal:** Make every completed feature leave a compact machine-readable handoff so the next model knows exactly what changed without rediscovering it.
- **Dependencies:** F-07.
- **Required implementation:**
  - Record feature ID, status, files changed, verification commands/results, commit SHA, provider/model used, attempts, and known follow-ups.
  - Add a compact `HANDOFF` section to the plan.
  - Include previous model's failure feedback when retrying.
  - Ensure the next agent receives only the relevant handoff context first.
- **Acceptance:** A new run can identify the previous completed feature and continue with the next eligible feature without scanning the entire repository.

### F-09 — Autonomous Orchestrator Dashboard Integration
- **Status:** TODO
- **Goal:** Show repository → feature → task → agent → model → tool activity → files changed → tests → retry → verification → commit/push in the dashboard.
- **Dependencies:** F-07, F-08.
- **Required implementation:**
  - Add feature/task telemetry.
  - Show current repository and feature.
  - Show current agent/model/provider.
  - Show current action/tool.
  - Show files changed.
  - Show test/verification state.
  - Show retry count and reason.
  - Show commit/push status.
- **Acceptance:** Dashboard can explain what the autonomous system is doing without opening raw logs.

### F-10 — Multi-Repository Sequential Execution
- **Status:** TODO
- **Goal:** Process multiple repositories sequentially using each repository's own project plan.
- **Dependencies:** F-07, F-08.
- **Required implementation:**
  - Each repository must have its own project plan.
  - Discovery identifies repositories but does not choose arbitrary work inside them.
  - Repositories without a valid plan are reported as `UNPLANNED` and are not given arbitrary engineering work.
  - Completed repositories/features are skipped according to persistent state.
  - Each repository resumes from its own active feature.
- **Acceptance:** Running the workspace orchestrator cannot randomly target a repository feature merely because the model discovered it during a scan.

### F-11 — Verification and Safe Commit Pipeline
- **Status:** TODO
- **Goal:** Make completion deterministic: implement → test → fix → verify → commit.
- **Dependencies:** F-07, F-08.
- **Required implementation:**
  - Feature-specific checks.
  - Retry feedback from failed checks.
  - Commit only after acceptance criteria pass.
  - Record commit SHA in handoff state.
  - Optional push only after successful verification.
- **Acceptance:** No feature is marked `DONE` or committed as complete while required verification is failing.

### F-12 — Model Efficiency / Routing Analytics
- **Status:** TODO
- **Goal:** Measure model/provider efficiency using actual project telemetry rather than subjective “best model” labels.
- **Dependencies:** F-09.
- **Metrics:**
  - latency
  - successful completions
  - failure/retry rate
  - tokens/request
  - tokens per completed feature
  - provider fallback count
  - verification pass rate
  - estimated cost when provider pricing is available
- **Acceptance:** Dashboard shows measured efficiency for models actually used by the orchestrator.

---

## 6. Feature Selection Algorithm

The orchestrator must use this order:

1. Find the single `IN_PROGRESS` feature.
2. If it exists, continue only that feature.
3. Otherwise find the first `TODO` feature whose dependencies are all `DONE`.
4. If none exists:
   - if all features are `DONE` or `SKIPPED`, mark the project `COMPLETE`;
   - if a dependency is `BLOCKED`, mark the project `WAITING`;
   - otherwise mark the plan `NEEDS_REVIEW`.
5. Never select a later feature while an earlier eligible feature is still unfinished unless the plan explicitly changes its dependency/status.

---

## 7. Agent Task Contract

The orchestrator should send the model a task shaped like this:

```text
PROJECT: <project name>
REPOSITORY: <repo>

ACTIVE FEATURE: <feature id>
FEATURE STATUS: <status>
OBJECTIVE: <objective>

DEPENDENCIES:
<dependency list>

REQUIRED IMPLEMENTATION:
<exact requirements>

ACCEPTANCE CHECKS:
<exact checks>

PREVIOUS HANDOFF:
<compact previous result>

PREVIOUS VERIFICATION FAILURES:
<only relevant failures>

EXECUTION RULES:
- Work only on the active feature.
- Do not search for unrelated TODOs.
- Do not redesign completed features.
- Inspect only relevant files first.
- Implement, test, fix, and verify.
- Update the project plan when the feature is actually complete.
```

This replaces the old open-ended instruction to “identify the highest-priority unfinished engineering work”.

---

## 8. Completion Record

When a feature becomes `DONE`, append/update:

- **Feature ID**
- **Completed at**
- **Files changed**
- **Tests/checks executed**
- **Verification result**
- **Commit SHA**
- **Provider**
- **Model**
- **Attempts**
- **Known follow-ups**

Keep this section short. Do not paste raw logs into the plan.

---

## 9. Project Completion

The project is complete only when:

- Every required feature is `DONE` or explicitly `SKIPPED`.
- No required feature is `TODO`, `IN_PROGRESS`, or `BLOCKED`.
- Required tests pass.
- The final verification gate passes.
- The final commit is recorded.
- Dashboard/orchestrator state reflects completion.

---

## 10. Notes for Future Models

- This file is intentionally explicit so another model does not spend tokens rediscovering the project.
- Prefer this file over broad repository exploration for deciding **what to build next**.
- Repository exploration is still required for understanding the implementation of the selected feature.
- A model may inspect adjacent code when necessary to implement the active feature, but it must not use unrelated findings as a reason to change the roadmap.
