from __future__ import annotations

import sys
from pathlib import Path

from app.services.event_store import EventStore
from app.services.process_manager import ProcessManager
from app.services.process_recovery import recover_processes
from app.storage import database as default_database
from app.storage.database import Database


def _manager(
    tmp_path: Path, **config_overrides: object
) -> tuple[ProcessManager, Database, EventStore]:
    database = Database(tmp_path / "operator.db")
    database.init_db()
    database.insert_workspace(
        "ws-00000001", "project", "events", str(tmp_path), "deadbeef"
    )
    event_store = EventStore(database)
    config: dict[str, object] = {
        "max_running_jobs": 2,
        "max_running_jobs_per_workspace": 2,
        "queue_timeout_seconds": 1,
        "heartbeat_interval_seconds": 0.05,
        "default_timeout_seconds": 10,
        "max_timeout_seconds": 10,
        "max_output_chars": 10_000,
        "output_tail_chars": 10_000,
        "max_output_bytes": 100_000,
        "register_artifacts": False,
    }
    config.update(config_overrides)
    manager = ProcessManager(
        database=database,
        event_store=event_store,
        processes_dir=tmp_path / "processes",
        config=config,
    )
    return manager, database, event_store


def test_process_lifecycle_and_output_events_are_durable(tmp_path: Path) -> None:
    manager, database, event_store = _manager(tmp_path)
    try:
        spawned = manager.spawn_interactive(
            "ws-00000001",
            tmp_path,
            "print('event-output')",
            shell=sys.executable,
            timeout_seconds=5,
            request_id="req_event_test",
        )
        process_id = str(spawned["process_id"])
        result = manager.wait_for_terminal(process_id, 5)
        assert result["status"] == "passed"
        assert "event-output" in result["stdout_tail"]

        page = event_store.list_after(
            None, workspace_id="ws-00000001", from_beginning=True
        )
        process_events = [
            event for event in page["events"] if event["process_id"] == process_id
        ]
        types = [event["type"] for event in process_events]
        assert types[0:2] == ["tool.queued", "tool.started"]
        assert "process.output" in types
        assert types[-1] == "process.exited"
        assert [event["sequence"] for event in process_events] == list(
            range(1, len(process_events) + 1)
        )
        terminal = process_events[-1]
        assert terminal["payload"]["status"] == "passed"
        assert terminal["payload"]["stdout_offset"] > 0

        restarted_store = EventStore(database)
        resumed = restarted_store.list_after(
            "evt1_0", workspace_id="ws-00000001"
        )
        assert any(
            event["process_id"] == process_id
            and event["type"] == "process.exited"
            for event in resumed["events"]
        )
    finally:
        manager.shutdown(grace_seconds=0, terminate_seconds=0)
        database.close()


def test_check_process_emits_check_completed(tmp_path: Path) -> None:
    manager, database, event_store = _manager(tmp_path)
    try:
        spawned = manager.spawn_interactive(
            "ws-00000001",
            tmp_path,
            "print('checked')",
            shell=sys.executable,
            tool_name="run_check:unit",
            timeout_seconds=5,
        )
        process_id = str(spawned["process_id"])
        assert manager.wait_for_terminal(process_id, 5)["status"] == "passed"
        page = event_store.list_after(
            None, workspace_id="ws-00000001", from_beginning=True
        )
        check_events = [
            event
            for event in page["events"]
            if event["process_id"] == process_id
            and event["type"] == "check.completed"
        ]
        assert check_events[0]["payload"] == {
            "check_id": "unit",
            "status": "passed",
            "process_id": process_id,
        }
    finally:
        manager.shutdown(grace_seconds=0, terminate_seconds=0)
        database.close()


def test_cancel_has_one_terminal_event(tmp_path: Path) -> None:
    manager, database, event_store = _manager(tmp_path)
    try:
        spawned = manager.spawn_interactive(
            "ws-00000001",
            tmp_path,
            "import time; print('ready', flush=True); time.sleep(30)",
            shell=sys.executable,
            timeout_seconds=10,
        )
        process_id = str(spawned["process_id"])
        assert manager.cancel(process_id)["status"] == "cancelled"
        page = event_store.list_after(
            None, workspace_id="ws-00000001", from_beginning=True
        )
        terminal = [
            event
            for event in page["events"]
            if event["process_id"] == process_id
            and event["type"] == "process.exited"
        ]
        assert len(terminal) == 1
        assert terminal[0]["payload"]["status"] == "cancelled"
    finally:
        manager.shutdown(grace_seconds=0, terminate_seconds=0)
        database.close()


def test_resource_limit_has_terminal_event(tmp_path: Path) -> None:
    manager, database, event_store = _manager(tmp_path, max_output_bytes=10)
    try:
        spawned = manager.spawn_interactive(
            "ws-00000001",
            tmp_path,
            "import time; print('x' * 100, flush=True); time.sleep(30)",
            shell=sys.executable,
            timeout_seconds=5,
        )
        process_id = str(spawned["process_id"])
        result = manager.wait_for_terminal(process_id, 3)
        assert result["status"] == "resource_exhausted"
        page = event_store.list_after(
            None, workspace_id="ws-00000001", from_beginning=True
        )
        terminal = [
            event
            for event in page["events"]
            if event["process_id"] == process_id
            and event["type"] == "process.exited"
        ]
        assert terminal[0]["payload"]["reason"] == "output_limit_exceeded"
    finally:
        manager.shutdown(grace_seconds=0, terminate_seconds=0)
        database.close()


def test_timeout_has_terminal_event(tmp_path: Path) -> None:
    manager, database, event_store = _manager(tmp_path)
    try:
        spawned = manager.spawn_interactive(
            "ws-00000001",
            tmp_path,
            "import time; time.sleep(30)",
            shell=sys.executable,
            timeout_seconds=1,
        )
        process_id = str(spawned["process_id"])
        result = manager.wait_for_terminal(process_id, 3)
        assert result["status"] == "timed_out"
        page = event_store.list_after(
            None, workspace_id="ws-00000001", from_beginning=True
        )
        terminal = [
            event
            for event in page["events"]
            if event["process_id"] == process_id
            and event["type"] == "process.exited"
        ]
        assert terminal[0]["payload"]["reason"] == "wall_clock_timeout"
    finally:
        manager.shutdown(grace_seconds=0, terminate_seconds=0)
        database.close()


def test_queue_timeout_has_failed_terminal_event(tmp_path: Path) -> None:
    manager, database, event_store = _manager(
        tmp_path,
        max_running_jobs=1,
        max_running_jobs_per_workspace=1,
        queue_timeout_seconds=0,
    )
    first = manager.spawn_interactive(
        "ws-00000001",
        tmp_path,
        "import time; time.sleep(30)",
        shell=sys.executable,
        timeout_seconds=10,
    )
    try:
        try:
            manager.spawn_interactive(
                "ws-00000001",
                tmp_path,
                "print('never-started')",
                shell=sys.executable,
                timeout_seconds=5,
            )
        except RuntimeError:
            pass
        else:
            raise AssertionError("second process should have hit the queue timeout")
        failed = next(
            record
            for record in database.list_processes("ws-00000001")
            if record["process_id"] != first["process_id"]
        )
        assert failed["status"] == "failed"
        assert failed["recovery_status"] == "queue_timeout"
        page = event_store.list_after(
            None, workspace_id="ws-00000001", from_beginning=True
        )
        types = [
            event["type"]
            for event in page["events"]
            if event["process_id"] == failed["process_id"]
        ]
        assert types == ["tool.queued", "process.exited"]
    finally:
        manager.cancel(str(first["process_id"]))
        manager.shutdown(grace_seconds=0, terminate_seconds=0)
        database.close()


def test_shutdown_emits_interrupted_terminal_event(tmp_path: Path) -> None:
    manager, database, event_store = _manager(tmp_path)
    spawned = manager.spawn_interactive(
        "ws-00000001",
        tmp_path,
        "import time; time.sleep(30)",
        shell=sys.executable,
        timeout_seconds=10,
    )
    process_id = str(spawned["process_id"])
    manager.shutdown(grace_seconds=0, terminate_seconds=0)
    page = event_store.list_after(
        None, workspace_id="ws-00000001", from_beginning=True
    )
    terminal = [
        event
        for event in page["events"]
        if event["process_id"] == process_id and event["type"] == "process.exited"
    ]
    assert terminal[0]["payload"]["status"] == "interrupted"
    assert terminal[0]["payload"]["reason"].startswith("service_shutdown_")
    database.close()


def test_recovery_disposition_emits_terminal_event(tmp_path: Path) -> None:
    manager, database, event_store = _manager(tmp_path)
    database.insert_process(
        "pr-00000001", "ws-00000001", "run_pwsh"
    )
    try:
        summary = recover_processes(database, manager)
        assert summary["interrupted"] == 1
        page = event_store.list_after(
            None, workspace_id="ws-00000001", from_beginning=True
        )
        terminal = [
            event
            for event in page["events"]
            if event["process_id"] == "pr-00000001"
            and event["type"] == "process.exited"
        ]
        assert len(terminal) == 1
        assert terminal[0]["payload"]["status"] == "interrupted"
        assert terminal[0]["payload"]["reason"] == "queued_without_process"
    finally:
        manager.shutdown(grace_seconds=0, terminate_seconds=0)
        database.close()


def test_process_output_artifact_emits_created_event(tmp_path: Path) -> None:
    default_database.insert_workspace(
        "ws-00000001", "project", "artifact-event", str(tmp_path), "deadbeef"
    )
    manager = ProcessManager(
        processes_dir=tmp_path / "processes",
        config={
            "max_running_jobs": 1,
            "max_running_jobs_per_workspace": 1,
            "queue_timeout_seconds": 1,
            "heartbeat_interval_seconds": 0.05,
            "default_timeout_seconds": 5,
            "max_timeout_seconds": 5,
            "max_output_chars": 10_000,
            "max_output_bytes": 100_000,
            "register_artifacts": True,
        },
    )
    try:
        spawned = manager.spawn_interactive(
            "ws-00000001",
            tmp_path,
            "print('artifact-event')",
            shell=sys.executable,
            timeout_seconds=5,
        )
        process_id = str(spawned["process_id"])
        cursor = manager.event_store.list_after(
            None, workspace_id="ws-00000001"
        )["cursor"]
        assert manager.wait_for_terminal(process_id, 5)["status"] == "passed"
        page = manager.event_store.wait_after(
            str(cursor),
            workspace_id="ws-00000001",
            process_id=process_id,
            event_types=["artifact.created"],
            timeout_seconds=2,
        )
        created = [
            event
            for event in page["events"]
            if event["process_id"] == process_id
            and event["type"] == "artifact.created"
        ]
        assert created
        assert all(
            not Path(str(event["payload"]["relative_path"])).is_absolute()
            for event in created
        )
    finally:
        manager.shutdown(grace_seconds=0, terminate_seconds=0)
