"""Unit coverage for Phase 5 service state, configuration, and logging."""

import asyncio
import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from mcp.server.fastmcp import FastMCP

from app.config import load_operator_config
from app.services import logging_config
from app.services.logging_config import configure_component_logger
from app.services.service_state import (
    ServiceLock,
    ServiceStateStore,
    process_creation_identity,
    process_matches,
)
from app.services.supervisor import (
    ChildRuntime,
    Supervisor,
    _listening_process_ids,
    _worktree_is_clean,
)
from app.storage import database as db
from app.tools import powershell, projects


def test_service_configuration_defaults_and_validation(tmp_path: Path) -> None:
    config_path = tmp_path / "operator.yaml"
    config_path.write_text("server:\n  host: 127.0.0.1\n", encoding="utf-8")
    config = load_operator_config(config_path)
    assert config["service"]["task_name"] == "gpt-local-code-operator"
    assert config["service"]["restart"]["max_delay_seconds"] == 60.0
    assert config["logging"]["backup_count"] == 5

    config_path.write_text("server:\n  host: 0.0.0.0\n", encoding="utf-8")
    with pytest.raises(ValueError, match="loopback"):
        load_operator_config(config_path)


def test_atomic_status_and_stop_request(tmp_path: Path) -> None:
    store = ServiceStateStore(tmp_path / "service")
    store.write_status({"state": "healthy", "counter": 1})
    assert store.read_status() == {"state": "healthy", "counter": 1}
    assert not list(store.root.glob("*.tmp"))

    request = store.request_stop(os.getpid())
    assert request["requester_pid"] == os.getpid()
    assert store.stop_requested()
    store.clear_stop_request()
    assert not store.stop_requested()


def test_status_is_always_parseable_during_replacement(tmp_path: Path) -> None:
    store = ServiceStateStore(tmp_path / "service")
    store.write_status({"counter": -1})

    def write_many() -> None:
        for counter in range(100):
            store.write_status({"counter": counter, "payload": "x" * 1000})

    writer = threading.Thread(target=write_many)
    writer.start()
    while writer.is_alive():
        status = store.read_status()
        assert status is not None
        assert isinstance(status["counter"], int)
    writer.join()


def test_service_lock_rejects_second_owner(tmp_path: Path) -> None:
    first = ServiceLock(tmp_path / "service.lock")
    second = ServiceLock(tmp_path / "service.lock")
    assert first.acquire()
    try:
        assert not second.acquire()
    finally:
        first.release()
    assert second.acquire()
    second.release()


def test_process_identity_guards_against_pid_reuse() -> None:
    identity = process_creation_identity(os.getpid())
    assert identity is not None
    assert process_matches(os.getpid(), identity)
    assert not process_matches(os.getpid(), identity + "-wrong")
    assert not process_matches(0, identity)


def test_exited_process_is_not_reported_as_live() -> None:
    process = subprocess.Popen([sys.executable, "-c", "pass"])
    identity = process_creation_identity(process.pid)
    assert identity is not None
    process.wait(timeout=5)
    assert not process_matches(process.pid, identity)


def test_component_logger_redacts_runtime_key(tmp_path: Path) -> None:
    secret = "secret-phase5-token"
    logger = configure_component_logger("redaction-test", secrets=[secret], logs_dir=tmp_path)
    logger.info("direct=%s CONTROL_PLANE_API_KEY=%s", secret, secret)
    for handler in logger.handlers:
        handler.flush()
    text = (tmp_path / "redaction-test.log").read_text(encoding="utf-8")
    assert secret not in text
    assert "[REDACTED]" in text


def test_restart_backoff_is_bounded(tmp_path: Path) -> None:
    config = load_operator_config()
    config["service"]["restart"] = {
        "initial_delay_seconds": 2,
        "multiplier": 2,
        "max_delay_seconds": 5,
        "stable_reset_seconds": 300,
    }
    supervisor = Supervisor(base_dir=tmp_path, config=config, reconciler=lambda: {})
    child = ChildRuntime("fixture", ["fixture"])
    supervisor._schedule_restart(child, 10)
    assert child.next_start_monotonic == 12
    supervisor._schedule_restart(child, 20)
    assert child.next_start_monotonic == 24
    supervisor._schedule_restart(child, 30)
    assert child.next_start_monotonic == 35
    assert child.consecutive_failures == 3


def test_redacting_formatter_is_attached_only_once(tmp_path: Path) -> None:
    first = configure_component_logger("same", logs_dir=tmp_path)
    second = configure_component_logger("same", logs_dir=tmp_path)
    assert first is second
    assert len(second.handlers) == 1
    assert isinstance(second.handlers[0], logging.Handler)


def test_component_log_rotates(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        logging_config,
        "get_logging_config",
        lambda: {
            "level": "INFO",
            "retention_days": 14,
            "max_file_bytes": 200,
            "backup_count": 2,
        },
    )
    logger = configure_component_logger("rotation-test", logs_dir=tmp_path)
    for index in range(20):
        logger.info("rotation-line-%s %s", index, "x" * 80)
    for handler in logger.handlers:
        handler.flush()
    assert (tmp_path / "rotation-test.log").is_file()
    assert (tmp_path / "rotation-test.log.1").is_file()


def test_recovery_marks_incomplete_processes_interrupted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    existing = getattr(db._local, "conn", None)
    if existing is not None:
        existing.close()
        db._local.conn = None
    monkeypatch.setattr(db, "_DB_PATH", tmp_path / "operator.db")
    db.init_db()
    db.insert_workspace("ws-00000001", "fixture", "task", str(tmp_path), "abc")
    db.insert_process("pr-00000001", "ws-00000001", "run_pwsh")
    db.insert_process("pr-00000002", "ws-00000001", "run_check")
    db.update_process_status("pr-00000002", "running", pid=123)

    assert len(db.list_incomplete_processes()) == 2
    assert db.interrupt_incomplete_processes() == 2
    first = db.get_process("pr-00000001")
    second = db.get_process("pr-00000002")
    assert first is not None and first["status"] == "interrupted"
    assert second is not None and second["status"] == "interrupted"
    db._local.conn.close()
    db._local.conn = None


@pytest.mark.asyncio
async def test_ping_output_schema_accepts_structured_envelope() -> None:
    mcp = FastMCP("schema-test")
    projects.register_tools(mcp)
    ping = next(tool for tool in await mcp.list_tools() if tool.name == "ping")
    assert ping.outputSchema["additionalProperties"] is True


@pytest.mark.asyncio
async def test_waiting_powershell_tool_does_not_block_event_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def slow_run(**_kwargs: object) -> dict[str, object]:
        time.sleep(0.2)
        return {"ok": True}

    monkeypatch.setattr(powershell, "_run_pwsh", slow_run)
    mcp = FastMCP("nonblocking-tool-test")
    powershell.register_tools(mcp)

    call = asyncio.create_task(
        mcp._tool_manager.call_tool(
            "run_pwsh",
            {"workspace_id": "ws-test", "script": "Start-Sleep 1"},
        )
    )
    started = time.monotonic()
    await asyncio.sleep(0.03)

    assert time.monotonic() - started < 0.15
    await call


def test_dirty_worktree_is_never_cleanup_eligible(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "phase5@example.invalid"], cwd=tmp_path, check=True
    )
    subprocess.run(["git", "config", "user.name", "Phase 5 Test"], cwd=tmp_path, check=True)
    tracked = tmp_path / "tracked.txt"
    tracked.write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.txt"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "base"], cwd=tmp_path, check=True)
    assert _worktree_is_clean(tmp_path)

    (tmp_path / "untracked.txt").write_text("must be retained\n", encoding="utf-8")
    assert not _worktree_is_clean(tmp_path)


@pytest.mark.skipif(os.name != "nt", reason="Windows exposes listener PIDs through netstat")
def test_listener_owner_reports_the_exact_server_process() -> None:
    import socket
    import time

    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = int(reservation.getsockname()[1])
    code = (
        "from http.server import ThreadingHTTPServer,SimpleHTTPRequestHandler;"
        f"ThreadingHTTPServer(('127.0.0.1',{port}),SimpleHTTPRequestHandler).serve_forever()"
    )
    process = subprocess.Popen([sys.executable, "-c", code])
    try:
        deadline = time.monotonic() + 5
        owners: set[int] | None = set()
        while time.monotonic() < deadline:
            owners = _listening_process_ids("127.0.0.1", port)
            if owners:
                break
            time.sleep(0.05)
        assert owners == {process.pid}
    finally:
        process.terminate()
        process.wait(timeout=5)
