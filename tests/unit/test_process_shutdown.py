from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest

from app.services.process_manager import ProcessManager, _RunningProcess
from app.services.process_scheduler import ConcurrencyKey


class FakeDatabase:
    def __init__(self) -> None:
        self.statuses: dict[str, dict[str, Any]] = {}
        self.runtime: dict[str, dict[str, Any]] = {}

    def update_process_status(self, process_id: str, **values: Any) -> None:
        self.statuses.setdefault(process_id, {}).update(values)

    def update_process_runtime(self, process_id: str, **values: Any) -> None:
        self.runtime.setdefault(process_id, {}).update(values)


class FakeProcess:
    def __init__(self, pid: int, *, exits_on_terminate: bool) -> None:
        self.pid = pid
        self.returncode: int | None = None
        self.exits_on_terminate = exits_on_terminate
        self.terminate_calls = 0

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.terminate_calls += 1
        if self.exits_on_terminate:
            self.returncode = -15


def test_shutdown_interrupts_terminates_then_kills(tmp_path: Path) -> None:
    database = FakeDatabase()
    manager = ProcessManager(
        database=database,
        processes_dir=tmp_path / "processes",
        config={
            "max_running_jobs": 3,
            "max_running_jobs_per_workspace": 1,
            "queue_timeout_seconds": 0,
            "heartbeat_interval_seconds": 1,
            "default_timeout_seconds": 60,
            "max_timeout_seconds": 60,
            "max_output_chars": 1000,
            "output_tail_chars": 1000,
            "max_output_bytes": 1000,
            "register_artifacts": False,
        },
    )

    graceful = FakeProcess(101, exits_on_terminate=False)
    terminated = FakeProcess(102, exits_on_terminate=True)
    killed = FakeProcess(103, exits_on_terminate=False)
    processes = [graceful, terminated, killed]

    for index, proc in enumerate(processes, start=1):
        workspace_id = f"ws-{index}"
        lease = manager._scheduler.acquire(
            workspace_id,
            key=ConcurrencyKey.workspace(workspace_id),
            timeout_seconds=0,
        )
        rp = _RunningProcess(
            process_id=f"pr-0000000{index}",
            workspace_id=workspace_id,
            proc=proc,
            stdout_path=tmp_path / f"stdout-{index}.txt",
            stderr_path=tmp_path / f"stderr-{index}.txt",
            working_directory=tmp_path,
            deadline=time.monotonic() + 60,
            lease=lease,
        )
        if proc is graceful:
            rp.send_interrupt = lambda proc=proc: setattr(proc, "returncode", 0)  # type: ignore[method-assign]
        else:
            rp.send_interrupt = lambda: None  # type: ignore[method-assign]
        manager._running[rp.process_id] = rp

    def kill_tree(pid: int) -> bool:
        assert pid == killed.pid
        killed.returncode = -9
        return True

    manager._kill_tree = kill_tree  # type: ignore[method-assign]
    result = manager.shutdown(grace_seconds=0, terminate_seconds=0)

    assert result == {"interrupted": 1, "terminated": 1, "killed": 1, "total": 3}
    assert not manager._running
    assert manager.scheduler_snapshot()["active"] == 0
    assert {
        values["recovery_status"] for values in database.runtime.values()
    } == {
        "service_shutdown_interrupt",
        "service_shutdown_terminate",
        "service_shutdown_kill",
    }
    assert all(values["status"] == "interrupted" for values in database.statuses.values())

    with pytest.raises(RuntimeError, match="shutting down"):
        manager._acquire_lease("ws-new", write=True, priority=0)

    assert manager.shutdown() == {
        "interrupted": 0,
        "terminated": 0,
        "killed": 0,
        "total": 0,
    }
