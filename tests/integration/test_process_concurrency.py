"""Live acceptance for the shared PTY/non-PTY process limit."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from app.services.process_manager import ProcessManager
from app.storage import database as db


def test_pty_and_pipe_processes_share_concurrency_limit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    existing = getattr(db._local, "conn", None)
    if existing is not None:
        existing.close()
        db._local.conn = None
    monkeypatch.setattr(db, "_DB_PATH", tmp_path / "operator.db")
    db.init_db()
    db.insert_workspace(
        "ws-00000001", "project", "concurrency", str(tmp_path), "deadbeef"
    )

    manager = ProcessManager()
    manager._processes_dir = tmp_path / "processes"
    manager._processes_dir.mkdir()
    process_ids: list[str] = []
    try:
        for tty in (True, False, False):
            spawned = manager.spawn_interactive(
                "ws-00000001",
                tmp_path,
                "import time; print('ready', flush=True); time.sleep(30)",
                shell="python",
                tty=tty,
                timeout_seconds=35,
            )
            assert "error" not in spawned, spawned
            process_ids.append(str(spawned["process_id"]))

        with pytest.raises(RuntimeError, match="Maximum concurrent jobs"):
            manager.spawn_interactive(
                "ws-00000001",
                tmp_path,
                "import time; print('ready', flush=True); time.sleep(30)",
                shell="python",
                tty=False,
                timeout_seconds=35,
            )
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            paths = [Path(str(db.get_process(pid)["stdout_path"])) for pid in process_ids]  # type: ignore[index]
            if all(path.is_file() and path.stat().st_size > 0 for path in paths):
                break
            time.sleep(0.05)
    finally:
        for process_id in process_ids:
            result = manager.cancel(process_id)
            assert result["status"] == "cancelled"

    assert all(db.get_process(process_id)["status"] == "cancelled" for process_id in process_ids)  # type: ignore[index]
    assert all(
        db.list_artifacts_for_process("ws-00000001", process_id)
        for process_id in process_ids
    )
