"""MCP tool for durable workspace event-stream reads."""

from __future__ import annotations

import asyncio
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from app.services.envelope import error_result, ok_result
from app.services.event_store import (
    CursorExpiredError,
    EventWaitLimitError,
    InvalidCursorError,
)
from app.services.process_manager import ProcessManager
from app.services.workspace_manager import get_workspace


def _get_events(
    workspace_id: str,
    cursor: str | None = None,
    process_id: str | None = None,
    event_types: list[str] | None = None,
    limit: int = 100,
    wait_seconds: float = 0,
    from_beginning: bool = False,
) -> dict[str, Any]:
    """Read or long-poll one page of workspace events."""
    if get_workspace(workspace_id) is None:
        return error_result(
            "WORKSPACE_NOT_FOUND",
            f"workspace not found: {workspace_id}",
            workspace_id=workspace_id,
        )
    manager = ProcessManager.get_instance()
    if process_id is not None:
        record = manager.get_record(process_id)
        if record is None:
            return error_result(
                "PROCESS_NOT_FOUND",
                f"process not found: {process_id}",
                workspace_id=workspace_id,
                extra={"process_id": process_id},
            )
        if str(record["workspace_id"]) != workspace_id:
            return error_result(
                "PERMISSION_DENIED",
                "process does not belong to the requested workspace",
                workspace_id=workspace_id,
                extra={"process_id": process_id},
            )
    if limit < 1 or wait_seconds < 0:
        return error_result(
            "INVALID_INPUT",
            "limit must be positive and wait_seconds must not be negative",
            workspace_id=workspace_id,
        )
    try:
        page = manager.event_store.wait_after(
            cursor,
            workspace_id=workspace_id,
            process_id=process_id,
            event_types=event_types,
            limit=limit,
            timeout_seconds=wait_seconds,
            from_beginning=from_beginning,
        )
    except InvalidCursorError as exc:
        return error_result("INVALID_CURSOR", str(exc), workspace_id=workspace_id)
    except CursorExpiredError as exc:
        return error_result(
            "EVENT_CURSOR_EXPIRED",
            str(exc),
            retryable=True,
            workspace_id=workspace_id,
            extra={"recovery_cursor": exc.recovery_cursor},
        )
    except EventWaitLimitError as exc:
        return error_result(
            "RATE_LIMITED",
            str(exc),
            retryable=True,
            workspace_id=workspace_id,
        )
    except ValueError as exc:
        return error_result("INVALID_INPUT", str(exc), workspace_id=workspace_id)
    return ok_result(
        page,
        workspace_id=workspace_id,
        next_cursor=str(page["cursor"]) if page.get("has_more") else None,
    )


def get_events(
    workspace_id: str,
    cursor: str | None = None,
    process_id: str | None = None,
    event_types: list[str] | None = None,
    limit: int = 100,
    wait_seconds: float = 0,
    from_beginning: bool = False,
) -> dict[str, Any]:
    """Public synchronous event cursor API used by local integrations."""
    return _get_events(
        workspace_id,
        cursor,
        process_id,
        event_types,
        limit,
        wait_seconds,
        from_beginning,
    )


def subscribe_process(
    process_id: str,
    cursor: str | None = None,
    event_types: list[str] | None = None,
    limit: int = 100,
    wait_seconds: float = 25,
    from_beginning: bool = False,
) -> dict[str, Any]:
    """Thin process-scoped wrapper over :func:`get_events`."""
    record = ProcessManager.get_instance().get_record(process_id)
    if record is None:
        return error_result(
            "PROCESS_NOT_FOUND",
            f"process not found: {process_id}",
            extra={"process_id": process_id},
        )
    return _get_events(
        str(record["workspace_id"]),
        cursor,
        process_id,
        event_types,
        limit,
        wait_seconds,
        from_beginning,
    )


def register_tools(mcp: FastMCP) -> None:
    """Register the event-stream read tool."""

    @mcp.tool(
        name="get_events",
        description=(
            "Read or long-poll the durable event stream for one workspace. "
            "Pass the opaque cursor returned by the previous call. With "
            "wait_seconds > 0, the call waits until a matching event arrives "
            "or the bounded timeout expires. Use process_id and event_types "
            "to narrow the subscription. cursor=null starts at the current "
            "tail; set from_beginning=true to read retained history."
        ),
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    async def get_events(
        workspace_id: str,
        cursor: str | None = None,
        process_id: str | None = None,
        event_types: list[str] | None = None,
        limit: int = 100,
        wait_seconds: float = 0,
        from_beginning: bool = False,
    ) -> dict[str, Any]:
        return await asyncio.to_thread(
            _get_events,
            workspace_id,
            cursor,
            process_id,
            event_types,
            limit,
            wait_seconds,
            from_beginning,
        )

    @mcp.tool(
        name="subscribe_process",
        description=(
            "Long-poll events for one process. This is a thin wrapper over "
            "get_events with identical opaque cursor and timeout semantics. "
            "Use the process_id returned by run_pwsh, run_command, or run_check."
        ),
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    async def subscribe_process_tool(
        process_id: str,
        cursor: str | None = None,
        event_types: list[str] | None = None,
        limit: int = 100,
        wait_seconds: float = 25,
        from_beginning: bool = False,
    ) -> dict[str, Any]:
        return await asyncio.to_thread(
            subscribe_process,
            process_id,
            cursor,
            event_types,
            limit,
            wait_seconds,
            from_beginning,
        )
