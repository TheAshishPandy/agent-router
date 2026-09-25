from pathlib import Path

from orchestrator.agent_router_client import _safe_path


def test_safe_path_stays_inside_repo(tmp_path: Path):
    assert _safe_path(tmp_path, "src/app.py") == (tmp_path / "src/app.py").resolve()


def test_safe_path_rejects_escape(tmp_path: Path):
    try:
        _safe_path(tmp_path, "../outside.txt")
    except Exception as exc:
        assert "escapes repository" in str(exc)
    else:
        raise AssertionError("path traversal was not rejected")
