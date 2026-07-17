"""Unit tests for the workspace plan service.

Covers the acceptance criteria from the iteration plan (section 7),
including plan creation, step updates, revision control, evidence
attachment, and error handling.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.services import workspace_plan as plan_service
from app.storage import database as db

# ---------------------------------------------------------------------------
# Database setup
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _plan_db(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Give each test a fresh database with all tables."""
    existing = getattr(db._local, "conn", None)
    if existing is not None:
        existing.close()
        db._local.conn = None
    db_dir = tmp_path / ".plan_db"
    db_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(db, "_DB_PATH", db_dir / "operator.db")
    db.init_db()
    yield
    conn = getattr(db._local, "conn", None)
    if conn is not None:
        conn.close()
        db._local.conn = None


def _create_workspace(workspace_id: str = "ws-00000001", worktree: Path | None = None) -> None:
    """Insert a workspace record into the test database."""
    from app.storage.database import _now_iso

    wt = str(worktree) if worktree else "/tmp/workspace"
    conn = db._get_connection()
    conn.execute(
        """INSERT OR IGNORE INTO workspaces
           (workspace_id, project_id, task_name, worktree_path, base_commit,
            status, created_at, last_accessed_at, revision, current_head)
           VALUES (?, ?, ?, ?, ?, 'active', ?, ?, 1, ?)""",
        (
            workspace_id,
            "test-project",
            "plan-test",
            wt,
            "abc123",
            _now_iso(),
            _now_iso(),
            "abc123",
        ),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Plan Creation
# ---------------------------------------------------------------------------


class TestCreatePlan:
    def test_create_plan(self) -> None:
        _create_workspace()
        steps = [
            {"id": "step1", "text": "First step", "status": "pending"},
            {"id": "step2", "text": "Second step", "status": "in_progress"},
            {"id": "step3", "text": "Third step", "status": "completed"},
        ]

        result = plan_service.update_plan("ws-00000001", "Test plan", steps)
        plan = result["plan"]
        assert plan is not None
        assert plan["explanation"] == "Test plan"
        assert plan["revision"] == 1
        assert len(plan["steps"]) == 3

        # Verify step order.
        assert plan["steps"][0]["id"] == "step1"
        assert plan["steps"][1]["id"] == "step2"
        assert plan["steps"][2]["id"] == "step3"

        # Verify statuses.
        assert plan["steps"][0]["status"] == "pending"
        assert plan["steps"][1]["status"] == "in_progress"
        assert plan["steps"][2]["status"] == "completed"

    def test_create_plan_default_status(self) -> None:
        _create_workspace()
        steps = [{"id": "s1", "text": "Default status"}]

        result = plan_service.update_plan("ws-00000001", "Default test", steps)
        assert result["plan"]["steps"][0]["status"] == "pending"

    def test_create_plan_empty_explanation_rejected(self) -> None:
        _create_workspace()
        with pytest.raises(ValueError, match="explanation must be a non-empty string"):
            plan_service.update_plan("ws-00000001", "", [{"id": "s1", "text": "step"}])

    def test_create_plan_no_steps_rejected(self) -> None:
        _create_workspace()
        with pytest.raises(ValueError, match="must be a non-empty list"):
            plan_service.update_plan("ws-00000001", "test", [])

    def test_create_plan_duplicate_step_ids_rejected(self) -> None:
        _create_workspace()
        steps = [
            {"id": "s1", "text": "first"},
            {"id": "s1", "text": "second"},
        ]
        with pytest.raises(ValueError, match="duplicate step id"):
            plan_service.update_plan("ws-00000001", "test", steps)

    def test_create_plan_empty_step_id_rejected(self) -> None:
        _create_workspace()
        with pytest.raises(ValueError, match="step.id is required"):
            plan_service.update_plan("ws-00000001", "test", [{"id": "", "text": "empty"}])

    def test_create_plan_empty_step_text_rejected(self) -> None:
        _create_workspace()
        with pytest.raises(ValueError, match="step.text is required"):
            plan_service.update_plan("ws-00000001", "test", [{"id": "s1", "text": ""}])

    def test_create_plan_invalid_status_rejected(self) -> None:
        _create_workspace()
        with pytest.raises(ValueError, match="step.status must be one of"):
            plan_service.update_plan(
                "ws-00000001", "test", [{"id": "s1", "text": "step", "status": "invalid"}]
            )

    def test_create_plan_multiple_in_progress_rejected(self) -> None:
        _create_workspace()
        steps = [
            {"id": "s1", "text": "first", "status": "in_progress"},
            {"id": "s2", "text": "second", "status": "in_progress"},
        ]
        with pytest.raises(ValueError, match="at most one step"):
            plan_service.update_plan("ws-00000001", "test", steps)


# ---------------------------------------------------------------------------
# Plan Retrieval
# ---------------------------------------------------------------------------


class TestGetPlan:
    def test_get_plan_when_none_exists(self) -> None:
        _create_workspace("ws-00000002")
        result = plan_service.get_plan("ws-00000002")
        assert result["plan"] is None

    def test_get_plan_after_create(self) -> None:
        _create_workspace("ws-00000003")
        plan_service.update_plan(
            "ws-00000003", "my plan", [{"id": "s1", "text": "step one"}]
        )
        result = plan_service.get_plan("ws-00000003")
        assert result["plan"] is not None
        assert result["plan"]["explanation"] == "my plan"
        assert len(result["plan"]["steps"]) == 1


# ---------------------------------------------------------------------------
# Plan Update / Revision Control
# ---------------------------------------------------------------------------


class TestUpdatePlan:
    def test_update_plan_with_revision(self) -> None:
        _create_workspace("ws-00000004")
        # Create initial plan.
        plan_service.update_plan(
            "ws-00000004", "initial", [{"id": "s1", "text": "step one"}]
        )
        assert plan_service.get_plan("ws-00000004")["plan"]["revision"] == 1

        # Update with correct revision.
        plan_service.update_plan(
            "ws-00000004",
            "updated",
            [{"id": "s1", "text": "step one updated"}, {"id": "s2", "text": "step two"}],
            expected_revision=1,
        )
        assert plan_service.get_plan("ws-00000004")["plan"]["revision"] == 2

    def test_update_plan_revision_conflict(self) -> None:
        _create_workspace("ws-00000005")
        plan_service.update_plan(
            "ws-00000005", "initial", [{"id": "s1", "text": "step one"}]
        )

        with pytest.raises(ValueError, match="revision conflict"):
            plan_service.update_plan(
                "ws-00000005",
                "updated",
                [{"id": "s1", "text": "step one"}],
                expected_revision=99,
            )

    def test_update_plan_requires_expected_revision(self) -> None:
        _create_workspace("ws-00000006")
        plan_service.update_plan(
            "ws-00000006", "initial", [{"id": "s1", "text": "step one"}]
        )

        with pytest.raises(ValueError, match="expected_revision is required"):
            plan_service.update_plan(
                "ws-00000006", "updated", [{"id": "s1", "text": "step one"}]
            )


# ---------------------------------------------------------------------------
# Step Status Update
# ---------------------------------------------------------------------------


class TestUpdateStepStatus:
    def test_update_step_status(self) -> None:
        _create_workspace("ws-00000007")
        plan_service.update_plan(
            "ws-00000007", "plan", [{"id": "s1", "text": "step one"}]
        )

        result = plan_service.update_step_status(
            "ws-00000007", "s1", "completed", expected_revision=1
        )
        assert result["plan"]["steps"][0]["status"] == "completed"
        assert result["plan"]["revision"] == 2

    def test_update_step_status_with_evidence(self) -> None:
        _create_workspace("ws-00000008")
        plan_service.update_plan(
            "ws-00000008", "plan", [{"id": "s1", "text": "step one"}]
        )

        evidence = [{"type": "process_id", "id": "pr_abc123"}]
        result = plan_service.update_step_status(
            "ws-00000008", "s1", "completed", evidence=evidence
        )
        assert result["plan"]["steps"][0]["evidence"] == evidence

    def test_update_step_status_multiple_evidence(self) -> None:
        _create_workspace("ws-00000009")
        plan_service.update_plan(
            "ws-00000009", "plan", [{"id": "s1", "text": "step one"}]
        )

        evidence = [
            {"type": "process_id", "id": "pr_abc"},
            {"type": "check_id", "id": "unit_tests"},
            {"type": "artifact_id", "id": "artifact_xyz"},
        ]
        result = plan_service.update_step_status(
            "ws-00000009", "s1", "completed", evidence=evidence
        )
        assert len(result["plan"]["steps"][0]["evidence"]) == 3

    def test_update_step_status_two_in_progress_rejected(self) -> None:
        _create_workspace("ws-00000010")
        plan_service.update_plan(
            "ws-00000010",
            "plan",
            [
                {"id": "s1", "text": "step one", "status": "in_progress"},
                {"id": "s2", "text": "step two"},
            ],
        )

        with pytest.raises(ValueError, match="already 'in_progress'"):
            plan_service.update_step_status("ws-00000010", "s2", "in_progress")

    def test_update_step_status_invalid_status(self) -> None:
        _create_workspace("ws-00000011")
        plan_service.update_plan(
            "ws-00000011", "plan", [{"id": "s1", "text": "step one"}]
        )

        with pytest.raises(ValueError, match="status must be one of"):
            plan_service.update_step_status("ws-00000011", "s1", "invalid")

    def test_update_step_status_nonexistent_step(self) -> None:
        _create_workspace("ws-00000012")
        plan_service.update_plan(
            "ws-00000012", "plan", [{"id": "s1", "text": "step one"}]
        )

        with pytest.raises(ValueError, match="step not found"):
            plan_service.update_step_status("ws-00000012", "s99", "completed")


# ---------------------------------------------------------------------------
# Blocked Steps
# ---------------------------------------------------------------------------


class TestBlockedSteps:
    def test_blocked_step_with_reason(self) -> None:
        _create_workspace("ws-00000013")
        plan_service.update_plan(
            "ws-00000013", "plan", [{"id": "s1", "text": "step one"}]
        )

        result = plan_service.update_step_status(
            "ws-00000013", "s1", "blocked", blocked_reason="Waiting for review"
        )
        assert result["plan"]["steps"][0]["status"] == "blocked"
        assert result["plan"]["steps"][0]["blocked_reason"] == "Waiting for review"


# ---------------------------------------------------------------------------
# Plan Persistence
# ---------------------------------------------------------------------------


class TestPlanPersistence:
    def test_plan_survives_get_after_create(self) -> None:
        """Plan should be retrievable from the database after creation."""
        _create_workspace("ws-00000014")
        plan_service.update_plan(
            "ws-00000014",
            "persistent plan",
            [{"id": "s1", "text": "first"}, {"id": "s2", "text": "second"}],
        )

        # Retrieve plan (simulates server restart / new session).
        result = plan_service.get_plan("ws-00000014")
        assert result["plan"] is not None
        assert result["plan"]["explanation"] == "persistent plan"
        assert len(result["plan"]["steps"]) == 2
        assert result["plan"]["steps"][0]["id"] == "s1"
        assert result["plan"]["steps"][1]["id"] == "s2"


# ---------------------------------------------------------------------------
# Plan Deletion
# ---------------------------------------------------------------------------


class TestDeletePlan:
    def test_delete_plan(self) -> None:
        _create_workspace("ws-00000015")
        plan_service.update_plan(
            "ws-00000015", "to delete", [{"id": "s1", "text": "step"}]
        )
        assert plan_service.get_plan("ws-00000015")["plan"] is not None

        db.delete_plan("ws-00000015")
        assert plan_service.get_plan("ws-00000015")["plan"] is None
