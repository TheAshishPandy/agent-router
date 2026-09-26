from pathlib import Path

from orchestrator.project_plan import ProjectPlan


def test_selects_in_progress_feature(tmp_path: Path):
    plan = tmp_path / "PLAN.md"
    plan.write_text(
        """- **Project:** Demo
- **Feature ID:** F-02
- **Status:** IN_PROGRESS

### F-01 — First
- **Status:** DONE
- **Dependencies:** None

### F-02 — Second
- **Status:** IN_PROGRESS
- **Dependencies:** F-01

### F-03 — Third
- **Status:** TODO
- **Dependencies:** F-02
""",
        encoding="utf-8",
    )
    loaded = ProjectPlan.load(plan)
    assert loaded.next_feature().feature_id == "F-02"


def test_selects_first_eligible_todo(tmp_path: Path):
    plan = tmp_path / "PLAN.md"
    plan.write_text(
        """- **Project:** Demo

### F-01 — First
- **Status:** DONE
- **Dependencies:** None

### F-02 — Second
- **Status:** TODO
- **Dependencies:** F-01

### F-03 — Third
- **Status:** TODO
- **Dependencies:** F-02
""",
        encoding="utf-8",
    )
    loaded = ProjectPlan.load(plan)
    assert loaded.next_feature().feature_id == "F-02"


def test_blocks_feature_with_incomplete_dependency(tmp_path: Path):
    plan = tmp_path / "PLAN.md"
    plan.write_text(
        """- **Project:** Demo

### F-01 — First
- **Status:** TODO
- **Dependencies:** None

### F-02 — Second
- **Status:** TODO
- **Dependencies:** F-01
""",
        encoding="utf-8",
    )
    loaded = ProjectPlan.load(plan)
    assert loaded.next_feature().feature_id == "F-01"
