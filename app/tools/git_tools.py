"""MCP tools: git_status and git_diff.

Both run inside a workspace's worktree directory and return the literal
output of the underlying git command so GPT can see real repository
state rather than a curated summary.
"""

from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from app.services.path_guard import is_denied, resolve_within
from app.services.workspace_manager import get_workspace

log = logging.getLogger(__name__)

# Cap captured output to keep individual MCP responses bounded.
_MAX_BYTES = 200_000


def _git_executable() -> str:
    return os.environ.get("GIT_EXECUTABLE", "git")


def _run(worktree: Path, args: list[str], *, timeout: int = 30) -> dict[str, Any]:
    cmd = [_git_executable(), *args]
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(worktree),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except FileNotFoundError:
        return {"error": "git executable not found", "command": cmd}
    except subprocess.TimeoutExpired:
        return {"error": f"git timed out after {timeout}s", "command": cmd}
    stdout = proc.stdout[:_MAX_BYTES]
    stderr = proc.stderr[:_MAX_BYTES]
    return {
        "exit_code": proc.returncode,
        "stdout": stdout,
        "stderr": stderr,
        "stdout_truncated": len(proc.stdout) > _MAX_BYTES,
        "stderr_truncated": len(proc.stderr) > _MAX_BYTES,
    }


def _ensure_worktree(workspace_id: str) -> tuple[Path | None, dict[str, Any] | None]:
    record = get_workspace(workspace_id)
    if record is None:
        return None, {
            "error": f"workspace not found: {workspace_id}",
            "workspace_id": workspace_id,
        }
    worktree = Path(record["worktree_path"])
    if not worktree.exists():
        return None, {
            "error": f"worktree path missing on disk: {worktree}",
            "workspace_id": workspace_id,
        }
    return worktree, None


def _git_status(workspace_id: str) -> dict[str, Any]:
    worktree, err = _ensure_worktree(workspace_id)
    if err is not None:
        return err
    assert worktree is not None
    result = _run(worktree, ["status", "--short", "--branch"])
    return {
        "workspace_id": workspace_id,
        "worktree_path": str(worktree),
        **result,
    }


def _git_diff(
    workspace_id: str,
    paths: list[str] | None = None,
    cached: bool = False,
) -> dict[str, Any]:
    worktree, err = _ensure_worktree(workspace_id)
    if err is not None:
        return err
    assert worktree is not None
    args = ["diff", "--no-color"]
    if cached:
        args.append("--cached")
    validated_paths: list[str] = []
    if paths:
        for p in paths:
            if not p or p.startswith("-"):
                return {
                    "error": f"invalid path argument: {p!r}",
                    "workspace_id": workspace_id,
                }
            try:
                resolved = resolve_within(worktree, p, must_exist=False)
            except ValueError as exc:
                return {"error": str(exc), "workspace_id": workspace_id}
            if is_denied(resolved, worktree):
                return {
                    "error": "path is denied by policy",
                    "workspace_id": workspace_id,
                }
            validated_paths.append(p)
    if validated_paths:
        args.extend(["--", *validated_paths])
    result = _run(worktree, args)
    return {
        "workspace_id": workspace_id,
        "worktree_path": str(worktree),
        "cached": cached,
        "paths": paths or [],
        **result,
    }


def register_tools(mcp: FastMCP) -> None:
    @mcp.tool(
        name="git_status",
        description=(
            "Run `git status --short --branch` inside the workspace. "
            "Returns the raw output so GPT can see what has actually changed."
        ),
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    async def git_status(workspace_id: str) -> dict[str, object]:
        log.info("git_status workspace_id=%s", workspace_id)
        return _git_status(workspace_id)

    @mcp.tool(
        name="git_diff",
        description=(
            "Run `git diff` (or `git diff --cached` when cached=True) inside "
            "the workspace. Optionally restrict to a list of relative paths. "
            "Output is captured verbatim for the model to inspect."
        ),
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    async def git_diff(
        workspace_id: str,
        paths: list[str] | None = None,
        cached: bool = False,
    ) -> dict[str, object]:
        log.info("git_diff workspace_id=%s paths=%s cached=%s", workspace_id, paths, cached)
        return _git_diff(workspace_id, paths, cached)
