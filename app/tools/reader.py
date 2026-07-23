"""MCP tool: read_files.

Reads one or more files (or file segments) from a workspace, bounded by
``files.max_read_chars`` per response. The path guard rejects escapes
and the always-denied entries (.git, .env, ...).
"""

from __future__ import annotations

import hashlib
import logging
import subprocess
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from app.config import get_files_config
from app.services.envelope import error_result, ok_result
from app.services.path_guard import is_denied, resolve_within
from app.services.subprocess_utils import no_window_creationflags
from app.services.workspace_manager import get_workspace

log = logging.getLogger(__name__)

_DEFAULT_MAX_CHARS = 100_000
_HARD_MAX_CHARS = 1_000_000
_MAX_LINES_PER_ITEM = 4000


def _read_text(path: Path, start_line: int, end_line: int) -> tuple[str, int, int, bool]:
    """Read text from ``path`` between 1-indexed start_line and end_line.

    Returns (content, total_lines, lines_returned, truncated).
    """
    raw = path.read_text(encoding="utf-8", errors="replace")
    lines = raw.splitlines()
    total = len(lines)
    start = max(1, start_line)
    end = min(end_line, total)
    if end < start:
        end = start
    end = min(end, start + _MAX_LINES_PER_ITEM)
    selected = lines[start - 1 : end]
    truncated = end < total
    return "\n".join(selected), total, len(selected), truncated


def _read_item(worktree: Path, item: dict[str, Any], remaining: int) -> dict[str, Any]:
    path = str(item.get("path", ""))
    try:
        start_line = int(item.get("start_line", 1) or 1)
        end_line = int(item.get("end_line", 0) or 0)
    except (TypeError, ValueError):
        return {"path": path, "error": "start_line and end_line must be integers"}
    if not path:
        return {"path": path, "error": "path is required"}
    if end_line <= 0:
        end_line = start_line + _MAX_LINES_PER_ITEM

    try:
        absolute = resolve_within(worktree, path)
    except ValueError as exc:
        return {"path": path, "error": str(exc)}
    if is_denied(absolute, worktree):
        return {"path": path, "error": "path is denied by policy"}
    if absolute.is_dir():
        return {"path": path, "error": "path is a directory; use get_repo_map"}
    if not absolute.is_file():
        return {"path": path, "error": "path is not a regular file"}

    try:
        raw_bytes = absolute.read_bytes()
        content, total, returned, truncated = _read_text(absolute, start_line, end_line)
    except OSError as exc:
        return {"path": path, "error": f"read failed: {exc}"}

    git_blob: str | None = None
    try:
        blob_result = subprocess.run(
            ["git", "hash-object", "--no-filters", "--", str(absolute)],
            cwd=str(worktree),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            creationflags=no_window_creationflags(),
        )
        if blob_result.returncode == 0:
            git_blob = blob_result.stdout.strip() or None
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    if len(content) > remaining:
        content = content[:remaining]
        truncated = True
    return {
        "path": path,
        "start_line": start_line,
        "end_line": start_line + returned - 1,
        "total_lines": total,
        "truncated": truncated,
        "sha256": hashlib.sha256(raw_bytes).hexdigest(),
        "git_blob": git_blob,
        "content": content,
    }


def _read_files(
    workspace_id: str,
    items: list[dict[str, Any]],
) -> dict[str, Any]:
    record = get_workspace(workspace_id)
    if record is None:
        return error_result(
            "WORKSPACE_NOT_FOUND", f"workspace not found: {workspace_id}", workspace_id=workspace_id
        )
    worktree = Path(record["worktree_path"])

    if not items:
        return error_result(
            "INVALID_INPUT", "items must be a non-empty list", workspace_id=workspace_id
        )

    cfg = get_files_config()
    max_chars = int(cfg.get("max_read_chars", _DEFAULT_MAX_CHARS))
    max_chars = min(max_chars, _HARD_MAX_CHARS)

    remaining = max_chars
    results: list[dict[str, Any]] = []
    for item in items:
        result = _read_item(worktree, item, remaining)
        results.append(result)
        if "content" in result:
            remaining -= len(result["content"])
            if remaining <= 0:
                break

    return ok_result(
        {
            "workspace_id": workspace_id,
            "files": results,
            "remaining_chars": max(remaining, 0),
        },
        workspace_id=workspace_id,
    )


def register_tools(mcp: FastMCP) -> None:
    @mcp.tool(
        name="read_files",
        description=(
            "Read one or more files (or line ranges) from a workspace. Each "
            "item is {path, start_line, end_line} where line numbers are "
            "1-indexed and inclusive. The response is bounded by "
            "files.max_read_chars. .git and .env paths are refused."
            " Each successful item includes a SHA-256 and Git blob ID for"
            " optimistic concurrency checks."
        ),
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    async def read_files(
        workspace_id: str,
        items: list[dict[str, object]],
    ) -> dict[str, object]:
        log.info("read_files workspace_id=%s items=%d", workspace_id, len(items))
        return _read_files(workspace_id, [dict(i) for i in items])
