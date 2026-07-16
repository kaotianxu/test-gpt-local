"""Unit tests for Phase 4: checks tools.

Tests the internal helper functions of ``app.tools.checks`` in isolation.
"""

import subprocess
from pathlib import Path

from app.tools.checks import _git_hygiene, _list_checks, _run_check

# ═══════════════════════════════════════════════════════════════════════
# _list_checks — edge cases that don't need a running server
# ═══════════════════════════════════════════════════════════════════════


class TestListChecks:
    def test_returns_error_for_unknown_workspace(self) -> None:
        result = _list_checks("ws-nonexistent")
        assert "error" in result
        # Envelope format: error is a dict with code/message keys.
        error = result.get("error", {})
        if isinstance(error, dict):
            assert "workspace not found" in error.get("message", "")
        else:
            assert "workspace not found" in error

    def test_returns_error_for_invalid_format(self) -> None:
        result = _list_checks("not-a-valid-id")
        assert "error" in result


# ═══════════════════════════════════════════════════════════════════════
# _run_check — edge cases
# ═══════════════════════════════════════════════════════════════════════


class TestRunCheck:
    def test_returns_error_for_unknown_workspace(self) -> None:
        result = _run_check("ws-nonexistent", "unit_tests", wait=True)
        assert "error" in result
        error = result.get("error", {})
        if isinstance(error, dict):
            assert "workspace not found" in error.get("message", "")
        else:
            assert "workspace not found" in error

    def test_returns_error_for_invalid_workspace_id(self) -> None:
        result = _run_check("not-valid", "unit_tests", wait=True)
        assert "error" in result


class TestGitHygiene:
    def test_distinguishes_cleanliness_from_diff_check(self, tmp_path: Path) -> None:
        subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
        (tmp_path / "generated.txt").write_text("noise\n", encoding="utf-8")

        result = _git_hygiene(tmp_path, {"exit_code": 0})

        assert result["diff_check_passed"] is True
        assert result["working_tree_clean"] is False
        assert result["untracked_files"] == ["generated.txt"]
