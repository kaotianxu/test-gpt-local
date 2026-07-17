"""Real Windows ConPTY acceptance for an interactive Python REPL."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from app.services.process_manager import ProcessManager, _has_conpty
from app.storage import database as db


@pytest.mark.skipif(os.name != "nt", reason="ConPTY is Windows-specific")
def test_python_repl_over_conpty(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    existing = getattr(db._local, "conn", None)
    if existing is not None:
        existing.close()
        db._local.conn = None
    monkeypatch.setattr(db, "_DB_PATH", tmp_path / "operator.db")
    db.init_db()
    db.insert_workspace(
        "ws-00000001", "project", "conpty", str(tmp_path), "deadbeef"
    )

    assert _has_conpty() is True
    manager = ProcessManager()
    manager._processes_dir = tmp_path / "processes"
    manager._processes_dir.mkdir()
    spawned = manager.spawn_interactive(
        "ws-00000001",
        tmp_path,
        "",
        shell="python",
        tty=True,
        timeout_seconds=20,
        columns=100,
        rows=30,
    )
    assert "error" not in spawned, spawned
    assert spawned["tty"] is True
    process_id = str(spawned["process_id"])

    try:
        manager.write_input(process_id, "print(6 * 7)")
        deadline = time.monotonic() + 8
        output = ""
        result = manager.get_result(process_id)
        while time.monotonic() < deadline:
            result = manager.get_result(process_id)
            output = result["stdout_tail"]
            if "42" in output:
                break
            time.sleep(0.1)
        running = manager._get_running(process_id)
        diagnostics = {
            "result": result,
            "reader_alive": bool(
                running and running.pty_reader_thread and running.pty_reader_thread.is_alive()
            ),
            "stdout_size": running.stdout_path.stat().st_size if running else -1,
        }
        assert "42" in output, diagnostics
        assert manager.resize_terminal(process_id, 120, 40)["status"] == "resized"
        assert manager.send_signal(process_id, "eof")["status"] == "eof_sent"

        deadline = time.monotonic() + 8
        result = manager.get_result(process_id)
        while result["status"] == "running" and time.monotonic() < deadline:
            time.sleep(0.1)
            result = manager.get_result(process_id)
        assert result["status"] == "passed", result
        assert any(item["source_process_id"] == process_id for item in result["artifacts"])
    finally:
        manager.cancel(process_id)
