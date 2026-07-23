"""Integration acceptance for workspace report, recovery, and cleanup."""

from __future__ import annotations

import hashlib
import json
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from app.services import artifact_registry, workspace_plan
from app.storage import database as db
from app.tools import reports


@pytest.fixture()
def acceptance_db(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Create an isolated state store and Git workspace."""
    existing = getattr(db._local, "conn", None)
    if existing is not None:
        existing.close()
        db._local.conn = None
    monkeypatch.setattr(db, "_DB_PATH", tmp_path / "operator.db")
    db.init_db()

    repository = tmp_path / "repository"
    repository.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repository, check=True)
    subprocess.run(["git", "config", "user.email", "acceptance@example.test"], cwd=repository)
    subprocess.run(["git", "config", "user.name", "Acceptance"], cwd=repository)
    (repository / "README.md").write_text("acceptance\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=repository, check=True)
    subprocess.run(["git", "commit", "-qm", "initial"], cwd=repository, check=True)
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repository, check=True,
        capture_output=True, text=True,
    ).stdout.strip()
    db.insert_workspace(
        "ws-00000001", "project", "acceptance", str(repository), head,
        main_head_at_creation=head,
        main_status_sha256_at_creation=hashlib.sha256(b"").hexdigest(),
    )
    monkeypatch.setattr(
        reports,
        "get_project",
        lambda project_id: {
            "id": project_id,
            "repository": str(repository),
            "checks": {"unit": {"script": "pytest -q"}},
        },
    )
    yield repository
    connection = getattr(db._local, "conn", None)
    if connection is not None:
        connection.close()
        db._local.conn = None


def _passed_check(repository: Path) -> str:
    process = db.insert_process(
        "pr-00000001", "ws-00000001", "run_check:unit",
        working_directory=str(repository),
    )
    db.update_process_status(
        process["process_id"], "passed", pid=123, exit_code=0,
        completed_at=db._now_iso(),
    )
    return str(process["process_id"])


def test_report_is_ready_with_completed_plan_and_test_evidence(
    acceptance_db: Path,
) -> None:
    process_id = _passed_check(acceptance_db)
    workspace_plan.update_plan(
        "ws-00000001",
        "Acceptance",
        [
            {"id": "implement", "text": "Implement feature", "status": "completed"},
            {
                "id": "test",
                "text": "Run unit tests",
                "status": "completed",
                "evidence": [{"type": "process_id", "id": process_id}],
            },
        ],
    )
    artifact_registry.register_artifact(
        "ws-00000001", str(acceptance_db / "README.md"), kind="text"
    )

    result = reports._get_workspace_report("ws-00000001")["result"]

    assert result["plan"]["revision"] == 1
    assert result["plan"]["completed"] == 2
    assert result["plan"]["pending"] == 0
    assert result["artifacts"]["count"] == 1
    assert result["active_processes"] == []
    assert result["acceptance_ready"] is True
    assert result["acceptance_blockers"] == []


def test_active_process_and_missing_test_evidence_block_acceptance(
    acceptance_db: Path,
) -> None:
    _passed_check(acceptance_db)
    workspace_plan.update_plan(
        "ws-00000001",
        "Acceptance",
        [{"id": "test", "text": "Run tests", "status": "completed"}],
    )
    db.insert_process(
        "pr-00000002", "ws-00000001", "run_pwsh",
        working_directory=str(acceptance_db),
    )

    result = reports._get_workspace_report("ws-00000001")["result"]

    assert [item["process_id"] for item in result["active_processes"]] == ["pr-00000002"]
    assert result["acceptance_ready"] is False
    assert "active_processes" in result["acceptance_blockers"]
    assert "test_step_missing_successful_evidence" in result["acceptance_blockers"]


def test_report_bounds_process_output_and_compacts_artifacts(
    acceptance_db: Path,
) -> None:
    process_id = _passed_check(acceptance_db)
    process_dir = acceptance_db / "process-output"
    process_dir.mkdir()
    stdout_path = process_dir / "stdout.txt"
    stderr_path = process_dir / "stderr.txt"
    stdout_path.write_text("x" * 100_000, encoding="utf-8")
    stderr_path.write_text("y" * 100_000, encoding="utf-8")
    connection = db._get_connection()
    connection.execute(
        "UPDATE processes SET stdout_path = ?, stderr_path = ? WHERE process_id = ?",
        (str(stdout_path), str(stderr_path), process_id),
    )
    connection.commit()
    artifact = artifact_registry.register_artifact(
        "ws-00000001", str(acceptance_db / "README.md"), kind="text"
    )
    connection.execute(
        "UPDATE artifacts SET source_process_id = ? WHERE artifact_id = ?",
        (process_id, artifact["artifact_id"]),
    )
    connection.commit()

    envelope = reports._get_workspace_report("ws-00000001")
    process = envelope["result"]["processes"][0]

    assert len(process["stdout_tail"]) == reports._REPORT_PROCESS_OUTPUT_CHARS
    assert len(process["stderr_tail"]) == reports._REPORT_PROCESS_OUTPUT_CHARS
    assert process["truncated"] is True
    assert process["artifact_ids"] == [artifact["artifact_id"]]
    assert "artifacts" not in process
    assert len(json.dumps(envelope)) < 20_000


def test_recovery_cleanup_and_concurrent_artifact_registration(
    acceptance_db: Path,
) -> None:
    artifact_path = acceptance_db / "README.md"

    with ThreadPoolExecutor(max_workers=4) as executor:
        records = list(
            executor.map(
                lambda _: artifact_registry.register_artifact(
                    "ws-00000001", str(artifact_path), kind="text"
                ),
                range(8),
            )
        )
    assert len({record["artifact_id"] for record in records}) == 1
    artifact_audits = [
        operation
        for operation in db.list_operations("ws-00000001")
        if operation["tool_name"] == "register_artifact"
    ]
    assert artifact_audits
    assert all(operation["request_id"] for operation in artifact_audits)
    assert all(operation["actor"] == "mcp" for operation in artifact_audits)
    assert all(operation["result_status"] for operation in artifact_audits)
    assert all(operation["duration_ms"] is not None for operation in artifact_audits)

    db.insert_process(
        "pr-00000003", "ws-00000001", "run_pwsh",
        working_directory=str(acceptance_db),
    )
    assert db.interrupt_incomplete_processes() == 1
    assert db.get_process("pr-00000003")["status"] == "interrupted"  # type: ignore[index]

    workspace_plan.update_plan(
        "ws-00000001", "Cleanup", [{"id": "done", "text": "Done"}]
    )
    assert db.delete_workspace("ws-00000001") is True
    assert db.get_workspace("ws-00000001") is None
    assert db.get_plan("ws-00000001") is None
    assert db.get_artifact(records[0]["artifact_id"]) is None
    assert db.get_process("pr-00000003") is None
