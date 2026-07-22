"""Live acceptance for shared PTY/non-PTY process scheduling."""

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

    workspaces: list[tuple[str, Path]] = []
    for index in range(1, 5):
        workspace_id = f"ws-{index:08d}"
        worktree = tmp_path / workspace_id
        worktree.mkdir()
        db.insert_workspace(
            workspace_id, "project", "concurrency", str(worktree), "deadbeef"
        )
        workspaces.append((workspace_id, worktree))

    manager = ProcessManager()
    manager._processes_dir = tmp_path / "processes"
    manager._processes_dir.mkdir()
    processes: list[tuple[str, str]] = []
    try:
        for (workspace_id, worktree), tty in zip(workspaces[:3], (True, False, False)):
            spawned = manager.spawn_interactive(
                workspace_id,
                worktree,
                "import time; print('ready', flush=True); time.sleep(30)",
                shell="python",
                tty=tty,
                timeout_seconds=35,
            )
            assert "error" not in spawned, spawned
            processes.append((workspace_id, str(spawned["process_id"])))

        fourth_workspace, fourth_worktree = workspaces[3]
        with pytest.raises(RuntimeError, match="Maximum concurrent jobs"):
            manager.spawn_interactive(
                fourth_workspace,
                fourth_worktree,
                "import time; print('ready', flush=True); time.sleep(30)",
                shell="python",
                tty=False,
                timeout_seconds=35,
            )
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            paths = [
                Path(str(db.get_process(process_id)["stdout_path"]))  # type: ignore[index]
                for _, process_id in processes
            ]
            if all(path.is_file() and path.stat().st_size > 0 for path in paths):
                break
            time.sleep(0.05)
    finally:
        for _, process_id in processes:
            result = manager.cancel(process_id)
            assert result["status"] == "cancelled"

    assert all(
        db.get_process(process_id)["status"] == "cancelled"  # type: ignore[index]
        for _, process_id in processes
    )
