from __future__ import annotations

import asyncio
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from mcp.server.fastmcp import FastMCP

from app.services.event_store import EventStore
from app.services.process_manager import ProcessManager
from app.storage.database import Database
from app.tools import events


@pytest.fixture
def configured_events(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[Database, EventStore]]:
    database = Database(tmp_path / "events-tool.db")
    database.init_db()
    workspace = database.insert_workspace(
        "ws-00000001", "project", "events", str(tmp_path), "deadbeef"
    )
    database.insert_workspace(
        "ws-00000002", "project", "other", str(tmp_path), "deadbeef"
    )
    store = EventStore(database)
    manager = ProcessManager(
        database=database,
        event_store=store,
        processes_dir=tmp_path / "processes",
        config={
            "max_running_jobs": 1,
            "max_running_jobs_per_workspace": 1,
            "queue_timeout_seconds": 0,
            "heartbeat_interval_seconds": 1,
            "default_timeout_seconds": 10,
            "max_timeout_seconds": 10,
            "max_output_chars": 1000,
            "max_output_bytes": 1000,
            "register_artifacts": False,
        },
    )
    monkeypatch.setattr(ProcessManager, "_instance", manager)
    monkeypatch.setattr(
        events,
        "get_workspace",
        lambda workspace_id: workspace if workspace_id == "ws-00000001" else None,
    )
    yield database, store
    manager.shutdown(grace_seconds=0, terminate_seconds=0)
    database.close()


def test_get_events_reads_retained_history(
    configured_events: tuple[Database, EventStore],
) -> None:
    _, store = configured_events
    store.append(
        "tool.queued",
        workspace_id="ws-00000001",
        payload={"tool_name": "run_pwsh", "priority": 0},
    )
    result = events._get_events("ws-00000001", from_beginning=True)
    assert result["ok"] is True
    assert result["result"]["events"][0]["type"] == "tool.queued"


def test_get_events_timeout_is_not_an_error(
    configured_events: tuple[Database, EventStore],
) -> None:
    result = events._get_events("ws-00000001", wait_seconds=0.01)
    assert result["ok"] is True
    assert result["result"]["events"] == []
    assert result["result"]["timed_out"] is True


def test_get_events_rejects_cross_workspace_process(
    configured_events: tuple[Database, EventStore],
) -> None:
    database, _ = configured_events
    database.insert_process("pr-00000001", "ws-00000002", "run_pwsh")
    result = events._get_events(
        "ws-00000001", process_id="pr-00000001"
    )
    assert result["error"]["code"] == "PERMISSION_DENIED"


def test_get_events_maps_invalid_cursor(
    configured_events: tuple[Database, EventStore],
) -> None:
    result = events._get_events("ws-00000001", cursor="invalid")
    assert result["error"]["code"] == "INVALID_CURSOR"


@pytest.mark.asyncio
async def test_get_events_long_poll_does_not_block_event_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def slow_get_events(*_args: object, **_kwargs: object) -> dict[str, object]:
        time.sleep(0.2)
        return {"ok": True, "result": {"events": [], "timed_out": True}}

    monkeypatch.setattr(events, "_get_events", slow_get_events)
    mcp = FastMCP("event-nonblocking-test")
    events.register_tools(mcp)
    call = asyncio.create_task(
        mcp._tool_manager.call_tool(
            "get_events",
            {"workspace_id": "ws-test", "wait_seconds": 1},
        )
    )
    started = time.monotonic()
    await asyncio.sleep(0.03)
    assert time.monotonic() - started < 0.15
    await call
