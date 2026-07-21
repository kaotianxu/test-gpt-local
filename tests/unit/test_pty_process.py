"""Unit tests for PTY / interactive process tools.

Covers the acceptance criteria from the iteration plan (section 5),
including process creation, stdin writing, signal sending, and error
handling.
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from app.services.process_manager import ProcessManager, _PseudoProcess, _RunningProcess
from app.storage.database import Database
from app.tools.pty_process import (
    _run_command,
    _send_process_signal,
    _write_process_input,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def isolated_process_manager(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Iterator[ProcessManager]:
    """Give every test independent DB, process registry, and output paths."""
    database = Database(tmp_path / "operator.db")
    database.init_db()
    manager = ProcessManager(
        database=database,
        config={
            "max_running_jobs": 3,
            "max_output_chars": 200000,
            "default_timeout_seconds": 10,
            "max_timeout_seconds": 60,
            "register_artifacts": False,
        },
        processes_dir=tmp_path / "processes",
    )
    monkeypatch.setattr(
        ProcessManager,
        "get_instance",
        classmethod(lambda cls: manager),
    )
    yield manager
    for process_id in list(manager._running):
        manager.cancel(process_id)
    database.close()


def _create_workspace_db(
    database: Database, workspace_id: str, worktree: Path
) -> None:
    """Insert a workspace record into the test database."""
    from app.storage.database import _now_iso

    conn = database.connect()
    conn.execute(
        """INSERT OR IGNORE INTO workspaces
           (workspace_id, project_id, task_name, worktree_path, base_commit,
            status, created_at, last_accessed_at, revision, current_head)
           VALUES (?, ?, ?, ?, ?, 'active', ?, ?, 1, ?)""",
        (
            workspace_id,
            "test-project",
            "pty-test",
            str(worktree),
            "abc123",
            _now_iso(),
            _now_iso(),
            "abc123",
        ),
    )
    conn.commit()


def _mock_workspace(
    monkeypatch: pytest.MonkeyPatch, worktree: Path, workspace_id: str = "ws-00000001"
) -> None:
    """Patch get_workspace to return a minimal record."""

    def fake_get_workspace(wid: str) -> dict[str, Any] | None:
        if wid == workspace_id:
            return {
                "workspace_id": wid,
                "project_id": "test-project",
                "worktree_path": str(worktree),
                "status": "active",
                "base_commit": "abc123",
            }
        return None

    import app.tools.pty_process as pty_mod

    monkeypatch.setattr(pty_mod, "get_workspace", fake_get_workspace)


def _mock_project(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch get_project to return a minimal project config."""

    def fake_get_project(project_id: str) -> dict[str, Any] | None:
        return {"project_id": project_id, "worktree_root": str(Path.cwd())}

    import app.tools.pty_process as pty_mod

    monkeypatch.setattr(pty_mod, "get_project", fake_get_project)


def _add_process_to_db(
    database: Database, process_id: str, workspace_id: str
) -> None:
    """Add a minimal process record to the database."""
    database.insert_process(
        process_id=process_id,
        workspace_id=workspace_id,
        tool_name="run_command",
        script_sha256="abc",
        script_preview="test",
        working_directory=str(Path.cwd()),
    )
    database.update_process_status(process_id, "running")


# ---------------------------------------------------------------------------
# _PseudoProcess
# ---------------------------------------------------------------------------


def test_database_instances_are_isolated(tmp_path: Path) -> None:
    """Two databases used on the same thread must never share state."""
    first = Database(tmp_path / "first.db")
    second = Database(tmp_path / "second.db")
    first.init_db()
    second.init_db()

    first.insert_workspace(
        "ws-00000001", "project", "first", str(tmp_path), "deadbeef"
    )

    assert first.get_workspace("ws-00000001") is not None
    assert second.get_workspace("ws-00000001") is None
    first.close()
    second.close()


class TestPseudoProcess:
    def test_construct(self) -> None:
        proc = _PseudoProcess(pid=12345, handle=0)
        assert proc.pid == 12345
        assert proc.poll() is not None  # handle 0 is invalid, so poll returns -1

    def test_terminate_does_not_crash_on_invalid_handle(self) -> None:
        proc = _PseudoProcess(pid=99999, handle=0)
        # Should not raise
        proc.terminate()


# ---------------------------------------------------------------------------
# _RunningProcess: write_stdin / close_stdin / send_interrupt
# ---------------------------------------------------------------------------


class TestRunningProcessStdin:
    def test_write_stdin_pipe_mode(self, tmp_path: Path) -> None:
        """Test that write_stdin works with a pipe-based process."""
        proc = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import sys; sys.stdout.write(sys.stdin.read()); sys.stdout.flush()",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )

        rp = _RunningProcess(
            process_id="pr-00000001",
            workspace_id="ws-00000001",
            proc=proc,
            stdout_path=tmp_path / "stdout.txt",
            stderr_path=tmp_path / "stderr.txt",
            working_directory=tmp_path,
            deadline=9999999999,
            stdin_pipe=proc.stdin,
        )

        # Write input and close stdin.
        rp.write_stdin("hello", append_newline=False)
        rp.close_stdin()

        stdout, _ = proc.communicate(timeout=5)
        assert stdout == "hello"

    def test_write_stdin_with_newline(self, tmp_path: Path) -> None:
        proc = subprocess.Popen(
            [sys.executable, "-c", "import sys; print(sys.stdin.readline().strip())"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )

        rp = _RunningProcess(
            process_id="pr-00000002",
            workspace_id="ws-00000001",
            proc=proc,
            stdout_path=tmp_path / "stdout.txt",
            stderr_path=tmp_path / "stderr.txt",
            working_directory=tmp_path,
            deadline=9999999999,
            stdin_pipe=proc.stdin,
        )

        rp.write_stdin("hello world")
        rp.close_stdin()

        stdout, _ = proc.communicate(timeout=5)
        assert stdout.strip() == "hello world"

    def test_close_stdin_raises_after_close(self, tmp_path: Path) -> None:
        """After close_stdin, write_stdin should raise RuntimeError."""
        import subprocess

        proc = subprocess.Popen(
            [sys.executable, "-c", "import sys; sys.stdin.read()"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )

        rp = _RunningProcess(
            process_id="pr-00000003",
            workspace_id="ws-00000001",
            proc=proc,
            stdout_path=tmp_path / "stdout.txt",
            stderr_path=tmp_path / "stderr.txt",
            working_directory=tmp_path,
            deadline=9999999999,
            stdin_pipe=proc.stdin,
        )

        rp.close_stdin()
        with pytest.raises(RuntimeError, match="process stdin is not available"):
            rp.write_stdin("test")


# ---------------------------------------------------------------------------
# ProcessManager: write_input
# ---------------------------------------------------------------------------


class TestProcessManagerWriteInput:
    def test_write_input_to_running_process(self, tmp_path: Path) -> None:
        """Integration: write to a running Python process."""
        pm = ProcessManager.get_instance()
        worktree = tmp_path / "worktree"
        worktree.mkdir(parents=True, exist_ok=True)
        _create_workspace_db(pm._database, "ws-00000001", worktree)

        # Start a Python process that reads from stdin line by line.
        result = pm.spawn_interactive(
            workspace_id="ws-00000001",
            worktree_path=worktree,
            command=(
                'import sys\nfor line in sys.stdin:\n    if line.strip() == "exit":\n'
                '        break\n    print(f"got: {line.strip()}")'
            ),
            shell="python",
            tty=False,
            timeout_seconds=10,
        )

        assert "error" not in result, result.get("error", "")
        process_id = result["process_id"]
        assert result["status"] == "running"

        # Write input and check output.
        write_result = pm.write_input(process_id, "hello", append_newline=True)
        assert "error" not in write_result, write_result.get("error", "")

        write_result = pm.write_input(process_id, "exit", append_newline=True)
        assert "error" not in write_result, write_result.get("error", "")

        # Wait for process to finish.
        import time

        for _ in range(50):
            time.sleep(0.1)
            rec = pm.get_result(process_id)
            if rec.get("status") in ("passed", "failed", "timed_out", "cancelled"):
                break

        status = rec.get("status", "")
        assert status == "passed", f"expected passed, got {status}: {rec.get('stdout_tail', '')}"

    def test_write_input_to_nonexistent_process(self) -> None:
        pm = ProcessManager.get_instance()
        result = pm.write_input("pr-00000000", "hello")
        assert "error" in result
        assert "not found" in result["error"]

    def test_write_input_to_finished_process(self, tmp_path: Path) -> None:
        pm = ProcessManager.get_instance()
        worktree = tmp_path / "worktree2"
        worktree.mkdir(parents=True, exist_ok=True)
        _create_workspace_db(pm._database, "ws-00000002", worktree)

        # Start a quick process.
        result = pm.spawn_interactive(
            workspace_id="ws-00000002",
            worktree_path=worktree,
            command="print('done')",
            shell="python",
            tty=False,
            timeout_seconds=10,
        )

        assert "error" not in result, result.get("error", "")
        process_id = result["process_id"]

        # Wait for it to finish.
        import time

        for _ in range(50):
            time.sleep(0.1)
            rec = pm.get_result(process_id)
            if rec.get("status") in ("passed", "failed", "timed_out", "cancelled"):
                break

        # Now try to write.
        write_result = pm.write_input(process_id, "hello")
        assert "error" in write_result
        assert "not running" in write_result["error"]


# ---------------------------------------------------------------------------
# ProcessManager: send_signal
# ---------------------------------------------------------------------------


class TestProcessManagerSendSignal:
    def test_send_eof(self, tmp_path: Path) -> None:
        """Send EOF to a Python process that reads stdin."""
        pm = ProcessManager.get_instance()
        worktree = tmp_path / "worktree3"
        worktree.mkdir(parents=True, exist_ok=True)
        _create_workspace_db(pm._database, "ws-00000003", worktree)

        result = pm.spawn_interactive(
            workspace_id="ws-00000003",
            worktree_path=worktree,
            command="import sys; data = sys.stdin.read(); print(f'read {len(data)} chars')",
            shell="python",
            tty=False,
            timeout_seconds=10,
        )

        assert "error" not in result, result.get("error", "")
        process_id = result["process_id"]

        # Write some input, then send EOF.
        pm.write_input(process_id, "hello", append_newline=False)
        pm.send_signal(process_id, "eof")

        import time

        for _ in range(50):
            time.sleep(0.1)
            rec = pm.get_result(process_id)
            if rec.get("status") in ("passed", "failed", "timed_out", "cancelled"):
                break

        assert rec.get("status") == "passed", (
            f"expected passed, got {rec.get('status')}: {rec.get('stdout_tail', '')}"
        )

    def test_send_terminate(self, tmp_path: Path) -> None:
        """Send terminate to a running process."""
        pm = ProcessManager.get_instance()
        worktree = tmp_path / "worktree4"
        worktree.mkdir(parents=True, exist_ok=True)
        _create_workspace_db(pm._database, "ws-00000004", worktree)

        # Start a long-running process.
        result = pm.spawn_interactive(
            workspace_id="ws-00000004",
            worktree_path=worktree,
            command="import time; time.sleep(30)",
            shell="python",
            tty=False,
            timeout_seconds=60,
        )

        assert "error" not in result, result.get("error", "")
        process_id = result["process_id"]

        # Send terminate.
        signal_result = pm.send_signal(process_id, "terminate")
        assert "error" not in signal_result, signal_result.get("error", "")

        import time

        for _ in range(30):
            time.sleep(0.1)
            rec = pm.get_result(process_id)
            if rec.get("status") in ("cancelled",):
                break

        assert rec.get("status") in ("cancelled", "failed"), (
            f"expected cancelled or failed, got {rec.get('status')}"
        )

    def test_send_signal_to_nonexistent_process(self) -> None:
        pm = ProcessManager.get_instance()
        result = pm.send_signal("pr-00000000", "interrupt")
        assert "error" in result
        assert "not found" in result["error"]


# ---------------------------------------------------------------------------
# run_command tool
# ---------------------------------------------------------------------------


class TestRunCommand:
    def test_unknown_workspace(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import app.tools.pty_process as pty_mod

        monkeypatch.setattr(pty_mod, "get_workspace", lambda wid: None)
        result = _run_command("ws-00000000", "echo hello")
        assert isinstance(result, dict)
        assert result["ok"] is False
        assert result["error"]["code"] == "WORKSPACE_NOT_FOUND"

    def test_empty_command(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        worktree = tmp_path / "ws"
        worktree.mkdir()
        _mock_workspace(monkeypatch, worktree)
        result = _run_command("ws-00000001", "")
        assert isinstance(result, dict)
        assert result["ok"] is False

    def test_invalid_shell(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        worktree = tmp_path / "ws2"
        worktree.mkdir()
        _mock_workspace(monkeypatch, worktree)
        result = _run_command("ws-00000001", "echo hello", shell="invalid")
        assert isinstance(result, dict)
        assert result["ok"] is False
        assert result["error"]["code"] == "INVALID_INPUT"


# ---------------------------------------------------------------------------
# write_process_input tool
# ---------------------------------------------------------------------------


class TestWriteProcessInput:
    def test_nonexistent_process(self) -> None:
        result = _write_process_input("pr-00000000", "hello")
        assert isinstance(result, dict)
        assert result["ok"] is False
        assert result["error"]["code"] == "PROCESS_NOT_FOUND"

    def test_empty_text_rejected(self) -> None:
        result = _write_process_input("pr-00000001", "", append_newline=False)
        assert isinstance(result, dict)
        assert result["ok"] is False
        assert result["error"]["code"] == "INVALID_INPUT"


# ---------------------------------------------------------------------------
# send_process_signal tool
# ---------------------------------------------------------------------------


class TestSendProcessSignal:
    def test_nonexistent_process(self) -> None:
        result = _send_process_signal("pr-00000000", "interrupt")
        assert isinstance(result, dict)
        assert result["ok"] is False
        assert result["error"]["code"] == "PROCESS_NOT_FOUND"

    def test_invalid_signal(self) -> None:
        result = _send_process_signal("pr-00000001", "invalid")
        assert isinstance(result, dict)
        assert result["ok"] is False
        assert result["error"]["code"] == "INVALID_INPUT"
