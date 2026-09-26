# Autonomous Project Plan

## 1. Project Identity
- **Project:** <project name>
- **Repository:** <repository>
- **Primary goal:** <one paragraph>
- **Source of truth:** This file

## 2. Current Execution State
- **Active Feature:** F-01
- **Status:** TODO
- **Next action:** <exact next action>

## 3. Agent Rules
1. Read this file before broad repository exploration.
2. Continue the active `IN_PROGRESS` feature first.
3. Otherwise select the first `TODO` feature whose dependencies are `DONE`.
4. Do not randomly select TODOs, issues, refactors, or “interesting” work.
5. Work only on the selected feature.
6. Run its acceptance checks.
7. Mark it `DONE` only after verification passes.
8. Record the handoff for the next model.
9. If blocked, record the exact blocker and stop.

## 4. Status Definitions
- `TODO` — not started
- `IN_PROGRESS` — currently being implemented
- `BLOCKED` — cannot continue; blocker documented
- `DONE` — implemented and verified
- `SKIPPED` — intentionally not required

## 5. Feature Roadmap

### F-01 — <feature name>
- **Status:** TODO
- **Dependencies:** None
- **Objective:** <what must be built>
- **Required implementation:**
  - <requirement>
- **Acceptance checks:**
  - <test/check>
- **Definition of done:** <objective evidence>

### F-02 — <feature name>
- **Status:** TODO
- **Dependencies:** F-01
- **Objective:** <what must be built>
- **Required implementation:**
  - <requirement>
- **Acceptance checks:**
  - <test/check>
- **Definition of done:** <objective evidence>

## 6. Feature Selection Algorithm
1. Continue the single `IN_PROGRESS` feature.
2. Otherwise select the first eligible `TODO`.
3. Never select a feature whose dependencies are incomplete.
4. If all required features are `DONE`/`SKIPPED`, the project is complete.
5. If progress is blocked, report the blocker instead of inventing unrelated work.

## 7. Handoff Record
For each completed feature record:
- Feature ID
- Files changed
- Tests/checks
- Verification result
- Commit SHA
- Provider/model
- Attempts
- Follow-ups

## 8. Project Completion
The project is complete only when all required features are `DONE` or `SKIPPED`, required verification passes, and the final commit is recorded.
