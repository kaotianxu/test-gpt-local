"""MCP tools: run_pwsh, get_process_result, cancel_process.

Provides PowerShell 7 execution on the user's trusted development machine.
The process starts in the selected Git worktree and inherits the user's
proxy and development environment.  It is not sandboxed.

Tools
-----
- ``run_pwsh`` — execute a PowerShell script (sync or async via ``wait``)
- ``get_process_result`` — poll for completion / read output
- ``cancel_process`` — terminate a running process tree
"""

from __future__ import annotations

import hashlib
import logging
import subprocess
import time
from pathlib import Path
from typing import Any, cast

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from app.config import get_process_config, get_project
from app.services.envelope import error_result, ok_result
from app.services.path_guard import is_denied, resolve_within
from app.services.process_manager import ProcessManager
from app.services.workspace_manager import get_workspace
from app.storage.idempotency import with_idempotency

log = logging.getLogger(__name__)

# Poll interval (seconds) when waiting for a process to finish.
_WAIT_POLL_INTERVAL = 0.5


def _resolve_pwsh(project: dict[str, Any]) -> str:
    """Return the pwsh executable path from the project config, or the default."""
    pwsh_cfg = project.get("pwsh", {})
    return cast(str, pwsh_cfg.get("executable", "pwsh.exe"))


def _git_status(worktree: Path) -> str | None:
    """Run ``git status --short --branch`` and return the stdout, or None."""
    try:
        result = subprocess.run(
            ["git", "status", "--short", "--branch"],
            cwd=str(worktree),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
        )
        return result.stdout.strip() or None
    except Exception:
        return None


def _run_pwsh(
    workspace_id: str,
    script: str,
    working_directory: str = "",
    timeout_seconds: int = 600,
    wait: bool = False,
) -> dict[str, Any]:
    """Execute a PowerShell script in the workspace.

    See the ``run_pwsh`` tool docstring for parameter details.
    """
    # ---- 1. Look up workspace ----
    record = get_workspace(workspace_id)
    if record is None:
        return error_result("WORKSPACE_NOT_FOUND", f"workspace not found: {workspace_id}", workspace_id=workspace_id)
    worktree = Path(record["worktree_path"])
    if not worktree.exists():
        return error_result("STALE_WORKSPACE", f"worktree path missing on disk: {worktree}", workspace_id=workspace_id)

    project = get_project(record["project_id"])
    if project is None:
        return error_result("PROJECT_NOT_FOUND", f"project config not found: {record['project_id']}", workspace_id=workspace_id)

    # ---- 2. Validate inputs ----
    if not script or not script.strip():
        return error_result("INVALID_INPUT", "script must be a non-empty string", workspace_id=workspace_id)

    timeout = min(
        max(timeout_seconds, 1),
        int(get_process_config().get("max_timeout_seconds", 3600)),
    )

    # ---- 3. Resolve working directory ----
    cwd = None
    if working_directory:
        try:
            resolved = resolve_within(worktree, working_directory, must_exist=False)
            if is_denied(resolved, worktree):
                return error_result("PATH_DENIED", "working_directory is denied by policy", workspace_id=workspace_id)
            cwd = str(resolved)
        except ValueError as exc:
            return error_result("PATH_DENIED", str(exc), workspace_id=workspace_id)

    # ---- 4. Spawn the process ----
    pm = ProcessManager.get_instance()
    pwsh_path = _resolve_pwsh(project)

    try:
        spawn_result = pm.spawn(
            workspace_id=workspace_id,
            worktree_path=worktree,
            script=script,
            pwsh_path=pwsh_path,
            working_directory=cwd,
            timeout_seconds=timeout,
        )
    except RuntimeError as exc:
        return error_result("RATE_LIMITED", str(exc), workspace_id=workspace_id)

    process_id = spawn_result["process_id"]

    # ---- 5. Wait (if requested) ----
    if wait:
        # Poll until the status changes from "running"/"queued".
        terminal_statuses = {"passed", "failed", "timed_out", "cancelled"}
        deadline = time.monotonic() + timeout + 5  # extra buffer
        while time.monotonic() < deadline:
            result = pm.get_result(process_id)
            status = result.get("status", "")
            if status in terminal_statuses:
                # Add git status after.
                result["git_status_after"] = _git_status(worktree)
                return ok_result(result, workspace_id=workspace_id)
            # Short sleep to avoid busy-waiting.
            time.sleep(_WAIT_POLL_INTERVAL)

        # Timed out waiting (should not normally happen since the watchdog
        # handles the timeout, but guard against races).
        pm.cancel(process_id)
        result = pm.get_result(process_id)
        result["git_status_after"] = _git_status(worktree)
        return ok_result(result, workspace_id=workspace_id)

    # Async: return immediately with the process_id.
    return ok_result(
        {
            "process_id": process_id,
            "status": "running",
            "workspace_id": workspace_id,
        },
        workspace_id=workspace_id,
    )


def _get_process_result(
    process_id: str,
    tail_chars: int = 50000,
) -> dict[str, Any]:
    """Return the current state of a process.

    See the ``get_process_result`` tool docstring for parameter details.
    """
    pm = ProcessManager.get_instance()
    cfg = get_process_config()
    max_tail = int(cfg.get("max_output_chars", 200000))
    tail = min(max(tail_chars, 1), max_tail)

    result = pm.get_result(process_id, tail_chars=tail)

    # If the process has finished, attach git_status_after.
    terminal = {"passed", "failed", "timed_out", "cancelled"}
    if result.get("status") in terminal:
        # We need to find the workspace for this process to run git status.
        # The process record has workspace_id but not the worktree path.
        # We'll look it up from the DB record.
        from app.storage import database as db

        proc_record = db.get_process(process_id)
        if proc_record and proc_record.get("workspace_id"):
            ws = get_workspace(proc_record["workspace_id"])
            if ws:
                wt = Path(ws["worktree_path"])
                if wt.exists():
                    result["git_status_after"] = _git_status(wt)

    if "error" in result:
        return error_result("PROCESS_TIMEOUT" if "not found" in str(result.get("error", "")) else "INTERNAL_ERROR", str(result.get("error", "")), extra={"process_id": process_id})
    return ok_result(result, workspace_id=proc_record.get("workspace_id") if proc_record else None)


def _read_process_output(
    process_id: str,
    stream: str = "stdout",
    offset: int = 0,
    max_chars: int = 50000,
) -> dict[str, Any]:
    """Read a segment of a process's output file.

    Args:
        process_id: The process ID returned by ``run_pwsh``.
        stream: Which stream to read — ``"stdout"`` or ``"stderr"``.
        offset: Character offset from the start of the file.
        max_chars: Maximum number of characters to return.
    """
    if stream not in ("stdout", "stderr"):
        return error_result("INVALID_INPUT", f"stream must be 'stdout' or 'stderr', got: {stream!r}")

    from app.storage import database as db

    record = db.get_process(process_id)
    if record is None:
        return error_result("PROCESS_TIMEOUT" if "not found" in str(process_id) else "INVALID_INPUT", f"process not found: {process_id}", extra={"process_id": process_id})

    stream_key = "stdout_path" if stream == "stdout" else "stderr_path"
    file_path = record.get(stream_key)
    if not file_path:
        return ok_result({"process_id": process_id, "stream": stream, "content": "", "total_chars": 0})

    path = Path(file_path)
    if not path.exists():
        return ok_result({"process_id": process_id, "stream": stream, "content": "", "total_chars": 0})

    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return error_result("INTERNAL_ERROR", f"cannot read output file: {exc}", extra={"process_id": process_id, "stream": stream})

    total = len(text)
    offset = max(0, min(offset, total - 1)) if total > 0 else 0
    max_chars = max(1, min(max_chars, 200000))
    content = text[offset : offset + max_chars]
    truncated = (offset + max_chars) < total

    return ok_result({
        "process_id": process_id,
        "stream": stream,
        "offset": offset,
        "content": content,
        "total_chars": total,
        "truncated": truncated,
    })


def _cancel_process(process_id: str) -> dict[str, Any]:
    """Cancel a running process.

    See the ``cancel_process`` tool docstring for parameter details.
    """
    pm = ProcessManager.get_instance()
    result = pm.cancel(process_id)
    if "error" in result:
        return error_result("INVALID_INPUT", str(result.get("error", "")), extra={"process_id": process_id})
    return ok_result(result)


# ── Tool registration ──────────────────────────────────────────────────


def register_tools(mcp: FastMCP) -> None:
    """Register all PowerShell-related tools on the FastMCP instance."""

    @mcp.tool(
        name="run_pwsh",
        description=(
            "Execute a PowerShell 7 script on the user's trusted development"
            " machine.  The script is sent to ``pwsh.exe`` via stdin and runs"
            " in the workspace worktree directory.\n\n"
            "**Security:** The process inherits the user's environment and is"
            " NOT sandboxed.  It can access the network, filesystem, and any"
            " tools installed on the machine.  Only use this for commands"
            " relevant to the current task.\n\n"
            "**Proxy:** If the operator config has proxy enabled, the child"
            " process inherits ``HTTP_PROXY``, ``HTTPS_PROXY``, and"
            " ``NO_PROXY`` environment variables automatically.\n\n"
            "**Parameters:**\n"
            "- ``wait=true`` (default): block until the script finishes and"
            " return the full result including exit code, output tails, and"
            " git status.\n"
            "- ``wait=false``: start the script in the background and return"
            " a ``process_id`` immediately.  Use ``get_process_result`` to"
            " poll for completion.\n\n"
            "**Output:** stdout and stderr are captured to temporary files"
            " and truncated to the configured maximum size.  The response"
            " includes the tail of both streams.\n\n"
            "**Git status:** After a modifying command completes, the"
            " response includes ``git_status_after`` so you can see what"
            " changed without a separate tool call."
        ),
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=True,
            idempotentHint=False,
            openWorldHint=True,
        ),
    )
    async def run_pwsh(
        workspace_id: str,
        script: str,
        working_directory: str = "",
        timeout_seconds: int = 600,
        wait: bool = True,
        idempotency_key: str | None = None,
    ) -> dict[str, object]:
        """Execute a PowerShell script in the workspace.

        Args:
            workspace_id: The target workspace.
            script: The PowerShell script to execute (multi-line allowed).
            working_directory: Relative path inside the worktree to use as the
                working directory.  Defaults to the worktree root.
            timeout_seconds: Maximum execution time in seconds (1-3600).
                Defaults to 600.
            wait: When ``True`` (default), block until the script finishes
                and return the full result.  When ``False``, return immediately
                with a ``process_id`` for later polling.
            idempotency_key: Optional key for idempotent retry.
        """
        log.info(
            "run_pwsh workspace_id=%s wait=%s timeout=%s script_len=%d idempotency_key=%s",
            workspace_id,
            wait,
            timeout_seconds,
            len(script),
            idempotency_key,
        )
        return with_idempotency(
            idempotency_key,
            "run_pwsh",
            {
                "workspace_id": workspace_id,
                "script_sha256": hashlib.sha256(script.encode("utf-8")).hexdigest(),
                "timeout_seconds": timeout_seconds,
                "wait": wait,
            },
            lambda: _run_pwsh(
                workspace_id=workspace_id,
                script=script,
                working_directory=working_directory,
                timeout_seconds=timeout_seconds,
                wait=wait,
            ),
        )

    @mcp.tool(
        name="get_process_result",
        description=(
            "Return the current state of a previously started process."
            "  If the process is still running, its status will be"
            " ``running``.  Terminal states are ``passed``, ``failed``,"
            " ``timed_out``, and ``cancelled``.\n\n"
            "The response includes the tail of stdout and stderr."
            "  For completed processes, ``git_status_after`` is also"
            " included so you can see what changed."
        ),
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    async def get_process_result(
        process_id: str,
        tail_chars: int = 50000,
    ) -> dict[str, object]:
        """Poll for process completion and retrieve output.

        Args:
            process_id: The process ID returned by ``run_pwsh``.
            tail_chars: Maximum number of characters to return from the
                tail of stdout and stderr (default 50000, max 200000).
        """
        log.info("get_process_result process_id=%s tail_chars=%d", process_id, tail_chars)
        return _get_process_result(process_id, tail_chars)

    @mcp.tool(
        name="cancel_process",
        description=(
            "Terminate a running process and its entire child process tree"
            " using ``taskkill /T /F``.  Use this when a command is taking"
            " too long or needs to be aborted.  Idempotent — calling it on"
            " an already-finished process returns the current status."
        ),
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=True,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    async def cancel_process(
        process_id: str,
        idempotency_key: str | None = None,
    ) -> dict[str, object]:
        """Cancel a running process.

        Args:
            process_id: The process ID returned by ``run_pwsh``.
            idempotency_key: Optional key for idempotent retry.
        """
        log.info("cancel_process process_id=%s idempotency_key=%s", process_id, idempotency_key)
        return with_idempotency(
            idempotency_key,
            "cancel_process",
            {"process_id": process_id},
            lambda: _cancel_process(process_id),
        )

    @mcp.tool(
        name="read_process_output",
        description=(
            "Read a segment of a previously captured process output file. "
            "Use this to inspect specific parts of a long build log without "
            "loading the entire output into context.  Each process has an "
            "``output_artifact_id`` (same as its ``process_id``) that you "
            "use as the ``process_id`` parameter.\n\n"
            "Parameters:\n"
            "- ``process_id``: the process ID or output_artifact_id.\n"
            "- ``stream``: ``\"stdout\"`` (default) or ``\"stderr\"``.\n"
            "- ``offset``: character offset from the start of the file.\n"
            "- ``max_chars``: maximum characters to return (default 50000)."
        ),
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    async def read_process_output(
        process_id: str,
        stream: str = "stdout",
        offset: int = 0,
        max_chars: int = 50000,
    ) -> dict[str, object]:
        """Read a segment of a process output file.

        Args:
            process_id: The process ID (also used as output_artifact_id).
            stream: ``\"stdout\"`` or ``\"stderr\"``.
            offset: Character offset from the start of the file.
            max_chars: Maximum characters to return.
        """
        log.info(
            "read_process_output process_id=%s stream=%s offset=%d max_chars=%d",
            process_id,
            stream,
            offset,
            max_chars,
        )
        return _read_process_output(process_id, stream, offset, max_chars)
