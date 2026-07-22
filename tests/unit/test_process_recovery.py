from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from app.services.process_recovery import recover_processes


class FakeDatabase:
    def __init__(self, records: list[dict[str, Any]]) -> None:
        self.records = records
        self.status_updates: list[tuple[str, str]] = []
        self.runtime_updates: list[tuple[str, dict[str, Any]]] = []

    def list_incomplete_processes(self) -> list[dict[str, Any]]:
        return self.records

    def update_process_status(self, process_id: str, status: str, **_: Any) -> None:
        self.status_updates.append((process_id, status))

    def update_process_runtime(self, process_id: str, **values: Any) -> None:
        self.runtime_updates.append((process_id, values))


class FakeManager:
    def __init__(self, adopted: bool) -> None:
        self.adopted = adopted
        self.records: list[dict[str, Any]] = []

    def adopt_recovered_process(self, record: dict[str, Any]) -> bool:
        self.records.append(record)
        return self.adopted


@pytest.mark.parametrize(
    ("record", "alive", "identity", "expected"),
    [
        ({"process_id": "pr-queued", "status": "queued"}, True, "same", "interrupted"),
        (
            {"process_id": "pr-dead", "status": "running", "pid": 10},
            False,
            "same",
            "interrupted",
        ),
        (
            {
                "process_id": "pr-reused",
                "status": "running",
                "pid": 11,
                "process_creation_identity": "old",
            },
            True,
            "new",
            "lost",
        ),
        (
            {"process_id": "pr-unknown", "status": "running", "pid": 12},
            True,
            "current",
            "recovery_required",
        ),
    ],
)
def test_recovery_classifies_unsafe_records(
    record: dict[str, Any], alive: bool, identity: str, expected: str
) -> None:
    database = FakeDatabase([record])
    result = recover_processes(
        database,
        alive_checker=lambda _: alive,
        identity_reader=lambda _: identity,
    )

    assert result[expected] == 1
    assert result["records"] == [
        {"process_id": record["process_id"], "disposition": expected}
    ]
    assert database.status_updates[-1] == (record["process_id"], expected)


def test_matching_identity_is_adopted_and_output_offset_persisted(tmp_path: Path) -> None:
    stdout = tmp_path / "stdout.txt"
    stderr = tmp_path / "stderr.txt"
    stdout.write_bytes(b"abc")
    stderr.write_bytes(b"12345")
    record = {
        "process_id": "pr-live",
        "workspace_id": "ws-a",
        "status": "running",
        "pid": 20,
        "process_creation_identity": "identity",
        "stdout_path": str(stdout),
        "stderr_path": str(stderr),
    }
    database = FakeDatabase([record])
    manager = FakeManager(adopted=True)

    result = recover_processes(
        database,
        manager,
        alive_checker=lambda _: True,
        identity_reader=lambda _: "identity",
    )

    assert result["recovered"] == 1
    assert manager.records == [record]
    assert database.status_updates == []
    assert database.runtime_updates[-1][1]["last_output_offset"] == 8
    assert database.runtime_updates[-1][1]["recovery_status"] == "recovered"


def test_failed_monitor_adoption_requires_operator_recovery() -> None:
    record = {
        "process_id": "pr-live",
        "status": "running",
        "pid": 20,
        "process_creation_identity": "identity",
    }
    database = FakeDatabase([record])

    result = recover_processes(
        database,
        FakeManager(adopted=False),
        alive_checker=lambda _: True,
        identity_reader=lambda _: "identity",
    )

    assert result["recovery_required"] == 1
    assert database.status_updates[-1] == ("pr-live", "recovery_required")
