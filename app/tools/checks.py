"""MCP tools: run_check, list_checks.

Reads pre-configured check scripts from ``config/projects.yaml`` and
executes them through the same process manager as ``run_pwsh``.

This is a convenience entry, not a security boundary — ``run_check``
delegates to the same underlying PowerShell execution as ``run_pwsh``.
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import time
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from app.config import get_process_config, get_project
from app.services.envelope import error_result, ok_result
from app.services.process_manager import ProcessManager
from app.services.workspace_manager import get_workspace
from app.storage.idempotency import with_idempotency

log = logging.getLogger(__name__)

_WAIT_POLL_INTERVAL = 0.5


def _list_checks(workspace_id: str) -> dict[str, Any]:
    """Return the available checks for the workspace's project."""
    record = get_workspace(workspace_id)
    if record is None:
        return error_result(
            "WORKSPACE_NOT_FOUND", f"workspace not found: {workspace_id}", workspace_id=workspace_id
        )

    project = get_project(record["project_id"])
    if project is None:
        return error_result(
            "PROJECT_NOT_FOUND",
            f"project config not found: {record['project_id']}",
            workspace_id=workspace_id,
        )

    checks = project.get("checks", {})
    return ok_result(
        {
            "workspace_id": workspace_id,
            "project_id": record["project_id"],
            "checks": [
                {
                    "check_id": cid,
                    "timeout_seconds": info.get("timeout_seconds", 600),
                    "script_preview": (info.get("script", "") or "")[:200],
                    "description": info.get("description", ""),
                    "result_semantics": info.get("result_semantics", "command_exit"),
                    "environment_keys": sorted((info.get("env") or {}).keys()),
                }
                for cid, info in checks.items()
            ],
        },
        workspace_id=workspace_id,
    )


def _run_check(
    workspace_id: str,
    check_id: str,
    wait: bool = True,
) -> dict[str, Any]:
    """Execute a pre-configured check script from the project config.

    See the ``run_check`` tool docstring for parameter details.
    """
    # ---- 1. Look up workspace and project ----
    record = get_workspace(workspace_id)
    if record is None:
        return error_result(
            "WORKSPACE_NOT_FOUND", f"workspace not found: {workspace_id}", workspace_id=workspace_id
        )
    worktree = Path(record["worktree_path"])
    if not worktree.exists():
        return error_result(
            "STALE_WORKSPACE",
            f"worktree path missing on disk: {worktree}",
            workspace_id=workspace_id,
        )

    project = get_project(record["project_id"])
    if project is None:
        return error_result(
            "PROJECT_NOT_FOUND",
            f"project config not found: {record['project_id']}",
            workspace_id=workspace_id,
        )

    # ---- 2. Look up check config ----
    checks = project.get("checks", {})
    check_cfg = checks.get(check_id)
    if check_cfg is None:
        available = ", ".join(sorted(checks.keys()))
        return error_result(
            "CHECK_NOT_FOUND",
            f"check_id {check_id!r} not found in project "
            f"{record['project_id']!r}. Available checks: [{available}]",
            workspace_id=workspace_id,
            extra={"check_id": check_id, "available_checks": sorted(checks.keys())},
        )

    script = check_cfg.get("script", "").strip()
    if not script:
        return error_result(
            "INVALID_INPUT",
            f"check {check_id!r} has an empty script",
            workspace_id=workspace_id,
            extra={"check_id": check_id},
        )

    timeout = check_cfg.get("timeout_seconds")
    if timeout is None:
        timeout = int(get_process_config().get("default_timeout_seconds", 600))
    timeout = min(timeout, int(get_process_config().get("max_timeout_seconds", 3600)))

    # ---- 3. Resolve pwsh path ----
    pwsh_cfg = project.get("pwsh", {})
    pwsh_path = pwsh_cfg.get("executable", "pwsh.exe")

    configured_env = check_cfg.get("env") or {}
    if not isinstance(configured_env, dict) or not all(
        isinstance(key, str) and isinstance(value, (str, int, float, bool))
        for key, value in configured_env.items()
    ):
        return error_result(
            "INVALID_INPUT",
            f"check {check_id!r} has an invalid env mapping",
            workspace_id=workspace_id,
            extra={"check_id": check_id},
        )
    check_env = {str(key): str(value) for key, value in configured_env.items()}
    check_env.setdefault("PYTHONDONTWRITEBYTECODE", "1")

    # ---- 4. Spawn via ProcessManager ----
    pm = ProcessManager.get_instance()

    try:
        spawn_result = pm.spawn(
            workspace_id=workspace_id,
            worktree_path=worktree,
            script=script,
            pwsh_path=pwsh_path,
            working_directory=None,
            timeout_seconds=timeout,
            env=check_env,
            tool_name=f"run_check:{check_id}",
            concurrency_write=False,
            priority=10,
        )
    except RuntimeError as exc:
        return error_result(
            "RATE_LIMITED", str(exc), workspace_id=workspace_id, extra={"check_id": check_id}
        )

    process_id = spawn_result["process_id"]

    # ---- 5. Wait (if requested) ----
    if wait:
        terminal_statuses = {
            "passed",
            "failed",
            "timed_out",
            "cancelled",
            "resource_exhausted",
            "interrupted",
            "lost",
            "recovery_required",
        }
        deadline = time.monotonic() + timeout + 5
        while time.monotonic() < deadline:
            result = pm.get_result(process_id)
            status = result.get("status", "")
            if status in terminal_statuses:
                # Add check-specific metadata.
                result["check_id"] = check_id
                result["workspace_id"] = workspace_id
                if check_cfg.get("result_semantics") == "git_diff_check":
                    result["git_hygiene"] = _git_hygiene(worktree, result)
                return ok_result(result, workspace_id=workspace_id)
            time.sleep(_WAIT_POLL_INTERVAL)

        pm.cancel(process_id)
        result = pm.get_result(process_id)
        result["check_id"] = check_id
        result["workspace_id"] = workspace_id
        if check_cfg.get("result_semantics") == "git_diff_check":
            result["git_hygiene"] = _git_hygiene(worktree, result)
        return ok_result(result, workspace_id=workspace_id)

    return ok_result(
        {
            "process_id": process_id,
            "status": "running",
            "workspace_id": workspace_id,
            "check_id": check_id,
        },
        workspace_id=workspace_id,
    )


def _git_hygiene(worktree: Path, process_result: dict[str, Any]) -> dict[str, Any]:
    """Return explicit working-tree semantics for a Git diff check."""
    try:
        status = subprocess.run(
            [os.environ.get("GIT_EXECUTABLE", "git"), "status", "--porcelain=v1"],
            cwd=str(worktree),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return {"error": str(exc)}

    entries: list[dict[str, str]] = []
    for line in status.stdout.splitlines():
        if len(line) < 3:
            continue
        code = line[:2]
        path = line[3:]
        entries.append({"path": path, "status_code": code})
    return {
        "diff_check_passed": process_result.get("exit_code") == 0,
        "working_tree_clean": not entries,
        "changed_files": entries,
        "untracked_files": [entry["path"] for entry in entries if entry["status_code"] == "??"],
    }


# ── Tool registration ──────────────────────────────────────────────────


def register_tools(mcp: FastMCP) -> None:
    """Register check-related tools on the FastMCP instance."""

    @mcp.tool(
        name="list_checks",
        description=(
            "Return the list of pre-configured checks available for the "
            "workspace's project.  Each check has a ``check_id``, a timeout, "
            "and a preview of the script that will be executed.  Use this "
            "before calling ``run_check`` to discover the available check IDs."
        ),
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    async def list_checks(workspace_id: str) -> dict[str, object]:
        """List available checks for the workspace's project.

        Args:
            workspace_id: The target workspace.
        """
        log.info("list_checks workspace_id=%s", workspace_id)
        return _list_checks(workspace_id)

    @mcp.tool(
        name="run_check",
        description=(
            "Execute a pre-configured check script (e.g. unit tests, lint, "
            "type check) from the project configuration.  The script is "
            "defined in ``config/projects.yaml`` under the project's "
            "``checks`` section.  Use ``list_checks`` to discover the "
            "available check IDs.\n\n"
            "This is a convenience wrapper around ``run_pwsh`` — the same "
            "process manager, timeout, and output capture apply.  It is not "
            "a security boundary.\n\n"
            "Parameters:\n"
            "- ``wait=true`` (default): block until the check finishes and "
            "return the full result including exit code and output tails.\n"
            "- ``wait=false``: start the check in the background and return "
            "a ``process_id`` immediately.  Use ``get_process_result`` to "
            "poll for completion."
        ),
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=True,
            idempotentHint=False,
            openWorldHint=True,
        ),
    )
    async def run_check(
        workspace_id: str,
        check_id: str,
        wait: bool = True,
        idempotency_key: str | None = None,
    ) -> dict[str, object]:
        """Execute a pre-configured check script.

        Args:
            workspace_id: The target workspace.
            check_id: The check identifier from the project config
                (e.g. ``unit_tests``, ``lint``).
            wait: When ``True`` (default), block until the check finishes
                and return the full result.  When ``False``, return
                immediately with a ``process_id`` for later polling.
            idempotency_key: Optional key for idempotent retry.
        """
        log.info(
            "run_check workspace_id=%s check_id=%s wait=%s idempotency_key=%s",
            workspace_id,
            check_id,
            wait,
            idempotency_key,
        )
        return await asyncio.to_thread(
            with_idempotency,
            idempotency_key,
            "run_check",
            {"workspace_id": workspace_id, "check_id": check_id, "wait": wait},
            lambda: _run_check(workspace_id, check_id, wait),
        )
