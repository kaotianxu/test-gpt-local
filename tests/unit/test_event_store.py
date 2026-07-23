from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

import pytest

from app.services.event_store import (
    CursorExpiredError,
    EventStore,
    EventWaitLimitError,
    InvalidCursorError,
    decode_cursor,
    encode_cursor,
)
from app.storage.database import Database


@pytest.fixture
def event_database(tmp_path: Path) -> Database:
    database = Database(tmp_path / "events.db")
    database.init_db()
    database.insert_workspace(
        "ws-00000001", "project", "events", str(tmp_path), "deadbeef"
    )
    database.insert_workspace(
        "ws-00000002", "project", "other", str(tmp_path), "deadbeef"
    )
    yield database
    database.close()


def test_cursor_round_trip_and_validation() -> None:
    assert decode_cursor(encode_cursor(184)) == 184
    with pytest.raises(InvalidCursorError):
        decode_cursor("184")


def test_append_sequence_and_workspace_isolation(event_database: Database) -> None:
    store = EventStore(event_database)
    first = store.append(
        "tool.queued",
        workspace_id="ws-00000001",
        process_id="pr-00000001",
        payload={"tool_name": "run_pwsh", "priority": 0},
    )
    second = store.append(
        "tool.started",
        workspace_id="ws-00000001",
        process_id="pr-00000001",
        payload={
            "tool_name": "run_pwsh",
            "pid": 10,
            "working_directory_redacted": ".",
            "recovered": False,
        },
    )
    store.append(
        "tool.queued",
        workspace_id="ws-00000002",
        process_id="pr-00000002",
        payload={"tool_name": "run_pwsh", "priority": 0},
    )

    page = store.list_after(
        None, workspace_id="ws-00000001", from_beginning=True
    )
    assert [event["event_id"] for event in page["events"]] == [
        first["event_id"],
        second["event_id"],
    ]
    assert [event["sequence"] for event in page["events"]] == [1, 2]
    assert all(event["workspace_id"] == "ws-00000001" for event in page["events"])


def test_wait_after_wakes_without_polling(event_database: Database) -> None:
    store = EventStore(event_database)
    tail = store.list_after(None, workspace_id="ws-00000001")["cursor"]
    result: dict[str, Any] = {}

    def wait() -> None:
        result.update(
            store.wait_after(
                str(tail),
                workspace_id="ws-00000001",
                timeout_seconds=2,
            )
        )

    thread = threading.Thread(target=wait)
    thread.start()
    time.sleep(0.05)
    store.append(
        "tool.queued",
        workspace_id="ws-00000001",
        process_id="pr-00000001",
        payload={"tool_name": "run_pwsh", "priority": 0},
    )
    thread.join(timeout=2)

    assert not thread.is_alive()
    assert result["timed_out"] is False
    assert [event["type"] for event in result["events"]] == ["tool.queued"]


def test_wait_timeout_is_successful_empty_page(event_database: Database) -> None:
    store = EventStore(event_database)
    page = store.wait_after(
        None, workspace_id="ws-00000001", timeout_seconds=0.01
    )
    assert page["events"] == []
    assert page["timed_out"] is True


def test_waiter_limit_is_bounded(event_database: Database) -> None:
    store = EventStore(event_database, max_waiters=1)
    tail = store.list_after(None, workspace_id="ws-00000001")["cursor"]
    entered = threading.Event()

    def wait() -> None:
        entered.set()
        store.wait_after(
            str(tail), workspace_id="ws-00000001", timeout_seconds=0.3
        )

    thread = threading.Thread(target=wait)
    thread.start()
    entered.wait(timeout=1)
    time.sleep(0.02)
    with pytest.raises(EventWaitLimitError):
        store.wait_after(
            str(tail), workspace_id="ws-00000001", timeout_seconds=0.1
        )
    thread.join(timeout=1)


def test_sensitive_and_oversized_payloads_are_rejected(
    event_database: Database,
) -> None:
    store = EventStore(event_database, max_payload_bytes=64)
    with pytest.raises(ValueError, match="sensitive"):
        store.append(
            "tool.queued",
            workspace_id="ws-00000001",
            payload={"command": "secret"},
        )
    with pytest.raises(ValueError, match="max_payload_bytes"):
        store.append(
            "tool.queued",
            workspace_id="ws-00000001",
            payload={"tool_name": "x" * 100},
        )
    with pytest.raises(ValueError, match="sensitive"):
        store.append(
            "artifact.created",
            workspace_id="ws-00000001",
            payload={
                "artifact_id": "artifact_1",
                "kind": "text",
                "relative_path": str(event_database.path.resolve()),
                "size_bytes": 1,
            },
        )


def test_terminal_transition_is_atomic_and_idempotent(
    event_database: Database,
) -> None:
    event_database.insert_process(
        "pr-00000001", "ws-00000001", "run_pwsh"
    )
    store = EventStore(event_database)
    first = store.transition_process(
        "pr-00000001", "passed", exit_code=0, reason="process_exit"
    )
    second = store.transition_process(
        "pr-00000001", "failed", exit_code=1, reason="late_finalize"
    )

    assert first["event_id"] == second["event_id"]
    assert event_database.get_process("pr-00000001")["status"] == "passed"
    page = store.list_after(
        None, workspace_id="ws-00000001", from_beginning=True
    )
    assert [event["type"] for event in page["events"]] == ["process.exited"]


def test_cleanup_expires_old_cursor(event_database: Database) -> None:
    store = EventStore(event_database)
    first = store.append(
        "tool.queued",
        workspace_id="ws-00000001",
        payload={"tool_name": "one", "priority": 0},
    )
    store.append(
        "tool.queued",
        workspace_id="ws-00000001",
        payload={"tool_name": "two", "priority": 0},
    )
    assert store.cleanup(retention_days=7, max_events_per_workspace=1) == 1
    with pytest.raises(CursorExpiredError) as exc_info:
        store.list_after(
            encode_cursor(first["event_id"] - 1),
            workspace_id="ws-00000001",
        )
    assert decode_cursor(exc_info.value.recovery_cursor) == first["event_id"]


def test_workspace_delete_removes_events_and_retention_state(
    event_database: Database,
) -> None:
    store = EventStore(event_database)
    store.append(
        "tool.queued",
        workspace_id="ws-00000001",
        payload={"tool_name": "one", "priority": 0},
    )
    store.append(
        "tool.queued",
        workspace_id="ws-00000001",
        payload={"tool_name": "two", "priority": 0},
    )
    store.cleanup(retention_days=7, max_events_per_workspace=1)
    assert event_database.delete_workspace("ws-00000001") is True
    connection = event_database.connect()
    assert connection.execute(
        "SELECT COUNT(*) FROM events WHERE workspace_id = 'ws-00000001'"
    ).fetchone()[0] == 0
    assert connection.execute(
        "SELECT COUNT(*) FROM event_retention_state "
        "WHERE workspace_id = 'ws-00000001'"
    ).fetchone()[0] == 0
