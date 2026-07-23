"""Unified response envelope for all MCP tools.

Provides ``ok_result`` and ``error_result`` helpers so every tool returns
a consistent structure.  When an error is returned, the ``error`` block
carries a stable error code that GPT can branch on programmatically,
rather than free-text parsing.

Stable error codes
------------------
- ``WORKSPACE_NOT_FOUND`` — workspace_id does not exist in the database
- ``STALE_WORKSPACE`` — workspace record exists but the on-disk worktree is gone
- ``PROJECT_NOT_FOUND`` — project_id is not registered in projects.yaml
- ``FILE_CHANGED`` — file content SHA-256 does not match the caller's expectation
- ``PATCH_CONFLICT`` — ``git apply --check`` rejected the patch
- ``PATH_DENIED`` — path is absolute, traverses ``..``, or is on the deny list
- ``PROCESS_NOT_FOUND`` — process_id does not exist
- ``PROCESS_NOT_RUNNING`` — process exists but is no longer accepting control/input
- ``PTY_NOT_ACTIVE`` — process exists but has no active pseudo-terminal
- ``PROCESS_TIMEOUT`` — command exceeded its allowed runtime
- ``PROCESS_CANCELLED`` — command was explicitly cancelled
- ``FILE_NOT_FOUND`` — requested file or artifact does not exist
- ``PERMISSION_DENIED`` — the active permission profile rejected the operation
- ``OUTPUT_TRUNCATED`` — response was truncated to stay within size limits
- ``CHECK_FAILED`` — ``run_check`` completed with a non-zero exit code
- ``CHECK_NOT_FOUND`` — check_id does not exist in the project config
- ``INVALID_INPUT`` — missing or malformed parameters
- ``IDEMPOTENCY_KEY_MISMATCH`` — same idempotency_key used with different input
- ``INVALID_CURSOR`` — pagination/event cursor is malformed or unsupported
- ``EVENT_CURSOR_EXPIRED`` — event retention removed history required by the cursor
- ``INTERNAL_ERROR`` — unexpected server-side failure
- ``RATE_LIMITED`` — too many concurrent operations
- ``NOT_IMPLEMENTED`` — feature is not yet available
"""

from __future__ import annotations

import secrets
import time
from typing import Any


def generate_request_id() -> str:
    """Return a short unique request identifier."""
    return "req_" + secrets.token_hex(8)


def ok_result(
    result: Any,
    *,
    workspace_id: str | None = None,
    request_id: str | None = None,
    revision: int | None = None,
    warnings: list[str] | None = None,
    truncated: bool = False,
    next_cursor: str | None = None,
) -> dict[str, Any]:
    """Wrap a successful tool result in the standard envelope.

    Parameters
    ----------
    result:
        The tool's primary return value.  This is placed under the
        ``result`` key.
    workspace_id:
        Optional workspace context.
    request_id:
        Optional unique request identifier.  Auto-generated if omitted.
    revision:
        Optional workspace revision counter.
    warnings:
        Optional list of non-fatal warning messages.
    truncated:
        ``True`` when the result was truncated to stay within size limits.
    next_cursor:
        Opaque pagination cursor for multi-page results.
    """
    envelope: dict[str, Any] = {
        "ok": True,
        "request_id": request_id or generate_request_id(),
        "result": result,
        "warnings": warnings or [],
        "truncated": truncated,
        "next_cursor": next_cursor,
    }
    if workspace_id is not None:
        envelope["workspace_id"] = workspace_id
    if revision is not None:
        envelope["revision"] = revision
    return envelope


def error_result(
    code: str,
    message: str,
    *,
    retryable: bool = False,
    suggested_next_tool: str | None = None,
    workspace_id: str | None = None,
    request_id: str | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Wrap a tool error in the standard envelope.

    Parameters
    ----------
    code:
        One of the stable error codes listed in the module docstring.
    message:
        Human-readable explanation of the error.
    retryable:
        ``True`` if the caller can reasonably retry the same operation.
    suggested_next_tool:
        Optional name of the tool the caller should use next (e.g.
        ``read_files`` when a patch conflicts).
    workspace_id:
        Optional workspace context.
    request_id:
        Optional unique request identifier.  Auto-generated if omitted.
    extra:
        Optional additional fields to merge into the ``error`` block.
    """
    error: dict[str, Any] = {
        "code": code,
        "message": message,
        "retryable": retryable,
    }
    if suggested_next_tool is not None:
        error["suggested_next_tool"] = suggested_next_tool
    if extra:
        error.update(extra)

    envelope: dict[str, Any] = {
        "ok": False,
        "request_id": request_id or generate_request_id(),
        "error": error,
    }
    if workspace_id is not None:
        envelope["workspace_id"] = workspace_id
    return envelope


def elapsed_ms(start: float) -> int:
    """Return the elapsed time in milliseconds since *start* (from ``time.monotonic()``)."""
    return int((time.monotonic() - start) * 1000)


def audit_event(
    *,
    tool_name: str,
    request_id: str,
    workspace_id: str | None,
    input_summary: str,
    success: bool,
    duration_ms: int,
    error_code: str | None = None,
    result_status: str | None = None,
) -> None:
    """Write a redacted audit event without changing the tool result.

    Tools pass summaries such as ``text_len=12`` rather than raw user input.
    Audit failures are intentionally swallowed so a logging/storage hiccup
    cannot crash the MCP server or turn a successful operation into an error.
    """
    try:
        from app.storage import database as db

        db.log_operation(
            operation_id="op_" + secrets.token_hex(8),
            tool_name=tool_name,
            summary=input_summary[:500],
            workspace_id=workspace_id,
            success=success,
            request_id=request_id,
            actor="mcp",
            input_summary=input_summary[:500],
            result_status=result_status or ("success" if success else "error"),
            duration_ms=duration_ms,
            error_code=error_code,
        )
    except Exception:
        # Audit must be best-effort and never expose a database exception to
        # the caller of an otherwise valid tool operation.
        return
